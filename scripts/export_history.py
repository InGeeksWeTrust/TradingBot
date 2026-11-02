"""READ-ONLY export of past Express account trade and order history. Sends no orders, changes nothing.

    python scripts/export_history.py [--start 2023-01-01]

1) Account/search with onlyActiveAccounts=false: lists every account and looks for EXPRESS_ACCOUNTS.
2) For each one found, Trade/search and Order/search in 30-day windows from --start to now. Every field
   the API returns is saved to research/history/<account>_trades.csv and <account>_orders.csv
   (gitignored). Voided trades are kept and flagged in the `voided_flag` column.
3) Prints per account: date range, fills, P&L, fees and contracts traded. No analysis.

Credentials come from TOPSTEPX_USERNAME / TOPSTEPX_API_KEY and are never printed.
Only read endpoints are called: Auth/loginKey, Account/search, Trade/search, Order/search.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orbbot.api import ApiError, ProjectXClient  # noqa: E402
from orbbot.config import _env  # noqa: E402

OUT = ROOT / "research" / "history"
WINDOW = timedelta(days=30)
PAUSE_S = 0.7  # between calls: an unpaced run hit HTTP 429 after ~100 calls
EXPRESS_ACCOUNTS = [
    "EXPRESS-V2-387652-46581084",
    "EXPRESS-V2-387652-43497191",
    "EXPRESS-V2-DLL-387652-13072940",
    "EXPRESS-V2-387652-44118636",
    "EXPRESS-V2-387652-96687247",
    "EXPRESS-V2-DLL-387652-53382767",
]


def windows(start: datetime, end: datetime):
    t = start
    while t < end:
        yield t, min(t + WINDOW, end)
        t += WINDOW


def fetch(fn, account_id: int, start: datetime, end: datetime) -> list[dict]:
    """Every row from fn over [start, end) in 30-day windows, de-duplicated by id."""
    rows: dict = {}
    for a, b in windows(start, end):
        for r in fn(account_id, a, b):
            rows.setdefault(r.get("id"), r)
        time.sleep(PAUSE_S)
    return sorted(rows.values(), key=lambda r: (r.get("creationTimestamp") or "", r.get("id") or 0))


def cell(v):
    return json.dumps(v) if isinstance(v, (dict, list)) else v


def write_csv(path: Path, rows: list[dict], extra: dict | None = None) -> None:
    cols: list[str] = []
    for r in rows:
        cols += [k for k in r if k not in cols]
    cols += list(extra or {})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols or ["id"])
        w.writeheader()
        for r in rows:
            out = {k: cell(v) for k, v in r.items()}
            for k, fn in (extra or {}).items():
                out[k] = fn(r)
            w.writerow(out)


def num(v) -> float:
    return float(v) if v is not None else 0.0


def summarize(name: str, trades: list[dict], orders: list[dict]) -> None:
    live = [t for t in trades if not t.get("voided")]
    void = [t for t in trades if t.get("voided")]
    stamps = [r["creationTimestamp"] for r in trades + orders if r.get("creationTimestamp")]
    print(f"\n{name}")
    if stamps:
        print(f"  date range      {min(stamps)[:10]} .. {max(stamps)[:10]}")
    else:
        print("  date range      (no trades or orders returned)")
    print(f"  fills           {len(live)}   (+ {len(void)} voided)")
    print(f"  total P&L       ${sum(num(t.get('profitAndLoss')) for t in live):,.2f}"
          + (f"   (voided: ${sum(num(t.get('profitAndLoss')) for t in void):,.2f})" if void else ""))
    print(f"  total fees      ${sum(num(t.get('fees')) for t in live):,.2f}"
          + (f"   (voided: ${sum(num(t.get('fees')) for t in void):,.2f})" if void else ""))
    by_contract = Counter()
    for t in live:
        by_contract[t.get("contractId")] += int(num(t.get("size")))
    print(f"  contracts       {sum(by_contract.values())}"
          + "".join(f"\n    {c}: {n}" for c, n in sorted(by_contract.items(), key=lambda x: str(x[0]))))
    print(f"  orders          {len(orders)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2023-01-01", help="first day to search (UTC), default 2023-01-01")
    args = ap.parse_args()
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.now(timezone.utc)

    api = ProjectXClient(_env("TOPSTEPX_USERNAME"), _env("TOPSTEPX_API_KEY"))
    try:
        api.login()
    except SystemExit as e:  # the client's message names the username; print only the first line
        raise SystemExit(str(e).splitlines()[0]) from None

    accounts = api.search_accounts(only_active=False)
    print(f"Account/search (onlyActiveAccounts=false): {len(accounts)} accounts")
    print(f"  {'id':>10}  {'name':<34} canTrade isVisible")
    for a in sorted(accounts, key=lambda a: a.get("name") or ""):
        print(f"  {a.get('id'):>10}  {a.get('name') or '':<34} {str(a.get('canTrade')):<8} {a.get('isVisible')}")

    by_name = {a.get("name"): a for a in accounts}
    missing = [n for n in EXPRESS_ACCOUNTS if n not in by_name]
    print("\nNot returned by Account/search: " + (", ".join(missing) if missing else "none"))

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"\nSearching {start:%Y-%m-%d} .. {end:%Y-%m-%d} in 30-day windows; writing to {OUT}")
    for name in EXPRESS_ACCOUNTS:
        if name not in by_name:
            continue
        aid = by_name[name]["id"]
        try:
            trades = fetch(api.search_trades, aid, start, end)
            orders = fetch(api.search_orders, aid, start, end)
        except (ApiError, RuntimeError) as e:
            print(f"\n{name}\n  ERROR: {e} (nothing written)")
            continue
        write_csv(OUT / f"{name}_trades.csv", trades,
                  {"voided_flag": lambda r: "VOIDED" if r.get("voided") else ""})
        write_csv(OUT / f"{name}_orders.csv", orders)
        summarize(name, trades, orders)


if __name__ == "__main__":
    main()
