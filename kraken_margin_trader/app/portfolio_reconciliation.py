from __future__ import annotations

from decimal import Decimal
from typing import Any

D = Decimal


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = D(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _history_ready(persistence: Any) -> tuple[bool, list[str]]:
    pending: list[str] = []
    for source in ("TRADES", "LEDGERS", "CLOSED_ORDERS"):
        state = persistence.history_sync_state(source)
        if (
            state is None
            or not bool(state.get("initial_backfill_complete"))
            or not bool(state.get("generation_complete"))
            or state.get("last_error")
        ):
            pending.append(source)
    return not pending, pending


def reconcile_positions_to_fills(persistence: Any) -> dict[str, Any]:
    """Compare every current margin position to its original Kraken trade/order.

    This verifies position-lot provenance only. It never declares wallet, credit,
    funding, tax, or whole-portfolio reconciliation complete.
    """
    result: dict[str, Any] = {
        "positions_checked": 0,
        "positions_matched_to_fills": 0,
        "opening_orders_matched": 0,
        "related_ledgers_checked": 0,
        "unmatched_position_fills": 0,
        "opening_order_mismatches": 0,
        "fill_field_mismatches": 0,
        "missing_related_ledgers": 0,
        "history_ready": False,
        "pending_history_sources": [],
        "position_fill_reconciliation_complete": False,
        "full_portfolio_reconciliation": False,
    }
    snapshot = persistence.latest_account_snapshot()
    positions = persistence.latest_exchange_position_snapshot()
    if snapshot is None:
        result["status"] = "NO_ACCOUNT_SNAPSHOT"
        persistence.trip_circuit_breaker(
            "POSITION_FILL_RECONCILIATION", "ACCOUNT_SNAPSHOT_MISSING", {}
        )
        persistence.record_event("POSITION_FILL_RECONCILIATION_SUMMARY", result)
        return result
    if not snapshot.get("positions_shape_complete"):
        result["status"] = "POSITION_SNAPSHOT_INVALID"
        persistence.trip_circuit_breaker(
            "POSITION_FILL_RECONCILIATION",
            "POSITION_SNAPSHOT_SHAPE_INVALID",
            {"snapshot_id": snapshot.get("snapshot_id")},
        )
        persistence.record_event("POSITION_FILL_RECONCILIATION_SUMMARY", result)
        return result

    ready, pending = _history_ready(persistence)
    result["history_ready"] = ready
    result["pending_history_sources"] = pending
    if not ready:
        result["status"] = "HISTORY_BACKFILL_PENDING"
        result["positions_checked"] = len(positions)
        persistence.record_event("POSITION_FILL_RECONCILIATION_SUMMARY", result)
        return result

    all_fills = persistence.fills()
    fills_by_trade_id = {str(item.get("trade_id", "")): item for item in all_fills}
    fills_by_order: dict[str, list[dict[str, Any]]] = {}
    for fill_item in all_fills:
        fill_order_id = str(fill_item.get("order_id") or "").strip()
        if fill_order_id:
            fills_by_order.setdefault(fill_order_id, []).append(fill_item)
    orders = {str(item.get("order_id", "")): item for item in persistence.exchange_orders()}
    ledgers = {str(item.get("ledger_id", "")) for item in persistence.ledger_events()}

    for position in positions:
        result["positions_checked"] += 1
        position_id = str(position.get("position_txid", "")).strip()
        raw_position = position.get("raw")
        if not isinstance(raw_position, dict) or not position_id:
            result["unmatched_position_fills"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "POSITION_ID_OR_RAW_EVIDENCE_MISSING",
                {"position_txid": position_id or None},
            )
            continue

        position_order_id = str(raw_position.get("ordertxid", "")).strip()
        position_pair = str(raw_position.get("pair", "")).strip()
        position_side = str(raw_position.get("type", "")).strip().lower()

        # The OpenPositions map key is a position transaction ID; Kraken exposes
        # the originating order separately as ordertxid. Never assume the position
        # key is a TradesHistory trade ID. Prefer a direct ID match only when the
        # related order, pair and side independently confirm it.
        direct_fill = fills_by_trade_id.get(position_id)
        if direct_fill is not None and str(direct_fill.get("order_id") or "").strip() == position_order_id:
            candidate_fills = [direct_fill]
        else:
            candidate_fills = [
                item for item in fills_by_order.get(position_order_id, [])
                if str(item.get("pair", "")).strip() == position_pair
                and str(item.get("side", "")).strip().lower() == position_side
            ]

        if not position_order_id or not position_pair or position_side not in {"buy", "sell"}:
            result["unmatched_position_fills"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "POSITION_OPENING_IDENTITY_INVALID",
                {"position_txid": position_id, "order_txid": position_order_id or None},
            )
            continue
        if not candidate_fills:
            result["unmatched_position_fills"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "OPEN_POSITION_WITHOUT_MATCHING_TRADE_FILL",
                {"position_txid": position_id, "order_txid": position_order_id, "pair": position_pair},
            )
            continue
        if len(candidate_fills) != 1:
            result["unmatched_position_fills"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "POSITION_OPENING_ORDER_HAS_AMBIGUOUS_FILLS",
                {
                    "position_txid": position_id,
                    "order_txid": position_order_id,
                    "candidate_trade_ids": [str(item.get("trade_id", "")) for item in candidate_fills],
                },
            )
            continue
        fill = candidate_fills[0]

        raw_fill_envelope = fill.get("raw")
        raw_fill = raw_fill_envelope.get("raw") if isinstance(raw_fill_envelope, dict) else None
        if not isinstance(raw_fill, dict):
            result["fill_field_mismatches"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "FILL_RAW_EVIDENCE_MISSING",
                {"position_txid": position_id},
            )
            continue

        fill_order_id = str(fill.get("order_id") or "").strip()
        if (
            not position_order_id
            or not fill_order_id
            or position_order_id != fill_order_id
            or position_order_id not in orders
        ):
            result["opening_order_mismatches"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "POSITION_OPENING_ORDER_NOT_RECONCILED",
                {
                    "position_txid": position_id,
                    "position_order_txid": position_order_id or None,
                    "fill_order_txid": fill_order_id or None,
                    "opening_order_known": position_order_id in orders if position_order_id else False,
                },
            )
        else:
            result["opening_orders_matched"] += 1

        position_volume = _decimal(raw_position.get("vol"))
        fill_volume = _decimal(fill.get("volume"))
        fill_pair = str(fill.get("pair", "")).strip()
        fill_side = str(fill.get("side", "")).strip().lower()
        margin_value = _decimal(raw_fill.get("margin"))
        mismatches: list[str] = []
        if position_volume is None or fill_volume is None or position_volume != fill_volume:
            mismatches.append("VOLUME")
        if not position_pair or position_pair != fill_pair:
            mismatches.append("PAIR")
        if position_side not in {"buy", "sell"} or position_side != fill_side:
            mismatches.append("SIDE")
        if margin_value is None or margin_value <= 0:
            mismatches.append("MARGIN_FUNDING_NOT_PROVEN")
        if mismatches:
            result["fill_field_mismatches"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "POSITION_FILL_FIELDS_MISMATCH",
                {"position_txid": position_id, "mismatches": mismatches},
            )
            continue

        result["positions_matched_to_fills"] += 1
        related_ledgers = raw_fill.get("ledgers", [])
        if related_ledgers is None:
            related_ledgers = []
        if not isinstance(related_ledgers, list):
            result["missing_related_ledgers"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "TRADE_LEDGER_REFERENCE_SHAPE_INVALID",
                {"position_txid": position_id},
            )
            continue
        if margin_value is not None and margin_value > 0 and not related_ledgers:
            result["missing_related_ledgers"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "MARGIN_POSITION_WITHOUT_RELATED_LEDGER_REFERENCES",
                {"position_txid": position_id},
            )
        for ledger_id in related_ledgers:
            result["related_ledgers_checked"] += 1
            if str(ledger_id) not in ledgers:
                result["missing_related_ledgers"] += 1
                persistence.trip_circuit_breaker(
                    "POSITION_FILL_RECONCILIATION",
                    "TRADE_REFERENCES_MISSING_LEDGER",
                    {"position_txid": position_id, "trade_id": position_id, "ledger_id": str(ledger_id)},
                )

    complete = (
        result["positions_checked"] == result["positions_matched_to_fills"]
        and result["opening_order_mismatches"] == 0
        and result["fill_field_mismatches"] == 0
        and result["missing_related_ledgers"] == 0
    )
    result["position_fill_reconciliation_complete"] = complete
    result["status"] = "POSITION_FILL_MATCHED" if complete else "POSITION_FILL_MISMATCH"
    # Full portfolio reconciliation remains false until balances, liabilities,
    # external transfers and asset-currency ledger deltas are independently proven.
    result["full_portfolio_reconciliation"] = False
    persistence.record_event("POSITION_FILL_RECONCILIATION_SUMMARY", result)
    return result
