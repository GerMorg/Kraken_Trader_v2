from __future__ import annotations

from app.history_sync import sync_history_source
from app.persistence import Persistence


class FakeHistoryClient:
    def __init__(self, trade_pages=None, ledger_pages=None):
        self.trade_pages = trade_pages or {}
        self.ledger_pages = ledger_pages or {}
        self.calls = []

    def trades_history(self, *, offset, start, end):
        self.calls.append(("TRADES", offset, start, end))
        return self.trade_pages[offset]

    def ledgers(self, *, offset, start, end):
        self.calls.append(("LEDGERS", offset, start, end))
        return self.ledger_pages[offset]


def trade(trade_time=1768000000):
    return {
        "time": trade_time, "type": "buy", "pair": "XBTUSD",
        "vol": "0.01", "price": "90000", "fee": "0.20",
        "ordertxid": "ORDER-1",
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
        0: {"trades": {"T1": trade(), "T2": trade()}, "count": 3},
        2: {"trades": {"T3": trade()}, "count": 3},
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
        0: {"trades": {"T1": {**trade(), "time": "NaN"}}, "count": 1},
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
    client = FakeHistoryClient(trade_pages={
        0: {"trades": {}, "count": "NaN"},
    })
    result = sync_history_source(client=client, persistence=db, source="TRADES",
                                 page_budget=1, now_epoch=1769000000)
    assert result["generation_complete"] is False
    assert result["last_error"] == "HISTORY_RESPONSE_SHAPE_OR_COUNT_INVALID"
    assert result["next_offset"] == 0
    db.close()


def test_history_requires_known_source_and_bounded_page_budget():
    import pytest

    db = Persistence(":memory:")
    client = FakeHistoryClient()
    with pytest.raises(ValueError, match="HISTORY_SOURCE_INVALID"):
        sync_history_source(client=client, persistence=db, source="CLOSED_ORDERS")
    with pytest.raises(ValueError, match="HISTORY_PAGE_BUDGET_OUT_OF_RANGE"):
        sync_history_source(client=client, persistence=db, source="TRADES", page_budget=0)
    db.close()
