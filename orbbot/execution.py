"""Execution engine: order intents with an explicit state machine, single-shot submission with
reconciliation, protective-stop verification, controlled flattening, data-freshness tracking and
an account-level (Topstep Maximum Loss Limit) guard.

Rules this module enforces:
  * An order is sent at most once. After a timeout, a dropped connection, HTTP 5xx/401/429 or an
    ambiguous broker answer (errorCode 6/7, or a duplicate-tag rejection) the intent becomes
    UNKNOWN -> RECONCILING and the bot looks the order up by its customTag. UNKNOWN is never
    treated as rejected; "not accepted" is concluded only after the broker's order list and
    position list both show nothing for `reconcile_grace_s`.
  * A position counts as protected only if a working stop order exists on the same contract, on
    the opposite side, for exactly the position's size, with a price on the loss side of the entry
    and within tolerance of the planned stop. Brackets have no parent link in the API, so the
    relationship is established by contract + side + size + price + creation after the entry.
  * Recovery from missing or wrong protection is to flatten via Position/closeContract (which closes
    exactly the open size and cannot reverse a position) and then cancel leftover orders, verifying
    after every step. No extra stop orders are placed: with Auto OCO brackets a late-arriving bracket
    plus our own stop could both fill and flip the position.
  * Flattening is attempted a bounded number of times and verified; failure is reported, never
    assumed to have worked.
"""
from __future__ import annotations

import logging
import time as _time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from .api import (ORDER_MARKET, ORDER_STOP, ORDER_STOP_LIMIT, ORDER_TRAILING_STOP, SIDE_BUY, SIDE_SELL,
                  STATUS_CANCELLED, STATUS_EXPIRED, STATUS_FILLED, STATUS_OPEN, STATUS_PENDING,
                  STATUS_REJECTED, AmbiguousError, ApiError)
from .state import StateError, StateStore

log = logging.getLogger("orbbot.exec")


class S(str, Enum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PROTECTED = "PROTECTED"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"


TERMINAL = {S.CLOSED, S.CANCELLED, S.REJECTED}
CT = ZoneInfo("America/Chicago")
TOPSTEP_DAY_END = time(15, 10)   # Topstep trading day: 5:00 PM CT to 3:10 PM CT
ALLOWED = {
    S.CREATED: {S.SUBMITTING, S.CANCELLED},
    S.SUBMITTING: {S.ACKNOWLEDGED, S.REJECTED, S.UNKNOWN, S.FILLED, S.PARTIALLY_FILLED},
    S.UNKNOWN: {S.RECONCILING},
    S.RECONCILING: {S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.PROTECTED, S.CANCELLED, S.REJECTED,
                    S.UNKNOWN, S.EXIT_PENDING, S.CLOSED},
    S.ACKNOWLEDGED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED, S.RECONCILING, S.EXIT_PENDING},
    S.PARTIALLY_FILLED: {S.FILLED, S.PROTECTED, S.EXIT_PENDING, S.CLOSED, S.RECONCILING},
    S.FILLED: {S.PROTECTED, S.EXIT_PENDING, S.CLOSED, S.RECONCILING},
    S.PROTECTED: {S.EXIT_PENDING, S.CLOSED, S.RECONCILING},
    S.EXIT_PENDING: {S.CLOSED, S.RECONCILING, S.EXIT_PENDING},
}
STOP_TYPES = {ORDER_STOP, ORDER_STOP_LIMIT, ORDER_TRAILING_STOP}
LIVE_STATUSES = {STATUS_OPEN, STATUS_PENDING}


class TransitionError(RuntimeError):
    pass


def _ts(x) -> datetime | None:
    if not x:
        return None
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


# ---------------------------------------------------------------------- freshness
class Freshness:
    """Last successful update time per data source; `stale()` names what is too old."""

    def __init__(self, safety, clock):
        self.c = safety
        self.clock = clock
        self.seen: dict[str, datetime] = {}
        self.bar_time: dict[str, datetime] = {}

    def ok(self, what: str) -> None:
        self.seen[what] = self.clock()

    def bar(self, what: str, newest_completed_open: datetime | None) -> None:
        if newest_completed_open is not None:
            self.bar_time[what] = newest_completed_open

    def age(self, what: str) -> float:
        t = self.seen.get(what)
        return float("inf") if t is None else (self.clock() - t).total_seconds()

    def bar_age(self, what: str) -> float:
        t = self.bar_time.get(what)
        return float("inf") if t is None else (self.clock() - (t + timedelta(minutes=1))).total_seconds()

    def stale(self, need_confirm: bool) -> list[str]:
        c, out = self.c, []
        if self.bar_age("bars") > c.bars_max_age_s:
            out.append(f"market bars ({self.bar_age('bars'):.0f}s old)")
        if need_confirm and self.bar_age("confirm") > c.confirm_max_age_s:
            out.append(f"confirmation bars ({self.bar_age('confirm'):.0f}s old)")
        if self.age("positions") > c.positions_max_age_s:
            out.append(f"positions ({self.age('positions'):.0f}s old)")
        if self.age("orders") > c.orders_max_age_s:
            out.append(f"orders ({self.age('orders'):.0f}s old)")
        if self.age("account") > c.account_max_age_s:
            out.append(f"account balance ({self.age('account'):.0f}s old)")
        return out


# ---------------------------------------------------------------------- account guard
class AccountGuard:
    """Topstep Maximum Loss Limit: trails up from each end-of-day balance, never down, locks at the
    starting balance, and is breached by balance + open P&L touching it. The API returns only the
    balance, so the limit and starting balance come from config (or the dashboard floor)."""

    def __init__(self, acfg, store: StateStore):
        self.a = acfg
        self.store = store
        self.balance: float | None = None

    @property
    def enabled(self) -> bool:
        return self.a.max_loss_limit > 0

    def start_balance(self) -> float | None:
        if self.a.start_balance:
            return self.a.start_balance
        return self.store.get("start_balance")

    def floor(self) -> float | None:
        if not self.enabled:
            return None
        if self.a.mll_floor:
            return self.a.mll_floor
        start = self.start_balance()
        if start is None:
            return None
        peak = self.store.get("peak_eod_balance", start)
        return min(start, max(start - self.a.max_loss_limit, peak - self.a.max_loss_limit))

    def update_balance(self, balance: float) -> None:
        self.balance = balance
        if self.enabled and not self.a.start_balance and self.store.get("start_balance") is None:
            self.store.put("start_balance", balance)

    def end_of_day(self, day: date, balance: float, now: datetime, approximate: bool = False) -> bool:
        """Record day's end-of-day balance for the trailing limit. Topstep's trading day ends at
        3:10 PM CT (help.topstep.com, Daily Loss Limit article), so a balance only counts as
        end-of-day once that time has passed for `day`; before it, nothing is recorded."""
        eod = datetime.combine(day, TOPSTEP_DAY_END, CT)
        if now.astimezone(CT) < eod:
            return False
        if self.store.get("last_eod_day") and self.store.get("last_eod_day") >= day.isoformat():
            return False
        peak = self.store.get("peak_eod_balance", self.start_balance() or balance)
        self.store.put("peak_eod_balance", max(peak, balance))
        self.store.put("last_eod_day", day.isoformat())
        self.store.put("last_eod_approx", bool(approximate))
        self.store.event("eod_balance", f"{day}: {balance:.2f}{' (approximate: taken after the fact)' if approximate else ''}"
                                        f" -> floor {self.floor()}")
        return True

    @property
    def floor_approximate(self) -> bool:
        """The floor rests on an end-of-day balance taken after the fact (and no dashboard override)."""
        return self.enabled and not self.a.mll_floor and bool(self.store.get("last_eod_approx", False))

    def buffer(self) -> float:
        return self.a.safety_buffer + (self.a.approx_floor_margin if self.floor_approximate else 0.0)

    def headroom(self, open_pnl: float) -> float | None:
        """Equity above floor + buffer (None = unknown -> no entries). While the floor is approximate
        the buffer is widened by approx_floor_margin, so an under-estimated floor can't let a trade
        come closer to the real limit than the configured buffer."""
        if not self.enabled:
            return float("inf")
        f = self.floor()
        if f is None or self.balance is None:
            return None
        return self.balance + open_pnl - f - self.buffer()


# ---------------------------------------------------------------------- protection check
@dataclass
class Protection:
    ok: bool
    problems: list[str] = field(default_factory=list)
    stop_ids: list[int] = field(default_factory=list)
    stop_price: float | None = None


def verify_protection(position: dict, orders: list[dict], tick: float, expected_stop: float | None,
                      tolerance_ticks: int, not_before: datetime | None = None) -> Protection:
    """Is this position protected by exactly one size-matched working stop on the loss side?"""
    long = position["type"] == 1
    size = int(position["size"])
    avg = float(position["averagePrice"])
    close_side = SIDE_SELL if long else SIDE_BUY
    cid = position["contractId"]
    probs: list[str] = []
    same = [o for o in orders if o.get("contractId") == cid]
    stops = [o for o in same if o.get("type") in STOP_TYPES and o.get("status", STATUS_OPEN) in LIVE_STATUSES]
    if not_before is not None:
        stops = [o for o in stops if _ts(o.get("creationTimestamp")) is None or _ts(o["creationTimestamp"]) >= not_before]
    wrong_side = [o for o in stops if o.get("side") != close_side]
    right = [o for o in stops if o.get("side") == close_side]
    if wrong_side:
        probs.append(f"{len(wrong_side)} stop order(s) on the wrong side (would add to the position)")
    if not right:
        probs.append("no working stop order for the position")
        return Protection(False, probs)
    qty = sum(int(o.get("size", 0)) - int(o.get("fillVolume") or 0) for o in right)
    if qty != size:
        probs.append(f"stop quantity {qty} != position size {size}")
    px = [float(o.get("stopPrice") or 0) for o in right]
    for p in px:
        if (long and p >= avg) or (not long and p <= avg):
            probs.append(f"stop {p} is not on the loss side of entry {avg}")
        if expected_stop is not None and abs(p - expected_stop) > tolerance_ticks * tick + 1e-9:
            probs.append(f"stop {p} is {abs(p - expected_stop) / tick:.0f} ticks from planned {expected_stop}")
    if len(right) > 1:
        probs.append(f"{len(right)} separate stop orders (expected one)")
    return Protection(not probs, probs, [o["id"] for o in right], px[0] if px else None)


# ---------------------------------------------------------------------- engine
class ExecutionEngine:
    def __init__(self, api, store: StateStore, account_id: int, spec, safety, clock=None, sleep=None, mode="paper"):
        self.api, self.store, self.account_id, self.spec, self.c = api, store, account_id, spec, safety
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or _time.sleep
        self.mode = mode
        self.active: dict | None = None        # the one non-terminal entry intent
        self.positions: list[dict] = []
        self.open_orders: list[dict] = []
        self.block_reasons: dict[str, str] = {}  # reason key -> text; any entry blocks new trades
        self.critical: list[str] = []
        self.last_closed: dict | None = None     # intent closed in the latest manage() call

    # ---------------- state machine
    def transition(self, intent: dict, new: S, detail: str = "", **fields) -> None:
        old = S(intent["state"])
        if new not in ALLOWED.get(old, set()):
            raise TransitionError(f"{intent['tag']}: {old.value} -> {new.value} is not allowed")
        self.store.set_state(intent["tag"], new.value, old.value, detail, **fields)
        intent["state"] = new.value
        intent.update(fields)
        log.info("Order %s: %s -> %s %s", intent["tag"], old.value, new.value, detail)

    def block(self, key: str, text: str) -> None:
        if key not in self.block_reasons:
            log.warning("New entries blocked: %s", text)
            self.store.event("block", f"{key}: {text}")
        self.block_reasons[key] = text

    def unblock(self, key: str) -> None:
        if self.block_reasons.pop(key, None) is not None:
            self.store.event("unblock", key)
            log.info("Entry block cleared: %s", key)

    # ---------------- broker snapshots
    def refresh(self, fresh: Freshness) -> bool:
        ok = True
        try:
            self.positions = [p for p in self.api.search_open_positions(self.account_id) if p.get("size")]
            fresh.ok("positions")
        except Exception as e:  # noqa: BLE001
            log.warning("Position/searchOpen failed: %s", e)
            ok = False
        try:
            self.open_orders = self.api.search_open_orders(self.account_id)
            fresh.ok("orders")
        except Exception as e:  # noqa: BLE001
            log.warning("Order/searchOpen failed: %s", e)
            ok = False
        return ok

    def position(self) -> dict | None:
        return next((p for p in self.positions if p["contractId"] == self.spec.contract_id), None)

    def other_positions(self) -> list[dict]:
        return [p for p in self.positions if p["contractId"] != self.spec.contract_id]

    # ---------------- entry
    def new_intent(self, day: date, sig, plan, level: str | None, levels_used: list[str], sides: list[str]) -> dict:
        tag = f"sweep-{day:%Y%m%d}-{sig.side}-{uuid.uuid4().hex[:10]}"
        p = {"stop_ticks": plan.stop_ticks, "target_ticks": plan.target_ticks, "stop_pts": plan.stop_points,
             "target_pts": plan.target_points, "risk": plan.risk_dollars, "cost": plan.cost_dollars,
             "level": level, "signal_time": sig.time.isoformat(), "signal_price": sig.price}
        self.store.create_intent(tag, day, self.spec.contract_id, sig.side, plan.contracts, p, levels_used, sides)
        self.store.forward(tag, day=day.isoformat(), side=sig.side, level=level, signal_time=sig.time.isoformat(),
                           signal_price=sig.price, stop_pts=plan.stop_points, target_pts=plan.target_points,
                           planned_risk=plan.risk_dollars, planned_cost=plan.cost_dollars, mode=self.mode)
        intent = self.store.intent(tag)
        self.active = intent
        return intent

    def submit(self, intent: dict, sl_ticks: int, tp_ticks: int) -> None:
        """Send the entry exactly once."""
        self.transition(intent, S.SUBMITTING, "sending Order/place")
        self.store.forward(intent["tag"], submit_time=self.clock().isoformat())
        side = SIDE_BUY if intent["side"] == "long" else SIDE_SELL
        try:
            oid = self.api.place_order(self.account_id, intent["contract_id"], ORDER_MARKET, side, intent["size"],
                                       sl_ticks=sl_ticks, tp_ticks=tp_ticks, custom_tag=intent["tag"])
        except AmbiguousError as e:
            self.transition(intent, S.UNKNOWN, str(e))
            self.block("unknown_order", f"order {intent['tag']} outcome unknown; reconciling")
            return
        except ApiError as e:
            if e.ambiguous:
                self.transition(intent, S.UNKNOWN, str(e))
                self.block("unknown_order", f"order {intent['tag']} outcome unknown ({e.code}); reconciling")
            else:
                self.transition(intent, S.REJECTED, str(e))
                self.active = None
            return
        except Exception as e:  # noqa: BLE001 - anything else: we can't know
            self.transition(intent, S.UNKNOWN, f"unexpected error: {e}")
            self.block("unknown_order", f"order {intent['tag']} outcome unknown; reconciling")
            return
        self.transition(intent, S.ACKNOWLEDGED, f"orderId {oid}", broker_order_id=oid)
        self.store.forward(intent["tag"], ack_time=self.clock().isoformat(), broker_order_id=oid)

    # ---------------- reconciliation of an uncertain intent
    def find_order(self, intent: dict) -> dict | None:
        start = _ts(intent["created_at"]) - timedelta(minutes=5)
        orders = self.api.search_orders(self.account_id, start)
        for o in orders:
            if o.get("customTag") == intent["tag"] or (intent.get("broker_order_id") and o.get("id") == intent["broker_order_id"]):
                return o
        return None

    def reconcile(self, intent: dict, fresh: Freshness) -> None:
        st = S(intent["state"])
        if st == S.UNKNOWN:
            self.transition(intent, S.RECONCILING, "looking up order by customTag")
            st = S.RECONCILING
        try:
            o = self.find_order(intent)
        except Exception as e:  # noqa: BLE001
            log.warning("Order/search failed while reconciling %s: %s", intent["tag"], e)
            return
        pos = self.position()
        if o is not None:
            filled = int(o.get("fillVolume") or 0)
            status = o.get("status")
            fields = {"broker_order_id": o["id"], "filled_qty": filled}
            if status == STATUS_FILLED or (pos and filled and filled >= intent["size"]):
                self.transition(intent, S.FILLED, f"found at broker: filled {filled}", **fields)
            elif filled > 0:
                self.transition(intent, S.PARTIALLY_FILLED, f"found at broker: filled {filled}/{intent['size']}", **fields)
            elif status in (STATUS_REJECTED,):
                self.transition(intent, S.REJECTED, "broker shows the order rejected", **fields)
                self.active = None
            elif status in (STATUS_CANCELLED, STATUS_EXPIRED):
                self.transition(intent, S.CANCELLED, "broker shows the order cancelled/expired", **fields)
                self.active = None
            else:
                self.transition(intent, S.ACKNOWLEDGED, "found at broker: working", **fields)
            self.unblock("unknown_order")
            return
        if pos is not None:
            # a position exists but no order carries our tag: the broker state wins, but we can't tie
            # it to this intent with certainty; treat as filled (conservative: it is OUR exposure)
            self.transition(intent, S.FILLED, f"no order found but a {pos['size']}-lot position exists",
                            filled_qty=int(pos["size"]))
            self.store.event("reconcile_position_without_order", intent["tag"])
            self.unblock("unknown_order")
            return
        age = (self.clock() - _ts(intent["updated_at"] if st != S.RECONCILING else intent["created_at"])).total_seconds()
        if age >= self.c.reconcile_grace_s:
            self.transition(intent, S.CANCELLED, f"not found at broker after {age:.0f}s (orders and positions checked)")
            self.store.event("reconcile_absent", intent["tag"])
            self.active = None
            self.unblock("unknown_order")
        # else: keep RECONCILING, entries stay blocked

    # ---------------- per-poll management of the active intent
    def manage(self, fresh: Freshness, expected_stop_fn) -> str | None:
        """Advance the active intent from broker state. Returns 'flatten' if protection failed."""
        it = self.active
        if it is None:
            return None
        st = S(it["state"])
        if st in (S.UNKNOWN, S.RECONCILING):
            self.reconcile(it, fresh)
            return None
        pos = self.position()
        if st == S.ACKNOWLEDGED:
            if pos:
                size = int(pos["size"])
                new = S.FILLED if size >= it["size"] else S.PARTIALLY_FILLED
                self.transition(it, new, f"position {size} @ {pos['averagePrice']}", filled_qty=size)
                self._on_fill(it, pos)
                st = new
            else:
                age = (self.clock() - _ts(it["updated_at"])).total_seconds()
                if age > self.c.reconcile_grace_s:
                    self.transition(it, S.RECONCILING, f"acknowledged {age:.0f}s ago but no position")
                return None
        if st in (S.FILLED, S.PARTIALLY_FILLED, S.PROTECTED):
            if pos is None:
                self.transition(it, S.CLOSED, "position closed (bracket or manual)")
                self.active = None
                self.last_closed = it
                return "closed"
            if int(pos["size"]) != int(it.get("filled_qty") or 0):
                if st == S.PARTIALLY_FILLED and int(pos["size"]) > int(it["filled_qty"] or 0):
                    self.store.set_state(it["tag"], st.value, st.value, f"fill grew to {pos['size']}",
                                         filled_qty=int(pos["size"]))
                    it["filled_qty"] = int(pos["size"])
                elif st == S.PROTECTED or int(pos["size"]) < int(it["filled_qty"] or 0):
                    self.store.event("position_size_changed", f"{it['tag']}: {it['filled_qty']} -> {pos['size']}")
                    it["filled_qty"] = int(pos["size"])
            not_before = _ts(it["created_at"]) - timedelta(seconds=5)
            prot = verify_protection(pos, self.open_orders, self.spec.tick_size, expected_stop_fn(it, pos),
                                     self.c.stop_price_tolerance_ticks, not_before)
            if prot.ok:
                if st != S.PROTECTED:
                    self.transition(it, S.PROTECTED, f"stop {prot.stop_ids} @ {prot.stop_price} x{pos['size']}")
                self.unblock("protection")
                return None
            since = _ts(it["updated_at"])
            age = (self.clock() - since).total_seconds() if since else 0
            if st == S.PROTECTED or age > self.c.protection_grace_s:
                why = "; ".join(prot.problems)
                self.block("protection", f"position not adequately protected: {why}")
                self.store.event("protection_failed", f"{it['tag']}: {why}")
                return "flatten"
            return None  # bracket legs can take a moment to appear
        return None

    def _on_fill(self, it: dict, pos: dict) -> None:
        plan = it.get("plan") or {}
        px = float(pos["averagePrice"])
        sig_px = plan.get("signal_price")
        slip = None
        if sig_px is not None:
            slip = ((px - sig_px) if it["side"] == "long" else (sig_px - px)) / self.spec.tick_size
        sign = 1 if it["side"] == "long" else -1
        # Paper fills are the real last price plus a MODELED slippage; only a broker fill measures real
        # slippage. What paper does measure for real is the market's move between the signal bar's close
        # and the fill (latency drift) = total - modeled.
        modeled = float(getattr(self.api, "slip_ticks", 0)) if self.mode == "paper" else None
        drift = (slip - modeled) if (slip is not None and modeled is not None) else slip
        self.store.forward(it["tag"], fill_time=self.clock().isoformat(), fill_price=px, fill_qty=int(pos["size"]),
                           stop_price=px - sign * plan.get("stop_pts", 0), target_price=px + sign * plan.get("target_pts", 0),
                           entry_slippage_ticks=slip, fill_source="paper-sim" if self.mode == "paper" else "broker",
                           modeled_slippage_ticks=modeled, market_drift_ticks=drift,
                           confirm_contract=getattr(self, "confirm_contract", None))

    # ---------------- unexpected exposure
    def check_unexpected(self) -> str | None:
        """A position with no active intent, or a position on another contract."""
        if self.other_positions():
            self.block("other_contract", f"open position(s) on other contracts: "
                       f"{[(p['contractId'], p['size']) for p in self.other_positions()]}")
        pos = self.position()
        if pos and self.active is None:
            prot = verify_protection(pos, self.open_orders, self.spec.tick_size, None, self.c.stop_price_tolerance_ticks)
            self.block("unexpected_position", f"unexpected {pos['size']}-lot position on {pos['contractId']}")
            if not prot.ok:
                self.store.event("unexpected_unprotected", "; ".join(prot.problems))
                return "flatten"
            self.store.event("unexpected_protected", f"stop {prot.stop_ids} @ {prot.stop_price}")
        elif not pos and "unexpected_position" in self.block_reasons:
            self.store.event("unexpected_position_gone", "")
        return None

    # ---------------- flatten
    def flatten(self, why: str, fresh: Freshness) -> bool:
        """Close the open position (exact size, cannot reverse) and cancel leftover orders on the contract,
        verifying after each step, at most `flatten_attempts` times. Returns True only if confirmed flat."""
        log.warning("FLATTEN: %s", why)
        self.store.event("flatten_start", why)
        if self.active and S(self.active["state"]) in (S.FILLED, S.PARTIALLY_FILLED, S.PROTECTED, S.ACKNOWLEDGED,
                                                       S.EXIT_PENDING):
            self.transition(self.active, S.EXIT_PENDING, why)
        for attempt in range(1, self.c.flatten_attempts + 1):
            if not self.refresh(fresh):
                log.error("Flatten attempt %d: broker state unavailable", attempt)
                self.sleep(2)
                continue
            if self.position():
                try:
                    self.api.close_position(self.account_id, self.spec.contract_id)
                except Exception as e:  # noqa: BLE001
                    log.error("Flatten attempt %d: closeContract failed: %s", attempt, e)
                self.sleep(1)
                if not self.refresh(fresh) or self.position():
                    self.sleep(2)
                    continue
            # flat: now remove leftovers (closing first so the stop protects until the last moment)
            for o in [o for o in self.open_orders if o.get("contractId") == self.spec.contract_id]:
                try:
                    self.api.cancel_order(self.account_id, o["id"])
                except Exception as e:  # noqa: BLE001
                    log.warning("Cancel %s failed: %s", o.get("id"), e)
            self.refresh(fresh)
            left = [o for o in self.open_orders if o.get("contractId") == self.spec.contract_id]
            if self.position() is None and not left:
                if self.active and S(self.active["state"]) == S.EXIT_PENDING:
                    self.transition(self.active, S.CLOSED, f"flattened: {why}")
                    self.last_closed = self.active
                    self.active = None
                self.store.event("flatten_done", why)
                self.unblock("flatten_failed")
                return True
            if self.position() is None and left:
                log.warning("Flat, but %d order(s) still working; retrying cancel", len(left))
        msg = f"could not confirm flat after {self.c.flatten_attempts} attempts ({why}) — CHECK THE ACCOUNT MANUALLY"
        log.critical(msg)
        self.critical.append(msg)
        self.store.event("flatten_failed", msg)
        self.block("flatten_failed", msg)
        return False

    # ---------------- restart recovery
    def recover(self, day: date, fresh: Freshness, expected_stop_fn) -> str | None:
        """Start-up reconciliation. Broker state is authoritative. Returns 'flatten' if an
        open position can't be shown to be protected."""
        self.block("reconciling", "start-up reconciliation in progress")
        if not self.refresh(fresh):
            return None  # stay blocked; caller retries
        opens = self.store.open_intents()
        for it in opens:
            if it["day"] != day.isoformat() and S(it["state"]) in (S.CREATED, S.SUBMITTING, S.UNKNOWN, S.RECONCILING, S.ACKNOWLEDGED):
                pass  # older intents are still reconciled below: broker decides
            self.active = it
            st = S(it["state"])
            if st == S.CREATED:
                self.transition(it, S.CANCELLED, "never sent (process stopped before submission)")
                self.active = None
                continue
            if st == S.SUBMITTING:
                self.transition(it, S.UNKNOWN, "process stopped during submission")
            if S(it["state"]) in (S.UNKNOWN, S.RECONCILING, S.ACKNOWLEDGED):
                if S(it["state"]) == S.ACKNOWLEDGED:
                    self.transition(it, S.RECONCILING, "restart: confirming order state")
                self.reconcile(it, fresh)
                if self.active is None:
                    continue
                if S(it["state"]) == S.ACKNOWLEDGED:   # still working at the broker: a stale entry
                    try:
                        self.api.cancel_order(self.account_id, it["broker_order_id"])
                    except Exception as e:  # noqa: BLE001
                        log.warning("Cancel of pending entry %s failed: %s", it["broker_order_id"], e)
                    self.transition(it, S.RECONCILING, "restart: cancelled stale pending entry; confirming")
                    self.reconcile(it, fresh)
                    if self.active is None:
                        continue
            if S(it["state"]) in (S.FILLED, S.PARTIALLY_FILLED, S.PROTECTED, S.EXIT_PENDING):
                pos = self.position()
                if pos is None:
                    if S(it["state"]) == S.EXIT_PENDING:
                        self.transition(it, S.CLOSED, "restart: position no longer open")
                    else:
                        self.transition(it, S.RECONCILING, "restart: no position")
                        self.transition(it, S.CLOSED, "restart: position closed while the bot was down")
                    self.active = None
                    continue
                it["filled_qty"] = int(pos["size"])
                prot = verify_protection(pos, self.open_orders, self.spec.tick_size, expected_stop_fn(it, pos),
                                         self.c.stop_price_tolerance_ticks)
                if not prot.ok:
                    self.store.event("restart_unprotected", "; ".join(prot.problems))
                    self.unblock("reconciling")
                    return "flatten"
                if S(it["state"]) != S.PROTECTED:
                    self.transition(it, S.RECONCILING, "restart: verifying protection")
                    self.transition(it, S.PROTECTED, f"restart: stop {prot.stop_ids} verified")
        action = self.check_unexpected()
        self.unblock("reconciling")
        self.store.event("reconciled", f"active={self.active['tag'] if self.active else None} "
                                       f"positions={[(p['contractId'], p['size']) for p in self.positions]}")
        return action
