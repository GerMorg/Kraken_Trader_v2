import pytest

from app.persistence import Persistence

def intent():
    return {"intent_id":"i1","decision_id":"d1","client_order_id":"c1","pair":"XBTUSD",
            "action":"OPEN_SPOT_LONG","side":"LONG","volume":"0.01","leverage":None}

def test_nonce_is_monotonic():
    db=Persistence(":memory:")
    assert db.next_nonce()<db.next_nonce()
    db.close()

def test_order_intent_idempotency_and_state_transition_validation():
    db=Persistence(":memory:")
    assert db.create_intent(intent())
    assert not db.create_intent(intent())
    with pytest.raises(ValueError,match="INVALID_ORDER_STATE_TRANSITION"):
        db.transition_intent("i1","FILLED")
    db.transition_intent("i1","PRECHECK_PASSED")
    db.transition_intent("i1","BLOCKED")
    assert db.intent_state("i1")=="BLOCKED"
    db.close()

def test_duplicate_fill_ledger_and_tax_events_are_ignored():
    db=Persistence(":memory:")
    fill={"trade_id":"T1","pair":"XBTUSD","side":"buy","volume":"1","price":"100","verified":True}
    assert db.record_fill(fill) and not db.record_fill(fill)
    assert db.record_ledger({"ledger_id":"L1","asset":"USDC","amount":"-1","verified":True})
    assert not db.record_ledger({"ledger_id":"L1","asset":"USDC","amount":"-1","verified":True})
    tax={"event_id":"E1","ts":"2026-01-01","year":2026,"event_type":"TRADE","verified":True,"manual_review":False}
    assert db.record_tax_event(tax) and not db.record_tax_event(tax)
    db.close()

def test_circuit_breaker_persists_in_database():
    db=Persistence(":memory:")
    db.trip_circuit_breaker("KRAKEN_API","TEST_FAILURE")
    assert db.circuit_breakers()[0]["reason"]=="TEST_FAILURE"
    db.close()


def test_shadow_intent_is_atomically_terminal_and_never_unresolved():
    db = Persistence(":memory:")
    shadow = intent()
    assert db.create_intent(shadow, initial_state="SHADOW_ONLY")
    assert db.intent_state("i1") == "SHADOW_ONLY"
    assert db.unresolved_intents() == []
    with pytest.raises(ValueError, match="INVALID_ORDER_STATE_TRANSITION"):
        db.transition_intent("i1", "SUBMITTING")
    db.close()


def test_intent_creation_rejects_unknown_initial_state():
    db = Persistence(":memory:")
    with pytest.raises(ValueError, match="INTENT_INITIAL_STATE_INVALID"):
        db.create_intent(intent(), initial_state="SUBMITTING")
    assert db.unresolved_intents() == []
    db.close()


def test_exchange_order_snapshots_are_durable_and_transition_open_to_closed():
    db = Persistence(":memory:")
    opened = {
        "status": "open", "cl_ord_id": "client-1", "vol": "0.2", "vol_exec": "0.1",
        "cost": "10", "fee": "0.04", "opentm": 1760000000,
        "descr": {"pair": "XBT/USD", "type": "buy", "ordertype": "limit"},
    }
    closed = {
        **opened, "status": "closed", "vol_exec": "0.2", "closetm": 1760000100,
    }
    assert db.record_exchange_order("O1", opened, source="OPEN")
    assert db.record_exchange_order("O1", closed, source="CLOSED")
    rows = db.exchange_orders()
    assert len(rows) == 1
    assert rows[0]["order_id"] == "O1"
    assert rows[0]["client_order_id"] == "client-1"
    assert rows[0]["source"] == "CLOSED"
    assert rows[0]["status"] == "closed"
    assert rows[0]["volume_executed"] == "0.2"
    assert '"closetm":1760000100' in rows[0]["raw_json"]
    db.close()


def test_exchange_order_snapshot_rejects_unknown_source_or_empty_id():
    db = Persistence(":memory:")
    with pytest.raises(ValueError, match="EXCHANGE_ORDER_SOURCE_INVALID"):
        db.record_exchange_order("O1", {"status": "open"}, source="HISTORY")
    with pytest.raises(ValueError, match="EXCHANGE_ORDER_ID_OR_PAYLOAD_INVALID"):
        db.record_exchange_order("", {"status": "open"}, source="OPEN")
    db.close()


def test_history_cursor_is_persisted_across_reopen(tmp_path):
    path = str(tmp_path / "cursor.sqlite3")
    db = Persistence(path)
    state = {
        "source": "TRADES", "start_epoch": 0, "end_epoch": 1769000000,
        "next_offset": 50, "next_cursor": "opaque-cursor-token", "expected_count": None,
        "initial_backfill_complete": False, "generation_complete": False, "last_error": None,
    }
    db.save_history_sync_state(state)
    db.close()

    reopened = Persistence(path)
    stored = reopened.history_sync_state("TRADES")
    assert stored is not None
    assert stored["next_cursor"] == "opaque-cursor-token"
    assert stored["next_offset"] == 50
    assert stored["generation_complete"] is False
    reopened.close()


def test_existing_history_schema_is_migrated_to_cursor_column(tmp_path):
    import sqlite3

    path = str(tmp_path / "legacy.sqlite3")
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO schema_meta(key,value) VALUES('schema_version','2');
        CREATE TABLE history_sync_state(
            source TEXT PRIMARY KEY,
            start_epoch INTEGER NOT NULL,
            end_epoch INTEGER NOT NULL,
            next_offset INTEGER NOT NULL,
            expected_count INTEGER,
            initial_backfill_complete INTEGER NOT NULL,
            generation_complete INTEGER NOT NULL,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        INSERT INTO history_sync_state VALUES(
            'TRADES',0,1769000000,12,40,0,0,NULL,'2026-10-16T00:00:00+00:00'
        );
    """)
    connection.close()

    db = Persistence(path)
    state = db.history_sync_state("TRADES")
    assert state is not None
    assert state["next_offset"] == 12
    assert state["next_cursor"] is None
    db.save_history_sync_state({
        **state, "next_cursor": "migrated-cursor", "generation_complete": False,
    })
    assert db.history_sync_state("TRADES")["next_cursor"] == "migrated-cursor"
    db.close()


def test_account_and_position_snapshots_are_durable_and_auditable():
    db = Persistence(":memory:")
    snapshot = {
        "captured_at": "2026-10-16T05:30:00+00:00",
        "balance": {"USDC": "100.0", "XBT": "0.01"},
        "trade_balance": {"e": "100.0", "mf": "80.0", "m": "20.0", "ml": "500"},
        "open_orders": {"open": {"O-OPEN": {"status": "open"}}},
        "closed_orders": {"closed": {"O-CLOSED": {"status": "closed"}}},
        "open_positions": {
            "P1": {
                "pair": "XXBTZUSD", "type": "buy", "posstatus": "open",
                "vol": "0.01", "vol_closed": "0", "cost": "500",
                "fee": "2", "margin": "100", "value": "520", "net": "18",
                "time": 1760590000, "rollovertm": 1760600000,
                "terms": "0.01% per 4 hours",
            },
        },
    }
    recorded = db.record_account_snapshot(snapshot)
    assert recorded["position_count"] == 1
    assert recorded["positions_shape_complete"] is True
    assert recorded["reconciliation_complete"] is False

    latest = db.latest_account_snapshot()
    assert latest is not None
    assert latest["snapshot_id"] == recorded["snapshot_id"]
    assert latest["balance"]["USDC"] == "100.0"
    assert latest["trade_balance"]["ml"] == "500"
    assert latest["raw_json"]["open_positions"]["P1"]["margin"] == "100"

    positions = db.latest_exchange_position_snapshot()
    assert len(positions) == 1
    assert positions[0]["position_txid"] == "P1"
    assert positions[0]["volume"] == "0.01"
    assert positions[0]["margin"] == "100"
    assert positions[0]["net"] == "18"
    assert positions[0]["raw"]["terms"] == "0.01% per 4 hours"
    db.close()


def test_malformed_position_snapshot_is_persisted_but_never_marked_complete():
    db = Persistence(":memory:")
    snapshot = {
        "captured_at": "2026-10-16T05:30:00+00:00",
        "balance": {}, "trade_balance": {},
        "open_orders": {"open": {}}, "closed_orders": {"closed": {}},
        "open_positions": {"P-BAD": {"pair": "XXBTZUSD"}},
    }
    recorded = db.record_account_snapshot(snapshot)
    assert recorded["positions_shape_complete"] is False
    assert recorded["position_count"] == 0
    assert db.latest_account_snapshot()["reconciliation_complete"] is False
    assert db.latest_account_snapshot()["open_positions"]["P-BAD"] == {"pair": "XXBTZUSD"}
    assert db.latest_exchange_position_snapshot() == []
    db.close()
