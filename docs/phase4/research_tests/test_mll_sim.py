"""Deterministic checks of the research simulator's intraday equity and Topstep MLL logic (Phase 4D pre-check).

Run: python -m pytest /home/claude/phase4/test_mll_sim.py -q
"""
import dataclasses
import sys
from datetime import date, datetime

import numpy as np
import pytest

sys.path.insert(0, "/home/claude/phase1")
sys.path.insert(0, "/home/claude/orb-bot")
from harness2 import Exec, sim  # noqa: E402
from phase3 import back_to_back, mc, replay_all_starts  # noqa: E402
from orbbot.config import load_config  # noqa: E402
from orbbot.risk import spec_for  # noqa: E402
from orbbot.strategy import ET, Bar, Signal  # noqa: E402

DAY = date(2025, 3, 3)
CFG = load_config("/home/claude/orb-bot/config-sweep.yaml")
RISK = dataclasses.replace(CFG.risk, stop_mode="structural", max_risk_per_trade=250, target_per_trade=500,
                           max_daily_loss=500, max_daily_profit=750, size_with_costs=True, est_slippage_ticks=1)
SPEC = spec_for("MNQ")
EX = Exec(fee=1.22, slip=1)


def t(h, m):
    return datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=ET)


class Stub:
    """Emits pre-set signals: {bar_open_time: Signal}."""

    def __init__(self, sigs):
        self.sigs, self.inner, self.tags, self.state = sigs, self, {}, {}

    def reset(self, day):
        pass

    def register_entry(self, side):
        pass

    def on_bar(self, b):
        return self.sigs.get(b.t)


def bars(rows):
    return [Bar(t(h, m), o, hi, lo, c, 100) for (h, m, o, hi, lo, c) in rows]


def long_sig(at, price=20000.0, stop=19900.0):
    # signal bar opening at `at` closes one minute later; stop 100 pts -> 1 MNQ at $250 incl. costs
    return Signal("long", at.replace(minute=at.minute + 1), price, 0.0, stop)


# ------------------------------------------------------------------ 1. intraday equity from sim()
def test_dip_then_target_records_the_dip_not_the_later_profit():
    sigs = {t(9, 34): long_sig(t(9, 34))}
    b = bars([(9, 34, 20000, 20001, 19999, 20000),
              (9, 35, 20000, 20005, 19995, 20000),      # entry at open 20000 + 1 tick = 20000.25
              (9, 36, 20000, 20000, 19920, 19930),      # dip: -80.25 pts = -$160.50, minus $1.22 fee
              (9, 37, 19930, 20210, 19930, 20205),      # target 20200.25 hit: +$400 - $1.22
              (11, 56, 20205, 20205, 20205, 20205)])
    r = sim(b, CFG, SPEC, 10, Stub(sigs), RISK, EX)
    assert len(r.trades) == 1 and r.trades[0].n == 1 and r.trades[0].reason == "target"
    assert r.pnl == pytest.approx(400.0 - 1.22)
    assert r.min_eq == pytest.approx(-160.50 - 1.22)    # equity at the worst moment; later profit not netted


def test_profit_first_then_dip_is_measured_from_realized():
    s2 = Signal("long", t(9, 41), 20200.0, 0.0, 20100.0)
    sigs = {t(9, 34): long_sig(t(9, 34)), t(9, 40): s2}
    b = bars([(9, 34, 20000, 20001, 19999, 20000),
              (9, 35, 20000, 20205, 20000, 20201),       # trade 1 target 20200.25 hit in its entry minute
              (9, 36, 20201, 20202, 20199, 20200),
              (9, 40, 20200, 20201, 20199, 20200),
              (9, 41, 20200, 20201, 20120, 20125),       # trade 2: entry 20200.25, dip to 20120 = -80.25 pts
              (9, 42, 20125, 20126, 20101, 20110),       # stop 20100.25 not hit (low 20101)
              (11, 56, 20110, 20110, 20110, 20110)])
    r = sim(b, CFG, SPEC, 10, Stub(sigs), RISK, EX)
    assert [x.reason for x in r.trades] == ["target", "time"]
    # The day's lowest equity is trade 1's first tick (1-tick entry slippage: -$0.50 - $1.22 fee).
    # Trade 2's dip is measured on top of trade 1's realized profit: 398.78 + (20101 - 20200.25) x $2 - 1.22
    # = +$199.06, which is above -$1.72, so it must NOT become the minimum (no netting, no double counting).
    assert r.min_eq == pytest.approx(-0.50 - 1.22)
    assert r.pnl == pytest.approx((400.0 - 1.22) + (20110 - 0.25 - 20200.25) * 2 - 1.22)   # time exit at 11:56 open - 1 tick


# ------------------------------------------------------------------ 2. MLL rules on day series
def run_replay(pnl, mineq, size="50K"):
    return replay_all_starts(np.array(pnl, float), np.array(mineq, float), size)[0]


def test_intraday_breach_then_recovery_fails():
    # day 1 leaves the balance at 48,100 (floor 48,000); day 2 dips $161.72 intraday then ends +$398.78
    res, k = run_replay([-1900, 398.78], [-1900, -161.72])
    assert (res, k) == ("fail", 2)


def test_same_day_survives_with_enough_room():
    res, _ = run_replay([-1800, 398.78], [-1800, -161.72])
    assert res == "open"                                   # 48,200 - 161.72 = 48,038.28 > 48,000


def test_hit_counts_as_breach():
    assert run_replay([0.0], [-2000.0])[0] == "fail"      # equity exactly at the floor = breach
    assert run_replay([0.0], [-1999.99])[0] == "open"


def test_floor_trails_eod_and_locks_at_start():
    # +1,500 EOD raises the floor to 49,500; next day an intraday dip to 49,500 fails
    assert run_replay([1500, 0], [0, -2000])[0] == "fail"
    # +2,900 EOD would put the floor at 50,900, but it locks at 50,000; a dip to 50,001 survives
    assert run_replay([2900, 0], [0, -2899])[0] == "open"
    assert run_replay([2900, 0], [0, -2900])[0] == "fail"


def test_no_double_counting_of_the_balance():
    # mineq is relative to the start of the day; a breach is balance + mineq, not balance + pnl + mineq
    assert run_replay([500, 0], [0, -1999])[0] == "open"  # floor 48,500 (500 above the start); 50,500 - 1,999 = 48,501
    assert run_replay([500, 0], [0, -2000])[0] == "fail"


def test_pass_needs_target_on_eod_balance():
    assert run_replay([1000, 1000, 999.99], [0, 0, 0])[0] == "open"
    assert run_replay([1000, 1000, 1000], [0, 0, 0])[0] == "pass"


def test_monte_carlo_matches_deterministic_path():
    rng = np.random.default_rng(1)
    p, f, o, d = mc(np.array([100.0]), np.array([-2000.0]), "50K", 50, 40, 5, rng)
    assert f == 1.0
    p, f, o, d = mc(np.array([100.0]), np.array([-1999.99]), "50K", 50, 40, 5, rng)
    assert p == 1.0 and set(d.tolist()) == {30}           # +$100/day reaches $3,000 on day 30


def test_back_to_back_restarts_after_fail():
    outs = back_to_back(np.array([-2000.0, 3000.0]), np.array([-2000.0, 0.0]), "50K")
    assert outs == [("fail", 1), ("pass", 1)]
