from __future__ import annotations

from bisect import bisect_left
from itertools import combinations
import hashlib
import math
from statistics import fmean, median, pstdev
from typing import Any, Mapping, Sequence
from decimal import Decimal

from app.regime_divergence import run_regime_divergence_research
from app.orderbook_simulator import (
    BookLevel,
    OrderBookSnapshot,
    simulate_order_book_execution,
    simulate_order_book_round_trip,
)

D_MIN = 1e-12
PORTFOLIO_RESEARCH_VERSION = "parrondo-multi-asset-v0.3"
CORRELATION_LOOKBACK_BARS = 168
PAIR_REGRESSION_LOOKBACK_BARS = 168
MIN_SYNCHRONIZED_BARS = 240
NEGATIVE_CORRELATION_THRESHOLD = -0.25
PAIR_MIN_CORRELATION = 0.30
PAIR_ENTRY_Z = 2.0
PAIR_EXIT_Z = 0.5
PAIR_STOP_Z = 4.0
PAIR_MAX_HOLDING_BARS = 48
REBALANCE_INTERVAL_BARS = 24
REBALANCE_DRIFT_THRESHOLD = 0.05
SLIPPAGE_BPS_PER_SIDE = 5.0
SLIPPAGE_SENSITIVITY_BPS_PER_SIDE = (0.0, 1.0, 2.0, 5.0, 10.0)
CORRELATION_SENSITIVITY_THRESHOLDS = (-0.25, -0.15, -0.05, 0.0)
MAX_CONCURRENT_PAIR_POSITIONS = 4
PAIR_SLOT_EQUITY_FRACTION = 0.25
ORDER_BOOK_NOTIONAL_USD = (250.0, 500.0, 1000.0, 2500.0)
MOMENTUM_FAST_LOOKBACK_BARS = 24
MOMENTUM_SLOW_LOOKBACK_BARS = 72
MOMENTUM_REBALANCE_INTERVAL_BARS = 6
MOMENTUM_SIDE_ASSET_COUNT = 3
MOMENTUM_GROSS_WEIGHT_PER_SIDE = 0.40
MOMENTUM_SCORE_FAST_WEIGHT = 0.40
MOMENTUM_SCORE_SLOW_WEIGHT = 0.60


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _aligned_market_histories(
    histories: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    min_bars: int = MIN_SYNCHRONIZED_BARS,
) -> dict[str, Any]:
    """Align hourly closed candles; never synthesize missing observations."""
    series: dict[str, dict[int, tuple[float, float]]] = {}
    for raw_symbol, rows in histories.items():
        symbol = str(raw_symbol).strip()
        if not symbol:
            continue
        by_ts: dict[int, tuple[float, float]] = {}
        for row in rows:
            try:
                timestamp = int(row["ts"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            open_price = _finite_float(row.get("open"))
            close_price = _finite_float(row.get("close"))
            if open_price is None or close_price is None or open_price <= 0 or close_price <= 0:
                continue
            by_ts[timestamp] = (open_price, close_price)
        if len(by_ts) >= min_bars:
            series[symbol] = by_ts

    while len(series) >= 2:
        common = set.intersection(*(set(values) for values in series.values()))
        if len(common) >= min_bars:
            break
        if len(series) == 2:
            break
        drop_symbol = min(series, key=lambda symbol: (len(series[symbol]), symbol))
        del series[drop_symbol]

    if len(series) < 2:
        return {"status": "INSUFFICIENT_ASSET_HISTORY", "symbols": [], "timestamps": []}

    common_timestamps = sorted(set.intersection(*(set(values) for values in series.values())))
    if len(common_timestamps) < min_bars:
        return {"status": "INSUFFICIENT_SYNCHRONIZED_HISTORY", "symbols": sorted(series), "timestamps": []}

    suffix_start = len(common_timestamps) - 1
    for index in range(len(common_timestamps) - 1, 0, -1):
        if common_timestamps[index] - common_timestamps[index - 1] != 3600:
            break
        suffix_start = index - 1
    timestamps = common_timestamps[suffix_start:]
    if len(timestamps) < min_bars:
        return {
            "status": "INSUFFICIENT_CONTIGUOUS_HISTORY",
            "symbols": sorted(series),
            "timestamps": timestamps,
        }

    symbols = sorted(series)
    opens = {symbol: [series[symbol][ts][0] for ts in timestamps] for symbol in symbols}
    closes = {symbol: [series[symbol][ts][1] for ts in timestamps] for symbol in symbols}
    return {
        "status": "READY",
        "symbols": symbols,
        "timestamps": timestamps,
        "opens": opens,
        "closes": closes,
        "excluded_symbols": sorted(set(str(key) for key in histories) - set(symbols)),
    }


def _returns(values: Sequence[float]) -> list[float]:
    return [values[index] / values[index - 1] - 1.0 for index in range(1, len(values))]


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    left_mean = fmean(left)
    right_mean = fmean(right)
    left_var = sum((value - left_mean) ** 2 for value in left)
    right_var = sum((value - right_mean) ** 2 for value in right)
    if left_var <= D_MIN or right_var <= D_MIN:
        return None
    covariance = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right, strict=True)
    )
    value = covariance / math.sqrt(left_var * right_var)
    return value if math.isfinite(value) else None


def _select_negative_pairs(
    closes: Mapping[str, Sequence[float]],
    *,
    index: int,
    lookback: int = CORRELATION_LOOKBACK_BARS,
    threshold: float = NEGATIVE_CORRELATION_THRESHOLD,
) -> list[tuple[str, str, float]]:
    if index < lookback:
        return []
    candidates: list[tuple[str, str, float]] = []
    for left, right in combinations(sorted(closes), 2):
        left_returns = _returns(closes[left][index - lookback : index + 1])
        right_returns = _returns(closes[right][index - lookback : index + 1])
        correlation = _correlation(left_returns, right_returns)
        if correlation is not None and correlation <= threshold:
            candidates.append((left, right, correlation))
    selected: list[tuple[str, str, float]] = []
    used: set[str] = set()
    for left, right, correlation in sorted(candidates, key=lambda item: (item[2], item[0], item[1])):
        if left in used or right in used:
            continue
        selected.append((left, right, correlation))
        used.update((left, right))
    return selected


def _side_cost_components(
    symbol: str,
    market_costs: Mapping[str, Mapping[str, Any]],
    *,
    slippage_bps_per_side: float = SLIPPAGE_BPS_PER_SIDE,
    fee_type: str = "taker",
) -> tuple[float, float, float] | None:
    raw = market_costs.get(symbol, {})
    fee_field = {
        "taker": "taker_fee_bps",
        "maker_reference": "maker_fee_bps",
    }.get(fee_type)
    if fee_field is None:
        return None
    fee = _finite_float(raw.get(fee_field))
    spread = _finite_float(raw.get("spread_bps"))
    if (
        fee is None or spread is None or fee < 0 or spread < 0
        or not math.isfinite(slippage_bps_per_side) or slippage_bps_per_side < 0
    ):
        return None
    return fee, spread / 2.0, slippage_bps_per_side


def _side_cost_bps(
    symbol: str,
    market_costs: Mapping[str, Mapping[str, Any]],
    *,
    slippage_bps_per_side: float = SLIPPAGE_BPS_PER_SIDE,
    fee_type: str = "taker",
) -> float | None:
    components = _side_cost_components(
        symbol, market_costs, slippage_bps_per_side=slippage_bps_per_side,
        fee_type=fee_type,
    )
    return sum(components) if components is not None else None


def _curve_metrics(returns: Sequence[float]) -> dict[str, Any]:
    if not returns:
        return {
            "sample_bars": 0,
            "total_return_pct": None,
            "max_drawdown_pct": None,
            "sharpe_hourly_annualized": None,
            "sortino_hourly_annualized": None,
        }
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= max(0.0, 1.0 + value)
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    average = fmean(returns)
    deviation = pstdev(returns) if len(returns) > 1 else 0.0
    downside = [min(value, 0.0) for value in returns]
    downside_deviation = math.sqrt(fmean(value * value for value in downside))
    sharpe = average / deviation * math.sqrt(24.0 * 365.0) if deviation > D_MIN else None
    sortino = average / downside_deviation * math.sqrt(24.0 * 365.0) if downside_deviation > D_MIN else None
    return {
        "sample_bars": len(returns),
        "total_return_pct": round((equity - 1.0) * 100.0, 6),
        "max_drawdown_pct": round(max_drawdown * 100.0, 6),
        "sharpe_hourly_annualized": round(sharpe, 6) if sharpe is not None else None,
        "sortino_hourly_annualized": round(sortino, 6) if sortino is not None else None,
    }


def _split_returns(returns: Sequence[float], boundaries: tuple[int, int, int, int]) -> dict[str, Any]:
    train_end, validation_end, start, end = boundaries
    return {
        "train": _curve_metrics(returns[start:train_end]),
        "validation": _curve_metrics(returns[train_end:validation_end]),
        "test": _curve_metrics(returns[validation_end:end]),
    }


def _simulate_rebalancing(
    market: Mapping[str, Any],
    market_costs: Mapping[str, Mapping[str, Any]],
    *,
    strategy: str,
    lookback: int = CORRELATION_LOOKBACK_BARS,
    correlation_threshold: float = NEGATIVE_CORRELATION_THRESHOLD,
    slippage_bps_per_side: float = SLIPPAGE_BPS_PER_SIDE,
    fee_type: str = "taker",
) -> dict[str, Any]:
    symbols = market["symbols"]
    closes = market["closes"]
    count = len(market["timestamps"])
    costs = {
        symbol: _side_cost_bps(
            symbol, market_costs, slippage_bps_per_side=slippage_bps_per_side,
            fee_type=fee_type,
        )
        for symbol in symbols
    }
    costs_complete = all(value is not None for value in costs.values())
    weights = {symbol: 0.0 for symbol in symbols}
    cash_weight = 1.0
    gross_returns = [0.0] * count
    net_returns = [0.0] * count
    turnover = 0.0
    rebalance_count = 0
    selection_count = 0
    selected_correlations: list[float] = []
    cash_weights: list[float] = []
    cost_total_fraction = 0.0
    last_rebalance_index = -REBALANCE_INTERVAL_BARS

    for index in range(lookback, count):
        if index > lookback:
            asset_returns = {
                symbol: closes[symbol][index] / closes[symbol][index - 1] - 1.0
                for symbol in symbols
            }
            portfolio_return = sum(weights[symbol] * asset_returns[symbol] for symbol in symbols)
            gross_returns[index] = portfolio_return
            denominator = 1.0 + portfolio_return
            if denominator <= 0 or not math.isfinite(denominator):
                break
            for symbol in symbols:
                weights[symbol] = weights[symbol] * (1.0 + asset_returns[symbol]) / denominator
            cash_weight = cash_weight / denominator

        periodic_rebalance_due = (index - lookback) % REBALANCE_INTERVAL_BARS == 0
        if strategy == "buy_and_hold_equal_weight" and index > lookback:
            periodic_rebalance_due = False
        if not periodic_rebalance_due:
            net_returns[index] = gross_returns[index]
            cash_weights.append(cash_weight)
            continue

        if strategy == "negative_correlation_threshold_rebalance":
            pairs = _select_negative_pairs(
                closes, index=index, lookback=lookback, threshold=correlation_threshold
            )
            selected = {symbol for left, right, _ in pairs for symbol in (left, right)}
            target = (
                {symbol: (1.0 / len(selected) if symbol in selected else 0.0) for symbol in symbols}
                if selected else {symbol: 0.0 for symbol in symbols}
            )
            target_cash = 1.0 - sum(target.values())
            selection_count += len(pairs)
            selected_correlations.extend(item[2] for item in pairs)
        else:
            target = {symbol: 1.0 / len(symbols) for symbol in symbols}
            target_cash = 0.0

        deltas = {symbol: target[symbol] - weights[symbol] for symbol in symbols}
        max_deviation = max((abs(value) for value in deltas.values()), default=0.0)
        membership_changed = any(
            (target[symbol] > 0.0) != (weights[symbol] > 1e-12) for symbol in symbols
        )
        should_rebalance = (
            strategy in {"periodic_equal_weight_rebalance", "buy_and_hold_equal_weight"}
            or membership_changed
            or max_deviation >= REBALANCE_DRIFT_THRESHOLD
        )
        if should_rebalance:
            traded_weights = sum(abs(value) for value in deltas.values())
            trade_cost_fraction = 0.0
            if costs_complete:
                for symbol in symbols:
                    side_cost_bps = costs[symbol]
                    if side_cost_bps is None:
                        continue
                    trade_cost_fraction += (
                        abs(deltas[symbol]) * side_cost_bps / 10000.0
                    )
            turnover += traded_weights
            rebalance_count += 1
            cost_total_fraction += trade_cost_fraction
            if costs_complete:
                cost_return = -trade_cost_fraction
                net_returns[index] = (1.0 + gross_returns[index]) * (1.0 + cost_return) - 1.0
            else:
                net_returns[index] = gross_returns[index]
            weights = target
            cash_weight = target_cash
            last_rebalance_index = index
        else:
            net_returns[index] = gross_returns[index]
        cash_weights.append(cash_weight)

    start = lookback
    train_end = int(count * 0.60)
    validation_end = int(count * 0.80)
    boundaries = (train_end, validation_end, start, count)
    return {
        "strategy": strategy,
        "correlation_threshold": correlation_threshold
        if strategy == "negative_correlation_threshold_rebalance" else None,
        "slippage_bps_per_side": slippage_bps_per_side,
        "fee_basis": (
            "KRAKEN_TAKER_FEE" if fee_type == "taker"
            else "HYPOTHETICAL_MAKER_FEE_REFERENCE_NO_FILL_ASSUMPTION"
        ),
        "gross": _curve_metrics(gross_returns[start:]),
        "gross_chronological_splits": _split_returns(gross_returns, boundaries),
        "spot_net": _curve_metrics(net_returns[start:]) if costs_complete else None,
        "spot_net_chronological_splits": _split_returns(net_returns, boundaries) if costs_complete else None,
        "cost_quality": "CURRENT_FEE_AND_SPREAD_PROXY_PLUS_ASSUMED_SLIPPAGE" if costs_complete else "INCOMPLETE_FEE_OR_SPREAD_INPUTS",
        "costed_turnover_pct_of_initial_equity": round(turnover * 100.0, 6),
        "rebalance_count": rebalance_count,
        "selected_negative_correlation_pair_count_sum": selection_count,
        "mean_selected_negative_correlation": round(fmean(selected_correlations), 6) if selected_correlations else None,
        "total_estimated_trading_cost_pct_of_initial_equity": round(cost_total_fraction * 100.0, 6) if costs_complete else None,
        "last_rebalance_index": last_rebalance_index,
        "mean_cash_weight_pct": round(fmean(cash_weights) * 100.0, 6) if cash_weights else None,
    }


def _pair_model(
    prices_a: Sequence[float],
    prices_b: Sequence[float],
    *,
    end_index: int,
    lookback: int = PAIR_REGRESSION_LOOKBACK_BARS,
) -> dict[str, float] | None:
    if end_index + 1 < lookback:
        return None
    start = end_index + 1 - lookback
    log_a = [math.log(value) for value in prices_a[start : end_index + 1] if value > 0]
    log_b = [math.log(value) for value in prices_b[start : end_index + 1] if value > 0]
    if len(log_a) != lookback or len(log_b) != lookback:
        return None
    mean_a = fmean(log_a)
    mean_b = fmean(log_b)
    variance_b = sum((value - mean_b) ** 2 for value in log_b)
    if variance_b <= D_MIN:
        return None
    covariance = sum(
        (a - mean_a) * (b - mean_b)
        for a, b in zip(log_a, log_b, strict=True)
    )
    beta = covariance / variance_b
    alpha = mean_a - beta * mean_b
    if not math.isfinite(beta) or beta < 0.2 or beta > 5.0:
        return None
    residuals = [a - alpha - beta * b for a, b in zip(log_a, log_b, strict=True)]
    residual_mean = fmean(residuals)
    residual_std = pstdev(residuals)
    if not math.isfinite(residual_std) or residual_std <= 1e-6:
        return None
    previous = residuals[:-1]
    following = residuals[1:]
    previous_mean = fmean(previous)
    following_mean = fmean(following)
    variance_previous = sum((value - previous_mean) ** 2 for value in previous)
    if variance_previous <= D_MIN:
        return None
    phi = sum(
        (left - previous_mean) * (right - following_mean)
        for left, right in zip(previous, following, strict=True)
    ) / variance_previous
    if not math.isfinite(phi) or not 0.0 < phi < 1.0:
        return None
    half_life = -math.log(2.0) / math.log(phi)
    if not math.isfinite(half_life) or half_life < 2.0 or half_life > 72.0:
        return None
    returns_a = _returns(prices_a[start : end_index + 1])
    returns_b = _returns(prices_b[start : end_index + 1])
    correlation = _correlation(returns_a, returns_b)
    if correlation is None or correlation < PAIR_MIN_CORRELATION:
        return None
    z_score = (residuals[-1] - residual_mean) / residual_std
    if not math.isfinite(z_score):
        return None
    return {
        "alpha": alpha,
        "beta": beta,
        "residual_mean": residual_mean,
        "residual_std": residual_std,
        "z_score": z_score,
        "return_correlation": correlation,
        "ar1_phi": phi,
        "half_life_bars": half_life,
    }


def _pair_trade_return(
    *,
    direction: str,
    entry_a: float,
    entry_b: float,
    exit_a: float,
    exit_b: float,
    beta: float,
) -> tuple[float, float, float]:
    # Dollar-notional hedge: d(log A) ~= beta * d(log B), so B notional
    # is beta times A notional; price levels must not be applied a second time.
    weight_a = 1.0 / (1.0 + beta)
    weight_b = beta / (1.0 + beta)
    sign_a = 1.0 if direction == "LONG_SPREAD" else -1.0
    sign_b = -sign_a
    pnl = (
        sign_a * weight_a * (exit_a / entry_a - 1.0)
        + sign_b * weight_b * (exit_b / entry_b - 1.0)
    )
    return pnl * 10000.0, weight_a, weight_b


def _pair_trade_summary(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    gross_values = [float(item["gross_return_bps"]) for item in trades]
    net_values = [float(item["net_return_bps"]) for item in trades if item.get("net_return_bps") is not None]
    wins = sum(value for value in net_values if value > 0)
    losses = -sum(value for value in net_values if value < 0)
    return {
        "trade_count": len(trades),
        "gross_mean_bps": round(fmean(gross_values), 6) if gross_values else None,
        "gross_median_bps": round(median(gross_values), 6) if gross_values else None,
        "net_costed_trade_count": len(net_values),
        "net_mean_bps": round(fmean(net_values), 6) if net_values else None,
        "net_median_bps": round(median(net_values), 6) if net_values else None,
        "net_win_rate_pct": round(sum(value > 0 for value in net_values) / len(net_values) * 100.0, 6) if net_values else None,
        "net_profit_factor": round(wins / losses, 6) if losses > 0 else None,
    }


def _backtest_pair(
    symbol_a: str,
    symbol_b: str,
    opens: Mapping[str, Sequence[float]],
    closes: Mapping[str, Sequence[float]],
    market_costs: Mapping[str, Mapping[str, Any]],
    *,
    train_end: int,
    validation_end: int,
    fee_type: str = "taker",
) -> dict[str, Any]:
    count = len(closes[symbol_a])
    trades: list[dict[str, Any]] = []
    pending = 0
    index = PAIR_REGRESSION_LOOKBACK_BARS - 1
    cost_a = _side_cost_components(symbol_a, market_costs, fee_type=fee_type)
    cost_b = _side_cost_components(symbol_b, market_costs, fee_type=fee_type)
    while index < count - 1:
        model = _pair_model(closes[symbol_a], closes[symbol_b], end_index=index)
        if model is None or abs(model["z_score"]) < PAIR_ENTRY_Z:
            index += 1
            continue
        direction = "SHORT_SPREAD" if model["z_score"] > 0 else "LONG_SPREAD"
        entry_index = index + 1
        entry_a = opens[symbol_a][entry_index]
        entry_b = opens[symbol_b][entry_index]
        exit_index: int | None = None
        exit_a = exit_b = 0.0
        exit_reason: str | None = None
        exit_trigger_index: int | None = None
        scheduled_max_exit = entry_index + PAIR_MAX_HOLDING_BARS - 1
        if scheduled_max_exit + 1 >= count:
            pending += 1
            break
        for future_index in range(entry_index, scheduled_max_exit + 1):
            spread = (
                math.log(closes[symbol_a][future_index])
                - model["alpha"]
                - model["beta"] * math.log(closes[symbol_b][future_index])
            )
            z_score = (spread - model["residual_mean"]) / model["residual_std"]
            max_holding_reached = future_index == scheduled_max_exit
            stop_triggered = abs(z_score) >= PAIR_STOP_Z
            mean_reverted = abs(z_score) <= PAIR_EXIT_Z
            if stop_triggered or mean_reverted or max_holding_reached:
                # The close signal is known only after this candle; exit no earlier
                # than the following hourly open to avoid same-close look-ahead.
                execution_index = future_index + 1
                if execution_index >= count:
                    exit_trigger_index = future_index
                    break
                exit_trigger_index = future_index
                exit_index = execution_index
                exit_reason = (
                    "SPREAD_ZSCORE_STOP" if stop_triggered else
                    "MEAN_REVERSION_EXIT" if mean_reverted else
                    "MAX_HOLDING_PERIOD_EXIT"
                )
                exit_a = opens[symbol_a][execution_index]
                exit_b = opens[symbol_b][execution_index]
                break
        if exit_index is None:
            pending += 1
            break
        gross_bps, weight_a, weight_b = _pair_trade_return(
            direction=direction,
            entry_a=entry_a,
            entry_b=entry_b,
            exit_a=exit_a,
            exit_b=exit_b,
            beta=model["beta"],
        )
        fee_per_side: float | None = None
        half_spread_per_side: float | None = None
        cost_per_side: float | None = None
        cost_bps: float | None = None
        net_bps: float | None = None
        if cost_a is not None and cost_b is not None:
            fee_per_side = weight_a * cost_a[0] + weight_b * cost_b[0]
            half_spread_per_side = weight_a * cost_a[1] + weight_b * cost_b[1]
            cost_per_side = fee_per_side + half_spread_per_side + SLIPPAGE_BPS_PER_SIDE
            cost_bps = 2.0 * cost_per_side
            net_bps = gross_bps - cost_bps
        split = (
            "train" if exit_index < train_end and index < train_end else
            "validation" if train_end <= index < validation_end and exit_index < validation_end else
            "test" if index >= validation_end else "purged_at_split_boundary"
        )
        trades.append({
            "symbol_a": symbol_a,
            "symbol_b": symbol_b,
            "direction": direction,
            "decision_index": index,
            "entry_index": entry_index,
            "exit_trigger_index": exit_trigger_index,
            "exit_index": exit_index,
            "entry_price_a": entry_a,
            "entry_price_b": entry_b,
            "exit_price_a": exit_a,
            "exit_price_b": exit_b,
            "leg_weight_a": weight_a,
            "leg_weight_b": weight_b,
            "holding_bars": exit_index - entry_index,
            "exit_reason": exit_reason,
            "exit_execution_reference": "NEXT_H1_OPEN_AFTER_CLOSE_SIGNAL",
            "entry_z_score": model["z_score"],
            "return_correlation": model["return_correlation"],
            "half_life_bars": model["half_life_bars"],
            "hedge_ratio_beta": model["beta"],
            "gross_return_bps": gross_bps,
            "fee_per_side_bps": fee_per_side,
            "fee_basis": (
                "KRAKEN_TAKER_FEE" if fee_type == "taker"
                else "HYPOTHETICAL_MAKER_FEE_REFERENCE_NO_FILL_ASSUMPTION"
            ),
            "half_spread_per_side_bps": half_spread_per_side,
            "spot_round_trip_cost_bps": cost_bps,
            "net_return_bps": net_bps,
            "split": split,
        })
        index = exit_index
    by_split = {
        name: _pair_trade_summary([item for item in trades if item["split"] == name])
        for name in ("train", "validation", "test")
    }
    return {
        "symbol_a": symbol_a,
        "symbol_b": symbol_b,
        "trades": trades,
        "by_split": by_split,
        "pending_at_sample_end": pending,
        "purged_split_boundary_trades": sum(item["split"] == "purged_at_split_boundary" for item in trades),
    }


def _latest_pair_signals(
    symbols: Sequence[str],
    closes: Mapping[str, Sequence[float]],
    market_costs: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    index = len(next(iter(closes.values()))) - 1
    eligible_count = 0
    signals: list[dict[str, Any]] = []
    for symbol_a, symbol_b in combinations(symbols, 2):
        model = _pair_model(closes[symbol_a], closes[symbol_b], end_index=index)
        if model is None:
            continue
        eligible_count += 1
        direction = (
            "SHORT_SPREAD" if model["z_score"] >= PAIR_ENTRY_Z else
            "LONG_SPREAD" if model["z_score"] <= -PAIR_ENTRY_Z else "NONE"
        )
        costs_a = market_costs.get(symbol_a, {})
        costs_b = market_costs.get(symbol_b, {})
        required_margin_permissions = (
            (bool(costs_a.get("margin_sell_supported")), bool(costs_b.get("margin_buy_supported")))
            if direction == "SHORT_SPREAD" else
            (bool(costs_a.get("margin_buy_supported")), bool(costs_b.get("margin_sell_supported")))
            if direction == "LONG_SPREAD" else (False, False)
        )
        signals.append({
            "symbol_a": symbol_a,
            "symbol_b": symbol_b,
            "direction": direction,
            "required_leg_margin_permissions": {
                "symbol_a": "SELL" if direction == "SHORT_SPREAD" else "BUY",
                "symbol_b": "BUY" if direction == "SHORT_SPREAD" else "SELL",
                "symbol_a_supported": required_margin_permissions[0],
                "symbol_b_supported": required_margin_permissions[1],
            },
            "margin_execution_eligible_from_instrument_metadata": (
                direction in {"LONG_SPREAD", "SHORT_SPREAD"} and all(required_margin_permissions)
            ),
            "signal_reason": (
                "SPREAD_ZSCORE_HIGH" if direction == "SHORT_SPREAD" else
                "SPREAD_ZSCORE_LOW" if direction == "LONG_SPREAD" else "SPREAD_WITHIN_ENTRY_BAND"
            ),
            "z_score": round(model["z_score"], 6),
            "return_correlation": round(model["return_correlation"], 6),
            "hedge_ratio_beta": round(model["beta"], 8),
            "ar1_phi": round(model["ar1_phi"], 8),
            "half_life_bars": round(model["half_life_bars"], 6),
            "score_is_calibrated_probability": False,
            "order_created": False,
        })
    signals.sort(key=lambda item: (-abs(float(item["z_score"])), item["symbol_a"], item["symbol_b"]))
    return signals[:10], eligible_count


def _pair_portfolio_simulation(
    trades: Sequence[Mapping[str, Any]],
    opens: Mapping[str, Sequence[float]],
    closes: Mapping[str, Sequence[float]],
    *,
    bar_count: int,
    train_end: int,
    validation_end: int,
    slippage_bps_per_side: float,
) -> dict[str, Any]:
    """Research portfolio with fixed slots, asset de-duplication and mark-to-market."""
    candidates = sorted(
        (
            item for item in trades
            if item.get("split") != "purged_at_split_boundary"
            and int(item.get("exit_index", -1)) > int(item.get("entry_index", -1))
        ),
        key=lambda item: (
            int(item["entry_index"]),
            -abs(float(item.get("entry_z_score", 0.0))),
            str(item.get("symbol_a", "")),
            str(item.get("symbol_b", "")),
        ),
    )
    accepted: list[Mapping[str, Any]] = []
    skipped_slot_limit = 0
    skipped_asset_overlap = 0
    for candidate in candidates:
        entry_index = int(candidate["entry_index"])
        active = [item for item in accepted if int(item["exit_index"]) > entry_index]
        if len(active) >= MAX_CONCURRENT_PAIR_POSITIONS:
            skipped_slot_limit += 1
            continue
        candidate_assets = {str(candidate["symbol_a"]), str(candidate["symbol_b"])}
        active_assets = {
            str(item["symbol_a"]) for item in active
        } | {
            str(item["symbol_b"]) for item in active
        }
        if candidate_assets & active_assets:
            skipped_asset_overlap += 1
            continue
        accepted.append(candidate)

    gross_pnl_fraction = [0.0] * bar_count
    cost_pnl_fraction = [0.0] * bar_count
    costs_complete = bool(accepted) and all(
        item.get("fee_per_side_bps") is not None
        and item.get("half_spread_per_side_bps") is not None
        for item in accepted
    )
    turnover_fraction = 0.0
    for item in accepted:
        entry_index = int(item["entry_index"])
        exit_index = int(item["exit_index"])
        symbol_a = str(item["symbol_a"])
        symbol_b = str(item["symbol_b"])
        weight_a = float(item["leg_weight_a"])
        weight_b = float(item["leg_weight_b"])
        long_a = 1.0 if item["direction"] == "LONG_SPREAD" else -1.0
        long_b = -long_a
        entry_a = float(item["entry_price_a"])
        entry_b = float(item["entry_price_b"])
        slot = PAIR_SLOT_EQUITY_FRACTION
        turnover_fraction += 2.0 * slot
        for bar_index in range(entry_index, exit_index + 1):
            if bar_index == exit_index:
                previous_a = float(closes[symbol_a][bar_index - 1])
                previous_b = float(closes[symbol_b][bar_index - 1])
                current_a = float(opens[symbol_a][bar_index])
                current_b = float(opens[symbol_b][bar_index])
            elif bar_index == entry_index:
                previous_a = entry_a
                previous_b = entry_b
                current_a = float(closes[symbol_a][bar_index])
                current_b = float(closes[symbol_b][bar_index])
            else:
                previous_a = float(closes[symbol_a][bar_index - 1])
                previous_b = float(closes[symbol_b][bar_index - 1])
                current_a = float(closes[symbol_a][bar_index])
                current_b = float(closes[symbol_b][bar_index])
            period_return = (
                long_a * weight_a * (current_a / previous_a - 1.0)
                + long_b * weight_b * (current_b / previous_b - 1.0)
            )
            gross_pnl_fraction[bar_index] += slot * period_return

        if costs_complete:
            cost_side = (
                float(item["fee_per_side_bps"])
                + float(item["half_spread_per_side_bps"])
                + slippage_bps_per_side
            )
            cost_fraction = slot * cost_side / 10000.0
            cost_pnl_fraction[entry_index] -= cost_fraction
            cost_pnl_fraction[exit_index] -= cost_fraction

    def equity_curve(period_pnl: Sequence[float]) -> tuple[list[float], list[float]]:
        equity = 1.0
        returns: list[float] = [0.0] * bar_count
        curve: list[float] = [equity] * bar_count
        for index, delta in enumerate(period_pnl):
            if not math.isfinite(delta):
                raise ValueError("PAIR_PORTFOLIO_PNL_NON_FINITE")
            if equity + delta <= 0.0:
                returns[index] = -1.0
                equity = 0.0
            else:
                returns[index] = delta / equity
                equity += delta
            curve[index] = equity
            if equity <= 0:
                for rest in range(index + 1, bar_count):
                    curve[rest] = 0.0
                    returns[rest] = 0.0
                break
        return returns, curve

    gross_returns, _gross_curve = equity_curve(gross_pnl_fraction)
    gross_metrics = _curve_metrics(gross_returns[PAIR_REGRESSION_LOOKBACK_BARS:])
    gross_splits = _split_returns(
        gross_returns,
        (train_end, validation_end, PAIR_REGRESSION_LOOKBACK_BARS, bar_count),
    )
    net_metrics = None
    net_splits = None
    if costs_complete:
        net_pnl_fraction = [
            gross_pnl_fraction[index] + cost_pnl_fraction[index]
            for index in range(bar_count)
        ]
        net_returns, _net_curve = equity_curve(net_pnl_fraction)
        net_metrics = _curve_metrics(net_returns[PAIR_REGRESSION_LOOKBACK_BARS:])
        net_splits = _split_returns(
            net_returns,
            (train_end, validation_end, PAIR_REGRESSION_LOOKBACK_BARS, bar_count),
        )
    active_counts = [
        sum(
            int(item["entry_index"]) <= index < int(item["exit_index"])
            for item in accepted
        )
        for index in range(bar_count)
    ]
    return {
        "simulation": "CONCURRENT_PAIR_PORTFOLIO_HOURLY_MARK_TO_MARKET",
        "capital_basis": "FIXED_INITIAL_EQUITY",
        "pair_slot_fraction_of_initial_equity": PAIR_SLOT_EQUITY_FRACTION,
        "maximum_concurrent_pairs": MAX_CONCURRENT_PAIR_POSITIONS,
        "observed_max_concurrent_pairs": max(active_counts, default=0),
        "maximum_combined_gross_notional_pct_of_initial_equity": (
            MAX_CONCURRENT_PAIR_POSITIONS * PAIR_SLOT_EQUITY_FRACTION * 100.0
        ),
        "leverage_used": False,
        "short_leg_requires_margin": True,
        "margin_execution_simulated": False,
        "margin_financing_costs_included": False,
        "selected_trade_count": len(accepted),
        "candidate_trade_count": len(candidates),
        "skipped_max_concurrent_pairs": skipped_slot_limit,
        "skipped_due_to_asset_overlap": skipped_asset_overlap,
        "turnover_pct_of_initial_equity": round(turnover_fraction * 100.0, 6),
        "gross_portfolio": gross_metrics,
        "gross_chronological_splits": gross_splits,
        "spot_net_portfolio": net_metrics,
        "spot_net_chronological_splits": net_splits,
        "slippage_bps_per_side": slippage_bps_per_side,
        "net_cost_quality": (
            "CURRENT_FEE_AND_SPREAD_PROXY_PLUS_ASSUMED_SLIPPAGE"
            if costs_complete else "INCOMPLETE_FEE_OR_SPREAD_INPUTS"
        ),
        "actual_fills": False,
        "portfolio_pnl": False,
    }


def _simulate_cross_sectional_momentum(
    market: Mapping[str, Any],
    market_costs: Mapping[str, Mapping[str, Any]],
    *,
    slippage_bps_per_side: float = SLIPPAGE_BPS_PER_SIDE,
    fee_type: str = "taker",
) -> dict[str, Any]:
    """Research a fixed long/short cross-sectional momentum portfolio.

    Scores use only closed bars through the decision timestamp. Orders are
    hypothetically executed at the following hourly open; PnL is marked from
    that open to the next open. Dollar exposures stay fixed to initial equity.
    """
    symbols = list(market["symbols"])
    opens = market["opens"]
    closes = market["closes"]
    count = len(market["timestamps"])
    start = MOMENTUM_SLOW_LOOKBACK_BARS + 1
    if count <= start + 1:
        return {
            "status": "INSUFFICIENT_SYNCHRONIZED_HISTORY",
            "portfolio_simulation": True,
            "actual_fills": False,
            "portfolio_pnl": False,
        }

    weights = {symbol: 0.0 for symbol in symbols}
    gross_pnl = [0.0] * count
    cost_pnl = [0.0] * count
    turnover = 0.0
    cost_total_fraction = 0.0
    rebalance_count = 0
    invested_rebalance_count = 0
    cash_rebalance_count = 0
    selected_long_counts: list[int] = []
    selected_short_counts: list[int] = []
    cash_weights: list[float] = []
    costs_complete = True

    for execution_index in range(start, count - 1):
        decision_index = execution_index - 1
        if (
            decision_index - MOMENTUM_SLOW_LOOKBACK_BARS
        ) % MOMENTUM_REBALANCE_INTERVAL_BARS == 0:
            ranked: list[tuple[str, float]] = []
            for symbol in symbols:
                fast_base = closes[symbol][decision_index - MOMENTUM_FAST_LOOKBACK_BARS]
                slow_base = closes[symbol][decision_index - MOMENTUM_SLOW_LOOKBACK_BARS]
                current = closes[symbol][decision_index]
                if min(fast_base, slow_base, current) <= 0:
                    continue
                fast_return = current / fast_base - 1.0
                slow_return = current / slow_base - 1.0
                score = (
                    MOMENTUM_SCORE_FAST_WEIGHT * fast_return
                    + MOMENTUM_SCORE_SLOW_WEIGHT * slow_return
                )
                if math.isfinite(score):
                    ranked.append((symbol, score))
            ranked.sort(key=lambda item: (-item[1], item[0]))
            positive = [item for item in ranked if item[1] > 0]
            negative = [item for item in ranked if item[1] < 0]
            long_symbols = [item[0] for item in positive[:MOMENTUM_SIDE_ASSET_COUNT]]
            short_symbols = [
                item[0] for item in sorted(
                    negative, key=lambda item: (item[1], item[0])
                )[:MOMENTUM_SIDE_ASSET_COUNT]
            ]
            target = {symbol: 0.0 for symbol in symbols}
            if (
                len(long_symbols) == MOMENTUM_SIDE_ASSET_COUNT
                and len(short_symbols) == MOMENTUM_SIDE_ASSET_COUNT
            ):
                for symbol in long_symbols:
                    target[symbol] = MOMENTUM_GROSS_WEIGHT_PER_SIDE / len(long_symbols)
                for symbol in short_symbols:
                    target[symbol] = -MOMENTUM_GROSS_WEIGHT_PER_SIDE / len(short_symbols)
                invested_rebalance_count += 1
            else:
                long_symbols = []
                short_symbols = []
                cash_rebalance_count += 1
            selected_long_counts.append(len(long_symbols))
            selected_short_counts.append(len(short_symbols))
            deltas = {
                symbol: target[symbol] - weights[symbol]
                for symbol in symbols
            }
            trade_cost = 0.0
            for symbol, delta in deltas.items():
                if abs(delta) <= 1e-15:
                    continue
                components = _side_cost_components(
                    symbol,
                    market_costs,
                    slippage_bps_per_side=slippage_bps_per_side,
                    fee_type=fee_type,
                )
                if components is None:
                    costs_complete = False
                    continue
                side_cost_bps = sum(components)
                trade_cost += abs(delta) * side_cost_bps / 10000.0
            turnover += sum(abs(delta) for delta in deltas.values())
            cost_total_fraction += trade_cost
            cost_pnl[execution_index] -= trade_cost
            weights = target
            rebalance_count += 1
            cash_weights.append(
                max(0.0, 1.0 - sum(abs(value) for value in weights.values()))
            )

        # The decision uses the previous closed bar; execution/PnL starts at
        # the next open and ends at the following open.
        gross_pnl[execution_index] = sum(
            weights[symbol] * (
                opens[symbol][execution_index + 1] / opens[symbol][execution_index] - 1.0
            )
            for symbol in symbols
        )

    def return_series_from_fixed_equity_deltas(deltas: Sequence[float]) -> list[float]:
        equity = 1.0
        returns = [0.0] * count
        for index, delta in enumerate(deltas):
            if not math.isfinite(delta):
                raise ValueError("CROSS_SECTIONAL_PNL_NON_FINITE")
            if equity <= 0.0:
                break
            returns[index] = delta / equity
            equity += delta
        return returns

    gross_returns = return_series_from_fixed_equity_deltas(gross_pnl)
    net_returns = (
        return_series_from_fixed_equity_deltas(
            [gross_pnl[index] + cost_pnl[index] for index in range(count)]
        )
        if costs_complete else None
    )
    train_end = int(count * 0.60)
    validation_end = int(count * 0.80)
    boundaries = (train_end, validation_end, start, count - 1)

    latest_index = count - 1
    latest_scores: list[tuple[str, float]] = []
    for symbol in symbols:
        current = closes[symbol][latest_index]
        fast_base = closes[symbol][latest_index - MOMENTUM_FAST_LOOKBACK_BARS]
        slow_base = closes[symbol][latest_index - MOMENTUM_SLOW_LOOKBACK_BARS]
        score = (
            MOMENTUM_SCORE_FAST_WEIGHT * (current / fast_base - 1.0)
            + MOMENTUM_SCORE_SLOW_WEIGHT * (current / slow_base - 1.0)
        )
        if math.isfinite(score):
            latest_scores.append((symbol, score))
    latest_scores.sort(key=lambda item: (-item[1], item[0]))
    latest_long = [
        item[0] for item in latest_scores if item[1] > 0
    ][:MOMENTUM_SIDE_ASSET_COUNT]
    latest_short = [
        item[0] for item in sorted(
            (item for item in latest_scores if item[1] < 0),
            key=lambda item: (item[1], item[0]),
        )[:MOMENTUM_SIDE_ASSET_COUNT]
    ]

    return {
        "status": "CANDIDATE_RESEARCH",
        "strategy": "CROSS_SECTIONAL_MOMENTUM_LONG_SHORT",
        "method": {
            "fast_lookback_bars": MOMENTUM_FAST_LOOKBACK_BARS,
            "slow_lookback_bars": MOMENTUM_SLOW_LOOKBACK_BARS,
            "fast_score_weight": MOMENTUM_SCORE_FAST_WEIGHT,
            "slow_score_weight": MOMENTUM_SCORE_SLOW_WEIGHT,
            "rebalance_interval_bars": MOMENTUM_REBALANCE_INTERVAL_BARS,
            "long_assets": MOMENTUM_SIDE_ASSET_COUNT,
            "short_assets": MOMENTUM_SIDE_ASSET_COUNT,
            "gross_weight_per_side": MOMENTUM_GROSS_WEIGHT_PER_SIDE,
            "gross_exposure_pct": 80.0,
            "net_exposure_pct": 0.0,
            "target_cash_weight_pct": 20.0,
            "maximum_single_asset_weight_pct": round(
                MOMENTUM_GROSS_WEIGHT_PER_SIDE / MOMENTUM_SIDE_ASSET_COUNT * 100.0, 6
            ),
            "selection_uses_only_closed_decision_bar": True,
            "execution_reference": "NEXT_H1_OPEN_TO_NEXT_H1_OPEN",
            "fixed_initial_equity_notional": True,
            "leverage_used": False,
        },
        "fee_basis": (
            "KRAKEN_TAKER_FEE" if fee_type == "taker"
            else "HYPOTHETICAL_MAKER_FEE_REFERENCE_NO_FILL_ASSUMPTION"
        ),
        "slippage_bps_per_side": slippage_bps_per_side,
        "gross": _curve_metrics(gross_returns[start:count - 1]),
        "gross_chronological_splits": _split_returns(gross_returns, boundaries),
        "costed_proxy": (
            _curve_metrics(net_returns[start:count - 1]) if net_returns is not None else None
        ),
        "costed_proxy_chronological_splits": (
            _split_returns(net_returns, boundaries) if net_returns is not None else None
        ),
        "cost_quality": (
            "CURRENT_KRAKEN_FEE_AND_TICKER_SPREAD_PROXY_PLUS_ASSUMED_SLIPPAGE"
            if costs_complete else "INCOMPLETE_FEE_OR_SPREAD_INPUTS"
        ),
        "turnover_pct_of_initial_equity": round(turnover * 100.0, 6),
        "estimated_trading_cost_pct_of_initial_equity": (
            round(cost_total_fraction * 100.0, 6) if costs_complete else None
        ),
        "rebalance_count": rebalance_count,
        "invested_rebalance_count": invested_rebalance_count,
        "cash_rebalance_count": cash_rebalance_count,
        "mean_selected_long_assets": (
            round(fmean(selected_long_counts), 6) if selected_long_counts else 0.0
        ),
        "mean_selected_short_assets": (
            round(fmean(selected_short_counts), 6) if selected_short_counts else 0.0
        ),
        "mean_cash_weight_pct": (
            round(fmean(cash_weights) * 100.0, 6) if cash_weights else 100.0
        ),
        "latest_long_candidates": latest_long,
        "latest_short_candidates": latest_short,
        "latest_rankings": [
            {"symbol": symbol, "score": round(score, 8)}
            for symbol, score in latest_scores[:12]
        ],
        "margin_shorting_required": True,
        "margin_financing_costs_included": False,
        "margin_net_returns_calculated": False,
        "actual_fills": False,
        "portfolio_pnl": False,
    }


def _rank_pair_correlations(
    closes: Mapping[str, Sequence[float]],
    *,
    index: int,
    lookback: int = CORRELATION_LOOKBACK_BARS,
    limit: int = 10,
) -> list[dict[str, Any]]:
    if index < lookback:
        return []
    ranked: list[tuple[str, str, float]] = []
    for left, right in combinations(sorted(closes), 2):
        left_returns = _returns(closes[left][index - lookback : index + 1])
        right_returns = _returns(closes[right][index - lookback : index + 1])
        correlation = _correlation(left_returns, right_returns)
        if correlation is not None:
            ranked.append((left, right, correlation))
    ranked.sort(key=lambda item: (item[2], item[0], item[1]))
    return [
        {"symbol_a": left, "symbol_b": right, "return_correlation": round(value, 6)}
        for left, right, value in ranked[:limit]
    ]


def _time_separated_l2_cost_diagnostics(
    depth_history: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> dict[str, Any]:
    """Estimate round-trip execution costs from distinct historical public L2 books.

    These are book-walk simulations, not actual fills. Entry and exit snapshots
    are separated in time; each observation uses the account taker fee stored
    with each snapshot, visible depth, and a fixed per-level participation cap.
    """
    source = depth_history or {}
    horizons = (1, 4, 24)
    notional_values = ORDER_BOOK_NOTIONAL_USD
    grouped: dict[tuple[int, float, str], dict[str, list[float]]] = {}
    for horizon in horizons:
        for notional in notional_values:
            for direction in ("LONG", "SHORT"):
                grouped[(horizon, notional, direction)] = {
                    "total": [], "fee": [], "spread": [], "slippage": []
                }

    total_snapshots = 0
    paired_snapshot_count = {horizon: 0 for horizon in horizons}
    insufficient_depth_count = 0
    missing_fee_count = 0
    invalid_snapshot_count = 0
    symbol_counts: dict[str, int] = {}

    for symbol, raw_snapshots in sorted(source.items()):
        parsed: list[tuple[OrderBookSnapshot, Decimal | None]] = []
        for raw in raw_snapshots:
            try:
                bids = tuple(
                    BookLevel(Decimal(str(row[0])), Decimal(str(row[1])))
                    for row in raw["bids"]
                )
                asks = tuple(
                    BookLevel(Decimal(str(row[0])), Decimal(str(row[1])))
                    for row in raw["asks"]
                )
                book = OrderBookSnapshot(
                    timestamp=int(raw["snapshot_ts"]),
                    bids=bids,
                    asks=asks,
                )
                raw_fee = raw.get("taker_fee_bps")
                fee = Decimal(str(raw_fee)) if raw_fee is not None else None
                if fee is not None and (not fee.is_finite() or fee < 0):
                    fee = None
                parsed.append((book, fee))
            except (ArithmeticError, KeyError, TypeError, ValueError, OverflowError):
                invalid_snapshot_count += 1
        parsed.sort(key=lambda item: item[0].timestamp)
        total_snapshots += len(parsed)
        symbol_counts[str(symbol)] = len(parsed)
        if len(parsed) < 2:
            continue

        snapshot_timestamps = [item[0].timestamp for item in parsed]
        for horizon in horizons:
            target_seconds = horizon * 3600
            minimum_seconds = int(target_seconds * 0.75)
            maximum_seconds = int(target_seconds * 1.25)
            for entry_index, (entry_book, entry_fee) in enumerate(parsed[:-1]):
                target_timestamp = entry_book.timestamp + target_seconds
                insertion_index = bisect_left(snapshot_timestamps, target_timestamp)
                candidate_indices = [
                    candidate_index
                    for candidate_index in (insertion_index - 1, insertion_index)
                    if candidate_index > entry_index
                    and 0 <= candidate_index < len(parsed)
                    and minimum_seconds
                    <= snapshot_timestamps[candidate_index] - entry_book.timestamp
                    <= maximum_seconds
                ]
                if not candidate_indices:
                    continue
                exit_index = min(
                    candidate_indices,
                    key=lambda candidate_index: (
                        abs(snapshot_timestamps[candidate_index] - target_timestamp),
                        snapshot_timestamps[candidate_index],
                    ),
                )
                exit_book, exit_fee = parsed[exit_index]
                paired_snapshot_count[horizon] += 1
                if entry_fee is None or exit_fee is None:
                    missing_fee_count += 1
                    continue
                for notional in notional_values:
                    for direction in ("LONG", "SHORT"):
                        try:
                            result = simulate_order_book_round_trip(
                                entry_book,
                                exit_book,
                                direction=direction,
                                requested_notional_quote=Decimal(str(notional)),
                                entry_fee_bps=entry_fee,
                                exit_fee_bps=exit_fee,
                                quote_currency="USD",
                                is_margin=False,
                                max_level_participation=Decimal("0.25"),
                            )
                        except (ArithmeticError, TypeError, ValueError, OverflowError):
                            invalid_snapshot_count += 1
                            continue
                        if (
                            result.position_status != "CLOSED"
                            or not result.entry_target_filled
                            or result.spread_slippage_cost_quote is None
                            or result.spread_cost_quote is None
                            or result.slippage_impact_quote is None
                            or result.trading_fees_quote is None
                        ):
                            insufficient_depth_count += 1
                            continue
                        denominator = Decimal(str(notional))
                        costs = {
                            "total": (
                                result.spread_slippage_cost_quote
                                + result.trading_fees_quote
                            ) / denominator * Decimal(10000),
                            "fee": result.trading_fees_quote / denominator * Decimal(10000),
                            "spread": result.spread_cost_quote / denominator * Decimal(10000),
                            "slippage": result.slippage_impact_quote / denominator * Decimal(10000),
                        }
                        key = (horizon, notional, direction)
                        for component, amount in costs.items():
                            if amount.is_finite() and amount >= 0:
                                grouped[key][component].append(float(amount))

    def percentile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        position = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
        return round(ordered[position], 6)

    by_horizon: list[dict[str, Any]] = []
    total_completed_samples = 0
    for horizon in horizons:
        scenarios: list[dict[str, Any]] = []
        for notional in notional_values:
            for direction in ("LONG", "SHORT"):
                values = grouped[(horizon, notional, direction)]
                count = len(values["total"])
                total_completed_samples += count
                scenarios.append({
                    "notional_usd": notional,
                    "direction": direction,
                    "sample_count": count,
                    "round_trip_cost_bps": {
                        "median": percentile(values["total"], 0.50),
                        "p90": percentile(values["total"], 0.90),
                        "minimum": round(min(values["total"]), 6) if values["total"] else None,
                        "maximum": round(max(values["total"]), 6) if values["total"] else None,
                    },
                    "components_bps_median": {
                        "trading_fees": percentile(values["fee"], 0.50),
                        "spread": percentile(values["spread"], 0.50),
                        "visible_depth_slippage": percentile(values["slippage"], 0.50),
                    },
                })
        by_horizon.append({
            "target_horizon_hours": horizon,
            "pair_observation_count": paired_snapshot_count[horizon],
            "scenarios": scenarios,
        })

    return {
        "status": (
            "TIME_SEPARATED_L2_COST_SAMPLES_AVAILABLE"
            if total_completed_samples else "INSUFFICIENT_TIME_SEPARATED_L2_COST_EVIDENCE"
        ),
        "source": "PERSISTED_KRAKEN_PUBLIC_DEPTH_SNAPSHOTS",
        "snapshot_count": total_snapshots,
        "symbols_with_snapshots": symbol_counts,
        "completed_round_trip_simulation_count": total_completed_samples,
        "pair_observations_are_independent": False,
        "matching": "NEAREST_FUTURE_SNAPSHOT_WITHIN_PLUS_MINUS_25_PERCENT_OF_TARGET_HORIZON",
        "horizons": by_horizon,
        "insufficient_visible_depth_count": insufficient_depth_count,
        "missing_fee_snapshot_count": missing_fee_count,
        "invalid_snapshot_or_simulation_count": invalid_snapshot_count,
        "maximum_level_participation": 0.25,
        "margin_financing_included": False,
        "maker_fills_assumed": False,
        "actual_fills": False,
        "portfolio_pnl": False,
    }


def _current_order_book_cost_diagnostics(
    market_costs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Current visible-book estimate at standard USD sizes; never historic fills."""
    by_symbol: dict[str, Any] = {}
    for symbol, raw in sorted(market_costs.items()):
        book_data = raw.get("order_book_l2")
        fee = _finite_float(raw.get("taker_fee_bps"))
        maker_fee = _finite_float(raw.get("maker_fee_bps"))
        if not isinstance(book_data, Mapping) or fee is None or fee < 0:
            continue
        try:
            bids = tuple(
                BookLevel(Decimal(str(row[0])), Decimal(str(row[1])))
                for row in book_data["bids"]
            )
            asks = tuple(
                BookLevel(Decimal(str(row[0])), Decimal(str(row[1])))
                for row in book_data["asks"]
            )
            snapshot = OrderBookSnapshot(
                timestamp=int(book_data["snapshot_ts"]),
                bids=bids,
                asks=asks,
            )
        except (ArithmeticError, KeyError, TypeError, ValueError, OverflowError):
            by_symbol[symbol] = {"status": "INVALID_ORDER_BOOK_SNAPSHOT"}
            continue
        mid = (snapshot.bids[0].price + snapshot.asks[0].price) / Decimal(2)
        notionals: list[dict[str, Any]] = []
        for notional in ORDER_BOOK_NOTIONAL_USD:
            quantity = Decimal(str(notional)) / mid
            buy = simulate_order_book_execution(
                snapshot,
                side="BUY",
                quantity=quantity,
                fee_bps=Decimal(str(fee)),
                max_level_participation=Decimal("0.25"),
            )
            sell = simulate_order_book_execution(
                snapshot,
                side="SELL",
                quantity=quantity,
                fee_bps=Decimal(str(fee)),
                max_level_participation=Decimal("0.25"),
            )
            complete = buy.complete and sell.complete
            notionals.append({
                "notional_usd": notional,
                "status": "VISIBLE_DEPTH_FILLED" if complete else "VISIBLE_DEPTH_INSUFFICIENT",
                "buy_slippage_bps": (
                    str(buy.slippage_bps) if buy.slippage_bps is not None else None
                ),
                "sell_slippage_bps": (
                    str(sell.slippage_bps) if sell.slippage_bps is not None else None
                ),
                "spread_bps": str(buy.spread_bps),
                "round_trip_taker_fee_bps": str(Decimal(2) * Decimal(str(fee))),
                "round_trip_maker_fee_reference_bps": (
                    str(Decimal(2) * Decimal(str(maker_fee)))
                    if maker_fee is not None else None
                ),
                "maker_fee_is_not_a_fill_assumption": True,
                "round_trip_execution_cost_bps": (
                    str(
                        Decimal(2) * Decimal(str(fee)) + buy.spread_bps
                        + (buy.slippage_bps or Decimal(0))
                        + (sell.slippage_bps or Decimal(0))
                    )
                    if complete else None
                ),
            })
        by_symbol[symbol] = {
            "status": "CURRENT_L2_COST_ESTIMATE",
            "snapshot_ts": snapshot.timestamp,
            "level_count_bids": len(snapshot.bids),
            "level_count_asks": len(snapshot.asks),
            "max_level_participation": 0.25,
            "round_trip_assumption": "SAME_SNAPSHOT_BUY_AND_SELL; NOT_A_FILL",
            "fee_basis": "KRAKEN_TAKER_FEE",
            "maker_fee_reference_is_not_an_execution_assumption": True,
            "notional_scenarios": notionals,
        }
    return {
        "source": "KRAKEN_PUBLIC_DEPTH_CURRENT_SNAPSHOT",
        "instrument_count": len(by_symbol),
        "by_symbol": by_symbol,
        "historical_cost_evidence": False,
        "actual_fills": False,
    }


def run_multi_asset_strategy_research(
    histories: Mapping[str, Sequence[Mapping[str, Any]]],
    market_costs: Mapping[str, Mapping[str, Any]],
    *,
    now_epoch: int | None = None,
    depth_history: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run independent historical and current-shadow research for variants A, B, C and D.

    No live trading, portfolio allocation approval, or order creation is performed.
    Costed results use current account fee/spread observations as historical proxies;
    they are not actual historical fills or confirmed margin financing costs.
    """
    market = _aligned_market_histories(histories)
    if market["status"] != "READY":
        return {
            "research_version": PORTFOLIO_RESEARCH_VERSION,
            "status": market["status"],
            "release_gate_eligible": False,
            "actual_fills": False,
            "portfolio_pnl": False,
            "order_creation_allowed": False,
            "symbols": market.get("symbols", []),
            "synchronized_bar_count": len(market.get("timestamps", [])),
        }

    symbols = market["symbols"]
    timestamps = market["timestamps"]
    closes = market["closes"]
    opens = market["opens"]
    count = len(timestamps)
    train_end = int(count * 0.60)
    validation_end = int(count * 0.80)

    negative_pairs = _select_negative_pairs(closes, index=count - 1)
    negative_pair_report = [
        {"symbol_a": left, "symbol_b": right, "return_correlation": round(correlation, 6)}
        for left, right, correlation in negative_pairs
    ]
    ranked_correlations = _rank_pair_correlations(closes, index=count - 1)

    strategy_a = {
        "parrondo_negative_correlation_rebalancing": _simulate_rebalancing(
            market, market_costs, strategy="negative_correlation_threshold_rebalance"
        ),
        "periodic_equal_weight_rebalancing_benchmark": _simulate_rebalancing(
            market, market_costs, strategy="periodic_equal_weight_rebalance"
        ),
        "buy_and_hold_equal_weight_benchmark": _simulate_rebalancing(
            market, market_costs, strategy="buy_and_hold_equal_weight"
        ),
    }
    correlation_threshold_sensitivity = []
    for threshold in CORRELATION_SENSITIVITY_THRESHOLDS:
        candidate = _simulate_rebalancing(
            market,
            market_costs,
            strategy="negative_correlation_threshold_rebalance",
            correlation_threshold=threshold,
        )
        correlation_threshold_sensitivity.append({
            "threshold": threshold,
            "selected_pair_count_sum": candidate["selected_negative_correlation_pair_count_sum"],
            "rebalance_count": candidate["rebalance_count"],
            "mean_cash_weight_pct": candidate["mean_cash_weight_pct"],
            "gross": candidate["gross"],
            "spot_net": candidate["spot_net"],
            "spot_net_chronological_splits": candidate["spot_net_chronological_splits"],
            "status": "EXPLORATORY_PREDECLARED_THRESHOLD_SENSITIVITY_NOT_PROMOTED",
        })
    variant_a_slippage_sensitivity = []
    for assumed_slippage in SLIPPAGE_SENSITIVITY_BPS_PER_SIDE:
        scenario = _simulate_rebalancing(
            market,
            market_costs,
            strategy="negative_correlation_threshold_rebalance",
            slippage_bps_per_side=assumed_slippage,
        )
        variant_a_slippage_sensitivity.append({
            "slippage_bps_per_side": assumed_slippage,
            "spot_net": scenario["spot_net"],
            "spot_net_chronological_splits": scenario["spot_net_chronological_splits"],
            "status": "COST_ASSUMPTION_SENSITIVITY_NOT_HISTORICAL_EXECUTION",
        })

    pair_results: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    maker_reference_trades: list[dict[str, Any]] = []
    for symbol_a, symbol_b in combinations(symbols, 2):
        pair_result = _backtest_pair(
            symbol_a,
            symbol_b,
            opens,
            closes,
            market_costs,
            train_end=train_end,
            validation_end=validation_end,
            fee_type="taker",
        )
        maker_reference_result = _backtest_pair(
            symbol_a,
            symbol_b,
            opens,
            closes,
            market_costs,
            train_end=train_end,
            validation_end=validation_end,
            fee_type="maker_reference",
        )
        if pair_result["trades"] or pair_result["pending_at_sample_end"]:
            all_trades.extend(pair_result["trades"])
            maker_reference_trades.extend(maker_reference_result["trades"])
            pair_results.append({
                "symbol_a": symbol_a,
                "symbol_b": symbol_b,
                "by_split": pair_result["by_split"],
                "pending_at_sample_end": pair_result["pending_at_sample_end"],
                "purged_split_boundary_trades": pair_result["purged_split_boundary_trades"],
            })

    pair_trade_summary = {
        split: _pair_trade_summary([item for item in all_trades if item["split"] == split])
        for split in ("train", "validation", "test")
    }
    pair_portfolio = _pair_portfolio_simulation(
        all_trades,
        opens,
        closes,
        bar_count=count,
        train_end=train_end,
        validation_end=validation_end,
        slippage_bps_per_side=SLIPPAGE_BPS_PER_SIDE,
    )
    maker_fee_reference_portfolio = _pair_portfolio_simulation(
        maker_reference_trades,
        opens,
        closes,
        bar_count=count,
        train_end=train_end,
        validation_end=validation_end,
        slippage_bps_per_side=SLIPPAGE_BPS_PER_SIDE,
    )
    pair_portfolio_slippage_sensitivity = []
    for assumed_slippage in SLIPPAGE_SENSITIVITY_BPS_PER_SIDE:
        scenario = _pair_portfolio_simulation(
            all_trades,
            opens,
            closes,
            bar_count=count,
            train_end=train_end,
            validation_end=validation_end,
            slippage_bps_per_side=assumed_slippage,
        )
        pair_portfolio_slippage_sensitivity.append({
            "slippage_bps_per_side": assumed_slippage,
            "spot_net_portfolio": scenario["spot_net_portfolio"],
            "spot_net_chronological_splits": scenario["spot_net_chronological_splits"],
            "status": "COST_ASSUMPTION_SENSITIVITY_NOT_HISTORICAL_EXECUTION",
        })
    current_pair_signals, current_pair_eligible_count = _latest_pair_signals(
        symbols, closes, market_costs
    )
    variant_c_taker = _simulate_cross_sectional_momentum(
        market, market_costs, fee_type="taker"
    )
    variant_c_maker_reference = _simulate_cross_sectional_momentum(
        market, market_costs, fee_type="maker_reference"
    )
    variant_c_slippage_sensitivity = [
        {
            "slippage_bps_per_side": assumed_slippage,
            "costed_proxy": _simulate_cross_sectional_momentum(
                market,
                market_costs,
                slippage_bps_per_side=assumed_slippage,
                fee_type="taker",
            )["costed_proxy"],
            "status": "COST_ASSUMPTION_SENSITIVITY_NOT_HISTORICAL_EXECUTION",
        }
        for assumed_slippage in SLIPPAGE_SENSITIVITY_BPS_PER_SIDE
    ]
    variant_d_regime_divergence = run_regime_divergence_research(
        market, market_costs
    )
    if now_epoch is None:
        current_bar_freshness = "NOT_CHECKED"
    else:
        age = int(now_epoch) - (int(timestamps[-1]) + 3600)
        current_bar_freshness = "FRESH" if -60 <= age <= 3900 else "STALE_OR_FORMING"

    identity = "|".join((PORTFOLIO_RESEARCH_VERSION, str(timestamps[-1]), *symbols))
    research_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return {
        "research_key": research_key,
        "research_version": PORTFOLIO_RESEARCH_VERSION,
        "status": "RESEARCH_COMPLETE",
        "as_of_bar_ts": int(timestamps[-1]),
        "as_of_bar_freshness": current_bar_freshness,
        "symbols": symbols,
        "excluded_symbols": market.get("excluded_symbols", []),
        "synchronized_bar_count": count,
        "chronological_splits": {
            "train": {"start_index": 0, "end_index_exclusive": train_end},
            "validation": {"start_index": train_end, "end_index_exclusive": validation_end},
            "test": {"start_index": validation_end, "end_index_exclusive": count},
        },
        "variant_a_volatility_harvesting": {
            "status": "CANDIDATE_RESEARCH" if negative_pairs else "NO_NEGATIVE_CORRELATION_PAIRS_AT_AS_OF_BAR",
            "method": {
                "pair_selection": "GREEDY_DISJOINT_PAIR_MATCHING_ON_ROLLING_RETURN_CORRELATION",
                "correlation_lookback_bars": CORRELATION_LOOKBACK_BARS,
                "negative_correlation_threshold": NEGATIVE_CORRELATION_THRESHOLD,
                "threshold_sensitivity_values": list(CORRELATION_SENSITIVITY_THRESHOLDS),
                "pair_selection_interval_bars": REBALANCE_INTERVAL_BARS,
                "rebalance_weight_drift_threshold": REBALANCE_DRIFT_THRESHOLD,
                "target_weights": "EQUAL_WEIGHT_ACROSS_SELECTED_PAIR_ASSETS",
                "unselected_weight": "USD_CASH",
                "cash_borrowing_or_leverage": False,
                "future_data_used_for_selection": False,
            },
            "latest_selected_pairs": negative_pair_report,
            "latest_pair_correlation_ranking": ranked_correlations,
            "negative_correlation_threshold_sensitivity": correlation_threshold_sensitivity,
            "primary_threshold_slippage_sensitivity": variant_a_slippage_sensitivity,
            "candidates": list(strategy_a.values()),
            "candidate_names": list(strategy_a.keys()),
            "portfolio_simulation": True,
            "actual_fills": False,
            "portfolio_pnl": False,
        },
        "variant_b_relative_value_pairs": {
            "status": "CANDIDATE_RESEARCH" if current_pair_eligible_count else "NO_STATISTICALLY_ELIGIBLE_PAIRS",
            "method": {
                "regression": "ROLLING_OLS_LOG_PRICE_SPREAD",
                "regression_lookback_bars": PAIR_REGRESSION_LOOKBACK_BARS,
                "minimum_return_correlation": PAIR_MIN_CORRELATION,
                "spread_stationarity_proxy": "AR1_RESIDUAL_HALF_LIFE_ONLY_NOT_FORMAL_COINTEGRATION_TEST",
                "formal_cointegration_test": False,
                "multiple_testing_correction": False,
                "half_life_range_bars": [2.0, 72.0],
                "entry_abs_z_score": PAIR_ENTRY_Z,
                "exit_abs_z_score": PAIR_EXIT_Z,
                "adverse_spread_stop_abs_z_score": PAIR_STOP_Z,
                "maximum_holding_bars": PAIR_MAX_HOLDING_BARS,
                "hedge_ratio_frozen_at_entry": True,
                "entry_at_next_bar_open": True,
                "exit_after_close_signal_at_next_bar_open": True,
                "candidate_trades_non_overlapping_per_pair": True,
                "cross_pair_portfolio_overlap_simulated": True,
                "pair_selection_survivorship_and_multiple_testing_risk": "NOT_FULLY_CORRECTED_RESEARCH_ONLY",
                "future_data_used_for_signal": False,
            },
            "latest_pair_eligible_count": current_pair_eligible_count,
            "latest_pair_signals": current_pair_signals,
            "pair_trade_summary_by_split": pair_trade_summary,
            "pair_summaries_with_trades": pair_results,
            "pending_trades_at_sample_end": sum(item["pending_at_sample_end"] for item in pair_results),
            "costed_sample_count": sum(item.get("net_return_bps") is not None for item in all_trades),
            "spot_net_cost_quality": "CURRENT_FEE_AND_SPREAD_PROXY_PLUS_ASSUMED_SLIPPAGE" if all_trades and all(item.get("net_return_bps") is not None for item in all_trades) else "INCOMPLETE_OR_NO_COSTED_TRADES",
            "margin_financing_costs": "UNKNOWN_NOT_INCLUDED",
            "eligible_current_margin_signal_count": sum(
                bool(item["margin_execution_eligible_from_instrument_metadata"])
                for item in current_pair_signals
            ),
            "concurrent_portfolio": pair_portfolio,
            "concurrent_portfolio_slippage_sensitivity": pair_portfolio_slippage_sensitivity,
            "maker_fee_reference_portfolio": maker_fee_reference_portfolio,
            "maker_fee_reference_status": (
                "HYPOTHETICAL_LOWER_COST_REFERENCE_NOT_A_PASSIVE_FILL_MODEL"
            ),
            "portfolio_simulation": True,
            "actual_fills": False,
            "portfolio_pnl": False,
        },
        "variant_c_cross_sectional_momentum": {
            "status": variant_c_taker.get("status", "NOT_AVAILABLE"),
            "method": variant_c_taker.get("method", {}),
            "taker_fee_baseline": variant_c_taker,
            "maker_fee_reference": variant_c_maker_reference,
            "maker_fee_reference_caveat": (
                "HYPOTHETICAL_FEE_ONLY; DOES_NOT_MODEL_QUEUE_POSITION_OR_MAKER_FILL_PROBABILITY"
            ),
            "slippage_sensitivity": variant_c_slippage_sensitivity,
            "portfolio_simulation": True,
            "actual_fills": False,
            "portfolio_pnl": False,
        },
        "variant_d_regime_conditioned_residual_recoil": {
            **variant_d_regime_divergence,
            "portfolio_simulation": False,
            "actual_fills": False,
            "portfolio_pnl": False,
        },
        "cost_assumptions": {
            "taker_fee_source": "CURRENT_KRAKEN_TRADE_VOLUME_WHEN_AVAILABLE",
            "maker_fee_source": "CURRENT_KRAKEN_TRADE_VOLUME_FEES_MAKER_WHEN_AVAILABLE",
            "maker_fee_is_used_for_primary_results": False,
            "fee_schedule_requested": True,
            "spread_source": "CURRENT_TICKER_SPREAD_APPLIED_AS_HISTORICAL_PROXY",
            "slippage_bps_per_side": SLIPPAGE_BPS_PER_SIDE,
            "slippage_sensitivity_bps_per_side": list(SLIPPAGE_SENSITIVITY_BPS_PER_SIDE),
            "unknown_fees_are_not_assumed_zero": True,
            "margin_opening_and_rollover_costs_included": False,
            "historical_cost_accuracy": "NOT_VERIFIED_HISTORICAL_QUOTES_OR_FILLS",
            "current_l2_order_book_cost_diagnostics": _current_order_book_cost_diagnostics(
                market_costs
            ),
            "time_separated_l2_cost_diagnostics": _time_separated_l2_cost_diagnostics(
                depth_history
            ),
            "time_separated_l2_snapshots_persisted_by_runtime": True,
        },
        "release_gate_eligible": False,
        "actual_fills": False,
        "portfolio_pnl": False,
        "order_creation_allowed": False,
        "strategy_promotion_allowed": False,
    }
