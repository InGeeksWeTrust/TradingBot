"""Download MNQ 1-second bars, 09:30-12:00 ET, every weekday 2024-10-21 .. 2026-10-20 (Phase 4A option b).

    python scripts/download_mnq_1s.py            # re-quotes, then asks y/N before buying
    python scripts/download_mnq_1s.py --quote    # quote only
    python scripts/download_mnq_1s.py --yes      # buy without the prompt (after a confirmed quote)

GLBX.MDP3, MNQ.v.0 (stype_in continuous), ohlcv-1s. Aborts if the quote is above $20. Each day is one
timeseries.get_range request; the records are written, in time order, to ONE DBN file with merged
metadata (symbol mappings joined across days): data/mnq_1s_0930-1200_2024-10-21_2026-10-20.dbn.zst.
The API key comes from DATABENTO_API_KEY and is never printed.
"""
from __future__ import annotations

import argparse
import os
import sys
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from price_1s import DATASET, ET, ROOT, SCHEMA, STYPE, cost, weekdays, window  # noqa: E402

SYMBOL = "MNQ.v.0"
FIRST, LAST = date(2024, 10, 21), date(2026, 10, 20)
MAX_COST = 20.0
OUT = ROOT / "data" / "mnq_1s_0930-1200_2024-10-21_2026-10-20.dbn.zst"


def _ns(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp()) * 1_000_000_000


def merge_mappings(stores) -> dict:
    """Join each day's {raw_symbol: [{start_date, end_date, symbol}]} into contiguous intervals."""
    out: dict[str, list[dict]] = {}
    for s in stores:
        for raw, ivs in (s.metadata.mappings or {}).items():
            cur = out.setdefault(raw, [])
            for iv in sorted(ivs, key=lambda x: x["start_date"]):
                if cur and cur[-1]["symbol"] == iv["symbol"] and iv["start_date"] >= cur[-1]["start_date"]:
                    cur[-1]["end_date"] = max(cur[-1]["end_date"], iv["end_date"])
                else:
                    cur.append(dict(iv))
    return out


def write_dbn(path: Path, stores, start_ns: int, end_ns: int) -> int:
    """One zstd-compressed DBN file: merged metadata, then every record of every store in order."""
    import databento_dbn as dbn
    from compression import zstd
    first = stores[0].metadata
    meta = dbn.Metadata(dataset=first.dataset, start=start_ns, stype_in=dbn.SType(first.stype_in),
                        stype_out=dbn.SType(first.stype_out), schema=dbn.Schema(first.schema),
                        symbols=list(first.symbols), partial=[], not_found=[], mappings=[
                            SimpleNamespace(raw_symbol=raw, intervals=[SimpleNamespace(**iv) for iv in ivs])
                            for raw, ivs in merge_mappings(stores).items()],
                        end=end_ns, limit=None, ts_out=first.ts_out, version=first.version)
    tmp = path.with_suffix(path.suffix + ".tmp")
    n = 0
    with zstd.open(tmp, "wb") as f:
        f.write(bytes(meta.encode()))
        for s in stores:
            for rec in s:
                f.write(bytes(rec))
                n += 1
    tmp.replace(path)
    return n


def fetch(client, d: date, tries: int = 3):
    start, end = window(d)
    for k in range(tries):
        try:
            return client.timeseries.get_range(dataset=DATASET, symbols=[SYMBOL], stype_in=STYPE, schema=SCHEMA,
                                               start=start, end=end)
        except Exception as e:  # noqa: BLE001
            if k == tries - 1:
                raise RuntimeError(f"{d}: {e}") from e
            _time.sleep(2 * (k + 1))


def summary(path: Path) -> None:
    import databento as db
    s = db.DBNStore.from_file(path)
    ts = [r.ts_event for r in s]
    days = {datetime.fromtimestamp(t / 1e9, timezone.utc).astimezone(ET).date() for t in ts}
    fmt = lambda t: datetime.fromtimestamp(t / 1e9, timezone.utc).astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")  # noqa: E731
    print(f"File       {path.relative_to(ROOT)}")
    print(f"Rows       {len(ts):,}")
    print(f"First      {fmt(min(ts))}" if ts else "First      -")
    print(f"Last       {fmt(max(ts))}" if ts else "Last       -")
    print(f"Days       {len(days)} with data (of {len(weekdays(FIRST, LAST))} weekdays requested)")
    print(f"Size       {path.stat().st_size / 1e6:,.2f} MB")
    print(f"Mappings   {s.metadata.mappings.get(SYMBOL, [])[:1]} ... {len(s.metadata.mappings.get(SYMBOL, []))} interval(s)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quote", action="store_true", help="print the cost and stop")
    ap.add_argument("--yes", action="store_true", help="skip the y/N prompt")
    a = ap.parse_args()
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        sys.exit("Set DATABENTO_API_KEY first.")
    if OUT.exists():
        sys.exit(f"{OUT.relative_to(ROOT)} already exists; not buying it again.")
    import databento as db
    client = db.Historical(key)
    days = weekdays(FIRST, LAST)
    quote = cost(client, SYMBOL, days)
    print(f"Quote: {DATASET} {SYMBOL} {SCHEMA}, 09:30-12:00 ET, {len(days)} weekdays {FIRST}..{LAST}: ${quote:,.2f}")
    if quote > MAX_COST:
        sys.exit(f"Aborting: the quote is above ${MAX_COST:.0f}.")
    if a.quote:
        return
    if not a.yes:
        try:
            ans = input(f"Download for ${quote:,.2f}? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans != "y":
            sys.exit("Not downloaded.")
    with ThreadPoolExecutor(4) as ex:
        stores = list(ex.map(lambda d: fetch(client, d), days))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    n = write_dbn(OUT, stores, _ns(window(days[0])[0]), _ns(window(days[-1])[1]))
    print(f"Wrote {n:,} records.")
    summary(OUT)


if __name__ == "__main__":
    main()
