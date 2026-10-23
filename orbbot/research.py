"""Compare entry ideas fairly: tune on older data, check on newer data, and beat random entries.

    python -m orbbot.research --csv data/mnq_databento.csv
    python -m orbbot.research --csv data/mnq_databento.csv --test-months 4 --strategies orb fade

How to read it
  * TRAIN = the older months. Pick variants by their TRAIN results only.
  * TEST  = the last --test-months months, never used for choosing. A variant is only
    interesting if it is still good here.
  * p(random) = how often random long/short entries at the same moments, with the same
    bracket, did at least as well. Small (< 0.02 by default) means the entry rule adds something;
    around 0.5 means it's no better than a coin flip.
  * Max DD vs the $2,000 Topstep 50K limit is shown because a profitable variant that
    draws down more than that would still fail the combine.

Every variant runs through the same engine as orbbot.backtest (same fills, 1-tick slippage,
fees, $500 / $1,250 daily limits), risking $250 per trade.

Data: mtf, sweep and gapfade need all 23 hours (1-hour trend, overnight levels). Rebuild the CSV
for free from your Databento download:
    python -m orbbot.databento_fetch --from-cache data/MNQ_v_0_<dates>.dbn.zst --all-hours --out data/mnq_all.csv

Final check on data never used for choosing (e.g. an extra year):
    python -m orbbot.research --csv data/mnq_2025.csv --holdout --only "mtf day struct 2.2R 10ct"

Filters (each idea is also run with each filter; all at 2:1 by default)
  rvol       only trade when the first 5 minutes' volume is >= 1.5x the prior-20-session average
  news       skip days whose 8:30-8:45 ET range is > 2.5x its prior-20-session median (data releases)
  es         longs only with ES above its session VWAP, shorts only below (needs --confirm-csv)
  all        every available filter together

Entry ideas (default run: orb, mtf, sweep, vwapband, gapfade)
  mtf        top-down trend: 1-hour EMA20 > EMA50 with the last hour closing above EMA20 (long
             bias), 15-min higher highs AND higher lows (2-bar swing points), price pulled back
             between the latest 15-min swing low and high, then a 5-min close above the prior
             5-min high -> long. Mirror for short. Tested mornings (am) and most of the day
             (day), with a structural stop (under the 15-min swing low, fewer contracts so
             risk stays $250) and with a plain $250 stop.
  sweep      price runs through the prior session's high/low or the overnight high/low, then a
             5-min bar closes back on the other side -> reverse; stop beyond the sweep.
  vwapband   5-min close outside VWAP +/- 2 standard deviations, then a 5-min close back inside
             and beyond the prior bar -> trade back toward VWAP; stop beyond the extreme.
  gapfade    opens 0.15%-1.0% away from the prior 16:00 close and still gapped at 9:35 ->
             trade toward the prior close.
  bbrev      (MACD/Bollinger/VWAP, reversion) a 5-min close outside Bollinger(20,2) on the far side
             of VWAP, then the next 5-min bar closes back toward VWAP with the MACD(12/26/9)
             histogram shrinking -> fade; stop beyond the two bars' extreme.
  bbmom      (MACD/Bollinger/VWAP, momentum) MACD crosses its signal line with price on that
             side of VWAP and beyond the Bollinger middle band -> go with it; $250 stop.
  crt1h_mid / crt1h_range / crt4h_mid / crt4h_range
             Candle Range Theory: previous 1-hour (or 4-hour NY-block) candle = range; the next
             candle sweeps exactly one side and closes back inside -> trade back in at its close;
             stop beyond the sweep wick (sized to $250); target the 50% midpoint or the far side.
             Not in the default run: --strategies crt1h_mid crt1h_range crt4h_mid crt4h_range
  zone4r / zone_runner
             order-block retest: zone = last opposite-color 1-min candle before a 3x-ATR move within
             3 bars; price returns and a 1-min candle closes back out of the zone in the trade
             direction -> enter; stop beyond zone/wick ($250). zone4r: all out at 4R. zone_runner:
             half at 4R, stop to breakeven, rest to 8R.
             Not in the default run: --strategies zone4r zone_runner
  orb        the current bot: 15-min range, 5-min close outside it, up to 2 trades/day
  orb_first  first breakout only, before 10:30, and only in the direction the 15-min range
             itself moved (open of 9:30 bar -> close of 9:44 bar)
  fade       failed breakout: a 5-min close outside the range that is followed, within 15
             minutes, by a 5-min close back inside it -> trade the other way
  pullback   trend pullback with your indicators: EMA13 > EMA50 and price above VWAP, the bar
             dips to the EMA13 then closes above the prior bar's high -> long (mirror for
             short), 10:00-11:00 ET. EMAs are on 1-minute bars seeded at 9:30.
"""
from __future__ import annotations

import argparse
import dataclasses
import random
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .backtest import load_csv, simulate_day
from .config import BotConfig, load_config
from .features import Features, has_overnight
from .filters import DayContext, ESConfirm, Filtered
from .risk import RANGE_FILTER, ContractSpec, spec_for
from .strategy import ET, Bar, ORBStrategy, Signal

COMBINE_DD = 2000.0
CONFIRM_NAME = "es"   # how the confirmation filter is labelled (set by --confirm-name)


@dataclasses.dataclass(frozen=True)
class Session:
    """Trading-session clock. Strategy times are offsets from `open`, so the same rules work
    for Nasdaq (9:30 open) and COMEX gold (8:20 open)."""
    name: str
    open: time
    close: time                       # end of the regular session (levels, VWAP)
    windows: dict                     # window name -> (last entry, flatten)

    def at(self, minutes: int) -> time:
        return (datetime.combine(date(2000, 1, 3), self.open) + timedelta(minutes=minutes)).time()


NASDAQ = Session("nasdaq", time(9, 30), time(16, 0),
                 {"am": (time(11, 30), time(11, 55)), "day": (time(15, 0), time(15, 55))})
COMEX = Session("comex", time(8, 20), time(13, 30), {"am": (time(10, 30), time(11, 0))})
SESSIONS = {"nasdaq": NASDAQ, "comex": COMEX}
SESSION_FOR_SYMBOL = {"MGC": "comex", "GC": "comex"}


# --------------------------------------------------------------------------- entry ideas
def _sig(side: str, b: Bar, hi: float | None = None, lo: float | None = None) -> Signal:
    return Signal(side, b.t + timedelta(minutes=1), b.c, hi if hi is not None else b.c,
                  lo if lo is not None else b.c)


class ORB(ORBStrategy):
    name = "orb"


class ORBFirst:
    """First breakout only, before cutoff, in the direction the opening range moved."""
    name = "orb_first"

    def __init__(self, cfg, tick, session=NASDAQ):
        self.s = session
        sc = dataclasses.replace(cfg, max_trades_per_day=1, entry_cutoff=session.at(60))
        self.inner = ORBStrategy(sc, tick)

    def reset(self, day):
        self.inner.reset(day)
        self.day = day
        self.or_open = self.or_close = None

    def register_entry(self, side):
        self.inner.register_entry(side)

    def on_bar(self, b: Bar):
        t = b.t.astimezone(ET)
        if t.date() != self.day:
            self.reset(t.date())
        if self.s.open <= t.time() < self.s.at(15):
            if self.or_open is None:
                self.or_open = b.o
            self.or_close = b.c
        sig = self.inner.on_bar(b)
        if not sig or self.or_open is None:
            return None
        rng = sig.or_high - sig.or_low
        move = self.or_close - self.or_open
        direction = "long" if move > 0 else "short"
        if abs(move) < 0.1 * rng or direction != sig.side:
            self.inner.register_entry(sig.side)  # first breakout went the wrong way: done for the day
            return None
        return sig


class Fade:
    """Failed breakout: 5-min close outside the range, then back inside within 3 bars."""
    name = "fade"

    def __init__(self, cfg, tick):
        self.cfg = cfg
        self.orb = ORBStrategy(cfg, tick)  # used only for the range and its filters

    def reset(self, day):
        self.orb.reset(day)
        self.agg = None
        self.armed = None
        self.count = 0
        self.done = False

    def register_entry(self, side):
        self.done = True

    def on_bar(self, b: Bar):
        t = b.t.astimezone(ET)
        if self.orb.session != t.date():
            self.reset(t.date())
        self.orb.on_bar(b)  # builds range, applies min/max range filters
        o = self.orb
        if not o.or_ready or o.day_disabled or self.done or t < o.or_end:
            return None
        mins = int((t - o.or_end).total_seconds() // 60)
        start = o.or_end + timedelta(minutes=(mins // 5) * 5)
        if self.agg is None or self.agg.t != start:
            self.agg = Bar(start, b.o, b.h, b.l, b.c)
        else:
            self.agg.h, self.agg.l, self.agg.c = max(self.agg.h, b.h), min(self.agg.l, b.l), b.c
        if (mins + 1) % 5:
            return None
        close_t = start + timedelta(minutes=5)
        if close_t > datetime.combine(t.date(), self.cfg.entry_cutoff, ET):
            return None
        c = self.agg.c
        if self.armed:
            self.count += 1
            if o.or_low < c < o.or_high:
                side = "short" if self.armed == "up" else "long"
                self.armed = None
                return _sig(side, b, o.or_high, o.or_low)
            if self.count >= 3:
                self.armed = None  # breakout held: no fade
        if not self.armed:
            if c > o.or_high:
                self.armed, self.count = "up", 0
            elif c < o.or_low:
                self.armed, self.count = "down", 0
        return None


class Pullback:
    """Trend pullback to EMA13 with VWAP and EMA13/50 trend filter, 10:00-11:00 ET."""
    name = "pullback"

    def __init__(self, cfg, tick, session=NASDAQ):
        self.cfg, self.s = cfg, session

    def reset(self, day):
        self.day = day
        self.e13 = self.e50 = None
        self.pv = self.vol = 0.0
        self.prev: Bar | None = None
        self.sides: set[str] = set()
        self.n = 0

    def register_entry(self, side):
        self.sides.add(side)
        self.n += 1

    def on_bar(self, b: Bar):
        t = b.t.astimezone(ET)
        if t.date() != self.day:
            self.reset(t.date())
        if t.time() < self.s.open:
            return None
        a13, a50 = 2 / 14, 2 / 51
        self.e13 = b.c if self.e13 is None else self.e13 + a13 * (b.c - self.e13)
        self.e50 = b.c if self.e50 is None else self.e50 + a50 * (b.c - self.e50)
        v = b.v or 1.0
        self.pv += (b.h + b.l + b.c) / 3 * v
        self.vol += v
        vwap = self.pv / self.vol
        prev, self.prev = self.prev, b
        if prev is None or not (self.s.at(29) <= t.time() < self.s.at(89)) or self.n >= 2:
            return None
        if (self.e13 > self.e50 and b.c > vwap and b.l <= self.e13 and b.c > prev.h
                and b.c > self.e13 and "long" not in self.sides):
            return _sig("long", b)
        if (self.e13 < self.e50 and b.c < vwap and b.h >= self.e13 and b.c < prev.l
                and b.c < self.e13 and "short" not in self.sides):
            return _sig("short", b)
        return None


# ------------------------------------------------- multi-timeframe and reversal ideas
class _Five:
    """Clock-aligned 5-minute bars built from 1-minute bars (ET)."""

    def _five_reset(self):
        self._agg = None
        self.prev5: Bar | None = None

    def _five(self, b: Bar):
        """Feed a 1-min bar; returns (completed 5-min bar, its close time) or None.
        After a completion, self.prev5 still points at the bar before it until the next call."""
        t = b.t.astimezone(ET)
        k = t.replace(minute=(t.minute // 5) * 5, second=0, microsecond=0)
        if self._agg is None or self._agg.t != k:
            self._agg = Bar(k, b.o, b.h, b.l, b.c, b.v)
        else:
            a = self._agg
            a.h, a.l, a.c, a.v = max(a.h, b.h), min(a.l, b.l), b.c, a.v + b.v
        if (t.minute + 1) % 5:
            return None
        return self._agg, k + timedelta(minutes=5)


class MTF(_Five):
    """1-hour EMA20/50 bias + 15-min higher highs/lows (or lower) + 5-min pullback entry."""
    name = "mtf"

    def __init__(self, cfg, tick, feats, window="am", session=NASDAQ, max_trades=2, reentry=False, **_):
        self.tick, self.f, self.s = tick, feats, session
        self.last_entry = session.windows[window][0]
        self.max_n, self.reentry = max_trades, reentry

    def reset(self, day):
        self._five_reset()
        self.day, self.n = day, 0

    def register_entry(self, side):
        self.n += 1

    def on_bar(self, b: Bar):
        t = b.t.astimezone(ET)
        if t.date() != self.day:
            self.reset(t.date())
        done = self._five(b)
        if not done:
            return None
        c5, ct = done
        prev, self.prev5 = self.prev5, Bar(c5.t, c5.o, c5.h, c5.l, c5.c, c5.v)
        if prev is None or self.n >= self.max_n or not (self.s.at(15) <= ct.time() <= self.last_entry):
            return None
        bias, st = self.f.bias(ct), self.f.structure(ct)
        hs, ls = self.f.swings(ct, 1)
        if not hs or not ls:
            return None
        if bias == "long" and st == "up" and ls[-1] < c5.c < hs[-1] and c5.c > prev.h:
            return Signal("long", ct, c5.c, hs[-1], ls[-1] - self.tick)
        if bias == "short" and st == "down" and ls[-1] < c5.c < hs[-1] and c5.c < prev.l:
            return Signal("short", ct, c5.c, hs[-1] + self.tick, ls[-1])
        return None


class Sweep(_Five):
    """Price runs through prior-day / overnight high or low, then a 5-min bar closes back
    on the other side -> reverse. Stop beyond the sweep's extreme.

    `extra` adds levels that form during the session itself:
      "or"    - opening-range high/low (first `or_minutes`), live once the range is complete
      "swing" - a 5-min swing high/low (2 bars each side) that has held, untouched, for at
                least `swing_age` minutes. A swing that gets taken out without a reclaim is gone.
    """
    name = "sweep"

    def __init__(self, cfg, tick, feats, window="am", session=NASDAQ, max_trades=2, reentry=False,
                 extra=(), or_minutes=15, swing_age=60, **_):
        self.tick, self.f, self.s = tick, feats, session
        # The configured entry_cutoff is authoritative (backtest, paper and live all pass the same
        # StrategyConfig). Before 2026-10-22 this used the session window (11:30 ET) and ignored it.
        self.last_entry = getattr(cfg, "entry_cutoff", None) or session.windows[window][0]
        self.max_n, self.reentry = max_trades, reentry
        self.extra = set(extra)
        assert self.extra <= {"or", "swing"}, f"unknown sweep levels: {self.extra}"
        self.or_minutes, self.swing_age = or_minutes, timedelta(minutes=swing_age)

    def reset(self, day):
        self._five_reset()
        self.day, self.n, self.used = day, 0, set()
        lv = self.f.day_levels(day)
        self.highs = [("pdh", lv.pdh), ("onh", lv.onh)]
        self.lows = [("pdl", lv.pdl), ("onl", lv.onl)]
        self.open_dt = datetime.combine(day, self.s.open, ET)
        self.or_end = self.open_dt + timedelta(minutes=self.or_minutes)
        self.or_hi = self.or_lo = None
        self.sess5: list[Bar] = []                 # completed 5-min bars since the open
        self.swing_hi: list[tuple] = []            # (name, price, formed_at) still untouched
        self.swing_lo: list[tuple] = []

    def register_entry(self, side):
        self.n += 1

    def _session_levels(self, c5):
        """Intraday levels allowed to be swept by the 5-min bar `c5` (all formed before it)."""
        hi, lo = [], []
        if "or" in self.extra and self.or_hi is not None and c5.t >= self.or_end:
            hi.append(("orh", self.or_hi))
            lo.append(("orl", self.or_lo))
        if "swing" in self.extra:
            hi += [(n, p) for n, p, at in self.swing_hi if c5.t - at >= self.swing_age]
            lo += [(n, p) for n, p, at in self.swing_lo if c5.t - at >= self.swing_age]
        return hi, lo

    def _track_swings(self, c5):
        """After a bar is checked: drop swings it took out, then confirm a new pivot two bars back."""
        self.swing_hi = [s for s in self.swing_hi if c5.h <= s[1]]
        self.swing_lo = [s for s in self.swing_lo if c5.l >= s[1]]
        self.sess5.append(c5)
        if len(self.sess5) < 5:
            return
        w = self.sess5[-5:]
        p = w[2]
        formed = p.t + timedelta(minutes=5)
        tag = formed.strftime("%H%M")
        if p.h > max(w[0].h, w[1].h) and p.h >= max(w[3].h, w[4].h):
            self.swing_hi.append((f"sh{tag}", p.h, formed))
        if p.l < min(w[0].l, w[1].l) and p.l <= min(w[3].l, w[4].l):
            self.swing_lo.append((f"sl{tag}", p.l, formed))

    def on_bar(self, b: Bar):
        t = b.t.astimezone(ET)
        if t.date() != self.day:
            self.reset(t.date())
        if self.open_dt <= t < self.or_end:
            self.or_hi = b.h if self.or_hi is None else max(self.or_hi, b.h)
            self.or_lo = b.l if self.or_lo is None else min(self.or_lo, b.l)
        done = self._five(b)
        if not done:
            return None
        c5, ct = done
        c5 = Bar(c5.t, c5.o, c5.h, c5.l, c5.c, c5.v)  # snapshot, the aggregator reuses its object
        sig = self._check(c5, ct)
        if c5.t >= self.open_dt and "swing" in self.extra:
            self._track_swings(c5)
        return sig

    def _check(self, c5, ct):
        if self.n >= self.max_n or not (self.s.at(5) <= ct.time() <= self.last_entry):
            return None
        s_hi, s_lo = self._session_levels(c5) if self.extra else ([], [])
        for name, lvl in self.highs + s_hi:
            if lvl is not None and (self.reentry or name not in self.used) and c5.h > lvl and c5.c < lvl:
                self.used.add(name)
                return Signal("short", ct, c5.c, c5.h + self.tick, c5.c)
        for name, lvl in self.lows + s_lo:
            if lvl is not None and (self.reentry or name not in self.used) and c5.l < lvl and c5.c > lvl:
                self.used.add(name)
                return Signal("long", ct, c5.c, c5.c, c5.l - self.tick)
        return None


class VWAPBand(_Five):
    """Session VWAP with 2-standard-deviation bands. A 5-min close outside a band followed by
    a 5-min close back inside and beyond the prior bar's low/high -> trade back toward VWAP."""
    name = "vwapband"

    def __init__(self, cfg, tick, feats=None, window="day", session=NASDAQ, max_trades=2, reentry=False, **_):
        self.tick, self.s = tick, session
        self.last_entry = session.windows[window][0]
        self.max_n, self.reentry = max_trades, reentry

    def reset(self, day):
        self._five_reset()
        self.day, self.n, self.sides = day, 0, set()
        self.sv = self.spv = self.spv2 = 0.0
        self.prev_state = None  # 'above' / 'below' / None for the previous 5-min close

    def register_entry(self, side):
        self.n += 1
        self.sides.add(side)

    def on_bar(self, b: Bar):
        t = b.t.astimezone(ET)
        if t.date() != self.day:
            self.reset(t.date())
        if not (self.s.open <= t.time() < self.s.close):
            return None
        v = b.v or 1.0
        tp = (b.h + b.l + b.c) / 3
        self.sv += v
        self.spv += v * tp
        self.spv2 += v * tp * tp
        vwap = self.spv / self.sv
        sd = max(self.spv2 / self.sv - vwap * vwap, 0.0) ** 0.5
        up, dn = vwap + 2 * sd, vwap - 2 * sd
        done = self._five(b)
        if not done:
            return None
        c5, ct = done
        prev, self.prev5 = self.prev5, Bar(c5.t, c5.o, c5.h, c5.l, c5.c, c5.v)
        state, self.prev_state = self.prev_state, ("above" if c5.c > up else "below" if c5.c < dn else None)
        if prev is None or sd == 0 or self.n >= self.max_n or not (self.s.at(30) <= ct.time() <= self.last_entry):
            return None
        if state == "above" and c5.c < up and c5.c < prev.l and (self.reentry or "short" not in self.sides):
            return Signal("short", ct, c5.c, max(prev.h, c5.h) + self.tick, c5.c)
        if state == "below" and c5.c > dn and c5.c > prev.h and (self.reentry or "long" not in self.sides):
            return Signal("long", ct, c5.c, c5.c, min(prev.l, c5.l) - self.tick)
        return None


class GapFade(_Five):
    """If the session opens 0.15%-1.0% away from the prior session's close and is still gapped
    5 minutes after the open, trade toward the prior close."""
    name = "gapfade"

    def __init__(self, cfg, tick, feats, session=NASDAQ, **_):
        self.tick, self.f, self.s = tick, feats, session

    def reset(self, day):
        self._five_reset()
        self.day, self.done = day, False
        self.pdc = self.f.day_levels(day).pdc

    def register_entry(self, side):
        self.done = True

    def on_bar(self, b: Bar):
        t = b.t.astimezone(ET)
        if t.date() != self.day:
            self.reset(t.date())
        done = self._five(b)
        if not done or self.done or self.pdc is None:
            return None
        c5, ct = done
        if ct.time() != self.s.at(5):
            return None
        self.done = True
        gap = (c5.o - self.pdc) / self.pdc
        if not (0.0015 <= abs(gap) <= 0.01):
            return None
        if gap > 0 and c5.c > self.pdc:
            return _sig("short", Bar(ct - timedelta(minutes=1), c5.c, c5.c, c5.c, c5.c))
        if gap < 0 and c5.c < self.pdc:
            return _sig("long", Bar(ct - timedelta(minutes=1), c5.c, c5.c, c5.c, c5.c))
        return None


class _VWAPFive(_Five):
    """Shared plumbing: session VWAP + 5-min bars + indicator lookup."""

    def __init__(self, cfg, tick, feats, window="am", session=NASDAQ, max_trades=2, reentry=False, **_):
        self.tick, self.f, self.s = tick, feats, session
        self.last_entry = session.windows[window][0]
        self.max_n, self.reentry = max_trades, reentry

    def ok_side(self, side):
        return self.reentry or side not in self.sides

    def reset(self, day):
        self._five_reset()
        self.day, self.n, self.sides = day, 0, set()
        self.sv = self.spv = 0.0

    def register_entry(self, side):
        self.n += 1
        self.sides.add(side)

    def _step(self, b: Bar):
        """Update VWAP; on a completed 5-min bar inside the entry window return (bar, close time,
        vwap, indicators), else None."""
        t = b.t.astimezone(ET)
        if t.date() != self.day:
            self.reset(t.date())
        if self.s.open <= t.time() < self.s.close:
            v = b.v or 1.0
            self.sv += v
            self.spv += v * (b.h + b.l + b.c) / 3
        done = self._five(b)
        if not done or self.sv == 0:
            return None
        c5, ct = done
        if self.n >= self.max_n or not (self.s.at(15) <= ct.time() <= self.last_entry):
            return None
        ind = self.f.five_at(ct)
        if not ind or ind["mid"] is None or ind["prev"] is None:
            return None
        return c5, ct, self.spv / self.sv, ind


class BBRev(_VWAPFive):
    """Reversion: a 5-min bar closes outside the Bollinger band (20, 2) on the far side of VWAP
    (the stretch), and on the NEXT 5-min bar the MACD histogram shrinks (momentum fading) -> fade
    toward VWAP. Stop beyond the extreme of the two bars."""
    name = "bbrev"

    def reset(self, day):
        super().reset(day)
        self.last = None  # (bar, vwap, indicators) of the previous completed 5-min bar

    def on_bar(self, b: Bar):
        st = self._step(b)
        if not st:
            return None
        c5, ct, vwap, ind = st
        prev, self.last = self.last, (Bar(c5.t, c5.o, c5.h, c5.l, c5.c), vwap, ind)
        if prev is None:
            return None
        pb, pv, pi = prev
        fading_up = ind["hist"] < pi["hist"]
        fading_dn = ind["hist"] > pi["hist"]
        if pb.c > pi["up"] and pb.c > pv and fading_up and c5.c < pb.c and self.ok_side("short"):
            return Signal("short", ct, c5.c, max(pb.h, c5.h) + self.tick, c5.c)
        if pb.c < pi["dn"] and pb.c < pv and fading_dn and c5.c > pb.c and self.ok_side("long"):
            return Signal("long", ct, c5.c, c5.c, min(pb.l, c5.l) - self.tick)
        return None


class BBMom(_VWAPFive):
    """Momentum: price on the trend side of VWAP, MACD line crosses its signal line that way, and
    the 5-min close is beyond the Bollinger middle band -> enter with the move."""
    name = "bbmom"

    def on_bar(self, b: Bar):
        st = self._step(b)
        if not st:
            return None
        c5, ct, vwap, ind = st
        p = ind["prev"]
        up_cross = p["macd"] <= p["signal"] and ind["macd"] > ind["signal"]
        dn_cross = p["macd"] >= p["signal"] and ind["macd"] < ind["signal"]
        if up_cross and c5.c > vwap and c5.c > ind["mid"] and self.ok_side("long"):
            return _sig("long", Bar(ct - timedelta(minutes=1), c5.c, c5.c, c5.c, c5.c))
        if dn_cross and c5.c < vwap and c5.c < ind["mid"] and self.ok_side("short"):
            return _sig("short", Bar(ct - timedelta(minutes=1), c5.c, c5.c, c5.c, c5.c))
        return None


class CRT:
    """Candle Range Theory: the previous 1-hour (or 4-hour) candle is the range; the next candle
    trades beyond exactly one side and closes back inside -> trade back into the range at that close.
    Stop one tick beyond the sweep wick; target the range's 50% (mid) or its far side (range)."""

    def __init__(self, cfg, tick, feats, window="day", session=NASDAQ, tf=60, target="mid",
                 max_trades=2, reentry=False, **_):
        self.tick, self.f, self.s, self.tf, self.tgt = tick, feats, session, tf, target
        self.last_entry = session.windows[window][0]
        self.max_n = max_trades

    def reset(self, day):
        self.day, self.n = day, 0

    def register_entry(self, side):
        self.n += 1

    def on_bar(self, b: Bar):
        t = b.t.astimezone(ET)
        if t.date() != self.day:
            self.reset(t.date())
        ct = b.t + timedelta(minutes=1)
        pat = self.f.crt_at(self.tf, ct)
        if not pat or self.n >= self.max_n or not (self.s.open < ct.astimezone(ET).time() <= self.last_entry):
            return None
        mid = (pat["ph"] + pat["pl"]) / 2
        if pat["side"] == "long":
            tgt = mid if self.tgt == "mid" else pat["ph"]
            if tgt <= pat["sc"]:
                return None  # already at/above the target
            return Signal("long", ct, pat["sc"], pat["sc"], pat["sl"] - self.tick, target=tgt)
        tgt = mid if self.tgt == "mid" else pat["pl"]
        if tgt >= pat["sc"]:
            return None
        return Signal("short", ct, pat["sc"], pat["sh"] + self.tick, pat["sc"], target=tgt)


class Zone:
    """Order-block retest: price returns into a demand (supply) zone formed since midnight ET and a
    1-min candle closes back above (below) it in the trade direction -> enter at that close. A close
    through the far side kills the zone; each zone is traded once. Stop one tick beyond the zone or
    the candle's wick, whichever is further."""

    def __init__(self, cfg, tick, feats, window="day", session=NASDAQ, max_trades=2, reentry=False, **_):
        self.tick, self.f, self.s = tick, feats, session
        self.last_entry = session.windows[window][0]
        self.max_n = max_trades

    def reset(self, day):
        self.day, self.n = day, 0
        self.active: list[dict] = []
        self.seen = datetime.combine(day, time(0, 0), ET)

    def register_entry(self, side):
        self.n += 1

    def on_bar(self, b: Bar):
        t = b.t.astimezone(ET)
        if t.date() != self.day:
            self.reset(t.date())
        ct = b.t + timedelta(minutes=1)
        # zones known before this bar opened are eligible
        new = self.f.zones_between(self.seen, b.t)
        # skip zones already broken before this bar (e.g. overnight, before the strategy was watching)
        self.active += [dict(z) for z in new if z["dead_at"] is None or z["dead_at"] > b.t]
        self.seen = max(self.seen, b.t)
        can_trade = self.n < self.max_n and self.s.at(5) <= ct.astimezone(ET).time() <= self.last_entry
        sig = None
        for z in reversed(self.active):  # most recent zone first
            if z.get("dead"):
                continue
            if z["kind"] == "demand":
                if b.c < z["lo"]:
                    z["dead"] = True     # closed through it: zone failed
                elif can_trade and sig is None and b.l <= z["hi"] and b.c > z["hi"] and b.c > b.o:
                    sig, z["dead"] = Signal("long", ct, b.c, b.c, min(z["lo"], b.l) - self.tick), True
            else:
                if b.c > z["hi"]:
                    z["dead"] = True
                elif can_trade and sig is None and b.h >= z["lo"] and b.c < z["lo"] and b.c < b.o:
                    sig, z["dead"] = Signal("short", ct, b.c, max(z["hi"], b.h) + self.tick, b.c), True
        return sig


def _crt(tf, target):
    return lambda cfg, tick, feats=None, **kw: CRT(cfg, tick, feats, tf=tf, target=target, **kw)


def _hhmm(x: str):
    h, m = x.split(":")
    return time(int(h), int(m))


def _sweep(*extra):
    return lambda cfg, tick, feats=None, **kw: Sweep(cfg, tick, feats, extra=extra, **kw)


def _legacy(cls, uses_session=False):
    if uses_session:
        return lambda cfg, tick, feats=None, session=NASDAQ, **_: cls(cfg, tick, session)
    return lambda cfg, tick, feats=None, **_: cls(cfg, tick)


STRATEGIES = {
    "orb": _legacy(ORB), "orb_first": _legacy(ORBFirst, True), "fade": _legacy(Fade),
    "pullback": _legacy(Pullback, True),
    "mtf": MTF, "sweep": Sweep, "vwapband": VWAPBand, "gapfade": GapFade,
    "sweep_or": _sweep("or"), "sweep_swing": _sweep("swing"), "sweep_all": _sweep("or", "swing"),
    "bbrev": BBRev, "bbmom": BBMom,
    "crt1h_mid": _crt(60, "mid"), "crt1h_range": _crt(60, "range"),
    "crt4h_mid": _crt(240, "mid"), "crt4h_range": _crt(240, "range"),
    "zone4r": Zone, "zone_runner": Zone,
}
ZONE_NAMES = {"zone4r", "zone_runner"}
CRT_NAMES = {"crt1h_mid", "crt1h_range", "crt4h_mid", "crt4h_range"}
SWEEP_NAMES = {"sweep", "sweep_or", "sweep_swing", "sweep_all"}
NEEDS_OVERNIGHT = SWEEP_NAMES | {"mtf", "gapfade", "bbrev", "bbmom"} | CRT_NAMES | ZONE_NAMES
# (window, stop) combinations tested for each idea
GRID = {
    "orb": [("am", "dollar")], "orb_first": [("am", "dollar")], "fade": [("am", "dollar")],
    "pullback": [("am", "dollar")],
    "mtf": [("am", "structural"), ("am", "dollar"), ("day", "structural"), ("day", "dollar")],
    **{n: [("am", "structural")] for n in SWEEP_NAMES},
    "vwapband": [("day", "structural")],
    "gapfade": [("am", "dollar")],
    "bbrev": [("am", "structural")],
    "bbmom": [("am", "dollar")],
    **{n: [("day", "structural")] for n in ("crt1h_mid", "crt1h_range", "crt4h_mid", "crt4h_range")},
    "zone4r": [("day", "structural")], "zone_runner": [("day", "structural")],
}
GRID_COMEX = {k: [("am", stop) for stop in dict.fromkeys(st for _, st in v)] for k, v in GRID.items()}
DEFAULT = ["orb", "mtf", "sweep", "vwapband", "gapfade", "bbrev", "bbmom"]


# --------------------------------------------------------------------------- evaluation
@dataclasses.dataclass
class Variant:
    strategy: str
    r: float
    contracts: int
    window: str = "am"
    stop: str = "dollar"
    filt: str = "none"
    stop_usd: float | None = None     # scalping mode: $ risk per trade
    target_usd: float | None = None   # scalping mode: $ take-profit per trade

    @property
    def label(self) -> str:
        bracket = ("level" if self.strategy in CRT_NAMES else "4R" if self.strategy == "zone4r" else
                   "4R+8R" if self.strategy == "zone_runner" else
                   f"${self.stop_usd:g}/${self.target_usd:g}" if self.target_usd else f"{self.r:g}R")
        base = f"{self.strategy} {self.window} {self.stop[:6]} {bracket} {self.contracts}ct"
        name = CONFIRM_NAME if self.filt == "es" else self.filt
        return base if self.filt == "none" else f"{base} +{name}"

    def cfg(self, base: BotConfig, session: Session = NASDAQ) -> BotConfig:
        last_entry, flatten = session.windows[self.window]
        st = dataclasses.replace(base.strategy, flatten_time=flatten)
        if session is not NASDAQ:  # ORB-style rules follow the session's own open
            # range filters in config.yaml are in Nasdaq points; gold ranges are a few points
            st = dataclasses.replace(st, or_start=session.open, entry_cutoff=last_entry,
                                     min_range_points=1.0, max_range_points=1000.0)
        return dataclasses.replace(base, strategy=st)


def bracket_r(bars: list[Bar], i: int, side: str, stop_pts: float, target_pts: float, n: int,
              spec: ContractSpec, cfg: BotConfig, risk_usd: float, slip_ticks: int,
              partial_pts: float = 0.0, n_part: int = 0) -> float:
    """Outcome in R of a bracket entered at bar i's open (no daily limits). With partial_pts, n_part
    contracts exit there, the stop moves to breakeven and the rest runs to the target."""
    slip = slip_ticks * spec.tick_size
    long = side == "long"
    sign = 1 if long else -1
    entry = bars[i].o + sign * slip
    stop, tgt = entry - sign * stop_pts, entry + sign * target_pts
    fee = cfg.risk.fee_per_contract_rt
    booked = 0.0
    if partial_pts and n_part:
        plvl = entry + sign * partial_pts
        day = bars[0].t.astimezone(ET).date()
        flat = datetime.combine(day, cfg.strategy.flatten_time, ET)
        rest, exit_px, done = n, bars[-1].c, False
        for b in bars[i:]:
            if b.t.astimezone(ET) >= flat:
                exit_px = b.o - sign * slip
                break
            if (b.l <= stop) if long else (b.h >= stop):
                exit_px = stop - sign * slip
                break
            if not done and ((b.h >= plvl) if long else (b.l <= plvl)):
                booked = (plvl - entry) * sign * n_part * spec.point_value - fee * n_part
                rest, stop, done = n - n_part, entry, True
                continue
            if done and ((b.h >= tgt) if long else (b.l <= tgt)):
                exit_px = tgt
                break
        pnl = booked + (exit_px - entry) * sign * rest * spec.point_value - fee * rest
        return pnl / risk_usd
    day = bars[0].t.astimezone(ET).date()
    flat = datetime.combine(day, cfg.strategy.flatten_time, ET)
    exit_px = bars[-1].c
    for b in bars[i:]:
        if b.t.astimezone(ET) >= flat:
            exit_px = b.o - sign * slip
            break
        if (b.l <= stop) if long else (b.h >= stop):
            exit_px = stop - sign * slip
            break
        if (b.h >= tgt) if long else (b.l <= tgt):
            exit_px = tgt
            break
    pnl = (exit_px - entry) * sign * n * spec.point_value - cfg.risk.fee_per_contract_rt * n
    return pnl / risk_usd


def stats(trades, days_by_key, spec, cfg, slip, seeds: int, rng: random.Random) -> dict:
    if not trades:
        return {"trades": 0}
    pnl = [t.pnl for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    daily: dict = defaultdict(float)
    for t in trades:
        daily[t.day] += t.pnl
    eq = peak = dd = 0.0
    for d in sorted(daily):
        eq += daily[d]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    streak = best = 0
    for p in pnl:
        streak = streak + 1 if p <= 0 else 0
        best = max(best, streak)

    # random-direction benchmark on the same entries and brackets
    pairs, actual = [], 0.0
    for t in trades:
        bars = days_by_key[t.day]
        lr = bracket_r(bars, t.entry_i, "long", t.stop_pts, t.target_pts, t.contracts, spec, cfg, t.risk_usd, slip,
                       t.partial_pts, t.n_part)
        sr = bracket_r(bars, t.entry_i, "short", t.stop_pts, t.target_pts, t.contracts, spec, cfg, t.risk_usd, slip,
                       t.partial_pts, t.n_part)
        pairs.append((lr, sr))
        actual += lr if t.side == "long" else sr
    better = 0
    for _ in range(seeds):
        if sum(p[rng.random() < 0.5] for p in pairs) >= actual - 1e-9:
            better += 1
    rand_mean = statistics.mean((a + b) / 2 for a, b in pairs)
    return {
        "trades": len(trades),
        "win": len(wins) / len(trades),
        "net": sum(pnl),
        "pf": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf"),
        "avg_r": statistics.mean(t.pnl / t.risk_usd for t in trades if t.risk_usd),
        "edge_r": actual / len(trades) - rand_mean,
        "p": better / seeds,
        "max_dd": dd,
        "streak": best,
        "worst_day": min(daily.values()),
        "survives": dd > -COMBINE_DD,
    }


def checks_for(filt: str, dayctx: DayContext | None, es: ESConfirm | None) -> list:
    """Filter name -> list of checks on a Signal."""
    rv = lambda s: dayctx.check_rvol(s.time)
    nw = lambda s: dayctx.check_news(s.time)
    ec = lambda s: es.check(s.side, s.time)
    table = {"none": [], "rvol": [rv], "news": [nw], "es": [ec],
             "all": [rv, nw] + ([ec] if es is not None else [])}
    return table[filt]


def run_variant(v: Variant, days, cfg: BotConfig, spec: ContractSpec, slip: int, feats=None,
                session: Session = NASDAQ, dayctx: DayContext | None = None, es: ESConfirm | None = None,
                max_trades: int | None = None, reentry: bool = False):
    vcfg = v.cfg(cfg, session)
    if max_trades is not None:  # ORB-style rules read their caps from the config
        vcfg = dataclasses.replace(vcfg, strategy=dataclasses.replace(
            vcfg.strategy, max_trades_per_day=max_trades, one_trade_per_side=not reentry))
    if v.strategy in ZONE_NAMES:  # 4R, or half at 4R + runner to 8R with breakeven stop
        mr = cfg.risk.max_risk_per_trade
        risk = dataclasses.replace(cfg.risk, stop_mode=v.stop, target_usd=0, target_mode="r",
                                   target_per_trade=mr * (4 if v.strategy == "zone4r" else 8),
                                   partial_r=4.0 if v.strategy == "zone_runner" else 0.0)
    elif v.strategy in CRT_NAMES:  # targets come from the candle range itself
        risk = dataclasses.replace(cfg.risk, stop_mode=v.stop, target_mode="level", target_usd=0,
                                   max_risk_per_trade=v.stop_usd or cfg.risk.max_risk_per_trade)
    elif v.target_usd:
        risk = dataclasses.replace(cfg.risk, stop_mode=v.stop, max_risk_per_trade=v.stop_usd,
                                   target_usd=v.target_usd)
    else:
        risk = dataclasses.replace(cfg.risk, stop_mode=v.stop, target_per_trade=cfg.risk.max_risk_per_trade * v.r)
    make = STRATEGIES[v.strategy]
    checks = checks_for(v.filt, dayctx, es)
    caps = {} if max_trades is None else {"max_trades": max_trades, "reentry": reentry}
    out = []
    for _, bars in days:
        strat = make(vcfg.strategy, spec.tick_size, feats, window=v.window, session=session, **caps)
        if checks:
            strat = Filtered(strat, checks)
        out += simulate_day(bars, vcfg, spec, v.contracts, slip, strat=strat, risk=risk)
    return out


def fmt(s: dict) -> str:
    if not s.get("trades"):
        return f"{'no trades':>58}"
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"{s['trades']:>4} {s['win']:>4.0%} {s['avg_r']:>+6.2f} {s['net']:>9,.0f} {pf:>5} "
            f"{s['max_dd']:>8,.0f} {s['streak']:>3} {s['p']:>5.2f}")


KEYS = [("trades", "Trades", "0"), ("win", "Win %", "0.0%"), ("avg_r", "Avg R", "+0.00;-0.00"),
        ("edge_r", "Edge vs random (R)", "+0.00;-0.00"), ("p", "p(random)", "0.00"),
        ("net", "Net $", '$#,##0;[Red]-$#,##0'), ("pf", "Profit factor", "0.00"),
        ("max_dd", "Max DD $", '$#,##0;[Red]-$#,##0'), ("streak", "Longest losing streak", "0"),
        ("worst_day", "Worst day $", '$#,##0;[Red]-$#,##0'), ("survives", "DD under $2k", "@")]


def write_xlsx(rows, path, periods, spans, symbol, threshold):
    wb = Workbook()
    ws = wb.active
    ws.title = "Results"
    fixed = ["Variant", "Strategy", "Window", "Stop", "R target", "Contracts", "Filter"]
    hdr = fixed + [f"{p} {k[1]}" for p in periods for k in KEYS]
    ws.append(hdr)
    for c in ws[1]:
        c.font = Font(name="Arial", bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", start_color="1F3864")
    for v, *per in rows:
        row = [v.label, v.strategy, v.window, v.stop, v.r, v.contracts, v.filt]
        for st in per:
            for k, _, _ in KEYS:
                val = st.get(k, "")
                if k == "pf" and val == float("inf"):
                    val = ""
                if k == "survives" and val != "":
                    val = "yes" if val else "NO"
                row.append(val)
        ws.append(row)
    for r in ws.iter_rows(min_row=2):
        for c in r:
            c.font = Font(name="Arial")
        for pi in range(len(periods)):
            for j, (_, _, f) in enumerate(KEYS):
                r[len(fixed) + pi * len(KEYS) + j].number_format = f
    for i in range(1, len(hdr) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 38 if i == 1 else 12
    ws.freeze_panes = ws.cell(row=2, column=len(fixed) + 1)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(hdr))}{ws.max_row}"
    info = wb.create_sheet("About")
    for line in [f"Symbol: {symbol}"] + [f"{p}: {s}" for p, s in zip(periods, spans)] + [
        "Choose variants by TRAIN only; TEST (and any HOLDOUT) is the honest check.",
        f"p(random): share of random long/short entries (same times, same bracket) that did at least as well. "
        f"Flag threshold: {threshold}.",
        "Edge vs random: average R per trade minus what a random direction would have averaged.",
        "DD under $2k: max drawdown (daily close) stayed inside the Topstep 50K trailing limit.",
        "All variants: $250 risk per trade, 1-tick slippage, fees, $500/$1,250 daily limits.",
        "Stop 'dollar' = fixed $250 distance; 'struct' = at the strategy's level with size reduced to $250 risk.",
        "Window 'am' = entries to 11:30 ET, flat 11:55; 'day' = entries to 15:00 ET, flat 15:55.",
    ]:
        info.append([line])
    info.column_dimensions["A"].width = 120
    wb.save(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare entry ideas with a train/test split")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--csv-tz", default="UTC")
    ap.add_argument("--symbol", help="MNQ, MES, MGC, GC... (default: symbol in config.yaml)")
    ap.add_argument("--session", choices=list(SESSIONS),
                    help="nasdaq (9:30 open) or comex (8:20 open); default follows the symbol")
    ap.add_argument("--test-months", type=float, default=4)
    ap.add_argument("--strategies", nargs="+", default=DEFAULT, choices=list(STRATEGIES))
    ap.add_argument("--r", nargs="+", type=float, default=[2.0], help="target as a multiple of the stop (default 2:1)")
    ap.add_argument("--filters", nargs="+", default=["none", "rvol", "news", "es", "all"],
                    choices=["none", "rvol", "news", "es", "all"],
                    help="rvol = busy open only; news = skip 8:30 release-shock days; es = ES on the same side "
                         "of its VWAP; all = every available filter")
    ap.add_argument("--confirm-csv", help="confirmation market, 1-minute all-hours CSV (ES for MNQ, MNQ for MES, "
                    "6E or SI for gold): trade only when it is on the same side of its VWAP")
    ap.add_argument("--confirm-name", default="es", help="label for the confirmation filter, e.g. 6e or si")
    ap.add_argument("--sizes", nargs="+", type=int, default=[10], help="max contracts ($250 risk each)")
    ap.add_argument("--target-usd", type=float, help="scalping mode: take profit at this many $ on every trade")
    ap.add_argument("--stops-usd", nargs="+", type=float, default=[75, 150, 250],
                    help="scalping mode: $ risk per trade to compare (default 75 150 250)")
    ap.add_argument("--max-trades", type=int, help="trades per day allowed for every strategy (default: 2)")
    ap.add_argument("--reentry", action="store_true", help="allow repeat trades in the same direction")
    ap.add_argument("--profit-cap", type=float, help="override the daily profit cap ($)")
    ap.add_argument("--loss-limit", type=float, help="override the daily loss limit ($)")
    ap.add_argument("--slippage", type=int, default=1)
    ap.add_argument("--seeds", type=int, default=2000)
    ap.add_argument("--threshold", type=float, default=0.02, help="p(random) needed in every period to flag")
    ap.add_argument("--entry-cutoff", metavar="HH:MM", help="last signal time (ET) for ORB and sweep entries; "
                    "default: entry_cutoff in the config. The pre-2026-10-22 sweep results used 11:30")
    ap.add_argument("--level-csv", nargs="+", default=[], help="extra per-contract bars used only for "
                    "prior-day/overnight levels on roll days (from databento_fetch --roll-fill)")
    ap.add_argument("--holdout", action="store_true",
                    help="final check: treat the whole CSV as one untouched period (use with --only)")
    ap.add_argument("--only", nargs="+", metavar="LABEL", help='run only these variants, e.g. "mtf day struct 2.2R 10ct"')
    ap.add_argument("--out", default="journal/research.xlsx")
    a = ap.parse_args()

    global CONFIRM_NAME
    CONFIRM_NAME = a.confirm_name.lower()
    cfg = load_config(a.config)
    if a.profit_cap is not None or a.loss_limit is not None:
        cfg = dataclasses.replace(cfg, risk=dataclasses.replace(
            cfg.risk, max_daily_profit=a.profit_cap if a.profit_cap is not None else cfg.risk.max_daily_profit,
            max_daily_loss=a.loss_limit if a.loss_limit is not None else cfg.risk.max_daily_loss))
    if a.entry_cutoff:
        cfg = dataclasses.replace(cfg, strategy=dataclasses.replace(cfg.strategy, entry_cutoff=_hhmm(a.entry_cutoff)))
    spec = spec_for(a.symbol or cfg.symbol)
    if spec.contract_id in RANGE_FILTER and (a.symbol or cfg.symbol).upper() != cfg.symbol.upper():
        lo, hi = RANGE_FILTER[spec.contract_id]  # config.yaml's range filter is for a different market
        cfg = dataclasses.replace(cfg, strategy=dataclasses.replace(cfg.strategy, min_range_points=lo,
                                                                    max_range_points=hi))
    session = SESSIONS[a.session or SESSION_FOR_SYMBOL.get(spec.contract_id, "nasdaq")]
    grid = GRID_COMEX if session is COMEX else GRID
    print(f"Instrument {spec.contract_id} (${spec.point_value:g}/pt), {session.name} session: open "
          f"{session.open:%H:%M} ET, entries until {session.windows['am'][0]:%H:%M}, flat by {session.windows['am'][1]:%H:%M}")
    if session is NASDAQ:
        print(f"ORB and sweep entries until {cfg.strategy.entry_cutoff:%H:%M} ET (config entry_cutoff)")
    if (CRT_NAMES | ZONE_NAMES) & set(a.strategies) and "day" in session.windows:
        print(f"  (CRT and zone strategies use the full-session window: entries until {session.windows['day'][0]:%H:%M}, "
              f"flat by {session.windows['day'][1]:%H:%M})")
    bars = load_csv(a.csv, a.csv_tz)
    feats = None
    if NEEDS_OVERNIGHT & set(a.strategies):
        if not has_overnight(bars):
            raise SystemExit(
                "mtf / sweep / gapfade / bbrev / bbmom need all 23 hours of data (1-hour trend, overnight levels). Rebuild it "
                "for free from your Databento download:\n  python -m orbbot.databento_fetch --from-cache "
                "data\\MNQ_v_0_<dates>.dbn.zst --all-hours --out data\\mnq_all.csv")
        print("Building 1-hour, 15-minute and session-level context...")
        extra = [b for f in a.level_csv for b in load_csv(f, a.csv_tz)]
        feats = Features(bars, session.open, session.close, level_bars=extra)
        if feats.roll_sessions:
            print(f"Contract rolls in data: {', '.join(str(d) for d in sorted(feats.roll_sessions))}")
            for d in sorted(feats.level_notes):
                print(f"  {d}: " + "; ".join(feats.level_notes[d]))
        elif not any(b.inst for b in bars[:100]):
            print("Note: CSV has no instrument_id column, so contract rolls can't be detected. Rebuild it with "
                  "python -m orbbot.databento_fetch --from-cache <file>.dbn.zst --all-hours --out <csv>")

    filters = list(a.filters)
    es = None
    if a.confirm_csv:
        print(f"Loading confirmation market from {a.confirm_csv}...")
        es = ESConfirm(load_csv(a.confirm_csv, a.csv_tz), session.open, session.close)
    elif "es" in filters:
        filters.remove("es")
        print("Note: skipping the ES filter (add --confirm-csv data\\es_all.csv to include it).")
    if not has_overnight(bars) and ({"news", "all"} & set(filters)):
        filters = [f for f in filters if f not in ("news", "all")]
        print("Note: skipping the news filter (it needs the 8:30 ET bars; use an --all-hours CSV).")
    dayctx = DayContext(bars, session.open) if set(filters) - {"none"} else None

    by_day: dict = defaultdict(list)
    for b in bars:
        t = b.t.astimezone(ET)
        if session.at(-5) <= t.time() < session.close:
            by_day[t.date()].append(b)
    days = [(d, sorted(bs, key=lambda x: x.t)) for d, bs in sorted(by_day.items())
            if d.weekday() in cfg.trading_days and d not in cfg.holidays]
    keyed = {str(d): bs for d, bs in days}
    span = lambda ds: f"{ds[0][0]} to {ds[-1][0]} ({len(ds)} sessions)" if ds else "none"
    if a.holdout:
        periods, chunks = ("HOLDOUT",), [days]
    else:
        split = days[-1][0] - timedelta(days=int(a.test_months * 30.5))
        periods = ("TRAIN", "TEST")
        chunks = [[x for x in days if x[0] <= split], [x for x in days if x[0] > split]]
    spans = [span(c) for c in chunks]
    for p, sp in zip(periods, spans):
        print(f"{p:<7} {sp}")
    print()

    if a.target_usd:
        variants = [Variant(s, 0.0, n, w, st, f, stop_usd=su, target_usd=a.target_usd)
                    for s in a.strategies for (w, st) in grid[s] for su in a.stops_usd for n in a.sizes for f in filters]
        print(f"Scalping mode: ${a.target_usd:g} take-profit, stops {', '.join('$%g' % x for x in a.stops_usd)}, "
              f"up to {a.max_trades or 2} trades/day{' with re-entry' if a.reentry else ''}, "
              f"daily cap +${cfg.risk.max_daily_profit:,.0f} / -${cfg.risk.max_daily_loss:,.0f}")
    else:
        variants = [Variant(s, r, n, w, st, f) for s in a.strategies for (w, st) in grid[s] for r in a.r
                    for n in a.sizes for f in filters]
    if a.only:
        wanted = {" ".join(x.split()) for x in a.only}  # labels as printed, e.g. "... +6e"
        variants = [v for v in variants if v.label in wanted]
        if not variants:
            raise SystemExit(f"No variant matched {a.only}. Labels look like: \"mtf day struct 2.2R 10ct\"")
    rng = random.Random(7)
    rows = []
    for v in variants:
        vcfg = v.cfg(cfg, session)
        per = [stats(run_variant(v, ch, cfg, spec, a.slippage, feats, session, dayctx, es, a.max_trades, a.reentry),
                     keyed, spec, vcfg, a.slippage, a.seeds, rng)
               for ch in chunks]
        rows.append((v, *per))
        print(f"  ran {v.label}")

    rows.sort(key=lambda x: x[1].get("avg_r", -99), reverse=True)
    cols = f"{'n':>4} {'win':>4} {'avgR':>6} {'net $':>9} {'PF':>5} {'maxDD':>8} {'LS':>3} {'p':>5}"
    w = 38
    print(f"\n{'variant (sorted by ' + periods[0] + ' avg R)':<{w}}" + "".join(f"| {p:^58} " for p in periods))
    print(f"{'':<{w}}" + "".join(f"| {cols} " for _ in periods))
    for v, *per in rows:
        ok = all(st.get("p", 1) < a.threshold and st.get("avg_r", -1) > 0 and st.get("survives") for st in per)
        flag = f" <- beats random (p<{a.threshold:g}), profitable, DD<$2k in every period" if ok else ""
        print(f"{v.label:<{w}}" + "".join(f"| {fmt(st)} " for st in per) + flag)
    print("\nn=trades  avgR=average result per trade in R  LS=longest losing streak  "
          f"p=share of random entries that did as well (want < {a.threshold:g})")
    print(f"Max DD over ${COMBINE_DD:,.0f} would fail a Topstep 50K combine.")
    write_xlsx(rows, a.out, periods, spans, f"{spec.contract_id} ({session.name} session)", a.threshold)
    print(f"\nWorkbook written to {a.out}")


if __name__ == "__main__":
    main()
