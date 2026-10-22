"""Download continuous MNQ 1-minute bars from Databento (CME Globex MDP 3.0) for backtesting.

Setup (once):
    pip install databento
    setx DATABENTO_API_KEY "db-..."          # from databento.com > API keys, then open a NEW terminal

Usage:
    python -m orbbot.databento_fetch --months 12               # shows the price, asks before buying
    python -m orbbot.databento_fetch --start 2024-01-01 --end 2026-10-01
    python -m orbbot.backtest --csv data/mnq_databento.csv

Symbol: MNQ.v.0 = the MNQ contract with the highest volume each day (follows the real roll).
Use --symbol MNQ.c.0 for a calendar roll instead. Prices are unadjusted, which is fine for ORB
since every trade opens and closes within one session.

The full download (all hours) is cached as a .dbn.zst file in data/, so you never pay twice for
the same range. The CSV written for the backtester keeps only 09:25-12:00 ET on weekdays.
"""
from __future__ import annotations

import argparse
import csv
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .strategy import ET

DATASET = "GLBX.MDP3"
SCHEMA = "ohlcv-1m"


def _client():
    try:
        import databento as db
    except ImportError:
        raise SystemExit("The Databento client isn't installed. Run: python -m pip install databento")
    key = os.environ.get("DATABENTO_API_KEY", "").strip().strip('"').strip("'")
    if not key:
        raise SystemExit('DATABENTO_API_KEY is not set. Run: setx DATABENTO_API_KEY "db-..." '
                         "and open a NEW terminal.")
    return db.Historical(key)


def available_end(client) -> date:
    rng = client.metadata.get_dataset_range(dataset=DATASET)
    end = rng.get("end") or rng.get("end_date")
    return datetime.fromisoformat(str(end).replace("Z", "+00:00")).date()


def write_session_csv(store, out: Path, keep_all: bool) -> tuple[int, str, str]:
    df = store.to_df()  # index = ts_event (UTC, bar open), float prices
    if df.empty:
        raise SystemExit("Databento returned no bars for that range.")
    et = df.index.tz_convert(ET)
    if not keep_all:
        tod = et.hour * 60 + et.minute
        mask = (et.dayofweek < 5) & (tod >= 9 * 60 + 25) & (tod < 12 * 60)
        df, et = df[mask], et[mask]
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "o", "h", "l", "c", "v"])
        for ts, r in zip(df.index, df.itertuples(index=False)):
            w.writerow([ts.isoformat(), r.open, r.high, r.low, r.close, int(r.volume)])
    return len(df), f"{et[0]:%Y-%m-%d}", f"{et[-1]:%Y-%m-%d}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Download MNQ 1-minute bars from Databento")
    ap.add_argument("--months", type=int, default=12, help="how far back from the latest available day")
    ap.add_argument("--start", help="YYYY-MM-DD (overrides --months)")
    ap.add_argument("--end", help="YYYY-MM-DD, exclusive (default: latest available)")
    ap.add_argument("--symbol", default="MNQ.v.0", help="continuous symbol, e.g. MNQ.v.0 or MNQ.c.0")
    ap.add_argument("--out", default="data/mnq_databento.csv")
    ap.add_argument("--all-hours", action="store_true", help="keep all 23 hours in the CSV")
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    ap.add_argument("--from-cache", metavar="FILE", help="rebuild the CSV from a .dbn.zst you already "
                    "downloaded (no API call, no charge)")
    a = ap.parse_args()

    if a.from_cache:
        try:
            import databento as db
        except ImportError:
            raise SystemExit("The Databento client isn't installed. Run: python -m pip install databento")
        print(f"Rebuilding from {a.from_cache} (no charge).")
        n, first, last = write_session_csv(db.DBNStore.from_file(a.from_cache), Path(a.out), a.all_hours)
        print(f"Wrote {n} bars ({first} to {last}) to {a.out}")
        return

    client = _client()
    latest = available_end(client)
    end = min(date.fromisoformat(a.end), latest) if a.end else latest
    start = date.fromisoformat(a.start) if a.start else end - timedelta(days=int(a.months * 30.5))
    if start >= end:
        raise SystemExit(f"Start {start} is not before end {end}.")

    cache = Path(a.out).with_name(f"{a.symbol.replace('.', '_')}_{start}_{end}.dbn.zst")
    req = dict(dataset=DATASET, schema=SCHEMA, symbols=[a.symbol], stype_in="continuous",
               start=start.isoformat(), end=end.isoformat())

    if cache.exists():
        import databento as db
        print(f"Using cached download {cache} (no charge).")
        store = db.DBNStore.from_file(cache)
    else:
        cost = client.metadata.get_cost(**req)
        print(f"Databento {DATASET} {SCHEMA} {a.symbol}, {start} to {end}")
        print(f"Cost: ${cost:,.2f} (charged to your Databento account; new accounts usually have free credit)")
        if not a.yes and input("Download? [y/N] ").strip().lower() not in ("y", "yes"):
            raise SystemExit("Cancelled. Nothing was charged.")
        print("Downloading...")
        store = client.timeseries.get_range(**req, path=cache)
        print(f"Saved raw data to {cache}")

    n, first, last = write_session_csv(store, Path(a.out), a.all_hours)
    print(f"Wrote {n} bars ({first} to {last}) to {a.out}")
    print(f"Next: python -m orbbot.backtest --csv {a.out}")


if __name__ == "__main__":
    main()
