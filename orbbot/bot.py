"""Session runner against TopstepX (ProjectX Gateway), with execution safety.

Usage:
    python -m orbbot.bot --config config-sweep.yaml --dry-run      # paper: live data, simulated fills
    python -m orbbot.bot --config config-sweep.yaml --read-only    # connect, reconcile, report, exit
    python -m orbbot.bot --config config-sweep.yaml --dry-run --once
    python -m orbbot.bot --config config-sweep.yaml --live         # real orders: ALSO needs
                                                                    # safety.live_trading_enabled: true

Without --dry-run, --read-only or --live the bot refuses to start (live trading is off by default).

Each poll (every `poll_seconds`):
  1. kill switches: STOP file = flatten and halt for the day; HALT file = no new entries
  2. market data: completed 1-min bars -> the entry rule; freshness of bars and confirmation bars
  3. broker state: account balance, open positions, open orders (each tracked for freshness)
  4. the active order intent is advanced through its state machine (execution.py); a position
     without verified protection is flattened; an unexpected position blocks entries
  5. daily loss/profit guard (only on a fresh price) and the account (Maximum Loss Limit) guard
  6. flatten time
  7. a new entry only if nothing blocks it (see Session.entry_blockers)
State lives in SQLite (state/<mode>.sqlite) so a restart resumes safely.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .api import ProjectXClient
from .config import BotConfig, load_config
from .execution import S, AccountGuard, ExecutionEngine, Freshness
from .features import Features
from .filters import ESConfirm, Filtered
from .journal import Journal
from .paper import PaperClient
from .risk import ContractSpec, DailyGuard, plan_trade, realized_from_trades, trading_day_start, unrealized
from .state import StateError, StateStore
from .strategy import ET, Bar, ORBStrategy

log = logging.getLogger("orbbot")
CT = ZoneInfo("America/Chicago")
MONTHS = "FGHJKMNQUVXZ"


def now_et() -> datetime:
    return datetime.now(ET)


class ORBBot:
    def __init__(self, cfg: BotConfig, mode: str = "paper", contracts: int | None = None, api=None,
                 clock=None, sleep=None):
        if mode not in ("paper", "live", "readonly"):
            raise ValueError(mode)
        req = cfg.safety.required_confirm_symbol
        if req and cfg.confirm_symbol.upper() != req.upper():
            raise SystemExit(f"Frozen forward test: confirm_symbol must be {req} (config has "
                             f"'{cfg.confirm_symbol}'). The bot will not substitute another market.")
        if mode == "live" and not cfg.safety.live_trading_enabled:
            raise SystemExit("Live trading is disabled. Set safety.live_trading_enabled: true in the config "
                             "AND pass --live to send real orders. Use --dry-run to paper trade.")
        self.cfg = cfg
        self.mode = mode
        self.clock = clock or now_et
        self.sleep = sleep or time.sleep
        self.contracts = contracts or cfg.risk.contracts
        if not 1 <= self.contracts <= cfg.risk.max_contracts:
            raise ValueError(f"contracts must be 1..{cfg.risk.max_contracts}")
        if api is None:
            real = ProjectXClient(cfg.username, cfg.api_key, cfg.base_url)
            api = (PaperClient(real, fee_per_contract_rt=cfg.risk.fee_per_contract_rt,
                               state_path=Path(cfg.safety.state_dir) / "paper_broker.json", clock=self.clock)
                   if mode == "paper" else real)
        self.api = api
        store_name = "paper" if mode == "paper" else "live"  # read-only inspects the live state
        self.store = StateStore(Path(cfg.safety.state_dir) / f"{store_name}.sqlite",
                                clock=lambda: self.clock().astimezone(timezone.utc))
        self.journal = Journal(cfg.journal_dir, store_name, cfg.risk.max_daily_loss, cfg.risk.max_daily_profit)
        self.account_id: int | None = None
        self.spec: ContractSpec | None = None
        self.confirm_spec: ContractSpec | None = None
        self.done_day = None

    # ------------------------------------------------------------------ setup
    def connect(self) -> None:
        self.api.login()
        accounts = self.api.search_accounts()
        if not self.cfg.account_name:
            names = ", ".join(f"{a['name']} (id {a['id']}, balance {a.get('balance')})" for a in accounts)
            raise SystemExit(f"Set account_name in config. Available: {names}")
        match = [a for a in accounts if a["name"] == self.cfg.account_name or str(a["id"]) == str(self.cfg.account_name)]
        if not match:
            raise SystemExit(f"Account '{self.cfg.account_name}' not found among active accounts")
        acct = match[0]
        if not acct.get("canTrade", True) and self.mode == "live":
            raise SystemExit(f"Account {acct['name']} cannot trade right now")
        self.account_id = acct["id"]
        log.info("Using account %s (id %s, balance %s) | mode=%s", acct["name"], acct["id"], acct.get("balance"), self.mode)
        self.resolve_contract()

    def _find(self, symbol: str) -> ContractSpec:
        """The active contract whose name is exactly <root><month code><year digit>, e.g. MNQZ6 / ESZ6."""
        cs = self.api.search_contracts(symbol)
        pat = re.compile(rf"^{re.escape(symbol.upper())}[{MONTHS}]\d{{1,2}}$")
        cands = [c for c in cs if c.get("activeContract") and pat.match(str(c["name"]).upper())]
        if not cands:
            raise SystemExit(f"No active {symbol} contract found: {[c['name'] for c in cs]}")
        c = cands[0]
        log.info("Contract %s (%s) tick %.2f = $%.2f", c["name"], c["id"], float(c["tickSize"]), float(c["tickValue"]))
        return ContractSpec(c["id"], float(c["tickSize"]), float(c["tickValue"]))

    def resolve_contract(self) -> None:
        """Pick the active front-month contract(s) (re-run each session to handle rollovers)."""
        self.spec = self._find(self.cfg.symbol)
        if self.cfg.entry == "sweep" and self.cfg.confirm_symbol:
            self.confirm_spec = self._find(self.cfg.confirm_symbol)
        if isinstance(self.api, PaperClient):
            self.api.set_contract(self.spec.contract_id, self.spec.tick_size, self.spec.point_value)

    def is_trading_day(self, d) -> bool:
        return d.weekday() in self.cfg.trading_days and d not in self.cfg.holidays

    # ------------------------------------------------------------------ modes
    def read_only(self) -> int:
        """Connect, reconcile against the broker and print what exists. Sends nothing."""
        self.connect()
        fresh = Freshness(self.cfg.safety, lambda: datetime.now(timezone.utc))
        eng = ExecutionEngine(self.api, self.store, self.account_id, self.spec, self.cfg.safety, mode=self.mode)
        eng.refresh(fresh)
        acct = next(a for a in self.api.search_accounts() if a["id"] == self.account_id)
        print(f"Account {acct['name']}: balance {acct.get('balance')}, canTrade {acct.get('canTrade')}")
        print(f"Open positions: {eng.positions or 'none'}")
        print(f"Open orders: {eng.open_orders or 'none'}")
        from .execution import verify_protection
        for p in eng.positions:
            pr = verify_protection(p, eng.open_orders, self.spec.tick_size, None, self.cfg.safety.stop_price_tolerance_ticks)
            print(f"  {p['contractId']} {p['size']} @ {p['averagePrice']}: "
                  f"{'protected' if pr.ok else 'NOT PROTECTED: ' + '; '.join(pr.problems)}")
        print(f"Unfinished order intents in {self.store.path}: "
              f"{[(i['tag'], i['state']) for i in self.store.open_intents()] or 'none'}")
        g = AccountGuard(self.cfg.account, self.store)
        g.update_balance(float(acct.get("balance") or 0))
        print(f"Account guard: {'MLL floor ' + str(g.floor()) if g.enabled else 'off (account.max_loss_limit is 0)'}")
        return 0

    def run_forever(self, once: bool = False) -> None:
        self.connect()
        s = self.cfg.strategy
        while True:
            now = self.clock()
            today = now.date()
            prep = datetime.combine(today, s.or_start, ET) - timedelta(minutes=5)
            end = datetime.combine(today, s.flatten_time, ET) + timedelta(minutes=2)
            if self.is_trading_day(today) and prep <= now < end and self.done_day != today:
                try:
                    self.run_session(today)
                    self.done_day = today
                except Exception:
                    log.exception("Session crashed; entries stop, the open position (if any) is reconciled on restart")
                    self.store.event("session_crash", "see log")
                    self.sleep(30)
                    continue
                if once:
                    return
                continue
            self.eod_snapshot(now)
            if once and (not self.is_trading_day(today) or now >= end or self.done_day == today):
                log.info("--once: no session to run today (%s). Exiting.", today)
                return
            nxt = prep if now < prep and self.done_day != today else prep + timedelta(days=1)
            eod = datetime.combine(today, dtime(15, 15), CT).astimezone(ET)   # wake for the EOD balance
            if self.is_trading_day(today) and now < eod < nxt:
                nxt = eod
            log.info("Idle. Next session check at %s ET", nxt.strftime("%a %Y-%m-%d %H:%M"))
            self.sleep(min(max((nxt - self.clock()).total_seconds(), 5), 900))

    def eod_snapshot(self, now: datetime) -> None:
        """Between 3:10 PM CT (end of Topstep's trading day) and 5:00 PM CT, record today's balance
        as the end-of-day balance that moves the Maximum Loss Limit."""
        ct = now.astimezone(CT)
        if not (self.cfg.account.max_loss_limit > 0 and self.is_trading_day(ct.date())
                and dtime(15, 10) <= ct.time() < dtime(17, 0)):
            return
        try:
            acct = next(a for a in self.api.search_accounts() if a["id"] == self.account_id)
            AccountGuard(self.cfg.account, self.store).end_of_day(ct.date(), float(acct["balance"]), now)
        except Exception as e:  # noqa: BLE001
            log.warning("End-of-day balance snapshot failed: %s", e)

    def run_session(self, day: date) -> None:
        sess = Session(self, day)
        sess.start()
        while sess.poll() != "done":
            self.sleep(self.cfg.poll_seconds)


class Session:
    """One trading day. `poll()` does one iteration; everything time-dependent uses bot.clock."""

    def __init__(self, bot: ORBBot, day: date):
        self.bot, self.day = bot, day
        self.cfg, self.s = bot.cfg, bot.cfg.strategy
        self.api, self.store = bot.api, bot.store
        from .research import SESSION_FOR_SYMBOL, SESSIONS
        self.session = SESSIONS[SESSION_FOR_SYMBOL.get(self.cfg.symbol.upper(), "nasdaq")]
        self.strat = None
        self.inner = None
        self.confirm = None
        self.last_bar_t: datetime | None = None
        self.last_price: float | None = None
        self.realized = 0.0
        self.last_realized: datetime | None = None
        self.last_account: datetime | None = None
        self.halted = False
        self.flatten_at = datetime.combine(day, self.s.flatten_time, ET)
        self.cutoff_at = datetime.combine(day, self.s.entry_cutoff, ET)
        self.ready_at = datetime.combine(day, self.session.open, ET) + timedelta(minutes=1)
        self.stale_since: datetime | None = None
        self.mfe = self.mae = 0.0

    # ------------------------------------------------------------------ helpers
    def utc(self) -> datetime:
        return self.bot.clock().astimezone(timezone.utc)

    def now(self) -> datetime:
        return self.bot.clock()

    def _bars(self, cid, start, end, partial=False) -> list[Bar]:
        raw = self.api.retrieve_bars(cid, start, end, include_partial=partial)
        return sorted((Bar.from_api(b) for b in raw), key=lambda b: b.t)

    def expected_stop(self, intent: dict, pos: dict) -> float | None:
        plan = intent.get("plan") or {}
        if "stop_pts" not in plan:
            return None
        sign = 1 if intent["side"] == "long" else -1
        return float(pos["averagePrice"]) - sign * float(plan["stop_pts"])

    def halt(self, reason: str) -> None:
        if not self.halted:
            self.halted = True
            log.warning("Halting new entries for the day: %s", reason)
            try:
                self.store.halt_day(self.day, reason)
            except StateError:
                pass

    # ------------------------------------------------------------------ start
    def start(self) -> None:
        b = self.bot
        b.resolve_contract()
        self.spec = b.spec
        self.fresh = Freshness(self.cfg.safety, self.utc)
        self.engine = ExecutionEngine(self.api, self.store, b.account_id, self.spec, self.cfg.safety,
                                      clock=self.utc, sleep=b.sleep, mode=b.mode)
        self.acct = AccountGuard(self.cfg.account, self.store)
        self.guard = DailyGuard(self.cfg.risk)
        d = self.store.day(self.day)
        if d["halted"]:
            self.halted = True
            log.warning("Day already halted earlier: %s", d["halt_reason"])
        if d["no_entries"]:
            self.engine.block("day_blocked", d["halt_reason"] or "entries blocked earlier today")
        if self.store.recovered_from_corruption:
            self.engine.block("state_lost", "state database was unreadable at start-up; no entries this session")
        self.refresh_account(force=True)
        self.refresh_realized(force=True)
        self._catch_up_eod()
        if b.confirm_spec:
            self.engine.confirm_contract = b.confirm_spec.contract_id
            self.store.event("session_start", f"confirmation contract {b.confirm_spec.contract_id}")
        action = self.engine.recover(self.day, self.fresh, self.expected_stop)
        if action == "flatten":
            self.do_flatten("start-up: open position without verified protection")
        log.info("=== Session %s | %s %s | contracts<=%d | realized $%.2f | mode=%s ===", self.day, self.cfg.symbol,
                 self.cfg.entry + (f" + {self.cfg.confirm_symbol} confirm" if b.confirm_spec else ""),
                 b.contracts, self.realized, b.mode)
        if self.cfg.entry == "orb":
            self.strat = ORBStrategy(self.s, self.spec.tick_size)
            self.strat.reset(self.day)
            self.inner = self.strat
            self._preload()

    def _catch_up_eod(self) -> None:
        """If the bot wasn't running at the previous trading day's close, record the balance now as that
        day's end-of-day balance (valid because the bot never trades between sessions), flagged as
        approximate; copy the floor from the dashboard into account.mll_floor if they disagree."""
        if not self.acct.enabled or self.acct.balance is None:
            return
        prev = self.day - timedelta(days=1)
        while not self.bot.is_trading_day(prev):
            prev -= timedelta(days=1)
        last = self.store.get("last_eod_day")
        if last is None or last < prev.isoformat():
            self.acct.end_of_day(prev, self.acct.balance, self.utc(), approximate=True)

    def _preload(self) -> None:
        """Restore what the strategy already did today (levels used, trades taken)."""
        d = self.store.day(self.day)
        used = self.store.used_levels(self.day)
        inner = self.inner
        if hasattr(inner, "used"):
            inner.used |= used
            inner.n = max(getattr(inner, "n", 0), d["trades"])
        if isinstance(inner, ORBStrategy):
            inner.trades_taken = max(inner.trades_taken, d["trades"])
            inner.sides_taken |= set(d["sides"])
        if used or d["trades"]:
            log.info("Restored from state: levels used %s, trades today %d", sorted(used), d["trades"])
        self._known_used = set(getattr(inner, "used", set()))

    def build_sweep(self) -> None:
        from .research import Sweep
        now = self.now()
        hist = self._bars(self.spec.contract_id, now - timedelta(days=self.cfg.history_days), now)
        feats = Features(hist, self.session.open, self.session.close)
        lv = feats.day_levels(self.day)
        log.info("Sweep levels: prior-day high %s low %s | overnight high %s low %s | entries until %s ET%s",
                 lv.pdh, lv.pdl, lv.onh, lv.onl, self.s.entry_cutoff.strftime("%H:%M"),
                 f" | plus intraday: {', '.join(self.cfg.sweep_extra)}" if self.cfg.sweep_extra else "")
        sw = Sweep(self.s, self.spec.tick_size, feats, window="am", session=self.session,
                   max_trades=self.s.max_trades_per_day, extra=self.cfg.sweep_extra)
        sw.reset(self.day)
        self.inner = sw
        self._preload()
        if self.bot.confirm_spec:
            self.strat = Filtered(sw, [lambda sig: self.confirm is not None and self.confirm.check(sig.side, sig.time)])
        else:
            self.strat = sw

    def refresh_confirm(self) -> None:
        cs = self.bot.confirm_spec
        if not cs:
            return
        start = datetime.combine(self.day, self.session.open, ET)
        now = self.now()
        try:
            done = [b for b in self._bars(cs.contract_id, start, now) if b.t + timedelta(minutes=1) <= now]
        except Exception as e:  # noqa: BLE001
            log.warning("Confirmation bars failed: %s", e)
            return
        self.confirm = ESConfirm(done, self.session.open, self.session.close)
        if done:
            self.fresh.bar("confirm", done[-1].t)

    def refresh_account(self, force=False) -> None:
        now = self.now()
        if not force and self.last_account and (now - self.last_account).total_seconds() < 30:
            return
        self.last_account = now
        try:
            acct = next(a for a in self.api.search_accounts() if a["id"] == self.bot.account_id)
            self.acct.update_balance(float(acct["balance"]))
            self.fresh.ok("account")
            if acct.get("canTrade") is False:
                self.engine.block("account_cannot_trade", "broker reports canTrade = false")
        except Exception as e:  # noqa: BLE001
            log.warning("Account/search failed: %s", e)

    def refresh_realized(self, force=False) -> None:
        now = self.now()
        if not force and self.last_realized and (now - self.last_realized).total_seconds() < 30:
            return
        self.last_realized = now
        try:
            trades = self.api.search_trades(self.bot.account_id, trading_day_start(self.utc()))
            self.realized = realized_from_trades(trades, self.cfg.risk.include_fees_in_pnl)
        except Exception as e:  # noqa: BLE001
            log.warning("Trade/search failed: %s", e)

    def do_flatten(self, why: str) -> bool:
        self._last_flatten = self.now()
        ok = self.engine.flatten(why, self.fresh)
        self.halt(why)
        self.refresh_realized(force=True)
        return ok

    def record_exit(self, intent: dict, reason: str | None) -> None:
        """Journal + forward-test record for a finished round trip, from the broker's fills."""
        try:
            start = datetime.fromisoformat(intent["created_at"]) - timedelta(seconds=5)
            fills = [f for f in self.api.search_trades(self.bot.account_id, start)
                     if not f.get("voided") and f["contractId"] == self.spec.contract_id]
        except Exception:  # noqa: BLE001
            log.exception("Could not read fills for the journal")
            return
        opens = [f for f in fills if f.get("profitAndLoss") is None]
        closes = [f for f in fills if f.get("profitAndLoss") is not None]
        if not opens or not closes:
            log.warning("Journal: no complete round trip found for %s", intent["tag"])
            return

        def vwap(fs):
            n = sum(f["size"] for f in fs)
            return sum(f["price"] * f["size"] for f in fs) / n, n

        entry, n = vwap(opens)
        exit_px, _ = vwap(closes)
        gross = sum(f["profitAndLoss"] for f in closes)
        fees = sum(float(f.get("fees") or 0) for f in fills)
        net = gross - fees if self.cfg.risk.include_fees_in_pnl else gross
        plan = intent.get("plan") or {}
        if reason is None:
            move = (exit_px - entry) * (1 if intent["side"] == "long" else -1)
            reason = ("target" if move >= 0.5 * plan.get("target_pts", 0) else
                      "stop" if move <= -0.5 * plan.get("stop_pts", 0) else "other")
        et = lambda f: datetime.fromisoformat(f["creationTimestamp"]).astimezone(ET).strftime("%H:%M:%S")  # noqa: E731
        self.bot.journal.record({
            "date": self.day.isoformat(), "side": intent["side"], "contracts": n, "entry_time": et(opens[0]),
            "entry": round(entry, 2), "exit_time": et(closes[-1]), "exit": round(exit_px, 2), "reason": reason,
            "stop_pts": plan.get("stop_pts"), "target_pts": plan.get("target_pts"),
            "risk_usd": round(plan.get("risk", 0), 2), "pnl_usd": round(net, 2)})
        self.store.forward(intent["tag"], exit_time=closes[-1]["creationTimestamp"], exit_price=round(exit_px, 2),
                           exit_reason=reason, gross_pnl=round(gross, 2), fees=round(fees, 2), net_pnl=round(net, 2),
                           mfe_pts=self.mfe, mae_pts=self.mae, equity_after=self.acct.balance)

    # ------------------------------------------------------------------ the poll
    def poll(self) -> str:
        cfg, s, eng = self.cfg, self.s, self.engine
        now = self.now()

        # 1. kill switches
        if Path(cfg.kill_switch_file).exists():
            self.do_flatten(f"kill switch file '{cfg.kill_switch_file}' present")
            return "done"
        if Path(cfg.safety.halt_file).exists():
            eng.block("halt_file", f"halt file '{cfg.safety.halt_file}' present")
        else:
            eng.unblock("halt_file")

        # 2. market data -> entry rule
        if cfg.entry == "sweep":
            if self.strat is None and now >= self.ready_at:
                try:
                    self.build_sweep()
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not build sweep levels yet: %s", e)
            if self.strat is not None:
                self.refresh_confirm()
        signals = self.feed_bars(now)

        # 3. broker state
        self.refresh_account()
        self.refresh_realized()
        eng.refresh(self.fresh)

        # 4. order intents / positions
        res = eng.manage(self.fresh, self.expected_stop)
        if res == "closed":
            it = eng.last_closed
            if it:
                self.record_exit(it, None)
            eng.refresh(self.fresh)
            self.refresh_realized(force=True)
            self.refresh_account(force=True)
        elif res == "flatten":
            self.do_flatten("position without verified protection")
        if (eng.active is not None and eng.active["state"] == S.EXIT_PENDING.value and eng.position()
                and (self._last_flatten is None or (now - self._last_flatten).total_seconds() >= 30)):
            # an earlier flatten could not be confirmed: keep trying (bounded each time), keep alerting
            self._last_flatten = now
            self.do_flatten("retrying an unconfirmed flatten")
        if eng.active is None:
            if eng.check_unexpected() == "flatten":
                self.do_flatten("unexpected unprotected position")
        if eng.active is not None:
            self._last_tag = eng.active["tag"]

        # 5. guards
        pos = eng.position()
        price_ok = self.fresh.bar_age("bars") <= cfg.safety.bars_max_age_s and self.last_price is not None
        open_pnl = unrealized(pos, self.last_price, self.spec) if (pos and price_ok) else 0.0
        if pos and self.last_price is not None:
            sign = 1 if pos["type"] == 1 else -1
            mv = (self.last_price - float(pos["averagePrice"])) * sign
            self.mfe, self.mae = max(self.mfe, mv), max(self.mae, -mv)
        if pos and not price_ok:
            self.stale_since = self.stale_since or now
            log.warning("Price data stale with an open position: relying on the broker-side stop")
            if (cfg.safety.stale_position_flatten_s > 0 and
                    (now - self.stale_since).total_seconds() > cfg.safety.stale_position_flatten_s):
                self.do_flatten("market data stale too long with an open position")
        else:
            self.stale_since = None
        if price_ok or not pos:
            hit = self.guard.check(self.realized, open_pnl)
            if hit:
                if pos:
                    self.do_flatten(self.guard.reason)
                self.halt(self.guard.reason)
        room = self.acct.headroom(open_pnl if price_ok else -self._open_risk(pos))
        if self.acct.enabled and room is not None and room < 0:
            if pos and cfg.account.flatten_at_buffer:
                self.do_flatten(f"account within ${cfg.account.safety_buffer:.0f} of the Maximum Loss Limit")
            eng.block("account_buffer", f"equity within ${cfg.account.safety_buffer:.0f} of the MLL floor")
        elif room is not None:
            eng.unblock("account_buffer")

        # 6. flatten time
        if now >= self.flatten_at:
            if eng.position():
                self.do_flatten("session flatten time")
            else:
                for o in [o for o in eng.open_orders if o.get("contractId") == self.spec.contract_id]:
                    try:
                        self.api.cancel_order(self.bot.account_id, o["id"])
                    except Exception as e:  # noqa: BLE001
                        log.warning("Cancel %s failed: %s", o.get("id"), e)
            self.refresh_account(force=True)
            self.refresh_realized(force=True)
            log.info("=== Session over. Realized $%.2f ===", self.realized)
            return "done"

        # 7. entries
        for sig in signals:
            self.consider(sig, now)
        self.bot.journal.flush()
        return "ok"

    def _open_risk(self, pos) -> float:
        it = self.engine.active
        if not pos or not it:
            return 0.0
        return float((it.get("plan") or {}).get("risk", 0)) + float((it.get("plan") or {}).get("cost", 0))

    _last_tag: str | None = None
    _last_flatten: datetime | None = None

    def feed_bars(self, now: datetime) -> list:
        first = datetime.combine(self.day, self.session.open if self.cfg.entry == "sweep" else self.s.or_start, ET)
        start = self.last_bar_t + timedelta(minutes=1) if self.last_bar_t else first - timedelta(minutes=1)
        try:
            raw = self.api.retrieve_bars(self.spec.contract_id, start, now + timedelta(minutes=1), include_partial=True)
        except Exception as e:  # noqa: BLE001
            log.warning("Bars failed: %s", e)
            return []
        bars = sorted((Bar.from_api(b) for b in raw), key=lambda b: b.t)
        cur_minute = now.replace(second=0, microsecond=0)
        if bars:
            self.last_price = bars[-1].c
        done = [b for b in bars if b.t.astimezone(ET) < cur_minute]
        if done:
            self.fresh.bar("bars", done[-1].t)
        elif self.last_bar_t is not None:
            self.fresh.bar("bars", self.last_bar_t)
        out = []
        if self.strat is None:
            return out
        for b in done:
            if self.last_bar_t and b.t <= self.last_bar_t:
                continue
            self.last_bar_t = b.t
            sig = self.strat.on_bar(b)
            used = set(getattr(self.inner, "used", set()))
            new_used = used - self._known_used
            level = next(iter(new_used), None) if new_used else None
            if new_used:
                try:
                    self.store.mark_levels(self.day, new_used)
                except StateError:
                    pass
                self._known_used = used
            if sig is None:
                if new_used:
                    self.store.event("signal_filtered", f"{sorted(new_used)} at {b.t.astimezone(ET):%H:%M} (confirmation disagreed)")
                continue
            sig.level = level
            if now - sig.time > timedelta(minutes=2):
                self.strat.register_entry(sig.side)
                self.store.event("signal_stale", f"{sig.side} {level} at {sig.time.astimezone(ET):%H:%M} (bot started after it)")
                log.info("Skipping stale %s signal from %s ET (bot started after it)", sig.side,
                         sig.time.astimezone(ET).strftime("%H:%M"))
                continue
            out.append(sig)
        return out

    def entry_blockers(self, now: datetime, sig) -> list[str]:
        why = []
        if self.halted:
            why.append("halted for the day")
        if not self.store.healthy:
            why.append("state database unavailable")
        why += list(self.engine.block_reasons.values())
        if self.engine.active is not None:
            why.append(f"order {self.engine.active['tag']} still {self.engine.active['state']}")
        if self.engine.position():
            why.append("already in a position")
        why += [f"stale {x}" for x in self.fresh.stale(bool(self.bot.confirm_spec))]
        if sig.time > self.cutoff_at or now > self.cutoff_at + timedelta(minutes=2):
            why.append(f"after entry cutoff {self.s.entry_cutoff:%H:%M} ET")
        return why

    def consider(self, sig, now: datetime) -> None:
        level = getattr(sig, "level", None)
        tm = sig.time.astimezone(ET).strftime("%H:%M")
        why = self.entry_blockers(now, sig)
        if why:
            # Same accounting as the backtest: a signal that arrives while a trade is open (or after the
            # day is halted) is not counted; one refused for any other reason uses up a trade slot.
            if not (self.halted or self.engine.active is not None or self.engine.position()):
                self.strat.register_entry(sig.side)
            self.store.event("signal_blocked", f"{sig.side} {level} at {tm}: {'; '.join(why)}")
            log.info("Not taking %s signal at %s ET: %s", sig.side, tm, "; ".join(why))
            return
        reasons: list = []
        plan = plan_trade(sig, self.cfg.risk, self.spec, self.realized, self.bot.contracts, reasons)
        if plan is None:
            self.strat.register_entry(sig.side)
            self.store.event("signal_skipped", f"{sig.side} {level} at {tm}: {reasons[0] if reasons else '?'}")
            log.info("%s signal at %s ET skipped: %s", sig.side, tm, reasons[0] if reasons else "no size fits")
            return
        room = self.acct.headroom(0.0)
        if room is None:
            self.strat.register_entry(sig.side)
            self.store.event("signal_blocked", f"{sig.side} {level} at {tm}: account balance or MLL unknown")
            return
        if room < plan.worst_case_loss:
            self.strat.register_entry(sig.side)
            self.store.event("signal_blocked", f"{sig.side} {level} at {tm}: worst case ${plan.worst_case_loss:.0f} "
                                               f"> ${room:.0f} above the MLL buffer")
            return
        sl, tp = plan.bracket_ticks()
        log.info("SIGNAL %s @ %.2f (%s) | %d ct | stop %.2f pts ($%.0f + $%.0f costs) | target %.2f pts ($%.0f) %s",
                 sig.side.upper(), sig.price, level, plan.contracts, plan.stop_points, plan.risk_dollars,
                 plan.cost_dollars, plan.target_points, plan.reward_dollars, f"[{plan.note}]" if plan.note else "")
        self.strat.register_entry(sig.side)
        sides = list(self.store.day(self.day)["sides"]) + [sig.side]
        try:
            intent = self.engine.new_intent(self.day, sig, plan, level, [level] if level else [], sides)
            self.store.forward(intent["tag"], equity_before=self.acct.balance)
        except StateError:
            self.engine.block("state", "could not record the order intent; nothing was sent")
            return
        self.mfe = self.mae = 0.0
        self._last_tag = intent["tag"]
        self.engine.submit(intent, sl, tp)


def setup_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles
    except AttributeError:
        pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(Path(log_dir) / f"orbbot-{datetime.now():%Y%m%d}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers[:] = [sh, fh]


def main() -> None:
    ap = argparse.ArgumentParser(description="TopstepX futures bot (ORB or sweep entries)")
    ap.add_argument("--config", default="config.yaml")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="paper trade: live data, simulated fills, no orders sent")
    g.add_argument("--read-only", action="store_true", help="connect, reconcile and report; send nothing")
    g.add_argument("--live", action="store_true", help="real orders (also needs safety.live_trading_enabled: true)")
    ap.add_argument("--once", action="store_true", help="run today's session if there is one, then exit")
    ap.add_argument("--contracts", type=int, help="override contract count (max per config, up to 20)")
    a = ap.parse_args()
    if not (a.dry_run or a.read_only or a.live):
        raise SystemExit("Choose a mode: --dry-run (paper), --read-only, or --live. Live trading is off by default.")
    cfg = load_config(a.config)
    setup_logging(cfg.log_dir)
    mode = "paper" if a.dry_run else "readonly" if a.read_only else "live"
    bot = ORBBot(cfg, mode=mode, contracts=a.contracts)
    if mode == "readonly":
        raise SystemExit(bot.read_only())
    try:
        bot.run_forever(once=a.once)
    except KeyboardInterrupt:
        log.warning("Interrupted. Open positions keep their server-side brackets; restart to resume management.")
    finally:
        bot.journal.flush()


if __name__ == "__main__":
    main()
