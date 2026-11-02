# Phase 4A complete: execution-adjusted Strategy A

Data: Databento MNQ 1-second bars (ohlcv-1s, 09:30-12:00 ET, Oct 21 2024 - Oct 20 2026; 515 sessions; $16.83). The frozen baseline and the Nov 2 forward test are unchanged. The scenarios below are research runs only.

## Terminology (per your note)

- **Market movement before submission** ("drift"): the price change from the signal bar's close to the moment the order is placed. For the two paper trades (24 and 10 ticks) this was **missed favourable movement**, not slippage. No real order existed, so no slippage was measured.
- **Execution slippage**: the fill versus the price at submission. The paper broker models this as 1 tick. Real broker slippage will come from the Practice drill.

## 1. Drift distribution at the 272 historical signals (1-second data)

Drift is the price d seconds after the signal close minus the signal price, in the trade's direction. **+ means price moved toward the target**, i.e. a worse entry.

| Seconds after close | Mean | Median | p10 | p90 | Toward target / against | 95% CI of the mean |
|---|---|---|---|---|---|---|
| 1 | -1.0 | -1 | -16 | +16 | 44% / 52% | |
| 2 | -2.4 | -1 | -25 | +17 | 47% / 51% | -4.8 to -0.1 |
| 3 | -2.1 | -1 | -26 | +24 | 45% / 53% | -4.8 to +0.6 |
| 4 | -1.7 | -1 | -31 | +27 | 46% / 51% | -4.7 to +1.4 |
| 5 | -1.2 | -1 | -34 | +32 | 47% / 51% | -4.7 to +2.4 |
| 10 | -0.6 | 0 | -39 | +43 | 48% / 49% | |
| 60 | +3.7 | +3 | -84 | +94 | 51% / 49% | |

- **Mean:** within the bot's 0-5 s polling window, average drift is about zero, if anything slightly in the entry's favour. There is no systematic cost to the delay.
- **Spread:** very wide. The standard deviation is about 20-30 ticks, so single trades routinely see ±25 ticks.
- **Where the paper trades fall:** Oct 28's +24 ticks at about 4 s is in the top 13% of the historical distribution; Oct 29's +10 ticks at about 5 s is in the top 34%. Both are unlucky draws, not evidence of a structural problem.
- **Consistency check:** the last 1-second price before each signal close equals the 1-minute signal close in 272 of 272 cases.

## 2. Ambiguities resolved

- **2024-12-05 (the last replay mismatch):** the 1-second data shows the 09:35 stop was hit *after* any fill between 0 and 5.2 s. **The backtest was right.** The replay/paper broker missed it because it ignores the entry minute's range and samples the price only every ~5 s. This is the paper-broker blind spot, now flagged in the audit as "paper-uncertain" (commit 984fbea).
- **The 10 entry-minute stop-outs:** all 10 are confirmed to happen after the fill. The backtest is correct on all of them.
- **Replay harness:** the look-ahead is fixed. Live-loop replay now matches 507/508 sessions; the remaining one is 2024-12-05, explained above.

## 3. Original vs execution-adjusted (the comparison you requested)

Fills use the last trade price at the stated delay plus 1 tick; stop and target are anchored to that fill (ProjectX semantics). The entry minute's bracket checks use only the 1-second bars after the fill. Monte Carlo: 10,000 paths × 500 trading days, 5-day blocks; the 90% interval comes from an outer bootstrap of the history.

| Scenario | Trades | Net (2 yrs) | Avg R (95% CI) | PF | Max intraday DD | 50K pass / fail | 150K pass / fail / open |
|---|---|---|---|---|---|---|---|
| **A. Original frozen baseline** (1-min bars, next open + 1 tick) | 272 | $6,125 | +0.116 (-0.043, +0.275) | 1.20 | -$3,125 | 57% / 43% | 28% / 11% / 61% |
| **B. Measurement-corrected** (1-s entry minute, fill at the close + 1 tick) | 272 | $6,054 | +0.115 (-0.043, +0.274) | 1.20 | -$3,121 | 56% / 44% | 29% / 11% / 60% |
| C. Paper-like delay (random 0.2-5.2 s per trade + 1 tick); 12 delay draws | 271-272 | median $8,161 (range $7,338 - $9,192) | median +0.153 (range +0.137 - +0.175) | ~1.28 | about -$3,290 | 68% / 32%* | 52% / 5% / 43%* |
| E. Fixed 5.2 s delay (slowest observed) + 1 tick | 272 | $6,864 | +0.129 (-0.033, +0.296) | 1.23 | -$3,066 | 61% / 39% | 35% / 9% / 56% |
| **F. Conservative: C + 3 ticks entry slippage and 3-tick stop slippage** | 273 | $6,766 | +0.128 (-0.036, +0.294) | 1.22 | -$3,326 | 58% / 42% | 35% / 10% / 55% |

\* Monte Carlo for C was run on a single delay draw (net $8,721), which is above the 12-draw median.

Reading:
1. **Correcting the measurement (A to B) changes almost nothing:** -$71 over 2 years.
2. **Realistic polling delay does not reduce the edge.** The improvement in C is real in all 12 draws, but it comes from only 5-9 trades whose outcome changes, mostly stops that become targets because the fill-anchored stop sits slightly further out. The mean drift's confidence interval includes zero from 3 s on.
   - We should read this as **"no measurable execution cost from the delay," not as a gain.**
   - Planning estimate: **B or F, roughly +0.12R, the same as the baseline.**
3. **The earlier constant-adverse-drift scenarios** (+0.04R at 17 ticks on every trade) assumed every fill is bad. The 1-second data shows that doesn't happen on average, so those were stress tests, not estimates.
4. **Unchanged:** the edge is still statistically unproven (the 95% CI includes zero in A, B, E and F), and Combine survival is unchanged by execution (50K fails about 42-44%; 150K about 10-11% fail with most runs still open after 500 days).

## 4. What remains open

- **Real broker slippage:** unmeasured until the Practice drill and live fills. Scenario F assumes 3 ticks on entries and stops; break-even total entry cost is about 22 ticks.
- **Forward-test monitoring (proposal; doesn't change the pre-registered criteria):** drift has sd ≈ 25 ticks, so after 20 trades the standard error is about 5.6 ticks. Flag execution risk if the running mean drift exceeds **+11 ticks (2 SE)** after 20+ trades. Latency is now logged per stage (commit 984fbea) to separate data lag from poll phase.

## 5. Changes made in Phase 4A (all regression-tested; the frozen fingerprint is unchanged)

- **Bot (commit 984fbea, 152 tests; documented in docs/phase4/CHANGES.md):**
  - per-stage latency logging and a `latency_report` command;
  - `equity_after` = `equity_before` + `net_pnl`, with a note on the two earlier rows (values not edited);
  - "paper-uncertain" audit flag.
- **Research tooling only:** a replay-harness look-ahead fix, and a 1-second entry hook in the research simulator (off by default; the baseline reproduces exactly: 272 trades, $6,125, PF 1.20).

**Conclusion:** Strategy A's historical result survives realistic signal-to-order timing, with no measurable cost. The remaining execution unknown is real broker slippage. Ready to proceed to Phase 4C (Topstep rule fidelity), with the Practice drill to measure real fills.
