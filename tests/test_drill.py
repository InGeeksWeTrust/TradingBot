"""Practice-account execution drill (orbbot/drill.py) against the fake broker and fake clock."""
from datetime import datetime, time, timedelta

import pytest

from fakes import DAY, FakeBroker, FakeClock, make_cfg
from orbbot.drill import ALLOWED_ACCOUNT_ID, ALLOWED_ACCOUNT_NAME, Drill, Refused, side_for
from orbbot.state import StateStore
from orbbot.strategy import ET

PRAC = {"id": ALLOWED_ACCOUNT_ID, "name": ALLOWED_ACCOUNT_NAME, "balance": 150_000.0, "canTrade": True}


class PracBroker(FakeBroker):
    accounts = [PRAC]

    def search_accounts(self, only_active=True):
        self._chk()
        return [dict(a) for a in self.accounts]


def _drill(tmp_path, at=time(13, 0), on_sleep=None, typed=ALLOWED_ACCOUNT_NAME, broker_cls=PracBroker, **faults):
    clock = FakeClock(datetime.combine(DAY, at, ET))
    broker = broker_cls(clock)
    for k, v in faults.items():
        setattr(broker, k, v)
    out, sleeps = [], []

    def sleep(s):
        sleeps.append(s)
        clock.advance(s)
        if on_sleep:
            on_sleep(broker, len(sleeps))
    cfg = make_cfg(tmp_path)
    d = Drill(cfg, api=broker, clock=clock, sleep=sleep, ask=lambda prompt: typed, root=tmp_path, out=out.append)
    return d, broker, cfg, out


def _hit_target_once(broker, n):
    if n == 3 and broker.pos() is not None:
        broker.hit_target()


def _states(tmp_path):
    s = StateStore(tmp_path / "state-drill" / "drill.sqlite")
    try:
        it = s.intents_for_day(DAY)[0]
        return it, [t[1] for t in s.transitions(it["tag"])], s.forward_rows()[0], s.events()
    finally:
        s.close()


def _paper_untouched(tmp_path):
    return not (tmp_path / "state" / "paper.sqlite").exists() and not (tmp_path / "state" / "paper_broker.json").exists() \
        and not (tmp_path / "journal").exists()


# ---------------------------------------------------------------- happy path
def test_drill_happy_path_opens_protects_closes_and_verifies_flat(tmp_path):
    from orbbot.audit import audit, frozen_fingerprint
    d, broker, cfg, out = _drill(tmp_path, on_sleep=_hit_target_once)
    fp = frozen_fingerprint(cfg)[0]
    assert d.run() == 0
    assert len(broker.place_calls) == 1
    call = broker.place_calls[0]
    assert call["tag"].startswith("DRILL-") and call["size"] == 1
    assert call["side"] == (0 if side_for(DAY) == "long" else 1)
    it, states, fwd, events = _states(tmp_path)
    assert states == ["CREATED", "SUBMITTING", "ACKNOWLEDGED", "FILLED", "PROTECTED", "CLOSED"]
    assert fwd["entry_slippage_ticks"] == 1 and fwd["exit_reason"] == "target" and fwd["net_pnl"] > 0
    assert any(e[1] == "drill_end" and e[2].startswith("flat") for e in events)
    assert broker.pos() is None and not broker.search_open_orders(1)
    text = "\n".join(out)
    assert "submit -> ack" in text and "last price at submit 20050" in text and "FLAT, no working orders" in text
    assert "bracket at broker    stop" in text
    assert (tmp_path / "journal-drill" / "drill.csv").read_text().count(it["tag"]) == 1
    assert _paper_untouched(tmp_path)
    lat = StateStore(tmp_path / "state-drill" / "drill.sqlite")
    names = {r[0] for r in lat.db.execute("SELECT name FROM latency WHERE kind='signal'")}
    lat.close()
    assert {"place_sent", "place_response", "fill_seen", "protected"} <= names
    # audit: its own drill section; Strategy A's result and fingerprint unchanged
    overall, report = audit(cfg, DAY, "paper", root=tmp_path)
    assert overall == "PASS" and "**Drill: PASS**" in report and frozen_fingerprint(cfg)[0] == fp
    assert report.index("## Practice-account drill") > report.index("6 frozen config")


def test_audit_shows_drill_pnl_next_to_the_eod_balance(tmp_path):
    from orbbot.audit import audit
    from orbbot.config import AccountConfig
    d, broker, cfg, out = _drill(tmp_path, on_sleep=_hit_target_once)
    d.run()
    net = _states(tmp_path)[2]["net_pnl"]
    cfg.account = AccountConfig(max_loss_limit=4500, start_balance=150_000)
    paper = StateStore(tmp_path / "state" / "paper.sqlite", clock=lambda: datetime.combine(DAY, time(16, 20), ET))
    paper.event("eod_balance", f"{DAY}: {150_000 + net:.2f} -> floor 145500")
    paper.close()
    overall, report = audit(cfg, DAY, "paper", root=tmp_path)
    assert f"includes Practice drill P&L ${net:+.2f}" in report


# ---------------------------------------------------------------- failures handled
def test_drill_protection_missing_flattens(tmp_path):
    d, broker, cfg, out = _drill(tmp_path, bracket_mode="missing")
    assert d.run() == 0
    it, states, fwd, events = _states(tmp_path)
    assert "PROTECTED" not in states and states[-2:] == ["EXIT_PENDING", "CLOSED"]
    assert broker.close_calls >= 1 and broker.pos() is None
    assert any(e[1] == "protection_failed" for e in events)
    assert fwd["exit_reason"] == "protection missing"
    from orbbot.audit import drill_section
    lines, _ = drill_section(cfg, DAY, tmp_path)
    assert "**Drill: FAIL**" in "\n".join(lines) and "never verified PROTECTED" in "\n".join(lines)


def test_drill_ambiguous_place_reconciles_by_tag_without_resending(tmp_path):
    d, broker, cfg, out = _drill(tmp_path, on_sleep=_hit_target_once, place_mode="timeout_after")
    assert d.run() == 0
    assert len(broker.place_calls) == 1                       # never resent
    it, states, fwd, events = _states(tmp_path)
    assert states[:6] == ["CREATED", "SUBMITTING", "UNKNOWN", "RECONCILING", "FILLED", "PROTECTED"]
    assert states[-1] == "CLOSED"


def test_drill_closes_a_position_still_open_after_ten_minutes(tmp_path):
    d, broker, cfg, out = _drill(tmp_path)
    assert d.run() == 0
    it, states, fwd, events = _states(tmp_path)
    assert "PROTECTED" in states and states[-1] == "CLOSED" and fwd["exit_reason"] == "10-minute limit"
    assert broker.close_calls == 1 and broker.pos() is None


# ---------------------------------------------------------------- refusals (nothing is sent)
def test_drill_refuses_an_account_not_on_the_allowlist(tmp_path):
    d, broker, cfg, out = _drill(tmp_path, broker_cls=FakeBroker)          # id 1 "TEST"
    with pytest.raises(Refused, match="allowlisted account 28771375 not found"):
        d.run()
    assert broker.place_calls == []


def test_drill_refuses_the_allowlisted_id_if_it_is_not_a_practice_account(tmp_path):
    class Renamed(PracBroker):
        accounts = [dict(PRAC, name="EXPRESS-150K-1")]
    d, broker, cfg, out = _drill(tmp_path, broker_cls=Renamed)
    with pytest.raises(Refused, match="not a Practice"):
        d.run()
    assert broker.place_calls == []


def test_drill_refuses_with_an_open_position_or_working_order(tmp_path):
    d, broker, cfg, out = _drill(tmp_path)
    broker.add_position(1, 20000.0)
    with pytest.raises(Refused, match="not flat"):
        d.run()
    d, broker, cfg, out = _drill(tmp_path / "b")
    broker.add_stop(1, 19990.0)
    with pytest.raises(Refused, match="not flat"):
        d.run()
    assert broker.place_calls == []


@pytest.mark.parametrize("at", [time(11, 59), time(15, 0), time(9, 45)])
def test_drill_refuses_outside_the_window(tmp_path, at):
    d, broker, cfg, out = _drill(tmp_path, at=at)
    with pytest.raises(Refused, match="outside the drill window"):
        d.run()
    assert broker.place_calls == []


def test_drill_refuses_while_the_paper_session_is_active(tmp_path):
    d, broker, cfg, out = _drill(tmp_path)
    now = datetime.combine(DAY, time(13, 0), ET)
    paper = StateStore(tmp_path / "state" / "paper.sqlite", clock=lambda: now)
    paper.put(f"alive:{DAY}:4242", (now - timedelta(seconds=30)).isoformat())
    paper.close()
    with pytest.raises(Refused, match="paper session active"):
        d.run()
    assert broker.place_calls == []


def test_drill_refuses_with_an_open_paper_intent(tmp_path):
    d, broker, cfg, out = _drill(tmp_path)
    paper = StateStore(tmp_path / "state" / "paper.sqlite")
    paper.create_intent("sweep-x", DAY, "C", "long", 1, {}, [], ["long"])
    paper.close()
    with pytest.raises(Refused, match="open intent"):
        d.run()
    assert broker.place_calls == []


def test_drill_aborts_unless_the_account_name_is_typed_exactly(tmp_path):
    for typed in ("", "y", ALLOWED_ACCOUNT_NAME.lower(), ALLOWED_ACCOUNT_NAME + " "):
        d, broker, cfg, out = _drill(tmp_path / str(len(typed)), typed=typed)
        with pytest.raises(Refused, match="did not type the account name"):
            d.run()
        assert broker.place_calls == []
        assert "REAL order" in "\n".join(out) and "Bracket   stop 20 ticks" in "\n".join(out)


def test_drill_refuses_a_second_instance(tmp_path):
    from orbbot.bot import acquire_instance_lock
    held = acquire_instance_lock(tmp_path / "state-drill" / "drill.lock")
    try:
        d, broker, cfg, out = _drill(tmp_path)
        with pytest.raises(Refused, match="another drill"):
            d.run()
        assert broker.place_calls == []
    finally:
        held.close()


class NewestFirst(PracBroker):
    """Like ProjectX: History/retrieveBars returns the newest bar first."""

    def retrieve_bars(self, *a, **kw):
        return list(reversed(super().retrieve_bars(*a, **kw)))


def _per_minute(t):
    c = 20000 + t.astimezone(ET).minute          # each minute closes at a different price
    return (c, c + 1, c - 1, c)


def test_drill_last_price_is_the_newest_bar_even_when_bars_come_newest_first(tmp_path):
    d, broker, cfg, out = _drill(tmp_path, on_sleep=_hit_target_once, broker_cls=NewestFirst, price_fn=_per_minute)
    assert d.run() == 0
    fwd = _states(tmp_path)[2]
    assert fwd["signal_price"] == 20000 + 0               # the 13:00 bar, not 12:55 (the window's oldest)
    assert abs(fwd["entry_slippage_ticks"]) == 1          # the fake fills 1 tick from that close
    lat = StateStore(tmp_path / "state-drill" / "drill.sqlite")
    row = lat.db.execute("SELECT bar_t, price FROM latency WHERE name='bars MNQ' ORDER BY id LIMIT 1").fetchone()
    lat.close()
    assert row[0].startswith(f"{DAY}T17:00") and row[1] == 20000   # latency log: newest bar too


def test_drill_refuses_when_the_newest_bar_is_stale(tmp_path):
    d, broker, cfg, out = _drill(tmp_path, bars_stop_at=datetime.combine(DAY, time(12, 55), ET))
    with pytest.raises(Refused, match="newest MNQ bar is 300s old"):
        d.run()
    assert broker.place_calls == []


def test_drill_command_requires_live(monkeypatch):
    from orbbot import drill
    monkeypatch.setattr("sys.argv", ["drill", "--config", "config-sweep.yaml"])
    with pytest.raises(SystemExit, match="--live"):
        drill.main()
