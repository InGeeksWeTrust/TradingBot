"""Phase 4A follow-ups: latency log, equity_after, paper-uncertain flag, 1-second price-check windows.
All logging/record changes; no signal, sizing or risk behaviour is touched."""
import importlib.util
import logging
import sqlite3
from datetime import date, datetime, time

from fakes import DAY, Scenario
from orbbot.state import SCHEMA, StateStore
from orbbot.strategy import ET
from test_phase5 import _paper_scenario, _two_sweeps


def _traded_day(tmp_path, entry_minute=None, until=(11, 56)):
    """Short 5 MNQ at 09:45 (fill 20049.75, stop 20070.00); target hit at 09:48."""
    market, paper, clock = _paper_scenario(tmp_path)
    base = _two_sweeps

    def price(t):
        et = t.astimezone(ET).time()
        if time(9, 48) <= et < time(9, 50):
            return (19900, 19905, 19895, 19900)
        if entry_minute and et == time(9, 45):
            return entry_minute
        return base(t)
    market.price_fn = price
    sc = Scenario(tmp_path, broker=paper, clock=clock)
    sc.run_until(*until, step=5)
    sc.close()
    return sc


def _rows(tmp_path, sql, args=()):
    db = sqlite3.connect(tmp_path / "state" / "paper.sqlite")
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(sql, args)]
    finally:
        db.close()


# ---------------------------------------------------------------- 1. latency log
def test_latency_log_records_api_calls_polls_and_every_signal_stage(tmp_path):
    from orbbot.latency import STAGES
    from orbbot.latency_report import report
    _traded_day(tmp_path, until=(9, 46, 30))
    api = {r["name"] for r in _rows(tmp_path, "SELECT name FROM latency WHERE kind='api'")}
    assert {"bars MNQ", "positions", "orders", "account", "trades", "place"} <= api
    polls = _rows(tmp_path, "SELECT * FROM latency WHERE kind='poll'")
    assert polls and all(p["bar_t"] and p["price"] for p in polls[-5:])
    tag = _rows(tmp_path, "SELECT tag FROM intents")[0]["tag"]
    st = {r["name"]: datetime.fromisoformat(r["start_at"]) for r in
          _rows(tmp_path, "SELECT name, start_at FROM latency WHERE kind='signal' AND tag=?", (tag,))}
    assert list(st) == STAGES                                   # all nine, in order
    assert st["bar_close"] == datetime.combine(DAY, time(9, 45), ET)
    assert (st["fill_seen"] - st["place_response"]).total_seconds() == 5    # seen at the next poll
    assert all(a <= b for a, b in zip(st.values(), list(st.values())[1:]))
    text = report(tmp_path / "state" / "paper.sqlite")
    assert "bars MNQ" in text and "new minute's bar first received" in text
    assert "bar_close -> poll_start" in text and "bar_close -> protected (total)" in text


def test_latency_write_failure_is_dropped_and_never_blocks_entries(tmp_path, caplog):
    from orbbot.latency import Latency, TimedApi
    store = StateStore(tmp_path / "s.sqlite", clock=lambda: datetime.combine(DAY, time(9, 45), ET))
    lat = Latency(store, DAY, store.clock)
    store.db.execute("DROP TABLE latency")
    lat.begin_poll()
    lat.stage("recognized", store.clock(), sig="x")
    with caplog.at_level(logging.WARNING):
        lat.end_poll(None, None)
    assert store.healthy and lat.rows == [] and "trading is unaffected" in caplog.text

    class Api:
        slip_ticks = 1

        def search_open_positions(self, account_id):
            raise ConnectionError("down")
    timed = TimedApi(Api(), lat, {})
    assert timed.slip_ticks == 1                                # attributes pass through
    try:
        timed.search_open_positions(1)
    except ConnectionError:
        pass
    else:
        raise AssertionError("the call's own error must propagate")
    assert lat.rows[-1][1:3] == ("api", "positions") and lat.rows[-1][9] == 0
    store.close()


# ---------------------------------------------------------------- 2. equity_after
def test_equity_after_is_equity_before_plus_net(tmp_path):
    _traded_day(tmp_path)
    r = _rows(tmp_path, "SELECT equity_before, net_pnl, equity_after, note FROM forward")[0]
    assert r["equity_before"] is not None and r["net_pnl"] > 0
    assert r["equity_after"] == round(r["equity_before"] + r["net_pnl"], 2) and r["note"] is None


def test_rows_written_before_the_fix_get_a_note_once_values_unchanged(tmp_path):
    p = tmp_path / "old.sqlite"
    db = sqlite3.connect(p)
    db.executescript(SCHEMA)                                    # the original forward columns only
    db.execute("INSERT INTO forward(tag, equity_before, net_pnl, equity_after) VALUES ('a', 150000, 433.9, 149996.95)")
    db.execute("INSERT INTO forward(tag, equity_before) VALUES ('b', 150433.9)")       # still open
    db.commit()
    db.close()
    StateStore(p).close()
    rows = {r["tag"]: r for r in StateStore(p).forward_rows()}
    assert rows["a"]["equity_after"] == 149996.95                                   # kept as recorded
    assert rows["a"]["note"] == ("equity_after recorded from a stale account snapshot (before the fix); "
                                 "equity_before + net_pnl = 150433.90")
    assert rows["b"]["note"] is None
    s = StateStore(p)
    s.forward("c", equity_before=1.0, net_pnl=2.0, equity_after=3.0)                # a new-code row
    s.close()
    assert {r["tag"]: r for r in StateStore(p).forward_rows()}["c"]["note"] is None  # note added only once


# ---------------------------------------------------------------- 3. paper-uncertain
def test_audit_flags_paper_uncertain_entry_minute_as_info_only(tmp_path):
    from orbbot.audit import audit
    sc = _traded_day(tmp_path, entry_minute=(20050, 20080, 20048, 20050))   # high 20080 >= stop 20070
    r = sc.bot.store.__class__(tmp_path / "state" / "paper.sqlite").forward_rows()[0]
    assert (r["entry_bar_high"], r["entry_bar_low"], r["stop_price"]) == (20080, 20048, 20070.0)
    assert r["exit_reason"] == "target"                       # the paper broker did not stop it out
    overall, text = audit(sc.cfg, DAY, "paper", root=tmp_path)
    assert overall == "PASS", text
    assert "| 3 lifecycle | INFO |" in text and "paper-uncertain: the entry minute's high 20080" in text


def test_audit_no_paper_uncertain_flag_when_the_entry_minute_stays_clear(tmp_path):
    from orbbot.audit import audit
    sc = _traded_day(tmp_path)
    overall, text = audit(sc.cfg, DAY, "paper", root=tmp_path)
    assert overall == "PASS" and "paper-uncertain" not in text


# ---------------------------------------------------------------- 4. 1-second price check (no network)
def _price_script():
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "scripts" / "price_1s.py"
    spec = importlib.util.spec_from_file_location("price_1s", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_price_check_windows_are_0930_1200_et_across_dst():
    m = _price_script()
    assert m.window(date(2025, 7, 1)) == ("2025-07-01T13:30:00+00:00", "2025-07-01T16:00:00+00:00")   # EDT
    assert m.window(date(2025, 12, 1)) == ("2025-12-01T14:30:00+00:00", "2025-12-01T17:00:00+00:00")  # EST
    days = m.weekdays(date(2024, 10, 21), date(2026, 10, 20))
    assert days[0] == date(2024, 10, 21) and days[-1] == date(2026, 10, 20) and len(days) == 522
    assert all(d.weekday() < 5 for d in days)
    assert len(m.signal_days()) == 250


def test_price_check_sums_one_quote_per_day():
    m = _price_script()
    calls = []

    class Meta:
        def get_cost(self, **kw):
            calls.append(kw)
            return 0.5

    class Client:
        metadata = Meta()
    assert m.cost(Client(), "MNQ.v.0", [date(2025, 7, 1), date(2025, 7, 2)]) == 1.0
    assert {c["schema"] for c in calls} == {"ohlcv-1s"} and {c["stype_in"] for c in calls} == {"continuous"}
    assert {c["dataset"] for c in calls} == {"GLBX.MDP3"} and all(c["symbols"] == ["MNQ.v.0"] for c in calls)
