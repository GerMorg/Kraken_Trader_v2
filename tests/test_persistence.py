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