from __future__ import annotations

from datetime import datetime, timezone
import json
from decimal import Decimal
from typing import Any

D = Decimal


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = D(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _history_ready(persistence: Any) -> tuple[bool, list[str], list[str]]:
    pending: list[str] = []
    quality_gap_sources: list[str] = []
    for source in ("TRADES", "LEDGERS", "CLOSED_ORDERS"):
        state = persistence.history_sync_state(source)
        if state is None:
            pending.append(source)
            continue

        if (
            not bool(state.get("initial_backfill_complete"))
            or not bool(state.get("generation_complete"))
            or state.get("last_error")
        ):
            pending.append(source)

        quality_gaps = state.get("quality_gaps")
        if not isinstance(quality_gaps, list) or quality_gaps:
            quality_gap_sources.append(source)
            if source not in pending:
                pending.append(source)

    return not pending, pending, quality_gap_sources


def _snapshot_epoch(snapshot: dict[str, Any]) -> Decimal | None:
    value = _decimal(snapshot.get("captured_at"))
    if value is not None:
        return value
    raw = snapshot.get("raw")
    if isinstance(raw, dict):
        return _decimal(raw.get("captured_at"))
    return None


def _balance_map(snapshot: dict[str, Any]) -> dict[str, Decimal] | None:
    balances = snapshot.get("balance")
    if not isinstance(balances, dict):
        return None
    normalized: dict[str, Decimal] = {}
    for asset, raw_amount in balances.items():
        asset_name = str(asset).strip()
        amount = _decimal(raw_amount)
        if not asset_name or amount is None:
            return None
        normalized[asset_name] = amount
    return normalized


def _ledger_epoch(entry: dict[str, Any]) -> Decimal | None:
    envelope = entry.get("raw")
    source = envelope.get("raw") if isinstance(envelope, dict) else None
    if isinstance(source, dict):
        epoch = _decimal(source.get("time"))
        if epoch is not None:
            return epoch
    raw_ts = str(entry.get("ts", "")).strip()
    try:
        parsed = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return D(str(parsed.timestamp()))
    except (TypeError, ValueError, OverflowError):
        return None


def reconcile_wallet_balance_deltas(
    previous_snapshot: dict[str, Any] | None,
    current_snapshot: dict[str, Any],
    ledger_events: list[dict[str, Any]],
    *,
    ledger_history_complete: bool,
) -> dict[str, Any]:
    """Check wallet-balance changes since the prior account snapshot against Kraken ledgers.

    This is one narrow reconciliation component, not proof of margin liabilities or
    whole-portfolio reconciliation. Missing evidence always produces an incomplete result.
    """
    result: dict[str, Any] = {
        "status": "UNKNOWN",
        "complete": False,
        "full_portfolio_reconciliation": False,
        "assets_checked": 0,
        "assets_matched_to_latest_ledger_balance": 0,
        "assets_stable_without_ledger_activity": 0,
        "mismatches": 0,
        "evidence_gaps": [],
        "asset_results": [],
    }
    if previous_snapshot is None:
        result["status"] = "BASELINE_REQUIRED"
        result["evidence_gaps"] = ["PREVIOUS_ACCOUNT_SNAPSHOT_MISSING"]
        return result
    previous_at = _snapshot_epoch(previous_snapshot)
    current_at = _snapshot_epoch(current_snapshot)
    previous_balances = _balance_map(previous_snapshot)
    current_balances = _balance_map(current_snapshot)
    if (
        previous_at is None or current_at is None or current_at <= previous_at
        or previous_balances is None or current_balances is None
    ):
        result["status"] = "SNAPSHOT_SHAPE_OR_TIME_INVALID"
        result["evidence_gaps"] = ["SNAPSHOT_BALANCE_OR_TIMESTAMP_INVALID"]
        return result
    if not ledger_history_complete:
        result["status"] = "LEDGER_WINDOW_INCOMPLETE"
        result["evidence_gaps"] = ["LEDGER_WINDOW_NOT_COMPLETE"]
        return result

    latest: dict[str, tuple[Decimal, list[dict[str, Any]]]] = {}
    for entry in ledger_events:
        if not isinstance(entry, dict):
            continue
        asset = str(entry.get("asset", "")).strip()
        stamp = _ledger_epoch(entry)
        if not asset or stamp is None or stamp < previous_at - D(1) or stamp > current_at + D(2):
            continue
        if asset not in latest or stamp > latest[asset][0]:
            latest[asset] = (stamp, [entry])
        elif stamp == latest[asset][0]:
            latest[asset][1].append(entry)

    assets = sorted(
        set(previous_balances) | set(current_balances) | set(latest),
    )
    tolerance = D("0.00000001")
    for asset in assets:
        previous_amount = previous_balances.get(asset, D(0))
        current_amount = current_balances.get(asset, D(0))
        evidence = latest.get(asset)
        item: dict[str, Any] = {
            "asset": asset,
            "previous_balance": str(previous_amount),
            "current_balance": str(current_amount),
        }
        if evidence is None:
            difference = current_amount - previous_amount
            item["evidence"] = "NO_LEDGER_ACTIVITY"
            item["expected_balance"] = str(previous_amount)
            item["difference"] = str(difference)
            if abs(difference) <= tolerance:
                item["status"] = "BALANCE_STABLE"
                result["assets_stable_without_ledger_activity"] += 1
            else:
                item["status"] = "BALANCE_CHANGED_WITHOUT_LEDGER_EVIDENCE"
                result["mismatches"] += 1
                result["evidence_gaps"].append(f"{asset}:BALANCE_CHANGED_WITHOUT_LEDGER")
        else:
            latest_events = evidence[1]
            ledger_balances = [
                _decimal(
                    (
                        event.get("raw", {}).get("raw", {})
                        if isinstance(event.get("raw"), dict) else {}
                    ).get("balance")
                )
                for event in latest_events
            ]
            if any(value is None for value in ledger_balances):
                item["status"] = "LATEST_LEDGER_BALANCE_MISSING_OR_INVALID"
                result["mismatches"] += 1
                result["evidence_gaps"].append(f"{asset}:LATEST_LEDGER_BALANCE_INVALID")
            elif len({str(value) for value in ledger_balances}) > 1:
                item["status"] = "LATEST_LEDGER_ORDERING_AMBIGUOUS"
                result["mismatches"] += 1
                result["evidence_gaps"].append(f"{asset}:SAME_TIMESTAMP_LEDGER_BALANCES_CONFLICT")
            else:
                expected = ledger_balances[0]
                if expected is None:
                    item["status"] = "LATEST_LEDGER_BALANCE_MISSING_OR_INVALID"
                    result["mismatches"] += 1
                    result["evidence_gaps"].append(f"{asset}:LATEST_LEDGER_BALANCE_INVALID")
                else:
                    difference = current_amount - expected
                    item["expected_balance"] = str(expected)
                    item["difference"] = str(difference)
                    item["evidence"] = "LATEST_LEDGER_BALANCE"
                    if abs(difference) <= tolerance:
                        item["status"] = "MATCHED"
                        result["assets_matched_to_latest_ledger_balance"] += 1
                    else:
                        item["status"] = "BALANCE_DIFFERS_FROM_LATEST_LEDGER"
                        result["mismatches"] += 1
                        result["evidence_gaps"].append(f"{asset}:BALANCE_DIFFERS_FROM_LEDGER")
        result["asset_results"].append(item)
        result["assets_checked"] += 1

    result["complete"] = result["mismatches"] == 0
    if not result["complete"]:
        result["status"] = "WALLET_BALANCE_MISMATCH"
    elif result["assets_matched_to_latest_ledger_balance"] > 0:
        result["status"] = "WALLET_BALANCES_MATCH_LEDGER_WINDOW"
    else:
        result["status"] = "WALLET_BALANCES_STABLE_NO_LEDGER_ACTIVITY"
    result["reconciliation_scope"] = "WALLET_DELTA_WINDOW_ONLY"
    result["ledger_verified_asset_count"] = result["assets_matched_to_latest_ledger_balance"]
    result["stable_without_ledger_activity_count"] = result["assets_stable_without_ledger_activity"]
    # A wallet delta match cannot prove credit balances, margin obligations, position lots,
    # funding/rollover attribution, external-transfer coverage, or whole-portfolio equality.
    result["full_portfolio_reconciliation"] = False
    return result



def assess_entry_reconciliation(
    current_snapshot: dict[str, Any],
    *,
    persisted_snapshot: dict[str, Any],
    wallet_reconciliation: dict[str, Any],
    history_ready: bool,
    order_reconciliation: dict[str, Any],
    position_reconciliation: dict[str, Any],
    active_reconciliation_breakers: list[str],
    equity_baseline_ready: bool,
    closed_history_included: bool,
) -> dict[str, Any]:
    """Assess whether the *current state* is trustworthy enough for entry-risk checks.

    This is deliberately narrower than full historical portfolio/tax reconciliation.
    A clean zero-activity ledger window is valid when both account snapshots agree and
    the bounded ledger query completed; no ledger event is required merely to show that
    balances have remained stable.
    """
    reasons: list[str] = []
    balance = current_snapshot.get("balance")
    trade_balance = current_snapshot.get("trade_balance")
    open_orders = current_snapshot.get("open_orders")
    closed_orders = current_snapshot.get("closed_orders")
    raw_positions = current_snapshot.get("open_positions")

    if not isinstance(balance, dict) or _balance_map(current_snapshot) is None:
        reasons.append("ACCOUNT_BALANCE_SHAPE_INVALID")
    if not isinstance(trade_balance, dict):
        reasons.append("TRADE_BALANCE_SHAPE_INVALID")
    else:
        for field in ("e", "mf", "m"):
            if _decimal(trade_balance.get(field)) is None:
                reasons.append("TRADE_BALANCE_FIELD_INVALID:" + field)
    if not isinstance(open_orders, dict) or not isinstance(open_orders.get("open"), dict):
        reasons.append("OPEN_ORDERS_SHAPE_INVALID")
    if not closed_history_included or not isinstance(closed_orders, dict) or not isinstance(
        closed_orders.get("closed"), dict
    ):
        reasons.append("CLOSED_ORDER_HISTORY_UNAVAILABLE")
    positions = (
        raw_positions.get("open")
        if isinstance(raw_positions, dict) and isinstance(raw_positions.get("open"), dict)
        else raw_positions
    )
    if not isinstance(positions, dict) or any(not isinstance(row, dict) for row in positions.values()):
        reasons.append("OPEN_POSITIONS_SHAPE_INVALID")
    if persisted_snapshot.get("positions_shape_complete") is not True:
        reasons.append("PERSISTED_POSITION_SNAPSHOT_INCOMPLETE")

    if not history_ready:
        reasons.append("ACCOUNT_HISTORY_NOT_READY")
    if (
        wallet_reconciliation.get("complete") is not True
        or wallet_reconciliation.get("mismatches", 0) != 0
        or wallet_reconciliation.get("status") not in {
            "WALLET_BALANCES_MATCH_LEDGER_WINDOW",
            "WALLET_BALANCES_STABLE_NO_LEDGER_ACTIVITY",
        }
    ):
        reasons.append("WALLET_DELTA_NOT_RECONCILED")
    if position_reconciliation.get("position_fill_reconciliation_complete") is not True:
        reasons.append("POSITIONS_NOT_RECONCILED_TO_FILLS")
    if any(
        order_reconciliation.get(field, 0) != 0
        for field in (
            "ambiguous_matches", "invalid_exchange_states",
            "unmatched_open_orders", "unmatched_submitted_intents",
        )
    ):
        reasons.append("ORDER_STATE_RECONCILIATION_INCOMPLETE")
    if not equity_baseline_ready:
        reasons.append("EQUITY_RISK_BASELINE_UNAVAILABLE")
    reasons.extend(
        "ACTIVE_RECONCILIATION_BREAKER:" + name
        for name in sorted(set(active_reconciliation_breakers))
    )

    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "status": "ENTRY_STATE_RECONCILED" if not unique_reasons else "ENTRY_RECONCILIATION_PENDING",
        "entry_reconciliation_ok": not unique_reasons,
        "reasons": unique_reasons,
        "history_ready": bool(history_ready),
        "wallet_delta_status": wallet_reconciliation.get("status"),
        "wallet_delta_complete": wallet_reconciliation.get("complete") is True,
        "positions_reconciled": (
            position_reconciliation.get("position_fill_reconciliation_complete") is True
        ),
        "orders_reconciled": not any(
            order_reconciliation.get(field, 0) != 0
            for field in (
                "ambiguous_matches", "invalid_exchange_states",
                "unmatched_open_orders", "unmatched_submitted_intents",
            )
        ),
        "equity_risk_baseline_ready": bool(equity_baseline_ready),
        "active_reconciliation_breakers": sorted(set(active_reconciliation_breakers)),
        "full_portfolio_reconciliation": False,
        "scope": "CURRENT_ACCOUNT_AND_FORWARD_DELTA;NOT_FULL_HISTORICAL_PORTFOLIO",
    }


def reconcile_positions_to_fills(persistence: Any) -> dict[str, Any]:
    """Compare every current margin position to its original Kraken trade/order.

    This verifies position-lot provenance only. It never declares wallet, credit,
    funding, tax, or whole-portfolio reconciliation complete.
    """
    result: dict[str, Any] = {
        "positions_checked": 0,
        "positions_matched_to_fills": 0,
        "opening_orders_matched": 0,
        "opening_fills_aggregated": 0,
        "related_ledgers_checked": 0,
        "unmatched_position_fills": 0,
        "opening_order_mismatches": 0,
        "fill_field_mismatches": 0,
        "closing_volume_unmapped": 0,
        "close_fill_reconciliation_complete": False,
        "missing_related_ledgers": 0,
        "history_ready": False,
        "pending_history_sources": [],
        "history_quality_gap_sources": [],
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

    ready, pending, quality_gap_sources = _history_ready(persistence)
    result["history_ready"] = ready
    result["pending_history_sources"] = pending
    result["history_quality_gap_sources"] = quality_gap_sources
    if not ready:
        result["status"] = (
            "HISTORY_DATA_QUALITY_GAP"
            if quality_gap_sources
            else "HISTORY_BACKFILL_PENDING"
        )
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
        normalized_fills: list[tuple[dict[str, Any], dict[str, Any], Decimal, Decimal | None]] = []
        malformed_fill_evidence = False
        for candidate in candidate_fills:
            raw_fill_envelope = candidate.get("raw")
            raw_fill = raw_fill_envelope.get("raw") if isinstance(raw_fill_envelope, dict) else None
            candidate_volume = _decimal(candidate.get("volume"))
            if not isinstance(raw_fill, dict):
                malformed_fill_evidence = True
                break
            if candidate_volume is None or candidate_volume <= 0:
                malformed_fill_evidence = True
                break
            normalized_fills.append(
                (candidate, raw_fill, candidate_volume, _decimal(raw_fill.get("margin")))
            )
        if malformed_fill_evidence or len(normalized_fills) != len(candidate_fills):
            result["fill_field_mismatches"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "OPENING_FILL_EVIDENCE_INVALID",
                {
                    "position_txid": position_id,
                    "trade_ids": [str(item.get("trade_id", "")) for item in candidate_fills],
                },
            )
            continue

        position_volume = _decimal(raw_position.get("vol"))
        fill_volume = sum((item[2] for item in normalized_fills), D(0))
        fill_order_ids = {str(item[0].get("order_id") or "").strip() for item in normalized_fills}
        order_record = orders.get(position_order_id)
        order_raw: dict[str, Any] = {}
        if order_record is not None:
            try:
                decoded = json.loads(str(order_record.get("raw_json", "{}")))
                if isinstance(decoded, dict):
                    order_raw = decoded
            except (json.JSONDecodeError, TypeError, ValueError):
                order_raw = {}
        order_identity_valid = (
            position_order_id
            and position_order_id in orders
            and fill_order_ids == {position_order_id}
        )
        if not order_identity_valid:
            result["opening_order_mismatches"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "POSITION_OPENING_ORDER_NOT_RECONCILED",
                {
                    "position_txid": position_id,
                    "position_order_txid": position_order_id or None,
                    "fill_order_ids": sorted(fill_order_ids),
                    "opening_order_known": position_order_id in orders if position_order_id else False,
                },
            )
        else:
            order_volume = _decimal(order_raw.get("vol"))
            order_executed = _decimal(order_raw.get("vol_exec"))
            if (
                order_volume is None or order_executed is None
                or order_volume <= 0 or order_executed < 0
                or order_executed > order_volume
                or order_executed != fill_volume
            ):
                result["opening_order_mismatches"] += 1
                persistence.trip_circuit_breaker(
                    "POSITION_FILL_RECONCILIATION",
                    "OPENING_ORDER_EXECUTED_VOLUME_MISMATCH",
                    {
                        "position_txid": position_id,
                        "order_txid": position_order_id,
                        "order_volume": str(order_volume) if order_volume is not None else None,
                        "order_executed": str(order_executed) if order_executed is not None else None,
                        "summed_fill_volume": str(fill_volume),
                    },
                )
            else:
                result["opening_orders_matched"] += 1

        fill_pairs = {str(item[0].get("pair", "")).strip() for item in normalized_fills}
        fill_sides = {str(item[0].get("side", "")).strip().lower() for item in normalized_fills}
        margin_values = [item[3] for item in normalized_fills]
        mismatches: list[str] = []
        if position_volume is None or position_volume != fill_volume:
            mismatches.append("VOLUME")
        if not position_pair or fill_pairs != {position_pair}:
            mismatches.append("PAIR")
        if position_side not in {"buy", "sell"} or fill_sides != {position_side}:
            mismatches.append("SIDE")
        if any(value is None or value <= 0 for value in margin_values):
            mismatches.append("MARGIN_FUNDING_NOT_PROVEN")
        if mismatches:
            result["fill_field_mismatches"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "POSITION_FILL_FIELDS_MISMATCH",
                {
                    "position_txid": position_id,
                    "mismatches": mismatches,
                    "candidate_trade_ids": [str(item[0].get("trade_id", "")) for item in normalized_fills],
                    "summed_fill_volume": str(fill_volume),
                },
            )
            continue

        if position_volume is None:
            result["fill_field_mismatches"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "POSITION_VOLUME_UNVERIFIABLE",
                {"position_txid": position_id},
            )
            continue
        result["positions_matched_to_fills"] += 1
        result["opening_fills_aggregated"] += len(normalized_fills)
        closed_volume = _decimal(raw_position.get("vol_closed"))
        if closed_volume is None or closed_volume < 0:
            result["closing_volume_unmapped"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "POSITION_CLOSED_VOLUME_INVALID",
                {"position_txid": position_id},
            )
        elif closed_volume > 0:
            # Kraken exposes the position's closed volume but does not always
            # provide a deterministic opening-lot-to-close-fill allocation. Do
            # not claim lot-level close reconciliation where lots overlap.
            result["closing_volume_unmapped"] += 1
            persistence.trip_circuit_breaker(
                "POSITION_FILL_RECONCILIATION",
                "CLOSED_VOLUME_HAS_NO_POSITION_LINKED_CLOSE_FILL",
                {
                    "position_txid": position_id,
                    "volume": str(position_volume),
                    "volume_closed": str(closed_volume),
                    "remaining_volume": str(position_volume - closed_volume),
                },
            )

        for candidate, raw_fill, _candidate_volume, margin_value in normalized_fills:
            related_ledgers = raw_fill.get("ledgers", [])
            if related_ledgers is None:
                related_ledgers = []
            if not isinstance(related_ledgers, list):
                result["missing_related_ledgers"] += 1
                persistence.trip_circuit_breaker(
                    "POSITION_FILL_RECONCILIATION",
                    "TRADE_LEDGER_REFERENCE_SHAPE_INVALID",
                    {"position_txid": position_id, "trade_id": candidate.get("trade_id")},
                )
                continue
            if margin_value is not None and margin_value > 0 and not related_ledgers:
                result["missing_related_ledgers"] += 1
                persistence.trip_circuit_breaker(
                    "POSITION_FILL_RECONCILIATION",
                    "MARGIN_POSITION_WITHOUT_RELATED_LEDGER_REFERENCES",
                    {"position_txid": position_id, "trade_id": candidate.get("trade_id")},
                )
            for ledger_id in related_ledgers:
                result["related_ledgers_checked"] += 1
                if str(ledger_id) not in ledgers:
                    result["missing_related_ledgers"] += 1
                    persistence.trip_circuit_breaker(
                        "POSITION_FILL_RECONCILIATION",
                        "TRADE_REFERENCES_MISSING_LEDGER",
                        {
                            "position_txid": position_id,
                            "trade_id": str(candidate.get("trade_id", "")),
                            "ledger_id": str(ledger_id),
                        },
                    )

    result["close_fill_reconciliation_complete"] = result["closing_volume_unmapped"] == 0
    complete = (
        result["positions_checked"] == result["positions_matched_to_fills"]
        and result["opening_order_mismatches"] == 0
        and result["fill_field_mismatches"] == 0
        and result["closing_volume_unmapped"] == 0
        and result["missing_related_ledgers"] == 0
    )
    result["position_fill_reconciliation_complete"] = complete
    result["status"] = "POSITION_FILL_MATCHED" if complete else "POSITION_FILL_MISMATCH"
    # Full portfolio reconciliation remains false until balances, liabilities,
    # external transfers and asset-currency ledger deltas are independently proven.
    result["full_portfolio_reconciliation"] = False
    persistence.record_event("POSITION_FILL_RECONCILIATION_SUMMARY", result)
    return result
