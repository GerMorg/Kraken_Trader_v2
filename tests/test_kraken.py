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
        "OpenPositions": {},
    }
    calls = []
    def fake_private(method, params=None):
        calls.append(method)
        return results[method]
    client.private = fake_private
    snapshot = client.account_snapshot()
    assert snapshot["api_queries_succeeded"] is True
    assert snapshot["reconciliation_complete"] is False
    assert "verified" not in snapshot
    assert calls == ["Balance", "TradeBalance", "OpenOrders", "OpenPositions"]


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
