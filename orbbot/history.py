"""Build a continuous MNQ 1-minute history CSV from TopstepX by stitching quarterly contracts.

    python -m orbbot.history --months 12                 # writes data/mnq_1m.csv
    python -m orbbot.backtest --csv data/mnq_1m.csv

MNQ contracts expire the 3rd Friday of Mar (H), Jun (M), Sep (U) and Dec (Z). Each contract
is used only while it was the front month: from the previous contract's roll date up to its
own roll date (the Thursday 8 days before expiry, when volume normally moves to the next
contract). Prices are NOT back-adjusted, which is fine for ORB because every trade opens and
closes within one session.

Whether TopstepX still serves expired contracts depends on their data retention. The script
reports what it actually got for each contract.
"""
from __future__ import annotations

import argparse
import csv
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path

from .api import ApiError, ProjectXClient
from .config import load_config
from .strategy import ET

MONTHS = {3: "H", 6: "M", 9: "U", 12: "Z"}


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def roll_date(year: int, month: int) -> date:
    return third_friday(year, month) - timedelta(days=8)


def contract_schedule(symbol: str, start: date, end: date) -> list[tuple[str, date, date]]:
    """[(contract_id, front_from, front_to)] covering start..end, oldest first."""
    out = []
    y, prev_roll = start.year - 1, None
    for year in range(start.year - 1, end.year + 2):
        for m in (3, 6, 9, 12):
            r = roll_date(year, m)
            if prev_roll is not None and r > start and prev_roll < end:
                cid = f"CON.F.US.{symbol}.{MONTHS[m]}{year % 100:02d}"
                out.append((cid, max(prev_roll, start), min(r, end)))
            prev_roll = r
    return out


def fetch(api: ProjectXClient, cid: str, frm: date, to: date) -> list[dict]:
    """One small request per weekday covering 09:25-12:00 ET, the only window the ORB backtest
    uses. Gives up on a contract after 5 empty weekdays in a row at the start (not retained)."""
    out: list[dict] = []
    d, empty_streak, got_any = frm, 0, False
    while d < to:
        if d.weekday() < 5:
            start = datetime.combine(d, dtime(9, 25), ET)
            end = datetime.combine(d, dtime(12, 0), ET)
            try:
                bars = api.retrieve_bars(cid, start, end, limit=1000)
            except ApiError as e:
                print(f"\n    {cid} {d}: {e}", end="")
                bars = []
            time.sleep(0.65)  # stay under 50 requests / 30 s
            if bars:
                got_any, empty_streak = True, 0
                out += sorted(bars, key=lambda b: b["t"])
                print(".", end="", flush=True)
            else:
                empty_streak += 1
                print("_", end="", flush=True)
                if not got_any and empty_streak >= 5:
                    print(" (no data, skipping)", end="")
                    break
        d += timedelta(days=1)
    print()
    return out


def write_csv(rows: list[dict], path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "o", "h", "l", "c", "v"])
        for b in rows:
            w.writerow([b["t"], b["o"], b["h"], b["l"], b["c"], b.get("v", 0)])


def main() -> None:
    ap = argparse.ArgumentParser(description="Download stitched MNQ 1m history from TopstepX")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--out", default="data/mnq_1m.csv")
    a = ap.parse_args()
    cfg = load_config(a.config)
    api = ProjectXClient(cfg.username, cfg.api_key, cfg.base_url)
    api.login()

    end = datetime.now(ET).date() + timedelta(days=1)
    start = end - timedelta(days=int(a.months * 30.5))
    rows: list[dict] = []
    print(f"Fetching {cfg.symbol} 1-minute bars, 09:25-12:00 ET each weekday, {start} to {end}")
    print("('.' = day downloaded, '_' = no data that day, e.g. a holiday)")
    try:
        for cid, frm, to in contract_schedule(cfg.symbol, start, end):
            print(f"  {cid}  {frm} -> {to}: ", end="", flush=True)
            bars = fetch(api, cid, frm, to)
            days = len({b["t"][:10] for b in bars})
            print(f"    {len(bars)} bars over {days} days")
            rows += bars
    except KeyboardInterrupt:
        print("\nInterrupted: saving what was downloaded so far.")

    if not rows:
        raise SystemExit("No bars returned at all. Use a CSV from a data vendor instead (see README).")
    rows.sort(key=lambda b: b["t"])
    write_csv(rows, a.out)
    first, last = rows[0]["t"][:10], rows[-1]["t"][:10]
    print(f"\nWrote {len(rows)} bars ({first} to {last}) to {a.out}")
    print(f"Next: python -m orbbot.backtest --csv {a.out}")


if __name__ == "__main__":
    main()
