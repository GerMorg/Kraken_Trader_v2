from __future__ import annotations

import time

from app.persistence import Persistence
from app.portfolio_reconciliation import reconcile_positions_to_fills


def _snapshot(position: dict | None, *, position_id: str = "P1") -> dict:
    positions = {position_id: position} if position else {}
    return {
        "captured_at": time.time(),
        "balance": {"ZUSD": "500"},
        "trade_balance": {"e": "500", "mf": "400", "m": "100", "ml": "500"},
        "open_orders": {"open": {}},
        "closed_orders": {"closed": {}},
        "open_positions": positions,
    }


def _position(**overrides):
    return {
        "ordertxid": "O1",
        "pair": "XXBTZUSD",
        "type": "buy",
        "posstatus": "open",
        "vol": "1",
        "vol_closed": "0",
        "cost": "500",
        "fee": "2",
        "margin": "100",
        "value": "520",
        "net": "18",
        "time": 1760590000,
        "rollovertm": 1760600000,
        "terms": "0.01% per 4 hours",
        **overrides,
    }


def _mark_history_complete(db: Persistence) -> None:
    for source in ("TRADES", "LEDGERS", "CLOSED_ORDERS"):
        db.save_history_sync_state({
            "source": source,
            "start_epoch": 0,
            "end_epoch": int(time.time()),
            "next_offset": 1,
            "next_cursor": None,
            "expected_count": 1,
            "initial_backfill_complete": True,
            "generation_complete": True,
            "last_error": None,
        })


def _add_fill(db: Persistence, *, trade_id="P1", volume="1", side="buy", pair="XXBTZUSD",
              margin="100", ledgers=None):
    db.record_fill({
        "trade_id": trade_id,
        "order_id": "O1",
        "pair": pair,
        "side": side,
        "volume": volume,
        "price": "500",
        "fee": "2",
        "fee_asset": None,
        "ts": "2026-10-16T05:00:00+00:00",
        "verified": True,
        "raw": {"margin": margin, "ledgers": ledgers or []},
    })


def _add_order(db: Persistence):
    db.record_exchange_order("O1", {
        "status": "closed",
        "vol": "1",
        "vol_exec": "1",
        "cl_ord_id": "C1",
        "descr": {"pair": "XXBTZUSD", "type": "buy", "ordertype": "limit"},
    }, source="CLOSED")


def test_open_position_is_reconciled_to_exact_trade_order_and_related_ledger():
    db = Persistence(":memory:")
    db.record_account_snapshot(_snapshot(_position()))
    _add_order(db)
    _add_fill(db, ledgers=["L1"])
    db.record_ledger({
        "ledger_id": "L1", "asset": "USDC", "amount": "-100",
        "fee": "0", "event_type": "margin", "ts": "2026-10-16T05:00:00+00:00",
        "verified": True, "raw": {"refid": "P1"},
    })
    _mark_history_complete(db)

    result = reconcile_positions_to_fills(db)

    assert result["status"] == "POSITION_FILL_MATCHED"
    assert result["positions_checked"] == 1
    assert result["positions_matched_to_fills"] == 1
    assert result["opening_orders_matched"] == 1
    assert result["related_ledgers_checked"] == 1
    assert result["missing_related_ledgers"] == 0
    assert result["position_fill_reconciliation_complete"] is True
    assert result["full_portfolio_reconciliation"] is False
    assert not db.circuit_breakers()
    db.close()


def test_position_key_is_not_assumed_to_be_trade_id_when_order_match_is_unique():
    db = Persistence(":memory:")
    db.record_account_snapshot(_snapshot(_position(), position_id="POSITION-TXID"))
    _add_order(db)
    _add_fill(db, trade_id="TRADE-TXID", ledgers=["L1"])
    db.record_ledger({
        "ledger_id": "L1", "asset": "USDC", "amount": "-100",
        "fee": "0", "event_type": "margin", "ts": "2026-10-16T05:00:00+00:00",
        "verified": True, "raw": {"refid": "POSITION-TXID"},
    })
    _mark_history_complete(db)

    result = reconcile_positions_to_fills(db)

    assert result["status"] == "POSITION_FILL_MATCHED"
    assert result["positions_matched_to_fills"] == 1
    assert result["opening_orders_matched"] == 1
    assert not db.circuit_breakers()
    db.close()


def test_one_opening_order_with_multiple_unallocated_fills_is_ambiguous():
    db = Persistence(":memory:")
    db.record_account_snapshot(_snapshot(_position(), position_id="POSITION-TXID"))
    _add_order(db)
    _add_fill(db, trade_id="TRADE-1", ledgers=["L1"])
    _add_fill(db, trade_id="TRADE-2", ledgers=["L2"])
    _mark_history_complete(db)

    result = reconcile_positions_to_fills(db)

    assert result["unmatched_position_fills"] == 1
    assert any(item["reason"] == "POSITION_OPENING_ORDER_HAS_AMBIGUOUS_FILLS"
               for item in db.circuit_breakers())
    assert result["full_portfolio_reconciliation"] is False
    db.close()


def test_position_without_trade_fill_trips_breaker_after_history_complete():
    db = Persistence(":memory:")
    db.record_account_snapshot(_snapshot(_position()))
    _add_order(db)
    _mark_history_complete(db)

    result = reconcile_positions_to_fills(db)

    assert result["status"] == "POSITION_FILL_MISMATCH"
    assert result["unmatched_position_fills"] == 1
    assert any(item["reason"] == "OPEN_POSITION_WITHOUT_MATCHING_TRADE_FILL"
               for item in db.circuit_breakers())
    assert result["full_portfolio_reconciliation"] is False
    db.close()


def test_position_fill_quantity_or_side_mismatch_trips_breaker():
    db = Persistence(":memory:")
    db.record_account_snapshot(_snapshot(_position()))
    _add_order(db)
    _add_fill(db, volume="2", side="sell")
    _mark_history_complete(db)

    result = reconcile_positions_to_fills(db)

    assert result["fill_field_mismatches"] == 1
    assert result["position_fill_reconciliation_complete"] is False
    assert any(item["reason"] == "POSITION_FILL_FIELDS_MISMATCH"
               for item in db.circuit_breakers())
    db.close()


def test_position_reconciliation_waits_for_backfill_without_false_failure():
    db = Persistence(":memory:")
    db.record_account_snapshot(_snapshot(_position()))
    result = reconcile_positions_to_fills(db)

    assert result["status"] == "HISTORY_BACKFILL_PENDING"
    assert result["pending_history_sources"] == ["TRADES", "LEDGERS", "CLOSED_ORDERS"]
    assert result["position_fill_reconciliation_complete"] is False
    assert not db.circuit_breakers()
    db.close()


def test_referenced_ledger_missing_from_history_trips_breaker():
    db = Persistence(":memory:")
    db.record_account_snapshot(_snapshot(_position()))
    _add_order(db)
    _add_fill(db, ledgers=["L-MISSING"])
    _mark_history_complete(db)

    result = reconcile_positions_to_fills(db)

    assert result["missing_related_ledgers"] == 1
    assert any(item["reason"] == "TRADE_REFERENCES_MISSING_LEDGER"
               for item in db.circuit_breakers())
    assert result["full_portfolio_reconciliation"] is False
    db.close()
