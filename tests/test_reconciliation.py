from __future__ import annotations

from app.persistence import Persistence
from app.reconciliation import reconcile_order_intents


def _intent():
    return {
        "intent_id": "I1",
        "decision_id": "D1",
        "client_order_id": "C1",
        "pair": "XBTUSD",
        "action": "OPEN_MARGIN_LONG",
        "side": "LONG",
        "volume": "1",
        "leverage": 2,
        "decision_evidence": {
            "instrument_pair_aliases": ["XBTUSD", "XBT/USD", "XXBTZUSD"],
        },
    }


def _submitted(db: Persistence) -> None:
    assert db.create_intent(_intent())
    db.transition_intent("I1", "PRECHECK_PASSED")
    db.transition_intent("I1", "SUBMITTING")


def _order(status: str, *, volume: str = "1", executed: str = "0", client_id: str = "C1"):
    return {
        "status": status,
        "cl_ord_id": client_id,
        "vol": volume,
        "vol_exec": executed,
        "descr": {"pair": "XBT/USD", "type": "buy", "ordertype": "market"},
    }


def test_reconciles_open_order_without_resubmitting_and_keeps_full_reconciliation_false():
    db = Persistence(":memory:")
    _submitted(db)
    db.record_exchange_order("O1", _order("open"), source="OPEN")

    result = reconcile_order_intents(db, current_open_order_ids={"O1"})

    assert db.intent_state("I1") == "OPEN"
    assert result["matched_intents"] == 1
    assert result["state_updates"] == 2
    assert result["full_portfolio_reconciliation"] is False
    assert not db.circuit_breakers()
    db.close()


def test_reconciles_closed_order_as_filled_only_when_quantity_proves_fill():
    db = Persistence(":memory:")
    _submitted(db)
    db.record_exchange_order("O1", _order("closed", executed="1"), source="CLOSED")

    result = reconcile_order_intents(db, current_open_order_ids=set())

    assert db.intent_state("I1") == "FILLED"
    assert result["matched_intents"] == 1
    assert result["state_updates"] == 2
    assert result["full_portfolio_reconciliation"] is False
    assert not db.circuit_breakers()
    db.close()


def test_partial_open_order_is_reconciled_as_partial_fill():
    db = Persistence(":memory:")
    _submitted(db)
    db.record_exchange_order("O1", _order("open", executed="0.25"), source="OPEN")

    reconcile_order_intents(db, current_open_order_ids={"O1"})

    assert db.intent_state("I1") == "PARTIALLY_FILLED"
    db.close()


def test_submitted_intent_missing_from_exchange_snapshot_trips_breaker_without_retry():
    db = Persistence(":memory:")
    _submitted(db)

    result = reconcile_order_intents(db, current_open_order_ids=set())

    assert db.intent_state("I1") == "SUBMITTING"
    assert result["unmatched_submitted_intents"] == 1
    assert any(item["name"] == "ORDER_RECONCILIATION" for item in db.circuit_breakers())
    db.close()


def test_duplicate_exchange_matches_trip_breaker_and_leave_intent_unresolved():
    db = Persistence(":memory:")
    _submitted(db)
    db.record_exchange_order("O1", _order("open"), source="OPEN")
    db.record_exchange_order("O2", _order("open"), source="OPEN")

    result = reconcile_order_intents(db, current_open_order_ids={"O1", "O2"})

    assert db.intent_state("I1") == "SUBMITTING"
    assert result["ambiguous_matches"] == 1
    assert any(item["reason"] == "MULTIPLE_EXCHANGE_ORDERS_MATCH_ONE_INTENT"
               for item in db.circuit_breakers())
    db.close()


def test_invalid_exchange_quantity_fails_closed():
    db = Persistence(":memory:")
    _submitted(db)
    db.record_exchange_order("O1", _order("closed", executed="NaN"), source="CLOSED")

    result = reconcile_order_intents(db, current_open_order_ids=set())

    assert db.intent_state("I1") == "SUBMITTING"
    assert result["invalid_exchange_states"] == 1
    assert any(item["reason"] == "EXCHANGE_ORDER_STATE_OR_QUANTITY_UNVERIFIABLE"
               for item in db.circuit_breakers())
    db.close()


def test_manual_external_open_order_is_classified_without_tripping_order_breaker():
    db = Persistence(":memory:")
    db.record_exchange_order("EXTERNAL", _order("open", client_id=""), source="OPEN")

    result = reconcile_order_intents(db, current_open_order_ids={"EXTERNAL"})

    assert result["unmanaged_external_open_orders"] == 1
    assert not db.circuit_breakers()
    db.close()


def test_stale_persisted_open_order_is_not_treated_as_current_external_order():
    db = Persistence(":memory:")
    db.record_exchange_order("STALE-EXTERNAL", _order("open", client_id=""), source="OPEN")

    result = reconcile_order_intents(db, current_open_order_ids=set())

    assert result["current_open_orders"] == 0
    assert result["stale_open_records_excluded"] == 1
    assert result["unmanaged_external_open_orders"] == 0
    assert not db.circuit_breakers()
    db.close()


def test_stale_open_record_cannot_resolve_submitted_intent_without_current_order_evidence():
    db = Persistence(":memory:")
    _submitted(db)
    db.record_exchange_order("O1", _order("open"), source="OPEN")

    result = reconcile_order_intents(db, current_open_order_ids=set())

    assert db.intent_state("I1") == "SUBMITTING"
    assert result["stale_open_records_excluded"] == 1
    assert result["matched_intents"] == 0
    assert result["unmatched_submitted_intents"] == 1
    assert any(
        item["reason"] == "SUBMITTED_INTENT_NOT_VISIBLE_IN_EXCHANGE_SNAPSHOT"
        for item in db.circuit_breakers()
    )
    db.close()


def test_rejected_exchange_order_can_close_submitting_intent_without_false_ack():
    db = Persistence(":memory:")
    _submitted(db)
    db.record_exchange_order("O1", _order("rejected", executed="0"), source="CLOSED")

    result = reconcile_order_intents(db, current_open_order_ids=set())

    assert db.intent_state("I1") == "REJECTED"
    assert result["state_updates"] == 1
    assert not db.circuit_breakers()
    db.close()


def test_canceled_order_with_unverifiable_executed_quantity_is_not_reconciled():
    db = Persistence(":memory:")
    _submitted(db)
    db.record_exchange_order("O1", _order("canceled", executed="NaN"), source="CLOSED")

    result = reconcile_order_intents(db, current_open_order_ids=set())

    assert db.intent_state("I1") == "SUBMITTING"
    assert result["invalid_exchange_states"] == 1
    assert any(item["reason"] == "EXCHANGE_ORDER_STATE_OR_QUANTITY_UNVERIFIABLE"
               for item in db.circuit_breakers())
    db.close()


def test_reconciliation_advances_open_order_to_filled_once_after_closed_snapshot():
    db = Persistence(":memory:")
    _submitted(db)
    open_order = _order("open", executed="0")
    db.record_exchange_order("O1", open_order, source="OPEN")
    first = reconcile_order_intents(db, current_open_order_ids={"O1"})
    assert first["matched_intents"] == 1
    assert db.intent_state("I1") == "OPEN"

    closed_order = _order("closed", executed="1")
    db.record_exchange_order("O1", closed_order, source="CLOSED")
    second = reconcile_order_intents(db, current_open_order_ids=set())
    assert second["matched_intents"] == 1
    assert db.intent_state("I1") == "FILLED"
    assert second["full_portfolio_reconciliation"] is False

    third = reconcile_order_intents(db, current_open_order_ids=set())
    assert third["matched_intents"] == 0
    assert db.intent_state("I1") == "FILLED"
    assert not db.circuit_breakers()
    db.close()


def test_reconciliation_rejects_exchange_order_pair_mismatch():
    db = Persistence(":memory:")
    _submitted(db)
    mismatched = _order("closed", executed="1")
    mismatched["descr"]["pair"] = "ETH/USD"
    db.record_exchange_order("O1", mismatched, source="CLOSED")

    result = reconcile_order_intents(db, current_open_order_ids=set())

    assert db.intent_state("I1") == "SUBMITTING"
    assert result["invalid_exchange_states"] == 1
    assert any(
        item["reason"] == "EXCHANGE_ORDER_DOES_NOT_MATCH_LOCAL_INTENT"
        and item["detail"].get("mismatch") == "PAIR"
        for item in db.circuit_breakers()
    )
    db.close()


def test_reconciliation_rejects_exchange_order_side_mismatch():
    db = Persistence(":memory:")
    _submitted(db)
    mismatched = _order("closed", executed="1")
    mismatched["descr"]["type"] = "sell"
    db.record_exchange_order("O1", mismatched, source="CLOSED")

    result = reconcile_order_intents(db, current_open_order_ids=set())

    assert db.intent_state("I1") == "SUBMITTING"
    assert result["invalid_exchange_states"] == 1
    assert any(
        item["reason"] == "EXCHANGE_ORDER_DOES_NOT_MATCH_LOCAL_INTENT"
        and item["detail"].get("mismatch") == "SIDE"
        for item in db.circuit_breakers()
    )
    db.close()


def test_reconciliation_rejects_exchange_order_volume_mismatch():
    db = Persistence(":memory:")
    _submitted(db)
    mismatched = _order("closed", volume="2", executed="2")
    db.record_exchange_order("O1", mismatched, source="CLOSED")

    result = reconcile_order_intents(db, current_open_order_ids=set())

    assert db.intent_state("I1") == "SUBMITTING"
    assert result["invalid_exchange_states"] == 1
    assert any(
        item["reason"] == "EXCHANGE_ORDER_DOES_NOT_MATCH_LOCAL_INTENT"
        and item["detail"].get("mismatch") == "ORDER_VOLUME"
        for item in db.circuit_breakers()
    )
    db.close()


def test_expected_order_side_mapping_covers_open_reduce_close_and_settlement_actions():
    from app.reconciliation import _expected_order_side

    cases = [
        ("OPEN_SPOT_LONG", "LONG", "buy"),
        ("OPEN_MARGIN_LONG", "LONG", "buy"),
        ("OPEN_MARGIN_SHORT", "SHORT", "sell"),
        ("INCREASE_LONG", "LONG", "buy"),
        ("INCREASE_SHORT", "SHORT", "sell"),
        ("REDUCE_LONG", "LONG", "sell"),
        ("CLOSE_LONG", "LONG", "sell"),
        ("REDUCE_SHORT", "SHORT", "buy"),
        ("CLOSE_SHORT", "SHORT", "buy"),
        ("SETTLE_MARGIN_POSITION", "LONG", "buy"),
        ("SETTLE_MARGIN_POSITION", "SHORT", "sell"),
    ]
    for action, direction, expected in cases:
        assert _expected_order_side({"action": action, "side": direction}) == expected

    assert _expected_order_side({"action": "OPEN_MARGIN_LONG", "side": "SHORT"}) is None
    assert _expected_order_side({"action": "UNKNOWN", "side": "LONG"}) is None
