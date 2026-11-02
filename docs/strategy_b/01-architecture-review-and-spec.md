# Strategy B (MTF Precision Scalper): architecture review and specification

Status: Deliverables 1 and 2, for review before any implementation. Nothing in the bot, the research
engine or Strategy A's config, state or records was changed to produce this.
Date: 2026-10-26. Source revision reviewed: `f276a80` (branch `phase5-execution-safety`).

---

## Part 1: Source-code architecture review

### 1.1 Measured latency (current REST-polling bot)

| Measurement | Result | How it was measured |
|---|---|---|
| REST round trip, `Account/search` | median 75 ms, p95 82 ms, max 113 ms (n=16) | gap between consecutive log lines at start-up, all logs 10-21..10-26 |
| REST round trip, `Contract/search` | median 75 ms, p95 99 ms, max 121 ms (n=41) | same |
| REST worst case seen | one 10 s read timeout (`Account/search`, 10-23 14:35) | log WARNING; the loop retried after a 1.5 s sleep |
| One `Session.poll()`, Python + SQLite only | median 0.07 ms, p95 0.09 ms, max 2.9 ms (n=1,074 polls, 1 trade) | real `Session` against `tests/fakes.py` broker, 9:30-11:00 at 5 s steps |
| REST calls per poll | 3 every poll (bars, positions, open orders) + account and trades every 30 s; max 6 | same benchmark, calls counted per method |
| Bar close to decision, live | 4.3 s and 4.1 s (n=2) | 10-23: 9:39 bar closed 9:40:00, logged 9:40:04.288; 10:49 bar closed 10:50:00, logged 10:50:04.113 |
| Session build (6 days of bars + `Features`) | ~0.4-0.6 s, once per day | log gap "Session" to "Sweep levels" |

Conclusion: **Python is not the bottleneck.** Decision logic costs well under a millisecond. Latency
comes from (a) waiting for the next poll, (b) sequential network calls inside a poll, and (c) the
REST rate limit, which stops the poll from simply being made faster.

### 1.2 Why REST polling adds latency, in order of size

1. **Poll interval (dominant, 0-5.3 s, ~2.5 s average).** `ORBBot.run_session` sleeps `poll_seconds`
   (5) after each poll, so a bar that closes just after a poll waits a full cycle. The two live
   observations (4.1 s, 4.3 s) fall in this range.
2. **Rate limit sets a floor on polling.** `History/retrieveBars` is limited to 50 requests per 30 s.
   Each poll already makes two bar calls once the sweep is built (MNQ 1-min, plus ES re-downloaded
   from 9:30 every poll in `refresh_confirm`), about 12 per 30 s. A 1 s poll would need ~60 per 30 s,
   which is over the limit. Two symbols cannot be polled faster than about every 1.2 s, and a
   second process (Strategy B) would share the same budget.
3. **Sequential calls before an order.** `poll()` computes signals in step 2 but sends orders in
   step 7, after `refresh_account`, `refresh_realized` and `engine.refresh` (positions + open
   orders). Live, that is 2-6 round trips (~150-450 ms) between seeing the bar and `Order/place`.
   (In paper mode positions/orders are local and cost nothing.) The pre-trade broker refresh is a
   safety feature and should stay; with streaming, most of it becomes a confirmation of state
   already held.
4. **Blocking retries.** `ProjectXClient._post` has a 10 s timeout and sleeps 1.5 s, 3 s between
   retries. One slow call stalls the whole loop, including position management.
5. **Stale prices for guards and paper fills.** `last_price` is the close of the latest partial
   1-min bar from the last poll, so it is up to ~5 s old. Paper market fills use it ±1 tick. The
   paper broker checks brackets against 1-min highs/lows, stop first. For 1-min scalps this
   overstates fill quality compared with what a live order would get.
6. **Protection verified one poll later.** After a fill, bracket legs are confirmed on the next
   poll (≥5 s) with a 15 s grace. Acceptable for A; slower than ideal for a strategy that may be
   in and out within minutes.

Freshness limits suit A, not a scalper: bars and confirmation may be up to 180 s old before
entries are blocked. B needs its own, much tighter limits.

### 1.3 What can be reused as is

| Component | Reuse for B | Notes |
|---|---|---|
| `execution.py`: `ExecutionEngine`, state machine, single-send `submit`, `reconcile`, `recover`, `verify_protection`, `flatten`, `AccountGuard`, `Freshness` | Yes, unchanged | B creates its own engine instance with its own store |
| `state.py` `StateStore` (SQLite intents, events, forward record) | Yes | Separate file via B's `safety.state_dir` |
| `paper.py` `PaperClient` | Yes for paper, with a separate state file | Its fill model is too coarse for scalps (1.2 #5); B needs a quote-driven paper fill mode |
| `risk.py` `plan_trade`, `DailyGuard`, `size_with_costs` | Yes | Same cost model: $1.22/contract RT, 1 tick slippage per side |
| `features.py` (hourly EMAs, 15-min swings, PDH/PDL/ONH/ONL from the traded contract only) | Yes, extended | Needs hourly swings, ATR, a :30-anchored hourly series (2.2) |
| `filters.py` `ESConfirm` (session VWAP) | Optional | Not in B's baseline (avoid indicator stacking) |
| `research.py` `simulate_day`, `stats` (p(random), edge vs random), TRAIN/TEST/HOLDOUT reports | Yes | Entry at the next 1-min open + slippage, stop wins ties |
| `audit.py` | Pattern reused | B gets its own fingerprint and checks |
| `tests/fakes.py` (fault-injecting broker, fake clock) | Yes | Extend with a fake event stream |

Separation from A is possible **through config alone**: `safety.state_dir`, `journal_dir`, `log_dir`,
`kill_switch_file` and `halt_file` are all configurable, and the paper broker file lives in
`state_dir`. One shared-code change is needed: order tags are hard-coded as
`sweep-YYYYMMDD-side-xxxx` (`execution.py:300`). A strategy prefix defaulting to `sweep` keeps A
identical.

### 1.4 New components required

1. **`orbbot/stream.py`**: client for the official ProjectX SignalR hubs (verified in the
   gateway docs, 2026-10-26):
   - market hub `https://rtc.topstepx.com/hubs/market`: `SubscribeContractQuotes`,
     `SubscribeContractTrades` → `GatewayQuote`, `GatewayTrade`
   - user hub `https://rtc.topstepx.com/hubs/user`: `SubscribeAccounts`, `SubscribeOrders`,
     `SubscribePositions`, `SubscribeTrades` (accountId) → `GatewayUserAccount`, `GatewayUserOrder`,
     `GatewayUserPosition`, `GatewayUserTrade`
   - JWT as `access_token`, WebSockets, automatic reconnect; **subscriptions must be re-sent after
     every reconnect**.

   Runs on its own thread and puts each event, stamped with its receive time, into a queue.
   Library choice is open (2.9, decision 5).
2. **`orbbot/candles.py`**: incremental 1/5/15/60-min candles built from `GatewayTrade` prints,
   with the same clock alignment as `features._agg`, so live and research bars match. Handles:
   - **Minute finalization:** a minute closes on the first print stamped at or after the next
     minute, or after a wall-clock grace period (start at 1.0 s).
   - **Duplicates:** `GatewayTrade` has no trade id. Prints are de-duplicated on (timestamp,
     price, volume, type) within a minute; this is imperfect, so every finished minute is
     **verified against `History/retrieveBars`** (1 REST call per symbol per minute, ~4 per 30 s,
     well inside the limit). Mismatches are logged and counted.
   - **Late or out-of-order prints:** applied to an open minute; for a finished minute they are
     counted as a "late print", never re-trigger a signal, and the REST check reports the
     difference.
   - **Reconnects and gaps:** missing minutes are backfilled from REST, and entries stay blocked
     (`engine.block("data_gap")`) until the candles are consistent again.
   - **Session boundaries, DST, rollovers:** times are converted to ET with `zoneinfo`; the
     17:00-18:00 ET halt produces no bars; contracts are re-resolved each session, as now.
3. **`orbbot/event_session.py`**: B's session loop. Each completed candle or quote updates the
   strategy, and a signal goes straight to `consider()` using the same risk and engine calls as
   `Session`. Rules:
   - Before `Order/place` it still takes a REST snapshot of positions and open orders (~150 ms).
     That is kept until measurements justify removing it.
   - A REST reconciliation heartbeat runs every 5 s, as today, so missed user-hub events are caught.
   - A's `Session.poll` is **not edited**, so A's behaviour and fingerprint cannot change.
4. **Latency instrumentation**: one row per signal in B's SQLite, recording the 9 stage times
   from the brief:
   1. event received
   2. candle closed
   3. signal
   4. risk approved
   5. order sent
   6. order acknowledged
   7. entry fill
   8. bracket verified
   9. exit fill

   Reports median, p95 and max per stage. The same stamps go into A's paper log only if that
   changes nothing else in A, otherwise not.
5. **Quote-driven paper fills for B**: fill market orders at the best ask (buys) or bid (sells)
   at the moment of submission, plus a configurable extra slippage; trigger stops and targets on
   trade prints. The model being replaced uses the last 1-min close ±1 tick.

### 1.5 Interactions with Phase 5 safeguards (must be designed in, not discovered)

- **Netted positions:** ProjectX nets positions per contract per account. If A and B ever trade
  MNQ **live on the same account**, A sees B's position as "unexpected"
  (`check_unexpected`). It blocks its own entries, and if it can't verify protection it
  **flattens B's trade**. Paper is unaffected because each process has its own `PaperClient`.
  - Before any combined live use there must be either separate accounts or one process that owns
    a single engine and a combined risk ledger. Until then, B stays paper-only and never sends
    orders while A is live.
- **Combined risk isn't visible to either process:** each paper broker reports the real balance
  plus only its own simulated P&L, so the MLL guard can't see combined exposure. Combined
  drawdown and worst simultaneous loss will be computed offline from both journals for Deliverable 6.
- **Shared rate limits:** both processes use the same credentials. With streaming, B adds only
  ~2 bar-verification calls a minute during its session. A is unchanged.
- **Kill switches:** B gets its own `STOP-B` and `HALT-B` files and also honours A's `STOP`
  (global stop). A ignores B's files.
- **Unverified events never replace reconciliation:** user-hub events update state early, but
  the state machine only advances on REST-confirmed state, exactly as now. Streaming makes
  detection faster; it doesn't replace any check.
- **Live trading stays off:** B's config has `live_trading_enabled: false`, B's entry point refuses
  `--live` during this project, and a test asserts that `place_order` is never reached on the real
  client.

### 1.6 Expected improvement (to be measured, not promised)

| Step | Now (REST poll, 5 s) | Event-driven estimate |
|---|---|---|
| Bar close → bar known | 0-5.3 s (2 observations: ~4 s) | finalization grace: about 1 s |
| Bar known → order sent (live) | ~150-450 ms of sequential REST | ~150 ms pre-trade snapshot |
| Order sent → acknowledged | 1 RTT (~75 ms) + broker processing | same (broker-bound) |
| Fill → bracket verified | next poll, 5-15 s | user-hub order event, then a REST check: ~0.1-0.5 s |
| Exit fill → bot knows | next poll, ≤5 s | user-hub trade event: sub-second |

Realistic goal: **bar close to order sent within about 1-1.5 s at p95**, about 3 s faster than
now, with the broker's own processing unchanged. Whether 3 s is worth it for B is itself a research
question (Part 2.8, delay stress test).

---

## Part 2: Strategy specification (B1, B2)

### 2.0 Conventions

- All times ET. Data: 1-min OHLCV; a bar's time is its open, and it is known at open + 1 min.
- Candles are clock-aligned like `features._agg`:
  - 5-min: :00, :05, …
  - 15-min: :00, :15, :30, :45
  - Hourly: see 2.1.
- Only **completed** candles are used for any decision. Each derived value (EMA, swing, level) is
  stamped with the time it became known and queried "as of t", as `features.py` already does.
- Instruments: MNQ traded. ES is not part of the B baseline (see 2.6).
- Levels (PDH/PDL/ONH/ONL) come from the traded contract only, as for A. Indicators across a roll
  use a difference back-adjusted series built from the overlapping roll bars
  (`mnq_rollfill_*.csv`).

### 2.1 Stage A: 1-hour bias

**Anchoring (decision for Pete).**
- **Baseline:** hourly candles anchored at **:30**, running continuously through the Globex
  session (… 8:30-9:30, 9:30-10:30, …).
  - The RTH open is then an hourly boundary, so no candle mixes pre-open and regular-session
    trading.
  - The bias at 9:30 is set by the 8:30-9:30 candle, which closes exactly at the open.
  - Candles cut short by the 17:00-18:00 halt are used as they are.
- **One alternative only:** clock hours, which is what the existing `mtf` research used.
- **Warm-up:** EMA50 needs ≥50 hourly candles (about 2.2 Globex sessions). At start-up, B loads
  6 days of 1-min bars, as A does.

| Input | Definition | Status |
|---|---|---|
| EMA20, EMA50 | EMAs of hourly closes | standard |
| EMA20 rising | EMA20(now) − EMA20(3 candles ago) > 0 | hypothesis (lookback 3) |
| Hourly swings | pivot high/low with 2 candles each side, known when the 2nd candle after it closes | as `features._swings`, on hourly |
| Structure up | last 2 confirmed swing highs rising AND last 2 swing lows rising; down = mirror | as `features.structure` |

- **Bullish** = EMA20 > EMA50 AND EMA20 rising AND structure up.
- **Bearish** = the mirror.
- **Neutral** = anything else, and B takes no trades.

ATR and VWAP distance are **not** in the bias, to avoid stacking indicators. ATR is used only for
B2's strength test and for the volatility-regime report.

Ablation (answers "does EMA alignment add value?"): run the B1 baseline with
(i) EMA-only bias, (ii) structure-only bias, (iii) both. Nothing else is varied.

### 2.2 Stage B: 15-minute setups

Candle geometry for a completed 15-min candle S:
- range R = h − l
- lower wick = min(o, c) − l
- upper wick = h − max(o, c)
- close position = (c − l) / R

ATR14 uses 15-min candles. A candle qualifies only if **R ≥ 0.5 × ATR14(15m)**, so tiny dojis
never count (hypothesis).

**B1 bullish rejection** (bearish = mirror):
1. Bias bullish (as of S's close).
2. Lower wick ≥ 0.40 R (hypothesis).
3. Close in the upper 30%: (c − l) ≥ 0.70 R (hypothesis).
4. **Location:** S traded through a support level L and closed back above it (l < L < c), where L
   is any of:
   - PDL, ONL
   - PDH, ONH (old resistance now acting as support)
   - the last 2 confirmed 15-min swing lows

   Every level must be known before S opened. A wick that touches no level is not a setup. A
   level can produce only one B1 setup per day.

**B2 bullish continuation** (bearish = mirror):
1. Bias bullish **and strong**: EMA20 − EMA50 ≥ 0.5 × ATR14(hourly) (hypothesis).
2. **Pullback:** S's low is below the previous 15-min candle's low.
3. **Structure intact:** S's low is above the last confirmed 15-min swing low.
4. S closes above its own midpoint (c ≥ l + 0.5 R).

No level is required. That's the difference from B1: B2 buys a dip inside an intact uptrend.

Setup window: S closes between 9:45 and 11:30 (baseline "am"). Variant: up to 15:00 ("day"). No
overnight setups.

### 2.3 Stages C and D: entries

When S closes, a setup is **armed**. Each setup can produce at most one entry, in any mode.

| Mode | Long trigger (short = mirror) | Expiry |
|---|---|---|
| **1m** (B1 baseline) | a completed 1-min bar closes above the highest high of the 3 completed 1-min bars before arming | 5 min after arming (hypothesis) |
| **5m** (B2 baseline) | a completed 5-min bar closes above the previous completed 5-min bar's high | 15 min (three 5-min bars) |
| **1m+5m** | 1m trigger, valid only if the last completed 5-min bar closed ≥ its open | 5 min |

- **Invalidation:** if price trades through the planned stop before the trigger, the setup is
  cancelled.
- **Chase limit:** skip if the trigger close is more than 0.25 × stop distance beyond the trigger
  level (hypothesis).
- **Fill (research):** next 1-min open + slippage, exactly as `simulate_day`.

### 2.4 Stops, targets, exits

- **Stop:** structural, 1 tick beyond S's extreme. B1: below the rejection wick. B2: below the
  pullback low. It is never moved to fit a contract count.
- **Target, baseline:** 1.5R.
- **Variants** (each on the baseline only):
  - 2R
  - structure target: the nearest opposite level/swing beyond 1R; skip the trade if none is
    within 3R
  - time exit at 10 min
  - early exit if a 1-min bar closes beyond S's close against the trade
  - partial: half at 1R, stop to breakeven, rest to 2R
- Flatten at 11:55 ("am") or 15:55 ("day").

### 2.5 Sizing and daily limits

- `plan_trade` with `size_with_costs: true`: stop $ + 1 tick slippage per side + $1.22/contract ≤
  **$250** (comparison baseline). Lower-risk variant: **$125**.
- Same limits as A: 1-10 MNQ, minimum stop 8 ticks. A trade that can't be sized within $250 at
  1 contract is skipped.
- **Daily limits:**
  - −$500 loss and +$750 profit, same as A, for comparability
  - at most **3** B trades a day, one position at a time
  - stop for the day after 2 consecutive losses (hypothesis)
- The income target never overrides these limits.

### 2.6 What is deliberately left out of the baseline

- ES VWAP confirmation, RVOL and news filters.
- Each can be added later only as one pre-declared variant, after the baseline result is known.

**Overlap with A:** B1 is structurally close to A. Both trade through a prior-day or overnight
level and then close back. B1 adds an hourly trend filter, a 15-min candle and a 1-min trigger.
Expect correlated days, so the comparison must report their daily P&L correlation and same-day
losses.

### 2.7 Prior evidence (read before expecting much)

- **The existing `mtf` strategy is close to B2:** hourly EMA bias, 15-min HH/HL, 5-min close above
  the prior 5-min high. It was tested on these same two years of MNQ (`journal/scalp_mnq.xlsx`,
  TRAIN 2025-10-20..2026-06-19, TEST to 2026-10-20):
  - **No `mtf` variant reviewed beat random entries** (the first 40 of the workbook's 60 rows):
    TEST p(random) ranged from 0.15 to 0.97.
  - Most were net negative.
  - The best TEST result was +0.11R over 60 trades, p = 0.15.
- B2 stays deliberately close to `mtf`, so that prior result tells us something; it needs a
  specific reason to do better.
- **Both years of data are already "seen":** A was selected on them and `mtf` was tested on them.
  Nothing in-sample counts as validation.

### 2.8 Research protocol for Deliverable 3 (pre-registered, no large sweeps)

**Fixed variant list (21 runs):**
- B1, 13 runs:
  - entry {1m, 5m, 1m+5m} × target {1.5R, 2R} = 6
  - bias ablation ×2
  - structure target, time exit, early exit, partial ×4
  - clock-hour anchoring ×1
- B2, 6 runs:
  - entry {5m, 1m} × target {1.5R, 2R} = 4
  - time exit, continuation-fail exit ×2
- $125 risk on the B1 and B2 baselines, 2 runs.

**Selection rule, fixed before results are seen:**
- **Choose on year 1** (2024-10-21..2025-10-17); **check on year 2** (2025-10-20..2026-10-20).
- A candidate needs, in year 2:
  - avg R > 0 after costs
  - PF ≥ 1.15
  - p(random) ≤ 0.02 (stricter than usual because 21 variants are run)
  - still > 0 at 2 ticks of slippage
- One variant at most per setup moves on to forward paper testing.

**Reported per variant:** everything in brief §6, plus the share of trades whose stop and target
fall in the **same 1-min bar**. That share is the measure of how much the conclusion depends on
data finer than 1 minute.

**Stress tests:**
- slippage 1/2/4 ticks per side
- entry delayed one 1-min bar
- 20% of signals dropped at random (10 seeds)
- stop fills at the bar extreme
- results split by volatility tercile (daily ATR) and by trend versus chop days (hourly
  structure)

**Feasibility questions answered directly:**
- share of days with ≥1 setup, and with ≥1 entry
- weeks with ≥4 such days
- distribution of daily net P&L
- share of days ≥ +$250, and days in the +$250..+$550 range

### 2.9 Realism check on the income target

- At $250 risk, $250-$550 a day means **+1R to +2.2R net every day**.
- A's research baseline is +0.12R per trade at ~0.5 trades a day: $6,125 over ~500 sessions, about
  **$12 a day** on average.
- The best `mtf` results were about +0.1R per trade.
- A scalper earning +0.1R per trade at 2 trades a day would average about **$50 a day**.
- So the honest framing is the share of days at ≥ +$250, not a daily expectation. The research
  will report that share.

---

## Decisions needed from Pete before implementation

1. **Hourly anchoring:** :30-anchored (baseline above) or clock hours.
2. **Variant list and selection rule** in 2.8: approve them or edit them, before any backtest runs.
3. **A genuinely unseen holdout:** buy older MNQ + ES 1-min data (e.g. 2022-10..2024-10) from
   Databento? I would check the exact price with Databento's free cost query and ask before
   downloading anything.
4. **Second-level data:** decide after Deliverable 3, using the measured same-bar ambiguity rate.
   - ProjectX `History/retrieveBars` also offers second bars (unit 1); how far back they go is
     unknown and needs a read-only API probe.
   - Databento 1-second data would be bought only for the minutes around candidate trades, priced
     first.
5. **SignalR client:** a maintained Python library, or a small client written here on
   `websockets`. The protocol needs a handshake, invocation and ping only.
6. **Live latency probe:** a read-only REST and market-hub latency test with the TopstepX API key,
   run outside A's session window.
   - Before running it, I'd confirm that a second login doesn't invalidate the running bot's
     token.
7. **B's daily limits:** 3 trades, −$500/+$750, stop after 2 consecutive losses. Confirm or change.
