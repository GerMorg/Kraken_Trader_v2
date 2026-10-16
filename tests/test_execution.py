from decimal import Decimal
import io
import pytest

from app.audit import AuditLogger
from app.config import Settings
from app.execution import LIVE_EXECUTION_RELEASED,LiveExecutionBlocked,OrderAuthority,OrderIntent,build_add_order_params
from app.kraken import KrakenClient
from app.models import Action,Instrument
from app.persistence import Persistence

D=Decimal

def instrument():
    return Instrument("XBTUSD","XBTUSD","XBT/USD","XXBT","USD","online",D("0.0001"),8,1,(2,3),(2,3))

def test_margin_short_and_reduce_only_order_params():
    short=OrderIntent.create(decision_id="d1",pair="XBTUSD",action=Action.OPEN_MARGIN_SHORT,
      side="SHORT",volume=D("0.1"),product_type="SPOT_MARGIN",leverage=2)
    params=build_add_order_params(short,instrument(),validate_only=True)
    assert params["type"]=="sell" and params["leverage"]=="2" and params["validate"] is True
    close=OrderIntent.create(decision_id="d2",pair="XBTUSD",action=Action.CLOSE_SHORT,
      side="SHORT",volume=D("0.1"),product_type="SPOT_MARGIN",leverage=2)
    params=build_add_order_params(close,instrument(),validate_only=True)
    assert params["type"]=="buy" and params["reduce_only"] is True

def test_unsupported_leverage_is_rejected_before_api_call():
    intent=OrderIntent.create(decision_id="d1",pair="XBTUSD",action=Action.OPEN_MARGIN_LONG,
      side="LONG",volume=D("0.1"),product_type="SPOT_MARGIN",leverage=4)
    with pytest.raises(ValueError,match="LEVERAGE_NOT_SUPPORTED"):
        build_add_order_params(intent,instrument(),validate_only=True)

def test_settlement_requires_verified_currency_and_obligation():
    intent=OrderIntent.create(decision_id="d1",pair="XBTUSD",action=Action.SETTLE_MARGIN_POSITION,
      side="LONG",volume=D("0.1"),product_type="SPOT_MARGIN",leverage=2,
      position_txid="TX1",settlement_currency="USDC",settlement_amount_verified=False)
    with pytest.raises(ValueError,match="SETTLEMENT_AMOUNT_NOT_VERIFIED"):
        build_add_order_params(intent,instrument(),validate_only=True)

def test_live_submission_is_blocked_by_source_owned_gate():
    assert LIVE_EXECUTION_RELEASED is False
    db=Persistence(":memory:")
    audit=AuditLogger(path="/definitely-invalid-root/events.jsonl",stream=io.StringIO())
    intent=OrderIntent.create(decision_id="live-d",pair="XBTUSD",action=Action.OPEN_MARGIN_LONG,
      side="LONG",volume=D("0.1"),product_type="SPOT_MARGIN",leverage=2)
    authority=OrderAuthority(KrakenClient(),db,Settings.from_mapping({
      "mode":"LIVE","live_enabled":True,"kill_switch":False}),audit)
    with pytest.raises(LiveExecutionBlocked,match="INTERNAL_LIVE_RELEASE_GATE_CLOSED"):
        authority.execute(intent,instrument(),prechecks_passed=True,validate_only=False)
    assert db.intent_state(intent.intent_id)=="BLOCKED"
    audit.close(); db.close()

def test_validation_flag_is_not_an_execution_permission():
    intent=OrderIntent.create(decision_id="validate-d",pair="XBTUSD",action=Action.OPEN_SPOT_LONG,
      side="LONG",volume=D("0.1"),product_type="CASH_SPOT")
    assert build_add_order_params(intent,instrument(),validate_only=True)["validate"] is True


def test_short_close_cannot_be_sent_as_cash_spot_buy():
    intent = OrderIntent.create(
        decision_id="short-close-spot",
        pair="XBTUSD",
        action=Action.CLOSE_SHORT,
        side="SHORT",
        volume=D("0.1"),
        product_type="CASH_SPOT",
    )
    with pytest.raises(ValueError, match="SHORT_CLOSE_REQUIRES_SPOT_MARGIN"):
        build_add_order_params(intent, instrument(), validate_only=True)


def test_short_reduction_cannot_be_sent_as_cash_spot_buy():
    intent = OrderIntent.create(
        decision_id="short-reduce-spot",
        pair="XBTUSD",
        action=Action.REDUCE_SHORT,
        side="SHORT",
        volume=D("0.1"),
        product_type="CASH_SPOT",
    )
    with pytest.raises(ValueError, match="SHORT_CLOSE_REQUIRES_SPOT_MARGIN"):
        build_add_order_params(intent, instrument(), validate_only=True)


def test_order_builder_rejects_non_finite_quantity():
    intent = OrderIntent.create(
        decision_id="nan-volume", pair="XBTUSD", action=Action.OPEN_MARGIN_LONG,
        side="LONG", volume=D("NaN"), product_type="SPOT_MARGIN", leverage=2,
    )
    with pytest.raises(ValueError, match="POSITIVE_AND_FINITE"):
        build_add_order_params(intent, instrument(), validate_only=True)


def test_order_builder_rejects_invalid_direction_for_any_order_action():
    intent = OrderIntent.create(
        decision_id="invalid-direction", pair="XBTUSD", action=Action.OPEN_MARGIN_SHORT,
        side="sideways", volume=D("0.1"), product_type="SPOT_MARGIN", leverage=2,
    )
    with pytest.raises(ValueError, match="ORDER_DIRECTION_INVALID"):
        build_add_order_params(intent, instrument(), validate_only=True)


def test_settlement_requires_position_and_reconciled_ledger_evidence():
    intent = OrderIntent.create(
        decision_id="settle-evidence",
        pair="XBTUSD",
        action=Action.SETTLE_MARGIN_POSITION,
        side="LONG",
        volume=D("0.05"),
        product_type="SPOT_MARGIN",
        leverage=2,
        position_txid="P-OPEN-1",
        settlement_currency="USDC",
        settlement_amount_verified=True,
    )
    with pytest.raises(ValueError, match="SETTLEMENT_EVIDENCE_REQUIRED"):
        build_add_order_params(intent, instrument(), validate_only=True)


def test_settlement_order_requires_evidence_that_matches_position_currency_and_volume():
    evidence = {
        "settlement": {
            "position_txid": "P-OPEN-1",
            "currency": "USDC",
            "borrowed_amount": "1200.00",
            "position_volume": "0.05",
            "snapshot_id": "snapshot-sha256",
            "ledger_evidence_id": "LEDGER-SETTLEMENT-DEBT",
            "ledger_reconciled": True,
        }
    }
    intent = OrderIntent.create(
        decision_id="settle-evidence-valid",
        pair="XBTUSD",
        action=Action.SETTLE_MARGIN_POSITION,
        side="LONG",
        volume=D("0.05"),
        product_type="SPOT_MARGIN",
        leverage=2,
        position_txid="P-OPEN-1",
        settlement_currency="USDC",
        settlement_amount_verified=True,
        evidence=evidence,
    )
    params = build_add_order_params(intent, instrument(), validate_only=True)
    assert params["ordertype"] == "settle-position"
    assert params["type"] == "buy"
    assert params["volume"] == "0.05"
    assert params["pair"] == "XBTUSD"
    assert params["leverage"] == "2"
    assert params["validate"] is True


def test_settlement_rejects_currency_or_ledger_mismatch():
    evidence = {
        "settlement": {
            "position_txid": "P-OTHER",
            "currency": "USD",
            "borrowed_amount": "1200",
            "position_volume": "0.05",
            "snapshot_id": "snapshot-sha256",
            "ledger_evidence_id": "LEDGER-1",
            "ledger_reconciled": True,
        }
    }
    intent = OrderIntent.create(
        decision_id="settle-evidence-mismatch",
        pair="XBTUSD",
        action=Action.SETTLE_MARGIN_POSITION,
        side="LONG",
        volume=D("0.05"),
        product_type="SPOT_MARGIN",
        leverage=2,
        position_txid="P-OPEN-1",
        settlement_currency="USDC",
        settlement_amount_verified=True,
        evidence=evidence,
    )
    with pytest.raises(ValueError, match="SETTLEMENT_EVIDENCE_POSITION_MISMATCH"):
        build_add_order_params(intent, instrument(), validate_only=True)


def test_missing_add_order_txid_keeps_intent_unknown_not_rejected(monkeypatch):
    db = Persistence(":memory:")
    audit = AuditLogger(path="/definitely-invalid-root/events.jsonl", stream=io.StringIO())
    client = KrakenClient("k", "s")
    client.private = lambda method, params=None: {}
    monkeypatch.setattr("app.execution.LIVE_EXECUTION_RELEASED", True)
    settings = Settings.from_mapping({
        "mode": "LIVE", "live_enabled": True, "kill_switch": False,
    })
    authority = OrderAuthority(client, db, settings, audit)
    intent = OrderIntent.create(
        decision_id="unknown-response",
        pair="XBTUSD",
        action=Action.OPEN_SPOT_LONG,
        side="LONG",
        volume=D("0.1"),
        product_type="CASH_SPOT",
    )

    with pytest.raises(Exception, match="ADD_ORDER_RESPONSE_TXID_MISSING_OR_INVALID"):
        authority.execute(intent, instrument(), prechecks_passed=True, validate_only=False)

    assert db.intent_state(intent.intent_id) == "UNKNOWN_RECONCILING"
    assert db.intent_state(intent.intent_id) != "REJECTED"
    audit.close()
    db.close()
