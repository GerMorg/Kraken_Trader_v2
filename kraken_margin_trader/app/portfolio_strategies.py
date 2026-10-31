from __future__ import annotations

from itertools import combinations
import hashlib
import math
from statistics import fmean, median, pstdev
from typing import Any, Mapping, Sequence

D_MIN = 1e-12
PORTFOLIO_RESEARCH_VERSION = "parrondo-multi-asset-v0.1"
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


def _side_cost_bps(symbol: str, market_costs: Mapping[str, Mapping[str, Any]]) -> float | None:
    raw = market_costs.get(symbol, {})
    fee = _finite_float(raw.get("taker_fee_bps"))
    spread = _finite_float(raw.get("spread_bps"))
    if fee is None or spread is None or fee < 0 or spread < 0:
        return None
    return fee + spread / 2.0 + SLIPPAGE_BPS_PER_SIDE


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
) -> dict[str, Any]:
    symbols = market["symbols"]
    closes = market["closes"]
    count = len(market["timestamps"])
    costs = {symbol: _side_cost_bps(symbol, market_costs) for symbol in symbols}
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
            pairs = _select_negative_pairs(closes, index=index, lookback=lookback)
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
                trade_cost_fraction = sum(
                    abs(deltas[symbol]) * float(costs[symbol]) / 10000.0
                    for symbol in symbols
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
    beta_notional = beta * entry_b / entry_a
    weight_a = 1.0 / (1.0 + beta_notional)
    weight_b = 1.0 - weight_a
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
) -> dict[str, Any]:
    count = len(closes[symbol_a])
    trades: list[dict[str, Any]] = []
    pending = 0
    index = PAIR_REGRESSION_LOOKBACK_BARS - 1
    side_cost_a = _side_cost_bps(symbol_a, market_costs)
    side_cost_b = _side_cost_bps(symbol_b, market_costs)
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
        scheduled_max_exit = entry_index + PAIR_MAX_HOLDING_BARS - 1
        if scheduled_max_exit >= count:
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
                exit_index = future_index
                exit_reason = (
                    "SPREAD_ZSCORE_STOP" if stop_triggered else
                    "MEAN_REVERSION_EXIT" if mean_reverted else
                    "MAX_HOLDING_PERIOD_EXIT"
                )
                exit_a = closes[symbol_a][future_index]
                exit_b = closes[symbol_b][future_index]
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
        cost_bps: float | None = None
        net_bps: float | None = None
        if side_cost_a is not None and side_cost_b is not None:
            cost_bps = 2.0 * (weight_a * side_cost_a + weight_b * side_cost_b)
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
            "exit_index": exit_index,
            "holding_bars": exit_index - entry_index + 1,
            "exit_reason": exit_reason,
            "entry_z_score": model["z_score"],
            "return_correlation": model["return_correlation"],
            "half_life_bars": model["half_life_bars"],
            "hedge_ratio_beta": model["beta"],
            "gross_return_bps": gross_bps,
            "spot_round_trip_cost_bps": cost_bps,
            "net_return_bps": net_bps,
            "split": split,
        })
        index = exit_index + 1
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


def run_multi_asset_strategy_research(
    histories: Mapping[str, Sequence[Mapping[str, Any]]],
    market_costs: Mapping[str, Mapping[str, Any]],
    *,
    now_epoch: int | None = None,
) -> dict[str, Any]:
    """Run independent historical and current-shadow research for variants A and B.

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

    pair_results: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    for symbol_a, symbol_b in combinations(symbols, 2):
        pair_result = _backtest_pair(
            symbol_a,
            symbol_b,
            opens,
            closes,
            market_costs,
            train_end=train_end,
            validation_end=validation_end,
        )
        if pair_result["trades"] or pair_result["pending_at_sample_end"]:
            all_trades.extend(pair_result["trades"])
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
    current_pair_signals, current_pair_eligible_count = _latest_pair_signals(
        symbols, closes, market_costs
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
                "pair_selection_interval_bars": REBALANCE_INTERVAL_BARS,
                "rebalance_weight_drift_threshold": REBALANCE_DRIFT_THRESHOLD,
                "target_weights": "EQUAL_WEIGHT_ACROSS_SELECTED_PAIR_ASSETS",
                "unselected_weight": "USD_CASH",
                "cash_borrowing_or_leverage": False,
                "future_data_used_for_selection": False,
            },
            "latest_selected_pairs": negative_pair_report,
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
                "entry_at_next_bar_open_exit_at_bar_close": True,
                "candidate_trades_non_overlapping_per_pair": True,
                "cross_pair_portfolio_overlap_simulated": False,
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
            "portfolio_simulation": False,
            "actual_fills": False,
            "portfolio_pnl": False,
        },
        "cost_assumptions": {
            "taker_fee_source": "CURRENT_KRAKEN_TRADE_VOLUME_WHEN_AVAILABLE",
            "spread_source": "CURRENT_TICKER_SPREAD_APPLIED_AS_HISTORICAL_PROXY",
            "slippage_bps_per_side": SLIPPAGE_BPS_PER_SIDE,
            "unknown_fees_are_not_assumed_zero": True,
            "margin_opening_and_rollover_costs_included": False,
            "historical_cost_accuracy": "NOT_VERIFIED_HISTORICAL_QUOTES_OR_FILLS",
        },
        "release_gate_eligible": False,
        "actual_fills": False,
        "portfolio_pnl": False,
        "order_creation_allowed": False,
        "strategy_promotion_allowed": False,
    }
