# Phase 4E (final): evaluation-to-payout economics of Strategy A at Topstep

Supersedes the earlier Phase 4E draft. Strategy A's signals, risk ($250/trade), the forward-test criteria and the Practice records are unchanged. Everything here is research simulation. **Positive modelled expected value is not evidence that Strategy A is profitable**: the edge is statistically unproven, and the zero-edge and negative-edge rows describe states of the world that are still entirely plausible.

Rules and prices are as published on 2026-10-30 (sources at the end).

## 1. Corrections made in this version

| ChatGPT item | Change |
|---|---|
| 30-day billing | Subscriptions now rebill every **30 calendar days** (modelled as 20.71 trading days). **Any Reset, purchased or from a Reset Credit, pushes the next rebill to 30 days after the reset.** Each rebill adds one Reset Credit (credits persist; they expire after 1 year, which never binds here). A breached Combine keeps rebilling until it is reset or cancelled. Three breach policies are compared (section 3). |
| API cost | The ProjectX API is shown as its own line: **$14.50/month** ($29 list, 50% off with code "topstep"), charged every 30 days while the account is in use. |
| Payout fees | Prop-to-Brokerage, Aeropay and Wise: $0 Topstep fee (base case). ACH or wire: $30 per payout (shown as a sensitivity). |
| Sunk costs | The existing 50K's purchase is excluded. Only future charges count. |
| Single-attempt pass | Reported separately from the 2-year pass rate with resets. |
| Wording | The new 50K also has positive modelled expectancy under the historical edge. Buying one isn't justified by the present evidence. |
| Metrics kept apart | P(pass), P(any payout), E[net], P(net > 0) and their uncertainty ranges are reported separately throughout. |
| Live Funded limitation | Added (section 6). |
| Paired draws | Every comparison within an account size (edge scenario, breach policy, payout policy and path, existing vs new) uses identical random draws: execution realization, history resample and Monte Carlo day paths. |

## 2. Model summary

| Stage | Rule |
|---|---|
| Combine | $49 / $99 / $199 per 30 days (Standard path; No Activation Fee path $95 / $149 / $229). Reset price = monthly price. Pass = EOD balance ≥ start + $3,000 / $6,000 / $9,000. MLL is real-time on intraday equity, trails EOD, locks at the start. |
| Pass | Standard path: $149 activation. No Activation Fee: $0. |
| XFA | Balance $0. MLL -$2,000 / -$3,000 / -$4,500, trailing EOD, locks at $0, **$0 permanently after the first payout**. A breach closes the XFA and the path ends. Scaling plan (≥20 micros) never binds at ≤10 MNQ. |
| Payouts | **Standard:** 5 days ≥ $150 since the last payout, plus profit since the last payout (the first is exempt). Cap $2,000 / $3,000 / $5,000. **Consistency:** 3 trading days, profit, best day ≤ 40% of the cycle profit. Cap $3,000 / $4,000 / $6,000. Both pay min(50% of balance, cap), minimum $125. Trader keeps 90%. |
| Payout timing | **Immediate:** first eligible day. **Buffer:** only once the XFA balance ≥ 2 × MLL. |
| Execution | TopstepX-simulator-like fills (stops fill at the stop + 1 tick, as seen in the Practice drills). 8 realizations with per-trade entry delay 0.2-5.2 s and 1-6 ticks of entry slippage. Trading P&L at $1.22 fees. |
| Uncertainty | 40 outer replicates (execution realization + stationary-bootstrap history) × 2,000 paths × 500 trading days (~2 years). Ranges are 5th-95th percentiles across replicates. |
| Edge scenarios | Historical; zero edge; -0.05R. Recentred by charging c × planned risk per trade at entry, inside the trade simulator. |

## 3. Breach policy: immediate reset vs waiting for a credit vs cancelling

50K, Standard purchase, Standard payouts, immediate payout timing:

| Breach policy | Edge | P(pass) | P(any payout) | E[spend + API] | E[net] [5-95%] | P(net > 0) |
|---|---|---|---|---|---|---|
| Immediate reset (credit if banked, else buy) | historical | 93% | 80% [53-96] | $591 + $188 | +$408 [-718 .. +2,252] | 36% |
| Wait for the next rebill's credit | historical | 93% | 80% | $591 + $188 | +$408 | 36% |
| Cancel at the first breach (single attempt) | historical | 61% | 55% [22-86] | $375 + $118 | +$399 [-291 .. +2,042] | 27% |
| Immediate reset | zero edge | 69% | 50% [14-85] | $790 + $247 | -$667 [-1,246 .. +153] | 13% |
| Cancel at the first breach | zero edge | 29% | 23% | $306 + $89 | **-$211** [-325 .. +69] | 8% |

- **Waiting for a credit saves nothing.** Because a Reset pushes the rebill out 30 days, a waiting account still pays the rebill that grants the credit. It spends the same and only loses trading days (identical to the dollar in these runs; passes come later).
- **Cancelling after one failed attempt** keeps about the same expected value under the historical edge (+$399 vs +$408). It cuts the zero-edge loss by about two-thirds (-$211 vs -$667) and halves the 95th-percentile spend ($926 vs $1,343 including the API). Given the unproven edge, it's the more defensible policy.

## 4. Results by account (2 years, immediate reset unless noted)

### Pass vs payout, reported separately

| Account | Edge | Single-attempt pass | Pass within 2 years (with resets) | P(any payout) [5-95%] |
|---|---|---|---|---|
| 50K | historical | 61% | 93% | 80% [53-96] |
| 50K | zero | 29% | 69% | 50% [14-85] |
| 50K | -0.05R | 20% | 57% | 38% [8-76] |
| 100K | historical | 60% | 69% | 61% [21-94] |
| 100K | zero | 18% | 25% | 19% [2-54] |
| 150K | historical | 38% | 38% | 33% [2-82] |
| 150K | zero | 8% | 8% | 6% [0-26] |

The 93% 2-year pass rate for the 50K includes repeated paid resets (0.74 on average). With a single attempt it's 61%. **A zero-edge strategy still passes a 50K 69% of the time with resets.**

### Expected economics, new accounts (Standard purchase path)

| Account / payout | Edge | E[spend] | E[API] | E[withdrawn] | E[net] [5-95%] | P(net > 0) | Median trading days to first payout |
|---|---|---|---|---|---|---|---|
| 50K Standard payouts | historical | $591 | $188 | $1,187 | +$408 [-718 .. +2,252] | 36% | 179 |
| 50K Consistency payouts | historical | $591 | $234 | $2,144 | +$1,318 [-616 .. +4,876] | 54% | 199 |
| 50K buffer (Standard) | historical | $591 | $298 | $2,799 | +$1,910 [-811 .. +6,521] | 50% | 294 |
| 50K Consistency | zero | $790 | $266 | $573 | -$483 [-1,245 .. +922] | 21% | |
| 50K Standard | -0.05R | | | | -$889 [-1,308 .. -223] | 9% | |
| 100K Consistency | historical | $1,798 | $319 | $1,286 | -$830 [-2,392 .. +1,880] | 23% | 328 |
| 100K Standard | historical | $1,798 | $288 | $789 | -$1,297 [-2,427 .. +397] | 14% | |
| 150K Consistency | historical | $4,389 | $350 | $645 | -$4,094 [-5,209 .. -1,848] | 6% | 394 |
| 150K Standard | historical | $4,389 | $337 | $446 | -$4,281 | 4% | |

Notes:
- **No Activation Fee path:** lower E[net] in every case (about $285 lower for the 50K under the historical edge; $540 lower under zero edge). Slow passes make the higher monthly fee cost more than the $149 it saves.
- **ACH/wire payouts at $30 each:** lower E[net] by about $45-$65 for the 50K (1.6-2.1 payouts on average).
- **Realistic 2-year exposure, spend + API at the 95th percentile:** 50K about $1,340 (about $930 if cancelling at the first breach); 100K about $2,810; 150K about $5,300.

## 5. Existing 50K Combine vs a new one

Costs already paid are sunk. The rows below assume the existing account is at its **starting balance and MLL floor, with no banked credits, no DLL, and its current 30-day period already paid**. **These facts are unconfirmed**, and the figures will be recomputed once Pete supplies the real balance, MLL floor, subscription date, credits and DLL status.

| Existing 50K | Edge | E[net] [5-95%] | P(net > 0) | P(any payout) |
|---|---|---|---|---|
| Immediate reset, Standard payouts | historical | +$457 [-669 .. +2,301] | 38% | 80% |
| Immediate reset, Consistency payouts | historical | +$1,367 [-567 .. +4,925] | 56% | 71% |
| Immediate reset, buffer payouts | historical | +$1,959 [-762 .. +6,570] | 50% | 50% |
| Cancel at first breach, Consistency payouts | historical | +$1,209 [-177 .. +4,575] | 43% | 49% |
| Immediate reset, Consistency payouts | zero | -$434 [-1,196 .. +971] | 23% | 38% |
| Cancel at first breach, Consistency payouts | zero | -$45 [-261 .. +678] | 14% | 18% |

A new 50K is identical except for the first $49 period.

## 6. Model limitations

- **Billing approximation.** Topstep bills every 30 calendar days. The model converts that to an average of 20.71 trading days, because simulated paths are sequences of trading days without calendar dates. Real rebill dates fall on specific calendar days, so weekends, holidays and early closes shift how many trading days each paid period contains: anywhere from about 19 to 22. This moves individual charges by a few days. It has a negligible effect on the expected totals, but it isn't an exact schedule.
- **Early move to Live Funded.** Topstep may move an XFA to a Live Funded Account at its discretion, before the modelled 2-year horizon ends. The bot can't trade a Live Funded Account (see below), so such a move would end automated trading and the XFA withdrawals modelled after that point. The withdrawal estimates assume the account stays an XFA for the whole horizon, or until it fails.

### Live Funded Account and the API

Topstep's API terms state that **Live Funded Accounts cannot trade through the ProjectX API**. These economics therefore cover only the Combine-to-XFA pathway.
- If Topstep moved the account to Live Funded, the bot could not continue, and any later income would depend on manual trading.
- Withdrawals after the 2-year horizon are also not counted.

## 7. Account-by-account recommendation

| Account | Recommendation | Reasons | Realistic exposure (2 years) |
|---|---|---|---|
| **Existing 50K** | **Do not trade it yet** (per your instruction). It is the first candidate once forward and Practice evidence support the edge. If deployed: cancel after a failed attempt rather than resetting, and use the Consistency payout path. | Under the historical edge: E[net] +$457 to +$1,959 depending on payout policy, but P(net>0) is only 38-56% and the ranges include losses. Under zero edge: -$45 (cancel policy) to -$618. Downside is limited by Topstep's structure. | ~$49 + $14.50 a month while active; ~$930 at the 95th percentile with the cancel policy |
| **New 50K** | **Do not buy.** | Positive modelled expectancy under the historical edge (+$408 to +$1,910), but a second account adds cost and exposure without adding evidence about the edge. | as above, plus the first $49 |
| **100K** | **Not worthwhile at $250 risk.** | Negative under every edge scenario (-$830 to -$1,297 historical). Too slow: median ~1.3 years to the first payout. | ~$2,800 at the 95th percentile |
| **150K** | **Not worthwhile at $250 risk.** | E[net] about -$4,100 historical; mostly 2 years of $199/month. | ~$5,300 at the 95th percentile |
| **No Activation Fee path** | Not recommended. | Dominated by Standard at these pass speeds. | |

**What would change the picture:**
1. Forward-test evidence on the edge. This is the dominant uncertainty.
2. Confirmed Combine fees: at $0.72 instead of $1.22, trading P&L is about $1,200 higher over 2 years.
3. The 100% split on the first $10,000, if the account qualifies (adds about 11% to withdrawals).
4. Double payout caps with a purchase-time DLL. Minor here: 50% of the balance usually binds before the cap.

Phase 4E is closed with this version, pending the existing account's facts (section 5).

## Sources

- [Trading Combine subscriptions](https://intercom.help/topstep-llc/en/articles/8284121-trading-combine-subscriptions) (30-day rebill; Reset Credits on rebill)
- [What is a Reset](https://help.topstep.com/en/articles/8284128-what-is-a-reset) (rebill pushed 30 days; credit rules)
- [Pricing and payment questions](https://help.topstep.com/en/articles/14289835-topstep-pricing-and-payment-questions)
- [TopstepX API Access](https://help.topstep.com/en/articles/11187768) ($29/month, 50% code; no API on Live Funded)
- [Payout Policy](https://help.topstep.com/en/articles/8284233) (paths, caps, 90/10, payout fees, MLL to $0)
- [What is the Trading Combine](https://help.topstep.com/en/articles/8284204-what-is-the-trading-combine)
- [Trading Combine Parameters](https://intercom.help/topstep-llc/en/articles/8284197-trading-combine-parameters)
- [Scaling Plan](https://help.topstep.com/en/articles/8284223-what-is-the-scaling-plan) (thresholds via [FuturesHive](https://www.futureshive.com/blog/topstep-scaling-plan-explained), a secondary source)
