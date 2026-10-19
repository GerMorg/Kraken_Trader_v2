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
        "quality_gaps": ["TRADE_LEDGER_REFERENCES_MISSING:46179196fa61"],
    }
    db.save_history_sync_state(state)
    db.close()

    reopened = Persistence(path)
    stored = reopened.history_sync_state("TRADES")
    assert stored is not None
    assert stored["next_cursor"] == "opaque-cursor-token"
    assert stored["next_offset"] == 50
    assert stored["generation_complete"] is False
    assert stored["quality_gaps"] == ["TRADE_LEDGER_REFERENCES_MISSING:46179196fa61"]
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
    assert state["quality_gaps"] == []
    db.save_history_sync_state({
        **state,
        "next_cursor": "migrated-cursor",
        "generation_complete": False,
        "quality_gaps": ["TRADE_LEDGER_REFERENCES_MISSING:abc123def456"],
    })
    migrated = db.history_sync_state("TRADES")
    assert migrated["next_cursor"] == "migrated-cursor"
    assert migrated["quality_gaps"] == ["TRADE_LEDGER_REFERENCES_MISSING:abc123def456"]
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
    assert latest["raw"]["open_positions"]["P1"]["margin"] == "100"

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


def test_position_snapshot_requires_finite_quantities_and_consistent_closed_volume():
    db = Persistence(":memory:")
    base_position = {
        "pair": "XXBTZUSD", "type": "buy", "posstatus": "open",
        "vol": "1", "vol_closed": "0", "cost": "500", "fee": "2",
        "margin": "100", "value": "520", "net": "18", "time": 1760590000,
    }
    for position in (
        {**base_position, "margin": "NaN"},
        {**base_position, "vol": "1", "vol_closed": "2"},
        {**base_position, "value": "-1"},
        {key: value for key, value in base_position.items() if key != "margin"},
    ):
        snapshot = {
            "captured_at": "2026-10-16T05:30:00+00:00",
            "balance": {}, "trade_balance": {},
            "open_orders": {"open": {}}, "closed_orders": {"closed": {}},
            "open_positions": {"P1": position},
        }
        stored = db.record_account_snapshot(snapshot)
        assert stored["positions_shape_complete"] is False
        assert stored["reconciliation_complete"] is False
    db.close()


def test_tax_event_can_be_explicitly_enriched_without_breaking_import_idempotency():
    db = Persistence(":memory:")
    source = {
        "event_id": "TAX-ENRICH",
        "ts": "2026-01-01T00:00:00+00:00",
        "year": 2026,
        "event_type": "SPOT_TRADE_FILL",
        "asset": "XBT",
        "quote_asset": "USD",
        "quantity": "0.1",
        "value_eur": None,
        "fee_amount": "0.0001",
        "fee_asset": "XBT",
        "source_ref": "T1",
        "verified": False,
        "manual_review": True,
        "payload": {"price_usd": "100000"},
    }
    assert db.record_tax_event(source)
    assert not db.record_tax_event(source)

    enriched = {
        **source,
        "value_eur": "9200.00",
        "verified": True,
        "manual_review": False,
        "payload": {"price_usd": "100000", "fx_rate_source": "verified-test-fixture"},
    }
    assert db.update_tax_event(enriched)
    assert not db.update_tax_event({**enriched, "event_id": "DOES-NOT-EXIST"})

    row = db.tax_events_for_year(2026)[0]
    assert row["event_id"] == "TAX-ENRICH"
    assert row["value_eur"] == "9200.00"
    assert bool(row["verified"]) is True
    assert bool(row["manual_review"]) is False
    assert row["payload"]["fx_rate_source"] == "verified-test-fixture"
    # Re-importing the immutable raw source must not erase the enrichment.
    assert not db.record_tax_event(source)
    row_after_reimport = db.tax_events_for_year(2026)[0]
    assert row_after_reimport["value_eur"] == "9200.00"
    assert bool(row_after_reimport["verified"]) is True
    db.close()


def test_authoritative_fill_upsert_corrects_changed_exchange_data_and_is_idempotent():
    db = Persistence(":memory:")
    original = {
        "trade_id": "T-UPD", "order_id": "O1", "pair": "XBTUSD", "side": "buy",
        "volume": "0.1", "price": "90000", "fee": "0.20", "fee_asset": None,
        "ts": "2026-10-01T00:00:00+00:00", "verified": True,
        "raw": {"price": "90000", "fee": "0.20"},
    }
    assert db.upsert_fill_from_exchange(original)
    assert not db.upsert_fill_from_exchange(original)
    corrected = {
        **original, "price": "91000", "fee": "0.21",
        "raw": {"price": "91000", "fee": "0.21"},
    }
    assert db.upsert_fill_from_exchange(corrected)
    rows = db.fills()
    assert len(rows) == 1
    assert rows[0]["price"] == "91000"
    assert rows[0]["fee"] == "0.21"
    assert not db.upsert_fill_from_exchange(corrected)
    db.close()


def test_authoritative_ledger_upsert_corrects_same_id_and_is_idempotent():
    db = Persistence(":memory:")
    original = {
        "ledger_id": "L-UPD", "asset": "USDC", "amount": "-0.15", "fee": "0",
        "event_type": "rollover", "ts": "2026-10-01T00:00:00+00:00",
        "verified": True, "raw": {"amount": "-0.15"},
    }
    assert db.upsert_ledger_from_exchange(original)
    assert not db.upsert_ledger_from_exchange(original)
    corrected = {**original, "amount": "-0.17", "raw": {"amount": "-0.17"}}
    assert db.upsert_ledger_from_exchange(corrected)
    rows = db.ledger_events()
    assert len(rows) == 1
    assert rows[0]["amount"] == "-0.17"
    assert not db.upsert_ledger_from_exchange(corrected)
    db.close()


def test_exchange_tax_reimport_preserves_valid_enrichment_but_invalidates_changed_price():
    db = Persistence(":memory:")
    raw = {
        "event_id": "TRADE:T-ENRICH", "ts": "2026-10-01T00:00:00+00:00", "year": 2026,
        "event_type": "SPOT_TRADE_FILL", "asset": None, "quote_asset": None,
        "quantity": "0.1", "value_eur": None, "fee_amount": "0.20", "fee_asset": None,
        "source_ref": "T-ENRICH", "verified": False, "manual_review": True,
        "payload": {"pair": "XBTUSD", "side": "buy", "price": "90000",
                    "margin_amount": "0", "product_type": "CASH_SPOT",
                    "related_ledger_ids": [], "raw": {"price": "90000"}},
    }
    assert db.upsert_imported_tax_event(raw)
    stored = db.tax_events_for_year(2026)[0]
    enriched = {
        **stored, "value_eur": "8300.00", "verified": True, "manual_review": False,
        "payload": {**stored["payload"], "fx_rate_source": "verified-fixture"},
    }
    assert db.update_tax_event(enriched)

    assert not db.upsert_imported_tax_event(raw)
    preserved = db.tax_events_for_year(2026)[0]
    assert preserved["value_eur"] == "8300.00"
    assert preserved["verified"] == 1
    assert preserved["manual_review"] == 0
    assert preserved["payload"]["fx_rate_source"] == "verified-fixture"

    corrected = {
        **raw, "payload": {**raw["payload"], "price": "91000",
                           "raw": {"price": "91000"}},
    }
    assert db.upsert_imported_tax_event(corrected)
    invalidated = db.tax_events_for_year(2026)[0]
    assert invalidated["value_eur"] is None
    assert invalidated["verified"] == 0
    assert invalidated["manual_review"] == 1
    assert invalidated["payload"]["price"] == "91000"
    db.close()


def test_trade_export_recovery_state_survives_restart(tmp_path):
    path = str(tmp_path / "trade-export-recovery.sqlite3")
    db = Persistence(path)
    db.save_history_export_recovery_request({
        "request_key": "recovery-abc123",
        "report_id": "EXPORT-123",
        "description": "KTV2-LedgerRecovery-abc123",
        "start_epoch": 1768000000,
        "end_epoch": 1768000010,
        "trade_ids": ["T1", "T2"],
        "status": "QUEUED",
        "requested_at_epoch": 1769000000,
        "next_poll_epoch": 1769000300,
        "attempt_count": 1,
        "last_error": None,
    })
    db.close()

    reopened = Persistence(path)
    requests = reopened.history_export_recovery_requests()
    assert len(requests) == 1
    assert requests[0]["report_id"] == "EXPORT-123"
    assert requests[0]["status"] == "QUEUED"
    assert requests[0]["trade_ids"] == ["T1", "T2"]
    assert requests[0]["next_poll_epoch"] == 1769000300

    reopened.save_history_export_recovery_request({
        **requests[0],
        "status": "IMPORTED",
        "next_poll_epoch": 1769022000,
        "last_error": None,
    })
    assert reopened.history_export_recovery_requests()[0]["status"] == "IMPORTED"
    schema = reopened.connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    assert schema is not None and schema["value"] == "7"
    reopened.close()
