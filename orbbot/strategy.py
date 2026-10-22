"""Opening Range Breakout signal logic. Pure, no I/O — the live bot and the backtester share it.

Feed it completed 1-minute bars in time order. It builds the opening range, aggregates
signal-timeframe bars after the range closes, and emits a Signal when a signal bar
closes beyond the range.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import StrategyConfig

ET = ZoneInfo("America/New_York")


@dataclass
class Bar:
    t: datetime  # bar OPEN time, timezone-aware
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0

    @classmethod
    def from_api(cls, d: dict) -> "Bar":
        return cls(datetime.fromisoformat(d["t"].replace("Z", "+00:00")),
                   float(d["o"]), float(d["h"]), float(d["l"]), float(d["c"]), float(d.get("v", 0)))


@dataclass
class Signal:
    side: str          # "long" | "short"
    time: datetime     # close time of the signal bar
    price: float       # signal bar close
    or_high: float
    or_low: float
    target: float | None = None  # optional take-profit price (level-based targets)


class ORBStrategy:
    def __init__(self, cfg: StrategyConfig, tick_size: float = 0.25):
        self.cfg = cfg
        self.tick = tick_size
        self.session: date | None = None
        self.reset(None)

    def reset(self, session: date | None) -> None:
        self.session = session
        self.or_high: float | None = None
        self.or_low: float | None = None
        self.or_ready = False
        self.day_disabled = False
        self.disabled_reason = ""
        self.sides_taken: set[str] = set()
        self.trades_taken = 0
        self._agg: Bar | None = None
        self._last_t: datetime | None = None

    # times for the current session
    def _at(self, t) -> datetime:
        return datetime.combine(self.session, t, ET)

    @property
    def or_start(self) -> datetime:
        return self._at(self.cfg.or_start)

    @property
    def or_end(self) -> datetime:
        return self.or_start + timedelta(minutes=self.cfg.or_minutes)

    def register_entry(self, side: str) -> None:
        self.sides_taken.add(side)
        self.trades_taken += 1

    def on_bar(self, bar: Bar) -> Signal | None:
        t = bar.t.astimezone(ET)
        if self.session != t.date():
            self.reset(t.date())
        if self._last_t is not None and t <= self._last_t:
            return None  # duplicate / out of order
        self._last_t = t

        if t < self.or_start:
            return None

        # 1) build the opening range
        if t < self.or_end:
            self.or_high = bar.h if self.or_high is None else max(self.or_high, bar.h)
            self.or_low = bar.l if self.or_low is None else min(self.or_low, bar.l)
            return None

        if not self.or_ready:
            self._finalize_range()
        if self.day_disabled:
            return None

        # 2) aggregate signal-timeframe bars aligned to the OR end
        tf = self.cfg.signal_tf_minutes
        mins_since = int((t - self.or_end).total_seconds() // 60)
        bucket_start = self.or_end + timedelta(minutes=(mins_since // tf) * tf)
        if self._agg is None or self._agg.t != bucket_start:
            self._agg = Bar(bucket_start, bar.o, bar.h, bar.l, bar.c, bar.v)
        else:
            a = self._agg
            a.h, a.l, a.c, a.v = max(a.h, bar.h), min(a.l, bar.l), bar.c, a.v + bar.v
        if (mins_since + 1) % tf != 0:
            return None  # signal bar not complete yet

        close_time = bucket_start + timedelta(minutes=tf)
        return self._check_breakout(self._agg, close_time)

    def _finalize_range(self) -> None:
        self.or_ready = True
        if self.or_high is None:
            self.day_disabled, self.disabled_reason = True, "no bars during opening range"
            return
        rng = self.or_high - self.or_low
        if rng < self.cfg.min_range_points:
            self.day_disabled, self.disabled_reason = True, f"range {rng:.2f} < min {self.cfg.min_range_points}"
        elif rng > self.cfg.max_range_points:
            self.day_disabled, self.disabled_reason = True, f"range {rng:.2f} > max {self.cfg.max_range_points}"

    def _check_breakout(self, agg: Bar, close_time: datetime) -> Signal | None:
        c = self.cfg
        if close_time > self._at(c.entry_cutoff) or self.trades_taken >= c.max_trades_per_day:
            return None
        buf = c.breakout_buffer_ticks * self.tick
        side = None
        if c.allow_long and agg.c > self.or_high + buf:
            side = "long"
        elif c.allow_short and agg.c < self.or_low - buf:
            side = "short"
        if side is None or (c.one_trade_per_side and side in self.sides_taken):
            return None
        if c.max_chase_points > 0:
            beyond = agg.c - self.or_high if side == "long" else self.or_low - agg.c
            if beyond > c.max_chase_points:
                return None
        return Signal(side, close_time, agg.c, self.or_high, self.or_low)
