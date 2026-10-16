from decimal import Decimal as D

import pytest

from app.orderbook_simulator import (
    BookLevel,
    MarginCostEvidence,
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



def test_long_round_trip_accounts_for_execution_prices_and_two_fee_rates():
    from app.orderbook_simulator import simulate_order_book_round_trip

    exit_book = OrderBookSnapshot(
        timestamp=1_800_000_300,
        bids=(BookLevel(D("103"), D("5")),),
        asks=(BookLevel(D("104"), D("5")),),
    )
    result = simulate_order_book_round_trip(
        book(),
        exit_book,
        direction="LONG",
        requested_notional_quote=D("200"),
        entry_fee_bps=D("10"),
        exit_fee_bps=D("20"),
        quote_currency="usd",
    )

    assert result.position_status == "CLOSED"
    assert result.entered_quantity == D("2")
    assert result.closed_quantity == D("2")
    assert result.remaining_quantity == D("0")
    assert result.gross_realized_pnl_quote == D("3")
    assert result.mid_market_gross_pnl_quote == D("7")
    assert result.spread_cost_quote == D("3")
    assert result.slippage_impact_quote == D("1")
    assert result.spread_slippage_cost_quote == D("4")
    assert result.mid_market_gross_pnl_quote - result.spread_slippage_cost_quote == result.gross_realized_pnl_quote
    assert result.trading_fees_quote == D("0.203") + D("0.412")
    assert result.net_realized_pnl_quote == D("3") - D("0.203") - D("0.412")
    assert result.quote_currency == "USD"


def test_margin_round_trip_refuses_net_pnl_without_verified_financing_cost():
    from app.orderbook_simulator import simulate_order_book_round_trip

    exit_book = OrderBookSnapshot(
        timestamp=1_800_000_300,
        bids=(BookLevel(D("95"), D("5")),),
        asks=(BookLevel(D("96"), D("5")),),
    )
    result = simulate_order_book_round_trip(
        book(),
        exit_book,
        direction="SHORT",
        requested_notional_quote=D("200"),
        entry_fee_bps=D("10"),
        exit_fee_bps=D("20"),
        quote_currency="USD",
        is_margin=True,
    )

    assert result.position_status == "CLOSED_COST_INCOMPLETE"
    assert result.gross_realized_pnl_quote == D("6")
    assert result.net_realized_pnl_quote is None
    assert result.margin_cost_quote is None


def test_margin_round_trip_subtracts_explicit_financing_in_same_quote_currency():
    from app.orderbook_simulator import simulate_order_book_round_trip

    exit_book = OrderBookSnapshot(
        timestamp=1_800_000_300,
        bids=(BookLevel(D("95"), D("5")),),
        asks=(BookLevel(D("96"), D("5")),),
    )
    result = simulate_order_book_round_trip(
        book(),
        exit_book,
        direction="SHORT",
        requested_notional_quote=D("200"),
        entry_fee_bps=D("10"),
        exit_fee_bps=D("20"),
        quote_currency="USD",
        is_margin=True,
        margin_cost_evidence=MarginCostEvidence(
            quote_currency="USD",
            opening_fee_quote=D("0.2"),
            opening_fee_source="synthetic-research-fixture",
            opening_fee_evidence_id="OPEN-FEE-TEST-1",
            opening_fee_timestamp=1_800_000_000,
            rollover_fee_quote=D("0.8"),
            rollover_fee_source="synthetic-research-fixture",
            rollover_fee_evidence_id="ROLLOVER-FEE-TEST-1",
            rollover_fee_timestamp=1_800_000_300,
            quality="HISTORICAL_SIMULATION",
        ),
    )

    assert result.position_status == "CLOSED"
    assert result.gross_realized_pnl_quote == D("6")
    assert result.mid_market_gross_pnl_quote == D("9")
    assert result.spread_cost_quote == D("3")
    assert result.slippage_impact_quote == D("0")
    assert result.spread_slippage_cost_quote == D("3")
    assert result.margin_cost_quote == D("1")
    assert result.margin_cost_evidence_quality == "HISTORICAL_SIMULATION"
    assert result.margin_cost_evidence_ids == ("OPEN-FEE-TEST-1", "ROLLOVER-FEE-TEST-1")
    assert result.margin_cost_sources == (
        "synthetic-research-fixture",
        "synthetic-research-fixture",
    )
    assert result.trading_fees_quote == D("0.198") + D("0.384")
    assert result.net_realized_pnl_quote == D("6") - D("0.198") - D("0.384") - D("1")


def test_partial_exit_keeps_unclosed_quantity_explicit():
    from app.orderbook_simulator import simulate_order_book_round_trip

    exit_book = OrderBookSnapshot(
        timestamp=1_800_000_300,
        bids=(BookLevel(D("103"), D("0.5")),),
        asks=(BookLevel(D("104"), D("0.5")),),
    )
    result = simulate_order_book_round_trip(
        book(),
        exit_book,
        direction="LONG",
        requested_notional_quote=D("200"),
        entry_fee_bps=D("0"),
        exit_fee_bps=D("0"),
        quote_currency="USD",
    )

    assert result.position_status == "PARTIALLY_CLOSED"
    assert result.closed_quantity == D("0.5")
    assert result.remaining_quantity == D("1.5")
    assert result.gross_realized_pnl_quote == D("0.75")
    assert result.mid_market_gross_pnl_quote == D("1.75")
    assert result.spread_cost_quote == D("0.75")
    assert result.slippage_impact_quote == D("0.25")
    assert result.net_realized_pnl_quote == D("0.75")


def test_round_trip_rejects_non_chronological_order_books():
    from app.orderbook_simulator import simulate_order_book_round_trip

    with pytest.raises(ValueError, match="ROUND_TRIP_BOOKS_NOT_CHRONOLOGICAL"):
        simulate_order_book_round_trip(
            book(),
            book(),
            direction="LONG",
            requested_notional_quote=D("100"),
            entry_fee_bps=D("0"),
            exit_fee_bps=D("0"),
            quote_currency="USD",
        )



def test_vwap_rounding_noise_does_not_reject_a_valid_single_level_buy():
    snapshot = OrderBookSnapshot(
        timestamp=1_800_000_000,
        bids=(BookLevel(D("100"), D("1000")),),
        asks=(BookLevel(D("101"), D("1000")),),
    )
    quantity = D("1000") / D("101")
    result = simulate_order_book_execution(
        snapshot, side="BUY", quantity=quantity, fee_bps=D("0")
    )

    assert result.status == "FILLED"
    assert result.slippage_bps is not None
    assert result.slippage_bps >= 0
    assert result.average_price is not None
    assert abs(result.average_price - D("101")) < D("1e-18")


def test_margin_cost_evidence_rejects_currency_mismatch_and_untraced_zero_rollover():
    from app.orderbook_simulator import simulate_order_book_round_trip

    exit_book = OrderBookSnapshot(
        timestamp=1_800_000_300,
        bids=(BookLevel(D("95"), D("5")),),
        asks=(BookLevel(D("96"), D("5")),),
    )
    with pytest.raises(ValueError, match="MARGIN_COST_EVIDENCE_REFERENCE_REQUIRED"):
        MarginCostEvidence(
            quote_currency="USD",
            opening_fee_quote=D("0.2"),
            opening_fee_source="exchange-ledger",
            opening_fee_evidence_id="OPEN-1",
            opening_fee_timestamp=1_800_000_000,
            rollover_fee_quote=D("0"),
            rollover_fee_source="",
            rollover_fee_evidence_id="",
            rollover_fee_timestamp=1_800_000_300,
            quality="EXCHANGE_VERIFIED",
        )

    mismatched = MarginCostEvidence(
        quote_currency="USDC",
        opening_fee_quote=D("0.2"),
        opening_fee_source="synthetic-test",
        opening_fee_evidence_id="OPEN-USDC",
        opening_fee_timestamp=1_800_000_000,
        rollover_fee_quote=D("0"),
        rollover_fee_source="synthetic-test",
        rollover_fee_evidence_id="NO-ROLLOVER-USDC",
        rollover_fee_timestamp=1_800_000_300,
        quality="RESEARCH_ESTIMATE",
    )
    with pytest.raises(ValueError, match="MARGIN_COST_QUOTE_CURRENCY_MISMATCH"):
        simulate_order_book_round_trip(
            book(),
            exit_book,
            direction="SHORT",
            requested_notional_quote=D("200"),
            entry_fee_bps=D("10"),
            exit_fee_bps=D("20"),
            quote_currency="USD",
            is_margin=True,
            margin_cost_evidence=mismatched,
        )


def test_margin_cost_evidence_rejects_rollover_after_exit():
    from app.orderbook_simulator import simulate_order_book_round_trip

    exit_book = OrderBookSnapshot(
        timestamp=1_800_000_300,
        bids=(BookLevel(D("95"), D("5")),),
        asks=(BookLevel(D("96"), D("5")),),
    )
    future_cost = MarginCostEvidence(
        quote_currency="USD",
        opening_fee_quote=D("0.2"),
        opening_fee_source="synthetic-test",
        opening_fee_evidence_id="OPEN-USD",
        opening_fee_timestamp=1_800_000_000,
        rollover_fee_quote=D("0.8"),
        rollover_fee_source="synthetic-test",
        rollover_fee_evidence_id="FUTURE-ROLLOVER-USD",
        rollover_fee_timestamp=1_800_000_301,
        quality="RESEARCH_ESTIMATE",
    )
    with pytest.raises(ValueError, match="MARGIN_ROLLOVER_FEE_AFTER_EXIT"):
        simulate_order_book_round_trip(
            book(),
            exit_book,
            direction="SHORT",
            requested_notional_quote=D("200"),
            entry_fee_bps=D("10"),
            exit_fee_bps=D("20"),
            quote_currency="USD",
            is_margin=True,
            margin_cost_evidence=future_cost,
        )
