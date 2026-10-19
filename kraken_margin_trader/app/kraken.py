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

def sign_spot(uri_path: str, params: dict[str, Any] | str, api_secret: str) -> str:
    """Sign a Kraken form payload or its exact JSON body representation."""
    if isinstance(params, str):
        try:
            nonce = str(json.loads(params)["nonce"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("KRAKEN_JSON_SIGNING_PAYLOAD_MISSING_NONCE") from exc
        post_data = params
    else:
        nonce = str(params["nonce"])
        post_data = urlencode(params, doseq=True)
    digest = hashlib.sha256((nonce + post_data).encode("utf-8")).digest()
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
    if assessment.get("api_key_expired") is True:
        reasons.append("API_KEY_EXPIRED")
    if assessment.get("historical_query_window_restricted") is True:
        reasons.append("API_KEY_HISTORY_QUERY_WINDOW_RESTRICTED")
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
    if (
        assessment.get("ready_for_account_reconciliation") is not True
        and assessment.get("api_key_metadata_complete") is True
        and not reasons
    ):
        # A complete but internally inconsistent assessment is a hard failure.
        # Missing optional metadata is verified against the actual history APIs.
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
    metadata_aliases = {
        "validUntil": ("validUntil", "valid_until"),
        "queryFrom": ("queryFrom", "query_from"),
        "queryTo": ("queryTo", "query_to"),
    }
    metadata_values: dict[str, int | None] = {}
    metadata_source_fields: dict[str, str] = {}
    metadata_fields_missing: list[str] = []
    metadata_fields_present: list[str] = []
    metadata_fields_invalid: list[str] = []
    for canonical, aliases in metadata_aliases.items():
        present = [field for field in aliases if field in response]
        if not present:
            metadata_fields_missing.append(canonical)
            metadata_values[canonical] = None
            continue
        metadata_fields_present.append(canonical)
        metadata_source_fields[canonical] = present[0]
        parsed = [(field, _nonnegative_epoch(response.get(field))) for field in present]
        parsed_values = {value for _field, value in parsed}
        if len(parsed_values) > 1 or parsed[0][1] is None:
            metadata_fields_invalid.append(canonical)
            metadata_values[canonical] = None
        else:
            metadata_values[canonical] = parsed[0][1]

    valid_until = metadata_values["validUntil"]
    query_from = metadata_values["queryFrom"]
    query_to = metadata_values["queryTo"]
    now = int(time.time()) if now_epoch is None else int(now_epoch)

    # Only field names are reported; response values, including apiKey, are never copied.
    response_fields_present = sorted(
        field for field in response
        if isinstance(field, str)
        and field.lower() not in {"apikey", "api_key", "secret", "apisecret", "privatekey"}
    )
    metadata_fields_missing.sort()
    metadata_fields_invalid.sort()
    metadata_complete = not metadata_fields_missing and not metadata_fields_invalid
    key_expiry_unknown = valid_until is None
    key_expired = valid_until is not None and valid_until != 0 and valid_until <= now
    history_window_unknown = query_from is None or query_to is None
    # Absence of optional metadata does not prove that the key is expired or that
    # the query window is restricted. Explicit restrictions remain hard blockers;
    # missing values require probing the actual history endpoints.
    history_window_restricted = (
        (query_from is not None and query_from > 0)
        or (query_to is not None and query_to > 0 and query_to <= now)
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
        "api_key_metadata_fields_missing": metadata_fields_missing,
        "api_key_metadata_fields_present": sorted(metadata_fields_present),
        "api_key_metadata_fields_invalid": sorted(metadata_fields_invalid),
        "api_key_metadata_source_fields": metadata_source_fields,
        "api_key_info_response_fields_present": response_fields_present,
        "api_key_expiry_unknown": key_expiry_unknown,
        "api_key_expired": key_expired,
        "api_key_expired_or_expiry_unknown": key_expired,
        "historical_query_window_unknown": history_window_unknown,
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
        # Kraken requires nonce order to match request arrival order.
        self._private_request_lock = threading.RLock()
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

    def private(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not self.api_key or not self.api_secret:
            raise KrakenAPIError("KRAKEN_CREDENTIALS_MISSING")
        with self._private_request_lock:
            path = f"/0/private/{method}"
            nonce = self.nonce_provider()
            try:
                # Current Kraken Spot REST documentation specifies JSON request bodies.
                # Keep native JSON values and sign the exact compact body sent on wire.
                payload: dict[str, Any] = {"nonce": int(nonce)}
                payload.update(dict(params or {}))
                payload["nonce"] = int(nonce)
                json_body = json.dumps(
                    payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False
                )
                encoded = json_body.encode("utf-8")
                signature = sign_spot(path, json_body, self.api_secret)
            except (ValueError, TypeError, OverflowError) as exc:
                raise KrakenAPIError("KRAKEN_PRIVATE_SIGNING_CONFIGURATION_INVALID") from exc
            request = Request(
                SPOT_BASE + path,
                data=encoded,
                method="POST",
                headers={
                    "API-Key": self.api_key,
                    "API-Sign": signature,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": f"KrakenTraderV2/{__version__}",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310 -- fixed Kraken HTTPS origin; endpoints are static
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
            if method == "ExportStatus" and isinstance(result, list):
                if any(not isinstance(item, dict) for item in result):
                    raise KrakenAPIError("KRAKEN_EXPORT_STATUS_RESULT_INVALID")
                return result
            if not isinstance(result, dict):
                if method == "AddOrder":
                    raise KrakenAmbiguousError("ADD_ORDER_RESULT_SHAPE_OUTCOME_UNKNOWN")
                raise KrakenAPIError("KRAKEN_PRIVATE_RESULT_INVALID")
            return result

    def private_binary(
        self, method: str, params: dict[str, Any] | None = None, *, max_bytes: int = 20_000_000
    ) -> bytes:
        """Call a fixed private endpoint that returns a binary report rather than JSON."""
        if not self.api_key or not self.api_secret:
            raise KrakenAPIError("KRAKEN_CREDENTIALS_MISSING")
        if max_bytes < 1:
            raise ValueError("KRAKEN_BINARY_RESPONSE_LIMIT_INVALID")
        with self._private_request_lock:
            path = f"/0/private/{method}"
            try:
                payload: dict[str, Any] = {"nonce": int(self.nonce_provider())}
                payload.update(dict(params or {}))
                payload["nonce"] = int(payload["nonce"])
                json_body = json.dumps(
                    payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False
                )
                signature = sign_spot(path, json_body, self.api_secret)
            except (ValueError, TypeError, OverflowError) as exc:
                raise KrakenAPIError("KRAKEN_PRIVATE_SIGNING_CONFIGURATION_INVALID") from exc
            request = Request(
                SPOT_BASE + path,
                data=json_body.encode("utf-8"),
                method="POST",
                headers={
                    "API-Key": self.api_key,
                    "API-Sign": signature,
                    "Content-Type": "application/json",
                    "Accept": "application/octet-stream",
                    "User-Agent": f"KrakenTraderV2/{__version__}",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    raw = response.read(max_bytes + 1)
            except HTTPError as exc:
                try:
                    value = _decode_response(exc.read(65_536))
                except KrakenAPIError:
                    value = {}
                errors = value.get("error") if isinstance(value, dict) else None
                if isinstance(errors, list) and errors:
                    raise KrakenAPIError(
                        "KRAKEN_PRIVATE:" + ";".join(str(item) for item in errors)
                    ) from exc
                raise KrakenAPIError("KRAKEN_PRIVATE_HTTP_ERROR") from exc
            except (TimeoutError, URLError, OSError) as exc:
                raise KrakenAPIError("KRAKEN_PRIVATE_NETWORK_ERROR") from exc
            if len(raw) > max_bytes:
                raise KrakenAPIError("KRAKEN_EXPORT_ARCHIVE_TOO_LARGE")
            if raw.lstrip().startswith(b"{"):
                try:
                    value = _decode_response(raw)
                except KrakenAPIError as exc:
                    raise KrakenAPIError("KRAKEN_EXPORT_BINARY_RESPONSE_INVALID") from exc
                errors = value.get("error")
                if isinstance(errors, list) and errors:
                    raise KrakenAPIError(
                        "KRAKEN_PRIVATE:" + ";".join(str(item) for item in errors)
                    )
                raise KrakenAPIError("KRAKEN_EXPORT_BINARY_RESPONSE_INVALID")
            return raw

    def request_export_report(
        self, *, report: str, description: str, starttm: int, endtm: int
    ) -> dict[str, Any]:
        """Request a bounded Kraken account-history export; no order is submitted."""
        if report not in {"trades", "ledgers"}:
            raise ValueError("KRAKEN_EXPORT_REPORT_TYPE_INVALID")
        if not description.strip() or len(description) > 128:
            raise ValueError("KRAKEN_EXPORT_DESCRIPTION_INVALID")
        if starttm < 0 or endtm < starttm:
            raise ValueError("KRAKEN_EXPORT_TIME_RANGE_INVALID")
        return self.private("AddExport", {
            "report": report,
            "description": description,
            "format": "CSV",
            "fields": "all",
            "starttm": int(starttm),
            "endtm": int(endtm),
        })

    def export_report_status(self, report: str) -> Any:
        if report not in {"trades", "ledgers"}:
            raise ValueError("KRAKEN_EXPORT_REPORT_TYPE_INVALID")
        return self.private("ExportStatus", {"report": report})

    def retrieve_export_report(self, export_id: str) -> bytes:
        normalized = export_id.strip()
        if not normalized or len(normalized) > 128:
            raise ValueError("KRAKEN_EXPORT_ID_INVALID")
        return self.private_binary("RetrieveExport", {"id": normalized})

    def remove_export_report(self, export_id: str, *, action: str = "delete") -> dict[str, Any]:
        normalized = export_id.strip()
        if not normalized or len(normalized) > 128:
            raise ValueError("KRAKEN_EXPORT_ID_INVALID")
        if action not in {"delete", "cancel"}:
            raise ValueError("KRAKEN_EXPORT_ACTION_INVALID")
        return self.private("RemoveExport", {"id": normalized, "type": action})

    def api_key_permission_assessment(self, *, now_epoch: int | None = None) -> dict[str, Any]:
        """Check required read scopes and reject overly privileged/limited API keys."""
        response = self.private("GetApiKeyInfo")
        return assess_api_key_info(response, now_epoch=now_epoch)

    def account_snapshot(self, *, include_closed_orders: bool = True) -> dict[str, Any]:
        """Fetch current account state, optionally omitting history-restricted closed orders."""
        balance = self.private("Balance")
        trade = self.private("TradeBalance", {"aclass":"currency","asset":"ZUSD"})
        orders = self.private("OpenOrders", {"trades":True})
        # ClosedOrders is historical data and may be limited by GetApiKeyInfo queryFrom/queryTo.
        # Current positions and open orders remain available for risk-reducing monitoring.
        closed = self.closed_orders(include_trades=True) if include_closed_orders else None
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

    def ledgers(
        self, *, offset: int = 0, start: int | None = None, end: int | None = None,
        ledger_type: str = "all", asset: str = "all",
        with_cursor: bool = False, cursor: str | None = None,
    ) -> dict[str, Any]:
        if offset < 0:
            raise ValueError("LEDGERS_OFFSET_MUST_BE_NON_NEGATIVE")
        if with_cursor and offset != 0:
            raise ValueError("LEDGERS_CURSOR_AND_OFFSET_MUTUALLY_EXCLUSIVE")
        if ledger_type not in {
            "all", "deposit", "withdrawal", "trade", "margin", "rollover",
            "credit", "transfer", "settled", "staking", "sale", "adjustment", "dividend", "nft_rebate",
        }:
            raise ValueError("LEDGER_TYPE_INVALID")
        # Current Kraken docs deprecate aclass and ofs; prefer asset
        # filtering and cursor pagination for regular history synchronization.
        params: dict[str, Any] = {"type": ledger_type, "asset": asset}
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
        return self.private("Ledgers", params)

    def query_trades_info(
        self, trade_ids: list[str], *, include_ledgers: bool = True
    ) -> dict[str, Any]:
        """Fetch exact trades to recover optional ledger references omitted by TradesHistory."""
        normalized = list(dict.fromkeys(
            str(trade_id).strip() for trade_id in trade_ids if str(trade_id).strip()
        ))
        if not normalized:
            raise ValueError("QUERY_TRADES_IDS_REQUIRED")
        if len(normalized) > 20:
            raise ValueError("QUERY_TRADES_IDS_LIMIT_EXCEEDED")
        params: dict[str, Any] = {
            "txid": ",".join(normalized),
            "trades": False,
            "ledgers": bool(include_ledgers),
        }
        return self.private("QueryTrades", params)

    def query_ledgers_info(self, ledger_ids: list[str]) -> dict[str, Any]:
        """Fetch exact ledger rows referenced by trade fills (maximum 20 IDs per call)."""
        normalized = list(dict.fromkeys(
            str(ledger_id).strip() for ledger_id in ledger_ids if str(ledger_id).strip()
        ))
        if not normalized:
            raise ValueError("QUERY_LEDGERS_IDS_REQUIRED")
        if len(normalized) > 20:
            raise ValueError("QUERY_LEDGERS_IDS_LIMIT_EXCEEDED")
        return self.private("QueryLedgers", {"id": ",".join(normalized)})

    def trade_volume(self, pairs: list[str]) -> dict[str, Any]:
        if not pairs:
            return {}
        return self.private("TradeVolume", {"pair": ",".join(pairs)})
