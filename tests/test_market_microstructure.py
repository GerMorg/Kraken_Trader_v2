from decimal import Decimal

import pytest

from app.market_microstructure import (
    MarketMicrostructureCollector,
    calculate_book_checksum,
)
from app.persistence import Persistence


def test_kraken_book_checksum_uses_asks_then_bids_and_preserves_level_order():
    bids = {Decimal("100"): Decimal("2"), Decimal("99.5"): Decimal("3")}
    asks = {Decimal("100.5"): Decimal("4"), Decimal("101"): Decimal("5")}
    assert calculate_book_checksum(bids, asks) == 1663522217


def test_book_updates_trade_aggregation_and_minute_persistence():
    db = Persistence(":memory:")
    collector = MarketMicrostructureCollector(db, enabled=False)

    bids = {Decimal("100"): Decimal("2"), Decimal("99.5"): Decimal("3")}
    asks = {Decimal("100.5"): Decimal("4"), Decimal("101"): Decimal("5")}
    snapshot = {
        "channel": "book",
        "type": "snapshot",
        "data": [{
            "symbol": "XBT/USD",
            "timestamp": "2027-01-15T08:00:01.000000Z",
            "bids": [
                {"price": "100", "qty": "2"},
                {"price": "99.5", "qty": "3"},
            ],
            "asks": [
                {"price": "100.5", "qty": "4"},
                {"price": "101", "qty": "5"},
            ],
            "checksum": calculate_book_checksum(bids, asks),
        }],
    }
    assert collector.process_message(snapshot) is True

    updated_bids = {Decimal("100"): Decimal("3"), Decimal("99.5"): Decimal("3")}
    updated_asks = {Decimal("101"): Decimal("5")}
    update = {
        "channel": "book",
        "type": "update",
        "data": [{
            "symbol": "XBT/USD",
            "timestamp": "2027-01-15T08:00:30.000000Z",
            "bids": [{"price": "100", "qty": "3"}],
            "asks": [{"price": "100.5", "qty": "0"}],
            "checksum": calculate_book_checksum(updated_bids, updated_asks),
        }],
    }
    assert collector.process_message(update) is True

    trades = {
        "channel": "trade",
        "type": "update",
        "data": [
            {
                "symbol": "XBT/USD",
                "side": "buy",
                "price": "100.5",
                "qty": "1",
                "trade_id": 100,
                "timestamp": "2027-01-15T08:00:40.000000Z",
            },
            {
                "symbol": "XBT/USD",
                "side": "sell",
                "price": "100.4",
                "qty": "0.5",
                "trade_id": 102,
                "timestamp": "2027-01-15T08:00:45.000000Z",
            },
        ],
    }
    assert collector.process_message(trades) is True
    assert collector.flush_due(1_800_000_100) == 1

    bars = db.microstructure_bars("XBT/USD")
    assert len(bars) == 1
    bar = bars[0]
    assert bar["trade_count"] == 2
    assert bar["buy_notional_usd"] == "100.5"
    assert bar["sell_notional_usd"] == "50.20"
    assert bar["signed_trade_notional_usd"] == "50.30"
    assert bar["trade_id_gaps"] == 1
    assert "TRADE_ID_BASELINE_UNAVAILABLE" in bar["quality_flags"]
    assert bar["checksum_checks"] == 2
    assert bar["checksum_failures"] == 0
    assert bar["book_update_count"] == 1
    assert bar["ofi_l1_base"] == "5"
    assert bar["quality_status"] == "PARTIAL"
    assert bar["actual_fills"] is False
    assert bar["order_creation_allowed"] is False
    db.close()


def test_checksum_mismatch_invalidates_book_and_requests_clean_resubscription():
    db = Persistence(":memory:")
    collector = MarketMicrostructureCollector(db, enabled=False)
    bids = {Decimal("100"): Decimal("2")}
    asks = {Decimal("101"): Decimal("3")}
    message = {
        "channel": "book",
        "type": "snapshot",
        "data": [{
            "symbol": "ETH/USD",
            "timestamp": "2027-01-15T08:00:01.000000Z",
            "bids": [{"price": "100", "qty": "2"}],
            "asks": [{"price": "101", "qty": "3"}],
            "checksum": calculate_book_checksum(bids, asks) + 1,
        }],
    }
    assert collector.process_message(message) is False
    status = collector.status()
    assert status["checksum_failures"] == 1
    assert status["last_error_type"] == "BOOK_CHECKSUM_MISMATCH"
    db.close()


def test_microstructure_observer_rejects_non_usd_pairs_and_bad_timestamps():
    db = Persistence(":memory:")
    with_value = {
        "pair": "XBT/USD",
        "minute_ts": 1_800_000_000,
        "source": "KRAKEN_SPOT_WS_V2",
        "quality_status": "VALID",
        "trade_count": 0,
        "book_update_count": 1,
        "checksum_checks": 1,
        "checksum_failures": 0,
        "trade_id_gaps": 0,
        "connection_gap_count": 0,
    }
    assert db.record_microstructure_bar(with_value) is True
    assert db.record_microstructure_bar(with_value) is False
    assert db.microstructure_bar_count() == 1
    with pytest.raises(ValueError, match="MICROSTRUCTURE_IDENTITY_INVALID"):
        db.record_microstructure_bar({**with_value, "pair": "EUR/USD"})
    with pytest.raises(ValueError, match="MICROSTRUCTURE_IDENTITY_INVALID"):
        db.record_microstructure_bar({**with_value, "minute_ts": 1_800_000_001})
    db.close()


def test_websocket_v2_converts_rest_xbt_symbol_and_maps_stream_data_back():
    db = Persistence(":memory:")
    collector = MarketMicrostructureCollector(db, enabled=False)
    collector.update_symbols(["XBT/USD", "ETH/USD"])

    ws_symbols = collector._prepare_subscriptions(collector.desired_symbols())
    assert ws_symbols == ["BTC/USD", "ETH/USD"]
    assert collector.status()["subscribed_symbols"] == ["XBT/USD", "ETH/USD"]

    collector._metrics["connected"] = True
    for channel, symbol in (
        ("book", "BTC/USD"),
        ("trade", "BTC/USD"),
        ("book", "ETH/USD"),
        ("trade", "ETH/USD"),
    ):
        assert collector.process_message({
            "method": "subscribe",
            "success": True,
            "result": {"channel": channel, "symbol": symbol},
        }) is True

    assert collector.status()["subscription_expected_count"] == 4
    assert collector.status()["subscription_acknowledged_count"] == 4
    assert collector.status()["subscription_pending_count"] == 0
    assert collector.status()["subscription_coverage_pct"] == 100.0
    assert collector.status()["connection_state"] == "CONNECTED"

    bids = {Decimal("100"): Decimal("2")}
    asks = {Decimal("101"): Decimal("3")}
    snapshot = {
        "channel": "book",
        "type": "snapshot",
        "data": [{
            "symbol": "BTC/USD",
            "timestamp": "2027-01-15T08:00:01.000000Z",
            "bids": [{"price": "100", "qty": "2"}],
            "asks": [{"price": "101", "qty": "3"}],
            "checksum": calculate_book_checksum(bids, asks),
        }],
    }
    assert collector.process_message(snapshot) is True
    assert "XBT/USD" in collector._books
    assert "BTC/USD" not in collector._books
    db.close()


def test_rejected_subscription_is_reported_without_reconnect_loop():
    db = Persistence(":memory:")
    collector = MarketMicrostructureCollector(db, enabled=False)
    collector.update_symbols(["XBT/USD"])
    assert collector._prepare_subscriptions(collector.desired_symbols()) == ["BTC/USD"]
    collector._metrics["connected"] = True

    assert collector.process_message({
        "method": "subscribe",
        "success": False,
        "symbol": "BTC/USD",
        "error": "Currency pair not supported BTC/USD",
    }) is True

    status = collector.status()
    assert status["connection_state"] == "CONNECTED_PARTIAL"
    assert status["subscription_expected_count"] == 2
    assert status["subscription_acknowledged_count"] == 0
    assert status["subscription_rejected_count"] == 2
    assert status["subscription_pending_count"] == 0
    assert status["last_error_type"] == "SUBSCRIPTION_REJECTED"
    assert status["last_subscription_error"] == "Currency pair not supported BTC/USD"
    assert status["last_subscription_symbol"] == "BTC/USD"
    db.close()


def test_minute_bar_persists_trade_ohlcv_midprice_and_verified_l2_snapshot():
    db = Persistence(":memory:")
    collector = MarketMicrostructureCollector(db, enabled=False)
    bids = {Decimal("100"): Decimal("2"), Decimal("99.5"): Decimal("3")}
    asks = {Decimal("101"): Decimal("4"), Decimal("101.5"): Decimal("5")}
    snapshot = {
        "channel": "book", "type": "snapshot",
        "data": [{
            "symbol": "ETH/USD", "timestamp": "2027-01-15T08:00:01.000000Z",
            "bids": [{"price": "100", "qty": "2"}, {"price": "99.5", "qty": "3"}],
            "asks": [{"price": "101", "qty": "4"}, {"price": "101.5", "qty": "5"}],
            "checksum": calculate_book_checksum(bids, asks),
        }],
    }
    assert collector.process_message(snapshot) is True
    updated_bids = {Decimal("100"): Decimal("3"), Decimal("99.5"): Decimal("3")}
    update = {
        "channel": "book", "type": "update",
        "data": [{
            "symbol": "ETH/USD", "timestamp": "2027-01-15T08:00:20.000000Z",
            "bids": [{"price": "100", "qty": "3"}], "asks": [],
            "checksum": calculate_book_checksum(updated_bids, asks),
        }],
    }
    assert collector.process_message(update) is True
    trades = {
        "channel": "trade", "type": "update",
        "data": [
            {"symbol": "ETH/USD", "side": "buy", "price": "100.5", "qty": "2",
             "trade_id": 1, "timestamp": "2027-01-15T08:00:22.000000Z"},
            {"symbol": "ETH/USD", "side": "sell", "price": "99.5", "qty": "1",
             "trade_id": 2, "timestamp": "2027-01-15T08:00:40.000000Z"},
        ],
    }
    assert collector.process_message(trades) is True
    assert collector.flush_due(1_800_000_100) == 1
    bar = db.microstructure_bars("ETH/USD")[0]
    assert bar["trade_open_usd"] == "100.5"
    assert bar["trade_high_usd"] == "100.5"
    assert bar["trade_low_usd"] == "99.5"
    assert bar["trade_close_usd"] == "99.5"
    assert bar["trade_base_volume"] == "3"
    assert bar["midprice_usd"] == "100.5"
    assert bar["book_sample_count"] == 2
    assert bar["first_book_event_ts"] < bar["last_book_event_ts"]
    assert bar["top10_bids"] == [{"price": "100", "qty": "3"}, {"price": "99.5", "qty": "3"}]
    assert bar["top10_asks"] == [{"price": "101", "qty": "4"}, {"price": "101.5", "qty": "5"}]
    assert bar["actual_fills"] is False
    assert bar["order_creation_allowed"] is False
    db.close()


def test_minute_bar_is_partial_without_minimum_book_coverage():
    db = Persistence(":memory:")
    collector = MarketMicrostructureCollector(db, enabled=False)
    bids = {Decimal("100"): Decimal("2")}
    asks = {Decimal("101"): Decimal("3")}
    snapshot = {
        "channel": "book", "type": "snapshot",
        "data": [{
            "symbol": "ETH/USD", "timestamp": "2027-01-15T08:00:01.000000Z",
            "bids": [{"price": "100", "qty": "2"}],
            "asks": [{"price": "101", "qty": "3"}],
            "checksum": calculate_book_checksum(bids, asks),
        }],
    }
    assert collector.process_message(snapshot) is True
    assert collector.flush_due(1_800_000_100) == 1
    assert db.microstructure_bars("ETH/USD")[0]["quality_status"] == "PARTIAL"
    db.close()
