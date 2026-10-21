from __future__ import annotations

from decimal import Decimal
import math
from statistics import pstdev
from typing import Sequence

from app.models import TrendSignal

D = Decimal

# These are fixed research guardrails, not parameters fitted to the current result set.
MIN_FAST_EFFICIENCY = D("0.20")
MIN_SLOW_EFFICIENCY = D("0.12")
MIN_STOP_DISTANCE_PCT = D("1.5")
MAX_STOP_DISTANCE_PCT = D("5.0")


def _efficiency_ratio(prices: Sequence[Decimal]) -> Decimal:
    """Return absolute net price movement divided by total absolute path movement."""
    if len(prices) < 2:
        return D(0)
    path = sum(
        (abs(prices[index] / prices[index - 1] - D(1))
         for index in range(1, len(prices))),
        D(0),
    )
    if not path.is_finite() or path <= 0:
        return D(0)
    displacement = abs(prices[-1] / prices[0] - D(1))
    if not displacement.is_finite():
        return D(0)
    return min(D(1), displacement / path)


def generate_trend_signal(
    pair_id: str,
    closes: Sequence[Decimal],
    *,
    fast_threshold_bps: Decimal = D("120"),
    slow_threshold_bps: Decimal = D("180"),
) -> TrendSignal:
    """Generate a price-only H1 trend signal with explicit trend-efficiency checks.

    The score is a bounded trend-strength diagnostic, never a calibrated probability
    or expected-return estimate. The signal alone cannot authorize an order.
    """
    if len(closes) < 73:
        return TrendSignal(
            pair_id, "NONE", D(0), D(0), D(0), D(0), "INSUFFICIENT_HISTORY"
        )
    if (
        not fast_threshold_bps.is_finite()
        or not slow_threshold_bps.is_finite()
        or fast_threshold_bps <= 0
        or slow_threshold_bps <= 0
    ):
        return TrendSignal(
            pair_id, "NONE", D(0), D(0), D(0), D(0), "INVALID_STRATEGY_THRESHOLDS"
        )

    prices = list(closes)
    if any(not value.is_finite() or value <= 0 for value in prices[-73:]):
        return TrendSignal(
            pair_id, "NONE", D(0), D(0), D(0), D(0), "INVALID_PRICE_HISTORY"
        )

    last = prices[-1]
    fast_start = prices[-25]
    slow_start = prices[-73]
    fast = (last / fast_start - D(1)) * D(10000)
    slow = (last / slow_start - D(1)) * D(10000)
    fast_mean = sum(prices[-24:], D(0)) / D(24)
    slow_mean = sum(prices[-72:], D(0)) / D(72)
    fast_efficiency = _efficiency_ratio(prices[-25:])
    slow_efficiency = _efficiency_ratio(prices[-73:])

    returns = [
        float(prices[index] / prices[index - 1] - D(1))
        for index in range(len(prices) - 24, len(prices))
    ]
    if not returns or any(not math.isfinite(value) for value in returns):
        return TrendSignal(
            pair_id, "NONE", fast, slow, D(0), D(0),
            "VOLATILITY_ESTIMATE_INVALID", fast_efficiency, slow_efficiency,
        )
    sigma_value = pstdev(returns) * math.sqrt(24) * 100
    if not math.isfinite(sigma_value):
        return TrendSignal(
            pair_id, "NONE", fast, slow, D(0), D(0),
            "VOLATILITY_ESTIMATE_INVALID", fast_efficiency, slow_efficiency,
        )
    sigma = D(str(sigma_value))
    stop_distance = max(
        MIN_STOP_DISTANCE_PCT,
        min(MAX_STOP_DISTANCE_PCT, sigma * D(2)),
    )

    long_candidate = (
        fast >= fast_threshold_bps
        and slow >= slow_threshold_bps
        and last > fast_mean > slow_mean
    )
    short_candidate = (
        fast <= -fast_threshold_bps
        and slow <= -slow_threshold_bps
        and last < fast_mean < slow_mean
    )
    if not long_candidate and not short_candidate:
        return TrendSignal(
            pair_id, "NONE", fast, slow, D(0), stop_distance,
            "NO_CONFIRMED_TREND", fast_efficiency, slow_efficiency,
        )

    if (
        fast_efficiency < MIN_FAST_EFFICIENCY
        or slow_efficiency < MIN_SLOW_EFFICIENCY
    ):
        return TrendSignal(
            pair_id, "NONE", fast, slow, D(0), stop_distance,
            "TREND_EFFICIENCY_TOO_LOW", fast_efficiency, slow_efficiency,
        )

    strength_multiple = min(
        abs(fast) / fast_threshold_bps,
        abs(slow) / slow_threshold_bps,
    )
    strength_score = min(D(1), strength_multiple / D(2))
    score = strength_score * fast_efficiency * slow_efficiency
    direction = "LONG" if long_candidate else "SHORT"
    reason = "TREND_UP" if long_candidate else "TREND_DOWN"
    return TrendSignal(
        pair_id, direction, fast, slow, score, stop_distance, reason,
        fast_efficiency, slow_efficiency,
    )


def take_profit_quantity(position_quantity: Decimal, fraction: Decimal = D("0.5")) -> Decimal:
    if not position_quantity.is_finite() or position_quantity <= 0:
        raise ValueError("POSITION_QUANTITY_MUST_BE_POSITIVE_AND_FINITE")
    if not fraction.is_finite() or not D(0) < fraction < D(1):
        raise ValueError("PARTIAL_FRACTION_MUST_BE_FINITE_AND_BETWEEN_ZERO_AND_ONE")
    return position_quantity * fraction


def profit_trigger_reached(
    entry_price: Decimal,
    current_price: Decimal,
    side: str,
    trigger_pct: Decimal,
) -> bool:
    values = (entry_price, current_price, trigger_pct)
    if any(not value.is_finite() for value in values):
        return False
    if entry_price <= 0 or current_price <= 0 or trigger_pct <= 0:
        return False
    normalized_side = side.upper()
    if normalized_side not in {"LONG", "SHORT"}:
        return False
    direction = D(1) if normalized_side == "LONG" else D(-1)
    return ((current_price / entry_price - D(1)) * direction * D(100)) >= trigger_pct
