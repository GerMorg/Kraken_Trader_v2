from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import threading
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app import __version__

SPOT_BASE = "https://api.kraken.com"

class KrakenAPIError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code[:300])
        self.code = code[:300]

class KrakenAmbiguousError(KrakenAPIError):
    """Private request outcome is unknown; never retry order submission blindly."""

def sign_spot(uri_path: str, params: dict[str, Any], api_secret: str) -> str:
    nonce = str(params["nonce"])
    encoded = urlencode(params, doseq=True)
    digest = hashlib.sha256((nonce + encoded).encode("utf-8")).digest()
    message = uri_path.encode("utf-8") + digest
    key = base64.b64decode(api_secret)
    return base64.b64encode(hmac.new(key, message, hashlib.sha512).digest()).decode("ascii")

def _decode_response(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KrakenAPIError("KRAKEN_RESPONSE_INVALID_JSON") from exc
    if not isinstance(value, dict):
        raise KrakenAPIError("KRAKEN_RESPONSE_INVALID_ROOT")
    return value

REQUIRED_ACCOUNT_QUERY_PERMISSIONS = frozenset({
    "query-funds",
    "query-open-trades",
    "query-closed-trades",
    "query-ledger",
})
WITHDRAWAL_PERMISSIONS = frozenset({
    "withdraw-funds",
    "add-withdraw-address",
    "update-withdraw-address",
})
ORDER_WRITE_PERMISSIONS = frozenset({"modify-trades", "close-trades"})


def _nonnegative_epoch(value: Any) -> int | None:
    try:
        epoch = int(str(value))
    except (ValueError, TypeError, OverflowError):
        return None
    return epoch if epoch >= 0 else None


def api_key_permission_gate_reasons(
    assessment: dict[str, Any],
    *,
    validate_shadow_orders: bool = False,
    live_requested: bool = False,
) -> list[str]:
    """Evaluate whether observed key scopes permit this runtime's requested behavior."""
    reasons: list[str] = []
    if assessment.get("missing_required_query_permissions"):
        reasons.append("REQUIRED_QUERY_PERMISSION_MISSING")
    if assessment.get("withdrawal_permissions_present"):
        reasons.append("WITHDRAWAL_PERMISSION_PRESENT")
    if assessment.get("api_key_expired_or_expiry_unknown"):
        reasons.append("API_KEY_EXPIRED_OR_EXPIRY_UNKNOWN")
    if assessment.get("historical_query_window_restricted"):
        reasons.append("API_KEY_HISTORY_QUERY_WINDOW_RESTRICTED")
    if assessment.get("api_key_metadata_complete") is not True:
        reasons.append("API_KEY_METADATA_INCOMPLETE")
    permissions = assessment.get("permissions")
    if not isinstance(permissions, list):
        permissions = []
        reasons.append("API_KEY_PERMISSIONS_UNKNOWN")
    if validate_shadow_orders and "modify-trades" not in permissions:
        reasons.append("SHADOW_ORDER_VALIDATION_PERMISSION_MISSING")
    if live_requested:
        missing_write = assessment.get("missing_order_write_permissions")
        if not isinstance(missing_write, list) or missing_write:
            reasons.append("LIVE_ORDER_PERMISSIONS_MISSING")
    if assessment.get("ready_for_account_reconciliation") is not True and not reasons:
        # Fail closed if the assessment schema evolves or an inconsistent assessment
        # is passed in; readiness must never be inferred from missing failure fields.
        reasons.append("API_KEY_ASSESSMENT_NOT_READY")
    return list(dict.fromkeys(reasons))


def assess_api_key_info(
    response: dict[str, Any], *, now_epoch: int | None = None
) -> dict[str, Any]:
    """Return a credential-safe API key permission assessment.

    Kraken's GetApiKeyInfo response contains the API key itself. This function
    deliberately returns only allowlisted, non-secret fields and never copies
    the raw response into logs or persistent event data.
    """
    raw_permissions = response.get("permissions")
    if not isinstance(raw_permissions, list) or any(
        not isinstance(item, str) or not item.strip()
        for item in raw_permissions
    ):
        raise KrakenAPIError("KRAKEN_API_KEY_INFO_PERMISSIONS_INVALID")

    permissions = sorted({item.strip() for item in raw_permissions})
    permission_set = set(permissions)
    missing_query = sorted(REQUIRED_ACCOUNT_QUERY_PERMISSIONS - permission_set)
    withdrawal = sorted(WITHDRAWAL_PERMISSIONS & permission_set)
    missing_order_write = sorted(ORDER_WRITE_PERMISSIONS - permission_set)
    valid_until = _nonnegative_epoch(response.get("validUntil"))
    query_from = _nonnegative_epoch(response.get("queryFrom"))
    query_to = _nonnegative_epoch(response.get("queryTo"))
    now = int(time.time()) if now_epoch is None else int(now_epoch)

    metadata_complete = all(
        value is not None for value in (valid_until, query_from, query_to)
    )
    key_expired = valid_until is None or (
        valid_until != 0 and valid_until <= now
    )
    history_window_restricted = (
        query_from is None
        or query_to is None
        or query_from > 0
        or (query_to > 0 and query_to <= now)
    )
    ready_for_account_reconciliation = (
        metadata_complete
        and not missing_query
        and not withdrawal
        and not key_expired
        and not history_window_restricted
    )
    return {
        "permissions": permissions,
        "missing_required_query_permissions": missing_query,
        "withdrawal_permissions_present": withdrawal,
        "missing_order_write_permissions": missing_order_write,
        "api_key_metadata_complete": metadata_complete,
        "api_key_expired_or_expiry_unknown": key_expired,
        "historical_query_window_restricted": history_window_restricted,
        "api_key_valid_until": valid_until,
        "api_key_query_from": query_from,
        "api_key_query_to": query_to,
        "ready_for_account_reconciliation": ready_for_account_reconciliation,
    }

class KrakenClient:
    MIN_PUBLIC_REQUEST_INTERVAL_SECONDS = 1.0

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        nonce_provider: Callable[[], int] | None = None,
        timeout_seconds: float = 15.0,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.api_key, self.api_secret = api_key or "", api_secret or ""
        self.nonce_provider = nonce_provider or (lambda: time.time_ns() // 1000)
        self.timeout_seconds = timeout_seconds
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._public_rate_lock = threading.Lock()
        self._last_public_request_at: float | None = None

    def _wait_for_public_rate_limit(self) -> None:
        # Kraken's public Spot REST endpoints share a conservative per-client
        # request cadence. Serialize reservation of request-start times to avoid
        # bursts when the runtime is extended with concurrent market scans.
        with self._public_rate_lock:
            now = self._clock()
            if self._last_public_request_at is not None:
                elapsed = now - self._last_public_request_at
                wait_seconds = self.MIN_PUBLIC_REQUEST_INTERVAL_SECONDS - elapsed
                if wait_seconds > 0:
                    self._sleeper(wait_seconds)
            self._last_public_request_at = self._clock()

    def public(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._wait_for_public_rate_limit()
        encoded = urlencode(params or {}, doseq=True)
        url = f"{SPOT_BASE}/0/public/{method}" + (("?" + encoded) if encoded else "")
        request = Request(url, headers={"User-Agent":f"KrakenTraderV2/{__version__}"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310 -- fixed Kraken HTTPS origin; in-code endpoint names only
                value = _decode_response(response.read())
        except (TimeoutError, URLError, OSError) as exc:
            raise KrakenAPIError("KRAKEN_PUBLIC_NETWORK_ERROR") from exc
        if value.get("error"):
            raise KrakenAPIError("KRAKEN_PUBLIC:" + ";".join(str(x) for x in value["error"]))
        result = value.get("result", {})
        if not isinstance(result, dict):
            raise KrakenAPIError("KRAKEN_PUBLIC_RESULT_INVALID")
        return result

    def private(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key or not self.api_secret:
            raise KrakenAPIError("KRAKEN_CREDENTIALS_MISSING")
        path = f"/0/private/{method}"
        payload = dict(params or {})
        payload["nonce"] = str(self.nonce_provider())
        encoded = urlencode(payload, doseq=True).encode("utf-8")
        try:
            signature = sign_spot(path, payload, self.api_secret)
        except (ValueError, TypeError) as exc:
            raise KrakenAPIError("KRAKEN_PRIVATE_SIGNING_CONFIGURATION_INVALID") from exc
        request = Request(
            SPOT_BASE + path, data=encoded, method="POST",
            headers={"API-Key": self.api_key, "API-Sign": signature,
                     "Content-Type":"application/x-www-form-urlencoded", "User-Agent":f"KrakenTraderV2/{__version__}"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310 -- fixed Kraken HTTPS origin; endpoint names are static
                try:
                    value = _decode_response(response.read())
                except KrakenAPIError as exc:
                    if method == "AddOrder":
                        raise KrakenAmbiguousError("ADD_ORDER_RESPONSE_PARSE_OUTCOME_UNKNOWN") from exc
                    raise
        except HTTPError as exc:
            try:
                value = _decode_response(exc.read())
            except (KrakenAPIError, TimeoutError, URLError, OSError) as body_exc:
                if method == "AddOrder":
                    raise KrakenAmbiguousError("ADD_ORDER_HTTP_OUTCOME_UNKNOWN") from body_exc
                raise KrakenAPIError("KRAKEN_PRIVATE_HTTP_ERROR") from body_exc
            # A parsed, explicit Kraken error is a definite rejection. An HTTP
            # error without one is not proof that the order was not accepted.
            if method == "AddOrder" and not value.get("error"):
                raise KrakenAmbiguousError("ADD_ORDER_HTTP_STATUS_OUTCOME_UNKNOWN") from exc
        except (TimeoutError, URLError, OSError) as exc:
            if method == "AddOrder":
                raise KrakenAmbiguousError("ADD_ORDER_NETWORK_OUTCOME_UNKNOWN") from exc
            raise KrakenAPIError("KRAKEN_PRIVATE_NETWORK_ERROR") from exc
        if value.get("error"):
            raise KrakenAPIError("KRAKEN_PRIVATE:" + ";".join(str(x) for x in value["error"]))
        result = value.get("result", {})
        if not isinstance(result, dict):
            if method == "AddOrder":
                raise KrakenAmbiguousError("ADD_ORDER_RESULT_SHAPE_OUTCOME_UNKNOWN")
            raise KrakenAPIError("KRAKEN_PRIVATE_RESULT_INVALID")
        return result

    def api_key_permission_assessment(self, *, now_epoch: int | None = None) -> dict[str, Any]:
        """Check required read scopes and reject overly privileged/limited API keys."""
        response = self.private("GetApiKeyInfo")
        return assess_api_key_info(response, now_epoch=now_epoch)

    def account_snapshot(self) -> dict[str, Any]:
        balance = self.private("Balance")
        trade = self.private("TradeBalance", {"aclass":"currency","asset":"ZUSD"})
        orders = self.private("OpenOrders", {"trades":True})
        closed = self.closed_orders(include_trades=True)
        positions = self.private("OpenPositions", {"docalcs": True})
        return {
            "balance": balance,
            "trade_balance": trade,
            "open_orders": orders,
            "closed_orders": closed,
            "open_positions": positions,
            "captured_at": time.time(),
            # Successful API replies are not proof that fills, ledgers, loans and wallet assets reconcile.
            "api_queries_succeeded": True,
            "reconciliation_complete": False,
        }

    def closed_orders(self, *, offset: int = 0, start: int | None = None,
                      end: int | None = None, close_time: str = "both",
                      include_trades: bool = True, with_cursor: bool = False,
                      cursor: str | None = None) -> dict[str, Any]:
        """Fetch closed-order history; cursor mode follows Kraken's current pagination contract."""
        if offset < 0:
            raise ValueError("CLOSED_ORDERS_OFFSET_MUST_BE_NON_NEGATIVE")
        if with_cursor and offset != 0:
            raise ValueError("CLOSED_ORDERS_CURSOR_AND_OFFSET_MUTUALLY_EXCLUSIVE")
        if close_time not in {"open", "close", "both"}:
            raise ValueError("CLOSED_ORDERS_CLOSE_TIME_INVALID")
        params: dict[str, Any] = {
            "closetime": close_time,
            "trades": include_trades,
        }
        if with_cursor:
            params["with_cursor"] = True
            if cursor:
                params["cursor"] = cursor
        else:
            params["ofs"] = offset
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        return self.private("ClosedOrders", params)

    def trades_history(self, *, offset: int = 0, start: int | str | None = None,
                       end: int | str | None = None, with_cursor: bool = False,
                       cursor: str | None = None) -> dict[str, Any]:
        """Fetch trade/fill history, supporting the current cursor pagination API."""
        if offset < 0:
            raise ValueError("TRADES_HISTORY_OFFSET_MUST_BE_NON_NEGATIVE")
        if with_cursor and offset != 0:
            raise ValueError("TRADES_HISTORY_CURSOR_AND_OFFSET_MUTUALLY_EXCLUSIVE")
        params: dict[str, Any] = {"trades": True, "ledgers": True}
        if with_cursor:
            params["with_cursor"] = True
            if cursor:
                params["cursor"] = cursor
        else:
            params["ofs"] = offset
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        return self.private("TradesHistory", params)

    def ledgers(self, *, offset: int = 0, start: int | None = None,
                end: int | None = None, ledger_type: str = "all",
                asset: str = "all") -> dict[str, Any]:
        if offset < 0:
            raise ValueError("LEDGERS_OFFSET_MUST_BE_NON_NEGATIVE")
        if ledger_type not in {"all", "deposit", "withdrawal", "trade", "margin",
                               "rollover", "credit", "transfer", "settled", "staking", "sale"}:
            raise ValueError("LEDGER_TYPE_INVALID")
        params: dict[str, Any] = {"aclass": "currency", "type": ledger_type, "asset": asset, "ofs": offset}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        return self.private("Ledgers", params)

    def trade_volume(self, pairs: list[str]) -> dict[str, Any]:
        if not pairs:
            return {}
        return self.private("TradeVolume", {"pair": ",".join(pairs)})
