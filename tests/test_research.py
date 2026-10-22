from datetime import date, datetime, time, timedelta

from orbbot.config import StrategyConfig
from orbbot.research import Fade, ORBFirst
from orbbot.strategy import ET, Bar

DAY = date(2026, 10, 19)


def session(or_bars, after):
    """or_bars: 15 (o,h,l,c) for 09:30-09:44; after: (o,h,l,c) per minute from 09:45."""
    t = datetime.combine(DAY, time(9, 30), ET)
    out = []
    for ohlc in list(or_bars) + list(after):
        out.append(Bar(t, *ohlc))
        t += timedelta(minutes=1)
    return out


FLAT_OR = [(20020, 20040, 20000, 20020)] * 15
UP_OR = [(20000, 20040, 20000, 20005 + 2 * i) for i in range(15)]       # opens 20000, closes 20033
DOWN_OR = [(20040, 20040, 20000, 20035 - 2 * i) for i in range(15)]     # opens 20040, closes 20007
BREAK_UP = [(20040, 20050, 20038, 20048)] * 5                           # 5-min close 20048 > 20040


def signals(strat, bars):
    strat.reset(DAY)
    out = []
    for b in bars:
        s = strat.on_bar(b)
        if s:
            out.append(s)
            strat.register_entry(s.side)
    return out


def test_fade_trades_failed_breakout_back_inside():
    back_in = [(20045, 20046, 20025, 20030)] * 5                        # 5-min close 20030, inside
    sigs = signals(Fade(StrategyConfig(), 0.25), session(FLAT_OR, BREAK_UP + back_in))
    assert len(sigs) == 1 and sigs[0].side == "short"
    assert sigs[0].time.astimezone(ET).time() == time(9, 55)


def test_fade_ignores_breakout_that_holds():
    hold = [(20048, 20060, 20045, 20055)] * 20
    assert not signals(Fade(StrategyConfig(), 0.25), session(FLAT_OR, BREAK_UP + hold))


def test_orb_first_takes_breakout_in_range_direction():
    sigs = signals(ORBFirst(StrategyConfig(), 0.25), session(UP_OR, BREAK_UP))
    assert [s.side for s in sigs] == ["long"]


def test_orb_first_skips_breakout_against_range_direction_and_rest_of_day():
    down_later = [(20000, 20002, 19980, 19990)] * 5                      # short breakout afterwards
    sigs = signals(ORBFirst(StrategyConfig(), 0.25), session(DOWN_OR, BREAK_UP + down_later))
    assert sigs == []
