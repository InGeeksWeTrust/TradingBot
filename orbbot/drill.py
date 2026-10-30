"""Practice-account execution drill (Phase 4 Task 7). Sends ONE REAL 1-lot MNQ market order with a
20-tick stop / 20-tick target bracket to the Topstep Practice account, then closes it.

    python -m orbbot.drill --config config-sweep.yaml --live

Run it supervised, 12:00-15:00 ET, while the paper bot is idle. Gates, all required:
  * --live on the command line (CLAUDE.md rule 2: real orders need an explicit opt-in; the config's
    safety.live_trading_enabled stays false, so the bot's own --live path stays disarmed)
  * the hard-coded allowlist below: account id 28771375, named PRAC-V2-387652-21891938 (and any
    account whose name doesn't start with "PRAC-" is refused)
  * the operator types the account name exactly after seeing account, contract, side, size, bracket
  * 12:00-15:00 ET on a weekday; the paper bot's session not active (no heartbeat in the last 2 min
    and outside its session times); no open intent in state/paper.sqlite (read-only); the real
    account has no open position and no working order on any contract
  * its own single-instance lock, state-drill/drill.lock

The order goes through ExecutionEngine: Order/place is sent once; any ambiguous outcome is reconciled
by customTag (prefix "DRILL-"), never resent; the stop must be verified within protection_grace_s
or the position is flattened via Position/closeContract. Anything still open after 10 minutes is
closed. Ctrl+C flattens and verifies flat. At the end the account must be flat with no working
orders, and a summary prints every state transition, submit->ack latency, fill vs the last price at
submission (real slippage) and the bracket prices.

Isolation: state-drill/drill.sqlite (intents, transitions, events, forward row, latency table),
journal-drill/drill.csv, logs/drill-YYYYMMDD.log. Nothing here opens state/paper.sqlite for
writing, and state/paper_broker.json and journal/paper.* are never touched.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
import time as _time
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .api import ORDER_LIMIT, ProjectXClient
from .bot import MONTHS, acquire_instance_lock
from .config import BotConfig, load_config
from .execution import STOP_TYPES, S, ExecutionEngine, Freshness
from .latency import Latency, TimedApi
from .risk import ContractSpec
from .state import StateStore
from .strategy import ET

log = logging.getLogger("orbbot.drill")

# ---- immutable drill parameters (deliberately not configurable)
ALLOWED_ACCOUNT_ID = 28771375
ALLOWED_ACCOUNT_NAME = "PRAC-V2-387652-21891938"
SYMBOL = "MNQ"
SIZE = 1
BRACKET_TICKS = 20
WINDOW = (time(12, 0), time(15, 0))         # ET
MAX_OPEN_S = 600                            # close anything still open after 10 minutes
POLL_S = 2.0
PAPER_ALIVE_S = 120                         # a paper heartbeat this recent = its session is active
TAG_PREFIX = "DRILL-"
STATE_DIR, JOURNAL_DIR = "state-drill", "journal-drill"
JOURNAL_FIELDS = ["date", "tag", "account", "contract", "side", "contracts", "last_price_at_submit", "entry_time",
                  "entry", "slippage_ticks", "stop", "target", "exit_time", "exit", "reason", "gross_usd",
                  "fees_usd", "net_usd", "submit_ack_ms", "flat"]


class Refused(Exception):
    """The drill will not send an order."""


def side_for(day: date) -> str:
    """Alternates by date: even ordinal -> long, odd -> short."""
    return "long" if day.toordinal() % 2 == 0 else "short"


def drill_paths(root: Path) -> dict[str, Path]:
    return {"db": root / STATE_DIR / "drill.sqlite", "lock": root / STATE_DIR / "drill.lock",
            "journal": root / JOURNAL_DIR / "drill.csv"}


def check_account(accounts: list[dict]) -> dict:
    acct = next((a for a in accounts if str(a.get("id")) == str(ALLOWED_ACCOUNT_ID)), None)
    if acct is None:
        raise Refused(f"allowlisted account {ALLOWED_ACCOUNT_ID} not found among active accounts "
                      f"{[(a.get('id'), a.get('name')) for a in accounts]}")
    name = str(acct.get("name") or "")
    if not name.startswith("PRAC-"):
        raise Refused(f"account {acct['id']} is named '{name}', not a Practice (PRAC-) account")
    if name != ALLOWED_ACCOUNT_NAME:
        raise Refused(f"account {acct['id']} is named '{name}', expected '{ALLOWED_ACCOUNT_NAME}'")
    if acct.get("canTrade") is False:
        raise Refused(f"broker reports canTrade = false for {name}")
    return acct


def paper_problems(cfg: BotConfig, now: datetime) -> list[str]:
    """Reasons the paper bot is (or may be) busy. Reads state/paper.sqlite READ-ONLY."""
    out = []
    et = now.astimezone(ET)
    s = cfg.strategy
    prep = datetime.combine(et.date(), s.or_start, ET) - timedelta(minutes=5)
    end = datetime.combine(et.date(), s.flatten_time, ET) + timedelta(minutes=2)
    if et.weekday() in cfg.trading_days and prep <= et < end:
        out.append(f"inside the paper bot's session ({prep:%H:%M}-{end:%H:%M} ET)")
    p = Path(cfg.safety.state_dir) / "paper.sqlite"
    if not p.exists():
        return out
    db = sqlite3.connect(f"file:{p.resolve().as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "intents" in tables:
            opens = db.execute("SELECT tag, state FROM intents WHERE state NOT IN ('CLOSED','CANCELLED','REJECTED')"
                               ).fetchall()
            if opens:
                out.append(f"open intent(s) in {p}: {opens}")
        if "account" in tables:
            for key, val in db.execute("SELECT key, value FROM account WHERE key LIKE ?", (f"alive:{et.date()}:%",)):
                age = (now - datetime.fromisoformat(json.loads(val))).total_seconds()
                if age < PAPER_ALIVE_S:
                    out.append(f"paper session active (pid {key.rsplit(':', 1)[-1]} heartbeat {age:.0f}s ago)")
    finally:
        db.close()
    return out


class Drill:
    def __init__(self, cfg: BotConfig, api=None, clock=None, sleep=None, ask=input, root: Path = Path("."),
                 out=print):
        self.cfg = cfg
        self.raw_api = api if api is not None else ProjectXClient(cfg.username, cfg.api_key, cfg.base_url)
        self.clock = clock or (lambda: datetime.now(ET))
        self.sleep = sleep or _time.sleep
        self.ask, self.out = ask, out
        self.paths = drill_paths(root)
        self.exit_reason: str | None = None
        self.brackets: dict = {}

    def utc(self) -> datetime:
        return self.clock().astimezone(timezone.utc)

    # ------------------------------------------------------------------ checks
    def check_window(self) -> None:
        et = self.clock().astimezone(ET)
        if et.weekday() >= 5 or not (WINDOW[0] <= et.time() < WINDOW[1]):
            raise Refused(f"outside the drill window {WINDOW[0]:%H:%M}-{WINDOW[1]:%H:%M} ET on a weekday "
                          f"(now {et:%a %H:%M} ET)")

    def check_paper(self) -> None:
        why = paper_problems(self.cfg, self.utc())
        if why:
            raise Refused("; ".join(why))

    def check_flat(self, api) -> None:
        pos = [p for p in api.search_open_positions(ALLOWED_ACCOUNT_ID) if p.get("size")]
        orders = api.search_open_orders(ALLOWED_ACCOUNT_ID)
        if pos or orders:
            raise Refused(f"account is not flat: positions {[(p['contractId'], p['size']) for p in pos]}, "
                          f"working orders {[(o.get('contractId'), o.get('id')) for o in orders]}")

    def find_contract(self, api) -> tuple[ContractSpec, str]:
        import re
        pat = re.compile(rf"^{SYMBOL}[{MONTHS}]\d{{1,2}}$")
        cands = [c for c in api.search_contracts(SYMBOL) if c.get("activeContract") and pat.match(str(c["name"]).upper())]
        if not cands:
            raise Refused(f"no active {SYMBOL} contract found")
        c = cands[0]
        return ContractSpec(c["id"], float(c["tickSize"]), float(c["tickValue"])), c["name"]

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        self.paths["db"].parent.mkdir(parents=True, exist_ok=True)
        lock = acquire_instance_lock(self.paths["lock"])
        if lock is None:
            raise Refused(f"another drill holds {self.paths['lock']}")
        try:
            return self._run()
        finally:
            lock.close()

    def _run(self) -> int:
        cfg = self.cfg
        self.check_window()
        self.check_paper()
        raw = self.raw_api
        raw.login()
        acct = check_account(raw.search_accounts())
        spec, cname = self.find_contract(raw)
        self.check_flat(raw)

        day = self.clock().astimezone(ET).date()
        side = side_for(day)
        tick, pv = spec.tick_size, spec.point_value
        pts = BRACKET_TICKS * tick
        sl, tp = (-BRACKET_TICKS, BRACKET_TICKS) if side == "long" else (BRACKET_TICKS, -BRACKET_TICKS)
        self.out("")
        self.out("=== PRACTICE ACCOUNT EXECUTION DRILL: this sends a REAL order ===")
        self.out(f"  Account   {acct['name']} (id {acct['id']}, balance {acct.get('balance')})")
        self.out(f"  Contract  {cname} ({spec.contract_id}), tick {tick} = ${spec.tick_value:.2f}")
        self.out(f"  Order     {side.upper()} {SIZE} {SYMBOL} at MARKET (side alternates by date)")
        self.out(f"  Bracket   stop {BRACKET_TICKS} ticks ({pts:.2f} pts, ${pts * pv * SIZE:.2f}) / "
                 f"target {BRACKET_TICKS} ticks ({pts:.2f} pts, ${pts * pv * SIZE:.2f}), from the fill")
        self.out(f"  Limits    closed after {MAX_OPEN_S // 60} min if still open; Ctrl+C flattens")
        typed = self.ask(f"Type the account name to send this order (anything else aborts): ")
        if typed != acct["name"]:
            raise Refused("operator did not type the account name exactly; nothing was sent")
        self.check_window()                       # the operator may have taken a while
        self.check_paper()
        self.check_flat(raw)

        store = StateStore(self.paths["db"], clock=self.utc)
        lat = Latency(store, day, self.utc)
        api = TimedApi(raw, lat, {spec.contract_id: SYMBOL})
        fresh = Freshness(cfg.safety, self.utc)
        eng = ExecutionEngine(api, store, ALLOWED_ACCOUNT_ID, spec, cfg.safety, clock=self.utc,
                              sleep=self.sleep, mode="live")
        eng.on_transition = lat.on_transition
        sign = 1 if side == "long" else -1
        expected = lambda it, pos: float(pos["averagePrice"]) - sign * pts   # noqa: E731
        eng.recover(day, fresh, expected)         # settle any earlier drill intent against the broker
        if eng.active is not None:
            raise Refused(f"an earlier drill intent is still open: {eng.active['tag']} {eng.active['state']}")

        tag = intent = None
        try:
            bars = api.retrieve_bars(spec.contract_id, self.utc() - timedelta(minutes=5),
                                     self.utc() + timedelta(minutes=1), include_partial=True)
            if not bars:
                raise Refused("no current MNQ price available")
            last_px = float(bars[-1]["c"])
            tag = f"{TAG_PREFIX}{day:%Y%m%d}-{side}-{uuid.uuid4().hex[:10]}"
            plan = {"stop_ticks": BRACKET_TICKS, "target_ticks": BRACKET_TICKS, "stop_pts": pts, "target_pts": pts,
                    "risk": pts * pv * SIZE, "cost": cfg.risk.fee_per_contract_rt * SIZE, "level": "drill",
                    "signal_time": self.utc().isoformat(), "signal_price": last_px}
            store.create_intent(tag, day, spec.contract_id, side, SIZE, plan, [], [side])
            store.forward(tag, day=day.isoformat(), side=side, level="drill", signal_time=plan["signal_time"],
                          signal_price=last_px, stop_pts=pts, target_pts=pts, planned_risk=plan["risk"],
                          planned_cost=plan["cost"], mode="drill")
            intent = store.intent(tag)
            eng.active = intent
            store.event("drill_start", f"{tag}: {side} {SIZE} {cname}, last price {last_px}")
            started = self.utc()
            eng.submit(intent, sl, tp)
            self._manage(eng, fresh, lat, expected, started)
        except KeyboardInterrupt:
            self.exit_reason = "operator Ctrl+C"
            self.out("Ctrl+C: flattening and verifying flat ...")
            self._flatten(eng, fresh, "drill: operator Ctrl+C")
        if intent is not None:
            self._record(eng, store, api, intent, cname, acct)
        flat = self._final_check(eng, fresh, api, store)
        if intent is not None:
            self._journal(store, intent, cname, acct, flat)
            self._summary(store, intent, flat)
        lat.flush()
        store.close()
        return 0 if flat else 1

    def _manage(self, eng: ExecutionEngine, fresh: Freshness, lat: Latency, expected, started: datetime) -> None:
        while eng.active is not None:
            lat.begin_poll()
            try:
                eng.refresh(fresh)
                res = eng.manage(fresh, expected)
                it = eng.active
                if it is not None and it["state"] == S.PROTECTED.value and not self.brackets:
                    self._note_brackets(eng)
                if res == "flatten":
                    self.exit_reason = "protection missing"
                    self._flatten(eng, fresh, "drill: protection missing")
                    return
                if res == "closed":
                    self.exit_reason = "bracket"
                    return
                if eng.active is not None and (self.utc() - started).total_seconds() >= MAX_OPEN_S:
                    self.exit_reason = f"{MAX_OPEN_S // 60}-minute limit"
                    self._flatten(eng, fresh, f"drill: still open after {MAX_OPEN_S // 60} minutes")
                    return
            finally:
                lat.end_poll(None, None)
            if eng.active is not None:
                self.sleep(POLL_S)
        self.exit_reason = self.exit_reason or "order not filled"

    def _flatten(self, eng: ExecutionEngine, fresh: Freshness, why: str) -> None:
        for _ in range(2):                        # a second Ctrl+C during the flatten retries it once
            try:
                eng.flatten(why, fresh)
                return
            except KeyboardInterrupt:
                self.out("Still flattening; please wait ...")

    def _note_brackets(self, eng: ExecutionEngine) -> None:
        mine = [o for o in eng.open_orders if o.get("contractId") == eng.spec.contract_id]
        stop = next((o for o in mine if o.get("type") in STOP_TYPES), None)
        tgt = next((o for o in mine if o.get("type") == ORDER_LIMIT), None)
        self.brackets = {"stop": stop and stop.get("stopPrice"), "target": tgt and tgt.get("limitPrice")}
        eng.store.event("drill_brackets", json.dumps(self.brackets))

    def _record(self, eng, store: StateStore, api, intent: dict, cname: str, acct: dict) -> None:
        """Exit fields of the forward row from the broker's fills."""
        try:
            start = datetime.fromisoformat(intent["created_at"]) - timedelta(seconds=5)
            fills = [f for f in api.search_trades(ALLOWED_ACCOUNT_ID, start)
                     if not f.get("voided") and f["contractId"] == eng.spec.contract_id]
        except Exception as e:  # noqa: BLE001
            log.error("Could not read the drill's fills: %s", e)
            return
        opens = [f for f in fills if f.get("profitAndLoss") is None]
        closes = [f for f in fills if f.get("profitAndLoss") is not None]
        if not opens or not closes:
            return
        gross = sum(f["profitAndLoss"] for f in closes)
        fees = sum(float(f.get("fees") or 0) for f in fills)
        exit_px = sum(f["price"] * f["size"] for f in closes) / sum(f["size"] for f in closes)
        reason = self.exit_reason
        if reason == "bracket" and self.brackets:
            reason = ("target" if self.brackets.get("target") is not None
                      and abs(exit_px - self.brackets["target"]) < abs(exit_px - (self.brackets.get("stop") or 0))
                      else "stop")
        store.forward(intent["tag"], exit_time=closes[-1]["creationTimestamp"], exit_price=round(exit_px, 2),
                      exit_reason=reason, gross_pnl=round(gross, 2), fees=round(fees, 2),
                      net_pnl=round(gross - fees, 2))

    def _final_check(self, eng: ExecutionEngine, fresh: Freshness, api, store: StateStore) -> bool:
        """The whole account must be flat with no working orders; leftovers on MNQ are cleaned up once."""
        if not eng.refresh(fresh):
            eng.refresh(fresh)
        if eng.positions or eng.open_orders:
            if any(p["contractId"] == eng.spec.contract_id for p in eng.positions) or \
                    any(o.get("contractId") == eng.spec.contract_id for o in eng.open_orders):
                self._flatten(eng, fresh, "drill: final clean-up")
            eng.refresh(fresh)
        flat = eng.refresh(fresh) and not eng.positions and not eng.open_orders
        if flat:
            store.event("drill_end", "flat: no positions, no working orders")
        else:
            msg = (f"NOT FLAT: positions {[(p['contractId'], p['size']) for p in eng.positions]}, "
                   f"orders {[(o.get('contractId'), o.get('id')) for o in eng.open_orders]} — CHECK THE ACCOUNT")
            log.critical("Drill: %s", msg)
            store.event("drill_end", msg)
        return flat

    def _journal(self, store: StateStore, intent: dict, cname: str, acct: dict, flat: bool) -> None:
        r = next((x for x in store.forward_rows() if x["tag"] == intent["tag"]), {})
        tr = self._transitions(store, intent["tag"])
        at = {to: t for t, _, to, _ in reversed(tr)}
        ack_ms = ((at["ACKNOWLEDGED"] - at["SUBMITTING"]).total_seconds() * 1000
                  if "ACKNOWLEDGED" in at and "SUBMITTING" in at else None)
        p = self.paths["journal"]
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with p.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            if new:
                w.writeheader()
            w.writerow({"date": r.get("day"), "tag": intent["tag"], "account": acct["name"], "contract": cname,
                        "side": intent["side"], "contracts": r.get("fill_qty") or 0,
                        "last_price_at_submit": r.get("signal_price"), "entry_time": r.get("fill_time"),
                        "entry": r.get("fill_price"), "slippage_ticks": r.get("entry_slippage_ticks"),
                        "stop": self.brackets.get("stop"), "target": self.brackets.get("target"),
                        "exit_time": r.get("exit_time"), "exit": r.get("exit_price"), "reason": r.get("exit_reason"),
                        "gross_usd": r.get("gross_pnl"), "fees_usd": r.get("fees"), "net_usd": r.get("net_pnl"),
                        "submit_ack_ms": None if ack_ms is None else round(ack_ms, 1), "flat": flat})

    @staticmethod
    def _transitions(store: StateStore, tag: str) -> list[tuple]:
        rows = store.db.execute("SELECT at, from_state, to_state, detail FROM transitions WHERE tag = ? ORDER BY id",
                                (tag,)).fetchall()
        return [(datetime.fromisoformat(a), f, t, d) for a, f, t, d in rows]

    def _summary(self, store: StateStore, intent: dict, flat: bool) -> None:
        r = next((x for x in store.forward_rows() if x["tag"] == intent["tag"]), {})
        tr = self._transitions(store, intent["tag"])
        self.out("")
        self.out(f"=== Drill summary: {intent['tag']} ===")
        for t, frm, to, detail in tr:
            self.out(f"  {t.astimezone(ET):%H:%M:%S.%f}"[:-3] + f" ET  {frm or '-':>12} -> {to:<13} {detail or ''}")
        at = {to: t for t, _, to, _ in reversed(tr)}
        if "ACKNOWLEDGED" in at and "SUBMITTING" in at:
            self.out(f"  submit -> ack        {(at['ACKNOWLEDGED'] - at['SUBMITTING']).total_seconds() * 1000:.0f} ms")
        if r.get("fill_price") is not None:
            self.out(f"  last price at submit {r.get('signal_price')}")
            self.out(f"  fill price           {r.get('fill_price')}  (slippage {r.get('entry_slippage_ticks'):+.0f} "
                     f"ticks; positive = worse than the last price)")
        self.out(f"  bracket at broker    stop {self.brackets.get('stop')} / target {self.brackets.get('target')}")
        if r.get("exit_price") is not None:
            self.out(f"  exit                 {r.get('exit_price')} ({r.get('exit_reason')}), net ${r.get('net_pnl')}"
                     f" (gross ${r.get('gross_pnl')}, fees ${r.get('fees')})")
        else:
            self.out(f"  exit                 none recorded ({self.exit_reason})")
        self.out(f"  account at the end   {'FLAT, no working orders' if flat else 'NOT FLAT: CHECK THE ACCOUNT NOW'}")


def setup_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(Path(log_dir) / f"drill-{datetime.now():%Y%m%d}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers[:] = [sh, fh]


def main() -> None:
    ap = argparse.ArgumentParser(description="Practice-account execution drill: one REAL 1-lot MNQ order")
    ap.add_argument("--config", default="config-sweep.yaml")
    ap.add_argument("--live", action="store_true", help="required: this sends a real order to the Practice account")
    a = ap.parse_args()
    if not a.live:
        raise SystemExit("The drill sends a REAL order to the Practice account. Re-run with --live to proceed.")
    cfg = load_config(a.config)
    setup_logging(cfg.log_dir)
    try:
        code = Drill(cfg).run()
    except Refused as e:
        log.error("Drill refused: %s", e)
        raise SystemExit(2)
    except KeyboardInterrupt:
        log.warning("Interrupted outside the order loop. If an order was sent, check the account: "
                    "python -m orbbot.bot --config %s --read-only", a.config)
        raise SystemExit(3)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
