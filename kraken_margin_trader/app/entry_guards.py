from __future__ import annotations

from typing import Any

from app.models import Instrument


def _normalize_pair(value: Any) -> str:
    return "".join(character for character in str(value).upper() if character.isalnum())


def _response_rows(response: Any) -> tuple[dict[str, Any], bool]:
    """Return rows and whether the response shape can be trusted for entry checks."""
    if not isinstance(response, dict):
        return {}, False
    wrapped = response.get("open")
    rows = wrapped if isinstance(wrapped, dict) else response
    if any(not isinstance(row, dict) for row in rows.values()):
        return {}, False
    return {str(key): row for key, row in rows.items()}, True


def entry_pair_exposure(
    instrument: Instrument,
    *,
    open_positions: Any,
    open_orders: Any,
) -> dict[str, Any]:
    """Fail closed on an existing same-pair position/order or unidentifiable exposure.

    This intentionally disables implicit pyramiding and same-pair reversals. Those
    require a separately validated increase/close state machine; OPEN actions must not
    accidentally create a second independent position for the same pair.
    """
    aliases = {
        _normalize_pair(instrument.pair_id),
        _normalize_pair(instrument.altname),
        _normalize_pair(instrument.wsname),
    }
    aliases.discard("")
    positions, positions_shape_ok = _response_rows(open_positions)
    orders, orders_shape_ok = _response_rows(open_orders)

    matching_positions: list[str] = []
    matching_orders: list[str] = []
    unidentifiable_positions: list[str] = []
    unidentifiable_orders: list[str] = []

    for position_id, position in positions.items():
        pair_key = _normalize_pair(position.get("pair", ""))
        if not pair_key:
            unidentifiable_positions.append(position_id)
        elif pair_key in aliases:
            unidentifiable_positions.append(position_id) if pair_key not in aliases else matching_positions.append(position_id)

    for order_id, order in orders.items():
        description = order.get("descr")
        description = description if isinstance(description, dict) else {}
        pair_key = _normalize_pair(description.get("pair") or order.get("pair") or "")
        if not pair_key:
            unidentifiable_orders.append(order_id)
        elif pair_key in aliases:
            matching_orders.append(order_id)

    reasons: list[str] = []
    if not positions_shape_ok:
        reasons.append("OPEN_POSITION_SNAPSHOT_SHAPE_INVALID")
    if not orders_shape_ok:
        reasons.append("OPEN_ORDER_SNAPSHOT_SHAPE_INVALID")
    if matching_positions:
        reasons.append("SAME_PAIR_POSITION_ALREADY_OPEN")
    if matching_orders:
        reasons.append("SAME_PAIR_ORDER_ALREADY_OPEN")
    if unidentifiable_positions:
        reasons.append("OPEN_POSITION_PAIR_UNIDENTIFIABLE")
    if unidentifiable_orders:
        reasons.append("OPEN_ORDER_PAIR_UNIDENTIFIABLE")

    return {
        "blocked": bool(reasons),
        "reasons": reasons,
        "matching_position_ids": sorted(matching_positions),
        "matching_open_order_ids": sorted(matching_orders),
        "unidentifiable_position_ids": sorted(unidentifiable_positions),
        "unidentifiable_open_order_ids": sorted(unidentifiable_orders),
        "full_portfolio_reconciliation": False,
    }
