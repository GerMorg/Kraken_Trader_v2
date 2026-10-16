from __future__ import annotations

from app.history_sync import history_sources_ready, sync_history_source
from app.persistence import Persistence


class FakeHistoryClient:
    def __init__(self, trade_pages=None, ledger_pages=None, closed_pages=None):
        self.trade_pages = trade_pages or {}
        self.ledger_pages = ledger_pages or {}
        self.closed_pages = closed_pages or {}
        self.calls = []

    def trades_history(self, *, offset, start, end, with_cursor=False, cursor=None):
        self.calls.append(("TRADES", offset, start, end, with_cursor, cursor))
        return self.trade_pages[cursor] if with_cursor else self.trade_pages[offset]

    def ledgers(self, *, offset, start, end):
        self.calls.append(("LEDGERS", offset, start, end))
        return self.ledger_pages[offset]

    def closed_orders(self, *, offset, start, end, close_time, include_trades,
                      with_cursor=False, cursor=None):
        self.calls.append(("CLOSED_ORDERS", offset, start, end, close_time, include_trades,
                           with_cursor, cursor))
        return self.closed_pages[cursor] if with_cursor else self.closed_pages[offset]


def trade(trade_time=1768000000):
    return {
        "time": trade_time, "type": "buy", "pair": "XBTUSD",
        "vol": "0.01", "price": "90000", "fee": "0.20", "margin": "0.00000",
        "ordertxid": "ORDER-1", "ledgers": [],
    }


def ledger(ledger_time=1768000000):
    return {
        "time": ledger_time, "type": "rollover", "subtype": "margin",
        "asset": "USDC", "amount": "-0.15", "fee": "0",
        "balance": "99.85",
    }


def test_trade_history_backfill_resumes_across_pages_and_is_idempotent():
    db = Persistence(":memory:")
    client = FakeHistoryClient(trade_pages={
        None: {"trades": {"T1": trade(), "T2": trade()}, "cursor": {"next": "TRADES-CURSOR-2"}},
        "TRADES-CURSOR-2": {"trades": {"T3": trade()}, "cursor": {"next": None}},
    })

    first = sync_history_source(client=client, persistence=db, source="TRADES",
                                page_budget=1, now_epoch=1769000000)
    assert first["generation_complete"] is False
    assert first["initial_backfill_complete"] is False
    assert first["next_offset"] == 2

    second = sync_history_source(client=client, persistence=db, source="TRADES",
                                 page_budget=1, now_epoch=1769000000)
    assert second["generation_complete"] is True
    assert second["initial_backfill_complete"] is True
    assert second["next_offset"] == 3
    assert len(db.tax_events_for_year(2026)) == 3

    # Completed generation is cached until its refresh interval; it must not duplicate rows.
    cached = sync_history_source(client=client, persistence=db, source="TRADES",
                                 page_budget=1, now_epoch=1769000010)
    assert cached["cached_until_refresh"] is True
    assert len(db.tax_events_for_year(2026)) == 3
    assert db.intent_state("not-an-intent") is None
    db.close()


def test_ledger_history_persists_actual_asset_and_keeps_tax_value_unverified():
    db = Persistence(":memory:")
    client = FakeHistoryClient(ledger_pages={
        0: {"ledger": {"L1": ledger()}, "count": 1},
    })
    summary = sync_history_source(client=client, persistence=db, source="LEDGERS",
                                  page_budget=1, now_epoch=1769000000)
    assert summary["generation_complete"] is True
    assert summary["initial_backfill_complete"] is True
    row = db.tax_events_for_year(2026)[0]
    assert row["event_id"] == "LEDGER:L1"
    assert row["asset"] == "USDC"
    assert row["value_eur"] is None
    assert row["verified"] == 0
    assert row["manual_review"] == 1
    db.close()


def test_history_invalid_page_never_advances_cursor_or_marks_complete():
    db = Persistence(":memory:")
    client = FakeHistoryClient(trade_pages={
        None: {"trades": {"T1": {**trade(), "time": "NaN"}}, "cursor": {"next": None}},
    })
    result = sync_history_source(client=client, persistence=db, source="TRADES",
                                 page_budget=1, now_epoch=1769000000)
    assert result["generation_complete"] is False
    assert result["last_error"] == "HISTORY_ITEM_INVALID"
    assert result["next_offset"] == 0
    assert result["initial_backfill_complete"] is False
    db.close()


def test_history_count_is_required_and_invalid_response_fails_closed():
    db = Persistence(":memory:")
    client = FakeHistoryClient(ledger_pages={
        0: {"ledger": {}, "count": "NaN"},
    })
    result = sync_history_source(client=client, persistence=db, source="LEDGERS",
                                 page_budget=1, now_epoch=1769000000)
    assert result["generation_complete"] is False
    assert result["last_error"] == "HISTORY_RESPONSE_SHAPE_OR_COUNT_INVALID"
    assert result["next_offset"] == 0
    db.close()


def test_cursor_history_missing_terminal_metadata_fails_closed():
    db = Persistence(":memory:")
    client = FakeHistoryClient(trade_pages={
        None: {"trades": {"T1": trade()}},
    })
    result = sync_history_source(
        client=client, persistence=db, source="TRADES",
        page_budget=1, now_epoch=1769000000,
    )
    assert result["generation_complete"] is False
    assert result["initial_backfill_complete"] is False
    assert result["last_error"] == "HISTORY_CURSOR_SHAPE_INVALID"
    assert result["next_offset"] == 0
    assert db.fills() == []
    db.close()


def test_history_requires_known_source_and_bounded_page_budget():
    import pytest

    db = Persistence(":memory:")
    client = FakeHistoryClient()
    with pytest.raises(ValueError, match="HISTORY_SOURCE_INVALID"):
        sync_history_source(client=client, persistence=db, source="UNKNOWN_ORDERS")
    with pytest.raises(ValueError, match="HISTORY_PAGE_BUDGET_OUT_OF_RANGE"):
        sync_history_source(client=client, persistence=db, source="TRADES", page_budget=0)
    db.close()


def _closed_order(closed_time=1768000000, *, status="closed", volume="1", executed="1"):
    return {
        "closetm": closed_time,
        "status": status,
        "vol": volume,
        "vol_exec": executed,
        "cl_ord_id": "client-1",
        "descr": {"pair": "XBT/USD", "type": "buy", "ordertype": "limit"},
    }


def test_closed_orders_history_backfill_resumes_and_persists_by_order_id():
    db = Persistence(":memory:")
    client = FakeHistoryClient(closed_pages={
        None: {"closed": {"O1": _closed_order(), "O2": _closed_order()},
               "cursor": {"next": "CLOSED-CURSOR-2"}},
        "CLOSED-CURSOR-2": {"closed": {"O3": _closed_order()}, "cursor": {"next": None}},
    })

    first = sync_history_source(client=client, persistence=db, source="CLOSED_ORDERS",
                                page_budget=1, now_epoch=1769000000)
    assert first["generation_complete"] is False
    assert first["initial_backfill_complete"] is False
    assert first["next_offset"] == 2

    second = sync_history_source(client=client, persistence=db, source="CLOSED_ORDERS",
                                 page_budget=1, now_epoch=1769000000)
    assert second["generation_complete"] is True
    assert second["initial_backfill_complete"] is True
    assert second["next_offset"] == 3
    assert {row["order_id"] for row in db.exchange_orders()} == {"O1", "O2", "O3"}
    assert all(row["source"] == "CLOSED" for row in db.exchange_orders())
    assert client.calls == [
        ("CLOSED_ORDERS", 0, 0, 1769000000, "close", True, True, None),
        ("CLOSED_ORDERS", 0, 0, 1769000000, "close", True, True, "CLOSED-CURSOR-2"),
    ]
    db.close()


def test_invalid_closed_order_does_not_advance_history_cursor():
    db = Persistence(":memory:")
    client = FakeHistoryClient(closed_pages={
        None: {"closed": {"O1": _closed_order(closed_time="NaN")}, "cursor": {"next": None}},
    })
    result = sync_history_source(client=client, persistence=db, source="CLOSED_ORDERS",
                                 page_budget=1, now_epoch=1769000000)
    assert result["generation_complete"] is False
    assert result["last_error"] == "HISTORY_ITEM_INVALID"
    assert result["next_offset"] == 0
    assert db.exchange_orders() == []
    db.close()


def test_trade_history_classifies_margin_fills_separately_and_preserves_ledger_refs():
    db = Persistence(":memory:")
    margin_trade = {
        **trade(),
        "margin": "900.00",
        "ledgers": ["L-MARGIN-1", "L-FEE-1"],
    }
    client = FakeHistoryClient(trade_pages={
        None: {"trades": {"TMARGIN": margin_trade}, "cursor": {"next": None}},
    })
    result = sync_history_source(client=client, persistence=db, source="TRADES",
                                 page_budget=1, now_epoch=1769000000)
    assert result["generation_complete"] is True
    event = next(e for e in db.tax_events_for_year(2026) if e["event_id"] == "TRADE:TMARGIN")
    assert event["event_type"] == "MARGIN_TRADE_FILL"
    assert event["verified"] == 0
    assert event["manual_review"] == 1
    assert event["payload"]["product_type"] == "SPOT_MARGIN"
    assert event["payload"]["margin_amount"] == "900.00"
    assert event["payload"]["related_ledger_ids"] == ["L-MARGIN-1", "L-FEE-1"]
    db.close()


def test_trade_history_missing_ledger_reference_shape_is_not_accepted():
    db = Persistence(":memory:")
    invalid = trade()
    invalid.pop("ledgers")
    client = FakeHistoryClient(trade_pages={None: {"trades": {"T-NO-LEDGERS": invalid}, "cursor": {"next": None}}})
    result = sync_history_source(client=client, persistence=db, source="TRADES",
                                 page_budget=1, now_epoch=1769000000)
    assert result["generation_complete"] is False
    assert result["last_error"] == "HISTORY_ITEM_INVALID"
    assert db.fills() == []
    db.close()


def test_trade_history_missing_margin_field_is_incomplete_not_guessed_as_spot():
    db = Persistence(":memory:")
    missing_margin = trade()
    missing_margin.pop("margin")
    client = FakeHistoryClient(trade_pages={
        None: {"trades": {"T-MISSING-MARGIN": missing_margin}, "cursor": {"next": None}},
    })
    result = sync_history_source(client=client, persistence=db, source="TRADES",
                                 page_budget=1, now_epoch=1769000000)
    assert result["generation_complete"] is False
    assert result["last_error"] == "HISTORY_ITEM_INVALID"
    assert db.tax_events_for_year(2026) == []
    db.close()


def test_repeated_history_cursor_does_not_mark_backfill_complete():
    db = Persistence(":memory:")
    client = FakeHistoryClient(trade_pages={
        None: {"trades": {"T1": trade()}, "cursor": {"next": "CURSOR-1"}},
        "CURSOR-1": {"trades": {"T2": trade()}, "cursor": {"next": "CURSOR-1"}},
    })
    first = sync_history_source(client=client, persistence=db, source="TRADES",
                                page_budget=1, now_epoch=1769000000)
    assert first["generation_complete"] is False
    assert first["last_error"] is None
    assert db.history_sync_state("TRADES")["next_cursor"] == "CURSOR-1"

    second = sync_history_source(client=client, persistence=db, source="TRADES",
                                 page_budget=1, now_epoch=1769000000)
    assert second["generation_complete"] is False
    assert second["last_error"] == "HISTORY_CURSOR_INVALID_OR_REPEATED"
    assert db.history_sync_state("TRADES")["next_cursor"] == "CURSOR-1"
    assert db.history_sync_state("TRADES")["initial_backfill_complete"] is False
    db.close()


def test_history_refresh_corrects_fill_and_invalidates_stale_tax_valuation():
    db = Persistence(":memory:")
    first_trade = trade()
    client = FakeHistoryClient(trade_pages={
        None: {"trades": {"T-REVISED": first_trade}, "cursor": {"next": None}},
    })
    first = sync_history_source(
        client=client, persistence=db, source="TRADES", page_budget=1,
        now_epoch=1769000000,
    )
    assert first["generation_complete"] is True
    assert db.fills()[0]["price"] == "90000"

    tax_event = next(
        item for item in db.tax_events_for_year(2026)
        if item["event_id"] == "TRADE:T-REVISED"
    )
    assert db.update_tax_event({
        **tax_event, "value_eur": "8300.00", "verified": True, "manual_review": False,
        "payload": {**tax_event["payload"], "fx_rate_source": "verified-fixture"},
    })

    corrected_trade = {**first_trade, "price": "91000", "fee": "0.21"}
    client.trade_pages = {None: {"trades": {"T-REVISED": corrected_trade}, "cursor": {"next": None}}}
    second = sync_history_source(
        client=client, persistence=db, source="TRADES", page_budget=1,
        now_epoch=1769000300,
    )
    assert second["generation_complete"] is True
    assert db.fills()[0]["price"] == "91000"
    assert db.fills()[0]["fee"] == "0.21"
    revised_event = next(
        item for item in db.tax_events_for_year(2026)
        if item["event_id"] == "TRADE:T-REVISED"
    )
    assert revised_event["value_eur"] is None
    assert revised_event["verified"] == 0
    assert revised_event["manual_review"] == 1
    db.close()


def test_forced_ledger_refresh_can_scope_to_a_snapshot_interval():
    db = Persistence(":memory:")
    client = FakeHistoryClient(ledger_pages={
        0: {"ledger": {"L-RECENT": ledger()}, "count": 1},
    })
    first = sync_history_source(
        client=client, persistence=db, source="LEDGERS", page_budget=1,
        now_epoch=1769000000,
    )
    assert first["generation_complete"] is True
    forced = sync_history_source(
        client=client, persistence=db, source="LEDGERS", page_budget=1,
        now_epoch=1769000010, force_refresh=True, start_epoch_override=1768999900,
    )
    assert forced["generation_complete"] is True
    assert client.calls[-1] == ("LEDGERS", 0, 1768999899, 1769000010)
    assert forced["initial_backfill_complete"] is True
    db.close()


def test_forced_refresh_does_not_reset_an_incomplete_initial_backfill():
    db = Persistence(":memory:")
    client = FakeHistoryClient(ledger_pages={
        0: {"ledger": {"L1": ledger()}, "count": 3},
        1: {"ledger": {"L2": ledger()}, "count": 3},
    })
    first = sync_history_source(
        client=client, persistence=db, source="LEDGERS", page_budget=1,
        now_epoch=1769000000,
    )
    assert first["generation_complete"] is False
    second = sync_history_source(
        client=client, persistence=db, source="LEDGERS", page_budget=1,
        now_epoch=1769000010, force_refresh=True, start_epoch_override=1768999900,
    )
    assert second["generation_complete"] is False
    assert second["next_offset"] == 2
    assert second["initial_backfill_complete"] is False
    assert client.calls[-1] == ("LEDGERS", 1, 0, 1769000000)
    db.close()



def _history_readiness_summary(source, *, initial=True, generation=True, error=None):
    return {
        "source": source,
        "initial_backfill_complete": initial,
        "generation_complete": generation,
        "last_error": error,
    }


def test_new_risk_history_gate_requires_all_sources_and_completed_current_sync():
    ready, gaps = history_sources_ready([
        _history_readiness_summary("TRADES"),
        _history_readiness_summary("LEDGERS"),
        _history_readiness_summary("CLOSED_ORDERS"),
    ])
    assert ready is True
    assert gaps == []

    ready, gaps = history_sources_ready([
        _history_readiness_summary("TRADES", generation=False),
        _history_readiness_summary("LEDGERS"),
        _history_readiness_summary("CLOSED_ORDERS"),
    ])
    assert ready is False
    assert "CURRENT_SYNC_INCOMPLETE:TRADES" in gaps


def test_new_risk_history_gate_fails_closed_for_missing_or_duplicate_sources():
    ready, gaps = history_sources_ready([
        _history_readiness_summary("TRADES"),
        _history_readiness_summary("LEDGERS"),
        _history_readiness_summary("TRADES"),
    ])
    assert ready is False
    assert "HISTORY_SOURCE_MISSING:CLOSED_ORDERS" in gaps
    assert "DUPLICATE_HISTORY_SOURCE:TRADES" in gaps


def test_new_risk_history_gate_fails_closed_for_errors_and_unexpected_sources():
    ready, gaps = history_sources_ready([
        _history_readiness_summary("TRADES", error="HISTORY_PAGE_INVALID"),
        _history_readiness_summary("LEDGERS"),
        _history_readiness_summary("CLOSED_ORDERS"),
        _history_readiness_summary("BALANCES"),
    ])
    assert ready is False
    assert "HISTORY_SOURCE_ERROR:TRADES:HISTORY_PAGE_INVALID" in gaps
    assert "UNEXPECTED_HISTORY_SOURCE:BALANCES" in gaps
