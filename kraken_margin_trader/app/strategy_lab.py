from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
from statistics import median
from typing import Any, Sequence

from app.fourier import FOURIER_REQUIRED_PRICES, analyze_fourier_cycle
from app.strategy import generate_trend_signal

D = Decimal
SUPPORTED_INTERVAL_MINUTES = frozenset({1, 5, 15, 30, 60, 240, 720, 1440})
FEATURE_VERSION = "log-return-fourier-v1"
RESEARCH_VERSION = "strategy-lab-v0.3"
FEATURE_LOOKBACK_BARS = FOURIER_REQUIRED_PRICES - 1


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
    gross_long_return_bps: Decimal


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


def _build_examples(
    candles: Sequence[Candle], *, interval_minutes: int, holding_bars: int
) -> list[ResearchExample]:
    """Build next-open labels using contiguous history and outcome bars."""
    interval_seconds = interval_minutes * 60
    examples: list[ResearchExample] = []
    for index in range(FEATURE_LOOKBACK_BARS, len(candles) - holding_bars - 1):
        if any(
            candles[position].timestamp - candles[position - 1].timestamp != interval_seconds
            for position in range(index - FEATURE_LOOKBACK_BARS + 1, index + 1)
        ):
            continue
        entry_index = index + 1
        exit_index = entry_index + holding_bars
        if any(
            candles[position].timestamp - candles[position - 1].timestamp != interval_seconds
            for position in range(index + 1, exit_index + 1)
        ):
            continue
        gross_return_bps = (
            candles[exit_index].open / candles[entry_index].open - D(1)
        ) * D(10000)
        if not gross_return_bps.is_finite():
            continue
        examples.append(
            ResearchExample(
                decision_index=index,
                entry_index=entry_index,
                exit_index=exit_index,
                gross_long_return_bps=gross_return_bps,
            )
        )
    return examples


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
    """Compare TSM and Fourier using fixed, chronologically separated research rules."""
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
    if len(candles) < FEATURE_LOOKBACK_BARS + holding_bars + 2:
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
    margin_budgets = list(margin_cost_budgets_bps)
    spot_round_trip_cost = (
        D(2) * taker_fee_bps_per_side
        + spread_bps_round_trip
        + D(2) * slippage_bps_per_side
    )
    candidate_results: list[dict[str, Any]] = []
    for name in ("current_tsm", "fourier_cycle"):
        split_results: dict[str, Any] = {}
        for split_name, split_items in splits.items():
            signals: dict[int, str] = {}
            for item in split_items:
                index = item.decision_index
                if name == "current_tsm":
                    direction = _trend_direction(
                        candles,
                        index,
                        fast_threshold_bps=fast_threshold_bps,
                        slow_threshold_bps=slow_threshold_bps,
                    )
                else:
                    history = [float(candle.close) for candle in candles[: index + 1]]
                    spectrum = analyze_fourier_cycle(history)
                    direction = spectrum.direction if spectrum.qualified else None
                if direction in {"LONG", "SHORT"}:
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
        parameters: dict[str, Any] = (
            {
                "fast_threshold_bps": str(fast_threshold_bps),
                "slow_threshold_bps": str(slow_threshold_bps),
                "implementation_reference": "app.strategy.generate_trend_signal",
            }
            if name == "current_tsm"
            else {
                "method": "ROLLING_DFT_ON_DEMEANED_LOG_RETURNS",
                "window_bars_per_regime": 128,
                "period_band_bars": [6, 32],
                "stable_period_tolerance_pct": 20,
                "minimum_spectral_concentration": 0.25,
                "maximum_normalized_entropy": 0.82,
                "minimum_peak_to_second_peak_ratio": 1.15,
                "parameters_fitted_on_test_data": False,
            }
        )
        candidate_results.append(
            {"strategy": name, "parameters": parameters, "splits": split_results}
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
            "fourier_thresholds_selected_using_test_data": False,
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
        "fourier_method": {
            "status": "FIXED_RULE_NO_MODEL_FIT",
            "input": "CONTIGUOUS_HOURLY_LOG_RETURNS",
            "window_bars": 128,
            "period_band_bars": [6, 32],
            "two_adjacent_windows_required": True,
            "direction_is_calibrated_probability": False,
            "release_gate_eligible": False,
        },
        "benchmarks": benchmarks,
        "candidates": candidate_results,
    }


LIVE_SHADOW_CANDIDATE_VERSIONS = {
    "current_tsm": "tsm-0.2.0",
    "fourier_cycle": "fourier-cycle-0.1.0",
    "volatility_compression_breakout": "compression-breakout-0.1.0",
}


def generate_live_shadow_candidate_signals(
    raw_bars: Sequence[dict[str, Any]],
    *,
    fast_threshold_bps: Decimal,
    slow_threshold_bps: Decimal,
    holding_bars: int = 8,
    tsm_version: str = "tsm-0.2.0",
) -> list[dict[str, Any]]:
    """Produce TSM and Fourier diagnostics for research only; never creates orders."""
    if holding_bars < 1:
        raise ValueError("LIVE_SHADOW_HOLDING_BARS_MUST_BE_POSITIVE")
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

    # TSM uses only the newest uninterrupted H1 segment. A gap must not silently
    # turn 73 five-hour observations into a supposed 72-hour momentum window.
    contiguous_start = max(0, len(candles) - 1)
    while (
        contiguous_start > 0
        and candles[contiguous_start].timestamp - candles[contiguous_start - 1].timestamp == 3600
    ):
        contiguous_start -= 1
    tsm_history = candles[contiguous_start:]
    tsm = generate_trend_signal(
        "SHADOW_RESEARCH",
        [item.close for item in tsm_history],
        fast_threshold_bps=fast_threshold_bps,
        slow_threshold_bps=slow_threshold_bps,
    )
    market_regime = "UNKNOWN"
    if len(tsm_history) >= 73:
        if tsm.trend_efficiency_72h >= D("0.25"):
            market_regime = "TRENDING"
        elif tsm.trend_efficiency_72h <= D("0.12"):
            market_regime = "RANGING"
        else:
            market_regime = "TRANSITIONAL"

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
            "market_regime": market_regime,
            "stop_distance_pct": str(tsm.stop_distance_pct),
            "contiguous_h1_bars_used": len(tsm_history),
            "score_is_calibrated_probability": False,
        },
    }

    window = candles[-FOURIER_REQUIRED_PRICES:]
    contiguous = len(window) == FOURIER_REQUIRED_PRICES and all(
        right.timestamp - left.timestamp == 3600
        for left, right in zip(window, window[1:])
    )
    spectrum = (
        analyze_fourier_cycle([float(item.close) for item in window])
        if contiguous else None
    )
    fourier_candidate = {
        "candidate_name": "fourier_cycle",
        "candidate_version": LIVE_SHADOW_CANDIDATE_VERSIONS["fourier_cycle"],
        "direction": spectrum.direction if spectrum is not None and spectrum.qualified else "NONE",
        "signal_reason": (
            spectrum.reason if spectrum is not None
            else (
                "INSUFFICIENT_HISTORY"
                if len(window) < FOURIER_REQUIRED_PRICES
                else "HOURLY_HISTORY_GAP_IN_FOURIER_WINDOW"
            )
        ),
        "score": (
            D(str(spectrum.spectral_concentration))
            if spectrum is not None and spectrum.qualified else D(0)
        ),
        "features": {
            "method": "ROLLING_DFT_ON_DEMEANED_LOG_RETURNS",
            "window_bars_per_regime": 128,
            "dominant_period_bars": spectrum.dominant_period_bars if spectrum is not None else None,
            "spectral_concentration": spectrum.spectral_concentration if spectrum is not None else None,
            "normalized_spectral_entropy": spectrum.normalized_spectral_entropy if spectrum is not None else None,
            "period_stability_pct": spectrum.period_stability_pct if spectrum is not None else None,
            "predicted_next_return_bps": spectrum.predicted_next_return_bps if spectrum is not None else None,
            "amplitude_to_sigma": spectrum.amplitude_to_sigma if spectrum is not None else None,
            "market_regime": market_regime,
            "score_is_calibrated_probability": False,
            "research_only": True,
            "parameters_fitted_on_forward_or_test_data": False,
        },
    }

    breakout_direction = "NONE"
    breakout_reason = "INSUFFICIENT_COMPRESSION_HISTORY"
    breakout_distance_bps: Decimal | None = None
    recent_range_pct: Decimal | None = None
    baseline_range_pct: Decimal | None = None
    compression_ratio: Decimal | None = None
    volume_ratio: Decimal | None = None
    # 168 historical bars form a seven-day baseline, followed by 24 completed
    # compression bars and the current completed breakout bar. The current bar
    # is never included in the reference range or volume baseline.
    if len(candles) >= 193:
        compression_window = candles[-193:]
        compression_contiguous = all(
            right.timestamp - left.timestamp == 3600
            for left, right in zip(compression_window, compression_window[1:])
        )
        if not compression_contiguous:
            breakout_reason = "HOURLY_HISTORY_GAP_IN_COMPRESSION_WINDOW"
        else:
            baseline_bars = compression_window[:168]
            prior_24h = compression_window[168:192]
            current_bar = compression_window[192]
            prior_high = max(item.high for item in prior_24h)
            prior_low = min(item.low for item in prior_24h)
            recent_mid = (prior_high + prior_low) / D(2)
            if recent_mid > 0:
                recent_range_pct = (prior_high - prior_low) / recent_mid * D(100)

            daily_range_pct: list[Decimal] = []
            for offset in range(0, 168, 24):
                block = baseline_bars[offset:offset + 24]
                block_high = max(item.high for item in block)
                block_low = min(item.low for item in block)
                block_mid = (block_high + block_low) / D(2)
                if block_mid > 0:
                    daily_range_pct.append(
                        (block_high - block_low) / block_mid * D(100)
                    )
            if len(daily_range_pct) == 7:
                baseline_range_pct = median(daily_range_pct)
            if (
                recent_range_pct is not None
                and baseline_range_pct is not None
                and baseline_range_pct > 0
            ):
                compression_ratio = recent_range_pct / baseline_range_pct

            median_volume = median([item.volume for item in prior_24h])
            if median_volume > 0:
                volume_ratio = current_bar.volume / median_volume

            if current_bar.close > prior_high:
                breakout_direction = "LONG"
                breakout_distance_bps = (
                    current_bar.close - prior_high
                ) / prior_high * D(10000)
            elif current_bar.close < prior_low:
                breakout_direction = "SHORT"
                breakout_distance_bps = (
                    prior_low - current_bar.close
                ) / prior_low * D(10000)

            if breakout_direction == "NONE":
                breakout_reason = "NO_RANGE_BREAKOUT"
            elif compression_ratio is None or volume_ratio is None:
                breakout_direction = "NONE"
                breakout_reason = "COMPRESSION_OR_VOLUME_BASELINE_INVALID"
            elif compression_ratio > D("0.75") or volume_ratio < D("1.5"):
                breakout_direction = "NONE"
                breakout_reason = "BREAKOUT_NOT_CONFIRMED_BY_COMPRESSION_OR_VOLUME"
            else:
                breakout_reason = "QUALIFIED_COMPRESSION_BREAKOUT"

    breakout_candidate = {
        "candidate_name": "volatility_compression_breakout",
        "candidate_version": LIVE_SHADOW_CANDIDATE_VERSIONS[
            "volatility_compression_breakout"
        ],
        "direction": breakout_direction,
        "signal_reason": breakout_reason,
        "score": volume_ratio if breakout_direction != "NONE" and volume_ratio is not None else D(0),
        "features": {
            "market_regime": market_regime,
            "breakout_distance_bps": (
                str(breakout_distance_bps) if breakout_distance_bps is not None else None
            ),
            "prior_24h_range_pct": str(recent_range_pct) if recent_range_pct is not None else None,
            "baseline_7d_median_daily_range_pct": (
                str(baseline_range_pct) if baseline_range_pct is not None else None
            ),
            "range_compression_ratio": (
                str(compression_ratio) if compression_ratio is not None else None
            ),
            "hourly_volume_ratio_vs_prior_24h_median": (
                str(volume_ratio) if volume_ratio is not None else None
            ),
            "required_range_compression_ratio_max": "0.75",
            "required_hourly_volume_ratio_min": "1.5",
            "score_is_calibrated_probability": False,
            "research_only": True,
            "features_use_current_bar_only_for_breakout_confirmation": True,
        },
    }
    return [tsm_candidate, fourier_candidate, breakout_candidate]


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
