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


def test_opening_order_with_multiple_fills_is_aggregated_against_position_volume():
    db = Persistence(":memory:")
    db.record_account_snapshot(_snapshot(_position(), position_id="POSITION-TXID"))
    _add_order(db)
    _add_fill(db, trade_id="TRADE-1", volume="0.4", ledgers=["L1"])
    _add_fill(db, trade_id="TRADE-2", volume="0.6", ledgers=["L2"])
    db.record_ledger({
        "ledger_id": "L1", "asset": "USDC", "amount": "-40",
        "fee": "0", "event_type": "margin", "ts": "2026-10-16T05:00:00+00:00",
        "verified": True, "raw": {"refid": "POSITION-TXID"},
    })
    db.record_ledger({
        "ledger_id": "L2", "asset": "USDC", "amount": "-60",
        "fee": "0", "event_type": "margin", "ts": "2026-10-16T05:00:01+00:00",
        "verified": True, "raw": {"refid": "POSITION-TXID"},
    })
    _mark_history_complete(db)

    result = reconcile_positions_to_fills(db)

    assert result["positions_matched_to_fills"] == 1
    assert result["opening_fills_aggregated"] == 2
    assert result["related_ledgers_checked"] == 2
    assert result["missing_related_ledgers"] == 0
    assert not db.circuit_breakers()
    db.close()


def test_partial_closed_volume_cannot_be_claimed_reconciled_without_lot_linked_close_fill():
    db = Persistence(":memory:")
    db.record_account_snapshot(_snapshot(_position(vol_closed="0.25")))
    _add_order(db)
    _add_fill(db, ledgers=["L1"])
    db.record_ledger({
        "ledger_id": "L1", "asset": "USDC", "amount": "-100",
        "fee": "0", "event_type": "margin", "ts": "2026-10-16T05:00:00+00:00",
        "verified": True, "raw": {"refid": "P1"},
    })
    _mark_history_complete(db)

    result = reconcile_positions_to_fills(db)

    assert result["positions_matched_to_fills"] == 1
    assert result["closing_volume_unmapped"] == 1
    assert result["close_fill_reconciliation_complete"] is False
    assert result["position_fill_reconciliation_complete"] is False
    assert any(item["reason"] == "CLOSED_VOLUME_HAS_NO_POSITION_LINKED_CLOSE_FILL"
               for item in db.circuit_breakers())
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


def test_wallet_balances_reconcile_against_latest_ledger_balance():
    from app.portfolio_reconciliation import reconcile_wallet_balance_deltas

    previous = {"captured_at": "100", "balance": {"USDC": "10.0", "XXBT": "0.5"}}
    current = {"captured_at": "120", "balance": {"USDC": "9.5", "XXBT": "0.5"}}
    ledgers = [{
        "ledger_id": "L1", "asset": "USDC",
        "raw": {"raw": {"time": 110, "balance": "9.5"}},
    }]
    result = reconcile_wallet_balance_deltas(
        previous, current, ledgers, ledger_history_complete=True
    )
    assert result["complete"] is True
    assert result["full_portfolio_reconciliation"] is False
    assert result["assets_matched_to_latest_ledger_balance"] == 1
    assert result["assets_stable_without_ledger_activity"] == 1
    assert result["mismatches"] == 0


def test_wallet_balance_change_without_a_ledger_entry_fails_closed():
    from app.portfolio_reconciliation import reconcile_wallet_balance_deltas

    previous = {"captured_at": "100", "balance": {"USDC": "10"}}
    current = {"captured_at": "120", "balance": {"USDC": "9.5"}}
    result = reconcile_wallet_balance_deltas(
        previous, current, [], ledger_history_complete=True
    )
    assert result["complete"] is False
    assert result["mismatches"] == 1
    assert result["asset_results"][0]["status"] == "BALANCE_CHANGED_WITHOUT_LEDGER_EVIDENCE"


def test_wallet_reconciliation_fails_closed_on_conflicting_same_time_ledger_balances():
    from app.portfolio_reconciliation import reconcile_wallet_balance_deltas

    previous = {"captured_at": "100", "balance": {"USDC": "10"}}
    current = {"captured_at": "120", "balance": {"USDC": "9.5"}}
    ledgers = [
        {"ledger_id": "L1", "asset": "USDC", "raw": {"raw": {"time": 110, "balance": "9.5"}}},
        {"ledger_id": "L2", "asset": "USDC", "raw": {"raw": {"time": 110, "balance": "9.4"}}},
    ]
    result = reconcile_wallet_balance_deltas(
        previous, current, ledgers, ledger_history_complete=True
    )
    assert result["complete"] is False
    assert result["asset_results"][0]["status"] == "LATEST_LEDGER_ORDERING_AMBIGUOUS"


def test_wallet_reconciliation_never_claims_success_without_baseline_or_complete_history():
    from app.portfolio_reconciliation import reconcile_wallet_balance_deltas

    current = {"captured_at": "120", "balance": {"USDC": "9.5"}}
    assert reconcile_wallet_balance_deltas(
        None, current, [], ledger_history_complete=True
    )["status"] == "BASELINE_REQUIRED"
    incomplete = reconcile_wallet_balance_deltas(
        {"captured_at": "100", "balance": {"USDC": "10"}},
        current, [], ledger_history_complete=False,
    )
    assert incomplete["complete"] is False
    assert incomplete["status"] == "LEDGER_WINDOW_INCOMPLETE"


def test_opening_fill_aggregate_must_match_position_and_exchange_executed_volume():
    db = Persistence(":memory:")
    db.record_account_snapshot(_snapshot(_position(), position_id="POSITION-TXID"))
    _add_order(db)
    _add_fill(db, trade_id="TRADE-1", volume="0.4", ledgers=["L1"])
    _add_fill(db, trade_id="TRADE-2", volume="0.4", ledgers=["L2"])
    _mark_history_complete(db)

    result = reconcile_positions_to_fills(db)

    assert result["positions_matched_to_fills"] == 0
    assert result["fill_field_mismatches"] == 1
    assert result["position_fill_reconciliation_complete"] is False
    assert any(
        item["reason"] == "POSITION_FILL_FIELDS_MISMATCH"
        for item in db.circuit_breakers()
    )
    db.close()
