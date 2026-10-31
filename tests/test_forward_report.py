"""Forward-test report (orbbot/forward_report.py): sample selection, statistics, pre-registered looks,
execution, account safety and reproducibility, built from a state database in tmp_path."""
import hashlib
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

from fakes import make_cfg
from orbbot.audit import backup_blocked
from orbbot.forward_report import build, drawdown, losing_streak, z_stat
from orbbot.latency import TABLE as LAT_TABLE
from orbbot.state import StateStore
from orbbot.strategy import ET

START = date(2026, 11, 2)       # a Monday


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.t


def _cfg(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.forward_test.sample_start = START
    cfg.account.max_loss_limit, cfg.account.start_balance = 4500, 150_000
    return cfg


def _store(tmp_path):
    clock = Clock()
    return StateStore(tmp_path / "state" / "paper.sqlite", clock=clock), clock


def _at(day: date, hh=9, mm=45, ss=0) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), ET).astimezone(timezone.utc)


def _trade(store, n, day, net, risk=100.0, side="short", drift=None, modeled=1.0):
    tag = f"sweep-{day:%Y%m%d}-{side}-{n:04d}"
    sig = datetime.combine(day, time(9, 45), ET)
    store.forward(tag, day=day.isoformat(), side=side, level="pdh", signal_time=sig.isoformat(), signal_price=20000.0,
                  fill_time=sig.isoformat(), fill_price=20000.0, fill_qty=1, exit_time=sig.isoformat(),
                  exit_price=20000.0, exit_reason="target" if net > 0 else "stop", net_pnl=net, fees=1.22,
                  planned_risk=risk, modeled_slippage_ticks=modeled, market_drift_ticks=drift, mode="paper")
    return tag


def _event(store, clock, at, kind, detail):
    clock.t = at
    store.event(kind, detail)


def _sessions(store, clock, days, code="abc1234"):
    for d in days:
        _event(store, clock, _at(d, 9, 25), "session_start", f"pid 1; code {code}")


def _weekdays(n, start=START):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _report(tmp_path, asof=date(2026, 11, 30), cfg=None):
    return build(cfg or _cfg(tmp_path), asof, root=tmp_path)


# ---------------------------------------------------------------- pure statistics

def test_statistics_helpers():
    assert drawdown([1, -2, -1, 3, -1]) == (-1.0, -3.0)
    assert drawdown([1, 2]) == (0.0, 0.0)
    assert losing_streak([1, -1, 0, -2, 1, -1]) == (1, 3)
    assert z_stat([1.0]) is None and z_stat([1.0, 1.0]) is None
    assert abs(z_stat([1.0, 2.0, 3.0]) - 2.0 / (1.0 / 3 ** 0.5)) < 1e-9


# ---------------------------------------------------------------- 1-2 sample and results

def test_sample_excludes_operational_week_and_drills(tmp_path):
    store, clock = _store(tmp_path)
    _trade(store, 1, date(2026, 10, 28), 433.9)                     # operational week
    _trade(store, 2, START, 200.0, risk=100.0)
    _trade(store, 3, START + timedelta(days=1), -100.0, risk=100.0)
    _trade(store, 4, START + timedelta(days=2), 50.0, risk=100.0)
    store.forward("open-one", day=START.isoformat(), side="long", signal_time="2026-11-02T10:00:00-05:00",
                  fill_qty=1, mode="paper")                         # filled, not closed
    store.close()
    drill = StateStore(tmp_path / "state-drill" / "drill.sqlite", clock=Clock())
    drill.forward("DRILL-x", day=START.isoformat(), side="short", signal_time="2026-11-02T13:00:00-05:00",
                  fill_qty=1, exit_time="x", net_pnl=-10.72, planned_risk=10.0, mode="drill")
    drill.close()
    text = _report(tmp_path)
    assert "Trades: **3** closed (+1 not yet closed, not counted)" in text
    assert "Net P&L: **$+150.00**" in text
    assert "mean **+0.500R**" in text and "total +1.50R" in text
    assert "Win rate: 2/3 = 67%" in text
    assert "Profit factor: 2.50" in text
    assert "433.90" not in text and "DRILL-x" not in text
    assert "Practice-drill P&L on: 2026-11-02" in text


def test_skipped_signals_by_reason_deduplicated(tmp_path):
    store, clock = _store(tmp_path)
    d = START
    _event(store, clock, _at(d, 9, 40), "signal_filtered", "['pdl', 'onl'] at 09:39 (confirmation disagreed)")
    _event(store, clock, _at(d, 9, 50), "signal_skipped", "short pdh at 09:50: stop_too_wide")
    _event(store, clock, _at(d, 9, 51), "signal_skipped", "short pdh at 09:50: stop_too_wide")   # restart replay
    _event(store, clock, _at(d, 10, 0), "signal_skipped", "long onl at 10:00: daily_headroom")
    _event(store, clock, _at(d, 10, 5), "signal_blocked", "long pdl at 10:05: halted for the day")
    _event(store, clock, _at(d, 10, 6), "signal_blocked", "long onh at 10:06: stale bars")
    _event(store, clock, _at(d, 10, 7), "signal_stale", "short onh at 09:31 (bot started after it)")
    _event(store, clock, _at(date(2026, 10, 30), 10, 0), "signal_skipped", "x y at 10:00: stop_too_wide")  # before
    store.close()
    text = _report(tmp_path, asof=START)
    assert "Signals not traded: 7" in text
    assert "filtered by ES confirmation: 2" in text
    assert "stop_too_wide: 1" in text
    assert "daily limits (daily_headroom): 1" in text and "daily limits (halted for the day): 1" in text
    assert "stale data: 1" in text and "stale: bot started after the signal: 1" in text


# ---------------------------------------------------------------- 3 drawdown and streak

def test_drawdown_and_streak_thresholds(tmp_path):
    store, clock = _store(tmp_path)
    days = _weekdays(26)
    for i, d in enumerate(days):
        _trade(store, i, d, -100.0, risk=100.0)                     # 26 straight -1R losses
    store.close()
    text = _report(tmp_path, asof=days[-1])
    assert "Drawdown in R: current -26.00R, max -26.00R" in text
    assert "Drawdown in $: current $-2,600.00" in text
    assert "| Max drawdown, first 100 trades | 26 of 100 | -26.00R | -20R: **REACHED** | -26R: **REACHED** |" in text
    assert "| Max drawdown, first 200 trades | 26 of 200 | -26.00R | -26R: **REACHED** | -34R: not reached |" in text
    assert "| Longest losing streak | 26 | 26 | 12: **REACHED** | 15: **REACHED** |" in text


# ---------------------------------------------------------------- 4 sequential test

def _many(tmp_path, rs):
    store, clock = _store(tmp_path)
    days = _weekdays(len(rs))
    for i, (d, r) in enumerate(zip(days, rs)):
        _trade(store, i, d, r * 100.0, risk=100.0)
    store.close()
    return days[-1]


def test_no_decision_before_100_trades(tmp_path):
    last = _many(tmp_path, [1.0, -0.5] * 20)
    text = _report(tmp_path, asof=last)
    assert "n = 40, z = " in text
    assert "**NO decision is made until a checkpoint.**" in text and "(60 to go)" in text
    assert "SUCCEED" not in text and "**FAIL**" not in text


def test_look_1_succeeds_at_100_trades(tmp_path):
    rs = [2.0, -0.5] * 50                                            # mean 0.75, sd ~1.26: z ~5.9
    last = _many(tmp_path, rs)
    text = _report(tmp_path, asof=last)
    assert "NO decision" not in text
    assert f"Look 1 (100 trades): z = {z_stat(rs):+.3f} vs succeed >= 2.538: **SUCCEED**" in text


def test_look_2_fail_boundary(tmp_path):
    rs = [-1.0, 0.7] * 100                                           # mean -0.15: z over 200 ~ -2.5
    last = _many(tmp_path, rs)
    text = _report(tmp_path, asof=last)
    z100 = z_stat(rs[:100])
    assert f"Look 1 (100 trades): z = {z100:+.3f} vs succeed >= 2.538: no decision: continue to 200 trades" in text
    z200 = z_stat(rs)
    assert z200 <= -1.645
    assert f"Look 2 (200 trades): z = {z200:+.3f} vs succeed >= 1.662, fail <= -1.645: **FAIL**" in text


# ---------------------------------------------------------------- 5 execution

def _lat(tmp_path, rows):
    db = sqlite3.connect(tmp_path / "state" / "paper.sqlite")
    db.executescript(LAT_TABLE)
    db.executemany("INSERT INTO latency(day, kind, name, poll, tag, sig, start_at, price) VALUES (?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()


def _exec_sample(tmp_path, n, drift_ticks):
    store, clock = _store(tmp_path)
    days = _weekdays(n)
    rows = []
    for i, d in enumerate(days):
        tag = _trade(store, i, d, 50.0, side="long")
        bar_close, poll = _at(d, 9, 46, 0), _at(d, 9, 46, 2)
        rows += [(d.isoformat(), "signal", "bar_close", None, tag, "k", bar_close.isoformat(), None),
                 (d.isoformat(), "signal", "recognized", poll.isoformat(), tag, "k",
                  (poll + timedelta(milliseconds=400)).isoformat(), None),
                 (d.isoformat(), "signal", "place_sent", poll.isoformat(), tag, "k",
                  (poll + timedelta(milliseconds=500)).isoformat(), None),
                 (d.isoformat(), "poll", "poll", poll.isoformat(), None, None, poll.isoformat(),
                  20000.0 + drift_ticks * 0.25)]                     # long: price above signal = worse
    store.close()
    _lat(tmp_path, rows)
    return days[-1]


def test_drift_to_submit_from_latency_and_flag(tmp_path):
    last = _exec_sample(tmp_path, 20, 12)
    text = _report(tmp_path, asof=last)
    assert "signal close -> order sent" in text and "mean +12.00 ticks, sd 0.00, n 20" in text
    assert "Modeled paper slippage: mean +1.00 ticks" in text
    assert "**FLAG: mean drift above +11 ticks after 20+ trades" in text
    assert "bar close -> Order/place sent: p50 2500 ms, p95 2500 ms (n 20)" in text
    assert "Signal recognized -> Order/place sent: p50 100 ms" in text


def test_drift_flag_needs_20_trades(tmp_path):
    last = _exec_sample(tmp_path, 19, 12)
    text = _report(tmp_path, asof=last)
    assert "FLAG" not in text and "not raised (needs 20+ trades with a measured drift; have 19)" in text


# ---------------------------------------------------------------- 6 account safety, 7 reproducibility

def test_account_safety_and_audits(tmp_path):
    store, clock = _store(tmp_path)
    days = [START, START + timedelta(days=1)]
    _sessions(store, clock, days)
    store.put("peak_eod_balance", 151_000.0)
    _event(store, clock, _at(days[0], 16, 15), "eod_balance", f"{days[0]}: 151000.00 -> floor 146500.0")
    _event(store, clock, _at(days[1], 10, 0), "signal_blocked", "long pdl at 10:00: worst case $300 > $200 above the MLL buffer")
    _event(store, clock, _at(days[1], 11, 55), "flatten_start", "session flatten time")
    _event(store, clock, _at(days[1], 10, 30), "flatten_start", "protection missing")
    store.close()
    (tmp_path / "audit").mkdir()
    (tmp_path / "audit" / f"{days[0]}.md").write_text(
        "# Operational audit\n\n**Overall: WARN** - IN\n\n| Area | Status | Detail |\n| --- | --- | --- |\n"
        "| 1 cross-store | PASS | fine |\n| 5 events | WARN | restart during session |\n"
        "\n## Practice-account drill (separate)\n\n| Status | Detail |\n| --- | --- |\n| FAIL | drill row |\n")
    text = _report(tmp_path, asof=days[1])
    assert "MLL floor $146,500.00" in text
    assert "Headroom above floor + $500 buffer: $4,000.00" in text
    assert "Guard blocks (MLL): 1" in text
    assert "Flattens: 2 (1 routine at the session flatten time)" in text and "protection missing" in text
    assert "- 2026-11-02: WARN\n    - WARN 5 events: restart during session" in text
    assert "drill row" not in text
    assert f"**no audit file for {days[1]}**" in text


def test_code_version_change_and_fingerprint(tmp_path):
    store, clock = _store(tmp_path)
    _sessions(store, clock, [START], code="aaa1111")
    _sessions(store, clock, [START + timedelta(days=1)], code="bbb2222 (dirty)")
    from orbbot.audit import frozen_fingerprint
    cfg = _cfg(tmp_path)
    store.put("frozen_fingerprint", frozen_fingerprint(cfg)[0])
    store.close()
    text = _report(tmp_path, asof=START + timedelta(days=1), cfg=cfg)
    assert "Code versions at session start: 2 **(changed during the sample: review)**" in text
    assert "bbb2222 (dirty): 2026-11-03 .. 2026-11-03 (1 session day(s)) **(uncommitted changes)**" in text
    assert "frozen baseline" in text and ": match" in text
    cfg.risk.max_risk_per_trade = 300
    assert "**DIFFERENT: frozen settings changed**" in _report(tmp_path, asof=START, cfg=cfg)


def test_report_is_read_only(tmp_path):
    store, clock = _store(tmp_path)
    _trade(store, 1, START, 100.0)
    store.close()
    db = tmp_path / "state" / "paper.sqlite"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    _report(tmp_path)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert not (tmp_path / "reports").exists()          # build() returns text; only main() writes the file


# ---------------------------------------------------------------- audit backup timing

def test_backup_not_run_inside_the_session_window(tmp_path):
    cfg = _cfg(tmp_path)
    mon = date(2026, 11, 2)
    assert backup_blocked(cfg, datetime.combine(mon, time(10, 0), ET))
    assert backup_blocked(cfg, datetime.combine(mon, time(11, 59), ET))
    assert backup_blocked(cfg, datetime.combine(mon, time(12, 1), ET)) is None
    assert backup_blocked(cfg, datetime.combine(mon, time(16, 30), ET)) is None
    assert backup_blocked(cfg, datetime.combine(date(2026, 11, 7), time(10, 0), ET)) is None   # Saturday
