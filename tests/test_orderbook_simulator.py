from decimal import Decimal as D

import pytest

from app.orderbook_simulator import (
    BookLevel,
    OrderBookSnapshot,
    simulate_order_book_execution,
)


def book() -> OrderBookSnapshot:
    return OrderBookSnapshot(
        timestamp=1_800_000_000,
        bids=(BookLevel(D("99"), D("2")), BookLevel(D("98"), D("5"))),
        asks=(BookLevel(D("101"), D("1")), BookLevel(D("102"), D("4"))),
    )


def test_market_buy_walks_ask_depth_and_reports_vwap_fees_and_slippage():
    result = simulate_order_book_execution(
        book(), side="BUY", quantity=D("3"), fee_bps=D("20")
    )

    assert result.status == "FILLED"
    assert result.complete
    assert result.filled_quantity == D("3")
    assert result.remaining_quantity == D("0")
    assert result.quote_amount == D("305")
    assert result.average_price == D("305") / D("3")
    assert result.fee_quote == D("0.61")
    assert result.levels_consumed == 2
    assert result.slippage_bps == (D("305") / D("3") / D("101") - D("1")) * D("10000")
    assert result.spread_bps > 0
    assert result.snapshot_timestamp == 1_800_000_000


def test_market_sell_walks_bid_depth_in_best_price_order():
    result = simulate_order_book_execution(
        book(), side="SELL", quantity=D("3"), fee_bps=D("10")
    )

    assert result.status == "FILLED"
    assert result.quote_amount == D("99") * D("2") + D("98")
    assert result.average_price == D("296") / D("3")
    assert result.fee_quote == D("0.296")
    assert result.slices[0].price == D("99")
    assert result.slices[1].price == D("98")
    assert result.slippage_bps is not None and result.slippage_bps > 0


def test_insufficient_depth_returns_partial_fill_without_inventing_liquidity():
    result = simulate_order_book_execution(
        book(), side="BUY", quantity=D("10"), fee_bps=D("0")
    )

    assert result.status == "PARTIALLY_FILLED"
    assert result.filled_quantity == D("5")
    assert result.remaining_quantity == D("5")
    assert result.quote_amount == D("101") + D("102") * D("4")
    assert not result.complete


def test_limit_price_prevents_execution_beyond_price_bound():
    result = simulate_order_book_execution(
        book(), side="BUY", quantity=D("5"), fee_bps=D("0"), limit_price=D("101")
    )

    assert result.status == "PARTIALLY_FILLED"
    assert result.filled_quantity == D("1")
    assert result.remaining_quantity == D("4")
    assert all(item.price <= D("101") for item in result.slices)


def test_non_marketable_limit_order_is_unfilled():
    result = simulate_order_book_execution(
        book(), side="SELL", quantity=D("1"), fee_bps=D("0"), limit_price=D("100")
    )

    assert result.status == "UNFILLED"
    assert result.average_price is None
    assert result.slippage_bps is None
    assert result.fee_quote == D("0")


def test_per_level_participation_cap_is_applied_to_each_visible_level():
    result = simulate_order_book_execution(
        book(),
        side="BUY",
        quantity=D("3"),
        fee_bps=D("0"),
        max_level_participation=D("0.5"),
    )

    assert result.status == "PARTIALLY_FILLED"
    assert result.filled_quantity == D("2.5")
    assert result.remaining_quantity == D("0.5")
    assert result.slices[0].quantity == D("0.5")
    assert result.slices[1].quantity == D("2")


@pytest.mark.parametrize(
    ("side", "quantity", "fee_bps", "participation", "limit_price", "error"),
    [
        ("HOLD", D("1"), D("0"), D("1"), None, "ORDER_SIDE_INVALID"),
        ("BUY", D("0"), D("0"), D("1"), None, "ORDER_QUANTITY_MUST_BE_POSITIVE_AND_FINITE"),
        ("BUY", D("1"), D("-1"), D("1"), None, "ORDER_FEE_BPS_INVALID"),
        ("BUY", D("1"), D("0"), D("0"), None, "ORDER_BOOK_PARTICIPATION_OUT_OF_RANGE"),
        ("BUY", D("1"), D("0"), D("1.01"), None, "ORDER_BOOK_PARTICIPATION_OUT_OF_RANGE"),
        ("BUY", D("1"), D("0"), D("1"), D("NaN"), "ORDER_LIMIT_PRICE_INVALID"),
    ],
)
def test_execution_rejects_invalid_inputs(side, quantity, fee_bps, participation, limit_price, error):
    with pytest.raises(ValueError, match=error):
        simulate_order_book_execution(
            book(),
            side=side,
            quantity=quantity,
            fee_bps=fee_bps,
            max_level_participation=participation,
            limit_price=limit_price,
        )


def test_order_book_rejects_invalid_ordering_and_crossed_quotes():
    with pytest.raises(ValueError, match="BID_LEVELS_NOT_STRICTLY_DESCENDING"):
        OrderBookSnapshot(
            timestamp=10,
            bids=(BookLevel(D("99"), D("1")), BookLevel(D("100"), D("1"))),
            asks=(BookLevel(D("101"), D("1")),),
        )

    with pytest.raises(ValueError, match="ORDER_BOOK_CROSSED_OR_LOCKED"):
        OrderBookSnapshot(
            timestamp=10,
            bids=(BookLevel(D("101"), D("1")),),
            asks=(BookLevel(D("101"), D("1")),),
        )


def test_non_finite_book_level_is_rejected():
    with pytest.raises(ValueError, match="ORDER_BOOK_LEVEL_MUST_BE_POSITIVE_AND_FINITE"):
        BookLevel(D("NaN"), D("1"))
