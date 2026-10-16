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

    result = reconcile_order_intents(db)

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

    result = reconcile_order_intents(db)

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

    reconcile_order_intents(db)

    assert db.intent_state("I1") == "PARTIALLY_FILLED"
    db.close()


def test_submitted_intent_missing_from_exchange_snapshot_trips_breaker_without_retry():
    db = Persistence(":memory:")
    _submitted(db)

    result = reconcile_order_intents(db)

    assert db.intent_state("I1") == "SUBMITTING"
    assert result["unmatched_submitted_intents"] == 1
    assert any(item["name"] == "ORDER_RECONCILIATION" for item in db.circuit_breakers())
    db.close()


def test_duplicate_exchange_matches_trip_breaker_and_leave_intent_unresolved():
    db = Persistence(":memory:")
    _submitted(db)
    db.record_exchange_order("O1", _order("open"), source="OPEN")
    db.record_exchange_order("O2", _order("open"), source="OPEN")

    result = reconcile_order_intents(db)

    assert db.intent_state("I1") == "SUBMITTING"
    assert result["ambiguous_matches"] == 1
    assert any(item["reason"] == "MULTIPLE_EXCHANGE_ORDERS_MATCH_ONE_INTENT"
               for item in db.circuit_breakers())
    db.close()


def test_invalid_exchange_quantity_fails_closed():
    db = Persistence(":memory:")
    _submitted(db)
    db.record_exchange_order("O1", _order("closed", executed="NaN"), source="CLOSED")

    result = reconcile_order_intents(db)

    assert db.intent_state("I1") == "SUBMITTING"
    assert result["invalid_exchange_states"] == 1
    assert any(item["reason"] == "EXCHANGE_ORDER_STATE_OR_QUANTITY_UNVERIFIABLE"
               for item in db.circuit_breakers())
    db.close()


def test_unmatched_external_open_order_trips_breaker():
    db = Persistence(":memory:")
    db.record_exchange_order("EXTERNAL", _order("open", client_id=""), source="OPEN")

    result = reconcile_order_intents(db)

    assert result["unmatched_open_orders"] == 1
    assert any(item["reason"] == "EXCHANGE_OPEN_ORDER_WITHOUT_MATCHING_LOCAL_INTENT"
               for item in db.circuit_breakers())
    db.close()
