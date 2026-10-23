# TopstepX sweep bot: working rules for Claude Code

Pete's automated futures bot on the TopstepX (ProjectX Gateway) API. Windows laptop, PowerShell,
Python venv `.pyvenv`, Arizona time (no DST; ET-3 until Nov 1, ET-2 after). Planning, reviews and
reports happen in a separate Claude chat ("Sweep Strategy Review" doc); this repo is where code changes.

## Current status (update this section when it changes)
- Strategy A is FROZEN for a forward test: MNQ liquidity sweep of prior-day / overnight high-low,
  ES session-VWAP confirmation, structural stop 1 tick past the wick, 2R target, $250 risk incl.
  costs (`size_with_costs`), max 10 MNQ, 2 trades/day, -$500/+$750 daily, entries until 11:00 ET,
  flat 11:55 ET. Config: `config-sweep.yaml` (paper, `--dry-run`).
- Operational validation week: 2026-10-23 and 10-26..10-29. Daily: `python -m orbbot.audit --config config-sweep.yaml`.
- Statistical sample starts 2026-11-02 (pre-registered; moves only if the operational week fails).
- Next phase after a clean week: Phase 4 simulator robustness, in this order: intrabar accuracy,
  Topstep-rule fidelity, Monte Carlo uncertainty, account economics.

## Hard rules
1. Do NOT change entry/exit signals, confirmation, stops, targets, sizing, risk limits, trading window,
   `forward_test:` dates or forward-test thresholds. `python -m orbbot.audit` fingerprints these and
   FAILs on any change. If a change seems necessary, stop and say so; it is Pete's decision.
2. Live trading stays disabled (`safety.live_trading_enabled: false`). Never add a path that sends
   real orders without both that switch and `--live`.
3. Never resend `Order/place`. Ambiguous outcomes go through reconciliation (`orbbot/execution.py`).
4. Never edit `state/`, `journal/`, `audit/`, `logs/` or `data/` by hand: they are the forward-test
   record. Read them freely.
5. Tests must use `tmp_path` and the fake clock (`tests/fakes.py`); a test that touches the wall
   clock or real journals is a bug (`tests/conftest.py` guards the clock).
6. Credentials only from env vars `TOPSTEPX_USERNAME`, `TOPSTEPX_API_KEY`, `DATABENTO_API_KEY`.
   Never print, log or commit them. Databento downloads cost money: show the price and ask first.
7. Work on a branch, commit with clear messages, never force-push or rewrite history.

## Before saying any code change is done
- `python -m pytest` passes (136+ tests).
- If anything under `orbbot/` that the bot or research uses changed: confirm signals are unchanged
  (research baseline for Strategy A with 11:00 cutoff, roll fill, $1.22 fees, cost-inclusive $250:
  272 trades, $6,125 net, PF 1.20 over Oct 2024 - Oct 2026).
- Never change the bot while it is running a session (6:25-8:55 AZ until Nov 1, 7:25-9:55 after);
  prepare the change and say when to restart.

## Map
- `orbbot/bot.py` session loop (`Session.poll`), modes `--dry-run` / `--read-only` / `--live`
- `orbbot/execution.py` order state machine, reconciliation, protection checks, flatten, freshness,
  Topstep MLL guard (end-of-day balance after 3:10 PM CT)
- `orbbot/state.py` SQLite state + forward-test record (`python -m orbbot.state state/paper.sqlite out.csv`)
- `orbbot/paper.py` durable paper broker (`state/paper_broker.json`)
- `orbbot/audit.py` daily operational audit
- `orbbot/research.py`, `backtest.py`, `features.py`, `filters.py` research engine (Sweep lives in research.py)
- `orbbot/databento_fetch.py` data (CSV has `instrument_id`; `--roll-fill` for contract rolls)
- `tests/fakes.py` fault-injecting fake broker, fake clock, scenario driver for `Session`

## Facts that are easy to get wrong
- ProjectX: customTag unique per account; errorCode 6/7 and duplicate-tag answers are ambiguous;
  Account/search returns balance only (no MLL); bracket legs aren't linked to their parent order.
- TopstepX fees $1.22/contract round turn for MNQ/MES. Topstep trading day 5:00 PM - 3:10 PM CT.
- Databento `.v.0` continuous data switches contract at an evening open; levels must come from the
  traded contract only (handled in `features.py`).
