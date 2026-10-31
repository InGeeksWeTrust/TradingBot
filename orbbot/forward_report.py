"""Forward-test report for Strategy A's prospective sample. Reporting only: reads the record, decides nothing.

    python -m orbbot.forward_report --config config-sweep.yaml                  # as of today (ET)
    python -m orbbot.forward_report --config config-sweep.yaml --date 2026-12-15

Writes reports/forward_<date>.md. The sample is the paper trades with session date on or after
forward_test.sample_start and on or before --date. Operational-week trades (before sample_start),
Practice-account drills (state-drill/, `python -m orbbot.drill --report`) and research results are
never included. Opens state/paper.sqlite read-only.

Sections
  1 Sample: trades, skipped signals by reason, sessions run and weekdays without one.
  2 Results: net P&L, R per trade (net P&L / planned stop risk, as in research), mean, sd, win rate,
    profit factor.
  3 Drawdown (R and $, current and max) and the longest losing streak against the pre-registered
    thresholds.
  4 Sequential test: z of mean R against the pre-registered looks at 100 and 200 trades. Before 100
    trades the value is shown but no decision is made.
  5 Execution: market drift (signal close -> the price the bot saw when it sent the order, from the
    latency log) and modeled paper slippage, kept separate; the proposed drift monitoring flag;
    signal-to-order latency.
  6 Account safety: the rehearsed MLL floor and headroom, guard blocks, halts and flattens, and the
    daily audits' WARN/FAIL/INFO rows.
  7 Reproducibility: code versions and the config fingerprint across the sample's sessions.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import statistics
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from .audit import frozen_fingerprint
from .config import BotConfig, load_config
from .execution import AccountGuard
from .risk import spec_for
from .strategy import ET

# PRE-REGISTERED (Strategy A forward test). Changing any of these changes the test: Pete's decision only.
LOOK_100, LOOK_200 = 100, 200
Z_SUCCEED_100 = 2.538            # at 100 trades: z >= 2.538 succeed, otherwise continue to 200
Z_SUCCEED_200 = 1.662            # at 200 trades: z >= 1.662 succeed
Z_FAIL_200 = -1.645              #                z <= -1.645 fail
DD_R = {LOOK_100: (-20.0, -26.0), LOOK_200: (-26.0, -34.0)}   # drawdown thresholds in R by trade count
STREAK = (12, 15)                # losing-streak thresholds
# PROPOSED monitoring flag (Phase 4A), not a decision rule
DRIFT_FLAG_TICKS, DRIFT_FLAG_MIN_N = 11.0, 20

ROUTINE_FLATTEN = "session flatten time"


def _ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


class _ReadOnlyAccount:
    """The account table, read-only, for AccountGuard's floor calculation."""

    def __init__(self, db: sqlite3.Connection):
        self.values = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT key, value FROM account")}

    def get(self, key, default=None):
        return self.values.get(key, default)


def _et_day(iso: str) -> date:
    return datetime.fromisoformat(iso).astimezone(ET).date()


def _f(x, fmt="{:+.2f}", none="n/a") -> str:
    return none if x is None else fmt.format(x)


def _sd(xs: list[float]) -> float | None:
    return statistics.stdev(xs) if len(xs) >= 2 else None


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))]


def z_stat(rs: list[float]) -> float | None:
    """Mean R over its standard error; None with fewer than 2 trades or no spread."""
    sd = _sd(rs)
    if not sd:
        return None
    return statistics.mean(rs) / (sd / math.sqrt(len(rs)))


def drawdown(values: list[float]) -> tuple[float, float]:
    """(current, max) drawdown of the running sum of `values`, from a peak that starts at 0. Both <= 0."""
    eq = peak = worst = 0.0
    for v in values:
        eq += v
        peak = max(peak, eq)
        worst = min(worst, eq - peak)
    return eq - peak, worst


def losing_streak(values: list[float]) -> tuple[int, int]:
    """(current, longest) run of trades with net P&L <= 0."""
    cur = best = 0
    for v in values:
        cur = cur + 1 if v <= 0 else 0
        best = max(best, cur)
    return cur, best


def classify(kind: str, detail: str) -> list[str]:
    """Reason(s) a signal wasn't traded, from its event."""
    if kind == "signal_filtered":
        n = len([x for x in detail.split(" at ")[0].strip("[]").split(",") if x.strip()])
        return ["filtered by ES confirmation"] * max(n, 1)
    if kind == "signal_stale":
        return ["stale: bot started after the signal"]
    if kind == "signal_skipped":
        why = detail.rsplit(": ", 1)[-1]
        return ["daily limits (daily_headroom)" if why == "daily_headroom" else why]
    why = detail.split(": ", 1)[-1]
    if "stale" in why:
        return ["stale data"]
    if "MLL" in why:
        return ["account guard (MLL)"]
    if "halted" in why or "daily" in why:
        return ["daily limits (halted for the day)"]
    if "after entry cutoff" in why:
        return ["after entry cutoff"]
    if "position" in why or "still" in why:
        return ["trade already open"]
    return [f"blocked: {why}"]


def _audit_rows(path: Path) -> tuple[str | None, list[tuple[str, str, str]]]:
    """(overall, [(area, status, detail)] for WARN/FAIL/INFO rows) of a daily audit file, Strategy A only."""
    text = path.read_text(encoding="utf-8", errors="replace").split("\n## Practice-account drill")[0]
    m = re.search(r"\*\*Overall: (\w+)\*\*", text)
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[1] in ("WARN", "FAIL", "INFO"):
            rows.append((cells[0], cells[1], cells[2]))
    return (m.group(1) if m else None), rows


def build(cfg: BotConfig, asof: date, root: Path = Path(".")) -> str:
    start = cfg.forward_test.sample_start
    if start is None:
        raise SystemExit("forward_test.sample_start is not set in the config")
    db_path = root / cfg.safety.state_dir / "paper.sqlite"
    if not db_path.exists():
        raise SystemExit(f"{db_path} not found")
    in_sample = lambda d: start <= d <= asof  # noqa: E731
    db = _ro(db_path)
    try:
        fwd = [dict(r) for r in db.execute("SELECT * FROM forward ORDER BY signal_time")]
        events = [(r["at"], r["kind"], r["detail"] or "") for r in db.execute("SELECT at, kind, detail FROM events ORDER BY id")]
        acct = _ReadOnlyAccount(db)
        has_lat = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='latency'").fetchone() is not None
        lat = [dict(r) for r in db.execute("SELECT kind, name, poll, tag, start_at, price FROM latency "
                                           "WHERE kind IN ('signal','poll') AND day >= ? AND day <= ?",
                                           (start.isoformat(), asof.isoformat()))] if has_lat else []
    finally:
        db.close()

    rows = [r for r in fwd if r.get("day") and in_sample(date.fromisoformat(r["day"])) and (r.get("mode") or "paper") == "paper"]
    trades = [r for r in rows if (r.get("fill_qty") or 0) > 0 and r.get("exit_time") and r.get("net_pnl") is not None]
    open_rows = [r for r in rows if (r.get("fill_qty") or 0) > 0 and r not in trades]
    ev = [e for e in events if in_sample(_et_day(e[0]))]

    out = [f"# Strategy A forward test: prospective sample as of {asof}", "",
           f"Sample: paper trades with session date {start} .. {asof} (`forward_test.sample_start` {start}). "
           "Operational-week trades, Practice-account drills and research results are not included. "
           "Practice drill slippage is reported separately by `python -m orbbot.drill --report`.", ""]

    # ---------------------------------------------------------------- 1 sample
    seen, reasons = set(), Counter()
    for at, kind, detail in ev:
        if kind in ("signal_filtered", "signal_stale", "signal_skipped", "signal_blocked"):
            key = (_et_day(at), kind, detail)     # a restart can record the same refusal twice
            if key not in seen:
                seen.add(key)
                reasons.update(classify(kind, detail))
    session_days = sorted({_et_day(at) for at, kind, _ in ev if kind == "session_start"})
    weekdays = [start + timedelta(days=i) for i in range((asof - start).days + 1)]
    missed = [d for d in weekdays if d.weekday() < 5 and d not in session_days]
    out += ["## 1. Sample", "",
            f"- Trades: **{len(trades)}** closed" + (f" (+{len(open_rows)} not yet closed, not counted)" if open_rows else ""),
            f"- Sessions run: {len(session_days)}"
            + (f"; weekdays with no session start (check for exchange holidays): {', '.join(map(str, missed))}" if missed else ""),
            f"- Signals not traded: {sum(reasons.values())}"]
    out += [f"  - {k}: {v}" for k, v in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))]
    out.append("")

    # ---------------------------------------------------------------- 2 results
    nets = [float(t["net_pnl"]) for t in trades]
    rs = [float(t["net_pnl"]) / float(t["planned_risk"]) if t.get("planned_risk") else 0.0 for t in trades]
    wins = [p for p in nets if p > 0]
    losses = [p for p in nets if p <= 0]
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None
    out += ["## 2. Results", ""]
    if trades:
        out += [f"- Net P&L: **${sum(nets):+,.2f}** (fees ${sum(float(t.get('fees') or 0) for t in trades):,.2f})",
                f"- R per trade (net P&L / planned stop risk): mean **{statistics.mean(rs):+.3f}R**, "
                f"sd {_f(_sd(rs), '{:.3f}')}R, total {sum(rs):+.2f}R",
                f"- Win rate: {len(wins)}/{len(trades)} = {len(wins) / len(trades):.0%}",
                "- Profit factor: " + (f"{pf:.2f}" if pf is not None else "n/a (no losing trades)"), "",
                "| # | Day | Side | Level | Contracts | Fill | Exit | Reason | Net $ | Risk $ | R | Cum R |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        cum = 0.0
        for i, (t, r) in enumerate(zip(trades, rs), 1):
            cum += r
            out.append(f"| {i} | {t['day']} | {t['side']} | {t.get('level') or ''} | {t['fill_qty']} | {t['fill_price']} | "
                       f"{t['exit_price']} | {t['exit_reason']} | {float(t['net_pnl']):+.2f} | {float(t['planned_risk'] or 0):.2f} | "
                       f"{r:+.3f} | {cum:+.2f} |")
    else:
        out.append("- No closed trades in the sample yet.")
    out.append("")

    # ---------------------------------------------------------------- 3 drawdown and streak
    cur_r, max_r = drawdown(rs)
    cur_d, max_d = drawdown(nets)
    cur_s, best_s = losing_streak(nets)
    out += ["## 3. Drawdown and losing streak", "",
            f"- Drawdown in R: current {cur_r:+.2f}R, max {max_r:+.2f}R",
            f"- Drawdown in $: current ${cur_d:+,.2f}, max ${max_d:+,.2f}",
            f"- Losing streak (net P&L <= 0): current {cur_s}, longest {best_s}", "",
            "Pre-registered thresholds (reached = the value is at or beyond the threshold):", "",
            "| Measure | Trades counted | Value | Threshold 1 | Threshold 2 |", "| --- | --- | --- | --- | --- |"]
    for look, (t1, t2) in DD_R.items():
        _, m = drawdown(rs[:look])
        n = min(len(rs), look)
        out.append(f"| Max drawdown, first {look} trades | {n} of {look} | {m:+.2f}R | {t1:+.0f}R: "
                   f"{'**REACHED**' if m <= t1 else 'not reached'} | {t2:+.0f}R: {'**REACHED**' if m <= t2 else 'not reached'} |")
    out.append(f"| Longest losing streak | {len(nets)} | {best_s} | {STREAK[0]}: "
               f"{'**REACHED**' if best_s >= STREAK[0] else 'not reached'} | {STREAK[1]}: "
               f"{'**REACHED**' if best_s >= STREAK[1] else 'not reached'} |")
    out.append("")

    # ---------------------------------------------------------------- 4 sequential test
    n = len(rs)
    z_now = z_stat(rs)
    out += ["## 4. Sequential test (z of mean R)", "",
            f"- Current: n = {n}, z = {_f(z_now, '{:+.3f}')}"]
    if n < LOOK_100:
        out.append(f"- **NO decision is made until a checkpoint.** The first look is at {LOOK_100} trades "
                   f"({LOOK_100 - n} to go); the value above is for information only.")
    if n >= LOOK_100:
        z = z_stat(rs[:LOOK_100])
        verdict = "**SUCCEED**" if z is not None and z >= Z_SUCCEED_100 else "no decision: continue to 200 trades"
        out.append(f"- Look 1 ({LOOK_100} trades): z = {_f(z, '{:+.3f}')} vs succeed >= {Z_SUCCEED_100}: {verdict}")
    else:
        out.append(f"- Look 1 ({LOOK_100} trades): succeed if z >= {Z_SUCCEED_100}; otherwise continue")
    if n >= LOOK_200:
        z = z_stat(rs[:LOOK_200])
        verdict = ("**SUCCEED**" if z is not None and z >= Z_SUCCEED_200 else
                   "**FAIL**" if z is not None and z <= Z_FAIL_200 else "neither boundary reached")
        out.append(f"- Look 2 ({LOOK_200} trades): z = {_f(z, '{:+.3f}')} vs succeed >= {Z_SUCCEED_200}, "
                   f"fail <= {Z_FAIL_200}: {verdict}")
    else:
        out.append(f"- Look 2 ({LOOK_200} trades): succeed if z >= {Z_SUCCEED_200}, fail if z <= {Z_FAIL_200}")
    out.append("")

    # ---------------------------------------------------------------- 5 execution
    polls = {r["start_at"]: r["price"] for r in lat if r["kind"] == "poll"}
    stage = {}
    for r in lat:
        if r["kind"] == "signal" and r["tag"]:
            stage.setdefault((r["tag"], r["name"]), r)
    drift, modeled, sig_ms, rec_ms = [], [], [], []
    rows5 = []
    for i, t in enumerate(trades, 1):
        sent = stage.get((t["tag"], "place_sent"))
        px = polls.get(sent["poll"]) if sent and sent.get("poll") else None
        d = None
        if px is not None and t.get("signal_price") is not None:
            sign = 1 if t["side"] == "long" else -1
            d = round((px - float(t["signal_price"])) * sign / cfg_tick(cfg), 2)
            drift.append(d)
        m = t.get("modeled_slippage_ticks")
        if m is not None:
            modeled.append(float(m))
        ms = {}
        for a in ("bar_close", "recognized"):
            s = stage.get((t["tag"], a))
            if s and sent:
                ms[a] = (datetime.fromisoformat(sent["start_at"]) - datetime.fromisoformat(s["start_at"])).total_seconds() * 1000
        if "bar_close" in ms:
            sig_ms.append(ms["bar_close"])
        if "recognized" in ms:
            rec_ms.append(ms["recognized"])
        rows5.append(f"| {i} | {t['tag']} | {_f(d, '{:+.2f}')} | {_f(m, '{:+.2f}')} | "
                     f"{_f(t.get('market_drift_ticks'), '{:+.2f}')} | {_f(ms.get('bar_close'), '{:.0f}')} |")
    flag = ("**FLAG: mean drift above +11 ticks after 20+ trades (proposed monitoring flag; not a decision rule)**"
            if len(drift) >= DRIFT_FLAG_MIN_N and statistics.mean(drift) > DRIFT_FLAG_TICKS else
            "not raised" + (f" (needs {DRIFT_FLAG_MIN_N}+ trades with a measured drift; have {len(drift)})"
                             if len(drift) < DRIFT_FLAG_MIN_N else ""))
    out += ["## 5. Execution", "",
            "Ticks are positive = worse for the trade. Market drift and modeled slippage are separate: drift is the "
            "real market move, modeled slippage is the paper broker's assumption.", "",
            f"- Market drift, signal close -> order sent (price the bot saw in the poll that sent the order; latency log): "
            f"mean {_f(statistics.mean(drift) if drift else None)} ticks, sd {_f(_sd(drift), '{:.2f}')}, n {len(drift)}",
            f"- Modeled paper slippage: mean {_f(statistics.mean(modeled) if modeled else None)} ticks, "
            f"sd {_f(_sd(modeled), '{:.2f}')}, n {len(modeled)}",
            f"- Drift monitoring flag (mean drift > +{DRIFT_FLAG_TICKS:.0f} ticks after {DRIFT_FLAG_MIN_N}+ trades): {flag}",
            f"- Signal-to-order latency, bar close -> Order/place sent: p50 {_f(_pct(sig_ms, 50), '{:.0f}')} ms, "
            f"p95 {_f(_pct(sig_ms, 95), '{:.0f}')} ms (n {len(sig_ms)})",
            f"- Signal recognized -> Order/place sent: p50 {_f(_pct(rec_ms, 50), '{:.0f}')} ms, "
            f"p95 {_f(_pct(rec_ms, 95), '{:.0f}')} ms (n {len(rec_ms)})"]
    if not has_lat:
        out.append("- The state database has no latency table yet (the bot creates it at its first poll).")
    if trades:
        out += ["", "The forward record's `market_drift_ticks` (signal close -> paper fill, which includes the "
                "seconds until the next poll fills the order) is shown for reference.", "",
                "| # | Trade | Drift to submit | Modeled slippage | Drift to paper fill (record) | Bar close -> sent ms |",
                "| --- | --- | --- | --- | --- | --- |"] + rows5
    out.append("")

    # ---------------------------------------------------------------- 6 account safety
    g = AccountGuard(cfg.account, acct)
    eods = [(at, d) for at, kind, d in events if kind == "eod_balance"]
    last_bal = None
    if eods:
        m = re.match(r"(\S+): ([\d.]+)", eods[-1][1])
        last_bal = float(m.group(2)) if m else None
    floor = g.floor() if g.enabled else None
    head = (last_bal - floor - g.buffer()) if (last_bal is not None and floor is not None) else None
    guard_blocks = [(at, d) for at, kind, d in ev if kind == "signal_blocked" and "MLL" in d]
    halts = [(at, d) for at, kind, d in ev if kind == "halt"]
    flattens = [(at, d) for at, kind, d in ev if kind == "flatten_start"]
    hard = [(at, kind, d) for at, kind, d in ev if kind in ("flatten_failed", "session_crash", "state_recovered",
                                                            "protection_failed", "unexpected_unprotected",
                                                            "restart_unprotected", "mll_floor_approximate")]
    drill_days = _drill_days(root, start, asof)
    out += ["## 6. Account safety (rehearsed 150K Maximum Loss Limit)", ""]
    if g.enabled:
        out += [f"- MLL floor ${floor:,.2f}" + (" (APPROXIMATE: last end-of-day balance taken after the fact)"
                                                 if g.floor_approximate else ""),
                f"- Latest end-of-day balance: {eods[-1][1] if eods else 'none recorded'}",
                f"- Headroom above floor + ${g.buffer():.0f} buffer: " + (f"${head:,.2f}" if head is not None else "n/a")]
    else:
        out.append("- Account guard off (account.max_loss_limit = 0)")
    if drill_days:
        out.append(f"- End-of-day balances include real Practice-drill P&L on: {', '.join(map(str, drill_days))} "
                   "(the paper balance is the real account balance plus simulated P&L)")
    routine = sum(1 for _, d in flattens if d == ROUTINE_FLATTEN)
    out += [f"- Guard blocks (MLL): {len(guard_blocks)}" + "".join(f"\n  - {_et_day(a)}: {d}" for a, d in guard_blocks),
            f"- Halts: {len(halts)} ({sum(1 for _, d in halts if d == ROUTINE_FLATTEN)} routine at the session flatten time)"
            + "".join(f"\n  - {_et_day(a)}: {d}" for a, d in halts if d != ROUTINE_FLATTEN),
            f"- Flattens: {len(flattens)} ({routine} routine at the session flatten time)"
            + "".join(f"\n  - {_et_day(a)}: {d}" for a, d in flattens if d != ROUTINE_FLATTEN),
            f"- Safety events: {len(hard)}" + "".join(f"\n  - {_et_day(a)} {k}: {d}" for a, k, d in hard)]
    audit_lines, no_audit = [], []
    for d in session_days:
        p = root / "audit" / f"{d}.md"
        if not p.exists():
            no_audit.append(d)
            continue
        overall, flagged = _audit_rows(p)
        audit_lines.append(f"  - {d}: {overall}" + "".join(f"\n    - {s} {a}: {x}" for a, s, x in flagged))
    out.append(f"- Daily audits (Strategy A rows marked WARN/FAIL/INFO): {len(audit_lines)} of {len(session_days)} sessions audited"
               + (f"; **no audit file for {', '.join(map(str, no_audit))}**" if no_audit else ""))
    out += audit_lines
    out.append("")

    # ---------------------------------------------------------------- 7 reproducibility
    starts = [(at, re.search(r"code ([0-9a-f]+(?: \(dirty\))?|unknown)", d)) for at, kind, d in ev if kind == "session_start"]
    versions: dict[str, list[date]] = {}
    for at, m in starts:
        versions.setdefault(m.group(1) if m else "not recorded", []).append(_et_day(at))
    fp, _ = frozen_fingerprint(cfg)
    base = acct.get("frozen_fingerprint")
    out += ["## 7. Reproducibility", "",
            f"- Code versions at session start: {len(versions)}"
            + (" **(changed during the sample: review)**" if len(versions) > 1 else "")]
    out += [f"  - {v}: {min(ds)} .. {max(ds)} ({len(set(ds))} session day(s))"
            + (" **(uncommitted changes)**" if "dirty" in v else "") for v, ds in versions.items()]
    out.append(f"- Config fingerprint now {fp}; frozen baseline {base or 'not recorded'}: "
               + ("match" if base == fp else "**DIFFERENT: frozen settings changed**"))
    fp_fail = [d for d in session_days if (root / "audit" / f"{d}.md").exists()
               and any(a.startswith("6") and s == "FAIL" for a, s, _ in _audit_rows(root / "audit" / f"{d}.md")[1])]
    out.append("- Daily audits with a frozen-config FAIL: " + (", ".join(map(str, fp_fail)) if fp_fail else "none"))
    return "\n".join(out) + "\n"


def cfg_tick(cfg: BotConfig) -> float:
    """MNQ tick size (the forward record's tick units)."""
    return spec_for(cfg.symbol).tick_size


def _drill_days(root: Path, start: date, asof: date) -> list[date]:
    from .drill import STATE_DIR
    p = root / STATE_DIR / "drill.sqlite"
    if not p.exists():
        return []
    db = _ro(p)
    try:
        days = {date.fromisoformat(r[0]) for r in db.execute("SELECT DISTINCT day FROM forward WHERE fill_qty > 0")
                if r[0]}
    finally:
        db.close()
    return sorted(d for d in days if start <= d <= asof)


def main() -> None:
    ap = argparse.ArgumentParser(description="Strategy A forward-test report (prospective sample only)")
    ap.add_argument("--config", default="config-sweep.yaml")
    ap.add_argument("--date", help="report as of YYYY-MM-DD (default: today, ET)")
    a = ap.parse_args()
    cfg = load_config(a.config)
    asof = date.fromisoformat(a.date) if a.date else datetime.now(ET).date()
    text = build(cfg, asof)
    out = Path("reports") / f"forward_{asof}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"Written to {out}")


if __name__ == "__main__":
    main()
