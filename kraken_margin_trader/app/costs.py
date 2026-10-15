from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any

D=Decimal

@dataclass(frozen=True)
class CostEstimate:
    complete: bool
    total_round_trip_bps: Decimal | None
    trade_fee_bps: Decimal | None
    spread_bps: Decimal
    slippage_bps_per_side: Decimal
    margin_open_bps_on_borrowed: Decimal | None
    margin_rollover_bps_on_borrowed: Decimal | None
    rollover_count: int
    quality: str
    reasons: tuple[str,...]

def rollover_count_for_hold(hours: int | Decimal) -> int:
    duration=D(str(hours))
    return int((duration/D(4)).to_integral_value(rounding=ROUND_FLOOR)) if duration>0 else 0

def percent_fee_to_bps(value: Any) -> Decimal | None:
    try:
        rate=D(str(value))
        return rate*D(100) if rate.is_finite() and rate>=0 else None
    except (ArithmeticError,TypeError,ValueError):
        return None

def estimate_round_trip_cost(*, is_margin: bool, taker_fee_bps: Decimal | None,
                             spread_bps: Decimal, slippage_bps_per_side: Decimal,
                             holding_hours: int | Decimal, leverage: int=1,
                             margin_open_bps: Decimal | None=None,
                             margin_rollover_bps: Decimal | None=None) -> CostEstimate:
    reasons=[]
    if taker_fee_bps is None or not taker_fee_bps.is_finite() or taker_fee_bps<0:
        reasons.append("TRADE_FEE_UNKNOWN")
    if not spread_bps.is_finite() or spread_bps<0:
        reasons.append("SPREAD_INVALID")
    if not slippage_bps_per_side.is_finite() or slippage_bps_per_side<0:
        reasons.append("SLIPPAGE_INVALID")
    rollovers=rollover_count_for_hold(holding_hours)
    margin_cost=D(0)
    if is_margin:
        if leverage<2:
            reasons.append("MARGIN_LEVERAGE_INVALID")
        if margin_open_bps is None or margin_open_bps<0:
            reasons.append("MARGIN_OPEN_FEE_UNKNOWN")
        if rollovers and (margin_rollover_bps is None or margin_rollover_bps<0):
            reasons.append("MARGIN_ROLLOVER_FEE_UNKNOWN")
        if margin_open_bps is not None and margin_rollover_bps is not None and leverage>=2:
            borrowed=D(1)-D(1)/D(leverage)
            margin_cost=borrowed*(margin_open_bps+D(rollovers)*margin_rollover_bps)
    complete=not reasons
    total=None
    fee_total=None
    if complete and taker_fee_bps is not None:
        fee_total=taker_fee_bps*D(2)
        total=fee_total+spread_bps+slippage_bps_per_side*D(2)+margin_cost
    return CostEstimate(complete,total,fee_total,spread_bps,slippage_bps_per_side,
                        margin_open_bps,margin_rollover_bps,rollovers,
                        "ESTIMATE_FROM_CURRENT_INPUTS" if complete else "INCOMPLETE",tuple(reasons))

def taker_fee_from_trade_volume(result: dict[str,Any], instrument_keys: tuple[str,...]) -> Decimal | None:
    fees=result.get("fees")
    if not isinstance(fees,dict):
        return None
    for key in instrument_keys:
        entry=fees.get(key)
        if isinstance(entry,dict):
            rate=percent_fee_to_bps(entry.get("fee"))
            if rate is not None:
                return rate
    return None


@dataclass(frozen=True)
class CostedPlan:
    product_type: str
    leverage: int
    cost: CostEstimate
    net_edge_proxy_bps: Decimal
    reason: str


def select_costed_plan(
    *,
    direction: str,
    margin_leverage: int | None,
    gross_edge_proxy_bps: Decimal,
    margin_cost: CostEstimate,
    spot_cost: CostEstimate | None,
) -> CostedPlan | None:
    """Prefer cost-complete margin when justified; only LONG may fall back to cash spot."""
    normalized_direction = direction.upper()
    if normalized_direction not in {"LONG", "SHORT"}:
        return None
    if not gross_edge_proxy_bps.is_finite() or gross_edge_proxy_bps <= 0:
        return None

    if margin_leverage is not None and margin_leverage >= 2:
        if margin_cost.complete and margin_cost.total_round_trip_bps is not None:
            margin_edge = gross_edge_proxy_bps - margin_cost.total_round_trip_bps
            if margin_edge > 0:
                return CostedPlan(
                    "SPOT_MARGIN", margin_leverage, margin_cost, margin_edge,
                    "MARGIN_NET_EDGE_POSITIVE",
                )

    # A sell signal is a short thesis. Never downgrade it to an ordinary spot sell.
    if normalized_direction != "LONG" or spot_cost is None:
        return None
    if not spot_cost.complete or spot_cost.total_round_trip_bps is None:
        return None
    spot_edge = gross_edge_proxy_bps - spot_cost.total_round_trip_bps
    if spot_edge <= 0:
        return None
    reason = (
        "SPOT_FALLBACK_MARGIN_COST_OR_EDGE_UNSUITABLE"
        if margin_leverage is not None
        else "SPOT_ONLY_MARGIN_UNAVAILABLE"
    )
    return CostedPlan("CASH_SPOT", 1, spot_cost, spot_edge, reason)
