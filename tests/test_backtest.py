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
