"""Position sizing, stop/target planning, and the daily loss/profit guard."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import RiskConfig
from .strategy import Signal

CT = ZoneInfo("America/Chicago")


@dataclass
class ContractSpec:
    contract_id: str
    tick_size: float = 0.25   # MNQ
    tick_value: float = 0.50  # MNQ: $0.50/tick = $2/point

    @property
    def point_value(self) -> float:
        return self.tick_value / self.tick_size


@dataclass
class TradePlan:
    side: str
    contracts: int
    entry_ref: float
    stop_points: float
    target_points: float
    stop_ticks: int      # unsigned distance
    target_ticks: int    # unsigned distance
    risk_dollars: float
    reward_dollars: float
    note: str = ""

    def bracket_ticks(self) -> tuple[int, int]:
        """Signed bracket ticks as ProjectX expects: for a long the stop is negative and the
        target positive; for a short, the reverse."""
        if self.side == "long":
            return -self.stop_ticks, self.target_ticks
        return self.stop_ticks, -self.target_ticks


def plan_trade(sig: Signal, cfg: RiskConfig, spec: ContractSpec, realized_pnl: float,
               contracts: int | None = None) -> TradePlan | None:
    """Return a plan that respects per-trade risk, remaining daily-loss headroom and the
    daily profit cap, or None if no acceptable trade fits."""
    tick, pv = spec.tick_size, spec.point_value
    n = min(contracts or cfg.contracts, cfg.max_contracts)
    notes = []

    # structural stop distance
    if cfg.stop_mode == "dollar":
        structural = cfg.max_risk_per_trade / (n * pv)  # exactly max_risk_per_trade at full size
    elif cfg.stop_mode == "opposite":
        structural = sig.price - sig.or_low if sig.side == "long" else sig.or_high - sig.price
    elif cfg.stop_mode == "mid":
        mid = (sig.or_high + sig.or_low) / 2
        structural = sig.price - mid if sig.side == "long" else mid - sig.price
    else:
        structural = cfg.fixed_stop_points

    # cap the stop so the full size risks no more than max_risk_per_trade
    cap = cfg.max_risk_per_trade / (n * pv)
    stop = min(structural, cap)
    if stop < structural:
        notes.append(f"stop capped {structural:.2f}->{stop:.2f}pts")
    stop_ticks = max(cfg.min_stop_ticks, math.floor(stop / tick + 1e-9))
    stop = stop_ticks * tick

    # remaining daily-loss headroom (realized is negative when losing)
    headroom = cfg.max_daily_loss + realized_pnl - cfg.loss_buffer
    per_contract_risk = stop * pv + cfg.fee_per_contract_rt
    by_trade = math.floor((cfg.max_risk_per_trade + 1e-9) / (stop * pv))
    by_day = math.floor(headroom / per_contract_risk) if headroom > 0 else 0
    sized = min(n, by_trade, by_day)
    if sized < n:
        notes.append(f"size reduced {n}->{sized} (headroom ${headroom:.0f})")
    if sized < cfg.min_contracts:
        return None

    # target: R multiple, trimmed so a win doesn't overshoot the daily profit cap
    rr = cfg.target_per_trade / cfg.max_risk_per_trade if cfg.target_per_trade > 0 else cfg.reward_risk
    target = stop * rr
    room = cfg.max_daily_profit - realized_pnl + cfg.fee_per_contract_rt * sized
    max_target = room / (sized * pv)
    if max_target < target:
        target = max_target
        notes.append("target trimmed to daily profit cap")
    target_ticks = max(cfg.min_stop_ticks, math.ceil(target / tick - 1e-9))
    target = target_ticks * tick

    return TradePlan(sig.side, sized, sig.price, stop, target, stop_ticks, target_ticks,
                     risk_dollars=stop * pv * sized, reward_dollars=target * pv * sized,
                     note="; ".join(notes))


def trading_day_start(now: datetime) -> datetime:
    """CME/Topstep trading day begins 5:00pm Central the previous calendar day."""
    c = now.astimezone(CT)
    start = c.replace(hour=17, minute=0, second=0, microsecond=0)
    if c < start:
        start -= timedelta(days=1)
    return start


def realized_from_trades(trades: list[dict], include_fees: bool = True) -> float:
    pnl = 0.0
    for t in trades:
        if t.get("voided"):
            continue
        if t.get("profitAndLoss") is not None:  # None = opening half-turn
            pnl += float(t["profitAndLoss"])
        if include_fees:
            pnl -= float(t.get("fees") or 0)
    return pnl


def unrealized(position: dict | None, last_price: float, spec: ContractSpec) -> float:
    if not position or not position.get("size"):
        return 0.0
    sign = 1 if position["type"] == 1 else -1
    return (last_price - float(position["averagePrice"])) * sign * position["size"] * spec.point_value


class DailyGuard:
    """Tracks the day's P&L and decides when to halt.

    total = realized (incl. fees) + open P&L. Hitting -max_daily_loss or +max_daily_profit
    flattens and halts new entries for the rest of the day.
    """

    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.halted = False
        self.reason = ""

    def check(self, realized: float, open_pnl: float) -> str | None:
        if self.halted:
            return None
        total = realized + open_pnl
        if total <= -self.cfg.max_daily_loss:
            self.halted, self.reason = True, f"daily loss limit hit (${total:.2f})"
            return "loss"
        if total >= self.cfg.max_daily_profit:
            self.halted, self.reason = True, f"daily profit target hit (${total:.2f})"
            return "profit"
        return None
