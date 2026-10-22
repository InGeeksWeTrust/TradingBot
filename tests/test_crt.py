from datetime import date, datetime, time, timedelta

from orbbot.config import RiskConfig
from orbbot.features import Features
from orbbot.research import CRT, NASDAQ
from orbbot.risk import plan_trade, spec_for
from orbbot.strategy import ET, Bar, Signal

DAY = date(2026, 10, 19)


def hour(h, o, hi, lo, c):
    """Sixty 1-min bars making one hourly candle: open o, high hi, low lo, close c."""
    t = datetime.combine(DAY, time(h, 0), ET)
    bars = []
    for m in range(60):
        px = o if m < 20 else (lo if m < 30 else (hi if m < 40 else c))
        bars.append(Bar(t + timedelta(minutes=m), px, px, px, px, 10))
    bars[0].o = o
    bars[-1].c = c
    return bars


def test_bullish_crt_detected_at_sweep_close_with_mid_target():
    bars = hour(9, 20050, 20060, 20000, 20020) + hour(10, 20020, 20040, 19990, 20010)  # sweeps low only, closes inside
    f = Features(bars)
    pat = f.crt_at(60, datetime.combine(DAY, time(11, 0), ET))
    assert pat and pat["side"] == "long" and (pat["ph"], pat["pl"], pat["sl"]) == (20060, 20000, 19990)
    s = CRT(None, 0.25, f, window="day", session=NASDAQ, tf=60, target="mid")
    s.reset(DAY)
    sigs = [x for b in bars if (x := s.on_bar(b))]
    assert len(sigs) == 1 and sigs[0].side == "long" and sigs[0].target == 20030 and sigs[0].or_low == 19989.75


def test_no_crt_when_both_sides_swept_or_close_outside():
    both = hour(9, 20050, 20060, 20000, 20020) + hour(10, 20020, 20070, 19990, 20010)
    outside = hour(9, 20050, 20060, 20000, 20020) + hour(10, 20020, 20040, 19990, 19995)
    for bars in (both, outside):
        assert Features(bars).crt_at(60, datetime.combine(DAY, time(11, 0), ET)) is None


def test_level_target_mode_uses_strategy_price():
    sig = Signal("long", datetime.now(ET), 20010, 20010, 19989.75, target=20030)
    p = plan_trade(sig, RiskConfig(stop_mode="structural", target_mode="level", max_daily_profit=5000),
                   spec_for("MNQ"), 0.0, 10)
    assert p.target_points == 20 and p.stop_points == 20.25 and p.risk_dollars <= 250
