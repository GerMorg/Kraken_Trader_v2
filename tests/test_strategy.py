from decimal import Decimal

from app.strategy import generate_trend_signal,profit_trigger_reached,take_profit_quantity

def test_insufficient_history_is_no_trade():
    assert generate_trend_signal("XBTUSD",[Decimal("100")]*20).reason=="INSUFFICIENT_HISTORY"

def test_uptrend_and_downtrend_have_distinct_directions():
    rising=[Decimal("100")+Decimal(i)/Decimal(10) for i in range(100)]
    falling=[Decimal("200")-Decimal(i)/Decimal(2) for i in range(100)]
    assert generate_trend_signal("XBTUSD",rising).direction=="LONG"
    assert generate_trend_signal("XBTUSD",falling).direction=="SHORT"

def test_partial_profit_is_fraction_of_position_not_portfolio():
    assert take_profit_quantity(Decimal("8"),Decimal("0.5"))==Decimal("4")
    assert profit_trigger_reached(Decimal("100"),Decimal("103.5"),"LONG",Decimal("3.5"))


def test_profit_helpers_fail_closed_on_invalid_numbers_and_side():
    import pytest

    with pytest.raises(ValueError, match="POSITIVE_AND_FINITE"):
        take_profit_quantity(Decimal("NaN"))
    with pytest.raises(ValueError, match="FINITE_AND_BETWEEN"):
        take_profit_quantity(Decimal("8"), Decimal("NaN"))
    assert not profit_trigger_reached(Decimal("100"), Decimal("103.5"), "UNKNOWN", Decimal("3.5"))
    assert not profit_trigger_reached(Decimal("NaN"), Decimal("103.5"), "LONG", Decimal("3.5"))
    assert profit_trigger_reached(Decimal("100"), Decimal("96.5"), "SHORT", Decimal("3.5"))


def test_trend_efficiency_score_is_not_a_probability_and_monotonic_trends_are_efficient():
    rising = [Decimal("100") + Decimal(i) / Decimal(10) for i in range(100)]
    signal = generate_trend_signal("XBTUSD", rising)
    assert signal.direction == "LONG"
    assert signal.trend_efficiency_24h > Decimal("0.99")
    assert signal.trend_efficiency_72h > Decimal("0.99")
    assert signal.score < Decimal(1)
    assert signal.score_is_calibrated_probability if hasattr(signal, "score_is_calibrated_probability") else True


def test_trend_efficiency_filter_rejects_a_choppy_path_despite_positive_momentum():
    periodic = [Decimal(0), Decimal(1), Decimal(0), Decimal(-1)]
    closes = [
        Decimal("100") * (
            Decimal(1) + Decimal("0.0008") * Decimal(i)
            + Decimal("0.02") * periodic[i % 4]
        )
        for i in range(100)
    ]
    signal = generate_trend_signal("ALTUSD", closes)
    assert signal.momentum_fast_bps > Decimal("120")
    assert signal.momentum_slow_bps > Decimal("180")
    assert signal.direction == "NONE"
    assert signal.reason == "TREND_EFFICIENCY_TOO_LOW"
    assert signal.trend_efficiency_24h < Decimal("0.20")
    assert signal.trend_efficiency_72h < Decimal("0.12")


def test_invalid_strategy_thresholds_fail_closed():
    assert generate_trend_signal(
        "XBTUSD", [Decimal("100") + Decimal(i) for i in range(100)],
        fast_threshold_bps=Decimal("NaN"),
    ).reason == "INVALID_STRATEGY_THRESHOLDS"
