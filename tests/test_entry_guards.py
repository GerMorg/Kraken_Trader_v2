from decimal import Decimal as D

from app.entry_guards import entry_pair_exposure
from app.execution import signal_decision_id
from app.market import closed_bar_is_fresh
from app.models import Instrument


def instrument() -> Instrument:
    return Instrument(
        "XXBTZUSD", "XBTUSD", "XBT/USD", "XBT", "USD", "online",
        D("0.0001"), 8, 1, (2, 3), (2, 3),
    )


def test_signal_decision_id_is_stable_for_same_bar_and_distinct_across_bars():
    first = signal_decision_id(
        strategy_version="tsm-0.2.0", pair_id="XXBTZUSD",
        signal_bar_ts=1792768800, direction="LONG",
    )
    same_bar_next_cycle = signal_decision_id(
        strategy_version="tsm-0.2.0", pair_id="XXBTZUSD",
        signal_bar_ts=1792768800, direction="long",
    )
    next_bar = signal_decision_id(
        strategy_version="tsm-0.2.0", pair_id="XXBTZUSD",
        signal_bar_ts=1792772400, direction="LONG",
    )
    reverse = signal_decision_id(
        strategy_version="tsm-0.2.0", pair_id="XXBTZUSD",
        signal_bar_ts=1792768800, direction="SHORT",
    )
    assert first == same_bar_next_cycle
    assert first != next_bar
    assert first != reverse


def test_stale_or_invalid_closed_bar_blocks_entry():
    now = 2_000_000_000
    assert closed_bar_is_fresh(now - 3600, now)
    assert not closed_bar_is_fresh(now - 3600 - 3901, now)
    assert not closed_bar_is_fresh(None, now)
    assert not closed_bar_is_fresh(now + 3600, now)
    assert not closed_bar_is_fresh(now - 3600, float("nan"))


def test_existing_same_pair_position_blocks_implicit_reentry():
    result = entry_pair_exposure(
        instrument(),
        open_positions={
            "POSITION-1": {
                "pair": "XXBTZUSD", "type": "buy", "posstatus": "open",
                "vol": "0.1",
            }
        },
        open_orders={"open": {}},
    )
    assert result["blocked"] is True
    assert result["reasons"] == ["SAME_PAIR_POSITION_ALREADY_OPEN"]
    assert result["matching_position_ids"] == ["POSITION-1"]


def test_existing_same_pair_order_blocks_entry_but_other_pair_does_not():
    same_pair = entry_pair_exposure(
        instrument(),
        open_positions={},
        open_orders={"open": {
            "ORDER-1": {"descr": {"pair": "XBT/USD", "type": "buy"}}
        }},
    )
    assert same_pair["blocked"] is True
    assert same_pair["matching_open_order_ids"] == ["ORDER-1"]

    other_pair = entry_pair_exposure(
        instrument(),
        open_positions={},
        open_orders={"open": {
            "ORDER-2": {"descr": {"pair": "ETH/USD", "type": "buy"}}
        }},
    )
    assert other_pair["blocked"] is False


def test_unidentifiable_open_order_blocks_new_entry_fail_closed():
    result = entry_pair_exposure(
        instrument(),
        open_positions={},
        open_orders={"open": {"ORDER-UNKNOWN": {"status": "open"}}},
    )
    assert result["blocked"] is True
    assert "OPEN_ORDER_PAIR_UNIDENTIFIABLE" in result["reasons"]
    assert result["unidentifiable_open_order_ids"] == ["ORDER-UNKNOWN"]
