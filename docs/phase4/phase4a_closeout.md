# Phase 4A close-out: three verification checks

## 1. The ten entry-minute stop-outs: resolved, none ambiguous

Each row gives the first 1-second bar whose range reaches the stop, measured from the signal close. The fill happens 0-5.2 s after the close (scenario range).

| Day | Trade | Stop | First touch |
|---|---|---|---|
| 2024-11-06 | short onh | 20,748.50 | +12 s |
| 2024-11-26 | short onh | 20,992.50 | +27 s |
| 2024-12-05 | short pdh | 21,551.00 | +54 s |
| 2025-06-25 | short onh | 22,515.25 | +40 s |
| 2025-07-22 | long onl | 23,258.75 | +52 s |
| 2025-09-23 | long onl | 24,969.75 | +34 s |
| 2025-10-09 | long onl | 25,289.75 | +49 s |
| 2026-02-13 | long pdl | 24,708.25 | +23 s |
| 2026-07-30 | short onh | 27,892.75 | +37 s |
| 2026-09-16 | short onh | 29,153.75 | +50 s |

- Every stop is first reached at least 12 s after the signal close, so always after any 0-5.2 s fill.
- None falls in the same second as the fill, so the order of fill and stop is unambiguous for all ten.
- These stop prices are the baseline's. In the delay scenarios the stop moves with the fill by a few ticks; the 1-second simulation evaluates those directly, and all ten remain stop-outs in every scenario (one becomes a later exit in scenario E).

## 2. Uncertainty across the random-delay draws: carried into the Monte Carlo phase

Scenario C's Monte Carlo used one delay draw ($8,721 net), which is above the 12-draw median ($8,161; range $7,338 - $9,192). In Phase 4D, each Monte Carlo replicate will draw:
- its own delay realisation, uniform 0.2-5.2 s per trade;
- its own execution-cost realisation: entry slippage and stop slippage drawn from the Practice-drill distribution once there are enough fills, and until then from a stated prior (for example 0-6 ticks on entries and 0-4 ticks on stops).

Results will be reported as distributions, not one favourable draw.

## 3. Why scenario F has 273 trades: 2025-03-17

| | Baseline A | Scenario F |
|---|---|---|
| 09:50 short (onh sweep) fill | 19,753.25 | 19,745.50 (2.67 s delay: 7.75 pts of drift + 3 ticks slippage) |
| Stop (74.25 pts from the fill) | 19,827.50 | 19,819.75 |
| Outcome | held; time exit 11:55, +$112.78 | stopped at 09:55 (price reached ~19,820, below A's stop), -$151.22 |
| 10:30 short (pdh sweep) | ignored: the position was still open | taken: account flat |
| 10:30 trade | | 1 MNQ, stop 83 pts ($166 + costs), time exit 11:55, +$21.78 |
| Day | +$112.78 (1 trade) | -$129.44 (2 trades) |

The extra trade complies with every frozen rule:
- signal at 10:30 ET, before the 11:00 cutoff;
- 2nd trade of the day, within the cap of 2;
- a different level (pdh) from the first trade (onh);
- planned risk $166 + ~$2 costs, under the $250 cap;
- realised loss before it was $151.22, so the headroom of $500 - $151 - $25 = $324 covers it.

This is the same logic the live bot applies: a sweep that occurs while a position is open is consumed but not traded. The trade count differs only because a fill-anchored stop, moved by the fill, changed whether the first position was still open at 10:30. The signal rules are unchanged.
