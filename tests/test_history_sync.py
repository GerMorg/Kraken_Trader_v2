from __future__ import annotations

from app.history_sync import history_sources_ready, reconcile_trade_ledger_coverage, sync_history_source
from app.persistence import Persistence


class FakeHistoryClient:
    def __init__(self, trade_pages=None, ledger_pages=None, closed_pages=None):
        self.trade_pages = trade_pages or {}
        self.ledger_pages = ledger_pages or {}
        self.closed_pages = closed_pages or {}
        self.targeted_trade_pages = {}
        self.calls = []

    def trades_history(self, *, offset, start, end, with_cursor=False, cursor=None):
        self.calls.append(("TRADES", offset, start, end, with_cursor, cursor))
        if (
            with_cursor and start is not None and end is not None
            and int(end) - int(start) <= 20
        ):
            return self.targeted_trade_pages.get(cursor, {"trades": {}, "cursor": {}})
        return self.trade_pages[cursor] if with_cursor else self.trade_pages[offset]

    def ledgers(self, *, offset, start, end, with_cursor=False, cursor=None):
        self.calls.append(("LEDGERS", offset, start, end, with_cursor, cursor))
        if with_cursor:
            if cursor in self.ledger_pages:
                return self.ledger_pages[cursor]
            return self.ledger_pages[0]
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
        "TRADES-CURSOR-2": {"trades": {"T3": trade()}},
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
        None: {"trades": {"T1": {**trade(), "time": "NaN"}}},
    })
    result = sync_history_source(client=client, persistence=db, source="TRADES",
                                 page_budget=1, now_epoch=1769000000)
    assert result["generation_complete"] is False
    assert result["last_error"] == "HISTORY_ITEM_INVALID:TRADE_TIME_INVALID"
    assert result["invalid_item_reason"] == "TRADE_TIME_INVALID"
    assert len(result["invalid_item_id_fingerprint"]) == 12
    assert result["next_offset"] == 0
    assert result["initial_backfill_complete"] is False
    db.close()


def test_ledger_history_rejects_invalid_cursor_shape_and_fails_closed():
    db = Persistence(":memory:")
    client = FakeHistoryClient(ledger_pages={
        0: {"ledger": {}, "cursor": "INVALID"},
    })
    result = sync_history_source(client=client, persistence=db, source="LEDGERS",
                                 page_budget=1, now_epoch=1769000000)
    assert result["generation_complete"] is False
    assert result["last_error"] == "HISTORY_CURSOR_SHAPE_INVALID"
    assert result["next_offset"] == 0
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
        "CLOSED-CURSOR-2": {"closed": {"O3": _closed_order()}},
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
        None: {"closed": {"O1": _closed_order(closed_time="NaN")}},
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
        None: {"trades": {"TMARGIN": margin_trade}},
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


def test_trade_history_missing_ledger_references_are_imported_but_block_new_risk():
    db = Persistence(":memory:")
    missing_refs = trade()
    missing_refs.pop("ledgers")
    client = FakeHistoryClient(trade_pages={None: {"trades": {"T-NO-LEDGERS": missing_refs}}})

    result = sync_history_source(
        client=client, persistence=db, source="TRADES", page_budget=1, now_epoch=1769000000
    )

    assert result["generation_complete"] is True
    assert result["initial_backfill_complete"] is True
    assert result["last_error"] is None
    assert result["quality_gap_count"] == 1
    assert result["quality_gap_types"] == ["TRADE_LEDGER_REFERENCES_MISSING"]
    assert result["quality_gaps"][0].startswith("TRADE_LEDGER_REFERENCES_MISSING:")
    assert len(db.fills()) == 1
    tax_event = db.tax_events_for_year(2026)[0]
    assert tax_event["payload"]["related_ledger_ids_missing"] is True
    ready, gaps = history_sources_ready([
        {**result, "source": "TRADES"},
        {"source": "LEDGERS", "initial_backfill_complete": True,
         "generation_complete": True, "last_error": None, "quality_gaps": []},
        {"source": "CLOSED_ORDERS", "initial_backfill_complete": True,
         "generation_complete": True, "last_error": None, "quality_gaps": []},
    ])
    assert ready is False
    assert "HISTORY_DATA_QUALITY_GAP:TRADES:TRADE_LEDGER_REFERENCES_MISSING" in gaps
    assert db.history_sync_state("TRADES")["quality_gaps"] == result["quality_gaps"]
    db.close()


def test_full_ledger_refid_recovers_trade_links_when_querytrades_omits_them():
    db = Persistence(":memory:")
    missing_refs = trade()
    missing_refs.pop("ledgers")
    linked_ledger = {**ledger(), "refid": "T-REFID"}
    client = FakeHistoryClient(
        trade_pages={None: {"trades": {"T-REFID": missing_refs}}},
        ledger_pages={0: {"ledger": {"L-REFID": linked_ledger}, "count": 1}},
    )
    trade_sync = sync_history_source(
        client=client, persistence=db, source="TRADES", page_budget=1, now_epoch=1769000000
    )
    assert trade_sync["quality_gap_count"] == 1
    ledger_sync = sync_history_source(
        client=client, persistence=db, source="LEDGERS", page_budget=1, now_epoch=1769000000
    )
    assert ledger_sync["generation_complete"] is True

    # QueryTrades was asked for the missing relation but omitted the optional field.
    client.query_trades_info = lambda trade_ids, *, include_ledgers=True: {
        "T-REFID": {**missing_refs}
    }
    client.query_ledgers_info = lambda ledger_ids: {}
    result = reconcile_trade_ledger_coverage(client=client, persistence=db)

    assert result["status"] == "COMPLETE"
    assert result["missing_trade_references"] == 0
    assert result["missing_ledger_references"] == 0
    fill = db.fills()[0]
    recovered_raw = fill["raw"]["raw"]
    assert recovered_raw["ledgers"] == ["L-REFID"]
    assert recovered_raw["_ledger_reference_evidence"]["source"] == "FULL_LEDGER_REFID_MATCH"
    assert db.history_sync_state("TRADES")["quality_gaps"] == []
    event_count = db.connection.execute(
        "SELECT COUNT(*) AS n FROM events WHERE name=?",
        ("KRAKEN_HISTORY_DATA_QUALITY_GAP_RESOLVED",),
    ).fetchone()["n"]
    assert event_count >= 1
    db.close()


def test_targeted_query_recovers_missing_trade_ledger_links_and_imports_ledgers():
    db = Persistence(":memory:")
    missing_refs = trade()
    missing_refs.pop("ledgers")
    client = FakeHistoryClient(
        trade_pages={None: {"trades": {"T-NO-LEDGERS": missing_refs}}},
        ledger_pages={0: {"ledger": {"L1": ledger(), "L2": ledger()}, "count": 2}},
    )
    first = sync_history_source(
        client=client, persistence=db, source="TRADES", page_budget=1, now_epoch=1769000000
    )
    assert first["quality_gap_count"] == 1
    sync_history_source(
        client=client, persistence=db, source="LEDGERS", page_budget=1, now_epoch=1769000000
    )

    client.query_trades_info = lambda trade_ids, *, include_ledgers=True: {
        "T-NO-LEDGERS": {**missing_refs, "ledgers": ["L1", "L2"]}
    }
    client.query_ledgers_info = lambda ledger_ids: {}

    result = reconcile_trade_ledger_coverage(client=client, persistence=db)

    assert result["status"] == "COMPLETE"
    assert result["trade_rows_queried"] == 1
    assert result["trade_rows_recovered"] == 1
    assert result["missing_trade_references"] == 0
    assert result["missing_ledger_references"] == 0
    assert db.history_sync_state("TRADES")["quality_gaps"] == []
    assert {item["ledger_id"] for item in db.ledger_events()} == {"L1", "L2"}
    fill = db.fills()[0]
    assert fill["raw"]["raw"]["ledgers"] == ["L1", "L2"]
    event = next(item for item in db.tax_events_for_year(2026)
                 if item["event_id"] == "TRADE:T-NO-LEDGERS")
    assert event["payload"]["related_ledger_ids"] == ["L1", "L2"]
    assert event["payload"]["related_ledger_ids_missing"] is False
    db.close()


def test_empty_full_ledger_backfill_cannot_pass_when_trades_reference_ledgers():
    db = Persistence(":memory:")
    row = {**trade(), "ledgers": ["L1"]}
    client = FakeHistoryClient(
        trade_pages={None: {"trades": {"T-REF": row}}},
        ledger_pages={0: {"ledger": {}, "count": 0}},
    )
    sync_history_source(
        client=client, persistence=db, source="TRADES", page_budget=1, now_epoch=1769000000
    )
    empty_backfill = sync_history_source(
        client=client, persistence=db, source="LEDGERS", page_budget=1, now_epoch=1769000000
    )
    assert empty_backfill["initial_backfill_complete"] is True
    client.query_trades_info = lambda trade_ids, *, include_ledgers=True: {}
    client.query_ledgers_info = lambda ledger_ids: {"L1": ledger()}

    result = reconcile_trade_ledger_coverage(client=client, persistence=db)

    assert result["ledger_full_backfill_conflict"] is True
    assert result["status"] == "INCOMPLETE"
    ledger_state = db.history_sync_state("LEDGERS")
    assert ledger_state["initial_backfill_complete"] is False
    assert ledger_state["generation_complete"] is False
    assert ledger_state["last_error"] == "LEDGER_HISTORY_COVERAGE_CONFLICT"
    assert "LEDGER_HISTORY_COVERAGE_INCOMPLETE:EMPTY_FULL_BACKFILL" in ledger_state["quality_gaps"]
    ready, gaps = history_sources_ready([
        {**db.history_sync_state("TRADES"), "source": "TRADES"},
        {**ledger_state, "source": "LEDGERS"},
        {"source": "CLOSED_ORDERS", "initial_backfill_complete": True,
         "generation_complete": True, "last_error": None, "quality_gaps": []},
    ])
    assert ready is False
    assert any("LEDGERS" in gap for gap in gaps)
    db.close()


def test_targeted_trades_history_uses_cursor_and_exact_trade_id():
    db = Persistence(":memory:")
    missing_refs = trade()
    missing_refs.pop("ledgers")
    client = FakeHistoryClient(
        trade_pages={None: {"trades": {"T-CURSOR-TARGET": missing_refs}}},
        ledger_pages={0: {"ledger": {"L-CURSOR": {**ledger(), "refid": "T-CURSOR-TARGET"}},
                        "count": 1}},
    )
    sync_history_source(
        client=client, persistence=db, source="TRADES", page_budget=1, now_epoch=1769000000
    )
    sync_history_source(
        client=client, persistence=db, source="LEDGERS", page_budget=1, now_epoch=1769000000
    )
    unrelated = {**trade(), "ledgers": []}
    client.targeted_trade_pages = {
        None: {"trades": {"T-UNRELATED": unrelated}, "cursor": {"next": "WINDOW-CURSOR-2"}},
        "WINDOW-CURSOR-2": {
            "trades": {"T-CURSOR-TARGET": {**missing_refs, "ledgers": ["L-CURSOR"]}},
            "cursor": {},
        },
    }
    client.query_trades_info = lambda trade_ids, *, include_ledgers=True: {
        "T-CURSOR-TARGET": dict(missing_refs)
    }
    client.query_ledgers_info = lambda ledger_ids: {}

    result = reconcile_trade_ledger_coverage(client=client, persistence=db)

    assert result["status"] == "COMPLETE"
    assert result["trade_rows_history_window_queried"] == 1
    assert result["trade_rows_history_window_pages"] == 2
    assert result["trade_rows_history_window_recovered"] == 1
    assert result["missing_trade_references"] == 0
    assert db.fills()[0]["raw"]["raw"]["ledgers"] == ["L-CURSOR"]
    window_calls = [
        call for call in client.calls
        if call[0] == "TRADES" and call[2] is not None and call[3] is not None
        and call[3] - call[2] <= 20
    ]
    assert [call[5] for call in window_calls] == [None, "WINDOW-CURSOR-2"]
    db.close()


def test_query_trades_empty_ledger_list_is_confirmed_only_after_full_ledger_check():
    db = Persistence(":memory:")
    missing_refs = trade()
    missing_refs.pop("ledgers")
    other_ledger = {**ledger(), "refid": "T-OTHER"}
    client = FakeHistoryClient(
        trade_pages={None: {"trades": {"T-EMPTY-RELATION": missing_refs}}},
        ledger_pages={0: {"ledger": {"L-OTHER": other_ledger}}},
    )
    sync_history_source(
        client=client, persistence=db, source="TRADES", page_budget=1, now_epoch=1769000000
    )
    sync_history_source(
        client=client, persistence=db, source="LEDGERS", page_budget=1, now_epoch=1769000000
    )
    client.query_trades_info = lambda trade_ids, *, include_ledgers=True: {
        "T-EMPTY-RELATION": {**missing_refs, "ledgers": []}
    }
    client.query_ledgers_info = lambda ledger_ids: {}

    result = reconcile_trade_ledger_coverage(client=client, persistence=db)

    assert result["status"] == "COMPLETE"
    assert result["trade_rows_query_empty_ledgers"] == 1
    assert result["trade_rows_confirmed_empty"] == 1
    assert result["missing_trade_references"] == 0
    assert result["trade_quality_gap_count"] == 0
    assert db.history_sync_state("TRADES")["quality_gaps"] == []
    fill = db.fills()[0]
    saved = fill["raw"]["raw"]
    assert saved["ledgers"] == []
    assert saved["_ledger_reference_evidence"]["source"] == "QUERY_TRADES_EMPTY_CONFIRMED"
    assert saved["_ledger_reference_evidence"]["full_ledger_history_checked"] is True
    tax_event = next(
        item for item in db.tax_events_for_year(2026)
        if item["event_id"] == "TRADE:T-EMPTY-RELATION"
    )
    assert tax_event["payload"]["related_ledger_ids"] == []
    assert tax_event["payload"]["related_ledger_ids_missing"] is False
    resolved = db.connection.execute(
        "SELECT COUNT(*) AS n FROM events WHERE name=?",
        ("KRAKEN_HISTORY_DATA_QUALITY_GAP_RESOLVED",),
    ).fetchone()["n"]
    assert resolved == 1
    db.close()


def test_query_trades_empty_ledger_list_conflicts_with_matching_ledger_refid():
    db = Persistence(":memory:")
    missing_refs = trade()
    missing_refs.pop("ledgers")
    linked_ledger = {**ledger(), "refid": "T-CONFLICT"}
    client = FakeHistoryClient(
        trade_pages={None: {"trades": {"T-CONFLICT": missing_refs}}},
        ledger_pages={0: {"ledger": {"L-CONFLICT": linked_ledger}}},
    )
    sync_history_source(
        client=client, persistence=db, source="TRADES", page_budget=1, now_epoch=1769000000
    )
    sync_history_source(
        client=client, persistence=db, source="LEDGERS", page_budget=1, now_epoch=1769000000
    )
    client.query_trades_info = lambda trade_ids, *, include_ledgers=True: {
        "T-CONFLICT": {**missing_refs, "ledgers": []}
    }
    client.query_ledgers_info = lambda ledger_ids: {}

    result = reconcile_trade_ledger_coverage(client=client, persistence=db)

    assert result["status"] == "INCOMPLETE"
    assert result["missing_trade_references"] == 1
    assert any(
        item.startswith("TRADE_LEDGER_REFERENCE_SOURCE_CONFLICT:")
        for item in db.history_sync_state("TRADES")["quality_gaps"]
    )
    assert any(
        item["outcome"] == "QUERY_TRADES_EMPTY_BUT_LEDGER_REFID_MATCHES"
        for item in result["trade_query_outcomes"]
    )
    db.close()


def test_query_trades_empty_ledger_list_conflicts_with_matching_order_refid():
    db = Persistence(":memory:")
    missing_refs = trade()
    missing_refs.pop("ledgers")
    order_ledger = {**ledger(), "refid": "ORDER-1"}
    client = FakeHistoryClient(
        trade_pages={None: {"trades": {"T-ORDER-REFID": missing_refs}}},
        ledger_pages={0: {"ledger": {"L-ORDER": order_ledger}}},
    )
    sync_history_source(
        client=client, persistence=db, source="TRADES", page_budget=1, now_epoch=1769000000
    )
    sync_history_source(
        client=client, persistence=db, source="LEDGERS", page_budget=1, now_epoch=1769000000
    )
    client.query_trades_info = lambda trade_ids, *, include_ledgers=True: {
        "T-ORDER-REFID": {**missing_refs, "ledgers": []}
    }
    client.query_ledgers_info = lambda ledger_ids: {}

    result = reconcile_trade_ledger_coverage(client=client, persistence=db)

    assert result["status"] == "INCOMPLETE"
    assert result["missing_trade_references"] == 1
    assert any(
        item["outcome"] == "QUERY_TRADES_EMPTY_BUT_LEDGER_ORDER_REFID_MATCHES"
        for item in result["trade_query_outcomes"]
    )
    db.close()


def test_trade_history_missing_margin_field_is_incomplete_not_guessed_as_spot():
    db = Persistence(":memory:")
    missing_margin = trade()
    missing_margin.pop("margin")
    client = FakeHistoryClient(trade_pages={
        None: {"trades": {"T-MISSING-MARGIN": missing_margin}},
    })
    result = sync_history_source(client=client, persistence=db, source="TRADES",
                                 page_budget=1, now_epoch=1769000000)
    assert result["generation_complete"] is False
    assert result["last_error"] == "HISTORY_ITEM_INVALID:TRADE_FIELD_MISSING_OR_INVALID:margin"
    assert result["invalid_item_reason"] == "TRADE_FIELD_MISSING_OR_INVALID:margin"
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
        None: {"trades": {"T-REVISED": first_trade}},
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
    client.trade_pages = {None: {"trades": {"T-REVISED": corrected_trade}}}
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
    assert client.calls[-1] == ("LEDGERS", 0, 1768999899, 1769000010, True, None)
    assert forced["initial_backfill_complete"] is True
    db.close()


def test_forced_refresh_does_not_reset_an_incomplete_initial_backfill():
    db = Persistence(":memory:")
    client = FakeHistoryClient(ledger_pages={
        None: {"ledger": {"L1": ledger()}, "cursor": {"next": "LEDGER-CURSOR-2"}},
        "LEDGER-CURSOR-2": {
            "ledger": {"L2": ledger()}, "cursor": {"next": "LEDGER-CURSOR-3"},
        },
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
    assert client.calls[-1] == ("LEDGERS", 0, 0, 1769000000, True, "LEDGER-CURSOR-2")
    db.close()



def _history_readiness_summary(source, *, initial=True, generation=True, error=None, quality_gaps=None):
    return {
        "source": source,
        "initial_backfill_complete": initial,
        "generation_complete": generation,
        "last_error": error,
        "quality_gaps": quality_gaps or [],
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


def test_new_risk_history_gate_fails_closed_for_persisted_quality_gaps():
    ready, gaps = history_sources_ready([
        _history_readiness_summary(
            "TRADES", quality_gaps=["TRADE_LEDGER_REFERENCES_MISSING:46179196fa61"]
        ),
        _history_readiness_summary("LEDGERS"),
        _history_readiness_summary("CLOSED_ORDERS"),
    ])

    assert ready is False
    assert gaps == ["HISTORY_DATA_QUALITY_GAP:TRADES:TRADE_LEDGER_REFERENCES_MISSING"]


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



def test_query_trades_missing_ledgers_field_is_persisted_and_not_requeried():
    db = Persistence(":memory:")
    missing_refs = trade()
    missing_refs.pop("ledgers")
    unrelated_ledger = {**ledger(), "refid": "UNRELATED-TRADE"}
    client = FakeHistoryClient(
        trade_pages={None: {"trades": {"T-MISSING-FIELD": missing_refs}}},
        ledger_pages={0: {"ledger": {"L-UNRELATED": unrelated_ledger}, "count": 1}},
    )
    client.targeted_trade_pages = {
        None: {"trades": {"T-MISSING-FIELD": dict(missing_refs)}, "cursor": {}},
    }
    sync_history_source(
        client=client, persistence=db, source="TRADES", page_budget=1, now_epoch=1769000000
    )
    sync_history_source(
        client=client, persistence=db, source="LEDGERS", page_budget=1, now_epoch=1769000000
    )

    calls = []
    def query_trades_info(trade_ids, *, include_ledgers=True):
        calls.append((list(trade_ids), include_ledgers))
        return {"T-MISSING-FIELD": dict(missing_refs)}

    client.query_trades_info = query_trades_info
    client.query_ledgers_info = lambda ledger_ids: {}

    first = reconcile_trade_ledger_coverage(client=client, persistence=db)
    assert first["status"] == "INCOMPLETE"
    assert first["trade_rows_queried"] == 1
    assert first["trade_rows_query_ledger_field_missing"] == 1
    assert first["trade_rows_history_window_queried"] == 1
    assert first["trade_rows_history_window_ledger_field_missing"] == 1
    assert first["trade_rows_history_window_pages"] == 1
    assert first["missing_trade_references"] == 1
    assert len(calls) == 1

    saved = db.fills()[0]["raw"]["raw"]
    assert "ledgers" not in saved
    assert saved["_ledger_reference_evidence"]["source"] == "TRADES_HISTORY_WINDOW_LEDGER_FIELD_MISSING"
    assert saved["_ledger_reference_evidence"]["ledgers_field_present"] is False
    assert saved["_ledger_reference_evidence"]["attempt_count"] == 2
    assert (
        saved["_ledger_reference_evidence"]["retry_after_epoch"]
        > saved["_ledger_reference_evidence"]["last_attempt_epoch"]
    )
    assert db.history_sync_state("TRADES")["quality_gaps"]

    second = reconcile_trade_ledger_coverage(client=client, persistence=db)
    assert second["status"] == "INCOMPLETE"
    assert second["trade_rows_queried"] == 0
    assert second["trade_rows_query_deferred"] == 0
    assert second["trade_rows_history_window_queried"] == 0
    assert second["trade_rows_history_window_deferred"] == 1
    assert (
        second["next_trade_history_retry_epoch"]
        == saved["_ledger_reference_evidence"]["retry_after_epoch"]
    )
    assert second["missing_trade_references"] == 1
    assert len(calls) == 1
    assert db.history_sync_state("TRADES")["quality_gaps"]
    db.close()


def test_missing_trade_ledger_field_is_retried_after_cooldown_and_can_recover():
    db = Persistence(":memory:")
    missing_refs = trade()
    missing_refs.pop("ledgers")
    missing_refs["_ledger_reference_evidence"] = {
        "source": "QUERY_TRADES_LEDGER_FIELD_MISSING",
        "ledgers_field_present": False,
        "attempt_count": 1,
        "last_attempt_epoch": 1769000000,
        "retry_after_epoch": 0,
    }
    unrelated_ledger = {**ledger(), "refid": "UNRELATED-TRADE"}
    client = FakeHistoryClient(
        trade_pages={None: {"trades": {"T-MISSING-FIELD-RETRY": missing_refs}}},
        ledger_pages={0: {"ledger": {"L-RECOVERED": unrelated_ledger}, "count": 1}},
    )
    sync_history_source(
        client=client, persistence=db, source="TRADES", page_budget=1, now_epoch=1769000000
    )
    sync_history_source(
        client=client, persistence=db, source="LEDGERS", page_budget=1, now_epoch=1769000000
    )

    calls = []
    recovered_row = {
        key: value for key, value in missing_refs.items()
        if key != "_ledger_reference_evidence"
    }
    recovered_row["ledgers"] = ["L-RECOVERED"]
    client.targeted_trade_pages = {
        None: {"trades": {"T-MISSING-FIELD-RETRY": recovered_row}, "cursor": {}},
    }

    def query_trades_info(trade_ids, *, include_ledgers=True):
        calls.append((list(trade_ids), include_ledgers))
        return {}

    client.query_trades_info = query_trades_info
    client.query_ledgers_info = lambda ledger_ids: {}

    result = reconcile_trade_ledger_coverage(client=client, persistence=db)

    assert result["status"] == "COMPLETE"
    assert result["trade_rows_queried"] == 0
    assert result["trade_rows_history_window_queried"] == 1
    assert result["trade_rows_history_window_recovered"] == 1
    assert result["trade_rows_query_deferred"] == 0
    assert result["trade_rows_recovered"] == 1
    assert result["missing_trade_references"] == 0
    assert calls == []
    saved = db.fills()[0]["raw"]["raw"]
    assert saved["ledgers"] == ["L-RECOVERED"]
    assert "_ledger_reference_evidence" not in saved
    assert db.history_sync_state("TRADES")["quality_gaps"] == []
    db.close()


def test_trade_export_recovery_restores_exact_trade_ledger_links(monkeypatch):
    import csv
    import io
    import zipfile
    from datetime import datetime, timezone

    db = Persistence(":memory:")
    missing_refs = trade()
    missing_refs.pop("ledgers")
    other_ledger = {**ledger(), "refid": "T-OTHER"}

    class ExportHistoryClient(FakeHistoryClient):
        def __init__(self):
            super().__init__(
                trade_pages={None: {"trades": {"T-EXPORT-RECOVERY": missing_refs}}},
                ledger_pages={0: {"ledger": {"L-OTHER": other_ledger}, "count": 1}},
            )
            self.export_request = None
            self.export_status_value = "Queued"
            self.export_archive = b""

        def request_export_report(self, *, description, start_epoch, end_epoch, report="trades"):
            self.export_request = {
                "description": description,
                "start_epoch": start_epoch,
                "end_epoch": end_epoch,
                "report": report,
            }
            return {"id": "EXPORT-RECOVERY-1"}

        def export_status(self, *, report="trades"):
            assert report == "trades"
            if self.export_request is None:
                return []
            return [{
                "id": "EXPORT-RECOVERY-1",
                "descr": self.export_request["description"],
                "status": self.export_status_value,
                "report": "trades",
                "starttm": str(self.export_request["start_epoch"]),
                "endtm": str(self.export_request["end_epoch"]),
            }]

        def retrieve_export(self, report_id):
            assert report_id == "EXPORT-RECOVERY-1"
            return self.export_archive

    client = ExportHistoryClient()
    sync_history_source(
        client=client, persistence=db, source="TRADES", page_budget=1, now_epoch=1769000000
    )
    sync_history_source(
        client=client, persistence=db, source="LEDGERS", page_budget=1, now_epoch=1769000000
    )
    client.query_trades_info = lambda trade_ids, *, include_ledgers=True: {
        "T-EXPORT-RECOVERY": dict(missing_refs)
    }
    client.query_ledgers_info = lambda ledger_ids: {
        "L-EXPORT-RECOVERY": {**ledger(), "refid": "T-EXPORT-RECOVERY"}
    }

    now = [1769000000]
    monkeypatch.setattr("app.history_sync.time.time", lambda: now[0])
    first = reconcile_trade_ledger_coverage(client=client, persistence=db)
    assert first["trade_export_supported"] is True
    assert first["trade_export_requests_created"] == 1
    assert first["trade_export_rows_recovered"] == 0
    assert first["status"] == "INCOMPLETE"
    assert client.export_request is not None
    assert client.export_request["report"] == "trades"
    assert client.export_request["start_epoch"] <= 1768000000
    assert client.export_request["end_epoch"] >= 1768000000

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=[
        "txid", "ordertxid", "time", "type", "pair", "price", "cost",
        "fee", "vol", "margin", "ledgers",
    ])
    writer.writeheader()
    timestamp = datetime.fromtimestamp(1768000000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )
    writer.writerow({
        "txid": "T-EXPORT-RECOVERY",
        "ordertxid": "ORDER-1",
        "time": timestamp,
        "type": "buy",
        "pair": "XBT/USD",
        "price": "90000",
        "cost": "900",
        "fee": "0.20",
        "vol": "0.01",
        "margin": "0",
        "ledgers": "L-EXPORT-RECOVERY",
    })
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("trades.csv", output.getvalue().encode("utf-8"))
    client.export_archive = buffer.getvalue()
    client.export_status_value = "Processed"
    now[0] += 301

    recovered = reconcile_trade_ledger_coverage(client=client, persistence=db)
    assert recovered["trade_export_requests_checked"] == 1
    assert recovered["trade_export_rows_matched"] == 1
    assert recovered["trade_export_rows_recovered"] == 1
    assert recovered["missing_trade_references"] == 0
    assert recovered["status"] == "COMPLETE"

    stored = next(item for item in db.fills() if item["trade_id"] == "T-EXPORT-RECOVERY")
    assert stored["raw"]["raw"]["ledgers"] == ["L-EXPORT-RECOVERY"]
    assert stored["raw"]["raw"]["_ledger_reference_evidence"]["source"] == (
        "TRADE_EXPORT_LEDGER_LINK_RECOVERED"
    )
    assert {item["ledger_id"] for item in db.ledger_events()} >= {
        "L-OTHER", "L-EXPORT-RECOVERY",
    }
    export_jobs = db.history_export_recovery_requests()
    assert len(export_jobs) == 1
    assert export_jobs[0]["status"] == "IMPORTED"
    db.close()


def test_trade_export_recovery_does_not_assume_empty_ledgers_are_complete():
    from app.history_sync import _parse_trade_export_archive

    import csv
    import io
    import zipfile

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=[
        "txid", "ordertxid", "time", "type", "pair", "price",
        "fee", "vol", "margin", "ledgers",
    ])
    writer.writeheader()
    writer.writerow({
        "txid": "T-EMPTY-EXPORT",
        "ordertxid": "ORDER-1",
        "time": "2026-01-09 09:46:40.000000",
        "type": "buy",
        "pair": "XBT/USD",
        "price": "90000",
        "fee": "0.20",
        "vol": "0.01",
        "margin": "0",
        "ledgers": "",
    })
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("trades.csv", output.getvalue().encode("utf-8"))

    rows = _parse_trade_export_archive(buffer.getvalue(), {"T-EMPTY-EXPORT"})
    assert rows["T-EMPTY-EXPORT"]["ledgers"] == []
    assert rows["T-EMPTY-EXPORT"]["_exchange_export_source"] == "KRAKEN_TRADE_EXPORT_CSV"
