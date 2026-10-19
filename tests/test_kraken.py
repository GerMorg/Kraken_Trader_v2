import base64

from app.kraken import sign_spot

def test_signature_is_deterministic_for_same_payload():
    secret=base64.b64encode(b"test-secret-key").decode("ascii")
    params={"nonce":"123","ordertype":"market","type":"buy","volume":"0.1","pair":"XBTUSD"}
    assert sign_spot("/0/private/AddOrder",params,secret)==sign_spot("/0/private/AddOrder",params,secret)

def test_signature_changes_when_nonce_changes():
    secret=base64.b64encode(b"test-secret-key").decode("ascii")
    assert sign_spot("/0/private/Balance",{"nonce":"123"},secret)!=sign_spot("/0/private/Balance",{"nonce":"124"},secret)


def test_private_json_booleans_keep_native_types_and_signature_matches_body(monkeypatch):
    import json

    from app.kraken import KrakenClient

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"error": [], "result": {"count": 0, "trades": {}}}).encode("utf-8")

    captured = []

    def fake_urlopen(request, timeout):
        captured.append(request)
        return Response()

    monkeypatch.setattr("app.kraken.urlopen", fake_urlopen)
    client = KrakenClient(
        "test-key",
        base64.b64encode(b"test-secret").decode("ascii"),
        nonce_provider=lambda: 123,
    )

    result = client.private("TradesHistory", {"trades": True, "ledgers": False})

    assert result == {"count": 0, "trades": {}}
    assert len(captured) == 1
    body = captured[0].data.decode("utf-8")
    assert body == '{"nonce":123,"trades":true,"ledgers":false}'
    assert json.loads(body) == {"nonce": 123, "trades": True, "ledgers": False}
    assert captured[0].get_header("Content-type") == "application/json"
    assert captured[0].get_header("Api-sign") == sign_spot(
        "/0/private/TradesHistory", body,
        base64.b64encode(b"test-secret").decode("ascii"),
    )



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
    assert calls[1] == ("Ledgers", {"type": "rollover", "asset": "USDC",
                                    "ofs": 100, "start": 100, "end": 200})


def test_query_trade_and_ledger_info_use_exact_bounded_ids():
    import pytest
    from app.kraken import KrakenClient

    client = KrakenClient("k", "s")
    calls = []
    client.private = lambda method, params=None: calls.append((method, params)) or {}

    client.query_trades_info(["T2", "T1", "T2"], include_ledgers=True)
    client.query_ledgers_info(["L2", "L1"])

    assert calls == [
        ("QueryTrades", {"txid": "T2,T1", "trades": False, "ledgers": True}),
        ("QueryLedgers", {"id": "L2,L1"}),
    ]
    with pytest.raises(ValueError, match="QUERY_TRADES_IDS_LIMIT_EXCEEDED"):
        client.query_trades_info([f"T{i}" for i in range(21)])
    with pytest.raises(ValueError, match="QUERY_LEDGERS_IDS_LIMIT_EXCEEDED"):
        client.query_ledgers_info([f"L{i}" for i in range(21)])
    assert len(calls) == 2


def test_ledger_history_cursor_params_omit_deprecated_filters_and_offset():
    from app.kraken import KrakenClient

    client = KrakenClient("k", "s")
    calls = []
    client.private = lambda method, params=None: calls.append((method, params)) or {"ledger": {}}

    client.ledgers(start=100, end=200, with_cursor=True, cursor="opaque-ledger-cursor")

    assert calls == [("Ledgers", {
        "type": "all", "asset": "all", "with_cursor": True,
        "cursor": "opaque-ledger-cursor", "start": 100, "end": 200,
    })]


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


def test_get_api_key_info_uses_json_body_and_signs_the_exact_body(monkeypatch):
    import base64
    import json

    from app.kraken import KrakenClient, sign_spot

    response_payload = {"error": [], "result": _api_key_info_fixture()}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(response_payload).encode("utf-8")

    captured = []

    def fake_urlopen(request, timeout):
        captured.append(request)
        return Response()

    secret = base64.b64encode(b"test-secret").decode("ascii")
    monkeypatch.setattr("app.kraken.urlopen", fake_urlopen)
    client = KrakenClient("test-key", secret, nonce_provider=lambda: 123)

    result = client.private("GetApiKeyInfo")

    assert result["validUntil"] == "0"
    assert result["queryFrom"] == "0"
    assert result["queryTo"] == "0"
    assert len(captured) == 1
    request = captured[0]
    body = request.data.decode("utf-8")
    assert body == '{"nonce":123}'
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("Api-sign") == sign_spot(
        "/0/private/GetApiKeyInfo", body, secret
    )


def test_api_key_assessment_reports_only_safe_response_field_names():
    from app.kraken import assess_api_key_info

    response = _api_key_info_fixture()
    response["apiKey"] = "must-never-appear-in-audit-output"
    assessment = assess_api_key_info(response, now_epoch=100)

    assert "apiKey" not in assessment["api_key_info_response_fields_present"]
    assert {
        "permissions", "validUntil", "queryFrom", "queryTo"
    }.issubset(set(assessment["api_key_info_response_fields_present"]))
    assert assessment["api_key_metadata_fields_present"] == [
        "queryFrom", "queryTo", "validUntil"
    ]
    assert assessment["api_key_metadata_fields_invalid"] == []


def test_api_key_permission_assessment_accepts_snake_case_metadata_from_live_response():
    from app.kraken import assess_api_key_info

    response = _api_key_info_fixture()
    response.pop("validUntil")
    response.pop("queryFrom")
    response.pop("queryTo")
    response.update({"valid_until": "0", "query_from": "0", "query_to": "0"})

    result = assess_api_key_info(response, now_epoch=100)

    assert result["api_key_metadata_complete"] is True
    assert result["api_key_metadata_fields_missing"] == []
    assert result["api_key_metadata_fields_invalid"] == []
    assert result["api_key_metadata_source_fields"] == {
        "validUntil": "valid_until",
        "queryFrom": "query_from",
        "queryTo": "query_to",
    }
    assert result["api_key_valid_until"] == 0
    assert result["api_key_query_from"] == 0
    assert result["api_key_query_to"] == 0
    assert result["api_key_expiry_unknown"] is False
    assert result["historical_query_window_unknown"] is False
    assert result["ready_for_account_reconciliation"] is True


def test_api_key_metadata_alias_conflict_is_treated_as_invalid():
    from app.kraken import assess_api_key_info

    response = _api_key_info_fixture(valid_until="123")

    result = assess_api_key_info(response, now_epoch=100)

    assert result["api_key_metadata_complete"] is False
    assert result["api_key_metadata_fields_invalid"] == ["validUntil"]
    assert result["api_key_expiry_unknown"] is True


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


def test_api_key_permission_assessment_detects_actual_expiry_and_limited_history_window():
    from app.kraken import assess_api_key_info

    result = assess_api_key_info(
        _api_key_info_fixture(validUntil="99", queryFrom="50"),
        now_epoch=100,
    )
    assert result["api_key_expired"] is True
    assert result["api_key_expiry_unknown"] is False
    assert result["api_key_expired_or_expiry_unknown"] is True
    assert result["historical_query_window_restricted"] is True
    assert result["historical_query_window_unknown"] is False
    assert result["ready_for_account_reconciliation"] is False


def test_api_key_metadata_omissions_are_unknown_not_proof_of_expiry_or_restriction():
    from app.kraken import api_key_permission_gate_reasons, assess_api_key_info

    response = _api_key_info_fixture()
    response.pop("validUntil")
    response.pop("queryFrom")
    response.pop("queryTo")

    assessment = assess_api_key_info(response, now_epoch=100)

    assert assessment["api_key_metadata_complete"] is False
    assert assessment["api_key_metadata_fields_missing"] == ["queryFrom", "queryTo", "validUntil"]
    assert assessment["api_key_expiry_unknown"] is True
    assert assessment["api_key_expired"] is False
    assert assessment["api_key_expired_or_expiry_unknown"] is False
    assert assessment["historical_query_window_unknown"] is True
    assert assessment["historical_query_window_restricted"] is False
    assert assessment["ready_for_account_reconciliation"] is False
    assert api_key_permission_gate_reasons(assessment) == []


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


def test_export_status_supports_krakens_list_result(monkeypatch):
    import json
    from app.kraken import KrakenClient, sign_spot

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size=-1):
            return json.dumps({
                "error": [],
                "result": [{"id": "EXPORT-1", "descr": "KTV2-test", "status": "Queued"}],
            }).encode("utf-8")

    captured = []
    monkeypatch.setattr("app.kraken.urlopen", lambda request, timeout: captured.append(request) or Response())
    secret = base64.b64encode(b"test-secret").decode("ascii")
    client = KrakenClient("test-key", secret, nonce_provider=lambda: 123)

    result = client.export_status(report="trades")

    assert result == [{"id": "EXPORT-1", "descr": "KTV2-test", "status": "Queued"}]
    assert len(captured) == 1
    body = captured[0].data.decode("utf-8")
    assert json.loads(body) == {"nonce": 123, "report": "trades"}
    assert captured[0].get_header("Api-sign") == sign_spot(
        "/0/private/ExportStatus", body, secret
    )


def test_trade_export_request_uses_bounded_csv_fields():
    from app.kraken import KrakenClient

    client = KrakenClient("test-key", "test-secret")
    captured = []
    client.private = lambda method, params=None: captured.append((method, params)) or {"id": "EXPORT-42"}

    result = client.request_export_report(
        description="KTV2-LedgerRecovery-test",
        start_epoch=1768000000,
        end_epoch=1768000010,
        report="trades",
    )

    assert result["id"] == "EXPORT-42"
    assert captured == [(
        "AddExport",
        {
            "report": "trades",
            "description": "KTV2-LedgerRecovery-test",
            "format": "CSV",
            "fields": "all",
            "starttm": 1768000000,
            "endtm": 1768000010,
        },
    )]


def test_private_binary_export_download_is_bounded_and_signed(monkeypatch):
    from app.kraken import KrakenClient, sign_spot

    archive_bytes = b"PK\x03\x04kraken-test-zip"
    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size=-1):
            assert size == 25_000_001
            return archive_bytes

    monkeypatch.setattr(
        "app.kraken.urlopen",
        lambda request, timeout: captured.append(request) or Response(),
    )
    secret = base64.b64encode(b"test-secret").decode("ascii")
    client = KrakenClient("test-key", secret, nonce_provider=lambda: 123)

    result = client.retrieve_export("EXPORT-1")

    assert result == archive_bytes
    assert len(captured) == 1
    body = captured[0].data.decode("utf-8")
    assert body == '{"nonce":123,"id":"EXPORT-1"}'
    assert captured[0].get_header("Api-sign") == sign_spot(
        "/0/private/RetrieveExport", body, secret
    )
    assert captured[0].get_header("Accept") == "application/octet-stream"
