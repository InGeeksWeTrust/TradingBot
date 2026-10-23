"""A scriptable fake TopstepX broker with fault injection, a fake clock, and a scenario driver
that runs the real Session.poll() loop. Everything writes under a pytest tmp_path."""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, time, timedelta, timezone

from orbbot.api import (ORDER_LIMIT, ORDER_MARKET, ORDER_STOP, SIDE_BUY, STATUS_CANCELLED, STATUS_FILLED,
                        STATUS_OPEN, AmbiguousError, ApiError)
from orbbot.config import BotConfig, RiskConfig, SafetyConfig, StrategyConfig
from orbbot.features import Levels
from orbbot.research import NASDAQ, Sweep
from orbbot.strategy import ET

CID = "CON.F.US.MNQ.Z26"
DAY = date(2026, 10, 23)           # a Friday, EDT
PDH = 20100.0                      # prior-day high the scripted sweep runs through


class Down(RuntimeError):
    pass


class FakeClock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds: float):
        self.t += timedelta(seconds=seconds)


class FakeBroker:
    """Holds orders/positions/trades like ProjectX. Faults are set as attributes by each test."""

    def __init__(self, clock: FakeClock, balance: float = 50_000.0):
        self.clock = clock
        self.balance = balance
        self.orders: list[dict] = []        # all orders, any status
        self.positions: list[dict] = []
        self.trades: list[dict] = []
        self.place_calls: list[dict] = []
        self.close_calls = 0
        self.cancel_calls: list[int] = []
        self._id = 100
        # --- faults
        self.place_mode = "ok"              # ok | timeout_after | timeout_before | reject | pending | dup_tag
        self.fill_qty = None                # partial entry fill size
        self.bracket_mode = "ok"            # ok | missing | wrong_qty | wrong_side
        self.bracket_delay = 0              # Order/searchOpen calls before the bracket shows up
        self.order_visible_after = 0        # Order/search calls before a placed order is visible
        self.close_mode = "ok"              # ok | reject | timeout
        self.down = False                   # every call fails
        self.account_down = False
        self.bars_stop_at: datetime | None = None  # stale market data after this time
        self.price_fn = default_price
        self._search_calls = 0
        self._open_calls = 0
        self._pending_legs: list[dict] = []
        self._legs_release = 0

    # ---------------- helpers
    def _next(self):
        self._id += 1
        return self._id

    def _chk(self):
        if self.down:
            raise Down("connection refused")

    def now(self):
        return self.clock().astimezone(timezone.utc)

    def add_position(self, size, avg, long=True, cid=CID):
        self.positions.append({"id": self._next(), "accountId": 1, "contractId": cid,
                               "creationTimestamp": self.now().isoformat(), "type": 1 if long else 2,
                               "size": size, "averagePrice": avg})

    def add_stop(self, size, price, sell=True, cid=CID, created=None):
        o = {"id": self._next(), "accountId": 1, "contractId": cid,
             "creationTimestamp": (created or self.now()).isoformat(), "updateTimestamp": self.now().isoformat(),
             "status": STATUS_OPEN, "type": ORDER_STOP, "side": 1 if sell else 0, "size": size,
             "limitPrice": None, "stopPrice": price, "fillVolume": 0, "filledPrice": None, "customTag": None}
        self.orders.append(o)
        return o

    def pos(self):
        return next((p for p in self.positions if p["contractId"] == CID), None)

    # ---------------- API surface used by the bot
    def login(self):
        self._chk()

    def search_accounts(self, only_active=True):
        self._chk()
        if self.account_down:
            raise Down("account service unavailable")
        return [{"id": 1, "name": "TEST", "balance": self.balance, "canTrade": True, "isVisible": True}]

    def search_contracts(self, text, live=False):
        self._chk()
        return [{"id": CID, "name": "MNQZ6", "activeContract": True, "tickSize": 0.25, "tickValue": 0.5}]

    def retrieve_bars(self, contract_id, start, end, include_partial=False, **kw):
        self._chk()
        now = self.clock()
        stop = min(end, now if include_partial else now)
        if self.bars_stop_at is not None:
            stop = min(stop, self.bars_stop_at)
        out, t = [], start.astimezone(ET).replace(second=0, microsecond=0)
        while t <= stop:
            if not (time(16, 0) <= t.time() < time(18, 0)):
                o, h, l, c = self.price_fn(t)
                out.append({"t": t.astimezone(timezone.utc).isoformat(), "o": o, "h": h, "l": l, "c": c, "v": 100})
            t += timedelta(minutes=1)
        return out

    def place_order(self, account_id, contract_id, order_type, side, size, limit_price=None, stop_price=None,
                    sl_ticks=None, tp_ticks=None, custom_tag=None):
        self._chk()
        self.place_calls.append({"tag": custom_tag, "side": side, "size": size, "type": order_type})
        if self.place_mode == "reject":
            raise ApiError("Order/place", 2, "Invalid size")
        if self.place_mode == "timeout_before":
            raise AmbiguousError("Order/place", "read timeout")
        if any(o.get("customTag") == custom_tag for o in self.orders if custom_tag):
            raise ApiError("Order/place", 2, "customTag must be unique across the account")
        buy = side == SIDE_BUY
        px = self.price_fn(self.clock().astimezone(ET).replace(second=0, microsecond=0))[3]
        px += 0.25 if buy else -0.25
        filled = size if self.fill_qty is None else self.fill_qty
        oid = self._next()
        o = {"id": oid, "accountId": 1, "contractId": contract_id, "creationTimestamp": self.now().isoformat(),
             "updateTimestamp": self.now().isoformat(), "status": STATUS_FILLED if filled == size else STATUS_OPEN,
             "type": ORDER_MARKET, "side": side, "size": size, "limitPrice": None, "stopPrice": None,
             "fillVolume": filled, "filledPrice": px, "customTag": custom_tag, "_visible_after": self.order_visible_after}
        self.orders.append(o)
        if filled:
            self.add_position(filled, px, long=buy)
        if filled:
          self.trades.append({"id": self._next(), "accountId": 1, "contractId": contract_id,
                            "creationTimestamp": self.now().isoformat(), "price": px, "profitAndLoss": None,
                            "fees": 0.61 * filled, "side": side, "size": filled, "voided": False, "orderId": oid})
        if filled and self.bracket_mode != "missing" and sl_ticks is not None:
            qty = size + 2 if self.bracket_mode == "wrong_qty" else size
            close_sell = buy if self.bracket_mode != "wrong_side" else not buy
            stop = px + sl_ticks * 0.25
            leg = self.add_stop(qty, stop, sell=close_sell)
            tgt = dict(leg, id=self._next(), type=ORDER_LIMIT, stopPrice=None, limitPrice=px + tp_ticks * 0.25,
                       side=1 if buy else 0)
            self.orders.append(tgt)
            if self.bracket_delay:
                for x in (leg, tgt):
                    x["_hidden"] = True
                self._legs_release = self._open_calls + self.bracket_delay
        if self.place_mode == "timeout_after":
            raise AmbiguousError("Order/place", "read timeout")
        if self.place_mode == "pending":
            raise ApiError("Order/place", 6, "Order pending")
        if self.place_mode == "dup_tag":
            raise ApiError("Order/place", 2, "customTag must be unique across the account")
        return oid

    def search_orders(self, account_id, start, end=None):
        self._chk()
        self._search_calls += 1
        out = []
        for o in self.orders:
            if o.get("_visible_after", 0) >= self._search_calls:
                continue
            if datetime.fromisoformat(o["creationTimestamp"]) >= start:
                out.append({k: v for k, v in o.items() if not k.startswith("_")})
        return out

    def search_open_orders(self, account_id):
        self._chk()
        self._open_calls += 1
        if self._legs_release and self._open_calls >= self._legs_release:
            for o in self.orders:
                o.pop("_hidden", None)
            self._legs_release = 0
        return [{k: v for k, v in o.items() if not k.startswith("_")} for o in self.orders
                if o["status"] == STATUS_OPEN and o["type"] != ORDER_MARKET and not o.get("_hidden")]

    def search_open_positions(self, account_id):
        self._chk()
        return [dict(p) for p in self.positions if p["size"]]

    def cancel_order(self, account_id, order_id):
        self._chk()
        self.cancel_calls.append(order_id)
        for o in self.orders:
            if o["id"] == order_id and o["status"] == STATUS_OPEN:
                o["status"] = STATUS_CANCELLED

    def close_position(self, account_id, contract_id):
        self._chk()
        self.close_calls += 1
        if self.close_mode == "reject":
            raise ApiError("Position/closeContract", 2, "rejected")
        if self.close_mode == "timeout":
            raise Down("read timeout")
        px = self.price_fn(self.clock().astimezone(ET).replace(second=0, microsecond=0))[3]
        if self.close_mode == "race_bracket_first":     # the stop fills a moment before our close arrives
            self.close_mode = "ok"
            stop = next(o for o in self.orders if o["type"] == ORDER_STOP and o["status"] == STATUS_OPEN)
            self.exit_position(stop["stopPrice"], self.pos()["size"], leg=stop)
            raise ApiError("Position/closeContract", 2, "No open position")
        p = next((p for p in self.positions if p["contractId"] == contract_id), None)
        if p:
            self.exit_position(px, p["size"])
        if self.close_mode == "race_stop_after":        # closed, but the still-working stop then triggers
            self.close_mode = "ok"
            stop = next(o for o in self.orders if o["type"] == ORDER_STOP and o["status"] == STATUS_OPEN)
            stop["status"], stop["fillVolume"] = STATUS_FILLED, stop["size"]
            self.add_position(stop["size"], stop["stopPrice"], long=stop["side"] == 0)   # opens the other way
            self.reversed = True

    def exit_position(self, price, qty, leg=None):
        """Reduce/close the position at `price` (a bracket fill or a close)."""
        p = self.pos()
        sign = 1 if p["type"] == 1 else -1
        pnl = (price - p["averagePrice"]) * sign * qty * 2.0
        self.trades.append({"id": self._next(), "accountId": 1, "contractId": CID,
                            "creationTimestamp": self.now().isoformat(), "price": price, "profitAndLoss": pnl,
                            "fees": 0.61 * qty, "side": 1 if sign > 0 else 0, "size": qty, "voided": False,
                            "orderId": leg["id"] if leg else 0})
        self.balance += pnl - 1.22 * qty
        p["size"] -= qty
        if p["size"] <= 0:
            self.positions.remove(p)
        if leg is not None:
            leg["fillVolume"] = leg.get("fillVolume", 0) + qty
            if leg["fillVolume"] >= leg["size"]:
                leg["status"] = STATUS_FILLED
                for o in self.orders:    # OCO: the sibling goes too
                    if o is not leg and o["status"] == STATUS_OPEN and o["type"] in (ORDER_STOP, ORDER_LIMIT):
                        o["status"] = STATUS_CANCELLED

    def hit_target(self, qty=None):
        tgt = next(o for o in self.orders if o["type"] == ORDER_LIMIT and o["status"] == STATUS_OPEN)
        self.exit_position(tgt["limitPrice"], qty or self.pos()["size"], leg=tgt)

    def search_trades(self, account_id, start, end=None):
        self._chk()
        return [dict(t) for t in self.trades if datetime.fromisoformat(t["creationTimestamp"]) >= start]


def default_price(t: datetime):
    """Prior day trades around 20000 with a 20100 high at 14:00; today 20050 except a 9:40-9:44 ET
    5-minute bar that wicks to 20110 and closes 20090: a short sweep of the prior-day high."""
    et = t.astimezone(ET)
    if et.date() < DAY:
        if et.time() == time(14, 0):
            return (20000, PDH, 19990, 20000)
        return (20000, 20005, 19995, 20000)
    if time(9, 40) <= et.time() < time(9, 45):
        if et.time() == time(9, 42):
            return (20090, 20110, 20088, 20092)
        if et.time() == time(9, 44):
            return (20092, 20095, 20088, 20090)
        return (20090, 20094, 20088, 20091)
    return (20050, 20052, 20048, 20050)


class FakeFeats:
    def __init__(self, lv):
        self.lv = lv

    def day_levels(self, d):
        return self.lv


def make_cfg(tmp, **safety):
    cfg = BotConfig(symbol="MNQ", entry="sweep", confirm_symbol="", account_name="TEST", poll_seconds=5,
                    kill_switch_file=str(tmp / "STOP"), log_dir=str(tmp / "logs"), journal_dir=str(tmp / "journal"))
    cfg.strategy = StrategyConfig(entry_cutoff=time(11, 0), flatten_time=time(11, 55), max_trades_per_day=2)
    cfg.risk = RiskConfig(stop_mode="structural", contracts=10, max_contracts=10, max_risk_per_trade=250,
                          target_per_trade=500, max_daily_loss=500, max_daily_profit=750, fee_per_contract_rt=1.22,
                          size_with_costs=True, est_slippage_ticks=1)
    cfg.safety = dataclasses.replace(SafetyConfig(state_dir=str(tmp / "state"), halt_file=str(tmp / "HALT")), **safety)
    return cfg


class Scenario:
    """Runs the real bot Session against a FakeBroker with a fake clock."""

    def __init__(self, tmp, start=time(9, 30, 30), broker=None, cfg=None, clock=None, **safety):
        from orbbot.bot import ORBBot, Session
        self.tmp = tmp
        self.clock = clock or FakeClock(datetime.combine(DAY, start, ET))
        self.broker = broker or FakeBroker(self.clock)
        self.cfg = cfg or make_cfg(tmp, **safety)
        self.bot = ORBBot(self.cfg, mode="paper", api=self.broker, clock=self.clock, sleep=self.clock.advance)
        self.bot.connect()
        self.sess = Session(self.bot, DAY)

        def build(sess=self.sess):     # real Sweep, scripted levels (no history download needed)
            sw = Sweep(sess.s, 0.25, FakeFeats(Levels(pdh=PDH)), window="am", session=NASDAQ,
                       max_trades=sess.s.max_trades_per_day)
            sw.reset(DAY)
            sess.inner = sess.strat = sw
            sess._preload()
        self.sess.build_sweep = build
        self.sess.start()

    def run_until(self, hh, mm, ss=0, step=5):
        end = datetime.combine(DAY, time(hh, mm, ss), ET)
        out = None
        while self.clock() < end:
            out = self.sess.poll()
            if out == "done":
                return out
            self.clock.advance(step)
        return out

    @property
    def eng(self):
        return self.sess.engine

    def states(self, tag=None):
        tag = tag or self.broker.place_calls[-1]["tag"]
        return [t[1] for t in self.bot.store.transitions(tag)]

    def events(self, kind):
        return self.bot.store.events(kind)

    def close(self):
        self.bot.store.close()
