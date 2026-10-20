from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from statistics import median
from typing import Any, Sequence

D = Decimal
RESEARCH_SLIPPAGE_BPS_PER_SIDE = D("5")
MINIMUM_COSTED_FORWARD_OBSERVATIONS = 100
MINIMUM_HOLDOUT_OBSERVATIONS_FOR_REVIEW = 20


def observation_id(
    *,
    pair: str,
    interval_minutes: int,
    bar_ts: int,
    strategy_version: str,
    fast_threshold_bps: Decimal,
    slow_threshold_bps: Decimal,
) -> str:
    identity = "|".join(
        (
            pair,
            str(interval_minutes),
            str(bar_ts),
            strategy_version,
            str(fast_threshold_bps),
            str(slow_threshold_bps),
        )
    )
    return sha256(identity.encode("utf-8")).hexdigest()


def estimate_forward_outcome(
    *,
    direction: str,
    entry_mid_price: Decimal,
    exit_close_price: Decimal,
    entry_spread_bps: Decimal,
    exit_spread_bps: Decimal,
    taker_fee_bps: Decimal | None,
    slippage_bps_per_side: Decimal = RESEARCH_SLIPPAGE_BPS_PER_SIDE,
) -> dict[str, Any]:
    """Calculate a forward signal label, not actual fills or portfolio PnL."""
    normalized_direction = direction.upper()
    values = (
        entry_mid_price,
        exit_close_price,
        entry_spread_bps,
        exit_spread_bps,
        slippage_bps_per_side,
    )
    if normalized_direction not in {"LONG", "SHORT"}:
        raise ValueError("RESEARCH_DIRECTION_NOT_DIRECTIONAL")
    if any(not value.is_finite() for value in values):
        raise ValueError("RESEARCH_OUTCOME_INPUT_NON_FINITE")
    if entry_mid_price <= 0 or exit_close_price <= 0:
        raise ValueError("RESEARCH_OUTCOME_PRICE_INVALID")
    if entry_spread_bps < 0 or exit_spread_bps < 0 or slippage_bps_per_side < 0:
        raise ValueError("RESEARCH_OUTCOME_COST_INPUT_NEGATIVE")
    if taker_fee_bps is not None and (
        not taker_fee_bps.is_finite() or taker_fee_bps < 0
    ):
        raise ValueError("RESEARCH_TAKER_FEE_INVALID")

    sign = D(1) if normalized_direction == "LONG" else D(-1)
    gross_return_bps = sign * (exit_close_price / entry_mid_price - D(1)) * D(10000)
    outcome: dict[str, Any] = {
        "gross_return_bps": gross_return_bps,
        "spot_cost_estimate_bps": None,
        "spot_net_return_bps": None,
        "margin_cost_break_even_bps": None,
        "cost_quality": "GROSS_ONLY_TAKER_FEE_UNKNOWN",
    }
    if taker_fee_bps is None:
        return outcome

    # Mid-to-mid price movement: pay half the observed spread on each side.
    # The exit spread is observed in the current ticker when the delayed target bar
    # becomes available, so it is an estimate—not historical executable-book evidence.
    spread_cost_bps = (entry_spread_bps + exit_spread_bps) / D(2)
    spot_cost_bps = D(2) * taker_fee_bps + spread_cost_bps + D(2) * slippage_bps_per_side
    spot_net_bps = gross_return_bps - spot_cost_bps
    outcome.update(
        {
            "spot_cost_estimate_bps": spot_cost_bps,
            "spot_net_return_bps": spot_net_bps,
            # Maximum combined opening + rollover cost before estimated margin net
            # expectancy turns non-positive. It is not a quoted Kraken fee.
            "margin_cost_break_even_bps": spot_net_bps,
            "cost_quality": (
                "KRAKEN_TRADE_VOLUME_TAKER_FEE; "
                "ENTRY_AND_CURRENT_EXIT_SPREAD_ESTIMATE; "
                "ASSUMED_SLIPPAGE_5BPS_PER_SIDE; "
                "MARGIN_FEES_UNKNOWN; NO_HISTORICAL_L2"
            ),
        }
    )
    return outcome


def _metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    returns = [
        D(str(row["spot_net_return_bps"]))
        for row in rows
        if row.get("spot_net_return_bps") is not None
        and D(str(row["spot_net_return_bps"])).is_finite()
    ]
    if not returns:
        return {
            "sample_count": 0,
            "mean_net_return_bps": None,
            "median_net_return_bps": None,
            "win_rate_pct": None,
            "profit_factor": None,
            "worst_net_return_bps": None,
        }

    wins = sum((value for value in returns if value > 0), D(0))
    losses = -sum((value for value in returns if value < 0), D(0))
    if losses:
        factor: str | None = str(wins / losses)
    elif wins:
        factor = "NO_LOSSES_IN_SAMPLE"
    else:
        factor = "ZERO_GROSS_WIN_AND_LOSS"
    return {
        "sample_count": len(returns),
        "mean_net_return_bps": str(sum(returns, D(0)) / D(len(returns))),
        "median_net_return_bps": str(D(str(median(returns)))),
        "win_rate_pct": str(D(sum(1 for value in returns if value > 0)) / D(len(returns)) * D(100)),
        "profit_factor": factor,
        "worst_net_return_bps": str(min(returns)),
    }


def summarize_shadow_research(observations: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Summarize chronological forward labels without declaring strategy calibration."""
    ordered = sorted(
        observations,
        key=lambda row: (int(row.get("decision_ts", 0)), str(row.get("observation_id", ""))),
    )
    directional = [row for row in ordered if row.get("direction") in {"LONG", "SHORT"}]
    settled = [
        row for row in directional
        if row.get("status") in {"SETTLED_COST_ESTIMATE", "SETTLED_GROSS_ONLY"}
    ]
    costed = [
        row for row in settled
        if row.get("spot_net_return_bps") is not None
    ]
    margin_break_even = [
        D(str(row["margin_cost_break_even_bps"]))
        for row in costed
        if row.get("margin_cost_break_even_bps") is not None
        and D(str(row["margin_cost_break_even_bps"])).is_finite()
    ]
    n = len(costed)
    train_end = int(n * 0.60)
    validation_end = int(n * 0.80)
    train = costed[:train_end]
    validation = costed[train_end:validation_end]
    test = costed[validation_end:]
    holdout_ready = (
        n >= MINIMUM_COSTED_FORWARD_OBSERVATIONS
        and len(test) >= MINIMUM_HOLDOUT_OBSERVATIONS_FOR_REVIEW
    )
    incomplete = sum(
        1 for row in directional if str(row.get("status", "")).startswith("INCOMPLETE")
    )
    no_fee_settled = sum(1 for row in settled if row.get("spot_net_return_bps") is None)

    return {
        "research_schema": "shadow-forward-v1",
        "strategy_version": str(ordered[-1].get("strategy_version", "unknown")) if ordered else "unknown",
        "total_observations": len(ordered),
        "directional_observations": len(directional),
        "long_observations": sum(1 for row in directional if row.get("direction") == "LONG"),
        "short_observations": sum(1 for row in directional if row.get("direction") == "SHORT"),
        "settled_directional_observations": len(settled),
        "costed_spot_proxy_observations": n,
        "pending_directional_observations": sum(1 for row in directional if row.get("status") == "PENDING"),
        "incomplete_directional_observations": incomplete,
        "settled_without_taker_fee": no_fee_settled,
        "spot_cost_proxy_all": _metrics(costed),
        "spot_cost_proxy_by_direction": {
            direction: _metrics([row for row in costed if row.get("direction") == direction])
            for direction in ("LONG", "SHORT")
        },
        "chronological_splits": {
            "train_first_60_percent": _metrics(train),
            "validation_next_20_percent": _metrics(validation),
            "test_last_20_percent": _metrics(test),
        },
        "margin_cost_diagnostics": {
            "actual_margin_fee_evidence_available": False,
            "break_even_observation_count": len(margin_break_even),
            "median_break_even_margin_cost_bps": (
                str(D(str(median(margin_break_even)))) if margin_break_even else None
            ),
            "positive_break_even_budget_count": sum(1 for value in margin_break_even if value > 0),
            "margin_net_return_calculated": False,
            "status": "BREAK_EVEN_ONLY_MARGIN_OPEN_AND_ROLLOVER_FEES_UNKNOWN",
        },
        "holdout_sample_available_for_review": holdout_ready,
        "validation_status": (
            "HOLDOUT_AVAILABLE_REVIEW_REQUIRED"
            if holdout_ready
            else "INSUFFICIENT_FORWARD_SAMPLE"
        ),
        "release_gate_eligible": False,
        "portfolio_pnl": False,
        "overlapping_signal_horizons": True,
        "limitations": [
            "Forward signal labels overlap across time and are not independent trades.",
            "Entry is ticker mid; outcomes use the nearest closed hourly bar at the target horizon.",
            "Exit spread is the current ticker spread when the target bar becomes available, not historical order-book evidence.",
            "Slippage is a fixed 5 bps per side research assumption.",
            "Margin opening and rollover fees are unknown; margin net returns are not computed.",
            "This report cannot calibrate edge or release live order execution automatically.",
        ],
    }
