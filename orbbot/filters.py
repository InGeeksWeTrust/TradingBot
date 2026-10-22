"""Trade filters that use information beyond the instrument's own price bars.

All values are known only from their decision time onward; a filter that isn't known yet at the
signal time blocks the trade.

  rvol  "in play": volume in the first 5 minutes of the session is at least 1.5x the average of
        the same window over the prior 20 sessions (needs >= 10 prior sessions). Known at open+5.
  news  skip release days: the 8:30-8:45 ET range is more than 2.5x its median over the prior
        20 sessions (needs >= 10). Known at 8:45; trades before 8:45 are blocked.
  es    intermarket confirmation: long only if ES's last completed 1-min close is above ES's
        session VWAP, short only if below.
"""
from __future__ import annotations

import statistics
from bisect import bisect_right
from datetime import date, datetime, time, timedelta

from .strategy import ET, Bar

RVOL_MIN = 1.5
NEWS_MAX = 2.5
LOOKBACK = 20
MIN_HISTORY = 10
NEWS_START, NEWS_END = time(8, 30), time(8, 45)


def _plus(t: time, minutes: int) -> time:
    return (datetime.combine(date(2000, 1, 3), t) + timedelta(minutes=minutes)).time()


class DayContext:
    def __init__(self, bars: list[Bar], open_t: time, rvol_minutes: int = 5):
        self.open_t = open_t
        self.rvol_known = _plus(open_t, rvol_minutes)
        vol: dict[date, float] = {}
        hi: dict[date, float] = {}
        lo: dict[date, float] = {}
        for b in bars:
            t = b.t.astimezone(ET)
            d, tt = t.date(), t.time()
            if open_t <= tt < self.rvol_known:
                vol[d] = vol.get(d, 0.0) + (b.v or 0.0)
            if NEWS_START <= tt < NEWS_END:
                hi[d] = max(hi.get(d, b.h), b.h)
                lo[d] = min(lo.get(d, b.l), b.l)
        self.rvol: dict[date, float | None] = {}
        days = sorted(vol)
        for i, d in enumerate(days):
            prior = [vol[x] for x in days[max(0, i - LOOKBACK):i] if vol[x] > 0]
            self.rvol[d] = vol[d] / statistics.mean(prior) if len(prior) >= MIN_HISTORY else None
        self.news: dict[date, float | None] = {}
        rdays = sorted(hi)
        ranges = {d: hi[d] - lo[d] for d in rdays}
        for i, d in enumerate(rdays):
            prior = [ranges[x] for x in rdays[max(0, i - LOOKBACK):i] if ranges[x] > 0]
            self.news[d] = ranges[d] / statistics.median(prior) if len(prior) >= MIN_HISTORY else None

    def check_rvol(self, t: datetime) -> bool:
        e = t.astimezone(ET)
        r = self.rvol.get(e.date())
        return e.time() >= self.rvol_known and r is not None and r >= RVOL_MIN

    def check_news(self, t: datetime) -> bool:
        e = t.astimezone(ET)
        r = self.news.get(e.date())
        return e.time() >= NEWS_END and r is not None and r <= NEWS_MAX


class ESConfirm:
    """Side of ES's session VWAP at any moment, from ES 1-minute bars."""

    def __init__(self, es_bars: list[Bar], open_t: time, close_t: time):
        self.by_day: dict[date, tuple[list[datetime], list[tuple[float, float]]]] = {}
        sv: dict[date, float] = {}
        spv: dict[date, float] = {}
        for b in sorted(es_bars, key=lambda x: x.t):
            t = b.t.astimezone(ET)
            d = t.date()
            if not (open_t <= t.time() < close_t):
                continue
            v = b.v or 1.0
            sv[d] = sv.get(d, 0.0) + v
            spv[d] = spv.get(d, 0.0) + v * (b.h + b.l + b.c) / 3
            ts, vals = self.by_day.setdefault(d, ([], []))
            ts.append(b.t + timedelta(minutes=1))  # known at the bar's close
            vals.append((b.c, spv[d] / sv[d]))

    def check(self, side: str, t: datetime) -> bool:
        d = t.astimezone(ET).date()
        if d not in self.by_day:
            return False
        ts, vals = self.by_day[d]
        i = bisect_right(ts, t) - 1
        if i < 0:
            return False
        close, vwap = vals[i]
        return close > vwap if side == "long" else close < vwap


class Filtered:
    """Wraps an entry rule; a signal only goes through if every check passes at its time."""

    def __init__(self, inner, checks):
        self.inner, self.checks = inner, checks

    def reset(self, day):
        self.inner.reset(day)

    def register_entry(self, side):
        self.inner.register_entry(side)

    def on_bar(self, b: Bar):
        sig = self.inner.on_bar(b)
        if sig and all(c(sig) for c in self.checks):
            return sig
        return None
