from decimal import Decimal as D

from app.models import Instrument, Ticker
from app.position_management import evaluate_position_exits


def _instrument(minimum="0.0001", decimals=8):
    return Instrument(
        pair_id="XXBTZUSD", altname="XBTUSD", wsname="XBT/USD",
        base="XBT", quote="USD", status="online", order_min=D(minimum),
        lot_decimals=decimals, pair_decimals=1,
        leverage_buy=(2, 3), leverage_sell=(2, 3),
    )


def _position(*, side="buy", volume="1", closed="0.2", cost="1000", pair="XXBTZUSD"):
    return {
        "pair": pair, "type": side, "posstatus": "open",
        "vol": volume, "vol_closed": closed, "cost": cost,
        "fee": "1", "margin": "250", "value": "1000", "net": "0",
        "time": 1760000000, "rollovertm": 1760600000,
    }


def _ticker(*, bid="1040", ask="1041"):
    return Ticker("XXBTZUSD", D("1040"), D(bid), D(ask), D("100000"))


def _evaluate(position, ticker, instrument=None):
    return evaluate_position_exits(
        {"open": {"P1": position}},
        {"XXBTZUSD": ticker},
        {"XXBTZUSD": instrument or _instrument()},
        take_profit_pct=D("3.5"),
        take_profit_fraction=D("0.5"),
        stop_loss_pct=D("3.0"),
    )[0]


def test_take_profit_reduces_half_of_remaining_long_position_not_original_volume():
    result = _evaluate(_position(side="buy", volume="1", closed="0.2"), _ticker())
    assert result["status"] == "PARTIAL_TAKE_PROFIT_RECOMMENDED"
    assert result["action"] == "REDUCE_LONG"
    assert result["recommended_volume"] == "0.40000000"
    assert result["original_volume"] == "1"
    assert result["volume_closed"] == "0.2"
    assert result["remaining_volume"] == "0.8"
    assert result["execution_mode"] == "SHADOW_ONLY"
    assert result["fees_and_financing_included"] is False
    assert result["exchange_native_protection"] is False


def test_take_profit_uses_ask_to_cover_a_short_and_reduces_remaining_half():
    position = _position(side="sell", volume="1", closed="0")
    result = _evaluate(position, _ticker(bid="959", ask="960"))
    assert result["status"] == "PARTIAL_TAKE_PROFIT_RECOMMENDED"
    assert result["action"] == "REDUCE_SHORT"
    assert result["recommended_volume"] == "0.50000000"
    assert result["exit_price"] == "960"
    assert D(result["price_move_pct"]) == D("4.0")


def test_stop_loss_closes_all_remaining_long_volume():
    result = _evaluate(_position(side="buy"), _ticker(bid="960", ask="961"))
    assert result["status"] == "RISK_REDUCTION_RECOMMENDED"
    assert result["reason"] == "HARD_STOP_PRICE_MOVE"
    assert result["action"] == "CLOSE_LONG"
    assert result["recommended_volume"] == "0.80000000"


def test_stop_loss_closes_all_remaining_short_volume_using_ask():
    result = _evaluate(_position(side="sell"), _ticker(bid="1039", ask="1040"))
    assert result["status"] == "RISK_REDUCTION_RECOMMENDED"
    assert result["action"] == "CLOSE_SHORT"
    assert result["recommended_volume"] == "0.80000000"
    assert D(result["price_move_pct"]) == D("-4.0")


def test_position_exit_plan_is_no_action_between_configured_thresholds():
    result = _evaluate(_position(side="buy"), _ticker(bid="1010", ask="1011"))
    assert result["status"] == "NO_ACTION"
    assert result["reason"] == "EXIT_TRIGGER_NOT_REACHED"
    assert result["action"] is None


def test_exit_plan_never_rounds_quantities_up_to_exchange_minimum():
    instrument = _instrument(minimum="0.01", decimals=3)
    result = _evaluate(
        _position(side="buy", volume="0.01", closed="0", cost="10"),
        _ticker(bid="1040", ask="1041"),
        instrument,
    )
    assert result["status"] == "BLOCKED_BELOW_MINIMUM"
    assert result["reason"] == "EXIT_QUANTITY_BELOW_EXCHANGE_MINIMUM"
    assert result["action"] is None


def test_unsupported_pair_missing_ticker_and_bad_positions_fail_closed():
    unsupported = _evaluate(_position(pair="XXBTZEUR"), _ticker())
    assert unsupported["status"] == "BLOCKED"
    assert unsupported["reason"] == "EXISTING_POSITION_OUTSIDE_SUPPORTED_USD_UNIVERSE"

    no_ticker = evaluate_position_exits(
        {"open": {"P1": _position()}}, {}, {"XXBTZUSD": _instrument()},
        take_profit_pct=D("3.5"), take_profit_fraction=D("0.5"), stop_loss_pct=D("3.0"),
    )[0]
    assert no_ticker["status"] == "BLOCKED"
    assert no_ticker["reason"] == "CURRENT_POSITION_TICKER_MISSING"

    bad_quantity = _evaluate(_position(volume="NaN"), _ticker())
    assert bad_quantity["status"] == "BLOCKED"
    assert bad_quantity["reason"] == "POSITION_QUANTITY_OR_PRICE_INVALID"
