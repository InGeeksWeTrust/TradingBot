"""Latency instrumentation. Logging only: nothing here changes what the bot does.

During a session every broker/data API call is timed (wall-clock start and end), and each poll
records its duration, the latest bar timestamp it received and the last price. Each signal gets
one row per stage:

  bar_close       the signal bar's close (the bar timestamp + 1 minute)
  poll_start      start of the poll that first received the completed bar
  bars_received   that poll's History/retrieveBars (MNQ) response
  recognized      the strategy returned the signal
  risk_done       sizing and entry checks finished (detail says why if the signal was refused)
  place_sent      Order/place sent
  place_response  Order/place returned
  fill_seen       the engine saw the position (intent FILLED)
  protected       the engine verified the stop (intent PROTECTED)

Rows are written to the `latency` table of the state database, one batch per poll. A failed write
is logged as a warning and dropped: it never marks the store unhealthy and never blocks an entry.
`python -m orbbot.latency_report` summarises the table.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

TABLE = """
CREATE TABLE IF NOT EXISTS latency (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,        -- api | poll | signal
    name TEXT NOT NULL,        -- api: call ("bars MNQ", "positions", ...); poll: "poll"; signal: stage
    poll TEXT,                 -- start of the poll the row belongs to (UTC)
    tag TEXT,                  -- order intent, once there is one
    sig TEXT,                  -- signal key, e.g. "short pdh 09:45"
    start_at TEXT NOT NULL,    -- UTC; for a signal stage, the time of the stage
    end_at TEXT,
    ms REAL,
    ok INTEGER,
    bar_t TEXT,                -- latest bar timestamp received (bars calls and polls)
    price REAL,                -- last price (bars calls and polls)
    detail TEXT
);
"""
STAGES = ["bar_close", "poll_start", "bars_received", "recognized", "risk_done", "place_sent",
          "place_response", "fill_seen", "protected"]
CALLS = {"retrieve_bars": "bars", "search_accounts": "account", "search_trades": "trades",
         "search_open_positions": "positions", "search_open_orders": "orders", "search_orders": "order_search",
         "place_order": "place", "cancel_order": "cancel", "close_position": "close"}


class Latency:
    def __init__(self, store, day: date, clock):
        self.store, self.day, self.clock = store, day, clock   # clock: aware datetime (UTC)
        self.rows: list[tuple] = []
        self.poll: datetime | None = None
        self.last_end: dict[str, datetime] = {}                # call name -> end of its latest call
        self._seen: set[tuple[str, str]] = set()
        self._warned = False
        try:
            store.db.executescript(TABLE)
        except sqlite3.Error as e:
            self._fail(e)

    def _fail(self, e) -> None:
        if not self._warned:
            log.warning("Latency log write failed (%s); latency rows are dropped, trading is unaffected", e)
            self._warned = True

    def _row(self, kind, name, start, end=None, *, tag=None, sig=None, ok=None, bar_t=None, price=None, detail=None):
        self.rows.append((self.day.isoformat(), kind, name, self.poll.isoformat() if self.poll else None, tag, sig,
                          start.isoformat(), end.isoformat() if end else None,
                          (end - start).total_seconds() * 1000 if end else None,
                          None if ok is None else int(ok), bar_t, price, detail))

    def api(self, name, start, end, ok, *, tag=None, bar_t=None, price=None, detail=None) -> None:
        self.last_end[name] = end
        self._row("api", name, start, end, tag=tag, ok=ok, bar_t=bar_t, price=price, detail=detail)

    def stage(self, name: str, at: datetime, *, sig=None, tag=None, detail=None) -> None:
        self._row("signal", name, at, sig=sig, tag=tag, detail=detail)

    def assign(self, sig: str, tag: str) -> None:
        """Give the not-yet-written stage rows of signal `sig` its intent tag."""
        self.rows = [r[:4] + (tag,) + r[5:] if r[1] == "signal" and r[5] == sig and r[4] is None else r
                     for r in self.rows]

    def on_transition(self, tag: str, state: str) -> None:
        stage = {"FILLED": "fill_seen", "PROTECTED": "protected"}.get(state)
        if stage and (tag, stage) not in self._seen:
            self._seen.add((tag, stage))
            self.stage(stage, self.clock(), tag=tag)

    def begin_poll(self) -> None:
        self.poll = self.clock()

    def end_poll(self, bar_t: str | None, price: float | None) -> None:
        if self.poll is not None:
            self._row("poll", "poll", self.poll, self.clock(), bar_t=bar_t, price=price)
        self.flush()
        self.poll = None

    def flush(self) -> None:
        if not self.rows:
            return
        try:
            self.store.db.executemany(
                "INSERT INTO latency(day, kind, name, poll, tag, sig, start_at, end_at, ms, ok, bar_t, price, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", self.rows)
        except sqlite3.Error as e:
            self._fail(e)
        finally:
            self.rows = []


class TimedApi:
    """Wraps the broker client and times every call into a Latency recorder. Everything else passes
    through unchanged; a failure while recording is swallowed."""

    def __init__(self, api, lat: Latency, symbols: dict[str, str]):
        self._api, self._lat, self._symbols = api, lat, symbols

    def __getattr__(self, attr):
        fn = getattr(self._api, attr)
        if attr not in CALLS:
            return fn

        def timed(*a, **kw):
            t0, out, ok = self._lat.clock(), None, False
            try:
                out = fn(*a, **kw)
                ok = True
                return out
            finally:
                try:
                    self._record(attr, a, kw, t0, self._lat.clock(), ok, out)
                except Exception:  # noqa: BLE001 - instrumentation must never break a call
                    log.debug("latency record failed", exc_info=True)
        return timed

    def _record(self, attr, a, kw, t0, t1, ok, out) -> None:
        lat, name = self._lat, CALLS[attr]
        if attr == "retrieve_bars":
            cid = a[0] if a else kw.get("contract_id")
            start = a[1] if len(a) > 1 else kw.get("start")
            end = a[2] if len(a) > 2 else kw.get("end")
            name = f"bars {self._symbols.get(cid, cid)}"
            if start is not None and end is not None and end - start > timedelta(days=1):
                name += " history"
            last = max(out, key=lambda b: str(b.get("t"))) if out else None   # the API returns newest first
            lat.api(name, t0, t1, ok, bar_t=last and last.get("t"), price=last and float(last["c"]),
                    detail=f"{len(out or [])} bars")
        elif attr == "place_order":
            tag = kw.get("custom_tag")
            lat.api(name, t0, t1, ok, tag=tag)
            if tag:
                lat.stage("place_sent", t0, tag=tag)
                lat.stage("place_response", t1, tag=tag, detail="ok" if ok else "error")
        else:
            lat.api(name, t0, t1, ok)
