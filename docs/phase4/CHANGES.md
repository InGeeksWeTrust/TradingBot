# Phase 4 changes log

Every change to the forward engine or its records during Phase 4. Each entry says what changed, why, and whether it affects comparability with earlier forward records. The frozen strategy (signals, confirmation, stops, targets, sizing, risk limits, trading window, forward-test settings) has not changed. The audit's config fingerprint is still `a6f638a7dc88463a`.

## Forward-test report and daily backup (2026-10-30)

### 1. `python -m orbbot.forward_report`

**What.** `python -m orbbot.forward_report --config config-sweep.yaml [--date D]` writes `reports/forward_<date>.md` (`orbbot/forward_report.py`). It opens `state/paper.sqlite` and `state-drill/drill.sqlite` read-only and reads `audit/*.md`.

**Sample.**
- Paper trades with session date from `forward_test.sample_start` (2026-11-02) to the report date, filled and closed.
- Never included: operational-week trades, Practice-account drills (`python -m orbbot.drill --report` stays their separate record) and research results.
- A filled trade that hasn't closed is shown but not counted.

**Sections.**
1. Trades; signals not traded, by reason; sessions run, and weekdays without a session start.
   - The reasons are: filtered by ES confirmation, `stop_too_wide`, daily limits (`daily_headroom` or halted), stale (stale data, or a bot started after the signal), account guard, entry cutoff, and trade already open.
   - A refusal recorded twice for the same signal (a restart replay) counts once.
2. Net P&L; R per trade with mean and sd; win rate; profit factor. A per-trade table shows cumulative R.
   - R = net P&L / planned stop risk (`planned_risk`, the plan's `risk_dollars`). This is the same definition research uses (`pnl / risk_usd`).
3. Drawdown in R and $ (current and max, from a peak that starts at 0) and the longest losing streak (net P&L ≤ 0).
   - These are set against the pre-registered thresholds: −20R / −26R over the first 100 trades, −26R / −34R over the first 200, and a streak of 12 / 15.
   - The report says "REACHED" or "not reached" for each and draws no conclusion from it.
4. The z of mean R (mean / (sd/√n)), against the pre-registered looks:
   - at 100 trades, using the first 100: z ≥ 2.538 succeeds, otherwise continue;
   - at 200 trades, using the first 200: z ≥ 1.662 succeeds, z ≤ −1.645 fails.
   - Before 100 trades the current z is shown with "NO decision is made until a checkpoint."
5. Execution, with drift and modeled slippage kept separate:
   - Market drift, signal close → order sent: the price the bot saw in the poll that sent Order/place, from the latency log, against the signal price. Positive = worse.
   - Modeled paper slippage, from the forward record.
   - The Phase 4A **proposed monitoring flag** (mean drift > +11 ticks after 20+ trades with a measured drift). It is labelled as not a decision rule.
   - Latency p50/p95, bar close → Order/place sent and signal recognized → sent.
   - The forward record's `market_drift_ticks` (signal close → paper fill, which includes the seconds until the next poll fills) is listed for reference.
6. Account safety:
   - the rehearsed MLL floor, the latest end-of-day balance, and the headroom above the floor plus buffer;
   - MLL guard blocks, halts and flattens (routine flatten-time ones counted separately), and safety events;
   - each sample session's audit result with its WARN/FAIL/INFO rows (drill rows excluded); a session with no audit file is flagged;
   - days whose end-of-day balance includes real drill P&L.
7. Reproducibility:
   - code versions from each session start, flagged if more than one or with uncommitted changes;
   - the config fingerprint now against the frozen baseline;
   - daily audits with a frozen-config FAIL.

The thresholds are constants at the top of `orbbot/forward_report.py`, marked PRE-REGISTERED.

**Comparability.** Reporting only. No signal, risk, sizing, config or pre-registered value changed; the fingerprint is still `a6f638a7dc88463a`.

### 2. Backup at the end of the daily audit

**What.**
- `python -m orbbot.audit` now runs `backup-forward-test.ps1` after writing the audit, producing `backups/<today>.zip` (state/, journal/, audit/, logs/).
- It is skipped, with a message, on a weekday between 09:00 ET and 5 minutes after the flatten time, so the SQLite file and its -wal stay a consistent pair.
- `--no-backup` turns it off.
- A failed backup is printed and never changes the audit's result or exit code.

**Comparability.** None.

## Drill follow-ups: Auto OCO preflight, audit, wording, slippage report (2026-10-30)

### 1. Auto OCO Brackets preflight

**What.**
- `bot --live` and the drill now check at start-up that bracket orders can be sent.
- ProjectX's Account/search reports only `id`, `name`, `balance`, `canTrade`, `isVisible` and `simulated` (checked 2026-10-30), so the bracket mode can't be read from the API.
- If the account reports a field containing "oco" (in case ProjectX ever adds one), that value decides.
- Otherwise both refuse to start until the new config attestation `safety.auto_oco_confirmed: true`. `bot --live` logs a CRITICAL line when it refuses, and a reminder when it starts on the attestation.
- `config-sweep.yaml` gains `auto_oco_confirmed: false`. Set it to `true` only after checking TopstepX.
- Paper mode needs no confirmation.
- The broker's rejection text ("Brackets cannot be used with Position Brackets …") is now translated into a clear instruction. It is logged CRITICAL and recorded as a `bracket_mode_rejected` event.

**Why.** The first two drills on 2026-10-30 were rejected for this reason, and every live bracket order would have been too.

**Comparability.** None: start-up gating and message text only. `safety.*` (apart from `required_confirm_symbol`) is not part of the frozen fingerprint, which is unchanged.

### 2. Drill audit: rejected runs

**What.**
- A drill the broker rejected before any fill, with the account verified flat after that run, is now **WARN** "rejected: <reason>" instead of FAIL.
- FAIL remains for a run that didn't end flat, wasn't journaled, or filled and wasn't protected in time or closed.
- Each run is checked against its own end record, the first `drill_end` after it started, not just the day's last one.
- The drill section's status is FAIL, WARN or PASS accordingly.

### 3. Wording

**What.** "bar started Ns earlier" replaces "Ns old". A bar's timestamp is the minute it started; the current minute's bar carries the latest price.

### 4. Drill slippage report

**What.** `python -m orbbot.drill --report` (read-only; sends nothing) lists every filled drill:
- entry slippage in ticks against the last price seen;
- the stop/target fill against its order price;
- fees and net;
- latency: Order/place round trip, submit→ack, ack→fill seen, fill→protected.

All tick figures are positive = worse for us, followed by means. Runs from before the newest-bar fix show no entry slippage and are left out of the mean.

**Comparability.** Reporting only.

## Fix: newest bar for the drill's last price and the latency log (2026-10-30)

**What.**
- ProjectX History/retrieveBars returns bars **newest first**. The drill took `bars[-1]` as the last price, which is the oldest bar in its 5-minute lookup window. The per-API-call rows of the latency log did the same for their `bar_t` and `price`.
- Both now take the newest bar by timestamp.
- The drill also refuses to send if that newest bar is more than 2 minutes old.

**Why.** The first filled drill (`DRILL-20261030-short-530e6e5a66`) recorded "−38 ticks" of slippage against a price 5 minutes old, so that run has no valid slippage measurement. Its fill (28,479.00), stop (28,484.00), target (28,474.00), stop exit at 28,484.00, 101 ms submit-to-ack and −$10.72 net are correct.

**Comparability.** Strategy A is not affected: the bot (`feed_bars`) and the paper broker already sorted bars by time, so signals and paper fills were always right. The per-poll latency rows and the latency report's figures were also right.

## Task 7: Practice-account execution drill (2026-10-30)

**What.** `python -m orbbot.drill --config config-sweep.yaml --live` (`orbbot/drill.py`) sends one **real** 1-lot MNQ market order to the Topstep Practice account and closes it. It is built only for a supervised run.

**The order.**
- 1 MNQ at market. The side alternates by date: an even date ordinal is long, odd is short.
- 20-tick stop and 20-tick target brackets ($10 each on 1 MNQ), measured from the fill.
- It goes through `ExecutionEngine`, the same engine as the bot. Order/place is sent once, and any ambiguous result is reconciled by customTag (prefix `DRILL-`), never resent.
- The stop must be verified within `protection_grace_s` (15 s); otherwise the position is flattened via Position/closeContract.
- A position still open after 10 minutes is closed. Ctrl+C flattens and verifies flat.

**Gates (all required, checked before anything is sent):**
1. `--live` on the command line. CLAUDE.md rule 2 requires real orders to need `--live` *and* `safety.live_trading_enabled`. For the drill, Pete chose `--live` plus the drill's own gates below, and the config switch stays `false`, so the bot's own `--live` path stays disarmed. **The drill is a deliberate, narrow exception to rule 2's config switch.**
2. A hard-coded allowlist: account id 28771375 named `PRAC-V2-387652-21891938`. Any other id is refused, and so is any account whose name doesn't start with `PRAC-`.
3. Operator authorization. The drill prints the account, contract, side, size and bracket, and the operator must type the account name exactly. Anything else aborts.
4. 12:00-15:00 ET on a weekday.
5. The paper bot must be idle: outside its session times, and no paper heartbeat in the last 2 minutes.
6. No open intent in `state/paper.sqlite`, which the drill opens **read-only**.
7. The real account has no position and no working order on any contract.
8. Its own lock, `state-drill/drill.lock`.

The window, paper-bot, intent and account checks are repeated after the account name is typed.

**Isolation.**
- State: `state-drill/drill.sqlite` (intents, transitions, events, forward row, and its own latency table).
- Journal: `journal-drill/drill.csv`.
- Log: `logs/drill-YYYYMMDD.log`. The Strategy A audit's log scan reads only `orbbot-*.log`.
- `state/paper.sqlite`, `state/paper_broker.json` and `journal/paper.*` are never written. The tests check this.
- Latency: `python -m orbbot.latency_report --db state-drill/drill.sqlite`.

**Summary printed at the end:**
- every state transition with timestamps;
- submit-to-ack latency;
- the fill against the last price at submission (real slippage, in ticks);
- the bracket prices at the broker, the exit and net P&L;
- whether the account is flat with no working orders.

**Audit.**
- A separate "Practice-account drill" section, with its own PASS/FAIL. It passes when the drill opened, was protected within `protection_grace_s`, closed, ended flat, and was journaled.
- It is appended after the Strategy A table and is not part of the overall result. Strategy A's checks, counts and fingerprint are unchanged.
- On a drill day, the Strategy A end-of-day balance row adds "includes Practice drill P&L $X". This matters because the paper broker's balance is the real account balance plus simulated P&L, so the drill's real P&L enters that day's end-of-day balance and the MLL floor history.

**Comparability.**
- Strategy A's signals, fills, P&L and journal are unaffected.
- On a drill day, the end-of-day balance (and so the MLL floor) includes the drill's real P&L. The audit row states the amount.
- The paper bot's daily loss/profit guard reads only simulated trades, so the drill can't change it.

## 4A follow-ups (2026-10-29)

### 1. Latency instrumentation (logging only)

**What.** A new `latency` table in `state/<mode>.sqlite` (`orbbot/latency.py`):
- **Per API call:** wall-clock start, end and duration of every broker/data call. The calls are History/retrieveBars for MNQ and ES (the once-per-session history download is labelled separately), positions, orders, account, trades, Order/place, cancel and close. For bar requests, the row also holds the latest bar timestamp received and its close.
- **Per poll:** start, end, the newest bar timestamp received and the last price.
- **Per signal:** one row per stage, in this order:
  1. `bar_close`
  2. `poll_start` (the poll that first received the completed bar)
  3. `bars_received`
  4. `recognized`
  5. `risk_done`
  6. `place_sent`
  7. `place_response`
  8. `fill_seen`
  9. `protected`

  A refused signal stops at `risk_done`, with "blocked" or "skipped" in the detail.

`python -m orbbot.latency_report --config config-sweep.yaml [--since D] [--until D]` prints p50/p95/p99/max for:
- each API call and the whole poll;
- the time from each minute's start until the first poll received that minute's bar;
- each stage-to-stage gap, plus the total from bar close to protection.

The report opens the database read-only.

**Why.** Phase 4A could not separate ProjectX bar-data lag from the 0-5 s poll phase. This table measures both.

**Safety.**
- Rows are written in one batch per poll.
- A failed latency write logs one warning and drops the rows. It never marks the state store unhealthy, so it can never block an entry.
- The timing wrapper passes every call, return value and error through unchanged.
- The engine's new `on_transition` hook is wrapped so that a failure inside it cannot affect an order.

**Comparability.** No effect: these are new records only, and no decision reads them.

### 2. Forward record: `equity_after`

**What.**
- `equity_after` is now `equity_before + net_pnl` for the trade.
- New `note` column in the `forward` table.
- When the `note` column is first added to an existing database, every row that already has an `equity_after` gets a note. The note says the value came from a stale account snapshot and gives `equity_before + net_pnl`.
- The recorded values themselves are not changed.
- The migration runs once, from code, on the first open by the new version: the bot's restart, or an audit run with the new code.

**Why.** The old code wrote `equity_after` from the bot's cached account balance, which could predate the exit. Oct 28 shows 149,996.95, which is before that trade's +$433.90.

**Rows affected.**
- 2026-10-28 and 2026-10-29.
- 2026-10-30 as well, if Friday's session runs on the old code (a restart after Friday's session).

**Comparability.** A record field only. P&L, fills, exits and the EOD balance / MLL floor were always correct and are unaffected. Compare equity across the change using the note's `equity_before + net_pnl`.

### 3. Audit: "paper-uncertain" entry minute

**What.**
- The bot now records the entry minute's 1-minute high and low in the forward record (`entry_bar_high`, `entry_bar_low`) once that minute's bar completes.
- The audit lists a paper trade as **INFO** `paper-uncertain` when that minute's high (short) or low (long) reached the trade's stop.
- INFO is a new audit status: it is listed but does not change the overall PASS/WARN/FAIL.

**Why.** The paper broker ignores its fill minute's range, because it cannot know whether the extreme came after the fill. A real broker could have stopped such a trade out, so the flag marks paper results that are optimistic in this way. Historically this applied to 10 of 272 trades (Phase 4A findings, section 3).

**Comparability.**
- The paper engine's fills and exits are unchanged.
- Trades before this change have no recorded entry-minute range, so they cannot be flagged. Oct 28 and Oct 29 are therefore unflagged.

### 4. Databento 1-second price check (no download)

**What.** `scripts/price_1s.py` gets a cost quote from Databento's free `metadata.get_cost` and stops; nothing is downloaded. The query is GLBX.MDP3 `ohlcv-1s`, `stype_in=continuous`, 09:30-12:00 ET each day (DST-aware).

Quotes on 2026-10-29:

| Option | Days | Cost |
|---|---|---|
| (a) MNQ.v.0, the signal days in `phase4a_signal_days.csv` | 250 | $8.17 |
| (b) MNQ.v.0, every weekday 2024-10-21 .. 2026-10-20 | 522 | $16.83 |
| (c) ES.v.0, same days as (b) | 522 | $16.09 |
| (b) + (c) | | $32.92 |

Weekdays include exchange holidays, which have no data and cost about $0.

**Comparability.** Not applicable: a tool only, with no data obtained.
