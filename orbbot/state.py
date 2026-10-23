"""Durable bot state in SQLite: order intents and their state transitions, sweep levels used,
daily trade counts, risk-halt state, account trailing data, events, and forward-test records.

The broker is the authority on orders and positions; this store is what lets the bot know,
after a crash or restart, what it *meant* to do (so it never resends an order or re-trades a
level) and gives the forward test a complete audit trail.

Any write failure marks the store unhealthy; the bot then refuses new entries (it keeps
managing an open position). A file that fails SQLite's integrity check at start-up is moved
aside as `<name>.corrupt-<time>` and a fresh one is created; the session that finds this does
not open new trades (local history is gone, so the broker state alone decides what exists).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    tag TEXT PRIMARY KEY,           -- unique per order intent; sent to the broker as customTag
    day TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,             -- entry
    contract_id TEXT NOT NULL,
    side TEXT NOT NULL,             -- long | short
    size INTEGER NOT NULL,
    state TEXT NOT NULL,
    broker_order_id INTEGER,
    filled_qty INTEGER DEFAULT 0,
    plan TEXT,                      -- JSON: stop/target ticks and points, risk, level, signal
    updated_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL, at TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS levels (
    day TEXT NOT NULL, name TEXT NOT NULL, used_at TEXT NOT NULL, PRIMARY KEY (day, name)
);
CREATE TABLE IF NOT EXISTS days (
    day TEXT PRIMARY KEY, trades INTEGER DEFAULT 0, sides TEXT DEFAULT '[]',
    halted INTEGER DEFAULT 0, halt_reason TEXT, no_entries INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS account (
    key TEXT PRIMARY KEY, value TEXT NOT NULL   -- start_balance, peak_eod_balance, last_eod_day
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS forward (
    tag TEXT PRIMARY KEY, day TEXT, side TEXT, level TEXT,
    signal_time TEXT, signal_price REAL, submit_time TEXT, ack_time TEXT, broker_order_id INTEGER,
    fill_time TEXT, fill_price REAL, fill_qty INTEGER, stop_price REAL, target_price REAL,
    stop_pts REAL, target_pts REAL, planned_risk REAL, planned_cost REAL,
    entry_slippage_ticks REAL, exit_time TEXT, exit_price REAL, exit_reason TEXT,
    gross_pnl REAL, fees REAL, net_pnl REAL, mfe_pts REAL, mae_pts REAL,
    equity_before REAL, equity_after REAL, mode TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateError(RuntimeError):
    pass


class StateStore:
    def __init__(self, path: str | Path, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.path = Path(path)
        self.healthy = True
        self.recovered_from_corruption = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = self._open()

    # ------------------------------------------------------------------ open / recover
    def _open(self) -> sqlite3.Connection:
        try:
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            ok = db.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                raise sqlite3.DatabaseError(ok)
            db.executescript(SCHEMA)
            db.execute("PRAGMA journal_mode=WAL")
            return db
        except sqlite3.DatabaseError as e:
            try:
                db.close()  # type: ignore[possibly-undefined]
            except Exception:  # noqa: BLE001
                pass
            bad = self.path.with_name(f"{self.path.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}")
            self.path.replace(bad)
            for side in (self.path.with_name(self.path.name + "-wal"), self.path.with_name(self.path.name + "-shm")):
                if side.exists():
                    side.replace(bad.with_name(bad.name + side.name[len(self.path.name):]))
            log.critical("State database %s was unreadable (%s); moved to %s and started fresh. "
                         "No new entries this session.", self.path, e, bad)
            self.recovered_from_corruption = True
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            db.executescript(SCHEMA)
            self._event_on(db, "state_recovered", f"corrupt file moved to {bad.name}: {e}")
            return db

    def now(self) -> str:
        return self.clock().astimezone(timezone.utc).isoformat()

    def _event_on(self, db, kind, detail):
        db.execute("INSERT INTO events(at, kind, detail) VALUES (?,?,?)", (self.now(), kind, detail))

    def _exec(self, sql: str, args=()) -> sqlite3.Cursor:
        try:
            return self.db.execute(sql, args)
        except sqlite3.Error as e:
            self.healthy = False
            log.critical("State database write/read failed (%s): new entries are blocked", e)
            raise StateError(str(e)) from e

    def _tx(self, statements: list[tuple[str, tuple]]) -> None:
        """Run several statements atomically."""
        try:
            self.db.execute("BEGIN IMMEDIATE")
            for sql, args in statements:
                self.db.execute(sql, args)
            self.db.execute("COMMIT")
        except sqlite3.Error as e:
            try:
                self.db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            self.healthy = False
            log.critical("State database transaction failed (%s): new entries are blocked", e)
            raise StateError(str(e)) from e

    def close(self) -> None:
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ events
    def event(self, kind: str, detail: str = "") -> None:
        log.info("[state] %s: %s", kind, detail)
        try:
            self._exec("INSERT INTO events(at, kind, detail) VALUES (?,?,?)", (self.now(), kind, detail))
        except StateError:
            pass  # already logged and marked unhealthy

    def events(self, kind: str | None = None) -> list[tuple]:
        q = "SELECT at, kind, detail FROM events" + (" WHERE kind=?" if kind else "") + " ORDER BY id"
        return self._exec(q, (kind,) if kind else ()).fetchall()

    # ------------------------------------------------------------------ intents
    def create_intent(self, tag: str, day: date, contract_id: str, side: str, size: int, plan: dict,
                      levels: list[str], sides: list[str]) -> None:
        """Record the intent, mark its level(s) used and count the trade in ONE transaction, before
        anything is sent, so a crash between here and the broker can never lead to a second entry."""
        now = self.now()
        d = day.isoformat()
        stmts = [
            ("INSERT INTO intents(tag, day, created_at, kind, contract_id, side, size, state, plan, updated_at) "
             "VALUES (?,?,?,?,?,?,?,?,?,?)", (tag, d, now, "entry", contract_id, side, size, "CREATED", json.dumps(plan), now)),
            ("INSERT INTO transitions(tag, at, from_state, to_state, detail) VALUES (?,?,?,?,?)",
             (tag, now, None, "CREATED", f"{side} {size}")),
            ("INSERT OR IGNORE INTO days(day) VALUES (?)", (d,)),
            ("UPDATE days SET trades = trades + 1, sides = ? WHERE day = ?", (json.dumps(sorted(set(sides))), d)),
        ]
        stmts += [("INSERT OR IGNORE INTO levels(day, name, used_at) VALUES (?,?,?)", (d, n, now)) for n in levels]
        self._tx(stmts)

    def set_state(self, tag: str, new: str, old: str | None, detail: str = "", **fields) -> None:
        now = self.now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        sql = f"UPDATE intents SET state = ?, updated_at = ?{', ' + cols if cols else ''} WHERE tag = ?"
        self._tx([(sql, (new, now, *fields.values(), tag)),
                  ("INSERT INTO transitions(tag, at, from_state, to_state, detail) VALUES (?,?,?,?,?)",
                   (tag, now, old, new, detail))])

    def intent(self, tag: str) -> dict | None:
        cur = self._exec("SELECT * FROM intents WHERE tag = ?", (tag,))
        row = cur.fetchone()
        return self._row(cur, row) if row else None

    def open_intents(self) -> list[dict]:
        cur = self._exec("SELECT * FROM intents WHERE state NOT IN ('CLOSED','CANCELLED','REJECTED') ORDER BY created_at")
        return [self._row(cur, r) for r in cur.fetchall()]

    def intents_for_day(self, day: date) -> list[dict]:
        cur = self._exec("SELECT * FROM intents WHERE day = ? ORDER BY created_at", (day.isoformat(),))
        return [self._row(cur, r) for r in cur.fetchall()]

    def transitions(self, tag: str) -> list[tuple]:
        return self._exec("SELECT from_state, to_state, detail FROM transitions WHERE tag = ? ORDER BY id",
                          (tag,)).fetchall()

    @staticmethod
    def _row(cur, row) -> dict:
        d = dict(zip([c[0] for c in cur.description], row))
        if d.get("plan"):
            d["plan"] = json.loads(d["plan"])
        return d

    # ------------------------------------------------------------------ levels / day
    def mark_levels(self, day: date, names) -> None:
        now = self.now()
        self._tx([("INSERT OR IGNORE INTO levels(day, name, used_at) VALUES (?,?,?)", (day.isoformat(), n, now))
                  for n in names])

    def used_levels(self, day: date) -> set[str]:
        return {r[0] for r in self._exec("SELECT name FROM levels WHERE day = ?", (day.isoformat(),)).fetchall()}

    def day(self, day: date) -> dict:
        cur = self._exec("SELECT trades, sides, halted, halt_reason, no_entries FROM days WHERE day = ?",
                         (day.isoformat(),))
        r = cur.fetchone()
        if not r:
            return {"trades": 0, "sides": [], "halted": False, "halt_reason": "", "no_entries": False}
        return {"trades": r[0], "sides": json.loads(r[1] or "[]"), "halted": bool(r[2]), "halt_reason": r[3] or "",
                "no_entries": bool(r[4])}

    def halt_day(self, day: date, reason: str, flatten: bool = False) -> None:
        d = day.isoformat()
        self._tx([("INSERT OR IGNORE INTO days(day) VALUES (?)", (d,)),
                  ("UPDATE days SET halted = 1, halt_reason = ? WHERE day = ?", (reason, d)),
                  ("INSERT INTO events(at, kind, detail) VALUES (?,?,?)", (self.now(), "halt", reason))])

    def block_entries(self, day: date, reason: str) -> None:
        d = day.isoformat()
        self._tx([("INSERT OR IGNORE INTO days(day) VALUES (?)", (d,)),
                  ("UPDATE days SET no_entries = 1, halt_reason = COALESCE(halt_reason, ?) WHERE day = ?", (reason, d)),
                  ("INSERT INTO events(at, kind, detail) VALUES (?,?,?)", (self.now(), "entries_blocked", reason))])

    # ------------------------------------------------------------------ account
    def get(self, key: str, default=None):
        r = self._exec("SELECT value FROM account WHERE key = ?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, key: str, value) -> None:
        self._exec("INSERT INTO account(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (key, json.dumps(value)))

    # ------------------------------------------------------------------ forward-test record
    def forward(self, tag: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        upd = ", ".join(f"{k} = excluded.{k}" for k in fields)
        self._exec(f"INSERT INTO forward(tag, {cols}) VALUES (?, {marks}) ON CONFLICT(tag) DO UPDATE SET {upd}",
                   (tag, *fields.values()))

    def forward_rows(self) -> list[dict]:
        cur = self._exec("SELECT * FROM forward ORDER BY signal_time")
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]


def export_forward(db_path: str, out_csv: str) -> int:
    """python -m orbbot.state state/paper.sqlite forward.csv  -> every forward-test trade as CSV."""
    import csv
    s = StateStore(db_path)
    rows = s.forward_rows()
    with open(out_csv, "w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    return len(rows)


if __name__ == "__main__":
    import sys
    n = export_forward(sys.argv[1], sys.argv[2])
    print(f"Wrote {n} forward-test trades to {sys.argv[2]}")
