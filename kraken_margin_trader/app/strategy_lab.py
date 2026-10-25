from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import math
from pathlib import Path
from statistics import fmean, median, pstdev
from typing import Any, Sequence

from app.strategy import generate_trend_signal

D = Decimal
SUPPORTED_INTERVAL_MINUTES = frozenset({1, 5, 15, 30, 60, 240, 720, 1440})
FEATURE_VERSION = "ohlcv-features-v1"
RESEARCH_VERSION = "strategy-lab-v0.1"
MIN_LOGISTIC_TRAIN_SAMPLES = 200
FEATURE_LOOKBACK_BARS = 72
LOGISTIC_LONG_THRESHOLD = 0.60
LOGISTIC_SHORT_THRESHOLD = 0.40
LOGISTIC_EPOCHS = 800
LOGISTIC_LEARNING_RATE = 0.04
LOGISTIC_L2 = 0.01


class HistoricalDataError(ValueError):
    """A source file contains incomplete, inconsistent, or ambiguous market data."""


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trades: int


@dataclass(frozen=True)
class ResearchExample:
    decision_index: int
    entry_index: int
    exit_index: int
    features: tuple[float, ...]
    gross_long_return_bps: Decimal


@dataclass(frozen=True)
class LogisticModel:
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float
    training_samples: int
    positive_label_rate_pct: float

    def probability_long(self, features: Sequence[float]) -> float:
        if len(features) != len(self.coefficients):
            raise ValueError("LOGISTIC_FEATURE_DIMENSION_MISMATCH")
        score = self.intercept
        for value, mean, scale, coefficient in zip(
            features, self.means, self.scales, self.coefficients, strict=True
        ):
            score += ((value - mean) / scale) * coefficient
        return _sigmoid(score)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _valid_decimal(value: str, *, line_number: int, field_name: str) -> Decimal:
    try:
        parsed = D(value.strip())
    except (ArithmeticError, ValueError) as exc:
        raise HistoricalDataError(
            f"INVALID_DECIMAL_LINE_{line_number}_{field_name.upper()}"
        ) from exc
    if not parsed.is_finite():
        raise HistoricalDataError(f"NON_FINITE_LINE_{line_number}_{field_name.upper()}")
    return parsed


def load_kraken_ohlcvt(
    source_path: str | Path, *, interval_minutes: int
) -> tuple[list[Candle], dict[str, Any]]:
    """Load Kraken's headerless seven-column OHLCVT CSV without filling missing bars.

    Identical duplicate rows are counted and ignored. Conflicting duplicates, unsorted
    rows, invalid OHLCV values, and timestamps outside the requested interval grid fail
    closed. Gaps are reported; no synthetic candles are generated.
    """
    path = Path(source_path)
    if interval_minutes not in SUPPORTED_INTERVAL_MINUTES:
        raise HistoricalDataError("UNSUPPORTED_KRAKEN_OHLCVT_INTERVAL")
    if not path.is_file():
        raise HistoricalDataError("HISTORICAL_SOURCE_FILE_NOT_FOUND")

    interval_seconds = interval_minutes * 60
    candles: list[Candle] = []
    duplicate_count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        for line_number, row in enumerate(reader, start=1):
            if not row or all(not value.strip() for value in row):
                continue
            if len(row) != 7:
                raise HistoricalDataError(f"EXPECTED_SEVEN_COLUMNS_LINE_{line_number}")
            try:
                timestamp = int(row[0].strip())
                trades = int(row[6].strip())
            except ValueError as exc:
                raise HistoricalDataError(f"INVALID_INTEGER_LINE_{line_number}") from exc
            if timestamp < 0 or timestamp % interval_seconds != 0:
                raise HistoricalDataError(f"TIMESTAMP_OFF_INTERVAL_GRID_LINE_{line_number}")
            if trades < 0:
                raise HistoricalDataError(f"NEGATIVE_TRADE_COUNT_LINE_{line_number}")

            candle = Candle(
                timestamp=timestamp,
                open=_valid_decimal(row[1], line_number=line_number, field_name="open"),
                high=_valid_decimal(row[2], line_number=line_number, field_name="high"),
                low=_valid_decimal(row[3], line_number=line_number, field_name="low"),
                close=_valid_decimal(row[4], line_number=line_number, field_name="close"),
                volume=_valid_decimal(row[5], line_number=line_number, field_name="volume"),
                trades=trades,
            )
            prices = (candle.open, candle.high, candle.low, candle.close)
            if (
                any(price <= 0 for price in prices)
                or candle.volume < 0
                or candle.high < max(candle.open, candle.close, candle.low)
                or candle.low > min(candle.open, candle.close, candle.high)
            ):
                raise HistoricalDataError(f"INVALID_OHLCV_VALUES_LINE_{line_number}")

            if candles and candle.timestamp == candles[-1].timestamp:
                if candle != candles[-1]:
                    raise HistoricalDataError(f"CONFLICTING_DUPLICATE_TIMESTAMP_LINE_{line_number}")
                duplicate_count += 1
                continue
            if candles and candle.timestamp < candles[-1].timestamp:
                raise HistoricalDataError(f"SOURCE_NOT_CHRONOLOGICAL_LINE_{line_number}")
            candles.append(candle)

    if not candles:
        raise HistoricalDataError("HISTORICAL_SOURCE_HAS_NO_CANDLES")

    gap_count = 0
    missing_interval_count = 0
    max_gap_seconds = 0
    for previous, current in zip(candles, candles[1:]):
        delta = current.timestamp - previous.timestamp
        if delta > interval_seconds:
            gap_count += 1
            missing_interval_count += delta // interval_seconds - 1
            max_gap_seconds = max(max_gap_seconds, delta)

    metadata = {
        "source_path": str(path.resolve()),
        "source_sha256": _sha256_file(path),
        "format": "KRAKEN_HEADERLESS_OHLCVT_CSV_7_COLUMNS",
        "interval_minutes": interval_minutes,
        "candle_count": len(candles),
        "identical_duplicate_rows_ignored": duplicate_count,
        "gap_count": gap_count,
        "missing_intervals_not_filled": missing_interval_count,
        "max_observed_gap_seconds": max_gap_seconds,
        "first_timestamp": candles[0].timestamp,
        "first_timestamp_utc": _iso_timestamp(candles[0].timestamp),
        "last_timestamp": candles[-1].timestamp,
        "last_timestamp_utc": _iso_timestamp(candles[-1].timestamp),
    }
    return candles, metadata


def _path_efficiency(prices: Sequence[float]) -> float:
    if len(prices) < 2:
        return 0.0
    path = sum(
        abs(prices[index] / prices[index - 1] - 1.0)
        for index in range(1, len(prices))
    )
    if not math.isfinite(path) or path <= 0:
        return 0.0
    return min(1.0, abs(prices[-1] / prices[0] - 1.0) / path)


def _feature_vector(
    candles: Sequence[Candle], index: int, interval_seconds: int
) -> tuple[float, ...] | None:
    start = index - FEATURE_LOOKBACK_BARS
    if start < 0:
        return None
    if any(
        candles[position].timestamp - candles[position - 1].timestamp != interval_seconds
        for position in range(start + 1, index + 1)
    ):
        return None

    prices = [float(candle.close) for candle in candles[start : index + 1]]
    if any(not math.isfinite(price) or price <= 0 for price in prices):
        return None
    returns = [
        prices[position] / prices[position - 1] - 1.0
        for position in range(1, len(prices))
    ]
    if not returns or any(not math.isfinite(value) for value in returns):
        return None

    ret_1_bps = returns[-1] * 10000.0
    ret_6_bps = (prices[-1] / prices[-7] - 1.0) * 10000.0
    ret_24_bps = (prices[-1] / prices[-25] - 1.0) * 10000.0
    ret_72_bps = (prices[-1] / prices[0] - 1.0) * 10000.0
    vol_24_bps = pstdev(returns[-24:]) * 10000.0
    vol_72_bps = pstdev(returns[-72:]) * 10000.0
    efficiency_24 = _path_efficiency(prices[-25:])
    efficiency_72 = _path_efficiency(prices)
    features = (
        ret_1_bps,
        ret_6_bps,
        ret_24_bps,
        ret_72_bps,
        vol_24_bps,
        vol_72_bps,
        efficiency_24,
        efficiency_72,
    )
    return features if all(math.isfinite(value) for value in features) else None


def _build_examples(
    candles: Sequence[Candle], *, interval_minutes: int, holding_bars: int
) -> list[ResearchExample]:
    interval_seconds = interval_minutes * 60
    examples: list[ResearchExample] = []
    for index in range(FEATURE_LOOKBACK_BARS, len(candles) - holding_bars - 1):
        features = _feature_vector(candles, index, interval_seconds)
        if features is None:
            continue
        entry_index = index + 1
        exit_index = entry_index + holding_bars
        if any(
            candles[position].timestamp - candles[position - 1].timestamp
            != interval_seconds
            for position in range(index + 1, exit_index + 1)
        ):
            continue
        entry_price = candles[entry_index].open
        exit_price = candles[exit_index].open
        gross_return_bps = (exit_price / entry_price - D(1)) * D(10000)
        if not gross_return_bps.is_finite():
            continue
        examples.append(
            ResearchExample(
                decision_index=index,
                entry_index=entry_index,
                exit_index=exit_index,
                features=features,
                gross_long_return_bps=gross_return_bps,
            )
        )
    return examples


def _sigmoid(value: float) -> float:
    bounded = max(-40.0, min(40.0, value))
    return 1.0 / (1.0 + math.exp(-bounded))


def _fit_logistic_model(
    training_examples: Sequence[ResearchExample],
) -> tuple[LogisticModel | None, str]:
    if len(training_examples) < MIN_LOGISTIC_TRAIN_SAMPLES:
        return None, "INSUFFICIENT_TRAINING_SAMPLES"
    labels = [1.0 if item.gross_long_return_bps > 0 else 0.0 for item in training_examples]
    if len(set(labels)) < 2:
        return None, "TRAINING_LABEL_HAS_ONE_CLASS"

    feature_count = len(training_examples[0].features)
    columns = [
        [item.features[feature_index] for item in training_examples]
        for feature_index in range(feature_count)
    ]
    means = [fmean(column) for column in columns]
    scales = [pstdev(column) for column in columns]
    scales = [scale if math.isfinite(scale) and scale > 1e-12 else 1.0 for scale in scales]
    standardized = [
        [(value - mean) / scale for value, mean, scale in zip(
            item.features, means, scales, strict=True
        )]
        for item in training_examples
    ]
    weights = [0.0] * feature_count
    intercept = 0.0
    sample_count = float(len(training_examples))

    for _ in range(LOGISTIC_EPOCHS):
        weight_gradients = [0.0] * feature_count
        intercept_gradient = 0.0
        for row, label in zip(standardized, labels, strict=True):
            probability = _sigmoid(
                intercept + sum(weight * value for weight, value in zip(
                    weights, row, strict=True
                ))
            )
            error = probability - label
            intercept_gradient += error
            for feature_index, value in enumerate(row):
                weight_gradients[feature_index] += error * value
        intercept -= LOGISTIC_LEARNING_RATE * intercept_gradient / sample_count
        for feature_index in range(feature_count):
            gradient = (
                weight_gradients[feature_index] / sample_count
                + LOGISTIC_L2 * weights[feature_index]
            )
            weights[feature_index] -= LOGISTIC_LEARNING_RATE * gradient

    model = LogisticModel(
        means=tuple(means),
        scales=tuple(scales),
        coefficients=tuple(weights),
        intercept=intercept,
        training_samples=len(training_examples),
        positive_label_rate_pct=sum(labels) / len(labels) * 100.0,
    )
    return model, "FITTED_ON_TRAIN_SPLIT_ONLY"


def _mean_reversion_direction(candles: Sequence[Candle], index: int) -> str | None:
    window = [float(item.close) for item in candles[index - 23 : index + 1]]
    if len(window) != 24:
        return None
    deviation = pstdev(window)
    if not math.isfinite(deviation) or deviation <= 0:
        return None
    z_score = (window[-1] - fmean(window)) / deviation
    if z_score >= 2.0:
        return "SHORT"
    if z_score <= -2.0:
        return "LONG"
    return None


def _trend_direction(
    candles: Sequence[Candle],
    index: int,
    *,
    fast_threshold_bps: Decimal,
    slow_threshold_bps: Decimal,
) -> str | None:
    history = [item.close for item in candles[index - 72 : index + 1]]
    signal = generate_trend_signal(
        "RESEARCH",
        history,
        fast_threshold_bps=fast_threshold_bps,
        slow_threshold_bps=slow_threshold_bps,
    )
    return signal.direction if signal.has_direction else None


def _split_examples(
    examples: Sequence[ResearchExample], *, train_end: int, validation_end: int
) -> dict[str, list[ResearchExample]]:
    result: dict[str, list[ResearchExample]] = {"train": [], "validation": [], "test": []}
    for item in examples:
        index = item.decision_index
        if index < train_end and item.exit_index < train_end:
            result["train"].append(item)
        elif train_end <= index < validation_end and item.exit_index < validation_end:
            result["validation"].append(item)
        elif index >= validation_end:
            result["test"].append(item)
    return result


def _summarize_returns(values: Sequence[Decimal]) -> dict[str, Any]:
    if not values:
        return {
            "sample_count": 0,
            "mean_bps": None,
            "median_bps": None,
            "win_rate_pct": None,
            "profit_factor": None,
            "profit_factor_unbounded": False,
        }
    wins = sum((value for value in values if value > 0), D(0))
    losses = -sum((value for value in values if value < 0), D(0))
    if losses > 0:
        profit_factor: float | None = float(wins / losses)
        unbounded = False
    else:
        profit_factor = None
        unbounded = wins > 0
    return {
        "sample_count": len(values),
        "mean_bps": round(float(sum(values, D(0)) / D(len(values))), 6),
        "median_bps": round(float(median(values)), 6),
        "win_rate_pct": round(
            sum(1 for value in values if value > 0) / len(values) * 100.0, 6
        ),
        "profit_factor": round(profit_factor, 6) if profit_factor is not None else None,
        "profit_factor_unbounded": unbounded,
    }


def _candidate_metrics(
    examples: Sequence[ResearchExample],
    signals: dict[int, str],
    *,
    spot_round_trip_cost_bps: Decimal,
    margin_cost_budgets_bps: Sequence[Decimal],
) -> dict[str, Any]:
    by_index = {item.decision_index: item for item in examples}
    ordered_signals = [
        (by_index[index], direction)
        for index, direction in sorted(signals.items())
        if index in by_index and direction in {"LONG", "SHORT"}
    ]
    selected: list[tuple[ResearchExample, str]] = []
    overlap_excluded = 0
    last_exit_index = -1
    for example, direction in ordered_signals:
        if example.entry_index < last_exit_index:
            overlap_excluded += 1
            continue
        selected.append((example, direction))
        last_exit_index = example.exit_index

    gross_returns: list[Decimal] = []
    spot_net_returns: list[Decimal] = []
    for example, direction in selected:
        sign = D(1) if direction == "LONG" else D(-1)
        gross = example.gross_long_return_bps * sign
        gross_returns.append(gross)
        spot_net_returns.append(gross - spot_round_trip_cost_bps)

    margin_scenarios: dict[str, Any] = {}
    for budget in margin_cost_budgets_bps:
        net_returns = [value - budget for value in spot_net_returns]
        summary = _summarize_returns(net_returns)
        summary["margin_cost_budget_bps_round_trip"] = str(budget)
        summary["budget_is_exchange_quote"] = False
        margin_scenarios[str(budget)] = summary

    return {
        "evaluation_example_count": len(examples),
        "candidate_signal_count": len(ordered_signals),
        "non_overlapping_trade_count": len(selected),
        "overlapping_signals_excluded": overlap_excluded,
        "long_trade_count": sum(1 for _, direction in selected if direction == "LONG"),
        "short_trade_count": sum(1 for _, direction in selected if direction == "SHORT"),
        "gross": _summarize_returns(gross_returns),
        "spot_net_after_assumed_costs": _summarize_returns(spot_net_returns),
        "margin_net_sensitivity": margin_scenarios,
    }


def _benchmark(
    candles: Sequence[Candle],
    *,
    start_index: int,
    end_index: int,
    spot_round_trip_cost_bps: Decimal,
    margin_cost_budgets_bps: Sequence[Decimal],
) -> dict[str, Any]:
    if end_index <= start_index or start_index >= len(candles):
        return {"status": "NO_BARS_IN_SPLIT"}
    end = min(end_index, len(candles))
    first_open = candles[start_index].open
    last_close = candles[end - 1].close
    gross = (last_close / first_open - D(1)) * D(10000)
    spot_net = gross - spot_round_trip_cost_bps
    return {
        "status": "BENCHMARK_ONLY_NOT_PORTFOLIO_PNL",
        "start_timestamp": candles[start_index].timestamp,
        "end_timestamp": candles[end - 1].timestamp,
        "gross_return_bps": round(float(gross), 6),
        "spot_net_return_after_one_round_trip_cost_bps": round(float(spot_net), 6),
        "cash_return_bps": 0.0,
        "margin_sensitivity_not_modeled": [
            {
                "margin_cost_budget_bps_round_trip": str(budget),
                "not_simulated": True,
            }
            for budget in margin_cost_budgets_bps
        ],
    }


def compare_strategies(
    candles: Sequence[Candle],
    *,
    pair: str,
    interval_minutes: int,
    taker_fee_bps_per_side: Decimal,
    spread_bps_round_trip: Decimal,
    slippage_bps_per_side: Decimal,
    margin_cost_budgets_bps: Sequence[Decimal],
    holding_bars: int = 8,
    fast_threshold_bps: Decimal = D("120"),
    slow_threshold_bps: Decimal = D("180"),
    source_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare fixed research candidates without touching live trading or persistence.

    Entries occur at the next bar open after a decision at the current bar close;
    exits occur at the open exactly holding_bars later. Features and target windows
    crossing a data gap are omitted rather than filled with synthetic candles.
    """
    if interval_minutes != 60:
        raise ValueError("STRATEGY_COMPARISON_REQUIRES_HOURLY_BARS")
    if not pair.strip():
        raise ValueError("RESEARCH_PAIR_REQUIRED")
    if holding_bars < 1:
        raise ValueError("HOLDING_BARS_MUST_BE_POSITIVE")
    numeric_inputs = (
        taker_fee_bps_per_side,
        spread_bps_round_trip,
        slippage_bps_per_side,
        fast_threshold_bps,
        slow_threshold_bps,
        *margin_cost_budgets_bps,
    )
    if any(not value.is_finite() or value < 0 for value in numeric_inputs):
        raise ValueError("RESEARCH_COSTS_AND_THRESHOLDS_MUST_BE_FINITE_NONNEGATIVE")
    if fast_threshold_bps <= 0 or slow_threshold_bps <= 0:
        raise ValueError("RESEARCH_TREND_THRESHOLDS_MUST_BE_POSITIVE")
    if not margin_cost_budgets_bps:
        raise ValueError("AT_LEAST_ONE_MARGIN_COST_BUDGET_REQUIRED")
    if len(candles) < 200:
        raise ValueError("INSUFFICIENT_CANDLES_FOR_CHRONOLOGICAL_COMPARISON")
    if any(
        candles[index].timestamp >= candles[index + 1].timestamp
        for index in range(len(candles) - 1)
    ):
        raise HistoricalDataError("CANDLES_MUST_BE_STRICTLY_CHRONOLOGICAL")

    train_end = int(len(candles) * 0.60)
    validation_end = int(len(candles) * 0.80)
    if train_end < FEATURE_LOOKBACK_BARS + holding_bars + 2 or validation_end <= train_end:
        raise ValueError("INSUFFICIENT_CHRONOLOGICAL_SPLIT_SIZE")

    all_examples = _build_examples(
        candles, interval_minutes=interval_minutes, holding_bars=holding_bars
    )
    splits = _split_examples(all_examples, train_end=train_end, validation_end=validation_end)
    model, model_status = _fit_logistic_model(splits["train"])
    margin_budgets = list(margin_cost_budgets_bps)
    spot_round_trip_cost = (
        D(2) * taker_fee_bps_per_side
        + spread_bps_round_trip
        + D(2) * slippage_bps_per_side
    )
    candidate_results: list[dict[str, Any]] = []
    names = ("current_tsm", "mean_reversion_24h", "logistic_regression_baseline")
    for name in names:
        split_results: dict[str, Any] = {}
        for split_name, split_items in splits.items():
            signals: dict[int, str] = {}
            for item in split_items:
                index = item.decision_index
                direction: str | None
                if name == "current_tsm":
                    direction = _trend_direction(
                        candles,
                        index,
                        fast_threshold_bps=fast_threshold_bps,
                        slow_threshold_bps=slow_threshold_bps,
                    )
                elif name == "mean_reversion_24h":
                    direction = _mean_reversion_direction(candles, index)
                elif model is not None:
                    probability = model.probability_long(item.features)
                    if probability >= LOGISTIC_LONG_THRESHOLD:
                        direction = "LONG"
                    elif probability <= LOGISTIC_SHORT_THRESHOLD:
                        direction = "SHORT"
                    else:
                        direction = None
                else:
                    direction = None
                if direction is not None:
                    signals[index] = direction
            metrics = _candidate_metrics(
                split_items,
                signals,
                spot_round_trip_cost_bps=spot_round_trip_cost,
                margin_cost_budgets_bps=margin_budgets,
            )
            metrics["evaluation_scope"] = (
                "IN_SAMPLE_TRAIN" if split_name == "train" else "CHRONOLOGICAL_HELD_OUT"
            )
            split_results[split_name] = metrics
        candidate_results.append(
            {
                "strategy": name,
                "parameters": (
                    {
                        "fast_threshold_bps": str(fast_threshold_bps),
                        "slow_threshold_bps": str(slow_threshold_bps),
                        "implementation_reference": "app.strategy.generate_trend_signal",
                    }
                    if name == "current_tsm"
                    else (
                        {"window_bars": 24, "z_score_threshold": 2.0}
                        if name == "mean_reversion_24h"
                        else {
                            "model": "deterministic_l2_logistic_regression",
                            "features": [
                                "return_1_bar_bps",
                                "return_6_bars_bps",
                                "return_24_bars_bps",
                                "return_72_bars_bps",
                                "realized_volatility_24_bars_bps",
                                "realized_volatility_72_bars_bps",
                                "path_efficiency_24_bars",
                                "path_efficiency_72_bars",
                            ],
                            "long_probability_threshold": LOGISTIC_LONG_THRESHOLD,
                            "short_probability_threshold": LOGISTIC_SHORT_THRESHOLD,
                            "fit_policy": "TRAIN_SPLIT_ONLY_NO_VALIDATION_OR_TEST_UPDATES",
                            "training_status": model_status,
                        }
                    )
                ),
                "splits": split_results,
            }
        )

    interval_seconds = interval_minutes * 60
    gap_count = sum(
        1
        for left, right in zip(candles, candles[1:])
        if right.timestamp - left.timestamp > interval_seconds
    )
    potential_examples = max(
        0, len(candles) - holding_bars - 1 - FEATURE_LOOKBACK_BARS
    )
    gap_crossing_samples_excluded = potential_examples - len(all_examples)
    model_report: dict[str, Any] = {
        "status": model_status,
        "trained_only_on_chronological_train_split": True,
        "minimum_training_samples": MIN_LOGISTIC_TRAIN_SAMPLES,
        "training_samples": model.training_samples if model else len(splits["train"]),
        "positive_training_label_rate_pct": (
            round(model.positive_label_rate_pct, 6) if model else None
        ),
        "coefficients": (
            [round(value, 10) for value in model.coefficients] if model else None
        ),
        "intercept": round(model.intercept, 10) if model else None,
        "standardization_means": (
            [round(value, 10) for value in model.means] if model else None
        ),
        "standardization_scales": (
            [round(value, 10) for value in model.scales] if model else None
        ),
    }
    boundaries = {
        "train": (0, train_end),
        "validation": (train_end, validation_end),
        "test": (validation_end, len(candles)),
    }
    benchmarks = {
        name: _benchmark(
            candles,
            start_index=start,
            end_index=end,
            spot_round_trip_cost_bps=spot_round_trip_cost,
            margin_cost_budgets_bps=margin_budgets,
        )
        for name, (start, end) in boundaries.items()
    }
    return {
        "research_version": RESEARCH_VERSION,
        "status": "RESEARCH_ONLY_NO_EXECUTION_SIMULATION",
        "release_gate_eligible": False,
        "portfolio_pnl": False,
        "actual_fills": False,
        "parameter_promotion": False,
        "pair": pair,
        "feature_version": FEATURE_VERSION,
        "data": {
            **(source_metadata or {}),
            "candle_count": len(candles),
            "comparison_interval_minutes": interval_minutes,
            "observed_gap_count": gap_count,
            "feature_or_horizon_samples_excluded_for_gaps_or_bounds": gap_crossing_samples_excluded,
            "features_or_targets_crossing_gaps_are_fabricated": False,
        },
        "method": {
            "decision_timing": "AFTER_CURRENT_CANDLE_CLOSE",
            "entry_timing": "OPEN_OF_NEXT_CANDLE",
            "exit_timing": "OPEN_AFTER_FIXED_HOLDING_BARS",
            "holding_bars": holding_bars,
            "holding_horizon_minutes": interval_minutes * holding_bars,
            "split_ratio": {"train": 0.60, "validation": 0.20, "test": 0.20},
            "eligible_examples_by_split": {
                name: len(items) for name, items in splits.items()
            },
            "target_horizon_purged_at_split_boundaries": True,
            "candidate_trade_horizons_are_non_overlapping_per_strategy": True,
            "cross_pair_correlation_modeled": False,
            "features_use_future_data": False,
            "training_labels_use_future_data_only_inside_train_split": True,
            "test_data_used_for_model_fitting_or_threshold_selection": False,
            "trade_execution_simulated": False,
            "portfolio_sizing_leverage_liquidation_and_orderbook_simulated": False,
        },
        "cost_assumptions": {
            "taker_fee_bps_per_side": str(taker_fee_bps_per_side),
            "spread_bps_round_trip": str(spread_bps_round_trip),
            "slippage_bps_per_side": str(slippage_bps_per_side),
            "spot_round_trip_cost_bps": str(spot_round_trip_cost),
            "margin_cost_budgets_bps_round_trip": [str(value) for value in margin_budgets],
            "margin_cost_budgets_are_exchange_quotes": False,
            "margin_cost_bases_are_not_verified_by_this_report": True,
            "historical_fee_and_spread_accuracy": "USER_SUPPLIED_ASSUMPTIONS_NOT_HISTORICAL_QUOTES",
        },
        "chronological_boundaries": {
            name: {
                "start_index_inclusive": start,
                "end_index_exclusive": end,
                "start_timestamp": candles[start].timestamp,
                "end_timestamp": candles[end - 1].timestamp,
            }
            for name, (start, end) in boundaries.items()
        },
        "machine_learning_model": model_report,
        "benchmarks": benchmarks,
        "candidates": candidate_results,
    }


LIVE_SHADOW_CANDIDATE_VERSIONS = {
    "current_tsm": "tsm-0.2.0",
    "mean_reversion_24h": "mean-reversion-24h-0.1.0",
    "logistic_regression_wf": "logistic-regression-wf-0.1.0",
}


def generate_live_shadow_candidate_signals(
    raw_bars: Sequence[dict[str, Any]],
    *,
    fast_threshold_bps: Decimal,
    slow_threshold_bps: Decimal,
    holding_bars: int = 8,
    tsm_version: str = "tsm-0.2.0",
) -> list[dict[str, Any]]:
    """Produce research-only signals from candles available at the current decision time.

    The logistic model is re-fit only on labelled examples whose complete outcome
    bar is older than the current decision bar. It is an online walk-forward
    prediction, not an order recommendation. The caller must persist these records
    in the isolated strategy-candidate table and must never feed them to execution.
    """
    from app.strategy import generate_trend_signal

    if holding_bars < 1:
        raise ValueError("LIVE_SHADOW_HOLDING_BARS_MUST_BE_POSITIVE")
    if len(raw_bars) < FEATURE_LOOKBACK_BARS + 2:
        return [
            {
                "candidate_name": name,
                "candidate_version": version if name != "current_tsm" else tsm_version,
                "direction": "NONE",
                "signal_reason": "INSUFFICIENT_HISTORY",
                "score": D(0),
                "features": {"minimum_candles": FEATURE_LOOKBACK_BARS + 2},
            }
            for name, version in LIVE_SHADOW_CANDIDATE_VERSIONS.items()
        ]

    candles: list[Candle] = []
    for index, row in enumerate(raw_bars):
        try:
            candle = Candle(
                timestamp=int(row["ts"]),
                open=D(str(row["open"])),
                high=D(str(row["high"])),
                low=D(str(row["low"])),
                close=D(str(row["close"])),
                volume=D(str(row.get("volume", "0"))),
                trades=int(row.get("trades", 0)),
            )
        except (ArithmeticError, KeyError, TypeError, ValueError, OverflowError) as exc:
            raise HistoricalDataError(f"LIVE_CANDIDATE_CANDLE_INVALID_AT_INDEX_{index}") from exc
        prices = (candle.open, candle.high, candle.low, candle.close)
        if (
            any(not value.is_finite() or value <= 0 for value in prices)
            or not candle.volume.is_finite()
            or candle.volume < 0
            or candle.high < max(candle.open, candle.close, candle.low)
            or candle.low > min(candle.open, candle.close, candle.high)
        ):
            raise HistoricalDataError(f"LIVE_CANDIDATE_OHLC_INVALID_AT_INDEX_{index}")
        if candles and candle.timestamp <= candles[-1].timestamp:
            raise HistoricalDataError("LIVE_CANDIDATE_BARS_NOT_STRICTLY_CHRONOLOGICAL")
        candles.append(candle)

    current_index = len(candles) - 1
    closes = [item.close for item in candles]
    tsm = generate_trend_signal(
        "SHADOW_RESEARCH",
        closes,
        fast_threshold_bps=fast_threshold_bps,
        slow_threshold_bps=slow_threshold_bps,
    )
    tsm_candidate = {
        "candidate_name": "current_tsm",
        "candidate_version": tsm_version,
        "direction": tsm.direction,
        "signal_reason": tsm.reason,
        "score": tsm.score,
        "features": {
            "momentum_fast_bps": str(tsm.momentum_fast_bps),
            "momentum_slow_bps": str(tsm.momentum_slow_bps),
            "trend_efficiency_24h": str(tsm.trend_efficiency_24h),
            "trend_efficiency_72h": str(tsm.trend_efficiency_72h),
            "stop_distance_pct": str(tsm.stop_distance_pct),
            "score_is_calibrated_probability": False,
        },
    }

    mean_reversion: dict[str, Any] = {
        "candidate_name": "mean_reversion_24h",
        "candidate_version": LIVE_SHADOW_CANDIDATE_VERSIONS["mean_reversion_24h"],
        "direction": "NONE",
        "signal_reason": "MEAN_REVERSION_WITHIN_FIXED_THRESHOLD",
        "score": D(0),
        "features": {"window_bars": 24, "z_score_threshold": 2.0},
    }
    if len(closes) >= 24:
        window = [float(value) for value in closes[-24:]]
        average = fmean(window)
        deviation = pstdev(window)
        if math.isfinite(deviation) and deviation > 0:
            z_score = (window[-1] - average) / deviation
            if math.isfinite(z_score):
                direction = (
                    "SHORT" if z_score >= 2.0 else
                    "LONG" if z_score <= -2.0 else "NONE"
                )
                reason = (
                    "MEAN_REVERSION_ZSCORE_HIGH" if direction == "SHORT" else
                    "MEAN_REVERSION_ZSCORE_LOW" if direction == "LONG" else
                    "MEAN_REVERSION_WITHIN_FIXED_THRESHOLD"
                )
                mean_reversion.update({
                    "direction": direction,
                    "signal_reason": reason,
                    "score": D(str(abs(z_score))),
                    "features": {
                        "window_bars": 24,
                        "z_score_threshold": 2.0,
                        "z_score": round(z_score, 8),
                        "moving_average": str(D(str(average))),
                        "population_stddev": str(D(str(deviation))),
                    },
                })
        else:
            mean_reversion["signal_reason"] = "MEAN_REVERSION_ZERO_OR_INVALID_VOLATILITY"

    logistic: dict[str, Any] = {
        "candidate_name": "logistic_regression_wf",
        "candidate_version": LIVE_SHADOW_CANDIDATE_VERSIONS["logistic_regression_wf"],
        "direction": "NONE",
        "signal_reason": "ML_MODEL_NOT_READY",
        "score": D(0),
        "features": {
            "model": "DETERMINISTIC_L2_LOGISTIC_REGRESSION",
            "long_probability_threshold": LOGISTIC_LONG_THRESHOLD,
            "short_probability_threshold": LOGISTIC_SHORT_THRESHOLD,
        },
    }
    features = _feature_vector(candles, current_index, 3600)
    if features is None:
        logistic["signal_reason"] = "ML_CURRENT_FEATURE_WINDOW_HAS_GAP_OR_INVALID_VALUE"
    else:
        historical_examples = _build_examples(
            candles, interval_minutes=60, holding_bars=holding_bars
        )
        # No training label may use an outcome bar at or after the current
        # decision bar. This is the crucial live walk-forward cutoff.
        mature_examples = [
            item for item in historical_examples if item.exit_index < current_index
        ]
        model, model_status = _fit_logistic_model(mature_examples)
        logistic_features = {
            "model": "DETERMINISTIC_L2_LOGISTIC_REGRESSION",
            "training_status": (
                "FITTED_ON_PRIOR_MATURED_LABELS" if model is not None else model_status
            ),
            "training_sample_count": len(mature_examples),
            "training_labels_may_overlap": True,
            "long_probability_threshold": LOGISTIC_LONG_THRESHOLD,
            "short_probability_threshold": LOGISTIC_SHORT_THRESHOLD,
            "feature_names": [
                "return_1_bar_bps",
                "return_6_bars_bps",
                "return_24_bars_bps",
                "return_72_bars_bps",
                "realized_volatility_24_bars_bps",
                "realized_volatility_72_bars_bps",
                "path_efficiency_24_bars",
                "path_efficiency_72_bars",
            ],
        }
        if model is None:
            logistic["signal_reason"] = "ML_" + model_status
            logistic["features"] = logistic_features
        else:
            probability = model.probability_long(features)
            direction = (
                "LONG" if probability >= LOGISTIC_LONG_THRESHOLD else
                "SHORT" if probability <= LOGISTIC_SHORT_THRESHOLD else "NONE"
            )
            logistic["direction"] = direction
            logistic["signal_reason"] = (
                "ML_LONG_PROBABILITY_THRESHOLD" if direction == "LONG" else
                "ML_SHORT_PROBABILITY_THRESHOLD" if direction == "SHORT" else
                "ML_PROBABILITY_IN_NO_TRADE_BAND"
            )
            logistic["score"] = D(str(round(probability, 10)))
            logistic_features.update({
                "probability_long": round(probability, 10),
                "probability_is_calibrated": False,
                "training_sample_count": model.training_samples,
                "positive_training_label_rate_pct": round(model.positive_label_rate_pct, 6),
            })
            logistic["features"] = logistic_features

    return [tsm_candidate, mean_reversion, logistic]


def strategy_candidate_observation_id(
    *,
    pair: str,
    interval_minutes: int,
    bar_ts: int,
    candidate_name: str,
    candidate_version: str,
    horizon_seconds: int,
) -> str:
    identity = "|".join((
        pair,
        str(interval_minutes),
        str(bar_ts),
        candidate_name,
        candidate_version,
        str(horizon_seconds),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
