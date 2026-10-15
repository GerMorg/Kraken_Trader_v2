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
