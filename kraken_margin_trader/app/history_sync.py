from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import time
from zoneinfo import ZoneInfo
from typing import Any


D = Decimal
PAGE_BUDGET_PER_SOURCE = 2
REFRESH_OVERLAP_SECONDS = 3600
REFRESH_INTERVAL_SECONDS = 300
QUERY_TRADES_MISSING_LEDGER_RETRY_SECONDS = 6 * 60 * 60


REQUIRED_HISTORY_SOURCES = ("TRADES", "LEDGERS", "CLOSED_ORDERS")


def history_sources_ready(summaries: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """Return whether all required exchange histories are fully imported for this cycle.

    A successful API call or an initially completed backfill is insufficient while a
    subsequent refresh is still incomplete. Unknown, duplicate, missing, or errored
    sources fail closed. This gate is for new-risk decisions; existing positions still
    require their independent risk-reduction and recovery handling.
    """
    by_source: dict[str, dict[str, Any]] = {}
    gaps: list[str] = []
    for item_summary in summaries:
        source = str(item_summary.get("source", "")).strip().upper()
        if source not in REQUIRED_HISTORY_SOURCES:
            gaps.append("UNEXPECTED_HISTORY_SOURCE:" + (source or "EMPTY"))
            continue
        if source in by_source:
            gaps.append("DUPLICATE_HISTORY_SOURCE:" + source)
            continue
        by_source[source] = item_summary

    for source in REQUIRED_HISTORY_SOURCES:
        summary = by_source.get(source)
        if summary is None:
            gaps.append("HISTORY_SOURCE_MISSING:" + source)
            continue
        if summary.get("initial_backfill_complete") is not True:
            gaps.append("INITIAL_BACKFILL_INCOMPLETE:" + source)
        if summary.get("generation_complete") is not True:
            gaps.append("CURRENT_SYNC_INCOMPLETE:" + source)
        error = summary.get("last_error")
        if error:
            gaps.append("HISTORY_SOURCE_ERROR:" + source + ":" + str(error)[:120])
        quality_gaps = summary.get("quality_gaps", [])
        if not isinstance(quality_gaps, list):
            gaps.append("HISTORY_QUALITY_GAP_STATE_INVALID:" + source)
        else:
            seen_gap_types: set[str] = set()
            for quality_gap in quality_gaps:
                if not isinstance(quality_gap, str) or not quality_gap.strip():
                    gaps.append("HISTORY_QUALITY_GAP_STATE_INVALID:" + source)
                    continue
                gap_type = quality_gap.split(":", 1)[0]
                if gap_type not in seen_gap_types:
                    gaps.append("HISTORY_DATA_QUALITY_GAP:" + source + ":" + gap_type)
                    seen_gap_types.add(gap_type)

    return not gaps, list(dict.fromkeys(gaps))


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


def _trade_id_fingerprint(row_id: str) -> str:
    return hashlib.sha256(row_id.encode("utf-8")).hexdigest()[:12]


def _quality_gap_fields(state: dict[str, Any]) -> dict[str, Any]:
    gaps = state.get("quality_gaps", [])
    if not isinstance(gaps, list):
        gaps = ["HISTORY_QUALITY_GAP_STATE_INVALID"]
    clean_gaps = sorted({item for item in gaps if isinstance(item, str) and item.strip()})
    return {
        "quality_gaps": clean_gaps,
        "quality_gap_count": len(clean_gaps),
        "quality_gap_types": sorted({item.split(":", 1)[0] for item in clean_gaps}),
    }


def _trade_validation_error(row_id: str, row: dict[str, Any]) -> str | None:
    if not row_id:
        return "TRADE_ID_MISSING"
    if _timestamp(row.get("time")) is None:
        return "TRADE_TIME_INVALID"

    numeric_fields: dict[str, Decimal] = {}
    for field in ("vol", "price", "fee", "margin"):
        try:
            numeric_fields[field] = D(str(row[field]))
        except (KeyError, ArithmeticError, TypeError, ValueError):
            return f"TRADE_FIELD_MISSING_OR_INVALID:{field}"
        value = numeric_fields[field]
        if not value.is_finite():
            return f"TRADE_FIELD_NOT_FINITE:{field}"
        if field in {"vol", "price"} and value <= 0:
            return f"TRADE_FIELD_OUT_OF_RANGE:{field}"
        if field in {"fee", "margin"} and value < 0:
            return f"TRADE_FIELD_OUT_OF_RANGE:{field}"

    # Kraken may omit an empty optional "ledgers" array from an individual trade.
    # Import the otherwise valid record but persist a data-quality gap, which keeps
    # new risk blocked until the missing relation has been independently resolved.
    # A malformed field that is present remains a hard validation failure.
    if "ledgers" in row:
        ledger_refs = row.get("ledgers", [])
        if not isinstance(ledger_refs, list) or any(
            not isinstance(ledger_id, str) or not ledger_id.strip()
            for ledger_id in ledger_refs
        ):
            return "TRADE_LEDGER_REFERENCES_INVALID"
    if str(row.get("type", "")).lower() not in {"buy", "sell"}:
        return "TRADE_SIDE_INVALID"
    if not str(row.get("pair", "")).strip():
        return "TRADE_PAIR_MISSING"
    return None


def _save_trade(row_id: str, row: dict[str, Any], persistence: Any) -> bool:
    if _trade_validation_error(row_id, row) is not None:
        return False
    stamp = _timestamp(row.get("time"))
    if stamp is None:
        return False
    volume = D(str(row["vol"]))
    price = D(str(row["price"]))
    fee = D(str(row["fee"]))
    margin_amount = D(str(row["margin"]))
    ledger_refs = row.get("ledgers", [])
    side = str(row.get("type", "")).lower()
    pair = str(row.get("pair", "")).strip()
    _, ts_iso, year = stamp
    persistence.upsert_fill_from_exchange({
        "trade_id": row_id, "order_id": row.get("ordertxid"), "pair": pair,
        "side": side, "volume": str(volume), "price": str(price), "fee": str(fee),
        # Kraken trade history does not always identify the fee asset here; do not guess it.
        "fee_asset": None, "ts": ts_iso, "verified": True, "raw": row,
    })
    persistence.upsert_imported_tax_event({
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
            "related_ledger_ids": ledger_refs,
            "related_ledger_ids_missing": "ledgers" not in row,
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
    persistence.upsert_ledger_from_exchange({
        "ledger_id": row_id, "asset": asset, "amount": str(amount),
        "fee": str(fee), "event_type": ledger_type, "ts": ts_iso,
        "verified": True, "raw": row,
    })
    persistence.upsert_imported_tax_event({
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



def _valid_ledger_references(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    if any(not isinstance(item, str) or not item.strip() for item in value):
        return None
    return list(dict.fromkeys(item.strip() for item in value))


def _trade_row_from_fill(fill: dict[str, Any]) -> dict[str, Any] | None:
    envelope = fill.get("raw")
    if not isinstance(envelope, dict):
        return None
    row = envelope.get("raw")
    if isinstance(row, dict):
        return row
    # Accept older persisted rows that stored the exchange payload directly.
    if "time" in envelope and "type" in envelope:
        return envelope
    return None


def _ledger_row_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    envelope = event.get("raw")
    if not isinstance(envelope, dict):
        return None
    row = envelope.get("raw")
    if isinstance(row, dict):
        return row
    if "time" in envelope and "refid" in envelope:
        return envelope
    return None


def _lookup_trade_history_window(
    *,
    client: Any,
    trade_id: str,
    existing_row: dict[str, Any],
    page_budget: int = 5,
) -> tuple[dict[str, Any] | None, int, str, int | None, int | None]:
    """Re-fetch one trade from TradesHistory with the documented ledgers option.

    QueryTrades does not document the ledgers field. TradesHistory does, and supports
    time bounds plus cursor pagination. Search a narrow window around the persisted
    exchange timestamp, and only accept an exact trade-ID match.
    """
    if page_budget < 1 or page_budget > 20:
        raise ValueError("TRADE_HISTORY_WINDOW_PAGE_BUDGET_OUT_OF_RANGE")
    parsed_time = _timestamp(existing_row.get("time"))
    if parsed_time is None:
        return None, 0, "TRADE_TIME_INVALID", None, None
    target_epoch = parsed_time[0]
    start_epoch = max(0, target_epoch - 5)
    end_epoch = target_epoch + 5
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages = 0

    while pages < page_budget:
        try:
            response = client.trades_history(
                offset=0,
                start=start_epoch,
                end=end_epoch,
                with_cursor=True,
                cursor=cursor,
            )
        except Exception as exc:
            return None, pages, "API_ERROR:" + type(exc).__name__, start_epoch, end_epoch
        pages += 1
        if not isinstance(response, dict):
            return None, pages, "RESPONSE_INVALID", start_epoch, end_epoch
        items = _result_items(response.get("trades"))
        if items is None:
            return None, pages, "TRADES_SHAPE_INVALID", start_epoch, end_epoch

        matched_row = items.get(trade_id)
        if isinstance(matched_row, dict):
            return matched_row, pages, "TRADE_FOUND", start_epoch, end_epoch

        cursor_payload = response.get("cursor")
        if cursor_payload is None:
            return None, pages, "TRADE_ID_NOT_RETURNED", start_epoch, end_epoch
        if not isinstance(cursor_payload, dict):
            return None, pages, "CURSOR_INVALID", start_epoch, end_epoch
        next_cursor = cursor_payload.get("next")
        if next_cursor is None:
            return None, pages, "TRADE_ID_NOT_RETURNED", start_epoch, end_epoch
        if not isinstance(next_cursor, str) or not next_cursor.strip():
            return None, pages, "CURSOR_INVALID", start_epoch, end_epoch
        if next_cursor == cursor or next_cursor in seen_cursors:
            return None, pages, "CURSOR_REPEATED", start_epoch, end_epoch
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return None, pages, "PAGE_BUDGET_EXCEEDED", start_epoch, end_epoch


def reconcile_trade_ledger_coverage(
    *,
    client: Any,
    persistence: Any,
    max_trade_queries: int = 20,
    max_ledger_queries: int = 40,
) -> dict[str, Any]:
    """Repair trade-ledger links using Kraken's ID-based APIs and ledger refid evidence.

    QueryTrades can return up to 20 trade records. QueryLedgers can retrieve exact
    ledger records in batches of 20. The full ledger history is also cross-checked
    by Kraken's explicit ledger refid, which identifies the related trade.
    Missing or contradictory evidence remains a hard new-risk blocker.
    """
    if max_trade_queries < 1 or max_trade_queries > 20:
        raise ValueError("TRADE_LEDGER_QUERY_BUDGET_OUT_OF_RANGE")
    if max_ledger_queries < 0 or max_ledger_queries > 40:
        raise ValueError("TRADE_LEDGER_QUERY_BUDGET_OUT_OF_RANGE")
    trade_state = persistence.history_sync_state("TRADES")
    ledger_state = persistence.history_sync_state("LEDGERS")
    if trade_state is None or ledger_state is None:
        return {
            "status": "HISTORY_STATE_MISSING",
            "trade_rows_queried": 0,
            "trade_rows_recovered": 0,
            "trade_rows_history_window_queried": 0,
            "trade_rows_history_window_pages": 0,
            "trade_rows_history_window_recovered": 0,
            "trade_rows_history_window_ledger_field_missing": 0,
            "trade_rows_history_window_not_found": 0,
            "trade_rows_history_window_empty_ledgers": 0,
            "trade_rows_history_window_invalid": 0,
            "trade_rows_history_window_deferred": 0,
            "next_trade_history_retry_epoch": None,
            "ledger_ids_requested": 0,
            "ledger_rows_recovered": 0,
            "missing_trade_references": 0,
            "missing_ledger_references": 0,
            "ledger_full_backfill_conflict": False,
            "api_errors": ["REQUIRED_HISTORY_STATE_MISSING"],
        }

    trade_gaps_value = trade_state.get("quality_gaps", [])
    trade_gaps = {
        item for item in trade_gaps_value if isinstance(item, str) and item.strip()
    } if isinstance(trade_gaps_value, list) else {"HISTORY_QUALITY_GAP_STATE_INVALID"}
    ledger_gaps_value = ledger_state.get("quality_gaps", [])
    ledger_gaps = {
        item for item in ledger_gaps_value if isinstance(item, str) and item.strip()
    } if isinstance(ledger_gaps_value, list) else {"HISTORY_QUALITY_GAP_STATE_INVALID"}

    api_errors: list[str] = []
    trade_rows_queried = 0
    trade_rows_recovered = 0
    trade_rows_query_empty_ledgers = 0
    trade_rows_query_missing = 0
    trade_rows_query_ledger_field_missing = 0
    trade_rows_query_invalid = 0
    trade_rows_query_deferred = 0
    trade_rows_history_window_queried = 0
    trade_rows_history_window_pages = 0
    trade_rows_history_window_recovered = 0
    trade_rows_history_window_ledger_field_missing = 0
    trade_rows_history_window_not_found = 0
    trade_rows_history_window_empty_ledgers = 0
    trade_rows_history_window_invalid = 0
    trade_rows_history_window_deferred = 0
    next_trade_history_retry_epoch: int | None = None
    ledger_ids_requested = 0
    ledger_rows_recovered = 0
    resolved_fingerprints: dict[str, str] = {}
    query_outcomes: list[dict[str, str]] = []
    confirmed_empty_fingerprints: set[str] = set()
    conflicting_empty_fingerprints: set[str] = set()
    history_window_trade_rows: dict[str, str] = {}

    initial_fills = persistence.fills()
    initial_ledger_events = persistence.ledger_events()
    now_epoch = int(time.time())
    initial_trade_rows_by_trade_id: dict[str, dict[str, Any]] = {}
    cached_query_evidence_by_trade_id: dict[str, dict[str, Any]] = {}
    trade_history_complete = (
        trade_state.get("initial_backfill_complete") is True
        and trade_state.get("generation_complete") is True
        and trade_state.get("last_error") is None
    )
    ledger_history_complete = (
        ledger_state.get("initial_backfill_complete") is True
        and ledger_state.get("generation_complete") is True
        and ledger_state.get("last_error") is None
        and not ledger_gaps
        and bool(initial_ledger_events)
    )

    missing_trade_rows: list[tuple[str, str]] = []
    for fill in initial_fills:
        trade_id = str(fill.get("trade_id", "")).strip()
        row = _trade_row_from_fill(fill)
        if not trade_id or row is None:
            continue
        fingerprint = _trade_id_fingerprint(trade_id)
        initial_trade_rows_by_trade_id[trade_id] = row
        refs = _valid_ledger_references(row.get("ledgers"))
        gap_token = "TRADE_LEDGER_REFERENCES_MISSING:" + fingerprint
        evidence = row.get("_ledger_reference_evidence")
        cached_query_evidence = (
            isinstance(evidence, dict)
            and evidence.get("source") in {
                "QUERY_TRADES_LEDGER_FIELD_MISSING",
                "QUERY_TRADES_LOOKUP_PENDING",
                "QUERY_TRADES_EMPTY_PENDING_LEDGER_CHECK",
                "QUERY_TRADES_EMPTY_CONFIRMED",
                "TRADES_HISTORY_WINDOW_LOOKUP_PENDING",
                "TRADES_HISTORY_WINDOW_LEDGER_FIELD_MISSING",
                "TRADES_HISTORY_WINDOW_TRADE_ID_NOT_FOUND",
                "TRADES_HISTORY_WINDOW_LOOKUP_FAILED",
                "TRADES_HISTORY_WINDOW_PAGE_BUDGET_EXCEEDED",
                "TRADES_HISTORY_WINDOW_EMPTY_PENDING_LEDGER_CHECK",
                "TRADE_HISTORY_EMPTY_CONFIRMED",
            }
        )
        if cached_query_evidence and isinstance(evidence, dict):
            cached_query_evidence_by_trade_id[trade_id] = evidence
        previously_confirmed_empty = (
            isinstance(evidence, dict)
            and evidence.get("source") in {
                "QUERY_TRADES_EMPTY_CONFIRMED", "TRADE_HISTORY_EMPTY_CONFIRMED",
            }
            and evidence.get("ledgers_field_present") is True
            and evidence.get("ledgers_count") == 0
            and evidence.get("full_ledger_history_checked") is True
        )
        if refs:
            trade_gaps.discard(gap_token)
        elif previously_confirmed_empty and trade_history_complete and ledger_history_complete:
            trade_gaps.discard(gap_token)
            confirmed_empty_fingerprints.add(fingerprint)
        elif cached_query_evidence:
            # A QueryTrades response without ledger IDs cannot repair this field. Immediately
            # switch those known cases to a bounded TradesHistory lookup using ledgers=true.
            # Repeated failed time-window lookups use their own persisted cooldown.
            trade_gaps.add(gap_token)
            retry_source = evidence.get("source") if isinstance(evidence, dict) else None
            if isinstance(evidence, dict) and retry_source in {
                "QUERY_TRADES_LEDGER_FIELD_MISSING",
                "QUERY_TRADES_LOOKUP_PENDING",
            }:
                history_window_trade_rows[trade_id] = fingerprint
            elif isinstance(evidence, dict) and retry_source in {
                "TRADES_HISTORY_WINDOW_LEDGER_FIELD_MISSING",
                "TRADES_HISTORY_WINDOW_TRADE_ID_NOT_FOUND",
                "TRADES_HISTORY_WINDOW_LOOKUP_FAILED",
                "TRADES_HISTORY_WINDOW_PAGE_BUDGET_EXCEEDED",
                "TRADES_HISTORY_WINDOW_LOOKUP_PENDING",
            }:
                retry_after = _count(evidence.get("retry_after_epoch"))
                if retry_after is None or retry_after <= now_epoch:
                    history_window_trade_rows[trade_id] = fingerprint
                else:
                    trade_rows_history_window_deferred += 1
        else:
            trade_gaps.add(gap_token)
            missing_trade_rows.append((trade_id, fingerprint))

    # Query exact trade IDs, not the completed history cursor again.
    query_trades = missing_trade_rows[:max_trade_queries]
    if query_trades:
        trade_ids = [trade_id for trade_id, _fingerprint in query_trades]
        trade_rows_queried = len(trade_ids)
        retry_evidence_by_trade_id: dict[str, dict[str, Any]] = {}
        for trade_id in trade_ids:
            previous_evidence = cached_query_evidence_by_trade_id.get(trade_id)
            previous_attempts = (
                _count(previous_evidence.get("attempt_count"))
                if isinstance(previous_evidence, dict) else None
            )
            retry_evidence = {
                "source": "QUERY_TRADES_LOOKUP_PENDING",
                "attempt_count": (previous_attempts or 0) + 1,
                "last_attempt_epoch": now_epoch,
                "retry_after_epoch": now_epoch + QUERY_TRADES_MISSING_LEDGER_RETRY_SECONDS,
                "last_attempt_outcome": "QUERY_IN_PROGRESS",
            }
            retry_evidence_by_trade_id[trade_id] = retry_evidence
            previous_row = initial_trade_rows_by_trade_id.get(trade_id)
            if previous_row is not None:
                _save_trade(
                    trade_id,
                    {**previous_row, "_ledger_reference_evidence": retry_evidence},
                    persistence,
                )
        query_request_failed = False
        try:
            response = client.query_trades_info(trade_ids, include_ledgers=True)
        except Exception as exc:
            api_errors.append("QUERY_TRADES_FAILED:" + type(exc).__name__)
            response = {}
            query_request_failed = True
        if not isinstance(response, dict):
            api_errors.append("QUERY_TRADES_INVALID_RESPONSE")
            response = {}
            query_request_failed = True
        for trade_id, fingerprint in query_trades:
            row = response.get(trade_id)
            if not isinstance(row, dict):
                trade_rows_query_missing += 1
                query_outcomes.append({
                    "trade_id_fingerprint": fingerprint,
                    "outcome": (
                        "QUERY_TRADES_REQUEST_FAILED" if query_request_failed
                        else "TRADE_ID_NOT_RETURNED_BY_QUERY_TRADES"
                    ),
                })
                previous_row = initial_trade_rows_by_trade_id.get(trade_id)
                pending_retry_evidence = retry_evidence_by_trade_id.get(trade_id)
                if previous_row is not None and pending_retry_evidence is not None:
                    _save_trade(
                        trade_id,
                        {
                            **previous_row,
                            "_ledger_reference_evidence": {
                                **pending_retry_evidence,
                                "last_attempt_outcome": (
                                    "QUERY_TRADES_REQUEST_FAILED" if query_request_failed
                                    else "TRADE_ID_NOT_RETURNED_BY_QUERY_TRADES"
                                ),
                            },
                        },
                        persistence,
                    )
                continue
            if "ledgers" not in row:
                trade_rows_query_ledger_field_missing += 1
                query_outcomes.append({
                    "trade_id_fingerprint": fingerprint,
                    "outcome": "QUERY_TRADES_LEDGER_FIELD_MISSING",
                })
                validation_error = _trade_validation_error(trade_id, row)
                if validation_error is not None:
                    trade_rows_query_invalid += 1
                    query_outcomes.append({
                        "trade_id_fingerprint": fingerprint,
                        "outcome": "QUERY_TRADES_ROW_INVALID:" + validation_error,
                    })
                    continue
                # Persist the exact valid trade payload and the missing-field outcome.
                # This is diagnostic evidence only: it does not resolve the ledger gap
                # or release the new-risk gate, but prevents redundant API calls.
                row_to_save = {
                    **row,
                    "_ledger_reference_evidence": {
                        **retry_evidence_by_trade_id.get(trade_id, {}),
                        "source": "QUERY_TRADES_LEDGER_FIELD_MISSING",
                        "ledgers_field_present": False,
                        "last_attempt_outcome": "QUERY_TRADES_LEDGER_FIELD_MISSING",
                    },
                }
                if not _save_trade(trade_id, row_to_save, persistence):
                    trade_rows_query_invalid += 1
                    query_outcomes.append({
                        "trade_id_fingerprint": fingerprint,
                        "outcome": "QUERY_TRADES_ROW_NOT_SAVED",
                    })
                continue
            refs = _valid_ledger_references(row.get("ledgers"))
            if refs is None:
                trade_rows_query_invalid += 1
                query_outcomes.append({
                    "trade_id_fingerprint": fingerprint,
                    "outcome": "QUERY_TRADES_LEDGER_FIELD_INVALID",
                })
                continue
            validation_error = _trade_validation_error(trade_id, row)
            if validation_error is not None:
                trade_rows_query_invalid += 1
                query_outcomes.append({
                    "trade_id_fingerprint": fingerprint,
                    "outcome": "QUERY_TRADES_ROW_INVALID:" + validation_error,
                })
                continue

            row_to_save = dict(row)
            if not refs:
                # The endpoint explicitly returned an empty relation. Do not release
                # the risk gate until a complete Ledger history independently confirms
                # that no Ledger row has this trade's reference ID.
                row_to_save["_ledger_reference_evidence"] = {
                    **retry_evidence_by_trade_id.get(trade_id, {}),
                    "source": "QUERY_TRADES_EMPTY_PENDING_LEDGER_CHECK",
                    "ledgers_field_present": True,
                    "ledgers_count": 0,
                    "last_attempt_outcome": "QUERY_TRADES_RETURNED_EMPTY_LEDGER_LIST",
                }
                trade_rows_query_empty_ledgers += 1
                query_outcomes.append({
                    "trade_id_fingerprint": fingerprint,
                    "outcome": "QUERY_TRADES_RETURNED_EMPTY_LEDGER_LIST",
                })
            else:
                # A fresh non-empty exchange result invalidates any prior empty-list
                # confirmation before the referenced IDs are checked.
                row_to_save.pop("_ledger_reference_evidence", None)

            if not _save_trade(trade_id, row_to_save, persistence):
                trade_rows_query_invalid += 1
                query_outcomes.append({
                    "trade_id_fingerprint": fingerprint,
                    "outcome": "QUERY_TRADES_ROW_NOT_SAVED",
                })
                continue
            if refs:
                trade_rows_recovered += 1
                resolved_fingerprints[fingerprint] = "QUERY_TRADES"

    # QueryTrades' documented response does not expose a "ledgers" field.
    # When that exact-ID lookup omits it, re-query the documented TradesHistory endpoint
    # over a narrow timestamp window, with ledgers=true and cursor pagination.
    # This bypasses the obsolete QueryTrades cooldown once, but does not clear any gap
    # unless the returned trade IDs or independent ledger refids support the link.
    legacy_history_sources = {
        "QUERY_TRADES_LEDGER_FIELD_MISSING",
        "QUERY_TRADES_LOOKUP_PENDING",
    }
    for fill in persistence.fills():
        trade_id = str(fill.get("trade_id", "")).strip()
        row = _trade_row_from_fill(fill)
        if not trade_id or row is None or _valid_ledger_references(row.get("ledgers")):
            continue
        evidence = row.get("_ledger_reference_evidence")
        if isinstance(evidence, dict) and evidence.get("source") in legacy_history_sources:
            history_window_trade_rows.setdefault(
                trade_id, _trade_id_fingerprint(trade_id)
            )

    history_window_items = list(history_window_trade_rows.items())[:max_trade_queries]
    for trade_id, fingerprint in history_window_items:
        current_fill = next(
            (
                item for item in persistence.fills()
                if str(item.get("trade_id", "")).strip() == trade_id
            ),
            None,
        )
        current_row = _trade_row_from_fill(current_fill) if current_fill is not None else None
        if current_row is None or _valid_ledger_references(current_row.get("ledgers")):
            continue

        current_evidence = current_row.get("_ledger_reference_evidence")
        previous_attempts = (
            _count(current_evidence.get("attempt_count"))
            if isinstance(current_evidence, dict) else None
        )
        attempt_count = (previous_attempts or 0) + 1
        parsed_time = _timestamp(current_row.get("time"))
        target_epoch = parsed_time[0] if parsed_time is not None else None
        pending_evidence = {
            "source": "TRADES_HISTORY_WINDOW_LOOKUP_PENDING",
            "attempt_count": attempt_count,
            "last_attempt_epoch": now_epoch,
            "retry_after_epoch": now_epoch + QUERY_TRADES_MISSING_LEDGER_RETRY_SECONDS,
            "last_attempt_outcome": "QUERY_IN_PROGRESS",
            "window_start_epoch": max(0, target_epoch - 5) if target_epoch is not None else None,
            "window_end_epoch": target_epoch + 5 if target_epoch is not None else None,
        }
        _save_trade(
            trade_id,
            {**current_row, "_ledger_reference_evidence": pending_evidence},
            persistence,
        )
        trade_rows_history_window_queried += 1
        matched_row, pages, outcome, window_start, window_end = _lookup_trade_history_window(
            client=client,
            trade_id=trade_id,
            existing_row=current_row,
        )
        trade_rows_history_window_pages += pages

        if outcome.startswith("API_ERROR:") or outcome in {
            "RESPONSE_INVALID", "TRADES_SHAPE_INVALID", "CURSOR_INVALID", "CURSOR_REPEATED",
        }:
            error_code = outcome.split(":", 1)[1] if ":" in outcome else outcome
            api_errors.append("TRADES_HISTORY_WINDOW_FAILED:" + error_code)
            retry_evidence = {
                **pending_evidence,
                "source": "TRADES_HISTORY_WINDOW_LOOKUP_FAILED",
                "last_attempt_outcome": outcome,
                "retry_after_epoch": now_epoch + 300,
                "window_start_epoch": window_start,
                "window_end_epoch": window_end,
                "pages_checked": pages,
            }
            _save_trade(
                trade_id,
                {**current_row, "_ledger_reference_evidence": retry_evidence},
                persistence,
            )
            trade_rows_history_window_invalid += 1
            query_outcomes.append({
                "trade_id_fingerprint": fingerprint,
                "outcome": "TRADES_HISTORY_WINDOW_" + outcome.split(":", 1)[0],
            })
            continue

        if matched_row is None:
            source = (
                "TRADES_HISTORY_WINDOW_PAGE_BUDGET_EXCEEDED"
                if outcome == "PAGE_BUDGET_EXCEEDED"
                else "TRADES_HISTORY_WINDOW_TRADE_ID_NOT_FOUND"
            )
            retry_evidence = {
                **pending_evidence,
                "source": source,
                "last_attempt_outcome": outcome,
                "retry_after_epoch": now_epoch + QUERY_TRADES_MISSING_LEDGER_RETRY_SECONDS,
                "window_start_epoch": window_start,
                "window_end_epoch": window_end,
                "pages_checked": pages,
            }
            _save_trade(
                trade_id,
                {**current_row, "_ledger_reference_evidence": retry_evidence},
                persistence,
            )
            trade_rows_history_window_not_found += 1
            query_outcomes.append({
                "trade_id_fingerprint": fingerprint,
                "outcome": "TRADES_HISTORY_WINDOW_" + outcome,
            })
            continue

        validation_error = _trade_validation_error(trade_id, matched_row)
        if validation_error is not None:
            retry_evidence = {
                **pending_evidence,
                "source": "TRADES_HISTORY_WINDOW_LOOKUP_FAILED",
                "last_attempt_outcome": "TRADE_ROW_INVALID:" + validation_error,
                "retry_after_epoch": now_epoch + QUERY_TRADES_MISSING_LEDGER_RETRY_SECONDS,
                "window_start_epoch": window_start,
                "window_end_epoch": window_end,
                "pages_checked": pages,
            }
            _save_trade(
                trade_id,
                {**current_row, "_ledger_reference_evidence": retry_evidence},
                persistence,
            )
            trade_rows_history_window_invalid += 1
            query_outcomes.append({
                "trade_id_fingerprint": fingerprint,
                "outcome": "TRADES_HISTORY_WINDOW_ROW_INVALID:" + validation_error,
            })
            continue

        if "ledgers" not in matched_row:
            retry_evidence = {
                **pending_evidence,
                "source": "TRADES_HISTORY_WINDOW_LEDGER_FIELD_MISSING",
                "ledgers_field_present": False,
                "last_attempt_outcome": "LEDGER_FIELD_MISSING",
                "retry_after_epoch": now_epoch + QUERY_TRADES_MISSING_LEDGER_RETRY_SECONDS,
                "window_start_epoch": window_start,
                "window_end_epoch": window_end,
                "pages_checked": pages,
            }
            _save_trade(
                trade_id,
                {**matched_row, "_ledger_reference_evidence": retry_evidence},
                persistence,
            )
            trade_rows_history_window_ledger_field_missing += 1
            query_outcomes.append({
                "trade_id_fingerprint": fingerprint,
                "outcome": "TRADES_HISTORY_WINDOW_LEDGER_FIELD_MISSING",
            })
            continue

        refs = _valid_ledger_references(matched_row.get("ledgers"))
        if refs is None:
            retry_evidence = {
                **pending_evidence,
                "source": "TRADES_HISTORY_WINDOW_LOOKUP_FAILED",
                "ledgers_field_present": True,
                "last_attempt_outcome": "LEDGER_FIELD_INVALID",
                "retry_after_epoch": now_epoch + QUERY_TRADES_MISSING_LEDGER_RETRY_SECONDS,
                "window_start_epoch": window_start,
                "window_end_epoch": window_end,
                "pages_checked": pages,
            }
            _save_trade(
                trade_id,
                {**current_row, "_ledger_reference_evidence": retry_evidence},
                persistence,
            )
            trade_rows_history_window_invalid += 1
            query_outcomes.append({
                "trade_id_fingerprint": fingerprint,
                "outcome": "TRADES_HISTORY_WINDOW_LEDGER_FIELD_INVALID",
            })
            continue

        if not refs:
            empty_row = {
                **matched_row,
                "_ledger_reference_evidence": {
                    **pending_evidence,
                    "source": "TRADES_HISTORY_WINDOW_EMPTY_PENDING_LEDGER_CHECK",
                    "ledgers_field_present": True,
                    "ledgers_count": 0,
                    "last_attempt_outcome": "EMPTY_LEDGER_LIST_REQUIRES_INDEPENDENT_CHECK",
                    "window_start_epoch": window_start,
                    "window_end_epoch": window_end,
                    "pages_checked": pages,
                },
            }
            if _save_trade(trade_id, empty_row, persistence):
                trade_rows_history_window_empty_ledgers += 1
                query_outcomes.append({
                    "trade_id_fingerprint": fingerprint,
                    "outcome": "TRADES_HISTORY_WINDOW_EMPTY_LEDGER_LIST",
                })
            else:
                trade_rows_history_window_invalid += 1
                query_outcomes.append({
                    "trade_id_fingerprint": fingerprint,
                    "outcome": "TRADES_HISTORY_WINDOW_ROW_NOT_SAVED",
                })
            continue

        recovered_row = dict(matched_row)
        recovered_row.pop("_ledger_reference_evidence", None)
        if not _save_trade(trade_id, recovered_row, persistence):
            trade_rows_history_window_invalid += 1
            query_outcomes.append({
                "trade_id_fingerprint": fingerprint,
                "outcome": "TRADES_HISTORY_WINDOW_ROW_NOT_SAVED",
            })
            continue
        trade_rows_history_window_recovered += 1
        trade_rows_recovered += 1
        resolved_fingerprints[fingerprint] = "TRADES_HISTORY_WINDOW"
        query_outcomes.append({
            "trade_id_fingerprint": fingerprint,
            "outcome": "TRADES_HISTORY_WINDOW_LEDGER_LINK_RECOVERED",
        })

    # Read trade references only from the nested authoritative exchange payload.
    referenced_ledger_ids: set[str] = set()
    for fill in persistence.fills():
        row = _trade_row_from_fill(fill)
        if row is None:
            continue
        refs = _valid_ledger_references(row.get("ledgers"))
        if refs:
            referenced_ledger_ids.update(refs)

    # Kraken caps QueryLedgers at 20 IDs per call. These targeted lookups can
    # repair ledger rows even while the wider full history is being paginated.
    initial_ledger_events = persistence.ledger_events()
    existing_ledger_ids = {
        str(item.get("ledger_id", "")).strip()
        for item in initial_ledger_events
        if str(item.get("ledger_id", "")).strip()
    }
    ledger_state_before_lookup = persistence.history_sync_state("LEDGERS") or ledger_state
    ledger_source_empty_before_repair = (
        bool(initial_fills)
        and not existing_ledger_ids
        and ledger_state_before_lookup.get("initial_backfill_complete") is True
        and ledger_state_before_lookup.get("generation_complete") is True
        and ledger_state_before_lookup.get("last_error") is None
    )
    missing_ledger_ids = sorted(referenced_ledger_ids - existing_ledger_ids)
    ledger_ids_to_query = missing_ledger_ids[:max_ledger_queries]
    for batch_start in range(0, len(ledger_ids_to_query), 20):
        batch = ledger_ids_to_query[batch_start:batch_start + 20]
        ledger_ids_requested += len(batch)
        try:
            response = client.query_ledgers_info(batch)
        except Exception as exc:
            api_errors.append("QUERY_LEDGERS_FAILED:" + type(exc).__name__)
            break
        if not isinstance(response, dict):
            api_errors.append("QUERY_LEDGERS_INVALID_RESPONSE")
            break
        for ledger_id in batch:
            row = response.get(ledger_id)
            if isinstance(row, dict) and _save_ledger(ledger_id, row, persistence):
                ledger_rows_recovered += 1

    # An empty finished ledger generation contradicts persisted fills. Restart it
    # safely instead of accepting count=0 or an empty cursor as complete coverage.
    ledger_state = persistence.history_sync_state("LEDGERS") or ledger_state
    full_ledger_window = (
        ledger_state.get("initial_backfill_complete") is True
        and ledger_state.get("generation_complete") is True
        and ledger_state.get("last_error") is None
    )
    all_ledger_events = persistence.ledger_events()
    final_ledger_ids = {
        str(item.get("ledger_id", "")).strip()
        for item in all_ledger_events
        if str(item.get("ledger_id", "")).strip()
    }
    empty_ledger_gap = "LEDGER_HISTORY_COVERAGE_INCOMPLETE:EMPTY_FULL_BACKFILL"
    empty_full_ledger_conflict = (
        bool(initial_fills) and full_ledger_window and not final_ledger_ids
    ) or ledger_source_empty_before_repair

    # When a full ledger backfill actually contains rows, use Kraken's explicit
    # ledger refid relation to repair trades where QueryTrades omits the ledgers field.
    if full_ledger_window and final_ledger_ids and not empty_full_ledger_conflict:
        ledger_ids_by_trade: dict[str, set[str]] = {}
        for event in all_ledger_events:
            ledger_id = str(event.get("ledger_id", "")).strip()
            row = _ledger_row_from_event(event)
            refid = str(row.get("refid", "")).strip() if row is not None else ""
            if ledger_id and refid:
                ledger_ids_by_trade.setdefault(refid, set()).add(ledger_id)

        can_confirm_empty_relations = (
            trade_history_complete
            and ledger_history_complete
            and full_ledger_window
            and not empty_full_ledger_conflict
            and not ledger_gaps
            and bool(final_ledger_ids)
        )
        for fill in persistence.fills():
            trade_id = str(fill.get("trade_id", "")).strip()
            row = _trade_row_from_fill(fill)
            if not trade_id or row is None:
                continue
            fingerprint = _trade_id_fingerprint(trade_id)
            refs = _valid_ledger_references(row.get("ledgers"))
            if refs:
                continue
            related_ids = sorted(ledger_ids_by_trade.get(trade_id, set()))
            order_txid = str(row.get("ordertxid", "")).strip()
            matching_order_refid_ids = sorted(ledger_ids_by_trade.get(order_txid, set()))
            evidence = row.get("_ledger_reference_evidence")
            evidence_source = (
                evidence.get("source") if isinstance(evidence, dict) else None
            )
            query_explicit_empty = (
                isinstance(evidence, dict)
                and evidence.get("source") in {
                    "QUERY_TRADES_EMPTY_PENDING_LEDGER_CHECK",
                    "QUERY_TRADES_EMPTY_CONFIRMED",
                    "TRADES_HISTORY_WINDOW_EMPTY_PENDING_LEDGER_CHECK",
                    "TRADE_HISTORY_EMPTY_CONFIRMED",
                }
                and evidence.get("ledgers_field_present") is True
                and evidence.get("ledgers_count") == 0
            )

            if query_explicit_empty:
                conflict_outcome = None
                if related_ids:
                    conflict_outcome = "QUERY_TRADES_EMPTY_BUT_LEDGER_REFID_MATCHES"
                elif matching_order_refid_ids:
                    # A refid that matches the order ID may cover multiple partial
                    # fills. It is not safe to assign those ledger IDs to one fill,
                    # but its existence contradicts an asserted empty relation.
                    conflict_outcome = "QUERY_TRADES_EMPTY_BUT_LEDGER_ORDER_REFID_MATCHES"
                if conflict_outcome is not None:
                    conflicting_empty_fingerprints.add(fingerprint)
                    confirmed_empty_fingerprints.discard(fingerprint)
                    trade_gaps.add("TRADE_LEDGER_REFERENCE_SOURCE_CONFLICT:" + fingerprint)
                    query_outcomes.append({
                        "trade_id_fingerprint": fingerprint,
                        "outcome": conflict_outcome,
                    })
                    continue
                if can_confirm_empty_relations:
                    already_confirmed = evidence_source == "QUERY_TRADES_EMPTY_CONFIRMED"
                    confirmed_source = (
                        "TRADE_HISTORY_EMPTY_CONFIRMED"
                        if evidence_source == "TRADES_HISTORY_WINDOW_EMPTY_PENDING_LEDGER_CHECK"
                        else "QUERY_TRADES_EMPTY_CONFIRMED"
                    )
                    confirmed_row = {
                        **row,
                        "_ledger_reference_evidence": {
                            "source": confirmed_source,
                            "ledgers_field_present": True,
                            "ledgers_count": 0,
                            "full_ledger_history_checked": True,
                            "matching_ledger_refid_count": 0,
                        },
                    }
                    if _save_trade(trade_id, confirmed_row, persistence):
                        trade_gaps.discard("TRADE_LEDGER_REFERENCES_MISSING:" + fingerprint)
                        trade_gaps.discard("TRADE_LEDGER_REFERENCE_SOURCE_CONFLICT:" + fingerprint)
                        conflicting_empty_fingerprints.discard(fingerprint)
                        confirmed_empty_fingerprints.add(fingerprint)
                        if not already_confirmed:
                            resolved_fingerprints[fingerprint] = "QUERY_TRADES_EMPTY_CONFIRMED"
                    continue

            if related_ids:
                derived_row = {
                    **row,
                    "ledgers": related_ids,
                    "_ledger_reference_evidence": {
                        "source": "FULL_LEDGER_REFID_MATCH",
                        "verified_by_exchange_refid": True,
                    },
                }
                if _trade_validation_error(trade_id, derived_row) is None and _save_trade(
                    trade_id, derived_row, persistence
                ):
                    trade_gaps.discard("TRADE_LEDGER_REFERENCES_MISSING:" + fingerprint)
                    trade_gaps.discard("TRADE_LEDGER_REFERENCE_SOURCE_CONFLICT:" + fingerprint)
                    conflicting_empty_fingerprints.discard(fingerprint)
                    resolved_fingerprints[fingerprint] = "LEDGER_REFID_MATCH"
                    trade_rows_recovered += 1

    # Re-evaluate from persistence after targeted queries and derived refid matching.
    final_fills = persistence.fills()
    expected_ledger_ids: set[str] = set()
    missing_trade_fingerprints: set[str] = set()
    for fill in final_fills:
        trade_id = str(fill.get("trade_id", "")).strip()
        row = _trade_row_from_fill(fill)
        if not trade_id or row is None:
            continue
        refs = _valid_ledger_references(row.get("ledgers"))
        fingerprint = _trade_id_fingerprint(trade_id)
        gap_token = "TRADE_LEDGER_REFERENCES_MISSING:" + fingerprint
        conflict_token = "TRADE_LEDGER_REFERENCE_SOURCE_CONFLICT:" + fingerprint
        if refs:
            trade_gaps.discard(gap_token)
            trade_gaps.discard(conflict_token)
            expected_ledger_ids.update(refs)
        elif fingerprint in confirmed_empty_fingerprints and fingerprint not in conflicting_empty_fingerprints:
            trade_gaps.discard(gap_token)
            trade_gaps.discard(conflict_token)
        else:
            missing_trade_fingerprints.add(fingerprint)
            trade_gaps.add(gap_token)
            if fingerprint in conflicting_empty_fingerprints:
                trade_gaps.add(conflict_token)

    all_ledger_events = persistence.ledger_events()
    final_ledger_ids = {
        str(item.get("ledger_id", "")).strip()
        for item in all_ledger_events
        if str(item.get("ledger_id", "")).strip()
    }
    missing_ledger_references = sorted(expected_ledger_ids - final_ledger_ids)
    coverage_gap = "TRADE_LEDGER_COVERAGE_INCOMPLETE:ALL_TRADES"
    if (
        missing_trade_fingerprints
        or missing_ledger_references
        or api_errors
        or conflicting_empty_fingerprints
    ):
        trade_gaps.add(coverage_gap)
    else:
        trade_gaps.discard(coverage_gap)

    if empty_full_ledger_conflict:
        ledger_gaps.add(empty_ledger_gap)
        ledger_state.update({
            "start_epoch": 0,
            "end_epoch": int(time.time()),
            "next_offset": 0,
            "next_cursor": None,
            "expected_count": None,
            "initial_backfill_complete": False,
            "generation_complete": False,
            "last_error": "LEDGER_HISTORY_COVERAGE_CONFLICT",
        })
    elif full_ledger_window and final_ledger_ids:
        ledger_gaps.discard(empty_ledger_gap)

    trade_state["quality_gaps"] = sorted(trade_gaps)
    ledger_state["quality_gaps"] = sorted(ledger_gaps)
    persistence.save_history_sync_state(trade_state)
    persistence.save_history_sync_state(ledger_state)

    for fingerprint, resolution_source in sorted(resolved_fingerprints.items()):
        reason = (
            "TRADE_LEDGER_EMPTY_RELATION_CONFIRMED"
            if resolution_source == "QUERY_TRADES_EMPTY_CONFIRMED"
            else "TRADE_LEDGER_REFERENCES_RECOVERED"
        )
        persistence.record_event("KRAKEN_HISTORY_DATA_QUALITY_GAP_RESOLVED", {
            "source": "TRADES",
            "reason": reason,
            "trade_id_fingerprint": fingerprint,
            "resolution_source": resolution_source,
            "new_risk_allowed": False,
        })

    final_trade_gaps = trade_state["quality_gaps"]
    final_ledger_gaps = ledger_state["quality_gaps"]
    missing_trade_references = len(missing_trade_fingerprints)
    ledger_full_backfill_conflict = empty_ledger_gap in final_ledger_gaps
    complete = (
        missing_trade_references == 0
        and not missing_ledger_references
        and not ledger_full_backfill_conflict
        and not api_errors
        and not final_trade_gaps
        and not final_ledger_gaps
    )
    pending_query_retry_epochs: list[int] = []
    pending_history_retry_epochs: list[int] = []
    history_retry_sources = {
        "TRADES_HISTORY_WINDOW_LOOKUP_PENDING",
        "TRADES_HISTORY_WINDOW_LEDGER_FIELD_MISSING",
        "TRADES_HISTORY_WINDOW_TRADE_ID_NOT_FOUND",
        "TRADES_HISTORY_WINDOW_LOOKUP_FAILED",
        "TRADES_HISTORY_WINDOW_PAGE_BUDGET_EXCEEDED",
    }
    for fill in final_fills:
        row = _trade_row_from_fill(fill)
        evidence = row.get("_ledger_reference_evidence") if row is not None else None
        if not isinstance(evidence, dict):
            continue
        source = evidence.get("source")
        retry_after = _count(evidence.get("retry_after_epoch"))
        if retry_after is None or retry_after <= now_epoch:
            continue
        if source in {"QUERY_TRADES_LEDGER_FIELD_MISSING", "QUERY_TRADES_LOOKUP_PENDING"}:
            pending_query_retry_epochs.append(retry_after)
        elif source in history_retry_sources:
            pending_history_retry_epochs.append(retry_after)
    next_trade_query_retry_epoch = (
        min(pending_query_retry_epochs) if pending_query_retry_epochs else None
    )
    next_trade_history_retry_epoch = (
        min(pending_history_retry_epochs) if pending_history_retry_epochs else None
    )

    return {
        "status": "COMPLETE" if complete else "INCOMPLETE",
        "trade_rows_queried": trade_rows_queried,
        "trade_rows_query_deferred": trade_rows_query_deferred,
        "next_trade_query_retry_epoch": next_trade_query_retry_epoch,
        "trade_rows_history_window_queried": trade_rows_history_window_queried,
        "trade_rows_history_window_pages": trade_rows_history_window_pages,
        "trade_rows_history_window_recovered": trade_rows_history_window_recovered,
        "trade_rows_history_window_ledger_field_missing": trade_rows_history_window_ledger_field_missing,
        "trade_rows_history_window_not_found": trade_rows_history_window_not_found,
        "trade_rows_history_window_empty_ledgers": trade_rows_history_window_empty_ledgers,
        "trade_rows_history_window_invalid": trade_rows_history_window_invalid,
        "trade_rows_history_window_deferred": trade_rows_history_window_deferred,
        "next_trade_history_retry_epoch": next_trade_history_retry_epoch,
        "trade_rows_recovered": trade_rows_recovered,
        "trade_rows_query_empty_ledgers": trade_rows_query_empty_ledgers,
        "trade_rows_query_missing": trade_rows_query_missing,
        "trade_rows_query_ledger_field_missing": trade_rows_query_ledger_field_missing,
        "trade_rows_query_invalid": trade_rows_query_invalid,
        "trade_rows_confirmed_empty": len(confirmed_empty_fingerprints),
        "trade_query_outcomes": query_outcomes,
        "ledger_ids_referenced": len(expected_ledger_ids),
        "ledger_ids_requested": ledger_ids_requested,
        "ledger_rows_recovered": ledger_rows_recovered,
        "missing_trade_references": missing_trade_references,
        "missing_ledger_references": len(missing_ledger_references),
        "ledger_full_backfill_conflict": ledger_full_backfill_conflict,
        "trade_quality_gap_count": len(final_trade_gaps),
        "ledger_quality_gap_count": len(final_ledger_gaps),
        "api_errors": sorted(set(api_errors)),
    }

def sync_history_source(
    *,
    client: Any,
    persistence: Any,
    source: str,
    page_budget: int = PAGE_BUDGET_PER_SOURCE,
    now_epoch: int | None = None,
    force_refresh: bool = False,
    start_epoch_override: int | None = None,
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
            "last_error": None, "quality_gaps": [],
        }
        persistence.save_history_sync_state(state)
    elif state["generation_complete"] and (
        force_refresh or now - int(state["end_epoch"]) >= REFRESH_INTERVAL_SECONDS
    ):
        previous_end = int(state["end_epoch"])
        requested_start = (
            max(0, int(start_epoch_override) - 1)
            if force_refresh and start_epoch_override is not None
            else max(0, previous_end - REFRESH_OVERLAP_SECONDS)
        )
        state = {
            "source": normalized,
            "start_epoch": requested_start,
            "end_epoch": now, "next_offset": 0, "expected_count": None,
            "initial_backfill_complete": bool(state["initial_backfill_complete"]),
            "generation_complete": False, "last_error": None,
            "quality_gaps": list(state.get("quality_gaps", [])),
        }
        persistence.save_history_sync_state(state)
    elif state["generation_complete"]:
        return {
            "source": normalized, "pages": 0, "items_saved": 0,
            "expected_count": state["expected_count"], "next_offset": state["next_offset"],
            "generation_complete": True,
            "initial_backfill_complete": bool(state["initial_backfill_complete"]),
            "cached_until_refresh": True, "last_error": None,
            **_quality_gap_fields(state),
        }

    cursor_source = normalized in {"TRADES", "LEDGERS", "CLOSED_ORDERS"}
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
            response = client.ledgers(
                offset=0, start=start, end=end, with_cursor=True, cursor=request_cursor
            )
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
            invalid_reason = (
                _trade_validation_error(row_id, row) if normalized == "TRADES" else None
            )
            if normalized == "TRADES":
                valid = invalid_reason is None and _save_trade(row_id, row, persistence)
            elif normalized == "LEDGERS":
                valid = _save_ledger(row_id, row, persistence)
            else:
                valid = _save_closed_order(row_id, row, persistence)
            if not valid:
                invalid_reason = invalid_reason or "HISTORY_ITEM_VALIDATION_FAILED"
                state["last_error"] = (
                    "HISTORY_ITEM_INVALID:" + invalid_reason
                    if normalized == "TRADES" else "HISTORY_ITEM_INVALID"
                )
                persistence.save_history_sync_state(state)
                failure = {
                    "source": normalized, "pages": pages + 1, "items_saved": saved,
                    "expected_count": state["expected_count"], "next_offset": state["next_offset"],
                    "generation_complete": False,
                    "initial_backfill_complete": bool(state["initial_backfill_complete"]),
                    "cached_until_refresh": False, "last_error": state["last_error"],
                    "invalid_item_reason": invalid_reason,
                }
                if normalized == "TRADES":
                    failure["invalid_item_id_fingerprint"] = _trade_id_fingerprint(row_id)
                failure.update(_quality_gap_fields(state))
                return failure

            if normalized == "TRADES":
                fingerprint = _trade_id_fingerprint(row_id)
                gap_token = "TRADE_LEDGER_REFERENCES_MISSING:" + fingerprint
                quality_gaps = list(state.get("quality_gaps", []))
                if "ledgers" not in row:
                    if gap_token not in quality_gaps:
                        quality_gaps.append(gap_token)
                        persistence.record_event("KRAKEN_HISTORY_DATA_QUALITY_GAP", {
                            "source": "TRADES",
                            "reason": "TRADE_LEDGER_REFERENCES_MISSING",
                            "trade_id_fingerprint": fingerprint,
                            "new_risk_allowed": False,
                        })
                else:
                    quality_gaps = [gap for gap in quality_gaps if gap != gap_token]
                state["quality_gaps"] = sorted(set(quality_gaps))
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
        **_quality_gap_fields(state),
    }
