"""Higher-timeframe context built from 1-minute bars, with no lookahead.

Every value is stamped with the moment it became KNOWN (the close of the bar that completes it).
Strategies query "as of time t" and only ever see values known at or before t:

  * 1-hour bars (clock hours, ET) with EMA20 / EMA50 of the closes
  * confirmed 15-minute swing highs / lows: a swing at bar k needs the 2 bars after it, so it is
    known only when bar k+2 closes
  * per session: prior regular-session high / low / close (default 9:30-16:00 ET; 8:20-13:30
    for COMEX gold), and the overnight high / low (18:00 ET the evening before to the open)
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .strategy import ET, Bar


@dataclass
class Swing:
    known_at: datetime
    price: float


@dataclass
class Levels:
    pdh: float | None = None   # prior session high (RTH)
    pdl: float | None = None
    pdc: float | None = None   # prior session close (last bar before 16:00)
    onh: float | None = None   # overnight high
    onl: float | None = None


def _agg(bars: list[Bar], minutes: int) -> list[tuple[datetime, Bar]]:
    """Clock-aligned bars in ET. Returns [(close_time, bar)]."""
    out: list[tuple[datetime, Bar]] = []
    cur, key = None, None
    for b in bars:
        t = b.t.astimezone(ET)
        k = t.replace(minute=(t.minute // minutes) * minutes, second=0, microsecond=0) if minutes < 60 \
            else t.replace(minute=0, second=0, microsecond=0)
        if k != key:
            if cur is not None:
                out.append((key + timedelta(minutes=minutes), cur))
            key, cur = k, Bar(k, b.o, b.h, b.l, b.c, b.v)
        else:
            cur.h, cur.l, cur.c, cur.v = max(cur.h, b.h), min(cur.l, b.l), b.c, cur.v + b.v
    if cur is not None:
        out.append((key + timedelta(minutes=minutes), cur))
    return out


def _agg4h(bars: list[Bar]) -> list[tuple[datetime, Bar]]:
    """4-hour candles in New York time, blocks starting 01:00, 05:00, 09:00, 13:00, 17:00, 21:00."""
    out: list[tuple[datetime, Bar]] = []
    cur, key = None, None
    for b in bars:
        t = b.t.astimezone(ET)
        h = ((t.hour - 1) // 4) * 4 + 1          # 1,5,9,13,17,21 (hour 0 -> previous day's 21)
        k = t.replace(hour=h % 24, minute=0, second=0, microsecond=0)
        if t.hour == 0:
            k = (t - timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
        if k != key:
            if cur is not None:
                out.append((key + timedelta(hours=4), cur))
            key, cur = k, Bar(k, b.o, b.h, b.l, b.c, b.v)
        else:
            cur.h, cur.l, cur.c, cur.v = max(cur.h, b.h), min(cur.l, b.l), b.c, cur.v + b.v
    if cur is not None:
        out.append((key + timedelta(hours=4), cur))
    return out


class Features:
    def __init__(self, bars: list[Bar], open_t: time = time(9, 30), close_t: time = time(16, 0),
                 swing_n: int = 2):
        bars = sorted(bars, key=lambda b: b.t)
        self.open_t, self.close_t = open_t, close_t
        self._hourly(bars)
        self._five_ind(bars)
        self._crt(bars)
        self._zones(bars)
        self._swings(bars, swing_n)
        self._levels(bars)

    # ------------------------------------------------------------- 1-hour EMAs
    def _hourly(self, bars):
        self.h_known: list[datetime] = []
        self.h_vals: list[tuple[float, float | None, float | None]] = []  # close, ema20, ema50
        e20 = e50 = None
        n = 0
        for close_t, b in _agg(bars, 60):
            n += 1
            e20 = b.c if e20 is None else e20 + 2 / 21 * (b.c - e20)
            e50 = b.c if e50 is None else e50 + 2 / 51 * (b.c - e50)
            self.h_known.append(close_t)
            self.h_vals.append((b.c, e20 if n >= 20 else None, e50 if n >= 50 else None))

    def bias(self, t: datetime) -> str | None:
        """'long' if EMA20 > EMA50 and the last completed hour closed above EMA20; 'short' mirror."""
        i = bisect_right(self.h_known, t) - 1
        if i < 0:
            return None
        c, e20, e50 = self.h_vals[i]
        if e20 is None or e50 is None:
            return None
        if e20 > e50 and c > e20:
            return "long"
        if e20 < e50 and c < e20:
            return "short"
        return None

    # ------------------------------------------------------------- 5-min indicators
    def _five_ind(self, bars):
        """Bollinger(20, 2) and MACD(12, 26, 9) on continuous 5-min closes, keyed by bar close time."""
        self.five: dict[datetime, dict] = {}
        closes: list[float] = []
        e12 = e26 = sig = None
        prev = None
        for close_t, b in _agg(bars, 5):
            closes.append(b.c)
            e12 = b.c if e12 is None else e12 + 2 / 13 * (b.c - e12)
            e26 = b.c if e26 is None else e26 + 2 / 27 * (b.c - e26)
            macd = e12 - e26
            sig = macd if sig is None else sig + 2 / 10 * (macd - sig)
            row = {"close": b.c, "macd": macd, "signal": sig, "hist": macd - sig, "mid": None, "up": None, "dn": None}
            if len(closes) >= 20:
                w = closes[-20:]
                m = sum(w) / 20
                sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
                row.update(mid=m, up=m + 2 * sd, dn=m - 2 * sd)
            ready = len(closes) >= 35  # MACD 26 + signal 9 warm-up
            row["prev"] = prev if ready else None
            self.five[close_t] = row if ready else None
            prev = row

    def five_at(self, close_t: datetime) -> dict | None:
        """Indicators for the 5-min bar that closed at close_t (None while warming up)."""
        return self.five.get(close_t)

    # ------------------------------------------------------------- CRT (candle range theory)
    def _crt(self, bars):
        """For 1-hour candles and 4-hour candles (NY blocks starting 1,5,9,13,17,21h): a candle that
        trades beyond exactly one side of the previous candle and closes back inside its range.
        Keyed by the sweep candle's close time: {60|240: {close_time: dict}}."""
        self.crt: dict[int, dict[datetime, dict]] = {}
        for tf, candles in ((60, _agg(bars, 60)), (240, _agg4h(bars))):
            out = {}
            for (pt, p), (ct, c) in zip(candles, candles[1:]):
                if ct - pt != timedelta(minutes=tf):  # not consecutive (weekend / gap)
                    continue
                above, below = c.h > p.h, c.l < p.l
                inside = p.l < c.c < p.h
                if above == below or not inside:     # no sweep, or both sides swept, or closed outside
                    continue
                out[ct] = {"side": "short" if above else "long", "ph": p.h, "pl": p.l,
                           "sh": c.h, "sl": c.l, "sc": c.c}
            self.crt[tf] = out

    def crt_at(self, tf: int, close_t: datetime) -> dict | None:
        return self.crt.get(tf, {}).get(close_t)

    # ------------------------------------------------------------- order-block zones
    def _zones(self, bars, impulse_atr: float = 3.0, impulse_bars: int = 3, atr_n: int = 14):
        """Demand zone: a bearish 1-min candle followed, within `impulse_bars` consecutive minutes,
        by a close at least `impulse_atr` x ATR(14) above its high. Supply zone: the mirror.
        Zone = that candle's low..high, known at the close of the impulse bar."""
        self.zones: list[dict] = []
        atr = None
        trs: list[float] = []
        prev_c = None
        atrs: list[float | None] = []
        for b in bars:
            tr = b.h - b.l if prev_c is None else max(b.h, prev_c) - min(b.l, prev_c)
            prev_c = b.c
            trs.append(tr)
            if atr is None and len(trs) >= atr_n:
                atr = sum(trs[-atr_n:]) / atr_n
            elif atr is not None:
                atr = atr + (tr - atr) / atr_n
            atrs.append(atr)
        n = impulse_bars
        for k in range(atr_n, len(bars) - n):
            a = atrs[k]
            base, end = bars[k], bars[k + n]
            if a is None or a <= 0 or end.t - base.t != timedelta(minutes=n):
                continue
            known = end.t + timedelta(minutes=1)
            z = None
            if base.c < base.o and end.c - base.h >= impulse_atr * a:
                z = {"kind": "demand", "lo": base.l, "hi": base.h, "known_at": known}
            elif base.c > base.o and base.l - end.c >= impulse_atr * a:
                z = {"kind": "supply", "lo": base.l, "hi": base.h, "known_at": known}
            if z is None:
                continue
            # when (if ever, that same ET day) a bar first closes through the zone's far side
            day = known.astimezone(ET).date()
            z["dead_at"] = None
            for j in range(k + n + 1, len(bars)):
                bj = bars[j]
                if bj.t.astimezone(ET).date() != day:
                    break
                if (bj.c < z["lo"]) if z["kind"] == "demand" else (bj.c > z["hi"]):
                    z["dead_at"] = bj.t + timedelta(minutes=1)  # known at that bar's close
                    break
            self.zones.append(z)
        self.zone_known = [z["known_at"] for z in self.zones]

    def zones_between(self, start: datetime, end: datetime) -> list[dict]:
        """Zones that became known in (start, end]."""
        i, j = bisect_right(self.zone_known, start), bisect_right(self.zone_known, end)
        return self.zones[i:j]

    # ------------------------------------------------------------- 15-min swings
    def _swings(self, bars, n):
        q = _agg(bars, 15)
        self.sh: list[Swing] = []
        self.sl: list[Swing] = []
        for k in range(n, len(q) - n):
            hi, lo = q[k][1].h, q[k][1].l
            around = q[k - n:k] + q[k + 1:k + n + 1]
            known = q[k + n][0]  # close of the last bar needed to confirm it
            if all(hi > x[1].h for x in around):
                self.sh.append(Swing(known, hi))
            if all(lo < x[1].l for x in around):
                self.sl.append(Swing(known, lo))
        self.sh_known = [s.known_at for s in self.sh]
        self.sl_known = [s.known_at for s in self.sl]

    def swings(self, t: datetime, k: int = 2) -> tuple[list[float], list[float]]:
        """Last k confirmed swing highs and lows known at t (oldest first)."""
        i = bisect_right(self.sh_known, t)
        j = bisect_right(self.sl_known, t)
        return [s.price for s in self.sh[max(0, i - k):i]], [s.price for s in self.sl[max(0, j - k):j]]

    def structure(self, t: datetime) -> str | None:
        """'up' = higher highs and higher lows; 'down' = lower highs and lower lows."""
        hs, ls = self.swings(t, 2)
        if len(hs) < 2 or len(ls) < 2:
            return None
        if hs[1] > hs[0] and ls[1] > ls[0]:
            return "up"
        if hs[1] < hs[0] and ls[1] < ls[0]:
            return "down"
        return None

    # ------------------------------------------------------------- session levels
    def _levels(self, bars):
        rth: dict[date, list[Bar]] = {}
        onx: dict[date, list[Bar]] = {}
        for b in bars:
            t = b.t.astimezone(ET)
            tt = t.time()
            if self.open_t <= tt < self.close_t:
                rth.setdefault(t.date(), []).append(b)
            elif tt >= time(18, 0):
                onx.setdefault(t.date() + timedelta(days=1), []).append(b)  # belongs to next morning
            elif tt < self.open_t:
                onx.setdefault(t.date(), []).append(b)
        # weekend: Sunday-evening bars are keyed to Monday already; Friday-evening has none
        self.levels: dict[date, Levels] = {}
        prev = None
        for d in sorted(set(rth) | set(onx)):
            lv = Levels()
            if prev is not None:
                pb = rth[prev]
                lv.pdh, lv.pdl, lv.pdc = max(b.h for b in pb), min(b.l for b in pb), pb[-1].c
            if d in onx and onx[d]:
                lv.onh, lv.onl = max(b.h for b in onx[d]), min(b.l for b in onx[d])
            self.levels[d] = lv
            if d in rth and rth[d]:
                prev = d

    def day_levels(self, d: date) -> Levels:
        return self.levels.get(d, Levels())


def has_overnight(bars: list[Bar]) -> bool:
    return any(b.t.astimezone(ET).time() < time(8, 0) for b in bars[:5000])
