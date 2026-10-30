"""Daily operational audit for the forward test.

    python -m orbbot.audit --config config-sweep.yaml              # today's session
    python -m orbbot.audit --config config-sweep.yaml --date 2026-10-23

Reads the bot's state database, the paper broker's account file, the journal and the day's log,
checks that they agree, and writes audit/<date>.md with an overall PASS / WARN / FAIL.

Checks
  1 Cross-store consistency: every broker entry order has an intent with its tag and vice versa;
    nothing is open or unfinished after the session; each closed trade has exactly one journal row
    and a complete forward-test row.
  2 Level consumption: every level the strategy reacted to (traded, filtered by the confirmation,
    blocked, skipped, stale) is recorded as used; no level is traded twice; trades <= the daily cap.
  3 Lifecycle: each intent's state changes follow the allowed transitions; filled trades were
    protected (or flattened) and have fill, exit, fees and P&L recorded. A paper trade whose entry
    minute's high (short) or low (long) reached its stop is listed as INFO "paper-uncertain".
  4 Account guard: the Maximum Loss Limit floor, whether it rests on an approximate end-of-day
    balance, and the room above the buffer.
  5 Operational events: CRITICAL log lines, failed flattens, crashes, protection failures,
    unexpected positions, state recovery, stale-data blocks. The scheduled "session flatten time"
    flatten is routine; any other flatten is to review. Session starts show the pid and code
    version; a second start is a WARN restart, or a FAIL if the earlier process was still alive
    (its once-a-minute heartbeat is later than the second start). A start refused by the
    single-instance lock is a WARN, not a CRITICAL.
  6 Frozen configuration: a fingerprint of every strategy/risk/confirmation/forward-test setting is
    stored on the first audit; any later difference is a FAIL.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from dataclasses import asdict
from datetime import date, datetime, time
from pathlib import Path

from .config import BotConfig, load_config
from .execution import ALLOWED, S, AccountGuard
from .state import StateStore
from .strategy import ET

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
INFO = "INFO"                                 # listed for review; does not change the overall result
ROUTINE_FLATTEN = "session flatten time"     # the reason bot.py gives the scheduled flatten
DUPLICATE_START = "duplicate start rejected"  # bot.py's log line when the instance lock is held


def frozen_fingerprint(cfg: BotConfig) -> tuple[str, dict]:
    keep = {"symbol": cfg.symbol, "entry": cfg.entry, "confirm_symbol": cfg.confirm_symbol,
            "sweep_extra": cfg.sweep_extra, "history_days": cfg.history_days,
            "strategy": asdict(cfg.strategy), "risk": asdict(cfg.risk),
            "account": asdict(cfg.account), "forward_test": asdict(cfg.forward_test),
            "required_confirm_symbol": cfg.safety.required_confirm_symbol}
    blob = json.dumps(keep, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16], json.loads(blob)


def _et_date(iso: str) -> date:
    return datetime.fromisoformat(iso).astimezone(ET).date()


def _et(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(ET).strftime("%H:%M:%S ET")


def _log_lines(log_dir: Path, day: date):
    """Lines stamped `day` from every bot log. A log file is named after the day its bot started,
    so a bot left running overnight writes the session into the previous day's file."""
    for f in sorted(log_dir.glob("orbbot-*.log")):
        if date.fromtimestamp(f.stat().st_mtime) < day:
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(day.isoformat()):
                yield line


def audit(cfg: BotConfig, day: date, mode: str = "paper", root: Path = Path(".")) -> tuple[str, str]:
    state_dir = root / cfg.safety.state_dir
    store = StateStore(state_dir / f"{mode}.sqlite")
    checks: list[tuple[str, str, str]] = []   # (area, status, detail)

    def add(area, status, detail):
        checks.append((area, status, detail))

    intents = store.intents_for_day(day)
    events = [e for e in store.events() if _et_date(e[0]) == day]
    fwd = {r["tag"]: r for r in store.forward_rows() if r.get("day") == day.isoformat()}
    used = store.used_levels(day)

    # ---------------- broker side (paper account file; live accounts are checked with --read-only)
    broker = None
    pb = state_dir / "paper_broker.json"
    if mode == "paper" and pb.exists():
        broker = json.loads(pb.read_text())
    journal_rows = []
    jp = root / cfg.journal_dir / f"{mode}.csv"
    if jp.exists():
        with jp.open() as f:
            journal_rows = [r for r in csv.DictReader(f) if r["date"] == day.isoformat()]

    # 1. cross-store consistency
    tags = {i["tag"] for i in intents}
    filled = [i for i in intents if (i.get("filled_qty") or 0) > 0]
    if broker is not None:
        entries = [o for o in broker["history"] if o.get("type") == 2 and o.get("customTag")
                   and _et_date(o["creationTimestamp"]) == day]
        btags = {o["customTag"] for o in entries}
        orphan = btags - tags
        add("1 cross-store", FAIL if orphan else PASS,
            f"broker entry orders without an intent: {sorted(orphan)}" if orphan else
            f"{len(entries)} broker entry order(s), all with a matching intent")
        missing = {i["tag"] for i in filled} - btags
        add("1 cross-store", FAIL if missing else PASS,
            f"filled intents with no broker order: {sorted(missing)}" if missing else
            "every filled intent has its broker order")
        if broker.get("pos"):
            add("1 cross-store", FAIL, f"paper position still open: {broker['pos']['size']} lots")
        open_ids = broker.get("open_ids") or []
        if open_ids:
            add("1 cross-store", WARN, f"{len(open_ids)} working order(s) left on the paper account")
    elif mode == "paper":       # the paper broker writes its file on the first simulated order
        add("1 cross-store", FAIL if intents else PASS,
            f"no paper account file but {len(intents)} entry intent(s) today" if intents else
            "no paper broker activity today")
    else:
        add("1 cross-store", WARN, "no paper account file to compare (live: run --read-only)")
    unfinished = [i for i in intents if S(i["state"]) not in (S.CLOSED, S.CANCELLED, S.REJECTED)]
    add("1 cross-store", FAIL if unfinished else PASS,
        f"unfinished intents: {[(i['tag'], i['state']) for i in unfinished]}" if unfinished else
        "no unfinished intents")
    closed = [i for i in filled if i["state"] == S.CLOSED.value]
    no_exit = [i["tag"] for i in closed if not (fwd.get(i["tag"]) or {}).get("exit_time")]
    add("1 cross-store", FAIL if no_exit else PASS,
        f"closed trades without a recorded exit: {no_exit}" if no_exit else
        f"{len(closed)} closed trade(s), each with an exit record")
    if len(journal_rows) != len(closed):
        add("1 cross-store", FAIL, f"journal rows {len(journal_rows)} != closed trades {len(closed)}")
    else:
        add("1 cross-store", PASS, f"journal rows match closed trades ({len(closed)})")

    # 2. level consumption
    reacted = set()
    for at, kind, detail in events:
        if kind in ("signal_filtered",):
            reacted |= {x.strip(" '[]") for x in detail.split(" at ")[0].split(",")}
        elif kind in ("signal_blocked", "signal_skipped", "signal_stale"):
            parts = detail.split()
            if len(parts) > 1 and parts[1] not in ("None",):
                reacted.add(parts[1])
    levels_traded = [((i.get("plan") or {}).get("level")) for i in intents if i["state"] != S.CANCELLED.value or i.get("filled_qty")]
    levels_traded = [x for x in levels_traded if x]
    not_marked = (reacted | set(levels_traded)) - used
    add("2 levels", FAIL if not_marked else PASS,
        f"levels reacted to but not recorded as used: {sorted(not_marked)}" if not_marked else
        f"levels used today: {sorted(used) or 'none'} (traded {levels_traded or 'none'}; "
        f"filtered/blocked/skipped {sorted(reacted - set(levels_traded)) or 'none'})")
    dup = {x for x in levels_traded if levels_traded.count(x) > 1}
    add("2 levels", FAIL if dup else PASS, f"level traded twice: {sorted(dup)}" if dup else "no level traded twice")
    sent = [i for i in intents if i["state"] not in (S.CREATED.value,)]
    add("2 levels", FAIL if len(sent) > cfg.strategy.max_trades_per_day else PASS,
        f"{len(sent)} entry order(s) vs daily cap {cfg.strategy.max_trades_per_day}")

    # 3. lifecycle
    for i in intents:
        tr = store.transitions(i["tag"])
        bad = []
        prev = None
        for frm, to, _ in tr:
            if prev is None:
                if to != "CREATED":
                    bad.append(f"starts at {to}")
            elif S(to) not in ALLOWED.get(S(prev), set()) and not (prev == to):
                bad.append(f"{prev}->{to}")
            prev = to
        seq = " > ".join(t[1] for t in tr)
        add("3 lifecycle", FAIL if bad else PASS, f"{i['tag']}: {seq}" + (f"  ILLEGAL {bad}" if bad else ""))
        if (i.get("filled_qty") or 0) > 0:
            states = [t[1] for t in tr]
            r = fwd.get(i["tag"]) or {}
            need = ["fill_price", "fill_qty", "exit_price", "exit_reason", "fees", "net_pnl"]
            gaps = [k for k in need if r.get(k) in (None, "")]
            if "PROTECTED" not in states:
                add("3 lifecycle", WARN, f"{i['tag']}: never verified PROTECTED (flattened instead)")
            add("3 lifecycle", FAIL if gaps else PASS,
                f"{i['tag']}: forward record missing {gaps}" if gaps else
                f"{i['tag']}: {r['fill_qty']} @ {r['fill_price']} -> {r['exit_price']} ({r['exit_reason']}), "
                f"net ${r['net_pnl']}, market drift {r.get('market_drift_ticks')} ticks")
            # the paper broker ignores its fill minute's range (it can't know what came after the fill)
            hi, lo, stop = r.get("entry_bar_high"), r.get("entry_bar_low"), r.get("stop_price")
            if mode == "paper" and None not in (hi, lo, stop):
                short = i["side"] == "short"
                if (hi >= stop) if short else (lo <= stop):
                    add("3 lifecycle", INFO,
                        f"{i['tag']}: paper-uncertain: the entry minute's {'high' if short else 'low'} "
                        f"{hi if short else lo} reached the stop {stop}; the paper broker can't tell whether "
                        f"that happened after the fill")

    # 4. account guard
    g = AccountGuard(cfg.account, store)
    if g.enabled:
        bal = None
        if broker is not None:
            pass
        floor = g.floor()
        approx = g.floor_approximate
        add("4 account", WARN if approx else PASS,
            f"MLL floor {floor} ({'APPROXIMATE: rests on an after-the-fact end-of-day balance; buffer widened to $' + format(g.buffer(), '.0f') + '. Confirm against the dashboard or set account.mll_floor' if approx else 'from recorded end-of-day balances'}); "
            f"last EOD day {store.get('last_eod_day')}, peak EOD {store.get('peak_eod_balance')}")
        eods = [e for e in store.events("eod_balance") if e[2].startswith(day.isoformat())]
        add("4 account", PASS if eods else WARN,
            f"end-of-day balance recorded for {day}: {eods[-1][2]}" if eods else
            f"no end-of-day balance recorded for {day} yet (bot must be running 3:10-5:00 PM CT)")
    else:
        add("4 account", PASS, "account guard off (account.max_loss_limit = 0)")

    # 5. operational events
    kinds = {}
    for _, kind, _d in events:
        kinds[kind] = kinds.get(kind, 0) + 1
    hard = {k: kinds[k] for k in ("flatten_failed", "session_crash", "state_recovered") if k in kinds}
    soft = {k: kinds[k] for k in ("protection_failed", "unexpected_unprotected",
                                  "unexpected_protected", "reconcile_absent", "reconcile_position_without_order",
                                  "restart_unprotected", "mll_floor_approximate") if k in kinds}
    # the scheduled end-of-session flatten is routine; any other flatten reason is reviewed
    flattens = [e[2] for e in events if e[1] == "flatten_start"]
    routine = sum(1 for why in flattens if why == ROUTINE_FLATTEN)
    if len(flattens) > routine:
        soft["flatten_start"] = len(flattens) - routine
    stale = sum(1 for e in events if e[1] == "signal_blocked" and "stale" in e[2])
    crit = errs = 0
    rejected = []                           # pids of starts refused by the single-instance lock
    for line in _log_lines(root / cfg.log_dir, day):
        if DUPLICATE_START in line:
            m = re.search(r"\(pid (\d+)\)", line)
            rejected.append(m.group(1) if m else "?")
            continue
        crit += " CRITICAL " in line
        errs += " ERROR " in line or "Traceback" in line
    add("5 events", FAIL if (hard or crit) else PASS,
        f"hard events {hard or 'none'}; CRITICAL log lines {crit}; ERROR lines {errs}")
    for pid in rejected:
        add("5 events", WARN, f"duplicate start rejected (pid {pid})")
    restarts = sum(1 for e in events if e[1] == "reconciled")
    add("5 events", WARN if soft else PASS,
        f"events to review {soft or 'none'}; start-up reconciliations {restarts}; stale-data blocks {stale}"
        + (f"; info: routine '{ROUTINE_FLATTEN}' flatten x{routine}" if routine else ""))
    starts = [(at, re.search(r"pid (\d+)", d), re.search(r"code ([0-9a-f]+(?: \(dirty\))?|unknown)", d))
              for at, kind, d in sorted(events, key=lambda e: datetime.fromisoformat(e[0])) if kind == "session_start"]
    ran = ", ".join(f"pid {p.group(1) if p else '?'} code {c.group(1) if c else '?'}" for _, p, c in starts)
    add("5 events", PASS, f"{len(starts)} session start(s) today" + (f": {ran}" if starts else ""))
    for n, (at, pid, _) in enumerate(starts[1:], 1):
        t = _et(at)
        if pid is None or any(p is None for _, p, _ in starts[:n]):
            add("5 events", WARN, f"restart during session at {t} (no pid recorded: cannot check for overlap)")
            continue
        overlap = []
        for _, p, _ in starts[:n]:
            alive = store.get(f"alive:{day}:{p.group(1)}")
            if p.group(1) != pid.group(1) and alive and datetime.fromisoformat(alive) >= datetime.fromisoformat(at):
                overlap.append(f"pid {p.group(1)} still running at {_et(alive)}")
        add("5 events", FAIL if overlap else WARN,
            f"two session starts overlapped: pid {pid.group(1)} started at {t} while {'; '.join(overlap)}"
            if overlap else f"restart during session: pid {pid.group(1)} started at {t} (review)")

    # 6. frozen configuration
    fp, keep = frozen_fingerprint(cfg)
    first = store.get("frozen_fingerprint")
    if first is None:
        store.put("frozen_fingerprint", fp)
        store.put("frozen_settings", keep)
        add("6 frozen config", PASS, f"fingerprint {fp} recorded as the frozen baseline")
    else:
        add("6 frozen config", PASS if first == fp else FAIL,
            f"fingerprint {fp} matches the frozen baseline" if first == fp else
            f"CONFIG CHANGED: {fp} != frozen {first}")

    # sample status
    ft = cfg.forward_test
    if ft.sample_start and day >= ft.sample_start:
        sample = f"IN the prospective statistical sample (started {ft.sample_start})"
    else:
        sample = f"operational validation, NOT in the statistical sample (sample starts {ft.sample_start or 'not set'})"

    overall = FAIL if any(c[1] == FAIL for c in checks) else WARN if any(c[1] == WARN for c in checks) else PASS
    lines = [f"# Operational audit {day} ({mode})", "", f"**Overall: {overall}** - {sample}", "",
             f"Signals/trades today: {len(intents)} entry intent(s); levels used {sorted(used) or 'none'}", "",
             "| Area | Status | Detail |", "| --- | --- | --- |"]
    lines += [f"| {a} | {s} | {d.replace('|', '/')} |" for a, s, d in checks]
    text = "\n".join(lines) + "\n"
    out = root / "audit" / f"{day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    store.event("daily_audit", f"{day}: {overall}")
    store.close()
    return overall, text


def main() -> None:
    ap = argparse.ArgumentParser(description="Daily operational audit")
    ap.add_argument("--config", default="config-sweep.yaml")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today, ET)")
    ap.add_argument("--mode", default="paper", choices=["paper", "live"])
    a = ap.parse_args()
    cfg = load_config(a.config)
    day = date.fromisoformat(a.date) if a.date else datetime.now(ET).date()
    overall, text = audit(cfg, day, a.mode)
    print(text)
    print(f"Written to audit/{day}.md")
    raise SystemExit(0 if overall != FAIL else 1)


if __name__ == "__main__":
    main()
