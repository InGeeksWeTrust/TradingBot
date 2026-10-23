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
        w.writerow(["t", "o", "h", "l", "c", "v", "instrument_id"])  # instrument_id: which contract
        for ts, r in zip(df.index, df.itertuples(index=False)):
            w.writerow([ts.isoformat(), r.open, r.high, r.low, r.close, int(r.volume), int(r.instrument_id)])
    return len(df), f"{et[0]:%Y-%m-%d}", f"{et[-1]:%Y-%m-%d}"


def roll_windows(store) -> list[tuple[int, datetime, datetime]]:
    """For each contract switch in a continuous download: (new instrument id, start, end) covering
    the prior regular session and the overnight hours that the series still took from the old
    contract. Those bars of the NEW contract are what the prior-day / overnight levels need."""
    df = store.to_df()
    ids = df["instrument_id"].to_numpy()
    out = []
    for i in range(1, len(df)):
        if ids[i] != ids[i - 1]:
            switch = df.index[i].to_pydatetime()
            et = switch.astimezone(ET)
            # prior RTH session = the last weekday before the switch's evening; start at its 9:30 ET
            prior = et.date() if et.time() >= time(16, 0) else et.date() - timedelta(days=1)
            while prior.weekday() >= 5:
                prior -= timedelta(days=1)
            start = datetime.combine(prior, time(9, 25), ET)
            end = datetime.combine(et.date() + timedelta(days=1), time(9, 30), ET)  # through the next open
            out.append((int(ids[i]), start, end))
    return out


def roll_fill(cache: str, out: Path, yes: bool) -> None:
    """Download the incoming contract's bars around every roll in `cache` (priced first, asks y/N)."""
    import databento as db
    wins = roll_windows(db.DBNStore.from_file(cache))
    if not wins:
        raise SystemExit("No contract switches in that file; nothing to fill.")
    client = _client()
    reqs = [dict(dataset=DATASET, schema=SCHEMA, symbols=[str(i)], stype_in="instrument_id",
                 start=s.isoformat(), end=e.isoformat()) for i, s, e in wins]
    cost = sum(client.metadata.get_cost(**r) for r in reqs)
    for i, s, e in wins:
        print(f"  contract {i}: {s:%Y-%m-%d %H:%M} to {e:%Y-%m-%d %H:%M} ET")
    print(f"Cost: ${cost:,.2f} for {len(reqs)} windows")
    if not yes and input("Download? [y/N] ").strip().lower() not in ("y", "yes"):
        raise SystemExit("Cancelled. Nothing was charged.")
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "o", "h", "l", "c", "v", "instrument_id"])
        for r in reqs:
            df = client.timeseries.get_range(**r).to_df()
            for ts, row in zip(df.index, df.itertuples(index=False)):
                w.writerow([ts.isoformat(), row.open, row.high, row.low, row.close, int(row.volume), int(row.instrument_id)])
                n += 1
    print(f"Wrote {n} bars of incoming-contract data to {out}")
    print(f"Use it with: python -m orbbot.research ... --level-csv {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Download MNQ 1-minute bars from Databento")
    ap.add_argument("--months", type=int, default=12, help="how far back from the latest available day")
    ap.add_argument("--start", help="YYYY-MM-DD (overrides --months)")
    ap.add_argument("--end", help="YYYY-MM-DD, exclusive (default: latest available)")
    ap.add_argument("--symbol", default="MNQ.v.0", help="continuous symbol, e.g. MNQ.v.0 or MNQ.c.0")
    ap.add_argument("--out", default="data/mnq_databento.csv")
    ap.add_argument("--all-hours", action="store_true", help="keep all 23 hours in the CSV")
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    ap.add_argument("--roll-fill", metavar="FILE", help="download the incoming contract around each roll in "
                    "this .dbn.zst, so prior-day/overnight levels on roll days use the right contract")
    ap.add_argument("--from-cache", metavar="FILE", help="rebuild the CSV from a .dbn.zst you already "
                    "downloaded (no API call, no charge)")
    a = ap.parse_args()

    if a.roll_fill:
        roll_fill(a.roll_fill, Path(a.out), a.yes)
        return

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
