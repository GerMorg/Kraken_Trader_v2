import base64

from app.kraken import sign_spot

def test_signature_is_deterministic_for_same_payload():
    secret=base64.b64encode(b"test-secret-key").decode("ascii")
    params={"nonce":"123","ordertype":"market","type":"buy","volume":"0.1","pair":"XBTUSD"}
    assert sign_spot("/0/private/AddOrder",params,secret)==sign_spot("/0/private/AddOrder",params,secret)

def test_signature_changes_when_nonce_changes():
    secret=base64.b64encode(b"test-secret-key").decode("ascii")
    assert sign_spot("/0/private/Balance",{"nonce":"123"},secret)!=sign_spot("/0/private/Balance",{"nonce":"124"},secret)



def test_account_snapshot_does_not_claim_reconciliation_from_api_success():
    from app.kraken import KrakenClient

    client = KrakenClient("k", "s")
    results = {
        "Balance": {"ZUSD": "10"},
        "TradeBalance": {"e": "10", "mf": "10"},
        "OpenOrders": {"open": {}},
        "ClosedOrders": {"closed": {}},
        "OpenPositions": {},
    }
    calls = []
    def fake_private(method, params=None):
        calls.append((method, params))
        return results[method]
    client.private = fake_private
    snapshot = client.account_snapshot()
    assert snapshot["api_queries_succeeded"] is True
    assert snapshot["reconciliation_complete"] is False
    assert "verified" not in snapshot
    assert snapshot["closed_orders"] == {"closed": {}}
    assert calls == [
        ("Balance", None),
        ("TradeBalance", {"aclass": "currency", "asset": "ZUSD"}),
        ("OpenOrders", {"trades": True}),
        ("ClosedOrders", {"closetime": "both", "trades": True, "ofs": 0}),
        # Do not request consolidation=market: individual position txids are needed
        # for partial close accounting, FIFO and per-position financing reconciliation.
        ("OpenPositions", {"docalcs": True}),
    ]


def test_account_snapshot_can_skip_closed_order_history_but_keeps_current_positions():
    from app.kraken import KrakenClient

    client = KrakenClient("k", "s")
    results = {
        "Balance": {"ZUSD": "10"},
        "TradeBalance": {"e": "10", "mf": "10"},
        "OpenOrders": {"open": {}},
        "OpenPositions": {},
    }
    calls = []

    def fake_private(method, params=None):
        calls.append((method, params))
        return results[method]

    client.private = fake_private
    snapshot = client.account_snapshot(include_closed_orders=False)

    assert snapshot["closed_orders"] is None
    assert snapshot["open_orders"] == {"open": {}}
    assert snapshot["open_positions"] == {}
    assert ("ClosedOrders", {"closetime": "both", "trades": True, "ofs": 0}) not in calls
    assert calls[-1] == ("OpenPositions", {"docalcs": True})


def test_history_clients_validate_offsets_and_send_pagination_parameters():
    from app.kraken import KrakenClient

    client = KrakenClient("k", "s")
    calls = []
    client.private = lambda method, params=None: calls.append((method, params)) or {"count": 0}
    client.trades_history(offset=50, start=100, end=200)
    client.ledgers(offset=100, start=100, end=200, ledger_type="rollover", asset="USDC")
    assert calls[0] == ("TradesHistory", {"ofs": 50, "trades": True, "ledgers": True,
                                          "start": 100, "end": 200})
    assert calls[1] == ("Ledgers", {"aclass": "currency", "type": "rollover",
                                    "asset": "USDC", "ofs": 100, "start": 100, "end": 200})


def test_closed_orders_history_uses_validated_pagination_and_close_filter():
    from app.kraken import KrakenClient

    client = KrakenClient("k", "s")
    calls = []
    client.private = lambda method, params=None: calls.append((method, params)) or {"closed": {}}
    result = client.closed_orders(offset=25, start=100, end=200, close_time="close",
                                  include_trades=True)
    assert result == {"closed": {}}
    assert calls == [("ClosedOrders", {
        "ofs": 25, "closetime": "close", "trades": True, "start": 100, "end": 200,
    })]


def test_closed_orders_reject_invalid_offset_and_filter_before_api_call():
    import pytest
    from app.kraken import KrakenClient

    client = KrakenClient("k", "s")
    calls = []
    client.private = lambda method, params=None: calls.append((method, params)) or {}
    with pytest.raises(ValueError, match="CLOSED_ORDERS_OFFSET_MUST_BE_NON_NEGATIVE"):
        client.closed_orders(offset=-1)
    with pytest.raises(ValueError, match="CLOSED_ORDERS_CLOSE_TIME_INVALID"):
        client.closed_orders(close_time="sometimes")
    assert calls == []


def test_cursor_history_params_omit_deprecated_offsets_and_resume_with_token():
    from app.kraken import KrakenClient

    client = KrakenClient("k", "s")
    calls = []
    client.private = lambda method, params=None: calls.append((method, params)) or {}
    client.trades_history(start=100, end=200, with_cursor=True, cursor="opaque-trades")
    client.closed_orders(start=100, end=200, close_time="close", with_cursor=True,
                         cursor="opaque-orders")
    assert calls == [
        ("TradesHistory", {
            "trades": True, "ledgers": True, "with_cursor": True,
            "cursor": "opaque-trades", "start": 100, "end": 200,
        }),
        ("ClosedOrders", {
            "closetime": "close", "trades": True, "with_cursor": True,
            "cursor": "opaque-orders", "start": 100, "end": 200,
        }),
    ]


def test_cursor_history_rejects_nonzero_offset_combination():
    import pytest
    from app.kraken import KrakenClient

    client = KrakenClient("k", "s")
    calls = []
    client.private = lambda method, params=None: calls.append((method, params)) or {}
    with pytest.raises(ValueError, match="TRADES_HISTORY_CURSOR_AND_OFFSET_MUTUALLY_EXCLUSIVE"):
        client.trades_history(offset=5, with_cursor=True)
    with pytest.raises(ValueError, match="CLOSED_ORDERS_CURSOR_AND_OFFSET_MUTUALLY_EXCLUSIVE"):
        client.closed_orders(offset=5, with_cursor=True)
    assert calls == []


def test_add_order_invalid_json_response_is_marked_ambiguous(monkeypatch):
    import base64
    import pytest
    from app.kraken import KrakenAmbiguousError, KrakenClient

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"{not-json"

    monkeypatch.setattr("app.kraken.urlopen", lambda *args, **kwargs: Response())
    client = KrakenClient(
        "test-key",
        base64.b64encode(b"test-secret").decode("ascii"),
        nonce_provider=lambda: 123,
    )
    with pytest.raises(KrakenAmbiguousError, match="ADD_ORDER_RESPONSE_PARSE_OUTCOME_UNKNOWN"):
        client.private("AddOrder", {"pair": "XBTUSD", "type": "buy", "volume": "0.1"})


def test_add_order_network_timeout_is_ambiguous(monkeypatch):
    import base64
    import pytest
    from app.kraken import KrakenAmbiguousError, KrakenClient

    def timeout(*args, **kwargs):
        raise TimeoutError("simulated read timeout")

    monkeypatch.setattr("app.kraken.urlopen", timeout)
    client = KrakenClient(
        "test-key",
        base64.b64encode(b"test-secret").decode("ascii"),
        nonce_provider=lambda: 123,
    )
    with pytest.raises(KrakenAmbiguousError, match="ADD_ORDER_NETWORK_OUTCOME_UNKNOWN"):
        client.private("AddOrder", {"pair": "XBTUSD", "type": "buy", "volume": "0.1"})


def test_public_requests_are_rate_limited_without_real_sleep(monkeypatch):
    import json
    from app.kraken import KrakenClient

    current = [100.0]
    sleeps = []

    def fake_clock():
        return current[0]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        current[0] += seconds

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"error": [], "result": {"status": "online"}}).encode("utf-8")

    calls = []
    def fake_urlopen(request, timeout):
        calls.append((request.full_url, current[0]))
        return Response()

    monkeypatch.setattr("app.kraken.urlopen", fake_urlopen)
    client = KrakenClient(clock=fake_clock, sleeper=fake_sleep)

    assert client.public("SystemStatus") == {"status": "online"}
    assert client.public("SystemStatus") == {"status": "online"}
    assert len(calls) == 2
    assert calls[1][1] - calls[0][1] >= 1.0
    assert sleeps == [1.0]



def _api_key_info_fixture(**overrides):
    return {
        "apiKey": "MUST_NEVER_BE_RETURNED_OR_LOGGED",
        "apiKeyName": "unit-test-key",
        "permissions": [
            "query-funds",
            "query-open-trades",
            "query-closed-trades",
            "query-ledger",
            "modify-trades",
            "close-trades",
        ],
        "validUntil": "0",
        "queryFrom": "0",
        "queryTo": "0",
        **overrides,
    }


def test_api_key_permission_assessment_is_ready_for_complete_read_only_history_scope():
    from app.kraken import assess_api_key_info

    result = assess_api_key_info(_api_key_info_fixture(), now_epoch=100)
    assert result["ready_for_account_reconciliation"] is True
    assert result["missing_required_query_permissions"] == []
    assert result["withdrawal_permissions_present"] == []
    assert result["missing_order_write_permissions"] == []
    assert result["api_key_expired_or_expiry_unknown"] is False
    assert result["historical_query_window_restricted"] is False


def test_api_key_permission_assessment_fails_closed_for_missing_scopes_and_withdrawal_rights():
    from app.kraken import assess_api_key_info

    result = assess_api_key_info(
        _api_key_info_fixture(
            permissions=["query-funds", "withdraw-funds", "add-withdraw-address"]
        ),
        now_epoch=100,
    )
    assert result["ready_for_account_reconciliation"] is False
    assert result["missing_required_query_permissions"] == [
        "query-closed-trades",
        "query-ledger",
        "query-open-trades",
    ]
    assert result["withdrawal_permissions_present"] == [
        "add-withdraw-address",
        "withdraw-funds",
    ]


def test_api_key_permission_assessment_detects_expiry_and_limited_history_window():
    from app.kraken import assess_api_key_info

    result = assess_api_key_info(
        _api_key_info_fixture(validUntil="99", queryFrom="50"),
        now_epoch=100,
    )
    assert result["api_key_expired_or_expiry_unknown"] is True
    assert result["historical_query_window_restricted"] is True
    assert result["ready_for_account_reconciliation"] is False


def test_api_key_permission_assessment_does_not_expose_secret_fields():
    from app.kraken import assess_api_key_info

    result = assess_api_key_info(_api_key_info_fixture(), now_epoch=100)
    assert "apiKey" not in result
    assert "secret" not in result
    assert "MUST_NEVER_BE_RETURNED_OR_LOGGED" not in repr(result)


def test_api_key_permission_assessment_requires_valid_permission_metadata():
    import pytest
    from app.kraken import KrakenAPIError, assess_api_key_info

    with pytest.raises(KrakenAPIError, match="KRAKEN_API_KEY_INFO_PERMISSIONS_INVALID"):
        assess_api_key_info(_api_key_info_fixture(permissions="query-funds"), now_epoch=100)


def test_kraken_client_api_key_permission_assessment_calls_get_api_key_info_without_returning_secret():
    from app.kraken import KrakenClient

    client = KrakenClient("dummy-key", "dummy-secret")
    calls = []

    def fake_private(method, params=None):
        calls.append((method, params))
        return _api_key_info_fixture()

    client.private = fake_private
    result = client.api_key_permission_assessment(now_epoch=100)
    assert calls == [("GetApiKeyInfo", None)]
    assert result["ready_for_account_reconciliation"] is True
    assert "apiKey" not in result



def test_api_key_permission_gate_requires_all_required_read_permissions():
    from app.kraken import api_key_permission_gate_reasons, assess_api_key_info

    assessment = assess_api_key_info(
        _api_key_info_fixture(permissions=["query-funds", "query-open-trades"]),
        now_epoch=100,
    )
    assert api_key_permission_gate_reasons(assessment) == [
        "REQUIRED_QUERY_PERMISSION_MISSING"
    ]


def test_api_key_permission_gate_blocks_withdrawal_scopes_but_keeps_reason_separate():
    from app.kraken import api_key_permission_gate_reasons, assess_api_key_info

    assessment = assess_api_key_info(
        _api_key_info_fixture(permissions=[
            "query-funds", "query-open-trades", "query-closed-trades", "query-ledger",
            "modify-trades", "close-trades", "withdraw-funds",
        ]),
        now_epoch=100,
    )
    reasons = api_key_permission_gate_reasons(assessment)
    assert reasons == ["WITHDRAWAL_PERMISSION_PRESENT"]


def test_api_key_permission_gate_checks_only_requested_order_capabilities():
    from app.kraken import api_key_permission_gate_reasons, assess_api_key_info

    read_only_assessment = assess_api_key_info(
        _api_key_info_fixture(permissions=[
            "query-funds", "query-open-trades", "query-closed-trades", "query-ledger",
        ]),
        now_epoch=100,
    )
    assert api_key_permission_gate_reasons(read_only_assessment) == []
    assert api_key_permission_gate_reasons(
        read_only_assessment,
        validate_shadow_orders=True,
    ) == ["SHADOW_ORDER_VALIDATION_PERMISSION_MISSING"]
    assert api_key_permission_gate_reasons(
        read_only_assessment,
        live_requested=True,
    ) == ["LIVE_ORDER_PERMISSIONS_MISSING"]


def test_api_key_permission_gate_fails_closed_on_inconsistent_assessment():
    from app.kraken import api_key_permission_gate_reasons, assess_api_key_info

    assessment = assess_api_key_info(_api_key_info_fixture(), now_epoch=100)
    assessment["ready_for_account_reconciliation"] = False
    assert api_key_permission_gate_reasons(assessment) == [
        "API_KEY_ASSESSMENT_NOT_READY"
    ]


def test_api_key_permission_gate_rechecks_recoverable_key_configuration_each_cycle():
    from app.kraken import api_key_permission_gate_reasons, assess_api_key_info

    invalid = assess_api_key_info(
        _api_key_info_fixture(queryFrom="100"),
        now_epoch=100,
    )
    corrected = assess_api_key_info(
        _api_key_info_fixture(queryFrom="0"),
        now_epoch=100,
    )
    assert api_key_permission_gate_reasons(invalid) == [
        "API_KEY_HISTORY_QUERY_WINDOW_RESTRICTED"
    ]
    assert api_key_permission_gate_reasons(corrected) == []
