"""Phase 5 execution-safety tests: fault injection against the real Session loop (tests/fakes.py).
Every test runs in pytest's tmp_path: its own state database, journal, logs and kill-switch files."""
import sqlite3
from datetime import datetime, time, timedelta, timezone

import pytest

from fakes import CID, DAY, PDH, FakeBroker, FakeClock, Scenario, make_cfg
from orbbot.api import ORDER_MARKET, STATUS_OPEN, AmbiguousError, ApiError
from orbbot.bot import ORBBot
from orbbot.execution import ALLOWED, S, TransitionError, verify_protection
from orbbot.state import StateError, StateStore
from orbbot.strategy import ET


def entries(sc):
    return [c for c in sc.broker.place_calls if c["type"] == ORDER_MARKET]


# ---------------------------------------------------------------- 1-4: submission and reconciliation
def test_01_timeout_after_accepted_order_is_found_not_resent(tmp_path):
    sc = Scenario(tmp_path)
    sc.broker.place_mode = "timeout_after"
    sc.run_until(9, 46)
    assert len(entries(sc)) == 1                                  # never resent
    assert sc.states()[:6] == ["CREATED", "SUBMITTING", "UNKNOWN", "RECONCILING", "FILLED", "PROTECTED"]
    assert sc.broker.pos()["size"] == 5 and not sc.eng.block_reasons


def test_02_timeout_before_acceptance_resolves_to_cancelled_after_grace(tmp_path):
    sc = Scenario(tmp_path)
    sc.broker.place_mode = "timeout_before"
    sc.run_until(9, 45, 20)
    assert "unknown_order" in sc.eng.block_reasons                # blocked while unknown
    assert sc.states()[-1] == "RECONCILING"                       # UNKNOWN is never read as "rejected"
    sc.run_until(9, 46)
    assert sc.states()[-1] == "CANCELLED" and sc.eng.active is None
    assert len(entries(sc)) == 1 and sc.broker.pos() is None
    assert sc.events("reconcile_absent")


def test_03_duplicate_tag_answer_is_reconciled_and_store_refuses_duplicate_intents(tmp_path):
    sc = Scenario(tmp_path)
    sc.broker.place_mode = "dup_tag"         # order exists, the answer says "duplicate customTag"
    sc.run_until(9, 46)
    assert len(entries(sc)) == 1
    assert "UNKNOWN" in sc.states() and sc.states()[-1] == "PROTECTED"
    tag = entries(sc)[0]["tag"]
    with pytest.raises(StateError):          # the same intent id can't be recorded twice
        sc.bot.store.create_intent(tag, DAY, CID, "short", 5, {}, [], [])


def test_04_delayed_acknowledgement_keeps_entries_blocked_until_visible(tmp_path):
    sc = Scenario(tmp_path)
    sc.broker.place_mode = "pending"         # errorCode 6: still processing
    sc.broker.order_visible_after = 3        # the order only shows in Order/search on the 4th look
    sc.broker.fill_qty = 0                   # and has not filled yet
    sc.run_until(9, 45, 6)
    assert sc.states()[-1] == "RECONCILING" and "unknown_order" in sc.eng.block_reasons
    sc.run_until(9, 45, 25)
    assert sc.states()[-1] == "ACKNOWLEDGED" and sc.eng.active is not None   # working order: still blocks
    sc.run_until(9, 50)
    assert len(entries(sc)) == 1


def test_04b_ambiguous_answer_but_position_exists_counts_as_filled(tmp_path):
    sc = Scenario(tmp_path)
    sc.broker.place_mode = "pending"
    sc.broker.order_visible_after = 99       # the order never shows up, but the position does
    sc.run_until(9, 46)
    assert sc.states()[2:5] == ["UNKNOWN", "RECONCILING", "FILLED"] and len(entries(sc)) == 1
    assert sc.events("reconcile_position_without_order")


# ---------------------------------------------------------------- 5-9: protection
def test_05_partial_entry_fill_with_full_size_bracket_is_flattened(tmp_path):
    sc = Scenario(tmp_path)
    sc.broker.fill_qty = 2                   # 2 of 5 filled; bracket still for 5
    sc.run_until(9, 46)
    assert "PARTIALLY_FILLED" in sc.states() and sc.states()[-1] == "CLOSED"
    assert sc.broker.pos() is None and sc.sess.halted
    assert any("stop quantity 5 != position size 2" in e[2] for e in sc.events("protection_failed"))


def test_06_partial_exit_leaves_oversized_stop_so_remainder_is_flattened(tmp_path):
    sc = Scenario(tmp_path)
    sc.run_until(9, 45, 30)
    assert sc.states()[-1] == "PROTECTED"
    sc.broker.hit_target(qty=2)              # target fills 2 of 5; stop still 5 -> could reverse us
    sc.run_until(9, 46)
    assert sc.broker.pos() is None and sc.states()[-1] == "CLOSED"
    assert all(p["size"] >= 0 for p in sc.broker.positions)


@pytest.mark.parametrize("mode,problem", [("missing", "no working stop"), ("wrong_qty", "stop quantity 7"),
                                          ("wrong_side", "wrong side")])
def test_07_08_09_bad_bracket_is_detected_and_flattened(tmp_path, mode, problem):
    sc = Scenario(tmp_path)
    sc.broker.bracket_mode = mode
    sc.run_until(9, 45, 10)                  # inside the 15 s grace: not yet acted on
    assert sc.broker.pos() is not None and sc.states()[-1] == "FILLED"
    sc.run_until(9, 46)
    assert sc.broker.pos() is None and sc.states()[-1] == "CLOSED"
    assert any(problem in e[2] for e in sc.events("protection_failed"))
    assert not [o for o in sc.broker.orders if o["status"] == STATUS_OPEN]   # leftovers cancelled
    assert len(entries(sc)) == 1                                            # no new entry after it


def test_late_bracket_inside_grace_is_accepted(tmp_path):
    sc = Scenario(tmp_path)
    sc.broker.bracket_delay = 2
    sc.run_until(9, 46)
    assert sc.states()[-1] == "PROTECTED" and sc.broker.pos()["size"] == 5


def test_verify_protection_unit_cases():
    pos = {"contractId": CID, "type": 1, "size": 3, "averagePrice": 20000.0}
    stop = {"id": 1, "contractId": CID, "type": 4, "side": 1, "size": 3, "stopPrice": 19990.0, "status": 1}
    assert verify_protection(pos, [stop], 0.25, 19990.0, 4).ok
    assert not verify_protection(pos, [dict(stop, stopPrice=20005.0)], 0.25, None, 4).ok       # profit side
    assert not verify_protection(pos, [dict(stop, stopPrice=19980.0)], 0.25, 19990.0, 4).ok    # 40 ticks off
    assert not verify_protection(pos, [dict(stop, status=3)], 0.25, None, 4).ok                # cancelled
    assert not verify_protection(pos, [dict(stop, contractId="X")], 0.25, None, 4).ok          # other contract


# ---------------------------------------------------------------- 10-12: restart recovery
def test_10_restart_while_holding_a_protected_position(tmp_path):
    sc = Scenario(tmp_path)
    sc.run_until(9, 45, 30)
    tag = entries(sc)[0]["tag"]
    sc.close()
    sc2 = Scenario(tmp_path, broker=sc.broker, clock=sc.clock)      # same broker, same state file
    assert sc2.eng.active["tag"] == tag and sc2.eng.active["state"] == "PROTECTED"
    sc2.broker.hit_target()
    sc2.run_until(9, 47)
    assert len(entries(sc2)) == 1 and sc2.states(tag)[-1] == "CLOSED"
    assert len(sc2.bot.journal.rows()) == 1


def test_11_restart_with_entry_order_still_pending_cancels_it(tmp_path):
    clock = FakeClock(datetime.combine(DAY, time(9, 45), ET))
    broker = FakeBroker(clock)
    cfg = make_cfg(tmp_path)
    store = StateStore(tmp_path / "state" / "paper.sqlite", clock=lambda: clock().astimezone(timezone.utc))
    store.create_intent("sweep-x", DAY, CID, "short", 5, {"stop_pts": 20.25, "target_pts": 40.5}, ["pdh"], ["short"])
    store.set_state("sweep-x", "SUBMITTING", "CREATED")
    store.set_state("sweep-x", "ACKNOWLEDGED", "SUBMITTING", broker_order_id=500)
    store.close()
    broker.orders.append({"id": 500, "accountId": 1, "contractId": CID, "creationTimestamp": broker.now().isoformat(),
                          "updateTimestamp": broker.now().isoformat(), "status": STATUS_OPEN, "type": 1, "side": 1,
                          "size": 5, "limitPrice": 20200.0, "stopPrice": None, "fillVolume": 0, "filledPrice": None,
                          "customTag": "sweep-x"})
    clock.advance(60)
    sc = Scenario(tmp_path, broker=broker, cfg=cfg, clock=clock)
    assert 500 in broker.cancel_calls
    assert sc.states("sweep-x")[-1] == "CANCELLED" and sc.eng.active is None and broker.pos() is None
    sc.run_until(9, 50)
    assert not entries(sc)                   # pdh was already used: no new entry


def test_12_restart_after_a_level_was_traded_never_retrades_it(tmp_path):
    sc = Scenario(tmp_path)
    sc.run_until(9, 45, 30)
    sc.broker.hit_target()
    sc.run_until(9, 46)
    sc.close()
    # restart later in the morning with fresh process memory; the whole morning replays
    sc2 = Scenario(tmp_path, broker=sc.broker, clock=sc.clock)
    sc2.run_until(9, 50)
    assert len(entries(sc2)) == 1
    assert "pdh" in sc2.bot.store.used_levels(DAY) and sc2.sess.inner.n >= 1


# ---------------------------------------------------------------- 13-17: data, account and risk
def test_13_stale_market_data_with_open_position(tmp_path):
    sc = Scenario(tmp_path, stale_position_flatten_s=120)
    sc.run_until(9, 45, 30)
    sc.broker.bars_stop_at = datetime.combine(DAY, time(9, 45), ET)
    sc.run_until(9, 47)                      # < 3 min stale + 2 min: no action on a stale price
    assert sc.broker.pos() is not None and not sc.sess.halted
    sc.run_until(9, 52)                      # stale for > bars_max_age + 120 s -> flatten
    assert sc.broker.pos() is None and sc.sess.halted


def test_13b_stale_data_blocks_new_entries(tmp_path):
    sc = Scenario(tmp_path)
    sc.broker.bars_stop_at = datetime.combine(DAY, time(9, 35), ET)
    sc.run_until(9, 46)
    assert not entries(sc)


def test_14_account_equity_unavailable_blocks_entries_when_mll_is_on(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.account.max_loss_limit = 2000
    clock = FakeClock(datetime.combine(DAY, time(9, 30, 30), ET))
    broker = FakeBroker(clock)
    broker.account_down = True
    with pytest.raises(Exception):
        Scenario(tmp_path, broker=broker, cfg=cfg, clock=clock)   # can't even pick the account
    broker.account_down = False
    sc = Scenario(tmp_path, broker=broker, cfg=cfg, clock=clock)
    broker.account_down = True
    sc.run_until(9, 46)
    assert not entries(sc)
    assert any("account balance" in e[2] for e in sc.events("signal_blocked"))


def test_14b_account_guard_blocks_a_trade_that_could_breach_the_mll(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.account.max_loss_limit = 2000
    cfg.account.start_balance = 50_000
    cfg.account.safety_buffer = 500
    clock = FakeClock(datetime.combine(DAY, time(9, 30, 30), ET))
    broker = FakeBroker(clock, balance=48_600)        # floor 48,000 + 500 buffer: $100 room
    sc = Scenario(tmp_path, broker=broker, cfg=cfg, clock=clock)
    sc.run_until(9, 46)
    assert not entries(sc)
    assert any("MLL" in e[2] for e in sc.events("signal_blocked"))


def test_15_unexpected_positions(tmp_path):
    clock = FakeClock(datetime.combine(DAY, time(9, 30, 30), ET))
    broker = FakeBroker(clock)
    broker.add_position(2, 20000.0, long=True)
    broker.add_stop(2, 19980.0, sell=True)
    sc = Scenario(tmp_path, broker=broker, clock=clock)           # protected: keep it, but no entries
    assert broker.pos() is not None and "unexpected_position" in sc.eng.block_reasons
    sc.run_until(9, 46)
    assert not entries(sc)
    sc.close()
    broker2 = FakeBroker(FakeClock(datetime.combine(DAY, time(9, 30, 30), ET)))
    broker2.add_position(2, 20000.0, long=True)                   # unprotected: flatten
    sc2 = Scenario(tmp_path / "b", broker=broker2, clock=broker2.clock)
    assert broker2.pos() is None and sc2.sess.halted
    broker3 = FakeBroker(FakeClock(datetime.combine(DAY, time(9, 30, 30), ET)))
    broker3.add_position(1, 6000.0, cid="CON.F.US.MES.Z26")       # another contract: block entries
    sc3 = Scenario(tmp_path / "c", broker=broker3, clock=broker3.clock)
    sc3.run_until(9, 46)
    assert not entries(sc3) and "other_contract" in sc3.eng.block_reasons


@pytest.mark.parametrize("mode", ["reject", "timeout"])
def test_16_emergency_flatten_failure_is_bounded_and_reported(tmp_path, mode):
    sc = Scenario(tmp_path)
    sc.broker.bracket_mode = "missing"
    sc.broker.close_mode = mode
    sc.run_until(9, 45, 25)                      # protection fails at ~9:45:20: one bounded attempt
    assert sc.broker.close_calls == sc.cfg.safety.flatten_attempts
    assert sc.eng.critical and "flatten_failed" in sc.eng.block_reasons
    sc.run_until(9, 46, 30)                      # keeps retrying every 30 s, never more often
    assert sc.broker.close_calls == 3 * sc.cfg.safety.flatten_attempts
    sc.broker.close_mode = "ok"                  # broker recovers: the next retry succeeds
    sc.run_until(9, 47, 30)
    assert sc.broker.pos() is None and "flatten_failed" not in sc.eng.block_reasons
    assert sc.events("flatten_failed")
    assert len(entries(sc)) == 1                 # nothing else was ever sent to "fix" it
    assert all(c["type"] == ORDER_MARKET for c in sc.broker.place_calls)


def test_17_daily_loss_guard_under_a_fast_move(tmp_path):
    sc = Scenario(tmp_path)
    sc.run_until(9, 45, 30)
    base = sc.broker.price_fn
    sc.broker.price_fn = lambda t: (20300, 20320, 20290, 20310) if t.astimezone(ET).time() >= time(9, 46) else base(t)
    sc.broker.bracket_mode = "ok"
    sc.run_until(9, 47, 10)                      # 260 pts against a 5-lot short = -$2,600 open
    assert sc.broker.pos() is None and sc.sess.halted
    assert "daily loss" in sc.bot.store.day(DAY)["halt_reason"]


def test_17b_sizing_respects_remaining_daily_headroom(tmp_path):
    from orbbot.risk import plan_trade, spec_for
    from orbbot.strategy import Signal
    cfg = make_cfg(tmp_path)
    sig = Signal("short", datetime.combine(DAY, time(9, 45), ET), 20090.0, 20110.25, 20090.0)
    p = plan_trade(sig, cfg.risk, spec_for("MNQ"), -400.0, 10)   # $75 left after the $25 buffer
    assert p.contracts == 1 and p.worst_case_loss <= 75


# ---------------------------------------------------------------- 18-20: cutoff, persistence, outage
def _late_price(t):
    """Like the default, but the sweep bar is 10:55-10:59 ET (closes at 11:00, the cutoff)."""
    et = t.astimezone(ET)
    if et.date() < DAY:
        return (20000, PDH, 19990, 20000) if et.time() == time(14, 0) else (20000, 20005, 19995, 20000)
    if time(10, 55) <= et.time() < time(11, 0):
        return (20090, 20110, 20088, 20092) if et.time() == time(10, 57) else (20090, 20094, 20088, 20090)
    return (20050, 20052, 20048, 20050)


@pytest.mark.parametrize("restart_at,expect", [(time(11, 1), 1), (time(11, 3), 0)])
def test_18_restart_near_the_1100_cutoff(tmp_path, restart_at, expect):
    clock = FakeClock(datetime.combine(DAY, restart_at, ET))
    broker = FakeBroker(clock)
    broker.price_fn = _late_price
    sc = Scenario(tmp_path, broker=broker, clock=clock)
    sc.run_until(11, 6)
    assert len(entries(sc)) == expect


def test_19a_corrupt_state_file_is_moved_aside_and_blocks_entries(tmp_path):
    p = tmp_path / "state" / "paper.sqlite"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"this is not a database" * 100)
    sc = Scenario(tmp_path)
    assert sc.bot.store.recovered_from_corruption
    assert list(p.parent.glob("paper.sqlite.corrupt-*"))
    sc.run_until(9, 46)
    assert not entries(sc) and "state_lost" in sc.eng.block_reasons


def test_19b_state_write_failure_means_nothing_is_sent(tmp_path, monkeypatch):
    sc = Scenario(tmp_path)

    def boom(*a, **k):
        sc.bot.store.healthy = False
        raise StateError("disk I/O error")
    monkeypatch.setattr(sc.bot.store, "create_intent", boom)
    sc.run_until(9, 46)
    assert not entries(sc) and "state" in sc.eng.block_reasons


def test_20_temporary_api_outage_while_in_a_position(tmp_path):
    sc = Scenario(tmp_path)
    sc.run_until(9, 45, 30)
    sc.broker.down = True
    sc.run_until(9, 46, 30)                      # nothing can be checked: no flatten, no entries
    assert sc.broker.pos() is not None and not sc.sess.halted
    sc.broker.down = False
    sc.broker.hit_target()                       # the bracket worked while we were blind
    sc.run_until(9, 47, 30)
    tag = entries(sc)[0]["tag"]
    assert sc.states(tag)[-1] == "CLOSED" and len(sc.bot.journal.rows()) == 1
    assert len(entries(sc)) == 1


# ---------------------------------------------------------------- state machine, modes, isolation
def test_state_machine_rejects_illegal_transitions(tmp_path):
    fixed = datetime.combine(DAY, time(9, 45), ET)
    store = StateStore(tmp_path / "s.sqlite", clock=lambda: fixed)
    from orbbot.execution import ExecutionEngine
    from orbbot.config import SafetyConfig
    eng = ExecutionEngine(None, store, 1, None, SafetyConfig())
    store.create_intent("t1", DAY, CID, "long", 1, {}, [], ["long"])
    it = store.intent("t1")
    with pytest.raises(TransitionError):
        eng.transition(it, S.FILLED)             # CREATED -> FILLED skips submission
    assert S.REJECTED not in ALLOWED[S.UNKNOWN]  # unknown is never turned into "rejected" directly
    eng.transition(it, S.SUBMITTING)
    eng.transition(it, S.UNKNOWN)
    with pytest.raises(TransitionError):
        eng.transition(it, S.REJECTED)


def test_live_trading_is_disabled_by_default(tmp_path):
    cfg = make_cfg(tmp_path)
    with pytest.raises(SystemExit, match="Live trading is disabled"):
        ORBBot(cfg, mode="live", api=FakeBroker(FakeClock(datetime.now(ET))))


def test_no_mode_flag_refuses_to_start(monkeypatch):
    import sys
    from orbbot import bot
    monkeypatch.setattr(sys, "argv", ["orbbot.bot", "--config", "config.yaml"])
    with pytest.raises(SystemExit, match="Choose a mode"):
        bot.main()


def test_tests_never_touch_real_journal_or_state(tmp_path):
    sc = Scenario(tmp_path)
    sc.run_until(9, 46)
    assert str(sc.bot.store.path).startswith(str(tmp_path))
    assert str(sc.bot.journal.csv).startswith(str(tmp_path))


def test_api_never_resends_order_place(monkeypatch):
    import requests
    from orbbot.api import ProjectXClient
    c = ProjectXClient("u", "k")
    c._token_time = 1e18
    calls = []

    def post(*a, **k):
        calls.append(1)
        raise requests.Timeout("read timed out")
    monkeypatch.setattr(c.s, "post", post)
    with pytest.raises(AmbiguousError):
        c.place_order(1, CID, ORDER_MARKET, 0, 1, custom_tag="x")
    assert len(calls) == 1


def test_api_error_ambiguity_classification():
    assert ApiError("Order/place", 6, "pending").ambiguous
    assert ApiError("Order/place", 7, None).ambiguous
    assert ApiError("Order/place", 2, "customTag must be unique").ambiguous
    assert not ApiError("Order/place", 2, "Invalid size").ambiguous
    assert not ApiError("Order/place", 5, "Outside trading hours").ambiguous


# ---------------------------------------------------------------- review round 2
def test_21_real_paper_broker_survives_a_restart(tmp_path):
    """The actual PaperClient (not the fake broker): its simulated account is on disk, so a restarted
    bot finds the same position and bracket, verifies it, and the exit is still recorded."""
    from orbbot.paper import PaperClient
    clock = FakeClock(datetime.combine(DAY, time(9, 30, 30), ET))
    market = FakeBroker(clock)                         # only supplies accounts, contracts and bars
    path = tmp_path / "state" / "paper_broker.json"
    paper = PaperClient(market, fee_per_contract_rt=1.22, state_path=path, clock=clock)
    sc = Scenario(tmp_path, broker=paper, clock=clock)
    sc.run_until(9, 45, 30)
    tag = sc.bot.store.forward_rows()[0]["tag"]
    assert sc.states(tag)[-1] == "PROTECTED" and paper.pos["size"] == 5
    sc.close()
    del paper                                          # the process dies; only the files remain
    clock.advance(120)
    paper2 = PaperClient(market, fee_per_contract_rt=1.22, state_path=path, clock=clock)
    assert paper2.pos["size"] == 5 and len(paper2.orders) == 2
    sc2 = Scenario(tmp_path, broker=paper2, clock=clock)
    assert sc2.eng.active["tag"] == tag and sc2.eng.active["state"] == "PROTECTED"
    base = market.price_fn                             # price falls through the short's target
    market.price_fn = lambda t: (19900, 19905, 19895, 19900) if t.astimezone(ET).time() >= time(9, 48) else base(t)
    sc2.run_until(9, 50)
    assert paper2.pos is None and sc2.states(tag)[-1] == "CLOSED"
    rows = sc2.bot.store.forward_rows()
    assert rows[0]["exit_reason"] == "target" and rows[0]["fill_source"] == "paper-sim"
    assert rows[0]["modeled_slippage_ticks"] == 1 and len(sc2.bot.journal.rows()) == 1
    assert len([o for o in paper2.history if o["type"] == ORDER_MARKET]) == 1     # never re-entered


def test_21b_paper_state_file_is_atomic_and_never_silently_reset(tmp_path):
    from orbbot.paper import PaperClient
    path = tmp_path / "paper_broker.json"
    path.write_text("{ half a fil")
    with pytest.raises(SystemExit, match="unreadable"):
        PaperClient(None, state_path=path)


@pytest.mark.parametrize("race", ["race_bracket_first", "race_stop_after"])
def test_22_close_contract_racing_a_bracket_fill(tmp_path, race):
    sc = Scenario(tmp_path)
    sc.run_until(9, 45, 30)
    assert sc.states()[-1] == "PROTECTED"
    sc.broker.close_mode = race
    (tmp_path / "STOP").write_text("")                 # emergency flatten while the bracket is live
    sc.run_until(9, 46)
    assert sc.broker.pos() is None                     # flat in the end, whichever filled first
    assert not [o for o in sc.broker.orders if o["status"] == STATUS_OPEN and o["type"] != ORDER_MARKET]
    assert len(entries(sc)) == 1                       # no order other than closeContract was ever sent
    if race == "race_stop_after":
        assert getattr(sc.broker, "reversed", False)   # the leftover stop did reverse us...
        assert sc.broker.close_calls == 2              # ...and the verify loop closed that too


def test_23_confirmation_market_cannot_be_substituted(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.confirm_symbol = "MES"
    cfg.safety.required_confirm_symbol = "ES"
    with pytest.raises(SystemExit, match="must be ES"):
        ORBBot(cfg, mode="paper", api=FakeBroker(FakeClock(datetime.combine(DAY, time(9, 30), ET))))
    cfg.confirm_symbol = "ES"                          # ES missing at the broker: refuse, don't fall back
    b = ORBBot(cfg, mode="paper", api=FakeBroker(FakeClock(datetime.combine(DAY, time(9, 30), ET))))
    with pytest.raises(SystemExit, match="No active ES contract"):
        b.connect()                                    # the fake only lists MNQZ6; MESZ6 would not match ^ES


def test_24_end_of_day_balance_only_after_the_310_pm_ct_close(tmp_path):
    from orbbot.config import AccountConfig
    from orbbot.execution import AccountGuard
    from zoneinfo import ZoneInfo
    ct = ZoneInfo("America/Chicago")
    store = StateStore(tmp_path / "a.sqlite", clock=lambda: datetime.combine(DAY, time(16, 0), ct))
    g = AccountGuard(AccountConfig(max_loss_limit=2000, start_balance=50_000, safety_buffer=500), store)
    assert g.floor() == 48_000
    assert not g.end_of_day(DAY, 51_000, datetime.combine(DAY, time(10, 55), ct))   # 11:55 ET: too early
    assert g.floor() == 48_000
    assert g.end_of_day(DAY, 51_000, datetime.combine(DAY, time(15, 11), ct))       # after the close
    assert g.floor() == 49_000                                                         # trails up 1,000
    assert not g.end_of_day(DAY, 52_500, datetime.combine(DAY, time(15, 40), ct))   # once per day
    nxt = DAY + timedelta(days=3)
    assert g.end_of_day(nxt, 52_500, datetime.combine(nxt, time(15, 20), ct))
    assert g.floor() == 50_000                                                         # locks at start
    g.update_balance(50_400)
    assert g.headroom(0.0) == 50_400 - 50_000 - 500                                    # inside the buffer


def test_25_old_state_files_gain_the_new_forward_columns(tmp_path):
    import sqlite3 as sq
    p = tmp_path / "old.sqlite"
    from orbbot.state import FORWARD_ADDED, SCHEMA
    db = sq.connect(p)
    db.executescript(SCHEMA)                       # the schema as released on Oct 22 (no new columns)
    db.commit()
    assert not {r[1] for r in db.execute("PRAGMA table_info(forward)")} & set(FORWARD_ADDED)
    db.close()
    s = StateStore(p)
    s.forward("t", fill_source="paper-sim", market_drift_ticks=2.0)
    assert s.forward_rows()[0]["fill_source"] == "paper-sim"


# ---------------------------------------------------------------- operational-week checks (review round 3)
def _paper_scenario(tmp_path, clock=None):
    from orbbot.paper import PaperClient
    clock = clock or FakeClock(datetime.combine(DAY, time(9, 30, 30), ET))
    market = FakeBroker(clock)
    paper = PaperClient(market, fee_per_contract_rt=1.22, state_path=tmp_path / "state" / "paper_broker.json",
                        clock=clock)
    return market, paper, clock


def _restart(tmp_path, market, clock):
    from orbbot.paper import PaperClient
    paper = PaperClient(market, fee_per_contract_rt=1.22, state_path=tmp_path / "state" / "paper_broker.json",
                        clock=clock)
    return paper, Scenario(tmp_path, broker=paper, clock=clock)


def test_26a_crash_after_intent_recorded_before_sending(tmp_path):
    market, paper, clock = _paper_scenario(tmp_path)
    st = StateStore(tmp_path / "state" / "paper.sqlite", clock=lambda: clock().astimezone(timezone.utc))
    st.create_intent("sweep-crash-a", DAY, CID, "short", 5, {"stop_pts": 20.25, "target_pts": 40.5}, ["pdh"], ["short"])
    st.close()
    clock.advance(60)
    paper2, sc = _restart(tmp_path, market, clock)
    assert sc.states("sweep-crash-a")[-1] == "CANCELLED" and paper2.pos is None and not paper2.history
    sc.run_until(9, 50)
    assert not paper2.history                       # pdh stays used: nothing is ever sent


def test_26b_crash_after_broker_accepted_before_ack_was_recorded(tmp_path):
    market, paper, clock = _paper_scenario(tmp_path)
    clock.t = datetime.combine(DAY, time(9, 45, 2), ET)
    paper.retrieve_bars(CID, clock() - timedelta(minutes=5), clock(), include_partial=True)   # a price
    st = StateStore(tmp_path / "state" / "paper.sqlite", clock=lambda: clock().astimezone(timezone.utc))
    st.create_intent("sweep-crash-b", DAY, CID, "short", 5, {"stop_pts": 20.25, "target_pts": 40.5}, ["pdh"], ["short"])
    st.set_state("sweep-crash-b", "SUBMITTING", "CREATED")
    st.close()
    paper.set_contract(CID, 0.25, 2.0)
    paper.place_order(1, CID, ORDER_MARKET, 1, 5, sl_ticks=81, tp_ticks=-162, custom_tag="sweep-crash-b")
    del paper                                        # process dies before "ACKNOWLEDGED" is written
    clock.advance(30)
    paper2, sc = _restart(tmp_path, market, clock)
    st = sc.states("sweep-crash-b")
    assert st[:3] == ["CREATED", "SUBMITTING", "UNKNOWN"] and st[-1] == "PROTECTED" and "FILLED" in st, st
    sc.run_until(9, 50)
    assert len([o for o in paper2.history if o["type"] == ORDER_MARKET]) == 1


def test_26c_crash_after_bracket_exit_before_close_was_recorded(tmp_path):
    market, paper, clock = _paper_scenario(tmp_path)
    sc = Scenario(tmp_path, broker=paper, clock=clock)
    sc.run_until(9, 45, 30)
    tag = sc.bot.store.forward_rows()[0]["tag"]
    tgt = next(o for o in paper.orders if o["type"] == 1)
    paper._exit(tgt["limitPrice"], tgt["id"], "target")   # fill saved to disk...
    paper._close_legs(tgt)
    sc.close()                                            # ...then the process dies before seeing it
    paper2, sc2 = _restart(tmp_path, market, clock)
    assert sc2.states(tag)[-1] == "CLOSED"
    rows = sc2.bot.store.forward_rows()
    assert rows[0]["exit_reason"] == "target" and len(sc2.bot.journal.rows()) == 1   # backfilled once
    sc2.run_until(9, 47)
    assert len(sc2.bot.journal.rows()) == 1


def _two_sweeps(t):
    """pdh 20100 swept at 9:40-9:44 and again at 10:00-10:04."""
    from fakes import default_price
    et = t.astimezone(ET)
    if et.date() == DAY and time(10, 0) <= et.time() < time(10, 5):
        return (20090, 20110, 20088, 20092) if et.time() == time(10, 2) else (20090, 20094, 20088, 20090)
    return default_price(t)


def test_27_filtered_sweep_uses_up_its_level(tmp_path):
    from orbbot.filters import Filtered
    from orbbot.research import NASDAQ, Sweep
    from fakes import FakeFeats
    from orbbot.features import Levels
    sc = Scenario(tmp_path)
    sc.broker.price_fn = _two_sweeps
    agree = {"v": False}
    sess = sc.sess

    def build(sess=sess):
        sw = Sweep(sess.s, 0.25, FakeFeats(Levels(pdh=PDH)), window="am", session=NASDAQ, max_trades=2)
        sw.reset(DAY)
        sess.inner = sw
        sess._preload()
        sess.strat = Filtered(sw, [lambda sig: agree["v"]])
    sess.strat = None
    sess.build_sweep = build
    sc.run_until(9, 46)                    # 9:45 sweep: confirmation disagrees
    assert not entries(sc) and "pdh" in sc.bot.store.used_levels(DAY)
    assert sc.events("signal_filtered")
    agree["v"] = True
    sc.run_until(10, 10)                   # the same level swept again with confirmation: not traded
    assert not entries(sc)                 # (backtest does the same: research.Sweep marks it used first)


def test_28_approximate_floor_widens_the_buffer(tmp_path):
    from orbbot.config import AccountConfig
    from orbbot.execution import AccountGuard
    from zoneinfo import ZoneInfo
    ct = ZoneInfo("America/Chicago")
    store = StateStore(tmp_path / "a.sqlite", clock=lambda: datetime.combine(DAY, time(8, 0), ct))
    g = AccountGuard(AccountConfig(max_loss_limit=4500, start_balance=150_000, safety_buffer=500,
                                   approx_floor_margin=250), store)
    prev = DAY - timedelta(days=1)
    g.end_of_day(prev, 151_000, datetime.combine(DAY, time(8, 0), ct), approximate=True)
    g.update_balance(151_000)
    assert g.floor_approximate and g.buffer() == 750
    assert g.headroom(0.0) == 151_000 - 146_500 - 750
    g.end_of_day(DAY, 151_000, datetime.combine(DAY, time(15, 20), ct))          # a real EOD balance
    assert not g.floor_approximate and g.buffer() == 500
    g2 = AccountGuard(AccountConfig(max_loss_limit=4500, mll_floor=146_000, safety_buffer=500), store)
    store.put("last_eod_approx", True)
    assert not g2.floor_approximate and g2.floor() == 146_000                      # dashboard wins


def test_29_time_flatten_exit_is_journaled(tmp_path):
    sc = Scenario(tmp_path)
    sc.run_until(9, 45, 30)
    sc.run_until(11, 56, step=60)
    rows = sc.bot.store.forward_rows()
    assert sc.broker.pos() is None and rows[0]["exit_time"] and rows[0]["exit_reason"] == "time"
    assert len(sc.bot.journal.rows()) == 1


def test_30_daily_audit_passes_a_clean_day_and_catches_problems(tmp_path):
    import csv as _csv
    from orbbot.audit import audit
    market, paper, clock = _paper_scenario(tmp_path)
    market.price_fn = _two_sweeps
    sc = Scenario(tmp_path, broker=paper, clock=clock)
    sc.run_until(9, 45, 30)
    base = market.price_fn                          # the short reaches its target at 9:48
    market.price_fn = lambda t: (19900, 19905, 19895, 19900) if time(9, 48) <= t.astimezone(ET).time() < time(9, 50) else base(t)
    sc.run_until(11, 56, step=30)
    sc.close()
    overall, text = audit(sc.cfg, DAY, "paper", root=tmp_path)
    assert overall == "PASS", text
    assert "pdh" in text and "NOT in the statistical sample" in text
    # tamper 1: a journal row goes missing -> FAIL
    jp = tmp_path / "journal" / "paper.csv"
    rows = list(_csv.DictReader(jp.open()))
    jp.write_text(",".join(rows[0].keys()) + "\n")
    overall, text = audit(sc.cfg, DAY, "paper", root=tmp_path)
    assert overall == "FAIL" and "journal rows 0 != closed trades 1" in text
    # tamper 2: a frozen setting changes -> FAIL
    sc.cfg.risk.max_risk_per_trade = 300
    overall, text = audit(sc.cfg, DAY, "paper", root=tmp_path)
    assert "CONFIG CHANGED" in text


def test_31_audit_missing_paper_file_passes_a_quiet_day_and_fails_with_intents(tmp_path):
    from orbbot.audit import audit
    quiet = tmp_path / "quiet"                      # no intents, no paper file -> PASS
    overall, text = audit(make_cfg(quiet), DAY, "paper", root=quiet)
    assert "| 1 cross-store | PASS | no paper broker activity today |" in text
    market, paper, clock = _paper_scenario(tmp_path)
    market.price_fn = _two_sweeps
    sc = Scenario(tmp_path, broker=paper, clock=clock)
    sc.run_until(9, 45, 30)
    sc.close()
    (tmp_path / "state" / "paper_broker.json").unlink()   # intents but no paper file -> FAIL
    overall, text = audit(sc.cfg, DAY, "paper", root=tmp_path)
    assert overall == "FAIL" and "| 1 cross-store | FAIL | no paper account file but 1 entry intent(s) today |" in text
