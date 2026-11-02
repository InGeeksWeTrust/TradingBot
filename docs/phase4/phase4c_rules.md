# Phase 4C: Topstep rule fidelity

Base for this phase: **scenario B**, the frozen signals with the entry minute evaluated on 1-second data (272 trades, $6,054, +0.115R; identical to the original baseline within $71). Rules were checked against Topstep's help center and product page on 2026-10-30; sources are at the end.

## 1. Rules vs our Combine simulator

| Rule (official) | Simulator | Status |
|---|---|---|
| **MLL** $2,000 / $3,000 / $4,500 (50K/100K/150K). Checked in real time on realized + unrealized P&L; fail if the balance "hits or goes below" it | Fails when EOD balance + the day's lowest intraday equity (bar extremes while in a position, fees included) is ≤ the floor | Matches (≤, not <) |
| **MLL trails the end-of-day balance**, never moves down, and locks at the starting balance | floor = min(start, max(floor, EOD balance − MLL)), updated after each day | Matches |
| **Trading day** 5:00 PM CT to 3:10 PM CT; flat by 3:10 PM CT (Topstep starts flattening at 3:08) | Days are ET sessions; Strategy A trades only 09:30-11:55 ET (08:30-10:55 CT), inside a single Topstep day. The bot records the EOD balance after 3:10 PM CT | Matches; the boundary can't split a trade |
| **Profit target** $3,000 / $6,000 / $9,000 | Pass when the EOD balance − start ≥ target | Matches |
| **Consistency**: best day < 55% of the target (breaching it raises the target; not a failure) | Not modelled | Never binds: Strategy A's best day is capped at $750 by its daily profit limit, vs $1,650 (50K) / $3,300 / $4,950. The largest day in 2 years was $750 |
| **Max position** 50 / 100 / 150 micros | Strategy cap 10 MNQ | Never binds |
| **Daily Loss Limit**: optional, fixed at purchase ($1,000 / $2,000 / $3,000); a breach locks the session, not a failure | Not modelled | Never binds: the strategy's own -$500 daily stop is tighter. Worst day: -$443 (B), -$474 with 8-tick slippage |
| **Prohibited conduct**: automation allowed (no exceptions for bot errors); no "ultra-high speed"/HFT, no trading the full max position into scheduled major news, no holding within 2% of a price-limit lock, flat by 3:10 PM CT | ≤2 trades/day held minutes to hours; ≤10 of 50 micros; entries 09:35-11:00 ET (after 08:30 data, before 14:00 FOMC); flat 11:55 ET | Compliant. A price-limit guard isn't implemented; the risk is low but not zero on a crash morning |
| **Fees** | $1.22 per round turn assumed | The Practice drill charged $0.72. Combine fees not yet confirmed (sensitivity below) |

**Open question for Topstep support:** the "When and what products can I trade" page marks **MNQ with an asterisk** linking to Prohibited Conduct, but neither the Prohibited Conduct nor the Prohibited Trading Strategies article mentions MNQ or micros. We should ask Topstep what the asterisk means before any paid Combine.

## 2. Intraday equity and MLL enforcement

- Intraday equity is evaluated at the adverse extreme of every 1-minute bar while in a position, so any touch of the floor counts. That's exact for a "hits or goes below" rule, because fills don't matter for a touch.
- **The entry-minute flaw is fixed in B.** The original baseline counted the whole entry minute's range, including the part before the fill. B uses only the 1-second bars after the fill. Survival effect: 50K fail 43% to 44%, 150K unchanged at 11%. Negligible.
- Planned vs realized risk: the planned trade risk is ≤ $250 including estimated costs. Realized losses exceed it only through stop slippage beyond 1 tick (below). The largest single-day loss across all scenarios is $474, inside the -$500 daily stop and well inside every MLL.

## 3. Stop-fill uncertainty (1-second data, all 147 stop-outs in B)

For a stop-market order, the fill happens somewhere in the second that first trades through the stop.

| Measure (ticks beyond the stop) | Mean | Median | p90 | p99 | Max |
|---|---|---|---|---|---|
| Worst price within the trigger second (upper bound) | 16.0 | 11 | 35 | 66 | 74 |
| Close of the trigger second (plausible fill) | 6.6 | 3 | 19 | 48 | 55 |
| Trigger second opened already beyond the stop (gap) | 0 | 0 | 0 | 0 | 0 |

- **Practice reality:** both filled drill stops on the TopstepX simulator filled exactly at the stop price (0 ticks). Combines run on the same simulator, so for passing a Combine, simulator fills are what count. The 1-second figures describe a real exchange and are the stress case.

Sensitivity (on B; the slippage applies to entries, stops and time exits; Monte Carlo 10,000 × 500 days, 5-day blocks):

| Scenario | Net (2 yrs) | Avg R | Max DD | 50K pass / fail | 100K pass / fail / open | 150K pass / fail / open |
|---|---|---|---|---|---|---|
| B, $1.22 fees, 1-tick slippage | $6,054 | +0.115 | -$3,121 | 57% / 43% | 54% / 30% / 16% | 29% / 11% / 60% |
| B, $0.72 fees (Practice rate) | $7,267 | +0.129 | -$3,102 | 61% / 39% | 64% / 24% / 13% | 39% / 9% / 53% |
| B, 4-tick slippage | $5,122 | +0.097 | -$3,183 | 51% / 49% | 47% / 36% / 17% | 22% / 15% / 63% |
| B, 8-tick slippage (about the median trigger-second close plus entry) | $3,570 | +0.066 | -$3,428 | 42% / 58% | 34% / 48% / 18% | 13% / 24% / 63% |

Reading:
- The edge survives realistic stop slippage, thinner.
- The 50K is fragile in every case: 39-58% fail. Its $2,000 MLL is only 8 full-risk losses.
- The 100K offers the best pass-to-fail ratio of the three on these numbers.
- The 150K rarely fails, but most attempts are still running after 500 days.
- Fees matter about as much as 4 ticks of slippage. Confirming the Combine's actual MNQ fee is worth doing.

## 4. Findings to carry forward

1. The simulator's MLL, lock, target and trading-day logic match Topstep's published rules. No correction needed.
2. The consistency rule, DLL and position limits never bind at the frozen settings, so not modelling them doesn't bias the results. This should be re-checked if risk per trade is ever raised: at $1,500 risk the daily profit cap of $4,500 would approach the 150K's $4,950 consistency threshold.
3. **Express Funded Account rules differ** and are not yet modelled:
   - the MLL starts at -$2,000 from a $0 balance and locks at $0 once the balance reaches $2,000;
   - **after the first payout the MLL is set to $0**;
   - payouts are capped ($2,000 / $3,000 / $5,000 standard).
   These go into the account-economics phase (4E).
4. The Monte Carlo docstring calls its resampling "stationary", but it uses fixed 5-day blocks. That will be corrected in 4D, which adds a true stationary bootstrap, block sizes 1/5/10/20, per-replicate delay and cost draws (per your note on Phase 4A), and pass/fail/open at 250 and 500 days.
5. To confirm with Topstep: the MNQ asterisk, and the Combine's MNQ round-turn fee.

## Sources

- [Trading Combine Parameters](https://intercom.help/topstep-llc/en/articles/8284197-trading-combine-parameters) (page dated Sep 24, 2026)
- [What is the Trading Combine? (MLL)](https://help.topstep.com/en/articles/8284204-what-is-the-trading-combine) (Sep 18, 2026)
- [Daily Loss Limit in the Trading Combine and XFA](https://help.topstep.com/en/articles/10490293-daily-loss-limit-in-the-trading-combine-and-express-funded-account) (Jun 30, 2026)
- [When and what products can I trade](https://help.topstep.com/en/articles/8284206-when-and-what-products-can-i-trade)
- [Prohibited Trading Strategies](https://help.topstep.com/en/articles/10305426) (Jun 10, 2026) and [Prohibited Conduct](https://help.topstep.com/en/articles/10296582)
- [Payout Policy](https://help.topstep.com/en/articles/8284233)
- [Topstep pricing / account parameters](https://www.topstep.com/topstep-prop)
