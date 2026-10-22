"""Live runner: Mon-Fri session loop against TopstepX.

Usage:
    python -m orbbot.bot --config config.yaml            # live (use a PRACTICE account first)
    python -m orbbot.bot --config config.yaml --dry-run  # paper: live data, simulated fills, no orders sent
    python -m orbbot.bot --config config.yaml --once     # run today's session (if any) then exit (for schedulers)
    python -m orbbot.bot --config config.yaml --contracts 20
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import (ORDER_MARKET, ORDER_STOP, SIDE_BUY, SIDE_SELL, ApiError,
                  ProjectXClient)
from .config import BotConfig, load_config
from .journal import Journal
from .paper import PaperClient
from .risk import (ContractSpec, DailyGuard, plan_trade, realized_from_trades,
                   trading_day_start, unrealized)
from .strategy import ET, Bar, ORBStrategy
from .features import Features
from .filters import ESConfirm, Filtered

log = logging.getLogger("orbbot")


def now_et() -> datetime:
    return datetime.now(ET)


class ORBBot:
    def __init__(self, cfg: BotConfig, dry_run: bool = False, contracts: int | None = None):
        self.cfg = cfg
        self.dry = dry_run
        self.contracts = contracts or cfg.risk.contracts
        if not 1 <= self.contracts <= cfg.risk.max_contracts:
            raise ValueError(f"contracts must be 1..{cfg.risk.max_contracts}")
        real = ProjectXClient(cfg.username, cfg.api_key, cfg.base_url)
        self.api = PaperClient(real, fee_per_contract_rt=cfg.risk.fee_per_contract_rt) if dry_run else real
        self.mode = "paper" if dry_run else "live"
        self.journal = Journal(cfg.journal_dir, self.mode, cfg.risk.max_daily_loss, cfg.risk.max_daily_profit)
        self.open_trade: dict | None = None   # round trip being tracked for the journal
        self.account_id: int | None = None
        self.spec: ContractSpec | None = None
        self.confirm_spec: ContractSpec | None = None
        self.done_day = None

    # ------------------------------------------------------------------ setup
    def connect(self) -> None:
        self.api.login()
        accounts = self.api.search_accounts()
        if not self.cfg.account_name:
            names = ", ".join(f"{a['name']} (id {a['id']})" for a in accounts)
            raise SystemExit(f"Set account_name in config. Available: {names}")
        match = [a for a in accounts if a["name"] == self.cfg.account_name or str(a["id"]) == str(self.cfg.account_name)]
        if not match:
            raise SystemExit(f"Account '{self.cfg.account_name}' not found among active accounts")
        acct = match[0]
        if not acct.get("canTrade", True):
            raise SystemExit(f"Account {acct['name']} cannot trade right now")
        self.account_id = acct["id"]
        log.info("Using account %s (id %s, balance %s)", acct["name"], acct["id"], acct.get("balance"))
        self.resolve_contract()

    def _find(self, symbol: str) -> ContractSpec:
        cs = self.api.search_contracts(symbol)
        cands = [c for c in cs if c.get("activeContract") and c["name"].upper().startswith(symbol.upper())]
        if not cands:
            raise SystemExit(f"No active contract found for {symbol}: {[c['name'] for c in cs]}")
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

    # ------------------------------------------------------------- broker ops
    def position(self) -> dict | None:
        for p in self.api.search_open_positions(self.account_id):
            if p["contractId"] == self.spec.contract_id and p.get("size"):
                return p
        return None

    def open_orders(self) -> list[dict]:
        return [o for o in self.api.search_open_orders(self.account_id) if o["contractId"] == self.spec.contract_id]

    def realized(self) -> float:
        trades = self.api.search_trades(self.account_id, trading_day_start(datetime.now(timezone.utc)))
        return realized_from_trades(trades, self.cfg.risk.include_fees_in_pnl)

    def cancel_all(self) -> None:
        for o in self.open_orders():
            try:
                self.api.cancel_order(self.account_id, o["id"])
                log.info("Cancelled order %s", o["id"])
            except ApiError as e:
                log.warning("Cancel %s failed: %s", o["id"], e)

    def flatten(self, why: str) -> None:
        log.warning("FLATTEN: %s", why)
        for attempt in range(3):
            if self.position():
                try:
                    self.api.close_position(self.account_id, self.spec.contract_id)
                except ApiError as e:
                    log.error("closeContract failed: %s", e)
            # Close first, THEN cancel: Auto-OCO bracket legs are not removed when another
            # order closes the position, and cancelling first would leave us unprotected.
            time.sleep(1)
            self.cancel_all()
            if not self.position():
                self.record_exit(why)
                return
            time.sleep(2)
        log.critical("Could not confirm flat after flatten attempts — CHECK THE ACCOUNT MANUALLY")

    # ---------------------------------------------------------------- journal
    def record_exit(self, reason: str | None) -> None:
        """Turn the fills since the entry order into one journal row."""
        ot, self.open_trade = self.open_trade, None
        if not ot:
            return
        try:
            fills = self.api.search_trades(self.account_id, ot["start"])
        except Exception:
            log.exception("Could not read fills for the journal")
            return
        fills = [f for f in fills if not f.get("voided") and f["contractId"] == self.spec.contract_id
                 and f["id"] not in ot["seen"]]  # only fills from this round trip
        opens = [f for f in fills if f.get("profitAndLoss") is None]
        closes = [f for f in fills if f.get("profitAndLoss") is not None]
        if not opens or not closes:
            log.warning("Journal: no complete round trip found for %s entry", ot["side"])
            return

        def vwap(fs):
            n = sum(f["size"] for f in fs)
            return sum(f["price"] * f["size"] for f in fs) / n, n

        def et(f):
            return datetime.fromisoformat(f["creationTimestamp"]).astimezone(ET).strftime("%H:%M:%S")

        entry, n = vwap(opens)
        exit_px, _ = vwap(closes)
        pnl = sum(f["profitAndLoss"] for f in closes)
        if self.cfg.risk.include_fees_in_pnl:
            pnl -= sum(float(f.get("fees") or 0) for f in fills)
        plan = ot["plan"]
        if reason is None:  # closed by a bracket leg: which one?
            move = (exit_px - entry) * (1 if ot["side"] == "long" else -1)
            reason = "target" if move >= 0.5 * plan.target_points else "stop" if move <= -0.5 * plan.stop_points else "other"
        self.journal.record({
            "date": ot["day"].isoformat(), "side": ot["side"], "contracts": n,
            "entry_time": et(opens[0]), "entry": round(entry, 2), "exit_time": et(closes[-1]),
            "exit": round(exit_px, 2), "reason": reason, "stop_pts": plan.stop_points,
            "target_pts": plan.target_points, "risk_usd": round(plan.risk_dollars, 2), "pnl_usd": round(pnl, 2),
        })

    # ---------------------------------------------------------------- session
    def is_trading_day(self, d) -> bool:
        return d.weekday() in self.cfg.trading_days and d not in self.cfg.holidays

    def run_forever(self, once: bool = False) -> None:
        self.connect()
        s = self.cfg.strategy
        while True:
            now = now_et()
            today = now.date()
            prep = datetime.combine(today, s.or_start, ET) - timedelta(minutes=5)
            end = datetime.combine(today, s.flatten_time, ET) + timedelta(minutes=2)
            if self.is_trading_day(today) and prep <= now < end and self.done_day != today:
                try:
                    self.run_session(today)
                    self.done_day = today  # halted, limit hit, or flattened at session end
                except Exception:
                    log.exception("Session crashed; attempting to flatten")
                    try:
                        self.flatten("session error")
                    except Exception:
                        log.exception("Flatten after crash failed — CHECK THE ACCOUNT MANUALLY")
                    time.sleep(30)
                    continue
                if once:
                    return
                continue
            if once and (not self.is_trading_day(today) or now >= end or self.done_day == today):
                log.info("--once: no session to run today (%s). Exiting.", today)
                return
            # sleep until next prep time (in chunks so Ctrl-C / restarts stay responsive)
            nxt = prep if now < prep and self.done_day != today else prep + timedelta(days=1)
            log.info("Idle. Next session check at %s ET", nxt.strftime("%a %Y-%m-%d %H:%M"))
            time.sleep(min(max((nxt - now_et()).total_seconds(), 5), 900))

    def _session(self):
        from .research import COMEX, NASDAQ, SESSION_FOR_SYMBOL, SESSIONS
        return SESSIONS[SESSION_FOR_SYMBOL.get(self.cfg.symbol.upper(), "nasdaq")]

    def _bars(self, contract_id, start, end, partial=False) -> list[Bar]:
        raw = self.api.retrieve_bars(contract_id, start, end, include_partial=partial)
        return sorted((Bar.from_api(b) for b in raw), key=lambda b: b.t)

    def build_sweep(self, day):
        """Sweep entry with prior-day / overnight levels from recent history, plus optional
        confirmation that the second market is on the trade's side of its VWAP."""
        from .research import Sweep
        session = self._session()
        now = now_et()
        hist = self._bars(self.spec.contract_id, now - timedelta(days=self.cfg.history_days), now)
        feats = Features(hist, session.open, session.close)
        lv = feats.day_levels(day)
        log.info("Sweep levels: prior-day high %s low %s | overnight high %s low %s",
                 lv.pdh, lv.pdl, lv.onh, lv.onl)
        strat = Sweep(self.cfg.strategy, self.spec.tick_size, feats, window="am", session=session)
        strat.reset(day)
        if self.confirm_spec:
            return Filtered(strat, [lambda sig: self.confirm is not None and self.confirm.check(sig.side, sig.time)])
        return strat

    def refresh_confirm(self, day) -> None:
        if not self.confirm_spec:
            return
        session = self._session()
        start = datetime.combine(day, session.open, ET)
        now = now_et()
        done = [b for b in self._bars(self.confirm_spec.contract_id, start, now)
                if b.t + timedelta(minutes=1) <= now]
        self.confirm = ESConfirm(done, session.open, session.close)

    def run_session(self, day) -> None:
        cfg, s = self.cfg, self.cfg.strategy
        self.resolve_contract()
        self.confirm = None
        if cfg.entry == "orb":
            strat = ORBStrategy(s, self.spec.tick_size)
            strat.reset(day)
        else:
            strat = None  # built once the session opens (levels need the overnight bars)
            sweep_ready_at = datetime.combine(day, self._session().open, ET) + timedelta(minutes=1)
        guard = DailyGuard(cfg.risk)
        realized = self.realized()
        flatten_at = datetime.combine(day, s.flatten_time, ET)
        log.info("=== Session %s | %s %s | contracts=%d | realized so far $%.2f | mode=%s ===",
                 day, cfg.symbol, cfg.entry + (f" + {cfg.confirm_symbol} confirm" if self.confirm_spec else ""),
                 self.contracts, realized, self.mode)

        pos = self.position()
        if pos:
            log.warning("Existing %s position of %s found at startup; managing it with the daily guard only",
                        "LONG" if pos["type"] == 1 else "SHORT", pos["size"])

        last_bar_t: datetime | None = None
        pending: dict | None = None      # entry awaiting fill/protection check
        last_realized_refresh = time.time()
        was_in_position = bool(pos)
        or_logged = False

        while True:
            now = now_et()

            # --- kill switch
            if Path(cfg.kill_switch_file).exists():
                self.flatten(f"kill switch file '{cfg.kill_switch_file}' present")
                log.warning("Kill switch active — halting for the day")
                return

            # --- sweep: build the strategy at the open, refresh the confirmation market
            if cfg.entry == "sweep":
                if strat is None and now >= sweep_ready_at:
                    strat = self.build_sweep(day)
                if strat is not None:
                    self.refresh_confirm(day)

            # --- market data: completed 1m bars (+ partial for last price)
            first = datetime.combine(day, self._session().open if cfg.entry == "sweep" else s.or_start, ET)
            start = last_bar_t + timedelta(minutes=1) if last_bar_t else first - timedelta(minutes=1)
            raw = self.api.retrieve_bars(self.spec.contract_id, start, now + timedelta(minutes=1),
                                         include_partial=True)
            bars = sorted((Bar.from_api(b) for b in raw), key=lambda b: b.t)
            cur_minute = now.replace(second=0, microsecond=0)
            last_price = bars[-1].c if bars else None
            signals = []
            for b in (bars if strat is not None else []):
                if b.t.astimezone(ET) >= cur_minute:
                    continue  # still forming
                if last_bar_t and b.t <= last_bar_t:
                    continue
                last_bar_t = b.t
                sig = strat.on_bar(b)
                if sig and now - sig.time > timedelta(minutes=2):
                    # Started late: this breakout happened before the bot was running. Count the
                    # side as taken, as you would have, but don't chase it at today's price.
                    strat.register_entry(sig.side)
                    log.info("Skipping stale %s signal from %s ET (bot started after it)",
                             sig.side, sig.time.astimezone(ET).strftime("%H:%M"))
                elif sig:
                    signals.append(sig)
            if isinstance(strat, ORBStrategy) and strat.or_ready and not or_logged:
                or_logged = True
                if strat.day_disabled:
                    log.info("No trading today: %s", strat.disabled_reason)
                else:
                    log.info("Opening range: high %.2f low %.2f (%.2f pts)",
                             strat.or_high, strat.or_low, strat.or_high - strat.or_low)

            # --- position + P&L
            pos = self.position()
            in_pos = bool(pos)
            if was_in_position and not in_pos:
                log.info("Position closed")
                time.sleep(1)
                self.cancel_all()          # remove any orphaned bracket leg
                self.record_exit(None)
                realized = self.realized()
                log.info("Realized today: $%.2f", realized)
                last_realized_refresh = time.time()
            elif time.time() - last_realized_refresh > 30:
                realized = self.realized()
                last_realized_refresh = time.time()
            was_in_position = in_pos
            open_pnl = unrealized(pos, last_price, self.spec) if (pos and last_price) else 0.0

            hit = guard.check(realized, open_pnl)
            if hit:
                log.warning("%s — flattening and halting for the day", guard.reason)
                self.flatten(guard.reason)
                return

            # --- protection check for a fresh entry
            if pending:
                if in_pos:
                    stops = [o for o in self.open_orders() if o["type"] == ORDER_STOP]
                    if stops:
                        log.info("Entry filled (%s %s @ %.2f); stop order %s working",
                                 pos["size"], pending["side"], pos["averagePrice"], stops[0]["id"])
                        pending = None
                    elif time.time() > pending["deadline"]:
                        guard.halted, guard.reason = True, "no protective stop found after entry"
                        self.flatten("no protective stop found after entry — check Auto OCO Brackets setting")
                        return
                elif time.time() > pending["deadline"] + 15:
                    log.warning("Entry order %s: no open position now; cancelling leftovers", pending["order_id"])
                    self.cancel_all()
                    self.record_exit(None)  # journals it if it filled and closed between polls
                    pending = None

            # --- time exit
            if now >= flatten_at:
                if in_pos:
                    self.flatten("session flatten time")
                else:
                    self.cancel_all()
                log.info("=== Session over. Realized $%.2f ===", self.realized())
                return

            # --- new entries
            for sig in signals:
                if guard.halted or in_pos or pending:
                    log.info("Ignoring %s signal (halted=%s in_pos=%s pending=%s)", sig.side, guard.halted, in_pos, bool(pending))
                    continue
                plan = plan_trade(sig, cfg.risk, self.spec, realized, self.contracts)
                if plan is None:
                    log.info("%s signal skipped: no size fits remaining risk (realized $%.2f)", sig.side, realized)
                    continue
                sl, tp = plan.bracket_ticks()
                log.info("SIGNAL %s @ %.2f | ref %.2f-%.2f | %d ct | stop %.2f pts ($%.0f) | target %.2f pts ($%.0f) %s",
                         sig.side.upper(), sig.price, sig.or_low, sig.or_high, plan.contracts,
                         plan.stop_points, plan.risk_dollars, plan.target_points, plan.reward_dollars,
                         f"[{plan.note}]" if plan.note else "")
                strat.register_entry(sig.side)
                self.cancel_all()
                day_start = trading_day_start(datetime.now(timezone.utc))
                self.open_trade = {"day": day, "side": sig.side, "plan": plan, "start": day_start,
                                   "seen": {f["id"] for f in self.api.search_trades(self.account_id, day_start)}}
                tag = f"{cfg.entry}-{day:%Y%m%d}-{sig.side}-{uuid.uuid4().hex[:8]}"
                try:
                    oid = self.api.place_order(self.account_id, self.spec.contract_id, ORDER_MARKET,
                                               SIDE_BUY if sig.side == "long" else SIDE_SELL, plan.contracts,
                                               sl_ticks=sl, tp_ticks=tp, custom_tag=tag)
                    log.info("Entry order %s sent (tag %s)", oid, tag)
                    pending = {"order_id": oid, "side": sig.side, "deadline": time.time() + 15}
                except ApiError as e:
                    log.error("Order rejected: %s", e)
                    # A rejected/ambiguous order could still have filled — reconcile next loop.
                    pending = {"order_id": None, "side": sig.side, "deadline": time.time() + 10}

            self.journal.flush()  # retries the workbook if it was locked in Excel

            time.sleep(cfg.poll_seconds)


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
    ap.add_argument("--dry-run", action="store_true", help="paper trade: live data, simulated fills, no orders sent")
    ap.add_argument("--once", action="store_true", help="run today's session if there is one, then exit")
    ap.add_argument("--contracts", type=int, help="override contract count (max per config, up to 20)")
    a = ap.parse_args()
    cfg = load_config(a.config)
    setup_logging(cfg.log_dir)
    bot = ORBBot(cfg, dry_run=a.dry_run, contracts=a.contracts)
    try:
        bot.run_forever(once=a.once)
    except KeyboardInterrupt:
        log.warning("Interrupted. Open positions keep their server-side brackets; flatten manually if needed.")
    finally:
        bot.journal.flush()


if __name__ == "__main__":
    main()
