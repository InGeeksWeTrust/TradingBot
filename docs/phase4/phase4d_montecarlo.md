# Phase 4D: Monte Carlo robustness, plus the two pre-checks

The frozen Strategy A signals are unchanged. Everything here is research simulation on 2 years of MNQ (1-minute bars plus 1-second data for the entry and stop seconds).

## Pre-check 1: the $1,213 fee difference ($1.22 vs $0.72 per round turn), reconciled

| | $1.22 | $0.72 | Difference |
|---|---|---|---|
| Trades | 272 | 272 | same signals |
| Round-turn contracts | 639 | 649 | +10 |
| Fees paid | $779.58 | $467.28 | -$312.30 |
| Gross P&L | $6,833.24 | $7,734.78 | +$901.54 |
| Net P&L | $6,053.66 | $7,267.50 | **+$1,213.84** |

Only 639 contracts were traded, not 2,426, so the direct fee saving is about $320. The rest comes from cost-inclusive sizing and one knife-edge outcome:

| Component | Trades | Effect |
|---|---|---|
| Same size, same outcome: direct fee saving | 261 | +$289.00 ($297.50 at $0.50/contract, less $8.50 from daily-cap exit levels that depend on fees) |
| One more contract fits under the $250 cap (cheaper costs) | 10 (40 to 50 contracts) | +$427.80 (7 winners and 3 losers, each one contract bigger) |
| Outcome flip, 2025-10-31 long onl | 1 | +$497.04 (-$218.88 to +$278.16) |
| **Total** | 272 | **+$1,213.84** |

**The 2025-10-31 flip is a knife-edge:**
- The day's first trade made +$470.34 / +$471.84, so the second trade's daily-profit exit level (where $750 is reached) sat at 26,183.32 at $1.22 and 26,182.88 at $0.72.
- The bar's high was 26,183.00. At $0.72 the cap was "hit", at $1.22 it wasn't, and that trade later stopped out.

**Simulator fidelity issue found:** the daily-profit exit fills at an off-tick price (26,182.88). It should round to the tick against the position, which here gives 26,183.00 for $0.72; at $1.22 the level would round up to 26,183.50, just above the high. This will be fixed in the research simulator.
- Effect on the frozen baseline: at most this kind of single-day flip.
- The live bot doesn't use this code path; it flattens at market when the cap is reached.

## Pre-check 2: intraday MLL logic, deterministic tests (10 of 10 pass)

`test_mll_sim.py` covers both the trade simulator and the Topstep rule engine:

- **Intraday dip, then recovery: the dip counts.** In a day that dips -$161.72 and then hits its target (+$398.78), the simulator records min equity = -$161.72; the later profit isn't netted against it. With the balance at $48,100 (floor $48,000) that day **fails**. At $48,200 it survives.
- **Equity is measured at the moment of the breach**, as balance + intraday min equity measured from the day's start. Realized profit earlier in the day is included, and a later dip is measured on top of it. Nothing is counted twice.
- **"Hits or goes below":** equity exactly at the floor fails; one cent above survives.
- **EOD trailing and the lock:** +$1,500 EOD raises the floor to $49,500. +$2,900 would mean $50,900, but the floor locks at $50,000.
- **Pass on the EOD balance:** $2,999.99 isn't a pass; $3,000.00 is.
- **The Monte Carlo engine matches a deterministic path:** +$100/day passes on exactly day 30. A -$2,000 intraday low fails on day 1.

## 4D setup

**Execution uncertainty.** Each family has 8 realizations; each realization redraws every trade's entry delay and entry slippage.

| Family | Entry | Stop fill |
|---|---|---|
| **sim** (TopstepX simulator, as observed in drills) | price 0.2-5.2 s after the signal (random), plus 1-6 ticks (random) | stop + 1 tick (drills filled exactly at the stop) |
| **uniform** (exchange-like) | same | random between the stop and the worst price of the 1-second trigger bar |
| **tail** (stress) | same | the worst price in the trigger second and the next 2 seconds |

The trigger-second close is not treated as an executable price.

Net P&L across the 8 realizations:

| Family | Net range (2 yrs) | Avg R range |
|---|---|---|
| sim | $5,354 - $8,613 | +0.097 to +0.163 |
| uniform | $5,017 - $6,897 | +0.090 to +0.131 |
| tail | $3,731 - $6,100 | +0.067 to +0.117 |

**Monte Carlo.** 100 outer replicates per cell. Each replicate:
1. draws one execution realization;
2. block-resamples the 2-year history (edge uncertainty);
3. runs 2,000 Combine paths over 500 trading days, with status recorded at day 250 and day 500.

Block schemes: fixed 1, 5, 10 and 20 days, plus a stationary bootstrap (random block lengths, mean 5). The intervals shown are the 5th-95th percentiles across replicates.

## Results: historical edge, stationary bootstrap

| Family | Account | Day 250 pass / fail / open | Day 500 pass [90% range] | Day 500 fail [90% range] | Day 500 open |
|---|---|---|---|---|---|
| sim | 50K | 55 / 40 / 5 | 58 [26-86] | 42 [14-74] | 0 |
| sim | 100K | 27 / 22 / 51 | 57 [12-94] | 30 [4-71] | 13 |
| sim | 150K | 7 / 6 / 87 | 38 [3-86] | 15 [0-48] | 48 |
| uniform | 50K | 54 / 42 / 4 | 57 [21-81] | 43 [19-79] | 0 |
| uniform | 100K | 27 / 24 / 48 | 57 [10-89] | 32 [8-81] | 11 |
| uniform | 150K | 7 / 8 / 86 | 39 [2-79] | 16 [1-64] | 45 |
| tail | 50K | 43 / 53 / 4 | 46 [19-77] | 54 [23-81] | 0 |
| tail | 100K | 20 / 35 / 45 | 42 [8-87] | 47 [10-83] | 11 |
| tail | 150K | 5 / 14 / 81 | 26 [1-75] | 29 [2-66] | 45 |

## Block-length sensitivity (day-500 fail, range across the five schemes)

| Family | 50K | 100K | 150K |
|---|---|---|---|
| sim | 42-43% | 30-31% | 15-16% |
| uniform | 43-49% | 32-40% | 16-22% |
| tail | 49-55% | 40-47% | 22-29% |

Block length moves the answer by at most about 6-8 points. The uncertainty about the edge (the 90% ranges above, often 10-80%) matters far more.

## Edge and stress scenarios (stationary bootstrap, day-500 pass / fail [90% range] / open)

| Scenario | Family | 50K | 100K | 150K |
|---|---|---|---|---|
| Zero edge | sim | 27 / 73 [40-93] / 0 | 19 / 71 [24-97] / 11 | 9 / 52 [8-90] / 40 |
| Zero edge | tail | 24 / 76 [47-96] / 0 | 16 / 75 [35-99] / 9 | 7 / 58 [16-96] / 34 |
| -0.05R per trade | sim | 16 / 84 [61-97] / 0 | 8 / 84 [51-99] / 8 | 2 / 69 [25-98] / 28 |
| -0.05R per trade | tail | 21 / 79 [50-96] / 0 | 13 / 79 [37-99] / 7 | 6 / 64 [13-97] / 30 |
| Losses ×1.25 | sim | 26 / 74 [49-94] / 0 | 19 / 74 [38-99] / 7 | 10 / 58 [18-96] / 32 |
| Losses ×1.25 | tail | 18 / 82 [49-96] / 0 | 12 / 84 [39-100] / 4 | 6 / 74 [19-99] / 20 |
| 15% of trading days missed | sim | 61 / 38 [10-81] / 0 | 58 / 27 [2-78] / 15 | 38 / 13 [0-51] / 50 |
| 15% of trading days missed | tail | 46 / 54 [20-90] / 0 | 40 / 45 [8-90] / 15 | 23 / 27 [1-72] / 50 |

Edge scenarios re-centre each realization's expectancy, so the execution family matters little there; the small family differences are noise and distribution shape.

## What it means

1. **The verdict depends on the edge, not on execution or the bootstrap method.**
   - With the historical edge: 50K fail 42-54%, 100K 30-47%, 150K 15-29% (the range is sim to tail execution).
   - With zero edge: 50K fail 73-76%, 100K 71-75%, 150K 52-58%.
   - The edge is still statistically unproven, so both rows are live possibilities.
2. **A zero-edge strategy still passes a 50K about 25% of the time.** A pass is weak evidence of an edge.
3. **The 50K is the most fragile account in every scenario.** It resolves fastest (almost nothing is still open at day 500) but fails most often.
4. **The 100K has the best pass/fail balance under the historical edge.** Pass is 57% vs 30% fail (sim) and 42% vs 47% (tail).
5. **The 150K rarely fails, but about half its attempts are still running at day 500,** and only 5-7% pass within 250 days. At $250 risk it's slow; the economics phase (4E) will price that time.
6. **Stop execution matters.** Moving from simulator-like stops to tail stops costs about 10-15 points of pass rate. On a Topstep Combine the TopstepX simulator fills stops, so "sim" is the relevant family; "tail" is what a live exchange account could see.
7. **Missing 15% of trading days hardly changes the odds.**

## Next: Phase 4E (account economics)

4E will combine these outcome distributions with:
- evaluation costs ($49 / $99 / $199 per month, reset fees, the $149 XFA activation fee);
- time to pass;
- XFA rules (the MLL starts $2,000 / $3,000 / $4,500 below a $0 balance, locks at $0, and **resets to $0 after the first payout**; payout caps);
- the edge uncertainty: historical vs zero edge, not one point estimate.
