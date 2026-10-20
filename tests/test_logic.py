from datetime import date, datetime, time, timedelta

from orbbot.backtest import simulate_day
from orbbot.config import BotConfig, RiskConfig, StrategyConfig
from orbbot.risk import ContractSpec, DailyGuard, plan_trade, realized_from_trades, trading_day_start
from orbbot.strategy import ET, Bar, ORBStrategy, Signal

SPEC = ContractSpec("CON.F.US.MNQ.Z26")
DAY = date(2026, 10, 19)  # a Monday


def mk(hh, mm, o, h, l, c):
    return Bar(datetime.combine(DAY, time(hh, mm), ET), o, h, l, c)


def session(after_or):
    """Flat 09:30-09:44 range 20000-20040, then the given (o,h,l,c) per minute from 09:45."""
    bars = [mk(9, 30 + i, 20020, 20040, 20000, 20020) for i in range(15)]
    t = datetime.combine(DAY, time(9, 45), ET)
    for ohlc in after_or:
        bars.append(Bar(t, *ohlc))
        t += timedelta(minutes=1)
    return bars


def test_range_and_long_breakout_on_5m_close():
    s = ORBStrategy(StrategyConfig())
    sigs = [x for b in session([(20030, 20045, 20030, 20042)] * 4 + [(20042, 20050, 20040, 20048)]) if (x := s.on_bar(b))]
    assert (s.or_high, s.or_low) == (20040, 20000)
    assert len(sigs) == 1 and sigs[0].side == "long" and sigs[0].price == 20048
    assert sigs[0].time.astimezone(ET).time() == time(9, 50)


def test_no_signal_when_5m_close_back_inside():
    s = ORBStrategy(StrategyConfig())
    bars = session([(20030, 20060, 20030, 20055)] * 4 + [(20055, 20055, 20030, 20035)])
    assert not [b for b in bars if s.on_bar(b)]


def test_range_filter_disables_day():
    s = ORBStrategy(StrategyConfig(min_range_points=50))
    for b in session([(20040, 20100, 20040, 20090)] * 5):
        assert s.on_bar(b) is None
    assert s.day_disabled


def test_entry_cutoff():
    s = ORBStrategy(StrategyConfig(entry_cutoff=time(9, 45)))
    assert not [b for b in session([(20040, 20100, 20040, 20090)] * 5) if s.on_bar(b)]


def test_stop_capped_to_risk_per_trade_10_and_20():
    sig = Signal("long", datetime.now(ET), 20048, 20040, 20000)  # structural stop 48 pts
    p10 = plan_trade(sig, RiskConfig(), SPEC, 0.0, 10)
    assert p10.contracts == 10 and p10.stop_points == 12.5 and p10.risk_dollars == 250
    assert p10.target_points == 27.5 and p10.reward_dollars == 550 and p10.bracket_ticks() == (-50, 110)
    p20 = plan_trade(sig, RiskConfig(), SPEC, 0.0, 20)
    assert p20.contracts == 20 and p20.stop_points == 6.25 and p20.risk_dollars == 250
    assert p20.target_points == 13.75 and p20.reward_dollars == 550


def test_dollar_stop_ignores_narrow_range():
    sig = Signal("long", datetime.now(ET), 20048, 20040, 20040 - 4)  # structural only 12 pts
    p = plan_trade(sig, RiskConfig(), SPEC, 0.0, 10)
    assert (p.risk_dollars, p.reward_dollars) == (250, 550)
    p_struct = plan_trade(sig, RiskConfig(stop_mode="opposite"), SPEC, 0.0, 10)
    assert p_struct.risk_dollars == 240  # structural mode keeps the tighter stop


def test_short_bracket_signs():
    sig = Signal("short", datetime.now(ET), 19990, 20040, 20000)
    sl, tp = plan_trade(sig, RiskConfig(), SPEC, 0.0, 10).bracket_ticks()
    assert sl > 0 and tp < 0


def test_size_reduced_to_fit_daily_headroom_and_skips_when_none():
    sig = Signal("long", datetime.now(ET), 20048, 20040, 20000)
    p = plan_trade(sig, RiskConfig(), SPEC, -250.0, 10)  # $225 headroom after buffer
    assert p.contracts == 8 and p.risk_dollars <= 225
    assert plan_trade(sig, RiskConfig(), SPEC, -480.0, 10) is None


def test_target_trimmed_to_profit_cap():
    sig = Signal("long", datetime.now(ET), 20048, 20040, 20000)
    p = plan_trade(sig, RiskConfig(), SPEC, 1000.0, 10)  # only $250 left to the $1250 cap
    net = p.reward_dollars - RiskConfig().fee_per_contract_rt * p.contracts
    assert 250 <= net <= 255  # lands at the cap after fees (rounded up to a whole tick)


def test_guard():
    g = DailyGuard(RiskConfig())
    assert g.check(-300, -150) is None
    assert g.check(-300, -200) == "loss" and g.halted
    g2 = DailyGuard(RiskConfig())
    assert g2.check(1000, 250) == "profit"


def test_realized_from_trades_ignores_half_turns_and_voided():
    trades = [{"profitAndLoss": None, "fees": 0.37, "voided": False},
              {"profitAndLoss": 100.0, "fees": 0.37, "voided": False},
              {"profitAndLoss": 999.0, "fees": 1, "voided": True}]
    assert round(realized_from_trades(trades), 2) == 99.26


def test_trading_day_start_is_5pm_central_prior_day():
    st = trading_day_start(datetime(2026, 10, 19, 10, 0, tzinfo=ET))
    assert (st.day, st.hour) == (18, 17)


def test_backtest_long_hits_target():
    bars = session([(20030, 20045, 20030, 20042)] * 4 + [(20042, 20050, 20040, 20048)]
                   + [(20048, 20060, 20045, 20058)] * 3 + [(20058, 20085, 20055, 20083)])
    tr = simulate_day(bars, BotConfig(), SPEC, 10, slippage_ticks=1)
    assert len(tr) == 1 and tr[0].reason == "target" and tr[0].pnl > 540


def test_backtest_stop_then_daily_loss_respected():
    # long breakout stopped, then short breakout stopped -> total loss must stay <= $500 + slippage
    up = [(20030, 20045, 20030, 20042)] * 4 + [(20042, 20050, 20040, 20048)]
    crash = [(20048, 20048, 20020, 20025)] * 5           # stops the long, closes below? no: inside range
    down = [(20020, 20020, 19990, 19992)] * 5             # 5m close below OR low -> short signal
    rip = [(19992, 20030, 19990, 20025)] * 5              # stops the short
    tr = simulate_day(session(up + crash + down + rip), BotConfig(), SPEC, 10, slippage_ticks=1)
    assert [t.side for t in tr] == ["long", "short"]
    assert sum(t.pnl for t in tr) >= -500 - 30
