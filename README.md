# MNQ Opening Range Breakout bot — TopstepX API

Python bot that trades the 9:30 ET opening range breakout on MNQ through the TopstepX
(ProjectX Gateway) API, Mon–Fri, with a hard daily loss limit and daily profit cap, and
journals every trade to Excel.

## The trading day

All strategy times are Eastern. Arizona doesn't change clocks but New York does, so the
Arizona time of the open moves twice a year. The bot handles this for you.

| ET | AZ summer (Mar–Oct) | AZ winter (Nov–Mar) | Step |
|---|---|---|---|
| 09:25 | 06:25 | 07:25 | Wakes, logs in, picks the active MNQ contract, reads today's P&L |
| 09:30–09:45 | 06:30–06:45 | 07:30–07:45 | Builds the opening range |
| 09:45–11:00 | 06:45–08:00 | 07:45–09:00 | Enters on a 5-min close outside the range |
| any time | | | Flattens and stops for the day at −$500 or +$1,250 (realized + open) |
| 11:55 | 08:55 | 09:55 | Flattens anything open, cancels leftover orders |

Entries carry a server-side stop + target (OCO bracket), so positions stay protected if the
bot or your network dies.

### Risk math
MNQ is $2/point: 10 contracts = $20/pt, 20 contracts = $40/pt. Every bracket risks
`max_risk_per_trade` ($250) to make `target_per_trade` ($550), a 2.2R trade:

| Size | Stop | Target |
|---|---|---|
| 10 MNQ | 12.5 pts ($250) | 27.5 pts ($550) |
| 20 MNQ | 6.25 pts ($250) | 13.75 pts ($550) |

If the day's remaining loss headroom can't cover a full $250 (e.g. after a loss, once fees and
the $25 `loss_buffer` are counted), size is reduced and the bracket keeps the same 2.2R shape.
Targets are trimmed so a win can't overshoot $1,250. Set `stop_mode: opposite` to use the
range-based stop instead (capped at $250).

## Three ways to run it

| Mode | Command | Orders go to | Journal |
|---|---|---|---|
| Backtest | `python -m orbbot.backtest` | nowhere (history) | `journal/backtest.xlsx` |
| Paper | `python -m orbbot.bot --dry-run` | nowhere (simulated fills on live prices) | `journal/paper.xlsx` |
| Live | `python -m orbbot.bot` | the account named in `config.yaml` | `journal/live.xlsx` |

**Recommended path:** backtest → 1–2 weeks of paper → 2+ weeks live on a TopstepX
**practice account** → combine/XFA. Paper mode tests the strategy; the practice account tests
the plumbing (real order acceptance, bracket signs, fills, fees) without real money.

Paper mode still logs in and needs `account_name` set (it reads market data), but it never
sends an order, so it doesn't matter which account you point it at.

## Setup

1. **API access**: TopstepX → Settings → API. Link ProjectX, subscribe to API access,
   generate a key. Confirm Topstep's current rules on automated trading for your account type.
2. **Bracket mode**: set TopstepX to **Auto OCO Brackets** (not Position Brackets) on the
   account you trade live. If a live entry ever fills without a working stop, the bot flattens
   and halts for the day.
3. Install:
   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   export TOPSTEPX_USERNAME="..."
   export TOPSTEPX_API_KEY="..."
   ```
4. Run once with `account_name: ""` — it lists your accounts and exits. Paste your practice
   account name into `config.yaml`.

## Running it every weekday

The bot is a long-running process; it doesn't need to be started at a precise time. Pick one:

**A. Scheduled (recommended, Linux/homelab).** `deploy/orb-bot.timer` starts the bot at
09:15 ET each weekday; `--once` makes it run that day's session and exit. The timer uses the
New York time zone, so daylight saving is handled.
```bash
sudo useradd -r -s /usr/sbin/nologin orbbot
sudo mkdir -p /opt/orb-bot && sudo cp -r . /opt/orb-bot && sudo chown -R orbbot: /opt/orb-bot
cd /opt/orb-bot && sudo -u orbbot python3 -m venv .venv && sudo -u orbbot .venv/bin/pip install -r requirements.txt
sudo install -m 600 /dev/null /etc/orb-bot.env   # then add the two TOPSTEPX_ lines
sudo cp deploy/orb-bot.service deploy/orb-bot.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now orb-bot.timer
systemctl list-timers orb-bot.timer        # shows the next start
journalctl -u orb-bot -f                   # watch today's session
```
The shipped service has `--dry-run` (paper). Remove it from `ExecStart` when you go live, then
`sudo systemctl daemon-reload`.

**B. Always on.** Run `python -m orbbot.bot` (no `--once`) under systemd, `tmux`, or
similar. It sleeps outside session hours and skips weekends and the holidays in `config.yaml`.

**C. Windows.** Task Scheduler → Create Task → Trigger: Weekly, Mon–Fri, 6:15 AM (change to
7:15 AM in November and back in March, since Windows uses your Arizona clock) → Action:
`C:\path\to\orb-bot\.venv\Scripts\python.exe` with arguments `-m orbbot.bot --once --dry-run`
and "Start in" set to the orb-bot folder. Set the two `TOPSTEPX_` variables as user
environment variables. The machine must be awake.

**Manual** works too: `python -m orbbot.bot --once --dry-run` any morning before the open.

## The Excel journal

Each closed trade is appended to `journal/<mode>.csv` and the matching workbook is rebuilt
immediately, so it's current while the session runs:

- **Summary**: trades, win rate, net P&L, avg win/loss, profit factor, avg R, best/worst day,
  max drawdown, days that hit each limit
- **Daily**: one row per session with trades, wins, net P&L, running equity, drawdown, and
  whether a limit was hit
- **Trades**: every round trip with entry/exit times and prices, exit reason
  (target/stop/time/limit), planned stop and target, planned risk, net P&L and R multiple

Because backtest, paper and live write the same layout, you can put them side by side and see
whether live behaves like the backtest. If a workbook is open in Excel when a trade closes,
the CSV still updates and the workbook is retried every poll, so it catches up once you close it.

## Other commands

```bash
pytest -q                                              # logic tests
python -m orbbot.backtest --days 30                    # current contract, from the API
python -m orbbot.backtest --csv mnq_1m.csv --contracts 20 --out journal/backtest-20ct.xlsx
python -m orbbot.bot --contracts 20 --dry-run
touch STOP                                             # kill switch: flatten + halt for the day
```

## Files
- `orbbot/strategy.py` — ORB signal logic (shared by live, paper and backtest)
- `orbbot/risk.py` — stop/size/target planning, daily guard
- `orbbot/api.py` — ProjectX Gateway REST client
- `orbbot/paper.py` — simulated broker for `--dry-run`
- `orbbot/journal.py` — CSV + Excel trade journal
- `orbbot/bot.py` — live session loop
- `orbbot/backtest.py` — conservative 1-min backtester
- `deploy/` — systemd service + timer
- `config.yaml` — every parameter; credentials stay in env vars

## Known limits
- Bracket tick signs (long: stop negative, target positive) come from community API clients,
  not ProjectX's docs. Watch the first practice-account fills.
- Paper fills are approximations: entries at last price + 1 tick, stops 1 tick worse, targets
  at the limit. Real fills can be worse at the open.
- The daily guard polls every 5 s; a fast spike can overshoot −$500 by slippage. `loss_buffer`
  holds back $25 for that, and Topstep's own limit is still the backstop.
- After a restart mid-session the bot re-reads P&L (limits hold) but forgets which side it traded.
  A paper session that restarts also loses its simulated position.
- Fees are an estimate (`fee_per_contract_rt`). Holidays are a manual list.
- `--days` backtests cover only the current contract month; use a continuous CSV for more.
