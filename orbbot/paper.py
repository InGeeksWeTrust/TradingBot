"""Paper broker for --dry-run.

Wraps the real ProjectX client: login, accounts, contracts and market data go to TopstepX,
but orders, positions and trades are simulated locally from the live bars. Nothing is sent
to your account.

Fill model:
  * market entries/exits fill at the latest price +/- `slippage_ticks`
  * stop and target legs are checked against each 1-min bar AFTER the fill minute
    (high/low) and against the latest price every poll; stop wins if both are touched
  * stops fill `slippage_ticks` worse, targets fill at their limit price
  * a bracket fill cancels its sibling (Auto OCO); a manual close leaves both legs open,
    exactly like TopstepX, so the bot's cleanup path gets exercised

Durability: with `state_path`, the simulated account (position, working and past orders, fills,
used customTags, id counter) is written atomically to a JSON file after every change and loaded on
start, so a restarted paper bot reconciles against the same broker state a real account would show.
Fills that would have happened while the bot was down are applied when the missed bars are replayed.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .api import (ORDER_LIMIT, ORDER_MARKET, ORDER_STOP, POS_LONG, POS_SHORT, SIDE_BUY, STATUS_CANCELLED,
                  STATUS_FILLED, STATUS_OPEN, ApiError)

log = logging.getLogger(__name__)


class PaperClient:
    def __init__(self, real, slippage_ticks: int = 1, fee_per_contract_rt: float = 0.74,
                 state_path: str | Path | None = None, clock=None):
        self.real = real
        self.state_path = Path(state_path) if state_path else None
        self.clock = clock
        self.slip_ticks = slippage_ticks
        self.fee_side = fee_per_contract_rt / 2
        self.tick = 0.25
        self.pv = 2.0
        self.last_price: float | None = None
        self.last_time: datetime | None = None
        self.pos: dict | None = None
        self.orders: list[dict] = []      # working (open) orders
        self.history: list[dict] = []     # every order ever placed, any status (Order/search)
        self.trades: list[dict] = []
        self.tags: set[str] = set()
        self.start_balance: float | None = None
        self._id = 1
        self.watch: str | None = None  # contract whose bars drive simulated fills
        if self.state_path is not None:
            self._load()

    # ---- durability
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            d = json.loads(self.state_path.read_text())
        except (OSError, ValueError) as e:
            raise SystemExit(f"Paper broker state {self.state_path} is unreadable ({e}). It holds the simulated "
                             f"account; move it aside only if you accept losing the open paper position.") from e
        self._id = d["next_id"]
        self.history = d["history"]
        open_ids = set(d["open_ids"])
        self.orders = [o for o in self.history if o["id"] in open_ids]
        self.trades = d["trades"]
        self.tags = set(d["tags"])
        self.pos = d["pos"]
        if self.pos and self.pos.get("_fill_minute"):
            self.pos["_fill_minute"] = datetime.fromisoformat(self.pos["_fill_minute"])
        log.info("[paper] restored simulated account from %s: position %s, %d working orders",
                 self.state_path, self.pos and self.pos["size"], len(self.orders))

    def _save(self) -> None:
        if self.state_path is None:
            return
        pos = dict(self.pos) if self.pos else None
        if pos and isinstance(pos.get("_fill_minute"), datetime):
            pos["_fill_minute"] = pos["_fill_minute"].isoformat()
        d = {"next_id": self._id, "history": self.history, "open_ids": [o["id"] for o in self.orders],
             "trades": self.trades, "tags": sorted(self.tags), "pos": pos}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(d, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)  # atomic: a crash leaves the old or the new file, never half

    def set_contract(self, contract_id: str, tick: float, point_value: float) -> None:
        self.watch, self.tick, self.pv = contract_id, tick, point_value

    # ---- passthrough
    def login(self):
        return self.real.login()

    def search_accounts(self, only_active: bool = True):
        """Real accounts, with the balance moved by the simulated trades' net P&L (fees included)."""
        accts = [dict(a) for a in self.real.search_accounts(only_active)]
        net = sum((t["profitAndLoss"] or 0) - t["fees"] for t in self.trades if not t.get("voided"))
        for a in accts:
            a["balance"] = float(a.get("balance") or 0) + net
        return accts

    def search_contracts(self, text: str, live: bool = False):
        return self.real.search_contracts(text, live)

    def retrieve_bars(self, contract_id, start, end, **kw):
        bars = self.real.retrieve_bars(contract_id, start, end, **kw)
        if self.watch is None or contract_id == self.watch:  # ignore other markets' bars
            self._on_bars(sorted(bars, key=lambda b: b["t"]))
        return bars

    # ---- simulated account
    def _next(self) -> int:
        self._id += 1
        return self._id

    def _now(self) -> datetime:
        return self.clock().astimezone(timezone.utc) if self.clock else datetime.now(timezone.utc)

    def _fill(self, side_buy: bool, size: int, price: float, pnl: float | None, order_id: int) -> None:
        self.trades.append({
            "id": self._next(), "accountId": 0, "contractId": self.pos["contractId"] if self.pos else "",
            "creationTimestamp": self._now().isoformat(), "price": round(price, 2),
            "profitAndLoss": None if pnl is None else round(pnl, 2),
            "fees": round(self.fee_side * size, 2), "side": 0 if side_buy else 1,
            "size": size, "voided": False, "orderId": order_id,
        })

    def place_order(self, account_id, contract_id, order_type, side, size, limit_price=None, stop_price=None,
                    sl_ticks=None, tp_ticks=None, custom_tag=None) -> int:
        if order_type != ORDER_MARKET:
            raise NotImplementedError("paper broker only simulates market entries")
        if custom_tag and custom_tag in self.tags:
            raise ApiError("Order/place", 2, "customTag must be unique across the account")
        if self.last_price is None:
            raise ApiError("Order/place", 2, "no last price")
        if self.pos:
            raise ApiError("Order/place", 2, "paper broker: already in a position")
        if custom_tag:
            self.tags.add(custom_tag)
        buy = side == SIDE_BUY
        slip = self.slip_ticks * self.tick
        px = self.last_price + slip if buy else self.last_price - slip
        oid = self._next()
        now = self._now().isoformat()
        self.history.append({"id": oid, "accountId": account_id, "contractId": contract_id, "creationTimestamp": now,
                             "updateTimestamp": now, "status": STATUS_FILLED, "type": ORDER_MARKET, "side": side,
                             "size": size, "limitPrice": None, "stopPrice": None, "fillVolume": size,
                             "filledPrice": px, "customTag": custom_tag})
        self.pos = {"id": oid, "accountId": account_id, "contractId": contract_id,
                    "creationTimestamp": self._now().isoformat(), "type": POS_LONG if buy else POS_SHORT,
                    "size": size, "averagePrice": px, "_fill_minute": self.last_time}
        self._fill(buy, size, px, None, oid)
        leg = {"accountId": account_id, "contractId": contract_id, "creationTimestamp": now, "updateTimestamp": now,
               "status": STATUS_OPEN, "side": 1 if buy else 0, "size": size, "fillVolume": 0, "filledPrice": None,
               "customTag": None, "limitPrice": None, "stopPrice": None}
        if sl_ticks is not None:
            self.orders.append({**leg, "id": self._next(), "type": ORDER_STOP, "stopPrice": px + sl_ticks * self.tick})
        if tp_ticks is not None:
            self.orders.append({**leg, "id": self._next(), "type": ORDER_LIMIT, "limitPrice": px + tp_ticks * self.tick})
        self.history.extend(self.orders[-2:] if (sl_ticks is not None and tp_ticks is not None) else self.orders[-1:])
        log.info("[paper] filled %s %d @ %.2f", "BUY" if buy else "SELL", size, px)
        self._save()
        return oid

    def _exit(self, price: float, order_id: int, why: str) -> None:
        p = self.pos
        sign = 1 if p["type"] == POS_LONG else -1
        pnl = (price - p["averagePrice"]) * sign * p["size"] * self.pv
        self._fill(sign < 0, p["size"], price, pnl, order_id)
        log.info("[paper] %s exit %d @ %.2f, P&L $%.2f", why, p["size"], price, pnl)
        self.pos = None
        self._save()

    def _check_brackets(self, hi: float, lo: float) -> None:
        if not self.pos:
            return
        long = self.pos["type"] == POS_LONG
        slip = self.slip_ticks * self.tick
        stop = next((o for o in self.orders if o["type"] == ORDER_STOP), None)
        tgt = next((o for o in self.orders if o["type"] == ORDER_LIMIT), None)
        if stop and ((long and lo <= stop["stopPrice"]) or (not long and hi >= stop["stopPrice"])):
            self._exit(stop["stopPrice"] - slip if long else stop["stopPrice"] + slip, stop["id"], "stop")
            self._close_legs(stop)  # OCO: sibling cancelled
        elif tgt and ((long and hi >= tgt["limitPrice"]) or (not long and lo <= tgt["limitPrice"])):
            self._exit(tgt["limitPrice"], tgt["id"], "target")
            self._close_legs(tgt)

    def _close_legs(self, filled: dict) -> None:
        for o in self.orders:
            o["status"] = STATUS_FILLED if o is filled else STATUS_CANCELLED
            if o is filled:
                o["fillVolume"] = o["size"]
        self.orders = []
        self._save()

    def _on_bars(self, bars: list[dict]) -> None:
        if not bars:
            return
        for b in bars:
            t = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
            fill_minute = self.pos and self.pos.get("_fill_minute")
            if self.pos and fill_minute is not None and t > fill_minute:
                self._check_brackets(float(b["h"]), float(b["l"]))
        last = bars[-1]
        self.last_price = float(last["c"])
        self.last_time = datetime.fromisoformat(last["t"].replace("Z", "+00:00"))
        self._check_brackets(self.last_price, self.last_price)

    def search_open_positions(self, account_id):
        return [{k: v for k, v in self.pos.items() if not k.startswith("_")}] if self.pos else []

    def search_open_orders(self, account_id):
        return [dict(o) for o in self.orders]

    def cancel_order(self, account_id, order_id):
        for o in self.orders:
            if o["id"] == order_id:
                o["status"] = STATUS_CANCELLED
        self.orders = [o for o in self.orders if o["id"] != order_id]
        self._save()

    def search_orders(self, account_id, start, end=None):
        out = []
        for o in self.history:
            ts = datetime.fromisoformat(o["creationTimestamp"])
            if ts >= start and (end is None or ts <= end):
                out.append(dict(o))
        return out

    def close_position(self, account_id, contract_id):
        if not self.pos:
            return
        slip = self.slip_ticks * self.tick
        long = self.pos["type"] == POS_LONG
        self._exit(self.last_price - slip if long else self.last_price + slip, self._next(), "close")

    def search_trades(self, account_id, start, end=None):
        out = []
        for t in self.trades:
            ts = datetime.fromisoformat(t["creationTimestamp"])
            if ts >= start and (end is None or ts <= end):
                out.append(t)
        return out
