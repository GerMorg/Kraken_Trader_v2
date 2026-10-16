from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import time
from zoneinfo import ZoneInfo
from typing import Any


D = Decimal
PAGE_BUDGET_PER_SOURCE = 2
REFRESH_OVERLAP_SECONDS = 3600
REFRESH_INTERVAL_SECONDS = 300


def _count(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _timestamp(value: Any) -> tuple[int, str, int] | None:
    try:
        numeric = D(str(value))
        if not numeric.is_finite() or numeric < 0:
            return None
        epoch = int(numeric)
        stamp = datetime.fromtimestamp(float(numeric), tz=timezone.utc).isoformat()
        year = datetime.fromtimestamp(float(numeric), tz=ZoneInfo("Europe/Vienna")).year
        return epoch, stamp, year
    except (ArithmeticError, TypeError, ValueError, OverflowError, OSError):
        return None


def _result_items(value: Any) -> dict[str, dict[str, Any]] | None:
    if not isinstance(value, dict):
        return None
    if any(not isinstance(key, str) or not isinstance(row, dict) for key, row in value.items()):
        return None
    return value


def _save_trade(row_id: str, row: dict[str, Any], persistence: Any) -> bool:
    stamp = _timestamp(row.get("time"))
    try:
        volume = D(str(row["vol"]))
        price = D(str(row["price"]))
        fee = D(str(row["fee"]))
        margin_amount = D(str(row["margin"]))
    except (KeyError, ArithmeticError, TypeError, ValueError):
        # The margin field is required to distinguish a cash-spot fill from
        # a margin fill. Missing data must never be silently classified as spot.
        return False
    side = str(row.get("type", "")).lower()
    pair = str(row.get("pair", "")).strip()
    if (
        stamp is None or not volume.is_finite() or volume <= 0
        or not price.is_finite() or price <= 0
        or not fee.is_finite() or fee < 0
        or not margin_amount.is_finite() or margin_amount < 0
        or side not in {"buy", "sell"} or not pair or not row_id
    ):
        return False
    _, ts_iso, year = stamp
    persistence.record_fill({
        "trade_id": row_id, "order_id": row.get("ordertxid"), "pair": pair,
        "side": side, "volume": str(volume), "price": str(price), "fee": str(fee),
        # Kraken trade history does not always identify the fee asset here; do not guess it.
        "fee_asset": None, "ts": ts_iso, "verified": True, "raw": row,
    })
    persistence.record_tax_event({
        "event_id": "TRADE:" + row_id, "ts": ts_iso, "year": year,
        "event_type": "MARGIN_TRADE_FILL" if margin_amount > 0 else "SPOT_TRADE_FILL",
        "asset": None, "quote_asset": None,
        "quantity": str(volume), "value_eur": None, "fee_amount": str(fee),
        "fee_asset": None, "source_ref": row_id, "verified": False,
        "manual_review": True,
        "payload": {
            "pair": pair, "side": side, "price": str(price),
            "margin_amount": str(margin_amount),
            "product_type": "SPOT_MARGIN" if margin_amount > 0 else "CASH_SPOT",
            "related_ledger_ids": row.get("ledgers", []),
            "raw": row,
        },
    })
    return True


def _save_ledger(row_id: str, row: dict[str, Any], persistence: Any) -> bool:
    stamp = _timestamp(row.get("time"))
    try:
        amount = D(str(row["amount"]))
        fee = D(str(row.get("fee", "0")))
    except (KeyError, ArithmeticError, TypeError, ValueError):
        return False
    asset = str(row.get("asset", "")).strip()
    ledger_type = str(row.get("type", "")).strip()
    if (
        stamp is None or not amount.is_finite() or not fee.is_finite()
        or fee < 0 or not asset or not ledger_type or not row_id
    ):
        return False
    _, ts_iso, year = stamp
    persistence.record_ledger({
        "ledger_id": row_id, "asset": asset, "amount": str(amount),
        "fee": str(fee), "event_type": ledger_type, "ts": ts_iso,
        "verified": True, "raw": row,
    })
    persistence.record_tax_event({
        "event_id": "LEDGER:" + row_id, "ts": ts_iso, "year": year,
        "event_type": "LEDGER_" + ledger_type.upper(), "asset": asset,
        "quote_asset": None, "quantity": str(amount), "value_eur": None,
        "fee_amount": str(fee), "fee_asset": None, "source_ref": row_id,
        "verified": False, "manual_review": True,
        "payload": {"subtype": row.get("subtype"), "raw": row},
    })
    return True


def _save_closed_order(row_id: str, row: dict[str, Any], persistence: Any) -> bool:
    stamp = _timestamp(row.get("closetm"))
    status = str(row.get("status", "")).strip().lower()
    description = row.get("descr")
    order_type = str(description.get("ordertype", "")).lower() if isinstance(description, dict) else ""
    try:
        volume = D(str(row["vol"]))
        executed = D(str(row["vol_exec"]))
    except (KeyError, ArithmeticError, TypeError, ValueError):
        return False
    settlement_zero_volume = order_type == "settle-position" and volume == 0 and executed == 0
    if (
        stamp is None
        or status not in {"closed", "canceled", "expired", "rejected"}
        or not volume.is_finite()
        or not executed.is_finite()
        or volume < 0
        or executed < 0
        or executed > volume
        or (volume == 0 and not settlement_zero_volume)
        or not row_id
    ):
        return False
    persistence.record_exchange_order(row_id, row, source="CLOSED")
    return True


def sync_history_source(
    *,
    client: Any,
    persistence: Any,
    source: str,
    page_budget: int = PAGE_BUDGET_PER_SOURCE,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    """Incrementally backfill one Kraken source and never overstate history completeness."""
    normalized = source.upper()
    if normalized not in {"TRADES", "LEDGERS", "CLOSED_ORDERS"}:
        raise ValueError("HISTORY_SOURCE_INVALID")
    if page_budget < 1 or page_budget > 50:
        raise ValueError("HISTORY_PAGE_BUDGET_OUT_OF_RANGE")
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    state = persistence.history_sync_state(normalized)
    if state is None:
        state = {
            "source": normalized, "start_epoch": 0, "end_epoch": now,
            "next_offset": 0, "next_cursor": None, "expected_count": None,
            "initial_backfill_complete": False, "generation_complete": False,
            "last_error": None,
        }
        persistence.save_history_sync_state(state)
    elif state["generation_complete"] and now - int(state["end_epoch"]) >= REFRESH_INTERVAL_SECONDS:
        previous_end = int(state["end_epoch"])
        state = {
            "source": normalized,
            "start_epoch": max(0, previous_end - REFRESH_OVERLAP_SECONDS),
            "end_epoch": now, "next_offset": 0, "expected_count": None,
            "initial_backfill_complete": bool(state["initial_backfill_complete"]),
            "generation_complete": False, "last_error": None,
        }
        persistence.save_history_sync_state(state)
    elif state["generation_complete"]:
        return {
            "source": normalized, "pages": 0, "items_saved": 0,
            "expected_count": state["expected_count"], "next_offset": state["next_offset"],
            "generation_complete": True,
            "initial_backfill_complete": bool(state["initial_backfill_complete"]),
            "cached_until_refresh": True, "last_error": None,
        }

    cursor_source = normalized in {"TRADES", "CLOSED_ORDERS"}
    pages = 0
    saved = 0
    while pages < page_budget and not state["generation_complete"]:
        offset = int(state["next_offset"])
        request_cursor = state.get("next_cursor") if cursor_source else None
        start = int(state["start_epoch"])
        end = int(state["end_epoch"])
        if normalized == "TRADES":
            response = client.trades_history(
                offset=0, start=start, end=end, with_cursor=True, cursor=request_cursor
            )
            items = _result_items(response.get("trades"))
        elif normalized == "LEDGERS":
            response = client.ledgers(offset=offset, start=start, end=end)
            items = _result_items(response.get("ledger"))
        else:
            response = client.closed_orders(
                offset=0, start=start, end=end, close_time="close",
                include_trades=True, with_cursor=True, cursor=request_cursor,
            )
            items = _result_items(response.get("closed"))

        # Kraken's cursor-mode ClosedOrders response deliberately reports count=0.
        # Cursor-bearing endpoints are therefore completed by cursor.next, not by ofs/count.
        if items is None:
            state["last_error"] = "HISTORY_RESPONSE_SHAPE_INVALID"
            persistence.save_history_sync_state(state)
            break

        next_cursor: str | None = None
        if cursor_source:
            cursor_payload = response.get("cursor")
            if cursor_payload is not None:
                if not isinstance(cursor_payload, dict):
                    state["last_error"] = "HISTORY_CURSOR_SHAPE_INVALID"
                    persistence.save_history_sync_state(state)
                    break
                raw_next = cursor_payload.get("next")
                if raw_next is not None:
                    if not isinstance(raw_next, str) or not raw_next.strip() or raw_next == request_cursor:
                        state["last_error"] = "HISTORY_CURSOR_INVALID_OR_REPEATED"
                        persistence.save_history_sync_state(state)
                        break
                    next_cursor = raw_next
            if next_cursor is not None and not items:
                state["last_error"] = "HISTORY_EMPTY_PAGE_WITH_NEXT_CURSOR"
                persistence.save_history_sync_state(state)
                break
        else:
            count = _count(response.get("count"))
            if count is None:
                state["last_error"] = "HISTORY_RESPONSE_SHAPE_OR_COUNT_INVALID"
                persistence.save_history_sync_state(state)
                break
            expected = state["expected_count"]
            if expected is not None and int(expected) != count:
                # Offset-based ledger pagination needs a stable count in a fixed window.
                state["end_epoch"] = now
                state["next_offset"] = 0
                state["expected_count"] = None
                state["last_error"] = "HISTORY_COUNT_CHANGED_RESTART_REQUIRED"
                persistence.save_history_sync_state(state)
                break
            state["expected_count"] = count
            if not items and offset < count:
                state["last_error"] = "HISTORY_EMPTY_PAGE_BEFORE_REPORTED_COUNT"
                persistence.save_history_sync_state(state)
                break

        for row_id, row in items.items():
            if normalized == "TRADES":
                valid = _save_trade(row_id, row, persistence)
            elif normalized == "LEDGERS":
                valid = _save_ledger(row_id, row, persistence)
            else:
                valid = _save_closed_order(row_id, row, persistence)
            if not valid:
                state["last_error"] = "HISTORY_ITEM_INVALID"
                persistence.save_history_sync_state(state)
                return {
                    "source": normalized, "pages": pages + 1, "items_saved": saved,
                    "expected_count": state["expected_count"], "next_offset": state["next_offset"],
                    "generation_complete": False,
                    "initial_backfill_complete": bool(state["initial_backfill_complete"]),
                    "cached_until_refresh": False, "last_error": state["last_error"],
                }
            saved += 1

        pages += 1
        state["next_offset"] = offset + len(items)
        if cursor_source:
            state["next_cursor"] = next_cursor
            state["generation_complete"] = next_cursor is None
        else:
            state["generation_complete"] = state["next_offset"] >= int(state["expected_count"])
        state["last_error"] = None
        if state["generation_complete"] and int(state["start_epoch"]) == 0:
            state["initial_backfill_complete"] = True
        persistence.save_history_sync_state(state)
    return {
        "source": normalized, "pages": pages, "items_saved": saved,
        "expected_count": state["expected_count"], "next_offset": state["next_offset"],
        "generation_complete": bool(state["generation_complete"]),
        "initial_backfill_complete": bool(state["initial_backfill_complete"]),
        "cached_until_refresh": False, "last_error": state["last_error"],
    }
