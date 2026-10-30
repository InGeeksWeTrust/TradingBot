# Phase 4 changes log

Every change to the forward engine or its records during Phase 4. Each entry says what changed, why, and whether it affects comparability with earlier forward records. The frozen strategy (signals, confirmation, stops, targets, sizing, risk limits, trading window, forward-test settings) has not changed. The audit's config fingerprint is still `a6f638a7dc88463a`.

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
