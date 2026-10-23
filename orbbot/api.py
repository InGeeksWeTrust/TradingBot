"""Thin REST client for the ProjectX Gateway API (TopstepX).

Docs: https://gateway.docs.projectx.com/
Every endpoint is a POST with a JSON body and returns {success, errorCode, errorMessage, ...}.

Rate limits (per docs): History/retrieveBars 50 req / 30 s, everything else 200 req / 60 s.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)

TOPSTEPX_BASE = "https://api.topstepx.com"

# Enums from the Gateway docs (gateway.docs.projectx.com, realtime "Enums")
ORDER_LIMIT, ORDER_MARKET, ORDER_STOP_LIMIT, ORDER_STOP, ORDER_TRAILING_STOP = 1, 2, 3, 4, 5
STATUS_NONE, STATUS_OPEN, STATUS_FILLED, STATUS_CANCELLED, STATUS_EXPIRED, STATUS_REJECTED, STATUS_PENDING = range(7)
SIDE_BUY, SIDE_SELL = 0, 1
POS_LONG, POS_SHORT = 1, 2
BAR_SECOND, BAR_MINUTE, BAR_HOUR, BAR_DAY = 1, 2, 3, 4

TOKEN_MAX_AGE_S = 20 * 3600  # tokens last ~24h; refresh well before that


LOGIN_ERRORS = {
    1: "user not found: check TOPSTEPX_USERNAME (your TopstepX username, which may differ from your email)",
    2: "password verification failed",
    3: "invalid credentials: the username and API key don't match. Check both for typos or extra spaces, "
       "or generate a new key in TopstepX > Settings > API",
    7: "agreements not signed: log in to TopstepX/ProjectX and accept the pending agreements",
    9: "no API subscription found: subscribe to API access in the ProjectX dashboard and link it in TopstepX",
    10: "API key login is disabled for this user",
}


class ApiError(RuntimeError):
    """The gateway answered success=false. For Order/place, codes 6 (OrderPending) and 7 (UnknownError)
    do not say whether the order exists, and code 2 is also used for a duplicate customTag (the text is
    undocumented), so callers must reconcile instead of treating every ApiError as a rejection."""

    def __init__(self, path: str, code: Any, message: Any):
        super().__init__(f"{path} failed: errorCode={code} errorMessage={message}")
        self.path, self.code, self.message = path, code, message

    @property
    def ambiguous(self) -> bool:
        if self.code in (6, 7):
            return True
        return self.code == 2 and "tag" in str(self.message or "").lower()


class AmbiguousError(RuntimeError):
    """A non-idempotent request may or may not have reached the broker (timeout, dropped connection,
    HTTP 5xx, 401/429 on the only attempt). Never resend; reconcile."""

    def __init__(self, path: str, cause: Any):
        super().__init__(f"{path}: outcome unknown ({cause})")
        self.path, self.cause = path, cause


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ProjectXClient:
    def __init__(self, username: str, api_key: str, base_url: str = TOPSTEPX_BASE, timeout: float = 10.0):
        self.username = username
        self.api_key = api_key
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"accept": "text/plain", "Content-Type": "application/json"})
        self._token_time = 0.0

    # ---------- auth ----------
    def login(self) -> None:
        r = self.s.post(f"{self.base}/api/Auth/loginKey",
                        json={"userName": self.username, "apiKey": self.api_key}, timeout=self.timeout)
        r.raise_for_status()
        d = r.json()
        if not d.get("success"):
            code = d.get("errorCode")
            hint = LOGIN_ERRORS.get(code, d.get("errorMessage") or "unknown error")
            key = self.api_key
            raise SystemExit(f"TopstepX login failed (code {code}): {hint}\n"
                             f"  username used: [{self.username}]\n"
                             f"  API key used:  {len(key)} chars, ends ...{key[-4:] if len(key) >= 4 else '?'}")
        self.s.headers["Authorization"] = f"Bearer {d['token']}"
        self._token_time = time.time()
        log.info("Authenticated to %s", self.base)

    def _post(self, path: str, payload: dict, retries: int = 3, idempotent: bool = True) -> dict:
        """Reads (and idempotent calls) retry; a non-idempotent call (Order/place) is sent exactly once
        and any doubt about its outcome raises AmbiguousError for the caller to reconcile."""
        if time.time() - self._token_time > TOKEN_MAX_AGE_S:
            self.login()
        if not idempotent:
            try:
                r = self.s.post(f"{self.base}/api/{path}", json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                raise AmbiguousError(path, e) from e
            if r.status_code in (401, 429) or r.status_code >= 500:
                if r.status_code == 401:
                    try:
                        self.login()
                    except Exception:  # noqa: BLE001 - the caller reconciles either way
                        pass
                raise AmbiguousError(path, f"HTTP {r.status_code}")
            try:
                r.raise_for_status()
                d = r.json()
            except (requests.RequestException, ValueError) as e:
                raise AmbiguousError(path, e) from e
            if not d.get("success", False):
                raise ApiError(path, d.get("errorCode"), d.get("errorMessage"))
            return d
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                r = self.s.post(f"{self.base}/api/{path}", json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = e
                log.warning("%s network error (attempt %d): %s", path, attempt + 1, e)
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 401:
                log.warning("%s got 401, re-authenticating", path)
                self.login()
                continue
            if r.status_code == 429:
                wait = 5 * (attempt + 1)
                log.warning("%s rate limited, sleeping %ss", path, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            d = r.json()
            if not d.get("success", False):
                raise ApiError(path, d.get("errorCode"), d.get("errorMessage"))
            return d
        raise RuntimeError(f"{path}: retries exhausted ({last_err})")

    # ---------- reference data ----------
    def search_accounts(self, only_active: bool = True) -> list[dict]:
        return self._post("Account/search", {"onlyActiveAccounts": only_active})["accounts"]

    def search_contracts(self, text: str, live: bool = False) -> list[dict]:
        return self._post("Contract/search", {"searchText": text, "live": live})["contracts"]

    # ---------- market data ----------
    def retrieve_bars(self, contract_id: str, start: datetime, end: datetime, unit: int = BAR_MINUTE,
                      unit_number: int = 1, limit: int = 20000, include_partial: bool = False,
                      live: bool = False) -> list[dict]:
        d = self._post("History/retrieveBars", {
            "contractId": contract_id, "live": live,
            "startTime": iso(start), "endTime": iso(end),
            "unit": unit, "unitNumber": unit_number, "limit": limit,
            "includePartialBar": include_partial,
        })
        return d.get("bars") or []

    # ---------- orders / positions / trades ----------
    def place_order(self, account_id: int, contract_id: str, order_type: int, side: int, size: int,
                    limit_price: float | None = None, stop_price: float | None = None,
                    sl_ticks: int | None = None, tp_ticks: int | None = None,
                    custom_tag: str | None = None) -> int:
        payload: dict[str, Any] = {
            "accountId": account_id, "contractId": contract_id, "type": order_type, "side": side,
            "size": size, "limitPrice": limit_price, "stopPrice": stop_price, "trailPrice": None,
            # customTag must be unique across the account (gateway docs); used to find the order
            # again after an ambiguous response. It is NOT relied on to make resends safe: we never resend.
            "customTag": custom_tag,
        }
        if sl_ticks is not None:
            payload["stopLossBracket"] = {"ticks": sl_ticks, "type": ORDER_STOP}
        if tp_ticks is not None:
            payload["takeProfitBracket"] = {"ticks": tp_ticks, "type": ORDER_LIMIT}
        return self._post("Order/place", payload, idempotent=False)["orderId"]

    def cancel_order(self, account_id: int, order_id: int) -> None:
        self._post("Order/cancel", {"accountId": account_id, "orderId": order_id})

    def search_orders(self, account_id: int, start: datetime, end: datetime | None = None) -> list[dict]:
        """All orders (any status) created since `start`: used to find an order by its customTag."""
        payload = {"accountId": account_id, "startTimestamp": iso(start)}
        if end is not None:
            payload["endTimestamp"] = iso(end)
        return self._post("Order/search", payload).get("orders") or []

    def search_open_orders(self, account_id: int) -> list[dict]:
        return self._post("Order/searchOpen", {"accountId": account_id}).get("orders") or []

    def search_open_positions(self, account_id: int) -> list[dict]:
        return self._post("Position/searchOpen", {"accountId": account_id}).get("positions") or []

    def close_position(self, account_id: int, contract_id: str) -> None:
        self._post("Position/closeContract", {"accountId": account_id, "contractId": contract_id})

    def search_trades(self, account_id: int, start: datetime, end: datetime | None = None) -> list[dict]:
        payload = {"accountId": account_id, "startTimestamp": iso(start)}
        if end is not None:
            payload["endTimestamp"] = iso(end)
        return self._post("Trade/search", payload).get("trades") or []
