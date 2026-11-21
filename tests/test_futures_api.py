from __future__ import annotations

import base64
import json
from urllib.error import URLError
from urllib.request import Request

import pytest

from app.futures_api import (
    DEMO_BASE_URL,
    FuturesAPIClient,
    FuturesAPIError,
    build_futures_authent,
)

TEST_SECRET = base64.b64encode(b"test-futures-secret").decode("ascii")
TEST_KEY = "demo-api-key"


class FakeTransport:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.requests: list[Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: Request, timeout_seconds: float) -> tuple[int, bytes]:
        self.requests.append(request)
        self.timeouts.append(timeout_seconds)
        return self.status_code, json.dumps(self.payload).encode("utf-8")


def test_futures_authent_matches_reference_signing_vector() -> None:
    assert build_futures_authent(
        TEST_SECRET,
        "/derivatives/api/v3/accounts",
    ) == "ORStlunQAEMHCpWOfBZGodLOkHHE2WMu+1eX4O7J+UfOdsOwCdCJggLpA59tTRZTfQyL/IEUDy995P4MMuM6ew=="


def test_futures_authent_signs_exact_query_string_and_optional_nonce() -> None:
    no_nonce = build_futures_authent(
        TEST_SECRET,
        "/derivatives/api/v3/historical-funding-rates",
        post_data="symbol=PF_XBTUSD",
    )
    with_nonce = build_futures_authent(
        TEST_SECRET,
        "/derivatives/api/v3/historical-funding-rates",
        post_data="symbol=PF_XBTUSD",
        nonce="123456789",
    )
    assert no_nonce != with_nonce


@pytest.mark.parametrize(
    ("secret", "endpoint", "query"),
    [
        ("", "/derivatives/api/v3/accounts", ""),
        ("not-base64", "/derivatives/api/v3/accounts", ""),
        (TEST_SECRET, "/api/v3/accounts", ""),
        (TEST_SECRET, "/derivatives/api/v3/accounts?x=1", ""),
        (TEST_SECRET, "/derivatives/api/v3/accounts", "?symbol=PF_XBTUSD"),
    ],
)
def test_futures_authent_rejects_malformed_inputs(
    secret: str, endpoint: str, query: str
) -> None:
    with pytest.raises(ValueError):
        build_futures_authent(secret, endpoint, query)


def test_public_instrument_request_is_pinned_to_demo_and_needs_no_key() -> None:
    transport = FakeTransport({
        "result": "success",
        "instruments": [{"symbol": "PF_XBTUSD", "type": "flexible_futures"}],
    })
    client = FuturesAPIClient(transport=transport)

    result = client.get_instruments()

    assert result["instruments"][0]["symbol"] == "PF_XBTUSD"
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.full_url == DEMO_BASE_URL + "/derivatives/api/v3/instruments"
    assert request.get_method() == "GET"
    assert request.get_header("Apikey") is None
    assert request.get_header("Authent") is None
    assert transport.timeouts == [10.0]


def test_private_accounts_request_uses_exact_futures_signature() -> None:
    transport = FakeTransport({
        "accounts": {"multiCollateral": {"balances": {"eur": "100.00"}}},
    })
    client = FuturesAPIClient(
        api_key=TEST_KEY,
        api_secret=TEST_SECRET,
        transport=transport,
    )

    result = client.get_accounts()

    assert result["accounts"]["multiCollateral"]["balances"]["eur"] == "100.00"
    request = transport.requests[0]
    assert request.full_url == DEMO_BASE_URL + "/derivatives/api/v3/accounts"
    assert request.get_header("Apikey") == TEST_KEY
    assert request.get_header("Authent") == build_futures_authent(
        TEST_SECRET, "/derivatives/api/v3/accounts"
    )
    assert request.get_header("Nonce") is None


def test_funding_rates_query_is_url_encoded_and_signed_identically() -> None:
    transport = FakeTransport({"result": "success", "rates": []})
    client = FuturesAPIClient(
        api_key=TEST_KEY,
        api_secret=TEST_SECRET,
        transport=transport,
    )

    client.get_historical_funding_rates("pf_xbtusd")

    request = transport.requests[0]
    query = "symbol=PF_XBTUSD"
    assert request.full_url.endswith("/historical-funding-rates?" + query)
    assert request.get_header("Authent") == build_futures_authent(
        TEST_SECRET,
        "/derivatives/api/v3/historical-funding-rates",
        post_data=query,
    )


def test_private_request_fails_closed_without_credentials() -> None:
    transport = FakeTransport({"accounts": {}})
    client = FuturesAPIClient(transport=transport)

    with pytest.raises(FuturesAPIError, match="FUTURES_API_CREDENTIALS_MISSING"):
        client.get_accounts()

    assert transport.requests == []


def test_only_demo_environment_is_available_in_this_stage() -> None:
    with pytest.raises(ValueError, match="FUTURES_PRODUCTION_ENVIRONMENT_NOT_ENABLED"):
        FuturesAPIClient(environment="live")


@pytest.mark.parametrize("timeout", [0.0, -1.0, 31.0, float("nan"), float("inf")])
def test_timeout_must_be_finite_and_bounded(timeout: float) -> None:
    with pytest.raises(ValueError, match="FUTURES_TIMEOUT_OUT_OF_RANGE"):
        FuturesAPIClient(timeout_seconds=timeout)


def test_api_rejection_is_sanitized_to_known_error_code() -> None:
    transport = FakeTransport({
        "result": "error",
        "error": "accountInactive",
        "errors": ["accountInactive"],
    })
    client = FuturesAPIClient(
        api_key=TEST_KEY,
        api_secret=TEST_SECRET,
        transport=transport,
    )

    with pytest.raises(FuturesAPIError, match="FUTURES_API_ACCOUNT_INACTIVE"):
        client.get_accounts()


def test_http_error_is_not_retried_or_exposed_as_response_body() -> None:
    transport = FakeTransport({"error": "sensitive-account-detail"}, status_code=429)
    client = FuturesAPIClient(transport=transport)

    with pytest.raises(FuturesAPIError, match="FUTURES_HTTP_STATUS_429") as error:
        client.get_instruments()

    assert str(error.value) == "FUTURES_HTTP_STATUS_429"
    assert len(transport.requests) == 1


def test_invalid_json_and_wrong_response_shapes_fail_closed() -> None:
    def malformed_transport(request: Request, timeout: float) -> tuple[int, bytes]:
        return 200, b"{not-json"

    client = FuturesAPIClient(transport=malformed_transport)
    with pytest.raises(FuturesAPIError, match="FUTURES_RESPONSE_INVALID_JSON"):
        client.get_instruments()

    client = FuturesAPIClient(
        transport=FakeTransport({"result": "success", "instruments": {}})
    )
    with pytest.raises(FuturesAPIError, match="FUTURES_RESPONSE_SCHEMA_INVALID"):
        client.get_instruments()


def test_response_size_is_bounded() -> None:
    def large_transport(request: Request, timeout: float) -> tuple[int, bytes]:
        return 200, b"x" * 2_000_001

    client = FuturesAPIClient(transport=large_transport)
    with pytest.raises(FuturesAPIError, match="FUTURES_RESPONSE_TOO_LARGE"):
        client.get_instruments()


def test_network_errors_are_wrapped_without_raw_exception_text() -> None:
    def failing_transport(request: Request, timeout: float) -> tuple[int, bytes]:
        raise URLError("api-key=should-never-appear")

    client = FuturesAPIClient(transport=failing_transport)
    with pytest.raises(FuturesAPIError, match="FUTURES_NETWORK_ERROR") as error:
        client.get_instruments()
    assert "should-never-appear" not in str(error.value)


def test_symbol_is_validated_before_request() -> None:
    transport = FakeTransport({"result": "success", "rates": []})
    client = FuturesAPIClient(transport=transport)

    with pytest.raises(ValueError, match="FUTURES_SYMBOL_INVALID"):
        client.get_historical_funding_rates("PF_XBTUSD&sendorder=true")
    assert transport.requests == []


def test_read_only_client_has_no_mutation_methods() -> None:
    client = FuturesAPIClient()
    assert not hasattr(client, "send_order")
    assert not hasattr(client, "cancel_order")
    assert not hasattr(client, "transfer")


@pytest.mark.parametrize(
    ("method_name", "response_field", "response_value", "path_suffix"),
    [
        ("get_open_positions", "openPositions", [], "/openpositions"),
        ("get_open_orders", "openOrders", [], "/openorders"),
        ("get_fills", "fills", [], "/fills"),
        ("get_leverage_preferences", "leveragePreferences", [], "/leveragepreferences"),
    ],
)
def test_all_private_read_endpoints_are_authenticated_and_schema_checked(
    method_name: str,
    response_field: str,
    response_value: object,
    path_suffix: str,
) -> None:
    transport = FakeTransport({"result": "success", response_field: response_value})
    client = FuturesAPIClient(
        api_key=TEST_KEY,
        api_secret=TEST_SECRET,
        transport=transport,
    )

    getattr(client, method_name)()

    request = transport.requests[0]
    assert request.full_url == DEMO_BASE_URL + "/derivatives/api/v3" + path_suffix
    assert request.get_header("Apikey") == TEST_KEY
    assert request.get_header("Authent")
