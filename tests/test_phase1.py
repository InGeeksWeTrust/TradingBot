"""Phase 1 correctness: contract-consistent levels across rolls, and one authoritative entry cutoff."""
import dataclasses
from datetime import date, datetime, time, timedelta

import pytest

from orbbot.config import BotConfig, StrategyConfig, validate
from orbbot.features import Features
from orbbot.research import NASDAQ, Sweep
from orbbot.strategy import ET, Bar, ORBStrategy

OLD, NEW = 111, 222
SPREAD = 250.0  # the new contract trades this far above the old one


def bars_between(start, end, px, inst, wiggle=2.0):
    out, t = [], start
    while t < end:
        if not (time(16, 0) <= t.time() < time(18, 0)):  # daily maintenance break / post-close
            out.append(Bar(t, px, px + wiggle, px - wiggle, px, 10, inst))
        t += timedelta(minutes=1)
    return out


def roll_series(roll_at_hour=19):
    """Wed 2026-12-16 RTH on OLD at 20000; Wed evening OLD until roll_at_hour, then NEW at 20250."""
    wed = date(2026, 12, 16)
    a = bars_between(datetime.combine(wed, time(9, 30), ET), datetime.combine(wed, time(16, 0), ET), 20000, OLD)
    a += bars_between(datetime.combine(wed, time(18, 0), ET), datetime.combine(wed, time(roll_at_hour, 0), ET), 20000, OLD)
    a += bars_between(datetime.combine(wed, time(roll_at_hour, 0), ET),
                      datetime.combine(wed + timedelta(days=1), time(9, 30), ET), 20000 + SPREAD, NEW)
    a += bars_between(datetime.combine(wed + timedelta(days=1), time(9, 30), ET),
                      datetime.combine(wed + timedelta(days=1), time(16, 0), ET), 20000 + SPREAD, NEW)
    return a, wed + timedelta(days=1)


def test_roll_session_levels_left_out_without_contract_data():
    bars, thu = roll_series()
    f = Features(bars)
    lv = f.day_levels(thu)
    assert thu in f.roll_sessions
    assert lv.pdh is None and lv.pdl is None          # prior day was the old contract
    assert lv.onh is None and lv.onl is None          # overnight began on the old contract
    assert any("unavailable" in n for n in f.level_notes[thu])


def test_roll_session_levels_use_incoming_contract_when_filled():
    bars, thu = roll_series()
    wed = thu - timedelta(days=1)
    fill = bars_between(datetime.combine(wed, time(9, 30), ET), datetime.combine(wed, time(16, 0), ET), 20240, NEW, 5)
    fill += bars_between(datetime.combine(wed, time(18, 0), ET), datetime.combine(thu, time(9, 30), ET), 20245, NEW, 3)
    f = Features(bars, level_bars=fill)
    lv = f.day_levels(thu)
    assert (lv.pdh, lv.pdl) == (20245, 20235)          # the new contract's own prior session
    assert lv.onh == 20000 + SPREAD + 2 and lv.onl == 20245 - 3
    assert all("roll fill" in n for n in f.level_notes[thu])


def test_partial_fill_that_misses_the_start_of_the_overnight_is_not_used():
    bars, thu = roll_series()
    late = bars_between(datetime.combine(thu, time(1, 0), ET), datetime.combine(thu, time(9, 30), ET), 20245, NEW)
    lv = Features(bars, level_bars=late).day_levels(thu)
    assert lv.onh is None and lv.onl is None


def test_no_false_sweep_on_a_roll_gap():
    """Old behaviour: the prior-day high (20002, old contract) sits 250 pts under the new price, so
    an ordinary dip on the new contract 'swept' it. With contract-aware levels there is nothing to sweep."""
    bars, thu = roll_series()
    rth = [b for b in bars if b.t.astimezone(ET).date() == thu and b.t.astimezone(ET).time() >= time(9, 30)]
    # one 1-minute dip to 19990 at 9:42: below the OLD contract's 19998 lows, closing back at 20250
    for b in rth:
        tt = b.t.astimezone(ET).time()
        if time(9, 40) <= tt < time(9, 45):
            b.l = 19990.0 if tt == time(9, 42) else b.l
    legacy = Features([dataclasses.replace(b, inst=0) for b in bars])
    fixed = Features(bars)
    def signals(f):
        st = Sweep(StrategyConfig(entry_cutoff=time(11, 30)), 0.25, f)
        st.reset(thu)
        out = []
        for b in rth:
            s = st.on_bar(b)
            if s:
                out.append(s)
                st.register_entry(s.side)
        return out
    assert signals(legacy), "test setup: legacy levels should produce the false sweep"
    assert signals(fixed) == []


def test_bars_without_instrument_ids_behave_as_before():
    bars, thu = roll_series()
    f = Features([dataclasses.replace(b, inst=0) for b in bars])
    assert not f.roll_sessions and f.day_levels(thu).pdh == 20002


# ------------------------------------------------------------------ entry cutoff
def five_min_bars(day, start, n, px=20000.0, sweep_at=None, level=20010.0):
    """1-minute bars; the 5-minute bar beginning at `sweep_at` wicks above `level` and closes below."""
    out, t = [], datetime.combine(day, start, ET)
    for _ in range(n):
        h = px + 1
        if sweep_at and datetime.combine(day, sweep_at, ET) <= t < datetime.combine(day, sweep_at, ET) + timedelta(minutes=5):
            h = level + 3
        out.append(Bar(t, px, h, px - 1, px, 10))
        t += timedelta(minutes=1)
    return out


class Lv:
    def __init__(self, pdh):
        from orbbot.features import Levels
        self.lv = Levels(pdh=pdh)

    def day_levels(self, d):
        return self.lv


@pytest.mark.parametrize("day", [date(2026, 10, 30), date(2026, 11, 2)])  # EDT and EST
@pytest.mark.parametrize("cutoff,bar_start,expect", [
    (time(11, 0), time(10, 55), True),    # bar closes 11:00 = cutoff -> allowed
    (time(11, 0), time(11, 0), False),    # bar closes 11:05 -> too late
    (time(11, 30), time(11, 25), True),
    (time(11, 30), time(11, 30), False),
])
def test_sweep_honours_configured_cutoff(day, cutoff, bar_start, expect):
    st = Sweep(StrategyConfig(entry_cutoff=cutoff), 0.25, Lv(20010.0))
    st.reset(day)
    sigs = [s for b in five_min_bars(day, time(10, 30), 75, sweep_at=bar_start) if (s := st.on_bar(b))]
    assert bool(sigs) is expect
    if sigs:
        assert sigs[0].time == datetime.combine(day, bar_start, ET) + timedelta(minutes=5)
        assert sigs[0].time.utcoffset() == (timedelta(hours=-4) if day.month == 10 else timedelta(hours=-5))


def test_sweep_and_orb_use_the_same_cutoff_field():
    cfg = StrategyConfig(entry_cutoff=time(10, 45))
    assert Sweep(cfg, 0.25, Lv(None)).last_entry == time(10, 45)
    o = ORBStrategy(cfg)
    o.reset(date(2026, 10, 30))
    assert o._at(o.cfg.entry_cutoff).time() == time(10, 45)


def test_cutoff_validation():
    with pytest.raises(AssertionError):
        validate(BotConfig(strategy=StrategyConfig(entry_cutoff=time(12, 0), flatten_time=time(11, 55))))
    with pytest.raises(AssertionError):
        validate(BotConfig(strategy=StrategyConfig(entry_cutoff=time(9, 0))))
