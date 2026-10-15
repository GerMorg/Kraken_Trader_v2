from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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

class KrakenClient:
    def __init__(self, api_key: str | None = None, api_secret: str | None = None,
                 nonce_provider: Callable[[], int] | None = None, timeout_seconds: float = 15.0) -> None:
        self.api_key, self.api_secret = api_key or "", api_secret or ""
        self.nonce_provider = nonce_provider or (lambda: time.time_ns() // 1000)
        self.timeout_seconds = timeout_seconds

    def public(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        encoded = urlencode(params or {}, doseq=True)
        url = f"{SPOT_BASE}/0/public/{method}" + (("?" + encoded) if encoded else "")
        request = Request(url, headers={"User-Agent":"KrakenTraderV2/0.1.0"})
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
        request = Request(
            SPOT_BASE + path, data=encoded, method="POST",
            headers={"API-Key": self.api_key, "API-Sign": sign_spot(path, payload, self.api_secret),
                     "Content-Type":"application/x-www-form-urlencoded", "User-Agent":"KrakenTraderV2/0.1.0"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310 -- fixed Kraken HTTPS origin; endpoint names are static
                value = _decode_response(response.read())
        except HTTPError as exc:
            try:
                value = _decode_response(exc.read())
            except KrakenAPIError:
                if method == "AddOrder":
                    raise KrakenAmbiguousError("ADD_ORDER_HTTP_OUTCOME_UNKNOWN") from exc
                raise KrakenAPIError("KRAKEN_PRIVATE_HTTP_ERROR") from exc
        except (TimeoutError, URLError, OSError) as exc:
            if method == "AddOrder":
                raise KrakenAmbiguousError("ADD_ORDER_NETWORK_OUTCOME_UNKNOWN") from exc
            raise KrakenAPIError("KRAKEN_PRIVATE_NETWORK_ERROR") from exc
        if value.get("error"):
            raise KrakenAPIError("KRAKEN_PRIVATE:" + ";".join(str(x) for x in value["error"]))
        result = value.get("result", {})
        if not isinstance(result, dict):
            raise KrakenAPIError("KRAKEN_PRIVATE_RESULT_INVALID")
        return result

    def account_snapshot(self) -> dict[str, Any]:
        balance = self.private("Balance")
        trade = self.private("TradeBalance", {"aclass":"currency","asset":"ZUSD"})
        orders = self.private("OpenOrders", {"trades":True})
        positions = self.private("OpenPositions", {"docalcs":True,"consolidation":"market"})
        return {"balance":balance, "trade_balance":trade, "open_orders":orders,
                "open_positions":positions, "captured_at":time.time(), "verified":True}

    def trade_volume(self, pairs: list[str]) -> dict[str, Any]:
        if not pairs:
            return {}
        return self.private("TradeVolume", {"pair": ",".join(pairs)})
