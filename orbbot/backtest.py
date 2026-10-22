"""Backtest the exact same strategy + risk code on 1-minute bars.

    python -m orbbot.backtest --config config.yaml --days 60            # pulls bars from TopstepX
    python -m orbbot.backtest --config config.yaml --csv mnq_1m.csv     # columns: t,o,h,l,c[,v] (t ISO, UTC or offset)
    python -m orbbot.backtest --config config.yaml --days 60 --contracts 20

Fill model (deliberately conservative):
  * entry at the open of the bar after the signal bar closes, plus `--slippage` ticks
  * stop and target checked on each 1m bar; if both are touched in one bar, the stop wins
  * stops fill `--slippage` ticks worse; targets fill at the limit price
  * daily guard: if a bar's adverse extreme takes the day to -max_daily_loss, exit at that level
    (minus slippage); profit cap likewise at the bar's favorable extreme
  * flat at flatten_time on that bar's open
"""
from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import BotConfig, load_config
from .journal import build_workbook
from .risk import RANGE_FILTER, ContractSpec, plan_trade, spec_for
from .strategy import ET, Bar, ORBStrategy


@dataclass
class Trade:
    day: str
    side: str
    contracts: int
    entry_time: str
    entry: float
    exit_time: str
    exit: float
    reason: str
    pnl: float
    stop_pts: float = 0.0
    target_pts: float = 0.0
    risk_usd: float = 0.0
    entry_i: int = -1      # index of the entry bar within the day's bars (research tool)
    partial_pts: float = 0.0  # partial take-profit distance (0 = none)
    n_part: int = 0           # contracts taken off at the partial


def simulate_day(bars: list[Bar], cfg: BotConfig, spec: ContractSpec, contracts: int,
                 slippage_ticks: int = 1, strat=None, risk=None) -> list[Trade]:
    """`strat` is any entry rule with reset(day), on_bar(bar) -> Signal | None and
    register_entry(side); default is the ORB. `risk` overrides cfg.risk."""
    s, r = cfg.strategy, (risk or cfg.risk)
    tick, pv = spec.tick_size, spec.point_value
    slip = slippage_ticks * tick
    fee = r.fee_per_contract_rt if r.include_fees_in_pnl else 0.0
    strat = strat or ORBStrategy(s, tick)
    day = bars[0].t.astimezone(ET).date()
    strat.reset(day)
    flatten_at = datetime.combine(day, s.flatten_time, ET)

    trades: list[Trade] = []
    realized = 0.0
    halted = False
    pos = None  # dict(side, n, entry, stop, target, time)
    pending_plan = None

    def close(b: Bar, price: float, reason: str):
        nonlocal pos, realized
        sign = 1 if pos["side"] == "long" else -1
        pnl = (price - pos["entry"]) * sign * pos["n"] * pv - fee * pos["n"]
        realized += pnl
        total = pnl + pos.get("booked", 0.0)
        if pos.get("booked_n"):
            reason = f"partial+{reason}"
        trades.append(Trade(str(day), pos["side"], pos.get("n0", pos["n"]), pos["time"].astimezone(ET).strftime("%H:%M"),
                            pos["entry"], b.t.astimezone(ET).strftime("%H:%M"), round(price, 2), reason, round(total, 2),
                            pos["plan"].stop_points, pos["plan"].target_points, round(pos["plan"].risk_dollars, 2),
                            pos["i"], pos.get("partial_pts", 0.0), pos.get("n_part", 0)))
        pos = None

    for i, b in enumerate(bars):
        t = b.t.astimezone(ET)
        if t >= flatten_at:
            if pos:
                close(b, b.o - slip if pos["side"] == "long" else b.o + slip, "time")
            break

        # fill an entry queued from the previous bar's signal
        if pending_plan and not pos and not halted:
            p = pending_plan
            px = b.o + slip if p.side == "long" else b.o - slip
            stop = px - p.stop_points if p.side == "long" else px + p.stop_points
            tgt = px + p.target_points if p.side == "long" else px - p.target_points
            pos = {"side": p.side, "n": p.contracts, "entry": px, "stop": stop, "target": tgt, "time": b.t, "plan": p, "i": i}
            if r.partial_r > 0:
                ppts = p.stop_points * r.partial_r
                plvl = px + ppts if p.side == "long" else px - ppts
                if p.contracts >= 2:   # half off at the partial, runner to the full target
                    pos.update(partial=plvl, partial_pts=ppts, n_part=p.contracts // 2, n0=p.contracts)
                else:                  # a single contract can't be split: it all exits at the partial
                    pos.update(target=plvl, partial_pts=0.0)
        pending_plan = None

        if pos:
            long = pos["side"] == "long"
            sign = 1 if long else -1
            adverse = b.l if long else b.h
            favorable = b.h if long else b.l
            # price level at which the day's total hits each limit
            loss_lvl = pos["entry"] - sign * (r.max_daily_loss + realized - fee * pos["n"]) / (pos["n"] * pv)
            prof_lvl = pos["entry"] + sign * (r.max_daily_profit - realized + fee * pos["n"]) / (pos["n"] * pv)
            stop_hit = (adverse <= pos["stop"]) if long else (adverse >= pos["stop"])
            loss_hit = (adverse <= loss_lvl) if long else (adverse >= loss_lvl)
            tgt_hit = (favorable >= pos["target"]) if long else (favorable <= pos["target"])
            prof_hit = (favorable >= prof_lvl) if long else (favorable <= prof_lvl)
            if stop_hit or loss_hit:
                # whichever level is closer to entry is reached first
                lvl, why = (pos["stop"], "stop")
                if loss_hit and (not stop_hit or abs(loss_lvl - pos["entry"]) < abs(pos["stop"] - pos["entry"])):
                    lvl, why = loss_lvl, "daily_loss"
                close(b, lvl - sign * slip, why)
            elif pos.get("partial") is not None and (
                    (favorable >= pos["partial"]) if long else (favorable <= pos["partial"])):
                k = pos["n_part"]  # bank half, stop to breakeven, let the rest run
                booked = (pos["partial"] - pos["entry"]) * sign * k * pv - fee * k
                realized += booked
                pos.update(booked=booked, booked_n=k, n=pos["n"] - k, partial=None, stop=pos["entry"])
            elif tgt_hit or prof_hit:
                lvl, why = (pos["target"], "target")
                if prof_hit and (not tgt_hit or abs(prof_lvl - pos["entry"]) < abs(pos["target"] - pos["entry"])):
                    lvl, why = prof_lvl, "daily_profit"
                close(b, lvl, why)
            if not pos and (realized <= -r.max_daily_loss or realized >= r.max_daily_profit):
                halted = True

        sig = strat.on_bar(b)
        if sig and not pos and not halted:
            plan = plan_trade(sig, r, spec, realized, contracts)
            strat.register_entry(sig.side)
            if plan:
                pending_plan = plan
    else:
        if pos:  # data ended before flatten time
            close(bars[-1], bars[-1].c, "eod_data")
    return trades


_ALIASES = {
    "t": ["t", "time", "timestamp", "datetime", "date_time", "ts_event", "ts", "date"],
    "o": ["o", "open"], "h": ["h", "high"], "l": ["l", "low"], "c": ["c", "close", "last"],
    "v": ["v", "volume", "vol"],
}


def _parse_time(raw: str, assume_tz) -> datetime:
    raw = raw.strip()
    if raw.isdigit():  # epoch: seconds, ms or ns
        n = int(raw)
        sec = n / 1e9 if n > 1e17 else n / 1e3 if n > 1e11 else n
        return datetime.fromtimestamp(sec, timezone.utc)
    raw = raw.replace("Z", "+00:00")
    if "." in raw and "+" in raw:  # trim nanoseconds (Python handles up to microseconds)
        head, tz = raw.rsplit("+", 1)
        base, frac = head.split(".", 1)
        raw = f"{base}.{frac[:6]}+{tz}"
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
                "%Y%m%d %H%M%S", "%Y%m%d %H:%M:%S"):
        try:
            t = datetime.fromisoformat(raw) if fmt is None else datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"Unrecognised timestamp: {raw!r}")
    return t if t.tzinfo else t.replace(tzinfo=assume_tz)


def load_csv(path: str, tz: str = "UTC") -> list[Bar]:
    """Load 1-minute OHLCV bars. Accepts common vendor layouts: t/o/h/l/c/v, Time/Open/High/...,
    Databento ts_event, or separate Date and Time columns. Timestamps without an offset are taken
    to be in `tz` (e.g. America/New_York for exchange-time exports). Bar time = bar OPEN time."""
    from zoneinfo import ZoneInfo
    assume = ZoneInfo(tz)
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        lower = {h.lower().strip(): h for h in rd.fieldnames or []}
        col = {}
        for k, names in _ALIASES.items():
            col[k] = next((lower[n] for n in names if n in lower), None)
        date_col, time_col = lower.get("date"), lower.get("time")
        split_dt = date_col and time_col
        missing = [k for k in "ohlc" if not col[k]] + ([] if (col["t"] or split_dt) else ["time"])
        if missing:
            raise SystemExit(f"{path}: can't find column(s) {missing}. Columns present: {rd.fieldnames}")
        out = []
        for row in rd:
            raw_t = f"{row[date_col]} {row[time_col]}" if split_dt else row[col["t"]]
            out.append(Bar(_parse_time(raw_t, assume), float(row[col["o"]]), float(row[col["h"]]),
                           float(row[col["l"]]), float(row[col["c"]]),
                           float(row[col["v"]] or 0) if col["v"] else 0.0))
    out.sort(key=lambda b: b.t)
    if not out:
        raise SystemExit(f"{path}: no rows")
    print(f"Loaded {len(out)} bars from {path}: {out[0].t.astimezone(ET):%Y-%m-%d} to {out[-1].t.astimezone(ET):%Y-%m-%d} (ET)")
    return out


def fetch_bars(cfg: BotConfig, days: int) -> tuple[list[Bar], ContractSpec]:
    from .bot import ORBBot  # lazy: needs credentials
    bot = ORBBot(cfg, dry_run=True)
    bot.api.login()
    bot.resolve_contract()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    bars: dict[datetime, Bar] = {}
    cur = start
    while cur < end:  # chunk by 10 days to stay well under the 20k bar limit
        nxt = min(cur + timedelta(days=10), end)
        for d in bot.api.retrieve_bars(bot.spec.contract_id, cur, nxt):
            b = Bar.from_api(d)
            bars[b.t] = b
        cur = nxt
    print(f"Fetched {len(bars)} bars for {bot.spec.contract_id}. NOTE: only the current contract month;"
          " for longer tests run `python -m orbbot.history --months 12`.")
    return [bars[k] for k in sorted(bars)], bot.spec


def run(bars: list[Bar], cfg: BotConfig, spec: ContractSpec, contracts: int, slippage: int) -> list[Trade]:
    by_day: dict = defaultdict(list)
    for b in bars:
        t = b.t.astimezone(ET)
        if t.weekday() in cfg.trading_days and t.date() not in cfg.holidays:
            by_day[t.date()].append(b)
    trades = []
    for d in sorted(by_day):
        trades += simulate_day(sorted(by_day[d], key=lambda x: x.t), cfg, spec, contracts, slippage)
    return trades


def report(trades: list[Trade], n_days: int) -> str:
    if not trades:
        return "No trades."
    daily: dict = defaultdict(float)
    for t in trades:
        daily[t.day] += t.pnl
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for d in sorted(daily):
        eq += daily[d]
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    reasons: dict = defaultdict(int)
    for t in trades:
        reasons[t.reason] += 1
    pf = sum(wins) / -sum(losses) if losses and sum(losses) else float("inf")
    lines = [
        f"Sessions with data: {n_days}   days traded: {len(daily)}   trades: {len(trades)}",
        f"Net P&L: ${sum(t.pnl for t in trades):,.2f}   win rate: {len(wins)/len(trades):.0%}   profit factor: {pf:.2f}",
        f"Avg win ${statistics.mean(wins) if wins else 0:,.2f}   avg loss ${statistics.mean(losses) if losses else 0:,.2f}",
        f"Best day ${max(daily.values()):,.2f}   worst day ${min(daily.values()):,.2f}   max drawdown (daily close) ${mdd:,.2f}",
        f"Exits: " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())),
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--csv", help="1-minute OHLCV file, e.g. data/mnq_1m.csv from `python -m orbbot.history`")
    ap.add_argument("--csv-tz", default="UTC", help="time zone for CSV timestamps that have no offset "
                    "(e.g. America/New_York or America/Chicago)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--contracts", type=int)
    ap.add_argument("--symbol", help="instrument for CSV data: MNQ, MES, NQ, ES, MGC, GC (default: symbol in config)")
    ap.add_argument("--max-trades", type=int, help="override max_trades_per_day")
    ap.add_argument("--one-per-side", choices=["yes", "no"], help="override one_trade_per_side")
    ap.add_argument("--profit-cap", type=float, help="override max_daily_profit ($)")
    ap.add_argument("--loss-limit", type=float, help="override max_daily_loss ($)")
    ap.add_argument("--target-usd", type=float, help="take profit at this many $ on every trade")
    ap.add_argument("--stop-usd", type=float, help="override max_risk_per_trade ($)")
    ap.add_argument("--slippage", type=int, default=1, help="ticks of slippage on entries and stops")
    ap.add_argument("--out", default="journal/backtest.xlsx", help="Excel workbook to write")
    a = ap.parse_args()
    cfg = load_config(a.config)
    import dataclasses
    st, rk = cfg.strategy, cfg.risk
    if a.max_trades is not None:
        st = dataclasses.replace(st, max_trades_per_day=a.max_trades)
    if a.one_per_side:
        st = dataclasses.replace(st, one_trade_per_side=a.one_per_side == "yes")
    if a.profit_cap is not None:
        rk = dataclasses.replace(rk, max_daily_profit=a.profit_cap)
    if a.loss_limit is not None:
        rk = dataclasses.replace(rk, max_daily_loss=a.loss_limit)
    if a.target_usd is not None:
        rk = dataclasses.replace(rk, target_usd=a.target_usd)
    if a.stop_usd is not None:
        rk = dataclasses.replace(rk, max_risk_per_trade=a.stop_usd)
    sym = (a.symbol or cfg.symbol).upper()
    if sym in RANGE_FILTER and sym != cfg.symbol.upper():
        lo, hi = RANGE_FILTER[sym]  # config's range filter is for a different market
        st = dataclasses.replace(st, min_range_points=lo, max_range_points=hi)
    cfg = dataclasses.replace(cfg, strategy=st, risk=rk)
    print(f"Settings: {st.max_trades_per_day} trades/day, one per side: {st.one_trade_per_side}, "
          f"daily loss ${rk.max_daily_loss:,.0f}, profit cap ${rk.max_daily_profit:,.0f}, "
          f"{st.or_start:%H:%M}-{st.entry_cutoff:%H:%M} ET entries, flat {st.flatten_time:%H:%M}")
    if a.csv:
        bars, spec = load_csv(a.csv, a.csv_tz), spec_for(a.symbol or cfg.symbol)
    else:
        bars, spec = fetch_bars(cfg, a.days)
    n = a.contracts or cfg.risk.contracts
    trades = run(bars, cfg, spec, n, a.slippage)
    n_days = len({b.t.astimezone(ET).date() for b in bars if b.t.astimezone(ET).weekday() < 5})
    print(f"\nORB backtest | {n} contracts | {spec.contract_id if a.csv else cfg.symbol}\n" + report(trades, n_days))
    rows = [{"date": t.day, "mode": "backtest", "side": t.side, "contracts": t.contracts,
             "entry_time": t.entry_time, "entry": round(t.entry, 2), "exit_time": t.exit_time, "exit": t.exit,
             "reason": t.reason, "stop_pts": t.stop_pts, "target_pts": t.target_pts,
             "risk_usd": t.risk_usd, "pnl_usd": t.pnl} for t in trades]
    build_workbook(rows, a.out, cfg.risk.max_daily_loss, cfg.risk.max_daily_profit,
                   title=f"ORB backtest: {n} {cfg.symbol}, {a.slippage} tick slippage")
    print(f"Workbook written to {a.out}")


if __name__ == "__main__":
    main()
