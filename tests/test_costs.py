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


def test_costed_plan_prefers_margin_when_current_rates_are_complete():
    from app.costs import select_costed_plan

    margin = estimate_round_trip_cost(
        is_margin=True, taker_fee_bps=D("20"), spread_bps=D("5"),
        slippage_bps_per_side=D("2"), holding_hours=8, leverage=2,
        margin_open_bps=D("10"), margin_rollover_bps=D("5"),
    )
    spot = estimate_round_trip_cost(
        is_margin=False, taker_fee_bps=D("20"), spread_bps=D("5"),
        slippage_bps_per_side=D("2"), holding_hours=8,
    )
    plan = select_costed_plan(direction="LONG", margin_leverage=2,
                              gross_edge_proxy_bps=D("100"), margin_cost=margin, spot_cost=spot)
    assert plan is not None
    assert plan.product_type == "SPOT_MARGIN"
    assert plan.leverage == 2


def test_long_may_fall_back_to_spot_but_short_must_not():
    from app.costs import select_costed_plan

    margin = estimate_round_trip_cost(
        is_margin=True, taker_fee_bps=D("20"), spread_bps=D("5"),
        slippage_bps_per_side=D("2"), holding_hours=8, leverage=2,
    )
    spot = estimate_round_trip_cost(
        is_margin=False, taker_fee_bps=D("20"), spread_bps=D("5"),
        slippage_bps_per_side=D("2"), holding_hours=8,
    )
    long_plan = select_costed_plan(direction="LONG", margin_leverage=2,
        gross_edge_proxy_bps=D("100"), margin_cost=margin, spot_cost=spot)
    short_plan = select_costed_plan(direction="SHORT", margin_leverage=2,
        gross_edge_proxy_bps=D("100"), margin_cost=margin, spot_cost=spot)
    assert long_plan is not None and long_plan.product_type == "CASH_SPOT"
    assert short_plan is None


def test_costed_plan_rejects_unknown_direction_non_finite_edge_and_negative_net_edge():
    from app.costs import select_costed_plan

    spot = estimate_round_trip_cost(
        is_margin=False, taker_fee_bps=D("20"), spread_bps=D("5"),
        slippage_bps_per_side=D("2"), holding_hours=8,
    )
    margin = estimate_round_trip_cost(
        is_margin=True, taker_fee_bps=D("20"), spread_bps=D("5"),
        slippage_bps_per_side=D("2"), holding_hours=8, leverage=2,
        margin_open_bps=D("10"), margin_rollover_bps=D("10"),
    )
    assert select_costed_plan(direction="HOLD", margin_leverage=2,
        gross_edge_proxy_bps=D("100"), margin_cost=margin, spot_cost=spot) is None
    assert select_costed_plan(direction="LONG", margin_leverage=2,
        gross_edge_proxy_bps=D("NaN"), margin_cost=margin, spot_cost=spot) is None
    assert select_costed_plan(direction="LONG", margin_leverage=2,
        gross_edge_proxy_bps=D("10"), margin_cost=margin, spot_cost=spot) is None
