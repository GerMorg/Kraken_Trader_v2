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
