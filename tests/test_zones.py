import dataclasses
from datetime import date, datetime, time, timedelta

from orbbot.backtest import simulate_day
from orbbot.config import BotConfig, RiskConfig
from orbbot.features import Features
from orbbot.research import NASDAQ, Zone
from orbbot.risk import spec_for
from orbbot.strategy import ET, Bar, Signal

DAY = date(2026, 10, 19)
SPEC = spec_for("MNQ")


def mins(start, ohlcs):
    t = datetime.combine(DAY, start, ET)
    out = []
    for o, h, l, c in ohlcs:
        out.append(Bar(t, o, h, l, c, 10))
        t += timedelta(minutes=1)
    return out


class OneShot:
    """Signals once at a given bar index with a given stop level."""
    def __init__(self, at, side, stop):
        self.at, self.side, self.stop, self.i = at, side, stop, -1
    def reset(self, day): self.i = -1
    def register_entry(self, side): pass
    def on_bar(self, b):
        self.i += 1
        if self.i == self.at:
            return Signal(self.side, b.t + timedelta(minutes=1), b.c, b.c, self.stop)
        return None


def run(bars, risk):
    cfg = BotConfig()
    cfg = dataclasses.replace(cfg, strategy=dataclasses.replace(cfg.strategy, flatten_time=time(15, 55)))
    return simulate_day(bars, cfg, SPEC, 10, 0, strat=OneShot(0, "long", 19990.0), risk=risk)


RUNNER = RiskConfig(stop_mode="structural", target_per_trade=2000, max_risk_per_trade=250, partial_r=4.0,
                    max_daily_profit=100000, fee_per_contract_rt=0.0)


def test_runner_half_at_4r_rest_at_8r():
    # entry 20000 (next open), stop 10 pts (19990), 10 ct risk $200 -> partial 20040, runner 20080
    bars = mins(time(10, 0), [(20000, 20000, 20000, 20000), (20000, 20001, 19999, 20000),
                              (20000, 20041, 19999, 20040), (20040, 20081, 20039, 20080)])
    t = run(bars, RUNNER)[0]
    # 5 ct x 40 pts x $2 + 5 ct x 80 pts x $2 = 400 + 800
    assert t.pnl == 1200 and t.reason == "partial+target" and t.contracts == 10


def test_runner_stopped_at_breakeven_keeps_partial():
    bars = mins(time(10, 0), [(20000, 20000, 20000, 20000), (20000, 20001, 19999, 20000),
                              (20000, 20041, 19999, 20040), (20040, 20041, 19999, 20005)])
    t = run(bars, RUNNER)[0]
    assert t.pnl == 400 and t.reason == "partial+stop"   # half at +40 pts, half back at entry


def test_full_loss_before_partial():
    bars = mins(time(10, 0), [(20000, 20000, 20000, 20000), (20000, 20001, 19989, 19995)])
    assert run(bars, RUNNER)[0].pnl == -200


def test_demand_zone_formed_and_retest_entry():
    base = [(20010, 20012, 20000, 20002)]                       # bearish candle = zone 20000-20012
    impulse = [(20002, 20025, 20002, 20024), (20024, 20045, 20023, 20044), (20044, 20065, 20043, 20064)]
    quiet = [(20000 + i % 3, 20001 + i % 3, 19999 + i % 3, 20000 + i % 3) for i in range(20)]
    drift_back = [(20064 - 6 * i, 20065 - 6 * i, 20057 - 6 * i, 20058 - 6 * i) for i in range(8)]
    retest = [(20015, 20016, 20005, 20016), (20014, 20022, 20008, 20020)]  # dips into zone, closes back above 20012
    bars = mins(time(9, 4), quiet + base + impulse + drift_back + retest)     # retest at 9:36-9:37
    f = Features(bars)
    zones = [z for z in f.zones if z["kind"] == "demand"]
    assert zones and (zones[0]["lo"], zones[0]["hi"]) == (20000, 20012)
    s = Zone(None, 0.25, f, window="day", session=NASDAQ)
    s.reset(DAY)
    sigs = [x for b in bars if (x := s.on_bar(b))]
    assert sigs and sigs[0].side == "long" and sigs[0].or_low == 19999.75
