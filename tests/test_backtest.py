from decimal import Decimal
import pytest

from app.backtest import Bar,BacktestCosts,chronological_split,simulate_trend_trades,summary_metrics

D=Decimal
def bars(count=150):
    return [Bar(i*3600,D(100)+D(i)/D(5),(D(100)+D(i)/D(5))*D("1.01"),
      (D(100)+D(i)/D(5))*D("0.99"),D(100)+D(i)/D(5),D(1000)) for i in range(count)]

def test_chronological_train_validation_test_split():
    train,validation,test=chronological_split(bars())
    assert train[-1].timestamp<validation[0].timestamp<test[0].timestamp
    assert len(train)+len(validation)+len(test)==150

def test_split_rejects_unsorted_bars():
    data=bars(); data[2],data[3]=data[3],data[2]
    with pytest.raises(ValueError,match="STRICTLY_CHRONOLOGICAL"):
        chronological_split(data)

def test_backtest_discloses_scenario_only_scope():
    cost=BacktestCosts(D(26),D(8),D(5),D(20),D(20))
    summary=summary_metrics(simulate_trend_trades(bars(),cost,max_holding_bars=4))
    assert summary["status"]=="SCENARIO_ONLY_NOT_LIVE_VALIDATION"
    assert summary["trade_count"]>=0

def test_backtest_rejects_leverage_over_source_owned_cap():
    cost=BacktestCosts(D(26),D(8),D(5),D(20),D(20),leverage=4)
    with pytest.raises(ValueError,match="LEVERAGE_OUTSIDE"):
        simulate_trend_trades(bars(),cost)


def test_backtest_uses_full_extended_amount_for_margin_fees_at_each_leverage():
    data = bars()
    level_two = simulate_trend_trades(
        data, BacktestCosts(D(26), D(8), D(5), D(20), D(20), leverage=2),
        max_holding_bars=4,
    )
    level_three = simulate_trend_trades(
        data, BacktestCosts(D(26), D(8), D(5), D(20), D(20), leverage=3),
        max_holding_bars=4,
    )
    assert level_two and level_three
    assert len(level_two) == len(level_three)
    assert [item.total_cost_pct for item in level_two] == [
        item.total_cost_pct for item in level_three
    ]
    assert [item.net_return_pct for item in level_two] == [
        item.net_return_pct for item in level_three
    ]


def test_backtest_portfolio_metrics_use_equity_weighted_pnl_not_raw_position_return():
    equity = D("10000")
    trades = simulate_trend_trades(
        bars(), BacktestCosts(D(26), D(8), D(5), D(20), D(20)),
        equity_usd=equity, position_pct=D("10"), max_holding_bars=4,
    )
    assert trades
    equity_before_trade = equity
    for trade in trades:
        assert trade.notional_usd <= equity_before_trade * D("0.10")
        assert abs(trade.net_pnl_usd - trade.notional_usd * trade.net_return_pct / D(100)) < D("1e-24")
        assert trade.equity_return_pct == trade.net_pnl_usd / equity_before_trade * D(100)
        equity_before_trade += trade.net_pnl_usd
    metrics = summary_metrics(trades)
    assert metrics["total_costs_usd"] == sum((t.total_cost_usd for t in trades), D(0))
    assert metrics["net_pnl_usd"] == sum((t.net_pnl_usd for t in trades), D(0))
    assert "expected_shortfall_95_equity_return_pct" in metrics
    assert "max_consecutive_loss_trades" in metrics
    assert "sortino_trade_proxy" in metrics
    assert "max_recovery_trades" in metrics
    assert "long_net_pnl_usd" in metrics
    assert "short_net_pnl_usd" in metrics


def test_backtest_rejects_nan_costs_invalid_ohlcv_and_excessive_participation():
    bad_costs = BacktestCosts(D("NaN"), D(8), D(5), D(20), D(20))
    with pytest.raises(ValueError, match="NON_FINITE_SIMULATOR_INPUT"):
        simulate_trend_trades(bars(), bad_costs)

    invalid_bars = bars()
    invalid_bars[10] = Bar(10 * 3600, D(100), D(90), D(95), D(101), D(1000))
    with pytest.raises(ValueError, match="INVALID_OHLCV_BAR"):
        simulate_trend_trades(invalid_bars, BacktestCosts(D(26), D(8), D(5), D(20), D(20)))

    participation = BacktestCosts(
        D(26), D(8), D(5), D(20), D(20), max_volume_participation=D("1.01")
    )
    with pytest.raises(ValueError, match="INVALID_SIMULATOR_LIQUIDITY_LIMITS"):
        simulate_trend_trades(bars(), participation)


def test_backtest_does_not_double_charge_slippage_already_applied_to_fill_prices():
    costs = BacktestCosts(
        taker_fee_bps_per_side=D("26"),
        spread_bps_round_trip=D("8"),
        slippage_bps_per_side=D("5"),
        margin_open_bps_on_extended_amount=D("20"),
        margin_rollover_bps_on_extended_amount=D("20"),
        holding_hours=8,
        leverage=2,
    )
    trades = simulate_trend_trades(bars(), costs, max_holding_bars=4)
    assert trades
    # 52 bps trading fees + 8 bps spread + 20 bps open + 2 * 20 bps rollover.
    # The 5 bps/side slippage is already reflected in entry/exit execution prices.
    for trade in trades:
        assert trade.total_cost_pct == D("1.20")
        direction_sign = D(1) if trade.direction == "LONG" else D(-1)
        assert trade.gross_pnl_usd == (
            trade.quantity * (trade.exit - trade.entry) * direction_sign
        )
        assert trade.gross_return_pct == trade.gross_pnl_usd / trade.notional_usd * D(100)
        assert trade.net_return_pct == trade.net_pnl_usd / trade.notional_usd * D(100)
        assert trade.net_pnl_usd == trade.gross_pnl_usd - trade.total_cost_usd



def order_books(data, *, default_depth=D("1000"), shallow_timestamp=None):
    from app.orderbook_simulator import BookLevel, OrderBookSnapshot

    snapshots = []
    for bar in data:
        if bar.timestamp <= 0:
            continue
        if shallow_timestamp == bar.timestamp:
            bid_depth = D("0.01")
            second_bid_depth = D("0.01")
        else:
            bid_depth = default_depth
            second_bid_depth = default_depth
        mid = bar.close
        snapshots.append(
            OrderBookSnapshot(
                timestamp=bar.timestamp,
                bids=(
                    BookLevel(mid * D("0.9995"), bid_depth),
                    BookLevel(mid * D("0.999"), second_bid_depth),
                ),
                asks=(
                    BookLevel(mid * D("1.0005"), default_depth),
                    BookLevel(mid * D("1.001"), default_depth),
                ),
            )
        )
    return snapshots


def test_orderbook_replay_executes_trend_entries_and_exits_on_visible_depth():
    data = bars()
    snapshots = order_books(data)
    replay = replay_orderbook(data, snapshots, leverage=1)

    assert replay.status == "ORDERBOOK_SCENARIO_ONLY"
    assert replay.issues == ()
    assert replay.trades
    assert all(trade.notional_usd > 0 for trade in replay.trades)
    assert all(trade.total_cost_usd > 0 for trade in replay.trades)
    assert all(trade.quantity > 0 for trade in replay.trades)
    assert all(trade.entry_time < trade.exit_time for trade in replay.trades)


def test_orderbook_replay_blocks_margin_net_pnl_without_financing_evidence():
    data = bars()
    replay = replay_orderbook(data, order_books(data), leverage=2)

    assert replay.status == "INCOMPLETE_REQUIRES_REVIEW"
    assert replay.trades == ()
    assert any(issue.reason == "NET_PNL_UNVERIFIABLE_MARGIN_FINANCING" for issue in replay.issues)


def test_orderbook_replay_accepts_explicit_financing_in_the_usd_quote_currency():
    data = bars()
    snapshots = order_books(data)
    funding = {snapshot.timestamp: D("1") for snapshot in snapshots}
    replay = replay_orderbook(data, snapshots, leverage=2, funding=funding)

    assert replay.status == "ORDERBOOK_SCENARIO_ONLY"
    assert replay.issues == ()
    assert replay.trades
    assert all(trade.total_cost_usd > 0 for trade in replay.trades)


def test_orderbook_replay_fails_closed_if_exit_book_is_missing_or_stale():
    data = bars()
    missing_exit_timestamp = data[76].timestamp
    snapshots = [
        snapshot for snapshot in order_books(data)
        if snapshot.timestamp != missing_exit_timestamp
    ]
    replay = replay_orderbook(data, snapshots, leverage=1)

    assert replay.status == "INCOMPLETE_REQUIRES_REVIEW"
    assert replay.trades == ()
    assert any(
        issue.reason == "EXIT_BOOK_MISSING_OR_STALE_POSITION_UNRESOLVED"
        for issue in replay.issues
    )


def test_orderbook_replay_does_not_mark_a_partially_exited_lot_closed():
    data = bars()
    partial_timestamp = data[76].timestamp
    snapshots = order_books(data, shallow_timestamp=partial_timestamp)
    replay = replay_orderbook(data, snapshots, leverage=1)

    assert replay.status == "INCOMPLETE_REQUIRES_REVIEW"
    assert replay.trades == ()
    assert any(issue.reason == "EXIT_PARTIAL_POSITION_REMAINS_OPEN" for issue in replay.issues)


def replay_orderbook(data, snapshots, *, leverage, funding=None):
    from app.backtest import replay_trend_strategy_on_order_books

    costs = BacktestCosts(
        D("26"),
        D("0"),
        D("0"),
        D("0"),
        D("0"),
        holding_hours=4,
        leverage=leverage,
        max_volume_participation=D("0.01"),
        minimum_order_usd=D("5"),
    )
    return replay_trend_strategy_on_order_books(
        data,
        costs,
        snapshots,
        financing_cost_quote_by_entry_timestamp=funding,
        quote_currency="USD",
        equity_usd=D("10000"),
        position_pct=D("10"),
        max_holding_bars=4,
        max_book_age_seconds=60,
    )
