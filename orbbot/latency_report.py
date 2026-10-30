"""Latency summary from the state database's `latency` table (see orbbot/latency.py).

    python -m orbbot.latency_report --config config-sweep.yaml
    python -m orbbot.latency_report --config config-sweep.yaml --since 2026-11-02 --until 2026-11-06

Prints p50 / p95 / p99 / max for each API call, the poll, how long after each minute starts the
first poll receives that minute's bar, and each signal stage. Opens the database read-only.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .config import load_config
from .latency import STAGES


def pct(xs: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in 0..100)."""
    s = sorted(xs)
    k = (len(s) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _line(label: str, xs: list[float], unit: str, extra: str = "") -> str:
    if not xs:
        return f"  {label:<34} {'n=0':>6}"
    f = (lambda v: f"{v:9.1f}") if unit == "ms" else (lambda v: f"{v:9.3f}")
    return (f"  {label:<34} {'n=' + str(len(xs)):>6} {f(pct(xs, 50))} {f(pct(xs, 95))} {f(pct(xs, 99))} "
            f"{f(max(xs))}{extra}")


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def report(db_path: str | Path, since: str | None = None, until: str | None = None) -> str:
    p = Path(db_path)
    if not p.exists():
        return f"No state database at {p}"
    db = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='latency'").fetchone():
            return "No latency data yet (the table appears after the first session with instrumentation)."
        q = "SELECT kind, name, poll, tag, sig, start_at, ms, ok, bar_t FROM latency WHERE 1=1"
        args: list = []
        if since:
            q, args = q + " AND day >= ?", args + [since]
        if until:
            q, args = q + " AND day <= ?", args + [until]
        rows = db.execute(q + " ORDER BY id", args).fetchall()
    finally:
        db.close()
    if not rows:
        return "No latency rows in that range."
    days = sorted({r[5][:10] for r in rows})
    hdr = f"  {'':<34} {'':>6} {'p50':>9} {'p95':>9} {'p99':>9} {'max':>9}"
    out = [f"Latency report ({days[0]} .. {days[-1]} UTC dates, {len(rows)} rows)", ""]

    # API calls and polls (milliseconds)
    calls: dict[str, list[float]] = defaultdict(list)
    fails: dict[str, int] = defaultdict(int)
    polls: list[tuple[datetime, str | None]] = []
    for kind, name, _poll, _tag, _sig, start, ms, ok, bar_t in rows:
        if kind == "api":
            calls[name].append(ms or 0.0)
            fails[name] += ok == 0
        elif kind == "poll":
            calls["(whole poll)"].append(ms or 0.0)
            polls.append((_t(start), bar_t))
    out += ["API calls and polls (ms)", hdr]
    for name in sorted(calls, key=lambda n: (n == "(whole poll)", n)):
        out.append(_line(name, calls[name], "ms", f"  failed {fails[name]}" if fails[name] else ""))

    # first poll that received each new minute's bar, measured from the minute's start (seconds)
    seen: dict[datetime, float] = {}
    for at, bar_t in polls:
        if bar_t:
            m = _t(bar_t).replace(second=0, microsecond=0)
            if at >= m and at - m < timedelta(minutes=1):
                seen.setdefault(m, (at - m).total_seconds())
    out += ["", "Data (s)", hdr, _line("new minute's bar first received", list(seen.values()), "s")]

    # signal stages (seconds between consecutive stages present)
    sigs: dict[str, dict[str, datetime]] = defaultdict(dict)
    for kind, name, _poll, tag, sig, start, *_ in rows:
        if kind == "signal":
            sigs[tag or sig][name] = _t(start)
    gaps: dict[str, list[float]] = defaultdict(list)
    for st in sigs.values():
        have = [s for s in STAGES if s in st]
        for a, b in zip(have, have[1:]):
            gaps[f"{a} -> {b}"].append((st[b] - st[a]).total_seconds())
        if "bar_close" in st and "protected" in st:
            gaps["bar_close -> protected (total)"].append((st["protected"] - st["bar_close"]).total_seconds())
    out += ["", f"Signal stages (s), {len(sigs)} signal(s)", hdr]
    order = [f"{a} -> {b}" for a, b in zip(STAGES, STAGES[1:])]
    for k in sorted(gaps, key=lambda k: order.index(k) if k in order else len(order)):
        out.append(_line(k, gaps[k], "s"))
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Latency percentiles from the state database")
    ap.add_argument("--config", default="config-sweep.yaml")
    ap.add_argument("--mode", default="paper", choices=["paper", "live"])
    ap.add_argument("--since", help="first day, YYYY-MM-DD")
    ap.add_argument("--until", help="last day, YYYY-MM-DD")
    a = ap.parse_args()
    cfg = load_config(a.config)
    print(report(Path(cfg.safety.state_dir) / f"{a.mode}.sqlite", a.since, a.until), end="")


if __name__ == "__main__":
    main()
