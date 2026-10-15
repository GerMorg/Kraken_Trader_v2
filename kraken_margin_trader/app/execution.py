from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import uuid
from typing import Any

from app.config import Settings
from app.kraken import KrakenAPIError, KrakenAmbiguousError, KrakenClient
from app.models import Action, Instrument
from app.persistence import Persistence

D = Decimal
# Source-owned release gate. Never activate before completing all safety/validation criteria.
LIVE_EXECUTION_RELEASED = False

class LiveExecutionBlocked(RuntimeError):
    pass

@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    decision_id: str
    client_order_id: str
    pair: str
    action: Action
    side: str
    volume: Decimal
    product_type: str
    leverage: int | None = None
    position_txid: str | None = None
    settlement_currency: str | None = None
    settlement_amount_verified: bool = False
    decision_evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, *, decision_id: str, pair: str, action: Action, side: str,
               volume: Decimal, product_type: str, leverage: int | None = None,
               position_txid: str | None = None, settlement_currency: str | None = None,
               settlement_amount_verified: bool = False,
               evidence: dict[str, Any] | None = None) -> OrderIntent:
        return cls(str(uuid.uuid4()), decision_id, uuid.uuid4().hex, pair, action, side.upper(),
                   volume, product_type.upper(), leverage, position_txid, settlement_currency,
                   settlement_amount_verified, evidence or {})

def build_add_order_params(intent: OrderIntent, instrument: Instrument, *,
                           validate_only: bool) -> dict[str, Any]:
    if intent.pair not in {instrument.pair_id, instrument.altname, instrument.wsname}:
        raise ValueError("ORDER_PAIR_INSTRUMENT_MISMATCH")
    if intent.volume <= 0:
        raise ValueError("ORDER_VOLUME_MUST_BE_POSITIVE")
    if intent.action in {Action.NO_TRADE, Action.RISK_REDUCTION_REQUIRED}:
        raise ValueError("ACTION_DOES_NOT_MAP_TO_AN_ORDER")
    opens = {Action.OPEN_MARGIN_LONG, Action.OPEN_MARGIN_SHORT, Action.INCREASE_LONG, Action.INCREASE_SHORT}
    closes = {Action.REDUCE_LONG, Action.REDUCE_SHORT, Action.CLOSE_LONG, Action.CLOSE_SHORT}
    is_settle = intent.action == Action.SETTLE_MARGIN_POSITION

    if intent.action == Action.OPEN_SPOT_LONG and (intent.side != "LONG" or intent.product_type != "CASH_SPOT"):
        raise ValueError("SPOT_LONG_PRODUCT_OR_DIRECTION_MISMATCH")
    if intent.action in {Action.OPEN_MARGIN_LONG, Action.INCREASE_LONG} and intent.side != "LONG":
        raise ValueError("MARGIN_LONG_DIRECTION_MISMATCH")
    if intent.action in {Action.OPEN_MARGIN_SHORT, Action.INCREASE_SHORT} and intent.side != "SHORT":
        raise ValueError("MARGIN_SHORT_DIRECTION_MISMATCH")
    if intent.action in closes:
        expected = "LONG" if intent.action in {Action.REDUCE_LONG, Action.CLOSE_LONG} else "SHORT"
        if intent.side != expected or intent.product_type not in {"CASH_SPOT", "SPOT_MARGIN"}:
            raise ValueError("CLOSE_DIRECTION_OR_PRODUCT_MISMATCH")
        if intent.action in {Action.REDUCE_SHORT, Action.CLOSE_SHORT} and intent.product_type != "SPOT_MARGIN":
            raise ValueError("SHORT_CLOSE_REQUIRES_SPOT_MARGIN")

    if is_settle:
        if intent.product_type != "SPOT_MARGIN":
            raise ValueError("SETTLEMENT_REQUIRES_SPOT_MARGIN")
        if not intent.position_txid or not intent.settlement_currency:
            raise ValueError("SETTLEMENT_POSITION_AND_CURRENCY_REQUIRED")
        if not intent.settlement_amount_verified:
            raise ValueError("SETTLEMENT_AMOUNT_NOT_VERIFIED")
        if intent.leverage is None or intent.leverage < 2:
            raise ValueError("SETTLEMENT_LEVERAGE_NOT_CONFIRMED")
        side = "buy" if intent.side == "LONG" else "sell"
        if intent.leverage not in instrument.leverages_for_order_side(side):
            raise ValueError("SETTLEMENT_LEVERAGE_NOT_CONFIRMED_FOR_PAIR")
        # Nonzero volume must be reconciled against the actual credit/position ledger first.
        return {"ordertype":"settle-position","type":side,"volume":format(intent.volume,"f"),
                "pair":instrument.altname,"leverage":str(intent.leverage),
                "cl_ord_id":intent.client_order_id,"validate":validate_only}

    if intent.action in closes:
        side = "sell" if intent.side == "LONG" else "buy"
    elif intent.side == "LONG":
        side = "buy"
    else:
        side = "sell"

    leverage = None
    if intent.action in opens or (intent.action in closes and intent.product_type == "SPOT_MARGIN"):
        if intent.product_type != "SPOT_MARGIN" or intent.leverage is None or intent.leverage < 2:
            raise ValueError("MARGIN_LEVERAGE_REQUIRED")
        if intent.leverage not in instrument.leverages_for_order_side(side):
            raise ValueError("LEVERAGE_NOT_SUPPORTED_BY_PAIR_AND_ORDER_SIDE")
        leverage = str(intent.leverage)
    elif intent.product_type != "CASH_SPOT":
        raise ValueError("PRODUCT_ACTION_MISMATCH")

    params: dict[str, Any] = {"ordertype":"market","type":side,"volume":format(intent.volume,"f"),
                              "pair":instrument.altname,"cl_ord_id":intent.client_order_id,
                              "validate":validate_only}
    if leverage is not None:
        params["leverage"] = leverage
    # reduce_only is documented for margin orders; do not send it on cash spot.
    if intent.action in closes and intent.product_type == "SPOT_MARGIN":
        params["reduce_only"] = True
    return params

class OrderAuthority:
    def __init__(self, client: KrakenClient, persistence: Persistence, settings: Settings, audit: Any):
        self.client, self.persistence, self.settings, self.audit = client, persistence, settings, audit

    def execute(self, intent: OrderIntent, instrument: Instrument, *, prechecks_passed: bool,
                validate_only: bool = False) -> dict[str, Any]:
        record = {"intent_id":intent.intent_id,"decision_id":intent.decision_id,
                  "client_order_id":intent.client_order_id,"pair":intent.pair,
                  "action":intent.action.value,"side":intent.side,"volume":str(intent.volume),
                  "leverage":intent.leverage,"product_type":intent.product_type,
                  "decision_evidence":intent.decision_evidence}
        if not self.persistence.create_intent(record):
            self.audit.emit("ORDER_INTENT_DUPLICATE_BLOCKED",intent_id=intent.intent_id)
            raise ValueError("DUPLICATE_ORDER_INTENT")
        if not prechecks_passed:
            self.persistence.transition_intent(intent.intent_id,"REJECTED",{"reason":"PRECHECK_FAILED"})
            return {"state":"REJECTED","reason":"PRECHECK_FAILED"}
        self.persistence.transition_intent(intent.intent_id,"PRECHECK_PASSED")
        if not validate_only:
            if not LIVE_EXECUTION_RELEASED:
                self.persistence.transition_intent(intent.intent_id,"BLOCKED",{"reason":"INTERNAL_LIVE_RELEASE_GATE_CLOSED"})
                self.audit.emit("LIVE_ORDER_BLOCKED_BY_RELEASE_GATE",intent_id=intent.intent_id,pair=intent.pair,action=intent.action.value)
                raise LiveExecutionBlocked("INTERNAL_LIVE_RELEASE_GATE_CLOSED")
            if not self.settings.live_requested:
                self.persistence.transition_intent(intent.intent_id,"BLOCKED",{"reason":"LIVE_SWITCH_OR_KILL_SWITCH"})
                raise LiveExecutionBlocked("LIVE_SWITCH_OR_KILL_SWITCH")
        try:
            params = build_add_order_params(intent,instrument,validate_only=validate_only)
        except ValueError as exc:
            self.persistence.transition_intent(intent.intent_id,"REJECTED",{"reason":str(exc)})
            self.audit.emit("ORDER_INTENT_REJECTED_PRE_SUBMIT",intent_id=intent.intent_id,reason=str(exc))
            raise
        self.persistence.transition_intent(intent.intent_id,"VALIDATING" if validate_only else "SUBMITTING")
        try:
            result = self.client.private("AddOrder",params)
        except KrakenAmbiguousError:
            self.persistence.transition_intent(intent.intent_id,"UNKNOWN_RECONCILING",{"reason":"API_OUTCOME_UNKNOWN"})
            self.audit.emit("ORDER_OUTCOME_UNKNOWN_RECONCILING",intent_id=intent.intent_id)
            raise
        except KrakenAPIError as exc:
            self.persistence.transition_intent(intent.intent_id,"REJECTED",{"reason":exc.code})
            self.audit.emit("ORDER_REJECTED_BY_EXCHANGE",intent_id=intent.intent_id,code=exc.code)
            raise
        if validate_only:
            self.persistence.transition_intent(intent.intent_id,"VALIDATED",{"result":"VALIDATION_ONLY"})
            self.audit.emit("ORDER_VALIDATION_SUCCEEDED_NOT_EXECUTED",intent_id=intent.intent_id)
            return {"state":"VALIDATED","result":result}
        self.persistence.transition_intent(intent.intent_id,"ACKNOWLEDGED",{"txid":result.get("txid",[])})
        self.audit.emit("ORDER_ACKNOWLEDGED_REQUIRES_RECONCILIATION",intent_id=intent.intent_id)
        return {"state":"ACKNOWLEDGED","result":result}
