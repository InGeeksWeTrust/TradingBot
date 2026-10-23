"""Config loading. Credentials come from env vars, never the YAML file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from datetime import date, time
from pathlib import Path

import yaml


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass
class StrategyConfig:
    # All session times are US/Eastern (exchange-relevant). 09:30 ET = 06:30 Arizona.
    or_start: time = time(9, 30)
    or_minutes: int = 15               # opening range length
    signal_tf_minutes: int = 5         # breakout is confirmed on the close of this bar size
    entry_cutoff: time = time(11, 0)   # last signal time for ORB AND sweep entries (signal bar close <= this)
    flatten_time: time = time(11, 55)  # force flat
    breakout_buffer_ticks: int = 0     # close must exceed OR high/low by this many ticks
    min_range_points: float = 10.0     # skip day if OR narrower than this
    max_range_points: float = 150.0    # skip day if OR wider than this
    max_chase_points: float = 0.0      # skip if signal close is this far beyond the OR (0 = off)
    max_trades_per_day: int = 2
    one_trade_per_side: bool = True
    allow_long: bool = True
    allow_short: bool = True


@dataclass
class RiskConfig:
    contracts: int = 10
    max_contracts: int = 20            # hard ceiling, nothing can size above this
    min_contracts: int = 1             # skip the trade rather than go below this
    stop_mode: str = "dollar"          # dollar | opposite | mid | fixed
    fixed_stop_points: float = 12.5
    max_risk_per_trade: float = 250.0  # stop is tightened (capped) to keep risk at/below this
    min_stop_ticks: int = 8            # TopstepX rejects brackets of 5 ticks or fewer
    size_with_costs: bool = False      # True: size so stop $ + est. slippage + fees <= max_risk_per_trade
    est_slippage_ticks: int = 1        # per side (entry, stop) used when size_with_costs is on
    reward_risk: float = 2.0           # used only when target_per_trade is 0
    target_per_trade: float = 550.0    # $ target at full size; sets R = target / max_risk_per_trade
    target_usd: float = 0.0            # >0: every trade takes profit at this many $, whatever its size
    target_mode: str = "r"             # r = multiple of the stop; level = the price the strategy names
    partial_r: float = 0.0             # >0: take half off at this many R, move stop to breakeven, run the rest
    max_daily_loss: float = 500.0
    max_daily_profit: float = 1250.0
    min_reward_r: float = 1.0          # skip a trade if the profit cap trims its target below this many R
    loss_buffer: float = 25.0          # slippage cushion held back from the daily-loss headroom
    fee_per_contract_rt: float = 0.74  # ESTIMATE, check your TopstepX fee schedule
    include_fees_in_pnl: bool = True


@dataclass
class BotConfig:
    symbol: str = "MNQ"
    entry: str = "orb"                 # entry rule for the live bot: orb | sweep
    confirm_symbol: str = ""           # sweep: second market that must agree (MES for MNQ); blank = off
    sweep_extra: list[str] = field(default_factory=list)  # sweep: also intraday levels: or, swing
    history_days: int = 6             # sweep: days of 1-min history loaded for prior-day/overnight levels
    account_name: str = ""             # exact account name/id as shown in TopstepX
    base_url: str = "https://api.topstepx.com"
    poll_seconds: float = 5.0
    trading_days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # Mon-Fri
    holidays: list[date] = field(default_factory=list)
    kill_switch_file: str = "STOP"
    log_dir: str = "logs"
    journal_dir: str = "journal"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    @property
    def username(self) -> str:
        return _env("TOPSTEPX_USERNAME")

    @property
    def api_key(self) -> str:
        return _env("TOPSTEPX_API_KEY")


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip().strip('"').strip("'").strip()
    if not v:
        raise SystemExit(f"{name} is not set. Run: setx {name} \"...\" and open a NEW terminal.")
    return v


_TIME_FIELDS = {"or_start", "entry_cutoff", "flatten_time"}


def _fill(dc, data: dict):
    names = {f.name for f in fields(dc)}
    for k, v in (data or {}).items():
        if k not in names:
            raise ValueError(f"Unknown config key: {k}")
        setattr(dc, k, _t(v) if k in _TIME_FIELDS else v)
    return dc


def load_config(path: str | Path) -> BotConfig:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    cfg = BotConfig()
    _fill(cfg.strategy, raw.pop("strategy", {}))
    _fill(cfg.risk, raw.pop("risk", {}))
    holidays = raw.pop("holidays", []) or []
    _fill(cfg, raw)
    cfg.holidays = [h if isinstance(h, date) else date.fromisoformat(str(h)) for h in holidays]
    validate(cfg)
    return cfg


def validate(cfg: BotConfig) -> None:
    r, s = cfg.risk, cfg.strategy
    assert 1 <= r.contracts <= r.max_contracts, "contracts must be between 1 and max_contracts"
    assert r.max_contracts <= 20, "max_contracts is capped at 20 for this bot"
    assert r.stop_mode in {"dollar", "opposite", "mid", "fixed", "structural"}
    assert r.max_daily_loss > 0 and r.max_daily_profit > 0
    assert r.min_stop_ticks >= 6, "TopstepX rejects brackets of 5 ticks or fewer"
    assert r.est_slippage_ticks >= 0 and r.max_risk_per_trade > 0
    assert s.entry_cutoff < s.flatten_time, "entry_cutoff must be before flatten_time"
    assert s.entry_cutoff > s.or_start, "entry_cutoff must be after the session open"
    assert cfg.entry in {"orb", "sweep"}, "entry must be orb or sweep"
    assert set(cfg.sweep_extra) <= {"or", "swing"}, "sweep_extra may only contain: or, swing"
