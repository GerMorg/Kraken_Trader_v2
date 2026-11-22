from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
from collections.abc import Callable
from typing import Any, Final
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEMO_BASE_URL: Final = "https://demo-futures.kraken.com"
PRODUCTION_BASE_URL: Final = "https://futures.kraken.com"
API_PREFIX: Final = "/derivatives/api/v3"
MAX_RESPONSE_BYTES: Final = 2_000_000
DEFAULT_TIMEOUT_SECONDS: Final = 10.0

Transport = Callable[[Request, float], tuple[int, bytes]]

_READ_ENDPOINTS: Final[dict[str, tuple[str, bool, str, str]]] = {
    "instruments": ("/derivatives/api/v3/instruments", False, "instruments", "list"),
    "tickers": ("/derivatives/api/v3/tickers", False, "tickers", "list"),
    "accounts": ("/derivatives/api/v3/accounts", True, "accounts", "dict"),
    "open_positions": ("/derivatives/api/v3/openpositions", True, "openPositions", "list"),
    "open_orders": ("/derivatives/api/v3/openorders", True, "openOrders", "list"),
    "fills": ("/derivatives/api/v3/fills", True, "fills", "list"),
    "leverage_preferences": (
        "/derivatives/api/v3/leveragepreferences",
        True,
        "leveragePreferences",
        "list",
    ),
    "historical_funding_rates": (
        "/derivatives/api/v3/historical-funding-rates",
        False,
        "rates",
        "list",
    ),
}

_EXCHANGE_ERROR_CODES: Final[dict[str, str]] = {
    "accountInactive": "ACCOUNT_INACTIVE",
    "apiLimitExceeded": "API_LIMIT_EXCEEDED",
    "authenticationError": "AUTHENTICATION_ERROR",
    "insufficientFunds": "INSUFFICIENT_FUNDS",
    "invalidAccount": "INVALID_ACCOUNT",
    "invalidAmount": "INVALID_AMOUNT",
    "invalidArgument": "INVALID_ARGUMENT",
    "invalidUnit": "INVALID_UNIT",
    "marketUnavailable": "MARKET_UNAVAILABLE",
    "nonceBelowThreshold": "NONCE_BELOW_THRESHOLD",
    "nonceDuplicate": "NONCE_DUPLICATE",
    "notFound": "NOT_FOUND",
    "requiredArgumentMissing": "REQUIRED_ARGUMENT_MISSING",
    "unknownError": "UNKNOWN_ERROR",
}


class FuturesAPIError(RuntimeError):
    """Sanitized Futures API error; never contains request credentials or response bodies."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def build_futures_authent(
    api_secret: str,
    endpoint_path: str,
    post_data: str = "",
    nonce: str = "",
) -> str:
    """Create Kraken Futures REST v3 Authent using the documented REST-v3 scheme.

    post_data must be the exact URL-encoded query/body string sent with the request.
    The /derivatives prefix is omitted for derivatives API paths; the API-key
    metadata endpoint is signed with its complete /api/auth/v1 path.
    """
    derivative_path = endpoint_path.startswith(API_PREFIX + "/")
    key_check_path = endpoint_path.startswith("/api/auth/v1/api-keys/v3/")
    if not api_secret or not (derivative_path or key_check_path):
        raise ValueError("FUTURES_AUTH_INPUT_INVALID")
    if "?" in endpoint_path or "#" in endpoint_path or post_data.startswith("?"):
        raise ValueError("FUTURES_AUTH_INPUT_INVALID")
    try:
        secret = base64.b64decode(api_secret.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise ValueError("FUTURES_API_SECRET_INVALID") from None
    if not secret:
        raise ValueError("FUTURES_API_SECRET_INVALID")

    signing_path = endpoint_path[len("/derivatives"):] if derivative_path else endpoint_path
    message = post_data + nonce + signing_path
    digest = hashlib.sha256(message.encode("utf-8")).digest()
    signature = hmac.new(secret, digest, hashlib.sha512).digest()
    return base64.b64encode(signature).decode("ascii")


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        fp: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


def _default_transport(request: Request, timeout_seconds: float) -> tuple[int, bytes]:
    opener = build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            return int(response.status), response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        try:
            return int(exc.code), exc.read(MAX_RESPONSE_BYTES + 1)
        finally:
            exc.close()


class FuturesAPIClient:
    """Read-only Kraken Futures REST client.

    The default host is production and all ordinary calls are GET-only. The legacy
    demo host remains isolated for retired adapter tests; runtime code must not
    depend on that environment.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        environment: str = "production_read_only",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Transport | None = None,
    ) -> None:
        normalized_environment = environment.strip().lower() if isinstance(environment, str) else ""
        if normalized_environment not in {"demo", "production_read_only"}:
            raise ValueError("FUTURES_PRODUCTION_ENVIRONMENT_NOT_ENABLED")
        if not math.isfinite(timeout_seconds) or not 1.0 <= timeout_seconds <= 30.0:
            raise ValueError("FUTURES_TIMEOUT_OUT_OF_RANGE")
        self._api_key = api_key.strip()
        self._api_secret = api_secret.strip()
        self._timeout_seconds = timeout_seconds
        self._transport = transport or _default_transport
        self.environment = normalized_environment
        self._base_url = DEMO_BASE_URL if normalized_environment == "demo" else PRODUCTION_BASE_URL

    @property
    def credentials_present(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def get_api_key_info(self) -> dict[str, Any]:
        """Verify this key and read its exchange-assigned access tiers.

        The endpoint exists on the production API host. The returned API key,
        account UID, IIBAN and CIDR values are deliberately not included in logs.
        """
        if self.environment != "production_read_only":
            raise FuturesAPIError("FUTURES_API_KEY_CHECK_PRODUCTION_ONLY")
        payload = self._request_get(
            "/api/auth/v1/api-keys/v3/check",
            query_string="",
            authenticated=True,
        )
        permissions = payload.get("permissions")
        if (
            not isinstance(payload.get("apiKey"), str)
            or not isinstance(payload.get("accountUid"), str)
            or not isinstance(permissions, dict)
            or not isinstance(permissions.get("general"), str)
            or not isinstance(permissions.get("transfer"), str)
        ):
            raise FuturesAPIError("FUTURES_RESPONSE_SCHEMA_INVALID")
        return payload

    def get_instruments(self) -> dict[str, Any]:
        return self._read("instruments")

    def get_tickers(self) -> dict[str, Any]:
        return self._read("tickers")

    def get_accounts(self) -> dict[str, Any]:
        """Read the Futures account/wallet snapshot from GET /accounts."""
        return self._read("accounts")

    def get_open_positions(self) -> dict[str, Any]:
        return self._read("open_positions")

    def get_open_orders(self) -> dict[str, Any]:
        return self._read("open_orders")

    def get_fills(self) -> dict[str, Any]:
        return self._read("fills")

    def get_leverage_preferences(self) -> dict[str, Any]:
        """Read configured leverage preferences; this client never changes them."""
        return self._read("leverage_preferences")

    def get_historical_funding_rates(self, symbol: str) -> dict[str, Any]:
        return self._read("historical_funding_rates", symbol=symbol)

    def get_ohlc_candles(
        self,
        symbol: str,
        *,
        tick_type: str = "trade",
        resolution: str = "1h",
        count: int = 90,
    ) -> dict[str, Any]:
        """Read public Futures chart candles through the separate Charts API."""
        normalized_symbol = str(symbol).strip().upper()
        normalized_tick_type = str(tick_type).strip().lower()
        normalized_resolution = str(resolution).strip().lower()
        if not re.fullmatch(r"PF_[A-Z0-9]{2,16}USD", normalized_symbol):
            raise ValueError("FUTURES_CHART_SYMBOL_INVALID")
        if normalized_tick_type not in {"trade", "mark", "spot"}:
            raise ValueError("FUTURES_CHART_TICK_TYPE_INVALID")
        if normalized_resolution not in {"1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w"}:
            raise ValueError("FUTURES_CHART_RESOLUTION_INVALID")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 500:
            raise ValueError("FUTURES_CHART_COUNT_OUT_OF_RANGE")
        path = (
            "/api/charts/v1/"
            + normalized_tick_type + "/"
            + normalized_symbol + "/"
            + normalized_resolution
        )
        payload = self._request_get(
            path,
            query_string=urlencode({"count": count}),
            authenticated=False,
        )
        candles = payload.get("candles")
        if not isinstance(candles, list) or not isinstance(payload.get("more_candles"), bool):
            raise FuturesAPIError("FUTURES_CHART_RESPONSE_SCHEMA_INVALID")
        for candle in candles:
            if not isinstance(candle, dict):
                raise FuturesAPIError("FUTURES_CHART_RESPONSE_SCHEMA_INVALID")
            try:
                timestamp = int(candle["time"])
                close = float(candle["close"])
                high = float(candle["high"])
                low = float(candle["low"])
                open_price = float(candle["open"])
            except (KeyError, TypeError, ValueError, OverflowError):
                raise FuturesAPIError("FUTURES_CHART_RESPONSE_SCHEMA_INVALID") from None
            if (
                timestamp < 0
                or not all(math.isfinite(value) and value > 0 for value in (
                    close, high, low, open_price
                ))
                or high < max(open_price, close, low)
                or low > min(open_price, close, high)
            ):
                raise FuturesAPIError("FUTURES_CHART_RESPONSE_SCHEMA_INVALID")
        return payload

    def _read(self, name: str, *, symbol: str | None = None) -> dict[str, Any]:
        endpoint = _READ_ENDPOINTS.get(name)
        if endpoint is None:
            raise FuturesAPIError("FUTURES_READ_ENDPOINT_NOT_ALLOWED")
        path, authenticated, payload_key, payload_kind = endpoint

        query_string = ""
        if symbol is not None:
            normalized_symbol = symbol.strip().upper()
            if not re.fullmatch(r"[A-Z0-9_]{1,48}", normalized_symbol):
                raise ValueError("FUTURES_SYMBOL_INVALID")
            query_string = urlencode({"symbol": normalized_symbol})
        elif name == "historical_funding_rates":
            raise ValueError("FUTURES_SYMBOL_REQUIRED")

        payload = self._request_get(
            path,
            query_string=query_string,
            authenticated=authenticated,
        )
        result = payload.get(payload_key)
        if payload_kind == "list" and not isinstance(result, list):
            raise FuturesAPIError("FUTURES_RESPONSE_SCHEMA_INVALID")
        if payload_kind == "dict" and not isinstance(result, dict):
            raise FuturesAPIError("FUTURES_RESPONSE_SCHEMA_INVALID")
        return payload

    def _request_post(self, endpoint_path: str, *, post_data: str) -> dict[str, Any]:
        """Signed mutation primitive restricted to the demo host and fixed order routes."""
        allowed = {
            "/derivatives/api/v3/sendorder",
            "/derivatives/api/v3/editorder",
            "/derivatives/api/v3/cancelorder",
        }
        if self.environment != "demo" or endpoint_path not in allowed:
            raise FuturesAPIError("FUTURES_MUTATION_DEMO_ONLY")
        if not self.credentials_present:
            raise FuturesAPIError("FUTURES_API_CREDENTIALS_MISSING")
        try:
            authent = build_futures_authent(
                self._api_secret, endpoint_path, post_data=post_data
            )
        except ValueError:
            raise FuturesAPIError("FUTURES_AUTH_CONFIGURATION_INVALID") from None
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "APIKey": self._api_key,
            "Authent": authent,
            "User-Agent": "KrakenTraderV2-FuturesDemo/0.1.77",
        }
        request = Request(
            self._base_url + endpoint_path,
            data=post_data.encode("ascii"),
            headers=headers,
            method="POST",
        )
        try:
            status_code, raw = self._transport(request, self._timeout_seconds)
        except (TimeoutError, URLError, OSError):
            raise FuturesAPIError("FUTURES_NETWORK_ERROR") from None
        except Exception:
            raise FuturesAPIError("FUTURES_TRANSPORT_ERROR") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FuturesAPIError("FUTURES_RESPONSE_TOO_LARGE")
        if status_code < 200 or status_code >= 300:
            raise FuturesAPIError(f"FUTURES_HTTP_STATUS_{status_code}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise FuturesAPIError("FUTURES_RESPONSE_INVALID_JSON") from None
        if not isinstance(payload, dict):
            raise FuturesAPIError("FUTURES_RESPONSE_SCHEMA_INVALID")
        result = payload.get("result")
        errors = payload.get("errors")
        if result is not None and result != "success":
            raise FuturesAPIError(self._api_rejection_code(payload))
        if isinstance(errors, list) and errors:
            raise FuturesAPIError(self._api_rejection_code(payload))
        return payload

    def _request_get(
        self,
        endpoint_path: str,
        *,
        query_string: str,
        authenticated: bool,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "KrakenTraderV2-FuturesReadOnly/0.1.77",
        }
        if authenticated:
            if not self.credentials_present:
                raise FuturesAPIError("FUTURES_API_CREDENTIALS_MISSING")
            try:
                authent = build_futures_authent(
                    self._api_secret,
                    endpoint_path,
                    post_data=query_string,
                )
            except ValueError:
                raise FuturesAPIError("FUTURES_AUTH_CONFIGURATION_INVALID") from None
            headers["APIKey"] = self._api_key
            headers["Authent"] = authent

        url = self._base_url + endpoint_path
        if query_string:
            url += "?" + query_string
        request = Request(url, headers=headers, method="GET")
        try:
            status_code, raw = self._transport(request, self._timeout_seconds)
        except (TimeoutError, URLError, OSError):
            raise FuturesAPIError("FUTURES_NETWORK_ERROR") from None
        except Exception:
            # Injected transports and platform URL handlers are not trusted to expose
            # raw exception text; it may contain request metadata.
            raise FuturesAPIError("FUTURES_TRANSPORT_ERROR") from None

        if len(raw) > MAX_RESPONSE_BYTES:
            raise FuturesAPIError("FUTURES_RESPONSE_TOO_LARGE")
        if status_code < 200 or status_code >= 300:
            raise FuturesAPIError(f"FUTURES_HTTP_STATUS_{status_code}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise FuturesAPIError("FUTURES_RESPONSE_INVALID_JSON") from None
        if not isinstance(payload, dict):
            raise FuturesAPIError("FUTURES_RESPONSE_SCHEMA_INVALID")

        result = payload.get("result")
        errors = payload.get("errors")
        if result is not None and result != "success":
            raise FuturesAPIError(self._api_rejection_code(payload))
        if isinstance(errors, list) and errors:
            raise FuturesAPIError(self._api_rejection_code(payload))
        return payload

    @staticmethod
    def _api_rejection_code(payload: dict[str, Any]) -> str:
        candidates: list[Any] = []
        if "error" in payload:
            candidates.append(payload["error"])
        errors = payload.get("errors")
        if isinstance(errors, list):
            candidates.extend(errors)
        for candidate in candidates:
            if isinstance(candidate, str) and candidate in _EXCHANGE_ERROR_CODES:
                return "FUTURES_API_" + _EXCHANGE_ERROR_CODES[candidate]
        return "FUTURES_API_REJECTED"
