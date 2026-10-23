"""Intraday sweep levels: opening-range high/low and aged swing highs/lows."""
from datetime import time

from orbbot.research import STRATEGIES, Sweep
from orbbot.strategy import ET
from tests.test_strategies_mtf import CFG, DAY, FakeFeats, minutes, run


def flat(n, px=20000.0):
    return [(px, px + 2, px - 2, px)] * n


def five(o, h, l, c):
    """One 5-minute bar as five 1-min bars with that combined OHLC."""
    return [(o, h, min(o, c), o)] + [(o, max(o, c), min(o, c), o)] * 3 + [(o, max(o, c), l, c)]


def test_default_sweep_ignores_intraday_levels():
    # OR 9:30-9:45 high 20010; at 10:00 a bar wicks to 20012 and closes back at 20000
    bars = minutes(time(9, 30), [(20000, 20010, 19990, 20000)] * 15 + flat(15) + five(20000, 20012, 19998, 20000))
    assert run(Sweep(CFG, 0.25, FakeFeats()), bars) == []


def test_or_high_sweep_shorts():
    bars = minutes(time(9, 30), [(20000, 20010, 19990, 20000)] * 15 + flat(15) + five(20000, 20012, 19998, 20000))
    sigs = run(Sweep(CFG, 0.25, FakeFeats(), extra=("or",)), bars)
    assert len(sigs) == 1 and sigs[0].side == "short"
    assert sigs[0].or_high == 20012.25                       # stop one tick over the wick
    assert sigs[0].time.astimezone(ET).time() == time(10, 5)


def test_or_low_not_live_before_range_completes():
    # 9:35-9:40 bar dips under the first 5 minutes' low and closes back: OR not finished yet
    bars = minutes(time(9, 30), flat(5) + five(20000, 20001, 19990, 20000))
    assert run(Sweep(CFG, 0.25, FakeFeats(), extra=("or",)), bars) == []


def _swing_day(pivot_high=20030.0, quiet_bars=12, sweep=(20000, 20033, 19998, 20000)):
    """9:30 open; pivot high 9:40-9:45 (5-min bars either side lower); quiet; then a sweep bar."""
    seq = (five(20000, 20010, 19995, 20000) + five(20000, 20012, 19995, 20000)
           + five(20000, pivot_high, 19995, 20000)
           + five(20000, 20012, 19995, 20000) + five(20000, 20010, 19995, 20000))
    seq += five(20000, 20005, 19995, 20000) * quiet_bars
    seq += five(*sweep)
    return minutes(time(9, 30), seq)


def test_swing_high_held_an_hour_is_swept():
    sigs = run(Sweep(CFG, 0.25, FakeFeats(), extra=("swing",)), _swing_day(quiet_bars=12))
    assert len(sigs) == 1 and sigs[0].side == "short"
    assert sigs[0].or_high == 20033.25


def test_young_swing_is_ignored():
    # sweep comes 30 minutes after the pivot formed: too young
    assert run(Sweep(CFG, 0.25, FakeFeats(), extra=("swing",)), _swing_day(quiet_bars=3)) == []


def test_swing_broken_by_a_close_is_gone():
    # a bar closes above the pivot (real breakout), price falls back, later wick+reclaim: no trade
    seq = (five(20000, 20010, 19995, 20000) + five(20000, 20012, 19995, 20000)
           + five(20000, 20030, 19995, 20000) + five(20000, 20012, 19995, 20000)
           + five(20000, 20010, 19995, 20000))
    seq += five(20000, 20005, 19995, 20000) * 4
    seq += five(20000, 20040, 19995, 20036)            # breaks out early (pivot too young to trade)
    seq += five(20036, 20036, 19995, 20000) * 8
    seq += five(20000, 20033, 19998, 20000)
    assert run(Sweep(CFG, 0.25, FakeFeats(), extra=("swing",)), minutes(time(9, 30), seq)) == []


def test_registered_names():
    for n in ("sweep_or", "sweep_swing", "sweep_all"):
        s = STRATEGIES[n](CFG, 0.25, FakeFeats())
        assert isinstance(s, Sweep) and s.extra
