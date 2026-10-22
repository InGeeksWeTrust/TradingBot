from datetime import date, datetime, time, timedelta

from orbbot.filters import DayContext, ESConfirm, Filtered
from orbbot.strategy import ET, Bar, Signal


def day_bars(d, open_vol, news_range, start=time(8, 30), end=time(10, 0)):
    """1-min bars for one day: 8:30-8:44 has the given range, the 9:30-9:34 bars carry open_vol."""
    out, t = [], datetime.combine(d, start, ET)
    while t.time() < end:
        tt = t.time()
        rng = news_range if time(8, 30) <= tt < time(8, 45) else 1.0
        v = open_vol / 5 if time(9, 30) <= tt < time(9, 35) else 10
        out.append(Bar(t, 100, 100 + rng / 2, 100 - rng / 2, 100, v))
        t += timedelta(minutes=1)
    return out


def history(n, last_open_vol, last_news):
    d0, bars = date(2026, 1, 5), []
    days = [d0 + timedelta(days=i) for i in range(n + 1)]
    for d in days[:-1]:
        bars += day_bars(d, 1000, 4.0)
    bars += day_bars(days[-1], last_open_vol, last_news)
    return bars, days[-1]


def at(d, hh, mm):
    return datetime.combine(d, time(hh, mm), ET)


def test_rvol_passes_on_busy_open_and_only_after_935():
    bars, d = history(20, 2000, 4.0)
    ctx = DayContext(bars, time(9, 30))
    assert ctx.check_rvol(at(d, 9, 50))
    assert not ctx.check_rvol(at(d, 9, 34))           # not known yet


def test_rvol_blocks_normal_open_and_short_history():
    bars, d = history(20, 1000, 4.0)
    assert not DayContext(bars, time(9, 30)).check_rvol(at(d, 9, 50))
    bars, d = history(5, 3000, 4.0)                     # fewer than 10 prior sessions
    assert not DayContext(bars, time(9, 30)).check_rvol(at(d, 9, 50))


def test_news_skips_shock_day_and_waits_until_845():
    bars, d = history(20, 1000, 15.0)                   # 8:30 range ~3.75x normal
    assert not DayContext(bars, time(9, 30)).check_news(at(d, 9, 50))
    bars, d = history(20, 1000, 4.0)
    ctx = DayContext(bars, time(9, 30))
    assert ctx.check_news(at(d, 9, 50)) and not ctx.check_news(at(d, 8, 44))


def test_es_confirm_uses_vwap_side_known_at_signal_time():
    d = date(2026, 1, 5)
    es = [Bar(at(d, 9, 30) + timedelta(minutes=i), 100, 100.5, 99.5, 100 + i, 10) for i in range(10)]
    c = ESConfirm(es, time(9, 30), time(16, 0))         # rising ES: close above VWAP
    assert c.check("long", at(d, 9, 35)) and not c.check("short", at(d, 9, 35))
    assert not c.check("long", at(d, 9, 30))            # first ES bar not closed yet


def test_filtered_wrapper_blocks_failing_signals():
    class Inner:
        def reset(self, day): pass
        def register_entry(self, side): pass
        def on_bar(self, b): return Signal("long", b.t, b.c, b.c, b.c)
    b = Bar(at(date(2026, 1, 5), 10, 0), 1, 1, 1, 1)
    assert Filtered(Inner(), [lambda s: True]).on_bar(b)
    assert Filtered(Inner(), [lambda s: True, lambda s: False]).on_bar(b) is None
