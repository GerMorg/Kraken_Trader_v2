from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Mapping

D=Decimal

@dataclass(frozen=True)
class CostEstimate:
    complete: bool
    total_round_trip_bps: Decimal | None
    trade_fee_bps: Decimal | None
    spread_bps: Decimal
    slippage_bps_per_side: Decimal
    margin_open_bps_on_extended_amount: Decimal | None
    margin_rollover_bps_on_extended_amount: Decimal | None
    rollover_count: int
    quality: str
    reasons: tuple[str,...]

def cost_diagnostic(cost: CostEstimate | None) -> dict[str, Any] | None:
    """Expose known cost components without treating missing margin inputs as zero."""
    if cost is None:
        return None
    return {
        "complete": cost.complete,
        "total_round_trip_bps": str(cost.total_round_trip_bps)
        if cost.total_round_trip_bps is not None else None,
        "trade_fee_round_trip_bps": str(cost.trade_fee_bps)
        if cost.trade_fee_bps is not None else None,
        "spread_round_trip_bps": str(cost.spread_bps)
        if cost.spread_bps.is_finite() else None,
        "slippage_round_trip_bps": str(cost.slippage_bps_per_side * D(2))
        if cost.slippage_bps_per_side.is_finite() else None,
        "margin_open_fee_bps_on_extended_amount": str(cost.margin_open_bps_on_extended_amount)
        if cost.margin_open_bps_on_extended_amount is not None
        and cost.margin_open_bps_on_extended_amount.is_finite() else None,
        "margin_rollover_fee_bps_per_event_on_extended_amount": (
            str(cost.margin_rollover_bps_on_extended_amount)
            if cost.margin_rollover_bps_on_extended_amount is not None
            and cost.margin_rollover_bps_on_extended_amount.is_finite() else None
        ),
        "rollover_count": cost.rollover_count,
        "quality": cost.quality,
        "reasons": list(cost.reasons),
    }


def rollover_count_for_hold(hours: int | Decimal) -> int:
    try:
        duration = D(str(hours))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError("HOLDING_HOURS_INVALID") from exc
    if not duration.is_finite() or duration < 0:
        raise ValueError("HOLDING_HOURS_INVALID")
    return int((duration / D(4)).to_integral_value(rounding=ROUND_FLOOR))

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
    try:
        rollovers = rollover_count_for_hold(holding_hours)
    except (ArithmeticError, TypeError, ValueError):
        rollovers = 0
        reasons.append("HOLDING_HOURS_INVALID")
    margin_cost = D(0)
    if is_margin:
        if leverage < 2:
            reasons.append("MARGIN_LEVERAGE_INVALID")
        if margin_open_bps is None:
            reasons.append("MARGIN_OPEN_FEE_UNKNOWN")
        elif not margin_open_bps.is_finite() or margin_open_bps < 0:
            reasons.append("MARGIN_OPEN_FEE_INVALID")
        else:
            # Kraken's fee basis is the amount extended; account collateral does
            # not reduce it. Until the loan amount is verified, use full notional.
            margin_cost += margin_open_bps

        if margin_rollover_bps is None:
            if rollovers:
                reasons.append("MARGIN_ROLLOVER_FEE_UNKNOWN")
        elif not margin_rollover_bps.is_finite() or margin_rollover_bps < 0:
            reasons.append("MARGIN_ROLLOVER_FEE_INVALID")
        elif rollovers:
            margin_cost += D(rollovers) * margin_rollover_bps
    complete=not reasons
    total=None
    fee_total=(
        taker_fee_bps*D(2)
        if taker_fee_bps is not None and taker_fee_bps.is_finite() and taker_fee_bps>=0
        else None
    )
    if complete and fee_total is not None:
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

def maker_fee_from_trade_volume(
    result: dict[str, Any], instrument_keys: tuple[str, ...]
) -> Decimal | None:
    """Read a pair-specific maker rate only when Kraken explicitly returns it."""
    fees_maker = result.get("fees_maker")
    if not isinstance(fees_maker, dict):
        return None
    for key in instrument_keys:
        entry = fees_maker.get(key)
        if isinstance(entry, dict):
            rate = percent_fee_to_bps(entry.get("fee"))
            if rate is not None:
                return rate
    return None


def trade_volume_fee_diagnostics(
    result: Mapping[str, Any] | None,
    pair_aliases: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    """Return bounded, pair-specific Kraken TradeVolume fee evidence.

    Maker rates are references only, not evidence that an order would execute
    passively or receive maker pricing. The complete schedule payload is omitted.
    """
    payload = result if isinstance(result, Mapping) else {}
    by_pair: dict[str, dict[str, str | None]] = {}
    taker_rates: list[Decimal] = []
    maker_rates: list[Decimal] = []
    for pair, aliases in sorted(pair_aliases.items()):
        keys = tuple(str(alias) for alias in aliases if str(alias).strip())
        taker = taker_fee_from_trade_volume(dict(payload), keys)
        maker = maker_fee_from_trade_volume(dict(payload), keys)
        by_pair[str(pair)] = {
            "taker_fee_bps": str(taker) if taker is not None else None,
            "maker_fee_bps": str(maker) if maker is not None else None,
        }
        if taker is not None:
            taker_rates.append(taker)
        if maker is not None:
            maker_rates.append(maker)

    requested_pairs = len(by_pair)
    if not payload:
        status = "NO_FEE_RESPONSE"
    elif taker_rates and len(taker_rates) == requested_pairs:
        status = "AVAILABLE"
    elif taker_rates:
        status = "PARTIAL"
    else:
        status = "NO_PAIR_FEES_RETURNED"

    raw_volume = payload.get("volume")
    volume_value: str | None = None
    if raw_volume is not None:
        try:
            parsed_volume = Decimal(str(raw_volume))
            if parsed_volume.is_finite() and parsed_volume >= 0:
                volume_value = str(parsed_volume)
        except (ArithmeticError, TypeError, ValueError):
            pass
    currency = payload.get("currency")
    safe_currency = (
        str(currency)[:16] if isinstance(currency, str) and currency.strip() else None
    )
    return {
        "status": status,
        "source": "KRAKEN_PRIVATE_TRADE_VOLUME",
        "fee_schedule_requested": True,
        "fee_schedule_present": bool(payload.get("schedules")),
        "pair_count": requested_pairs,
        "taker_fee_pair_count": len(taker_rates),
        "maker_fee_pair_count": len(maker_rates),
        "taker_fee_bps_min": str(min(taker_rates)) if taker_rates else None,
        "taker_fee_bps_max": str(max(taker_rates)) if taker_rates else None,
        "maker_fee_bps_min": str(min(maker_rates)) if maker_rates else None,
        "maker_fee_bps_max": str(max(maker_rates)) if maker_rates else None,
        "account_volume_30d": volume_value,
        "account_volume_currency": safe_currency,
        "fees_by_pair": by_pair,
        "maker_rates_are_fill_assumptions": False,
        "raw_schedule_payload_logged": False,
    }


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
