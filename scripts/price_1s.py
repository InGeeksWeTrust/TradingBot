"""Databento PRICE CHECK ONLY: quotes what 1-second bars would cost. Downloads nothing.

    python scripts/price_1s.py

Uses metadata.get_cost (free) on GLBX.MDP3, schema ohlcv-1s, continuous front month (stype_in
"continuous"), 09:30-12:00 ET each day (DST-aware), for:
  (a) MNQ.v.0 on the signal days in docs/phase4/phase4a_signal_days.csv
  (b) MNQ.v.0 on every weekday 2024-10-21 .. 2026-10-20 (exchange holidays have no data and cost ~$0)
  (c) ES.v.0 on the same days as (b)
The API key comes from DATABENTO_API_KEY and is never printed.
"""
from __future__ import annotations

import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
SIGNAL_DAYS = ROOT / "docs" / "phase4" / "phase4a_signal_days.csv"
DATASET, SCHEMA, STYPE = "GLBX.MDP3", "ohlcv-1s", "continuous"
WINDOW = (time(9, 30), time(12, 0))


def window(d: date) -> tuple[str, str]:
    """09:30-12:00 ET on day d, as UTC ISO strings."""
    return tuple(datetime.combine(d, t, ET).astimezone(timezone.utc).isoformat() for t in WINDOW)


def weekdays(first: date, last: date) -> list[date]:
    n = (last - first).days + 1
    return [first + timedelta(days=i) for i in range(n) if (first + timedelta(days=i)).weekday() < 5]


def signal_days(path: Path = SIGNAL_DAYS) -> list[date]:
    with path.open() as f:
        return sorted({date.fromisoformat(r["day"]) for r in csv.DictReader(f)})


def cost(client, symbol: str, days: list[date], workers: int = 8) -> float:
    def one(d):
        start, end = window(d)
        return client.metadata.get_cost(dataset=DATASET, symbols=[symbol], stype_in=STYPE, schema=SCHEMA,
                                        start=start, end=end)
    with ThreadPoolExecutor(workers) as ex:
        return float(sum(ex.map(one, days)))


def main() -> None:
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        sys.exit("Set DATABENTO_API_KEY first.")
    import databento as db
    client = db.Historical(key)
    sig = signal_days()
    every = weekdays(date(2024, 10, 21), date(2026, 10, 20))
    options = [("(a) MNQ.v.0, signal days", "MNQ.v.0", sig), ("(b) MNQ.v.0, every weekday", "MNQ.v.0", every),
               ("(c) ES.v.0, every weekday", "ES.v.0", every)]
    print(f"Databento {DATASET} {SCHEMA}, stype_in={STYPE}, 09:30-12:00 ET per day. PRICE CHECK ONLY.")
    total = {}
    for label, sym, days in options:
        total[label] = c = cost(client, sym, days)
        print(f"  {label:<30} {len(days):>4} days   ${c:,.2f}")
    print(f"  {'(b) + (c)':<30} {'':>4}        ${total[options[1][0]] + total[options[2][0]]:,.2f}")
    print("Nothing was downloaded.")


if __name__ == "__main__":
    main()
