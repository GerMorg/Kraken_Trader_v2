from __future__ import annotations

import base64
import json
from urllib.request import Request

import pytest

from app.futures_api import (
    DEMO_BASE_URL,
    PRODUCTION_BASE_URL,
    FuturesAPIClient,
    build_futures_authent,
)

SECRET = base64.b64encode(b"production-readonly-test-secret").decode("ascii")
KEY = "production-readonly-test-key"


class FakeTransport:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.requests: list[Request] = []

    def __call__(self, request: Request, timeout: float) -> tuple[int, bytes]:
        self.requests.append(request)
        return 200, json.dumps(self.payload).encode("utf-8")


def test_production_readonly_uses_fixed_production_host_and_private_auth() -> None:
    transport = FakeTransport({"result": "success", "accounts": {"multiCollateral": {}}})
    client = FuturesAPIClient(
        api_key=KEY,
        api_secret=SECRET,
        environment="production_read_only",
        transport=transport,
    )

    result = client.get_accounts()

    request = transport.requests[0]
    assert result["accounts"] == {"multiCollateral": {}}
    assert request.full_url == PRODUCTION_BASE_URL + "/derivatives/api/v3/accounts"
    assert request.get_method() == "GET"
    assert request.get_header("Apikey") == KEY
    assert request.get_header("Authent") == build_futures_authent(
        SECRET, "/derivatives/api/v3/accounts"
    )


def test_production_readonly_public_market_data_does_not_send_credentials() -> None:
    transport = FakeTransport({"result": "success", "instruments": []})
    client = FuturesAPIClient(
        api_key=KEY,
        api_secret=SECRET,
        environment="production_read_only",
        transport=transport,
    )

    client.get_instruments()

    request = transport.requests[0]
    assert request.full_url == PRODUCTION_BASE_URL + "/derivatives/api/v3/instruments"
    assert request.get_header("Apikey") is None
    assert request.get_header("Authent") is None


def test_demo_environment_remains_pinned_to_demo_host() -> None:
    transport = FakeTransport({"result": "success", "instruments": []})
    client = FuturesAPIClient(environment="demo", transport=transport)

    client.get_instruments()

    assert transport.requests[0].full_url.startswith(DEMO_BASE_URL + "/")


@pytest.mark.parametrize("environment", ["production", "live", "https://evil.example"])
def test_arbitrary_or_ambiguous_environments_are_rejected(environment: str) -> None:
    with pytest.raises(ValueError, match="FUTURES_PRODUCTION_ENVIRONMENT_NOT_ENABLED"):
        FuturesAPIClient(environment=environment)


def test_production_readonly_client_exposes_no_mutation_methods() -> None:
    client = FuturesAPIClient(
        api_key=KEY,
        api_secret=SECRET,
        environment="production_read_only",
    )
    for name in ("send_order", "cancel_order", "transfer", "set_leverage"):
        assert not hasattr(client, name)
