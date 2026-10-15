from decimal import Decimal

from app.costs import estimate_round_trip_cost,rollover_count_for_hold

D=Decimal

def test_unknown_margin_funding_rates_prevent_complete_cost_estimate():
    cost=estimate_round_trip_cost(is_margin=True,taker_fee_bps=D("40"),spread_bps=D("5"),
        slippage_bps_per_side=D("2"),holding_hours=8,leverage=2)
    assert not cost.complete and cost.total_round_trip_bps is None
    assert "MARGIN_OPEN_FEE_UNKNOWN" in cost.reasons

def test_spot_round_trip_adds_entry_exit_fee_full_spread_and_both_slippages():
    cost=estimate_round_trip_cost(is_margin=False,taker_fee_bps=D("20"),spread_bps=D("5"),
        slippage_bps_per_side=D("2"),holding_hours=4)
    assert cost.complete and cost.total_round_trip_bps==D("49")

def test_rollover_count_uses_four_hour_periods():
    assert rollover_count_for_hold(3)==0
    assert rollover_count_for_hold(4)==1
    assert rollover_count_for_hold(8)==2