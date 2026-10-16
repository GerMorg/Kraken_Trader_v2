from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

D = Decimal

_SUBMIT_STATES = {
    "SUBMITTING",
    "ACKNOWLEDGED",
    "OPEN",
    "PARTIALLY_FILLED",
    "UNKNOWN_RECONCILING",
}


def _number(value: Any) -> Decimal | None:
    try:
        result = D(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _target_state(order: dict[str, Any]) -> str | None:
    status = str(order.get("status", "")).strip().lower()
    if status not in {"open", "closed", "canceled", "expired", "rejected"}:
        return None
    volume = _number(order.get("vol"))
    executed = _number(order.get("vol_exec"))
    if volume is None or executed is None or volume <= 0 or executed < 0 or executed > volume:
        return None
    if status == "canceled":
        return "CANCELED"
    if status == "expired":
        return "EXPIRED"
    if status == "rejected":
        return "REJECTED" if executed == 0 else None
    if status == "open":
        if executed == volume:
            # An exchange-open order with fully executed volume is inconsistent.
            return None
        return "PARTIALLY_FILLED" if executed > 0 else "OPEN"
    # Kraken's "closed" is only treated as filled when the executed amount proves it.
    return "FILLED" if executed == volume else None


def reconcile_order_intents(persistence: Any) -> dict[str, Any]:
    """Reconcile matching persisted intents against observed exchange orders.

    This only reconciles order state; it intentionally never declares full portfolio,
    funding, trade-history or ledger reconciliation complete.
    """
    exchange_orders = persistence.exchange_orders()
    by_client: dict[str, list[dict[str, Any]]] = {}
    by_order_id: dict[str, dict[str, Any]] = {}
    for row in exchange_orders:
        order_id = str(row.get("order_id", "")).strip()
        if order_id:
            by_order_id[order_id] = row
        client_id = str(row.get("client_order_id") or "").strip()
        if client_id:
            by_client.setdefault(client_id, []).append(row)

    stats: dict[str, Any] = {
        "observed_orders": len(exchange_orders),
        "matched_intents": 0,
        "state_updates": 0,
        "unmatched_submitted_intents": 0,
        "ambiguous_matches": 0,
        "invalid_exchange_states": 0,
        "unmatched_open_orders": 0,
        "full_portfolio_reconciliation": False,
    }
    unresolved = persistence.unresolved_intents()
    known_unresolved_ids = {str(row.get("client_order_id", "")) for row in unresolved}

    # An open exchange order with no live local intent is external or inconsistent.
    for order in exchange_orders:
        if order.get("source") != "OPEN":
            continue
        client_id = str(order.get("client_order_id") or "").strip()
        if not client_id or client_id not in known_unresolved_ids:
            stats["unmatched_open_orders"] += 1
            persistence.trip_circuit_breaker(
                "ORDER_RECONCILIATION",
                "EXCHANGE_OPEN_ORDER_WITHOUT_MATCHING_LOCAL_INTENT",
                {"order_id": order.get("order_id"), "client_order_id": client_id or None},
            )

    for intent in unresolved:
        state = str(intent.get("state", ""))
        client_id = str(intent.get("client_order_id", "")).strip()
        matches = list(by_client.get(client_id, []))
        payload: dict[str, Any] = {}
        try:
            decoded = json.loads(str(intent.get("payload_json", "{}")))
            if isinstance(decoded, dict):
                payload = decoded
        except (TypeError, ValueError):
            payload = {}
        transition_detail = payload.get("last_transition_detail", {})
        if isinstance(transition_detail, dict):
            txids = transition_detail.get("txid", [])
            if isinstance(txids, list):
                for txid in txids:
                    candidate = by_order_id.get(str(txid))
                    if candidate is not None and all(candidate.get("order_id") != m.get("order_id") for m in matches):
                        matches.append(candidate)

        if not matches:
            if state in _SUBMIT_STATES:
                stats["unmatched_submitted_intents"] += 1
                persistence.trip_circuit_breaker(
                    "ORDER_RECONCILIATION",
                    "SUBMITTED_INTENT_NOT_VISIBLE_IN_EXCHANGE_SNAPSHOT",
                    {"intent_id": intent.get("intent_id"), "client_order_id": client_id, "state": state},
                )
            continue
        if len(matches) != 1:
            stats["ambiguous_matches"] += 1
            persistence.trip_circuit_breaker(
                "ORDER_RECONCILIATION",
                "MULTIPLE_EXCHANGE_ORDERS_MATCH_ONE_INTENT",
                {"intent_id": intent.get("intent_id"), "client_order_id": client_id,
                 "order_ids": [m.get("order_id") for m in matches]},
            )
            continue

        order = matches[0]
        stats["matched_intents"] += 1
        if state not in _SUBMIT_STATES:
            stats["invalid_exchange_states"] += 1
            persistence.trip_circuit_breaker(
                "ORDER_RECONCILIATION",
                "EXCHANGE_ORDER_CONTRADICTS_LOCAL_PRE_SUBMIT_STATE",
                {"intent_id": intent.get("intent_id"), "state": state, "order_id": order.get("order_id")},
            )
            continue

        try:
            raw_order = json.loads(str(order.get("raw_json", "{}")))
        except (TypeError, ValueError):
            raw_order = {}
        if not isinstance(raw_order, dict):
            raw_order = {}
        target = _target_state(raw_order)
        if target is None:
            stats["invalid_exchange_states"] += 1
            persistence.trip_circuit_breaker(
                "ORDER_RECONCILIATION",
                "EXCHANGE_ORDER_STATE_OR_QUANTITY_UNVERIFIABLE",
                {"intent_id": intent.get("intent_id"), "state": state, "order_id": order.get("order_id")},
            )
            continue

        detail = {
            "source": "EXCHANGE_ORDER_SNAPSHOT",
            "order_id": order.get("order_id"),
            "exchange_status": raw_order.get("status"),
            "executed_volume": raw_order.get("vol_exec"),
            "order_volume": raw_order.get("vol"),
            "full_portfolio_reconciliation": False,
        }
        try:
            current = state
            if current == "SUBMITTING" and target == "REJECTED":
                persistence.transition_intent(str(intent["intent_id"]), target, detail)
                current = target
                stats["state_updates"] += 1
            elif current == "SUBMITTING":
                persistence.transition_intent(
                    str(intent["intent_id"]), "ACKNOWLEDGED", {**detail, "reconciled_state": "ACKNOWLEDGED"}
                )
                current = "ACKNOWLEDGED"
                stats["state_updates"] += 1
            if target != current:
                persistence.transition_intent(str(intent["intent_id"]), target, detail)
                stats["state_updates"] += 1
        except (KeyError, ValueError) as exc:
            stats["invalid_exchange_states"] += 1
            persistence.trip_circuit_breaker(
                "ORDER_RECONCILIATION",
                "LOCAL_ORDER_STATE_TRANSITION_REJECTED",
                {"intent_id": intent.get("intent_id"), "target": target,
                 "exception_type": type(exc).__name__},
            )

    persistence.record_event("ORDER_RECONCILIATION_SUMMARY", stats)
    return stats
