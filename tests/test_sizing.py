"""Structural-stop position sizing, with and without costs in the budget (review section 4)."""
import dataclasses
from datetime import datetime

import pytest

from orbbot.config import RiskConfig
from orbbot.risk import plan_trade, spec_for
from orbbot.strategy import ET, Signal

MNQ, MES = spec_for("MNQ"), spec_for("MES")
T = datetime(2026, 10, 22, 9, 45, tzinfo=ET)
BASE = RiskConfig(stop_mode="structural", max_risk_per_trade=250, target_per_trade=500, contracts=10,
                  max_contracts=10, max_daily_loss=500, max_daily_profit=750, fee_per_contract_rt=1.22)
WITH_COSTS = dataclasses.replace(BASE, size_with_costs=True, est_slippage_ticks=1)


def short(stop_pts, price=20000.0):
    """Short at `price` with the structural stop `stop_pts` above it."""
    return Signal("short", T, price, price + stop_pts, price)


def plan(sig, cfg, spec=MNQ, realized=0.0):
    why = []
    p = plan_trade(sig, cfg, spec, realized, cfg.contracts, why)
    return p, why


def test_stop_is_never_moved_only_size_changes():
    for cfg in (BASE, WITH_COSTS):
        p, _ = plan(short(30.0), cfg)
        assert p.stop_points == 30.0            # the structural level is kept
        assert p.contracts == 4                 # 4 x 30 pts x $2 = $240
        assert p.worst_case_loss <= 250 or cfg is BASE


def test_costs_in_budget_reduce_size_and_cap_worst_case():
    p0, _ = plan(short(31.0), BASE)            # $62/contract -> 4 contracts, $248 stop risk
    p1, _ = plan(short(31.0), WITH_COSTS)      # $62 + $1 slip + $1.22 fee = $64.22 -> 3 contracts
    assert (p0.contracts, p1.contracts) == (4, 3)
    assert p0.worst_case_loss > 250             # the old sizing can lose more than the budget
    assert p1.worst_case_loss <= 250


def test_unusually_tight_stop_widened_to_minimum_then_capped_at_max_contracts():
    p, _ = plan(short(0.5), WITH_COSTS)        # 2 ticks -> raised to the 8-tick minimum (2 pts)
    assert p.stop_ticks == 8 and p.stop_points == 2.0
    assert p.contracts == 10                    # max_contracts caps it; risk $40 is well under budget
    assert p.worst_case_loss <= 250


def test_unusually_wide_stop_is_rejected_not_sized_over_budget():
    p, why = plan(short(130.0), BASE)          # 1 contract = $260 > $250
    assert p is None and why == ["stop_too_wide"]
    p, why = plan(short(124.0), WITH_COSTS)    # $248 + $2.22 costs = $250.22 > $250
    assert p is None and why == ["stop_too_wide"]
    assert plan(short(124.0), BASE)[0].contracts == 1


@pytest.mark.parametrize("budget,stop_pts,expect", [(75, 36.0, 1), (75, 37.0, 0), (100, 48.0, 1), (100, 49.0, 0)])
def test_minimum_one_contract_at_small_budgets(budget, stop_pts, expect):
    cfg = dataclasses.replace(WITH_COSTS, max_risk_per_trade=budget, max_daily_loss=2 * budget,
                              target_per_trade=2 * budget)
    p, why = plan(short(stop_pts), cfg)
    assert (p.contracts if p else 0) == expect
    if not p:
        assert why == ["stop_too_wide"]


def test_daily_headroom_limits_size_and_reports_reason():
    p, _ = plan(short(10.0), WITH_COSTS, realized=-400.0)   # $75 left after the $25 buffer
    assert p.contracts == 3                                 # 3 x ($20 + $2.22) = $66.66
    p, why = plan(short(10.0), WITH_COSTS, realized=-480.0)
    assert p is None and why == ["daily_headroom"]


def test_size_reduction_note_names_the_binding_limit():
    p, _ = plan(short(22.0), WITH_COSTS)                    # $46.22/ct: cap allows 5, headroom $475 allows 10
    assert p.contracts == 5 and p.note == "size reduced 10->5 (bound by trade risk cap $250)"
    p, _ = plan(short(10.0), WITH_COSTS, realized=-400.0)   # $22.22/ct: cap allows 11, headroom $75 allows 3
    assert p.contracts == 3 and p.note == "size reduced 10->3 (bound by daily headroom $75)"
    p = plan_trade(short(2.0), WITH_COSTS, MNQ, 0.0, 15)    # asked for 15, max_contracts is 10
    assert p.contracts == 10 and p.note == "size reduced 15->10 (bound by max_contracts 10)"


def test_target_is_2r_of_the_kept_stop_never_resized_for_profit():
    p, _ = plan(short(25.0), WITH_COSTS)
    assert p.target_points == 50.0 and p.contracts == 4


def test_mes_sizing():
    p, _ = plan(short(9.0, 6000.0), WITH_COSTS, spec=MES)  # $45 + $2.50 + $1.22 = $48.72 -> 5
    assert p.contracts == 5 and p.worst_case_loss <= 250
