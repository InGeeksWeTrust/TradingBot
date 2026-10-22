from datetime import date, datetime, time, timedelta

from orbbot.config import RiskConfig, StrategyConfig
from orbbot.features import Levels
from orbbot.research import MTF, GapFade, Sweep, VWAPBand
from orbbot.risk import plan_trade, spec_for
from orbbot.strategy import ET, Bar, Signal

DAY = date(2026, 10, 19)
CFG = StrategyConfig()


class FakeFeats:
    def __init__(self, bias="long", structure="up", hs=20100.0, ls=20000.0, levels=None):
        self._b, self._s, self._hs, self._ls = bias, structure, hs, ls
        self._lv = levels or Levels()

    def bias(self, t):
        return self._b

    def structure(self, t):
        return self._s

    def swings(self, t, k=2):
        return [self._hs], [self._ls]

    def day_levels(self, d):
        return self._lv


def minutes(start, ohlcs, v=100):
    t = datetime.combine(DAY, start, ET)
    out = []
    for o, h, l, c in ohlcs:
        out.append(Bar(t, o, h, l, c, v))
        t += timedelta(minutes=1)
    return out


def run(strat, bars):
    strat.reset(DAY)
    sigs = []
    for b in bars:
        s = strat.on_bar(b)
        if s:
            sigs.append(s)
            strat.register_entry(s.side)
    return sigs


# 10:00-10:04 pullback bar (high 20040), then 10:05-10:09 bar closing above it
PULLBACK_THEN_UP = [(20035, 20040, 20030, 20032)] * 5 + [(20032, 20046, 20031, 20045)] * 5


def test_mtf_long_when_bias_structure_and_trigger_agree():
    sigs = run(MTF(CFG, 0.25, FakeFeats()), minutes(time(10, 0), PULLBACK_THEN_UP))
    assert len(sigs) == 1 and sigs[0].side == "long"
    assert sigs[0].or_low == 20000 - 0.25            # structural stop under the 15-min swing low
    assert sigs[0].time.astimezone(ET).time() == time(10, 10)


def test_mtf_no_trade_when_hourly_bias_disagrees():
    assert run(MTF(CFG, 0.25, FakeFeats(bias="short")), minutes(time(10, 0), PULLBACK_THEN_UP)) == []


def test_mtf_no_trade_when_chasing_above_swing_high():
    assert run(MTF(CFG, 0.25, FakeFeats(hs=20040.0)), minutes(time(10, 0), PULLBACK_THEN_UP)) == []


def test_mtf_morning_window_ends_1130():
    assert run(MTF(CFG, 0.25, FakeFeats(), window="am"), minutes(time(11, 30), PULLBACK_THEN_UP)) == []
    assert len(run(MTF(CFG, 0.25, FakeFeats(), window="day"), minutes(time(11, 30), PULLBACK_THEN_UP))) == 1


def test_sweep_short_after_run_above_prior_day_high():
    feats = FakeFeats(levels=Levels(pdh=20100.0))
    bars = minutes(time(10, 0), [(20090, 20105, 20088, 20095)] * 5)
    sigs = run(Sweep(CFG, 0.25, feats), bars)
    assert len(sigs) == 1 and sigs[0].side == "short" and sigs[0].or_high == 20105.25


def test_sweep_ignores_breakout_that_closes_beyond_level():
    feats = FakeFeats(levels=Levels(pdh=20100.0))
    assert run(Sweep(CFG, 0.25, feats), minutes(time(10, 0), [(20090, 20110, 20088, 20108)] * 5)) == []


def test_vwapband_short_after_close_back_inside_upper_band():
    quiet = [(20000, 20001, 19999, 19999 + 2 * (i % 2)) for i in range(30)]   # 09:30-09:59
    spike = [(20010, 20011, 20009, 20010)] * 5                                # 10:00-10:04 closes above band
    back = [(20005, 20006, 20002, 20003)] * 5                                 # 10:05-10:09 back inside
    sigs = run(VWAPBand(CFG, 0.25), minutes(time(9, 30), quiet + spike + back))
    assert len(sigs) == 1 and sigs[0].side == "short" and sigs[0].or_high == 20011.25


def test_gapfade_short_on_gap_up_still_open_at_935():
    feats = FakeFeats(levels=Levels(pdc=20000.0))
    sigs = run(GapFade(CFG, 0.25, feats), minutes(time(9, 30), [(20100, 20105, 20085, 20090)] * 5))
    assert len(sigs) == 1 and sigs[0].side == "short"


def test_gapfade_skips_tiny_gap():
    feats = FakeFeats(levels=Levels(pdc=20000.0))
    assert run(GapFade(CFG, 0.25, feats), minutes(time(9, 30), [(20010, 20015, 20005, 20012)] * 5)) == []


def test_structural_stop_sizes_down_to_keep_risk():
    sig = Signal("long", datetime.now(ET), 20050, 20100, 20020)               # 30-pt stop
    p = plan_trade(sig, RiskConfig(stop_mode="structural"), spec_for("MNQ"), 0.0, 10)
    assert p.stop_points == 30 and p.contracts == 4 and p.risk_dollars <= 250
    too_wide = Signal("long", datetime.now(ET), 20050, 20100, 19800)          # 250 pts > $250 at 1 ct
    assert plan_trade(too_wide, RiskConfig(stop_mode="structural"), spec_for("MNQ"), 0.0, 10) is None


# ---- COMEX gold session: same rules, times follow the 8:20 open
from orbbot.research import COMEX, Variant  # noqa: E402


def test_comex_mtf_trades_after_835_not_before():
    early = run(MTF(CFG, 0.10, FakeFeats(), session=COMEX), minutes(time(8, 20), PULLBACK_THEN_UP))  # 8:30
    ok = run(MTF(CFG, 0.10, FakeFeats(), session=COMEX), minutes(time(8, 25), PULLBACK_THEN_UP))     # 8:35
    assert early == [] and len(ok) == 1 and ok[0].time.astimezone(ET).time() == time(8, 35)


def test_comex_window_ends_1030():
    assert run(MTF(CFG, 0.10, FakeFeats(), session=COMEX), minutes(time(10, 30), PULLBACK_THEN_UP)) == []


def test_comex_gapfade_decides_at_825():
    feats = FakeFeats(levels=Levels(pdc=20000.0))
    sigs = run(GapFade(CFG, 0.10, feats, session=COMEX), minutes(time(8, 20), [(20100, 20105, 20085, 20090)] * 5))
    assert len(sigs) == 1 and sigs[0].time.astimezone(ET).time() == time(8, 25)


def test_comex_variant_config_moves_orb_to_820_and_flat_1100():
    from orbbot.config import BotConfig
    c = Variant("orb", 2.0, 10).cfg(BotConfig(), COMEX).strategy
    assert (c.or_start, c.entry_cutoff, c.flatten_time) == (time(8, 20), time(10, 30), time(11, 0))
    assert c.min_range_points == 1.0


def test_mgc_spec():
    s = spec_for("MGC")
    assert (s.tick_size, s.tick_value, s.point_value) == (0.10, 1.00, 10.0)


# ---- MACD / Bollinger / VWAP
from orbbot.research import BBMom, BBRev  # noqa: E402


class IndFeats:
    """Returns the same 5-min indicator row for every bar close."""
    def __init__(self, **row):
        prev = row.pop("prev")
        self.row = dict(row, prev=prev)

    def five_at(self, t):
        return self.row


def gold_session(after):
    quiet = [(3400, 3400.5, 3399.5, 3400)] * 20               # 08:20-08:39: VWAP ~3400
    return minutes(time(8, 20), quiet + after)


class SeqFeats:
    """Indicator rows that change between the two 5-min closes."""
    def __init__(self, first, second):
        self.rows = [first, second]

    def five_at(self, t):
        return self.rows[0] if t.astimezone(ET).time() <= time(8, 45) else self.rows[1]


STRETCH = dict(mid=3401, up=3404, dn=3398, macd=1.0, signal=0.3, hist=0.7, prev={"hist": 0.4})
FADE = dict(mid=3401.5, up=3405, dn=3398, macd=1.0, signal=0.6, hist=0.4, prev={"hist": 0.7})
GROW = dict(mid=3401.5, up=3405, dn=3398, macd=1.4, signal=0.5, hist=0.9, prev={"hist": 0.7})


def test_bbrev_shorts_after_stretch_then_fading_histogram():
    bars = gold_session([(3404, 3406, 3403.5, 3405)] * 5 + [(3405, 3405.5, 3403, 3403.5)] * 5)
    sigs = run(BBRev(CFG, 0.10, SeqFeats(STRETCH, FADE), session=COMEX), bars)
    assert len(sigs) == 1 and sigs[0].side == "short" and abs(sigs[0].or_high - 3406.1) < 1e-9
    assert sigs[0].time.astimezone(ET).time() == time(8, 50)


def test_bbrev_waits_while_histogram_still_growing():
    bars = gold_session([(3404, 3406, 3403.5, 3405)] * 5 + [(3405, 3405.5, 3403, 3403.5)] * 5)
    assert run(BBRev(CFG, 0.10, SeqFeats(STRETCH, GROW), session=COMEX), bars) == []


def test_bbmom_long_on_macd_cross_above_vwap_and_mid():
    f = IndFeats(mid=3401, up=3406, dn=3396, macd=0.3, signal=0.1, hist=0.2, prev={"hist": -0.1, "macd": 0.0, "signal": 0.1})
    sigs = run(BBMom(CFG, 0.10, f, session=COMEX), gold_session([(3401, 3403, 3400.8, 3402.5)] * 5))
    assert len(sigs) == 1 and sigs[0].side == "long"


def test_bbmom_no_trade_without_cross():
    f = IndFeats(mid=3401, up=3406, dn=3396, macd=0.3, signal=0.1, hist=0.2, prev={"hist": 0.1, "macd": 0.2, "signal": 0.1})
    assert run(BBMom(CFG, 0.10, f, session=COMEX), gold_session([(3401, 3403, 3400.8, 3402.5)] * 5)) == []
