from __future__ import annotations

from math import isfinite, sqrt
from statistics import fmean, median
from typing import Any, Mapping, Sequence

SLIPPAGE_SCENARIOS_BPS_PER_SIDE = (0.0, 1.0, 2.0, 5.0, 10.0)
BETA_LOOKBACK = 72
RESIDUAL_LOOKBACK = 72
SHOCK_HORIZON = 6
CONFIRMATION_HORIZON = 2
HOLDING_HORIZON = 12
ENTRY_Z = 1.5
REGIME_VOLATILITY_WINDOW = 24
REGIME_REFERENCE_WINDOW = 72
MAX_VOLATILITY_EXPANSION_MULTIPLE = 1.5
MINIMUM_BARS = 240


def _metrics(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {
            "sample_count": 0,
            "mean_net_return_bps": None,
            "median_net_return_bps": None,
            "profit_factor": None,
            "win_rate_pct": None,
            "worst_net_return_bps": None,
        }
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return {
        "sample_count": len(values),
        "mean_net_return_bps": fmean(values),
        "median_net_return_bps": median(values),
        "profit_factor": gains / losses if losses > 0 else (None if gains == 0 else "INF"),
        "win_rate_pct": 100.0 * sum(value > 0 for value in values) / len(values),
        "worst_net_return_bps": min(values),
    }


def _returns(values: Sequence[float]) -> list[float]:
    return [values[index] / values[index - 1] - 1.0 for index in range(1, len(values))]


def _rolling_beta(asset: Sequence[float], market: Sequence[float], end: int) -> float | None:
    start = end - BETA_LOOKBACK
    left = asset[start:end]
    right = market[start:end]
    if len(left) != BETA_LOOKBACK or len(right) != BETA_LOOKBACK:
        return None
    left_mean = fmean(left)
    right_mean = fmean(right)
    variance = sum((value - right_mean) ** 2 for value in right)
    if variance <= 1e-15:
        return None
    covariance = sum(
        (a - left_mean) * (m - right_mean)
        for a, m in zip(left, right, strict=True)
    )
    beta = covariance / variance
    return beta if isfinite(beta) else None


def _realized_volatility(values: Sequence[float], end: int) -> float | None:
    start = end - REGIME_VOLATILITY_WINDOW + 1
    window = values[start : end + 1]
    if start < 0 or len(window) != REGIME_VOLATILITY_WINDOW:
        return None
    result = sqrt(sum(value * value for value in window))
    return result if isfinite(result) else None


def _split(index: int, train_end: int, validation_end: int) -> str:
    if index < train_end:
        return "train"
    if index < validation_end:
        return "validation"
    return "test"


def run_regime_divergence_research(
    market: Mapping[str, Any],
    market_costs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate conditional idiosyncratic shock-recoil signals; never creates orders.

    Signals are formed from closed bars only. Beta, residual shock normalization and
    confirmation use data available at the decision close. Entry is next-bar open;
    exit is the open after a fixed 12-bar holding interval. A trade crossing a
    chronological split boundary is excluded from that split.
    """
    symbols = list(market.get("symbols", []))
    timestamps = list(market.get("timestamps", []))
    closes = market.get("closes", {})
    opens = market.get("opens", {})
    count = len(timestamps)
    unavailable = {
        "status": "INSUFFICIENT_SYNCHRONIZED_HISTORY",
        "minimum_bars": MINIMUM_BARS,
        "available_bars": count,
        "release_gate_eligible": False,
        "order_creation_allowed": False,
        "actual_fills": False,
        "portfolio_pnl": False,
    }
    if count < MINIMUM_BARS or len(symbols) < 3:
        return unavailable
    if not isinstance(closes, Mapping) or not isinstance(opens, Mapping):
        return unavailable
    try:
        close_series = {symbol: [float(value) for value in closes[symbol]] for symbol in symbols}
        open_series = {symbol: [float(value) for value in opens[symbol]] for symbol in symbols}
    except (KeyError, TypeError, ValueError, OverflowError):
        return unavailable
    if any(len(values) != count for values in (*close_series.values(), *open_series.values())):
        return unavailable
    if any(
        not isfinite(value) or value <= 0
        for values in (*close_series.values(), *open_series.values())
        for value in values
    ):
        return unavailable

    asset_returns = {symbol: _returns(close_series[symbol]) for symbol in symbols}
    market_returns = [
        fmean(asset_returns[symbol][index] for symbol in symbols)
        for index in range(count - 1)
    ]
    train_end = int(count * 0.60)
    validation_end = int(count * 0.80)
    trades: list[dict[str, Any]] = []
    skipped_unknown_costs = 0
    skipped_split_boundary = 0
    regime_filter_excluded_count = 0
    next_entry_index = {symbol: 0 for symbol in symbols}

    for symbol in symbols:
        residuals: list[float | None] = [None]
        for return_index in range(1, count):
            beta = _rolling_beta(
                asset_returns[symbol], market_returns, return_index - 1
            )
            if beta is None:
                residuals.append(None)
            else:
                residuals.append(
                    asset_returns[symbol][return_index - 1]
                    - beta * market_returns[return_index - 1]
                )

        for decision_index in range(BETA_LOOKBACK + RESIDUAL_LOOKBACK + SHOCK_HORIZON, count - HOLDING_HORIZON - 1):
            if decision_index < next_entry_index[symbol]:
                continue
            shock_start = decision_index - SHOCK_HORIZON + 1
            recent_start = decision_index - CONFIRMATION_HORIZON + 1
            prior_shocks: list[float] = []
            valid = True
            for end in range(decision_index - RESIDUAL_LOOKBACK, decision_index):
                window = residuals[end - SHOCK_HORIZON + 1 : end + 1]
                if len(window) != SHOCK_HORIZON or any(value is None for value in window):
                    valid = False
                    break
                prior_shocks.append(sum(float(value) for value in window if value is not None))
            current_window = residuals[shock_start : decision_index + 1]
            confirmation = residuals[recent_start : decision_index + 1]
            if (
                not valid
                or len(prior_shocks) < RESIDUAL_LOOKBACK
                or any(value is None for value in current_window)
                or any(value is None for value in confirmation)
            ):
                continue
            shock = sum(float(value) for value in current_window if value is not None)
            mean = fmean(prior_shocks)
            variance = fmean((value - mean) ** 2 for value in prior_shocks)
            scale = sqrt(variance)
            if scale <= 1e-12:
                continue
            z_score = (shock - mean) / scale
            confirmation_return = sum(
                float(value) for value in confirmation if value is not None
            )
            direction: str | None = None
            if z_score <= -ENTRY_Z and confirmation_return > 0:
                direction = "LONG"
            elif z_score >= ENTRY_Z and confirmation_return < 0:
                direction = "SHORT"
            if direction is None:
                continue

            market_volatility = _realized_volatility(
                market_returns, decision_index - 1
            )
            reference_volatility = [
                value
                for end in range(
                    decision_index - REGIME_VOLATILITY_WINDOW - REGIME_REFERENCE_WINDOW,
                    decision_index - REGIME_VOLATILITY_WINDOW,
                )
                if (value := _realized_volatility(market_returns, end)) is not None
            ]
            if (
                market_volatility is None
                or len(reference_volatility) < REGIME_REFERENCE_WINDOW
                or market_volatility
                > median(reference_volatility) * MAX_VOLATILITY_EXPANSION_MULTIPLE
            ):
                regime_filter_excluded_count += 1
                continue

            entry_index = decision_index + 1
            exit_index = entry_index + HOLDING_HORIZON
            split_name = _split(decision_index, train_end, validation_end)
            if exit_index >= count or _split(exit_index, train_end, validation_end) != split_name:
                skipped_split_boundary += 1
                continue
            entry_price = open_series[symbol][entry_index]
            exit_price = open_series[symbol][exit_index]
            sign = 1.0 if direction == "LONG" else -1.0
            gross_bps = sign * (exit_price / entry_price - 1.0) * 10000.0
            cost = market_costs.get(symbol, {})
            try:
                fee = float(cost["taker_fee_bps"])
                spread = float(cost["spread_bps"])
            except (KeyError, TypeError, ValueError, OverflowError):
                fee = spread = float("nan")
            if not isfinite(fee) or not isfinite(spread) or fee < 0 or spread < 0:
                skipped_unknown_costs += 1
                net_bps = None
            else:
                net_bps = gross_bps - (2.0 * fee + spread + 10.0)
            trades.append({
                "symbol": symbol,
                "direction": direction,
                "decision_bar_ts": int(timestamps[decision_index]),
                "entry_bar_ts": int(timestamps[entry_index]),
                "exit_bar_ts": int(timestamps[exit_index]),
                "split": split_name,
                "residual_shock_z": z_score,
                "confirmation_residual_return_bps": confirmation_return * 10000.0,
                "gross_return_bps": gross_bps,
                "net_return_bps": net_bps,
                "cost_status": "COSTED_PROXY" if net_bps is not None else "UNKNOWN_COST_EXCLUDED",
            })
            next_entry_index[symbol] = exit_index + 1

    by_split: dict[str, Any] = {}
    for split_name in ("train", "validation", "test"):
        selected = [
            float(item["net_return_bps"])
            for item in trades
            if item["split"] == split_name and item["net_return_bps"] is not None
        ]
        by_split[split_name] = _metrics(selected)

    sensitivity: list[dict[str, Any]] = []
    for slippage in SLIPPAGE_SCENARIOS_BPS_PER_SIDE:
        split_metrics: dict[str, Any] = {}
        for split_name in ("train", "validation", "test"):
            values = [
                float(item["gross_return_bps"])
                - (
                    2.0 * float(market_costs[item["symbol"]]["taker_fee_bps"])
                    + float(market_costs[item["symbol"]]["spread_bps"])
                    + 2.0 * slippage
                )
                for item in trades
                if item["split"] == split_name
                and item["net_return_bps"] is not None
            ]
            split_metrics[split_name] = _metrics(values)
        sensitivity.append({
            "slippage_bps_per_side": slippage,
            "chronological_splits": split_metrics,
            "status": "COST_ASSUMPTION_SENSITIVITY_NOT_HISTORICAL_EXECUTION",
        })

    costed = [item for item in trades if item["net_return_bps"] is not None]
    return {
        "status": "RESEARCH_ONLY_NO_EXECUTION_SIMULATION",
        "candidate_name": "regime_conditioned_residual_recoil",
        "candidate_version": "rcr-0.1.0",
        "method": {
            "hypothesis": "IDIOSYNCRATIC_SHOCK_FOLLOWED_BY_RESIDUAL_REVERSAL_CONFIRMATION",
            "market_factor": "EQUAL_WEIGHT_MEAN_OF_SYNCHRONIZED_ASSET_HOURLY_RETURNS",
            "beta_estimation": "ROLLING_72_BAR_COVARIANCE_OVER_MARKET_VARIANCE",
            "residual_shock_horizon_bars": SHOCK_HORIZON,
            "residual_normalization_lookback": RESIDUAL_LOOKBACK,
            "entry_abs_z_score": ENTRY_Z,
            "reversal_confirmation_bars": CONFIRMATION_HORIZON,
            "market_regime_filter": "24H_REALIZED_VOLATILITY_NOT_ABOVE_1_5X_TRAILING_72H_MEDIAN",
            "market_volatility_window_bars": REGIME_VOLATILITY_WINDOW,
            "market_volatility_reference_windows": REGIME_REFERENCE_WINDOW,
            "holding_horizon_bars": HOLDING_HORIZON,
            "entry_timing": "NEXT_H1_OPEN_AFTER_CLOSED_SIGNAL_BAR",
            "exit_timing": "OPEN_AFTER_FIXED_HOLDING_HORIZON",
            "overlap_control": "NON_OVERLAPPING_TRADES_PER_ASSET",
            "cross_asset_portfolio_correlation_modeled": False,
            "future_data_used_for_signal": False,
            "thresholds_promoted": False,
        },
        "trade_count": len(trades),
        "costed_trade_count": len(costed),
        "unknown_cost_trade_count": skipped_unknown_costs,
        "split_boundary_excluded_count": skipped_split_boundary,
        "market_regime_filter_excluded_count": regime_filter_excluded_count,
        "chronological_splits": by_split,
        "slippage_sensitivity": sensitivity,
        "sample_trades": trades[-20:],
        "cost_model": {
            "fee_source": "CURRENT_KRAKEN_ACCOUNT_TAKER_FEE_PROXY",
            "spread_source": "CURRENT_SPREAD_PROXY",
            "slippage_bps_per_side": 5.0,
            "unknown_costs_excluded_not_zero": True,
            "margin_opening_and_rollover_costs_included": False,
        },
        "interpretation": "SIGNAL_LABELS_NOT_PORTFOLIO_PNL_OR_ACTUAL_FILLS",
        "release_gate_eligible": False,
        "strategy_promotion_allowed": False,
        "order_creation_allowed": False,
        "actual_fills": False,
        "portfolio_pnl": False,
    }
