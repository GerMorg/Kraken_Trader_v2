import csv
from decimal import Decimal as D
import math

import pytest

from app.strategy_lab import (
    Candle,
    HistoricalDataError,
    compare_strategies,
    load_kraken_ohlcvt,
)


def synthetic_candles(count: int, *, test_shift_from: int | None = None) -> list[Candle]:
    result: list[Candle] = []
    for index in range(count):
        close = 100.0 * math.exp(
            0.00025 * index + 0.03 * math.sin(index / 18.0) + 0.009 * math.sin(index / 4.7)
        )
        if test_shift_from is not None and index >= test_shift_from:
            close *= 1.25
        open_price = close * (1.0 + 0.0002 * math.sin(index / 3.0))
        high = max(open_price, close) * 1.002
        low = min(open_price, close) * 0.998
        result.append(
            Candle(
                timestamp=index * 3600,
                open=D(str(open_price)),
                high=D(str(high)),
                low=D(str(low)),
                close=D(str(close)),
                volume=D("10000"),
                trades=100,
            )
        )
    return result


def compare(candles: list[Candle]) -> dict:
    return compare_strategies(
        candles,
        pair="XBT/USD",
        interval_minutes=60,
        taker_fee_bps_per_side=D("5"),
        spread_bps_round_trip=D("4"),
        slippage_bps_per_side=D("2"),
        margin_cost_budgets_bps=[D("5"), D("10"), D("20"), D("40")],
    )


def write_csv(path, rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows)


def valid_row(timestamp: int, open_price: str, close: str) -> list[str]:
    opening = D(open_price)
    closing = D(close)
    return [
        str(timestamp),
        str(opening),
        str(max(opening, closing) + D("1")),
        str(min(opening, closing) - D("1")),
        str(closing),
        "100",
        "10",
    ]


def test_kraken_headerless_ohlcvt_import_reports_gaps_without_filling_them(tmp_path):
    path = tmp_path / "XBTUSD_60.csv"
    second = valid_row(3600, "101", "102")
    write_csv(
        path,
        [
            valid_row(0, "100", "101"),
            second,
            second,
            valid_row(10800, "102", "103"),
        ],
    )

    candles, metadata = load_kraken_ohlcvt(path, interval_minutes=60)

    assert len(candles) == 3
    assert metadata["candle_count"] == 3
    assert metadata["identical_duplicate_rows_ignored"] == 1
    assert metadata["gap_count"] == 1
    assert metadata["missing_intervals_not_filled"] == 1
    assert metadata["source_sha256"]
    assert candles[-1].timestamp == 10800


def test_kraken_ohlcvt_rejects_conflicting_duplicate_timestamps(tmp_path):
    path = tmp_path / "conflict.csv"
    write_csv(
        path,
        [
            valid_row(0, "100", "101"),
            valid_row(3600, "101", "102"),
            valid_row(3600, "101", "101.5"),
        ],
    )

    with pytest.raises(HistoricalDataError, match="CONFLICTING_DUPLICATE_TIMESTAMP"):
        load_kraken_ohlcvt(path, interval_minutes=60)


def test_kraken_ohlcvt_rejects_unsorted_rows_and_invalid_ohlc(tmp_path):
    unsorted = tmp_path / "unsorted.csv"
    write_csv(unsorted, [valid_row(3600, "101", "102"), valid_row(0, "100", "101")])
    with pytest.raises(HistoricalDataError, match="SOURCE_NOT_CHRONOLOGICAL"):
        load_kraken_ohlcvt(unsorted, interval_minutes=60)

    bad_ohlc = tmp_path / "bad.csv"
    write_csv(
        bad_ohlc,
        [["0", "100", "99", "98", "101", "10", "1"]],
    )
    with pytest.raises(HistoricalDataError, match="INVALID_OHLCV_VALUES"):
        load_kraken_ohlcvt(bad_ohlc, interval_minutes=60)


def test_strategy_lab_compares_fixed_candidates_and_keeps_live_gate_closed():
    report = compare(synthetic_candles(1500))

    assert report["status"] == "RESEARCH_ONLY_NO_EXECUTION_SIMULATION"
    assert report["release_gate_eligible"] is False
    assert report["portfolio_pnl"] is False
    assert report["actual_fills"] is False
    assert report["method"]["target_horizon_purged_at_split_boundaries"] is True
    assert report["method"]["test_data_used_for_model_fitting_or_threshold_selection"] is False
    assert [item["strategy"] for item in report["candidates"]] == [
        "current_tsm",
        "mean_reversion_24h",
        "logistic_regression_baseline",
    ]
    assert report["machine_learning_model"]["status"] == "FITTED_ON_TRAIN_SPLIT_ONLY"
    assert report["machine_learning_model"]["training_samples"] >= 200
    assert "5" in report["candidates"][0]["splits"]["test"]["margin_net_sensitivity"]
    for candidate in report["candidates"]:
        for metrics in candidate["splits"].values():
            assert (
                metrics["non_overlapping_trade_count"]
                + metrics["overlapping_signals_excluded"]
                == metrics["candidate_signal_count"]
            )
            assert metrics["evaluation_scope"] in {
                "IN_SAMPLE_TRAIN",
                "CHRONOLOGICAL_HELD_OUT",
            }


def test_ml_fit_is_unchanged_when_only_test_period_prices_change():
    original = compare(synthetic_candles(1500))
    changed = compare(synthetic_candles(1500, test_shift_from=1200))

    original_model = original["machine_learning_model"]
    changed_model = changed["machine_learning_model"]
    assert original_model["training_samples"] == changed_model["training_samples"]
    assert original_model["coefficients"] == changed_model["coefficients"]
    assert original_model["intercept"] == changed_model["intercept"]
    assert original_model["standardization_means"] == changed_model["standardization_means"]
    assert original_model["standardization_scales"] == changed_model["standardization_scales"]


def test_strategy_lab_rejects_non_hourly_data_and_invalid_cost_assumptions():
    candles = synthetic_candles(300)
    with pytest.raises(ValueError, match="REQUIRES_HOURLY_BARS"):
        compare_strategies(
            candles,
            pair="XBT/USD",
            interval_minutes=15,
            taker_fee_bps_per_side=D("5"),
            spread_bps_round_trip=D("4"),
            slippage_bps_per_side=D("2"),
            margin_cost_budgets_bps=[D("5")],
        )
    with pytest.raises(ValueError, match="FINITE_NONNEGATIVE"):
        compare_strategies(
            candles,
            pair="XBT/USD",
            interval_minutes=60,
            taker_fee_bps_per_side=D("-1"),
            spread_bps_round_trip=D("4"),
            slippage_bps_per_side=D("2"),
            margin_cost_budgets_bps=[D("5")],
        )


def test_live_shadow_candidates_produce_three_separate_predictions_with_walk_forward_ml():
    from app.strategy_lab import generate_live_shadow_candidate_signals

    candles = synthetic_candles(720)
    raw_bars = [
        {
            "ts": item.timestamp,
            "open": item.open,
            "high": item.high,
            "low": item.low,
            "close": item.close,
            "volume": item.volume,
        }
        for item in candles
    ]
    signals = generate_live_shadow_candidate_signals(
        raw_bars,
        fast_threshold_bps=D("120"),
        slow_threshold_bps=D("180"),
        holding_bars=8,
        tsm_version="tsm-0.2.0",
    )

    assert [item["candidate_name"] for item in signals] == [
        "current_tsm",
        "mean_reversion_24h",
        "logistic_regression_wf",
    ]
    assert all(item["direction"] in {"LONG", "SHORT", "NONE"} for item in signals)
    assert signals[0]["candidate_version"] == "tsm-0.2.0"
    model = signals[2]
    assert model["features"]["training_status"] == "FITTED_ON_TRAIN_SPLIT_ONLY"
    assert model["features"]["training_sample_count"] >= 200
    assert model["features"]["training_labels_may_overlap"] is True
    assert model["features"]["probability_is_calibrated"] is False
    assert all("order" not in str(item).lower() for item in signals)


def test_live_walk_forward_model_does_not_train_on_the_current_decision_bar():
    from app.strategy_lab import generate_live_shadow_candidate_signals

    candles = synthetic_candles(720)
    changed_last = list(candles)
    last = changed_last[-1]
    changed_open = last.open * D("1.02")
    changed_close = last.close * D("1.03")
    changed_last[-1] = Candle(
        timestamp=last.timestamp,
        open=changed_open,
        high=max(changed_open, changed_close) * D("1.002"),
        low=min(changed_open, changed_close) * D("0.998"),
        close=changed_close,
        volume=last.volume,
        trades=last.trades,
    )

    def convert(data):
        return [
            {
                "ts": item.timestamp,
                "open": item.open,
                "high": item.high,
                "low": item.low,
                "close": item.close,
                "volume": item.volume,
            }
            for item in data
        ]

    original = generate_live_shadow_candidate_signals(
        convert(candles),
        fast_threshold_bps=D("120"),
        slow_threshold_bps=D("180"),
        holding_bars=8,
    )[2]
    changed = generate_live_shadow_candidate_signals(
        convert(changed_last),
        fast_threshold_bps=D("120"),
        slow_threshold_bps=D("180"),
        holding_bars=8,
    )[2]

    assert original["features"]["training_sample_count"] == changed["features"]["training_sample_count"]
    assert (
        original["features"]["positive_training_label_rate_pct"]
        == changed["features"]["positive_training_label_rate_pct"]
    )
