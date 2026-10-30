# Phase 4A, initial findings: signal-to-fill timing, replay discrepancies, fill-anchored stops

Strategy A, the frozen baseline (272 trades, +$6,125, PF 1.20, +0.12R, max intraday DD -$3,125) and the forward-test record are unchanged. All scenarios below are separate sensitivity runs on the same 2-year MNQ/ES data (Oct 2024 - Oct 2026) with the frozen signals.

## Summary

1. **Where the 24- and 10-tick "drift" came from.** Both paper entries were placed 3.9 s and 5.2 s after the signal bar closed. That delay is entirely the 5-second polling phase; processing took about 2 ms. The drift is genuine market movement during those few seconds, and in both cases it was movement *toward the target*.
2. **The backtest effectively fills at the signal price.** The next 1-minute bar's open is on average 0.01 ticks from the signal close. So the backtest assumes about 1 tick of entry cost; the paper broker's cost is whatever the market does in the 0-5 s before the next poll, plus 1 tick.
3. **Waiting does not cost much on average, but paying a worse price does.**
   - Entering a full minute late changes almost nothing: $6,088 vs $6,125. The first minute after a signal moves toward the target only 51% of the time (median +3 ticks), so there is no systematic momentum to miss.
   - A consistently worse entry price is what hurts. At 10 extra ticks per trade, expectancy is +0.09R. At 17 ticks total, close to the 2-trade average of 18, it is +0.04R. At 25 ticks it is negative.
   - Break-even is about 22 adverse ticks per entry.
4. **Two trades cannot tell us the typical drift.** The historical 1-minute data only bounds it. Measuring it properly needs 1-second data around the 272 historical signal times (proposed below, cost to be checked first).
5. **The two replay discrepancies are both intrabar-timing ambiguities.** Each is worth one trade. The backtest version is the *more conservative* in both. Combined effect if the replay version were right: +$860 over 2 years (+14%), trade count unchanged.
6. **Intrabar ambiguity is small overall.**
   - 10 of 272 backtest trades stop out inside their entry minute (-$2,075 total).
   - Re-running them from the next minute (the paper broker's view) changes only one of them (2024-12-05, +$665).
   - No trade has a bar that touches both its stop and its target.
7. **Fill-anchored stops.**
   - In the backtest, stops sit at the wick: median 1 tick inside, range -6 to +9 ticks.
   - On the two paper trades they sat 24 and 10 ticks inside the wick, because the bracket is anchored to the later fill.
   - Dollar risk is unchanged either way.
   - The drift scenarios below already model fill-anchored stops (the stop moves with the fill), so they represent the live behaviour.
8. **Two record/simulator issues found** (correctness, not signals): a stale `equity_after` value in the forward record, and a blind spot in the paper broker for stops during the entry minute. Details in section 5.

## 1. Signal-to-fill timelines (from state/paper.sqlite, forward + transitions tables)

All times are ET. The signal bar is the 5-minute bar 09:40:00-09:45:00 (last 1-minute bar 09:44).

| Step | Oct 28 short 5 MNQ (pdh) | Oct 29 long 1 MNQ (onl) |
|---|---|---|
| Signal bar closes | 09:45:00.000 | 09:45:00.000 |
| Signal price (5-min close) | 29,879.00 | 29,550.50 |
| First poll after close; signal recognized; intent CREATED | 09:45:03.921 (+3.92 s) | 09:45:05.153 (+5.15 s) |
| Risk calc and intent creation | <2 ms | <2 ms |
| SUBMITTING (Order/place sent) | 09:45:03.923 | 09:45:05.155 |
| Paper fill (last price of the partial 09:45 bar ± 1 tick) | 29,872.75 | 29,553.25 |
| ACKNOWLEDGED (paper broker, local, no network) | 09:45:03.926 (+3 ms) | 09:45:05.159 (+5 ms) |
| Fill seen by the engine (next poll) | 09:45:09.126 (+5.2 s) | 09:45:10.360 (+5.2 s) |
| PROTECTED (stop verified) | 09:45:09.137 | 09:45:10.369 |
| Exit | target 29,828.75 at 09:47:24.76 | time flatten 29,608.00 at 11:55:04.07 |
| Net | +$433.90 | +$108.28 |

Decomposition of the adverse entry (signal close to fill):

| Component | Oct 28 | Oct 29 |
|---|---|---|
| 1. Market movement after the close, up to the poll | 24 ticks (toward target) | 10 ticks (toward target) |
| 2. Delay until the next poll | 3.92 s | 5.15 s |
| 3. Signal + risk evaluation time | ~2 ms | ~2 ms |
| 4. Network/API latency of the order | not applicable (paper) | not applicable (paper) |
| 5. Synthetic paper slippage | 1 tick | 1 tick |
| 6. Broker-observed slippage | not available (no real order) | not available |

What cannot be separated yet:
- How much of the 3.9-5.2 s is ProjectX bar-data availability lag and how much is poll phase. The bot doesn't log per-poll retrieval times or the latest bar timestamp it received; this is part of Task 2.
- The "last price" is the close of the partial 09:45 bar returned by History/retrieveBars at that moment, not a quote.

Expected recognition delay with 5 s polling is uniform 0-5 s plus retrieval time. 3.9 and 5.2 s are at the slow end; the poll cadence's phase relative to the minute is arbitrary.

Note on the stop-confirmation gap: the paper broker attaches the bracket instantly, but the engine only confirms protection at the next poll, about 5.2 s later. With the real broker the bracket is server-side from the fill, so this gap affects only how fast the bot can *detect* missing protection.

## 2. The two replay discrepancies (506/508 identical sessions)

| Day | Backtest | Live-loop replay | Cause | P&L effect (replay minus backtest) |
|---|---|---|---|---|
| 2024-12-05 | short pdh 09:35 stopped out in its entry minute (-$237.20); short onh 09:40 (+$53.12). Day -$184.08 | short pdh 09:35 only, held, target at 09:49 (+$427.80). Day +$427.80 | The 09:35 bar's high (21,552.00) touched the stop (21,551). The backtest fills at the bar's open, so the whole bar counts against the position. The replay/paper broker fills ~5 s into the minute and ignores that minute's range, because it cannot know whether the high came before the fill. | +$611.88 |
| 2025-11-03 | short onh 09:35, target 09:40 (+$371.34). The 09:40 pdh signal is blocked because the position is still open at 09:40:00 | Same first trade, plus short pdh at 09:40, 1 MNQ (62.25-pt stop), target at 10:26 (+$247.78). Day +$619.12 | The target was hit by the 09:40 bar's low (26,170.75 vs 26,172.00). The replay harness served the whole 09:40 bar at its 09:40:05 poll (look-ahead within the minute), so the first trade closed before the signal was evaluated. This is a flaw in my replay harness, not in the bot. | +$247.78 |

- **Trade count:** unchanged (272 vs 272; one fewer trade on 12-05, one more on 11-03).
- **P&L:** +$859.66 if the replay version were right on both days (+14% on $6,125).
- **Expectancy and drawdown:** expectancy rises slightly. Neither day is in the max-drawdown window.
- **Topstep survival:** effectively unchanged.
- **Resolution:** neither engine is "right" without sub-minute data. 1-second bars resolve both days (Task 1D). The replay harness will be fixed to serve partial bars only up to the poll time, so it has no look-ahead. Neither engine will be forced to match the other.

## 3. Intrabar accuracy across the 272 baseline trades

- **Entry minute.** The entry bar's open vs the signal close averages +0.01 ticks (median 0). The backtest therefore fills at the signal price + 1 tick.
- **First minute after entry, in the trade's direction:**
  - Close minus open: mean +3.6 ticks, median +3, p25 -34, p75 +50.
  - The move goes toward the target 51% of the time.
  - Median excursion within the minute: 46 ticks favourable, 41 ticks adverse.
  - In words: there is no systematic momentum, but a lot of noise. A delay of a few seconds should cost little *on average* but varies widely.
- **Exits inside the entry minute:**
  - 10 trades, all stops, -$2,075 combined.
  - If the entry minute's range is ignored (the paper broker's behaviour), 9 of them still stop later. One flips to a target: 2024-12-05, +$665.
  - So the backtest/paper difference here is at most +$665 over 2 years.
- **Ambiguous bars** (a single 1-minute bar touches both stop and target during the trade): 0 of 272.

## 4. Fill-anchored stops

| | Oct 28 short | Oct 29 long |
|---|---|---|
| Structural wick | 29,900.75 (high) | 29,431.25 (low) |
| Intended stop (wick ± 1 tick, from the signal) | 29,901.00 | 29,431.00 |
| Actual bracket stop (fill ± stop_ticks) | 29,894.75 | 29,433.75 |
| Difference | 25 ticks tighter | 11 ticks tighter |
| Stop beyond the original wick? | No (24 ticks inside) | No (10 ticks inside) |
| Price reached the actual stop? | No (MAE 1.25 pts) | No (MAE 105.75 pts; low 29,447.50, 13.75 pts above the stop) |
| Dollar risk | $220 as planned | $239 as planned |

- **Backtest, all 272 trades:** the stop sits a median 1 tick inside the wick (mean 1.0, range -6 to +9). Fill-anchoring is effectively equivalent to wick-anchoring in the backtest, because the backtest fill equals the signal price.
- **Live:** fill-anchoring only matters when there is drift, and it always moves the stop *toward* the entry by the drift.
- **Validity of a delayed entry:** with the stop inside the wick, price has already traded beyond the stop level during the signal bar, so the invalidation point is weaker. The trade is still the planned dollar risk, but a larger share of normal noise can reach the stop. This is captured in the drift scenarios below; I did not change the frozen rule.

## 5. Sensitivity of the historical baseline to execution (separate scenarios; baseline unchanged)

Entry-only drift is applied as extra adverse ticks on every entry; stop and target are anchored to that fill, as in live. Monte Carlo: 10,000 paths of 500 trading days, 5-day blocks.

| Scenario | Trades | Net | PF | Avg R (95% CI) | Max DD | 50K pass / fail | 150K pass / fail / open |
|---|---|---|---|---|---|---|---|
| **Frozen baseline (1 tick entry, 1 tick stops)** | 272 | **$6,125** | 1.20 | +0.116 (-0.043, +0.275) | -$3,125 | 57% / 43% | 28% / 11% / 61% |
| Entry 1 minute late, 1 tick | 271 | $6,088 | 1.20 | +0.115 (-0.048, +0.282) | -$3,562 | 55% / 45% | 30% / 13% / 57% |
| 6 ticks total entry cost | 272 | $5,584 | 1.19 | +0.105 (-0.052, +0.266) | -$3,294 | 54% / 46% | 25% / 13% / 61% |
| 11 ticks total entry cost | 272 | $4,717 | 1.16 | +0.088 (-0.068, +0.247) | -$3,294 | 50% / 50% | 19% / 16% / 64% |
| 11 ticks entry + 3-tick stop slippage | 272 | $4,268 | 1.14 | +0.079 (-0.077, +0.239) | -$3,333 | 47% / 53% | 17% / 19% / 64% |
| 17 ticks total entry (paper average so far: 18) | 271 | $2,122 | 1.07 | +0.042 (-0.120, +0.203) | -$3,294 | 35% / 65% | 7% / 33% / 60% |
| 25 ticks total entry (Oct 28's drift on every trade) | 270 | -$1,186 | 0.96 | -0.021 (-0.181, +0.140) | -$4,356 | 20% / 80% | 2% / 59% / 40% |

Earlier grid (slippage on both entries *and* stops): 5 ticks gives $4,725; 10 ticks gives $2,895; 2 minutes late plus 10 ticks gives -$33.

Reading:
- The edge survives about 10 ticks of average entry drift, though thinner.
- It does not survive the drift seen on the first two paper trades (18 ticks on average) if that were typical.
- Historical 1-minute evidence (point 3) suggests a 4-5 s delay costs a few ticks on average, not 18. But this needs the 1-second check before anyone relies on it.
- Combine survival is far more sensitive than expectancy. At 11 ticks the 50K becomes a coin flip; at 17 ticks it fails 2 times in 3.

## 6. Issues found (correctness; none touch signals)

1. **Forward record `equity_after` is stale.** It is written from the bot's cached account snapshot at exit time: Oct 28 shows 149,996.95 and Oct 29 shows 150,433.29, i.e. before the trade's P&L. The EOD balance and MLL floor are correct. Fix: refresh the account before writing, or record `equity_before + net_pnl`. This is a versioned forward-engine change that doesn't affect comparability (a record field only).
2. **Paper broker blind spot in the entry minute.** It ignores the fill minute's high/low (correctly, since it can't know whether they came before the fill) but then only samples the price every ~5 s for the rest of that minute. A stop touched and reversed between polls inside the entry minute is missed, which is optimistic. Historically, entry-minute stop-outs are 10/272 trades. The real broker would catch them.
   - Proposal: leave the paper engine unchanged for comparability, and flag any paper trade whose entry-minute range crossed its stop as "paper-uncertain" in the audit.
   - Alternative, once streaming data exists (Task 2): evaluate brackets on streamed prices. That would be a versioned change.
3. **Replay harness look-ahead** (research tooling, my harness): it served the whole current minute's OHLC at hh:mm:05. To be fixed before any further replay comparisons.

## 7. Proposed next steps for 4A

1. **1-second data for the 272 historical signals** (Databento MNQ ohlcv-1s, roughly 09:35-11:55 ET on the ~250 signal days). Purpose: measure the true price change 0-6 s after each signal close (the drift distribution), and resolve the 10 entry-minute stop-outs and the 2 replay days. Databento downloads cost money, so I'll get the price quoted before anything is downloaded.
2. **Instrument the bot (Task 2 groundwork, logging only):** per poll, record retrieval latency, the latest bar timestamp received, and the last price; per signal, record bar-close-to-recognition time. That separates data lag from poll phase.
3. **Forward-test monitoring rule (proposal, not a change to the pre-registered criteria):** report the running mean of market drift per trade. If the mean exceeds 10 ticks after 20 trades, raise it as an execution-risk flag before any Combine decision.
4. Fix items 6.1 and 6.3. Add the 6.2 audit flag.
5. Then continue with 4C (Topstep rule fidelity, including "a bar's extreme before the fill does not count against the position") and 4D (Monte Carlo block-size and edge scenarios).

**Readiness:** Strategy A remains suitable for continued paper validation. Before any paid Combine we need (a) the 1-second drift measurement, (b) real broker fills from the Practice drill, and (c) drift data from the first 20+ forward trades. Today's evidence says the edge is sensitive to entry quality, and that is not yet measured.
