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
    for trade in trades:
        assert trade.notional_usd <= equity * D("0.10")
        assert trade.net_pnl_usd == trade.notional_usd * trade.net_return_pct / D(100)
        assert trade.equity_return_pct == trade.net_pnl_usd / equity * D(100)
    metrics = summary_metrics(trades)
    assert metrics["total_costs_usd"] == sum((t.total_cost_usd for t in trades), D(0))
    assert metrics["net_pnl_usd"] == sum((t.net_pnl_usd for t in trades), D(0))
    assert "expected_shortfall_95_equity_return_pct" in metrics
    assert "max_consecutive_loss_trades" in metrics


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
