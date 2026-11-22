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
        "fourier_cycle",
    ]
    assert report["fourier_method"]["status"] == "FIXED_RULE_NO_MODEL_FIT"
    assert report["fourier_method"]["release_gate_eligible"] is False
    assert report["method"]["fourier_thresholds_selected_using_test_data"] is False
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


def test_test_split_prices_do_not_change_fixed_fourier_rule():
    original = compare(synthetic_candles(1500))
    changed = compare(synthetic_candles(1500, test_shift_from=1200))
    assert original["fourier_method"] == changed["fourier_method"]
    assert original["method"]["fourier_thresholds_selected_using_test_data"] is False


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


def test_live_shadow_candidates_produce_three_separate_predictions():
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
        "current_tsm", "fourier_cycle", "volatility_compression_breakout"
    ]
    assert all(item["direction"] in {"LONG", "SHORT", "NONE"} for item in signals)
    assert signals[0]["candidate_version"] == "tsm-0.2.0"
    assert signals[1]["candidate_version"] == "fourier-cycle-0.1.0"
    assert signals[1]["features"]["research_only"] is True
    assert signals[2]["candidate_version"] == "compression-breakout-0.1.0"
    assert signals[2]["features"]["research_only"] is True
    assert signals[2]["features"]["score_is_calibrated_probability"] is False
    assert signals[1]["features"]["score_is_calibrated_probability"] is False
    assert all("order" not in str(item).lower() for item in signals)



def test_tsm_uses_73_contiguous_hourly_bars_while_fourier_requires_257():
    from app.strategy_lab import generate_live_shadow_candidate_signals

    candles = synthetic_candles(100)
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
    )
    assert signals[0]["signal_reason"] != "INSUFFICIENT_HISTORY"
    assert signals[0]["features"]["contiguous_h1_bars_used"] == 100
    assert signals[1]["direction"] == "NONE"
    assert signals[1]["signal_reason"] == "INSUFFICIENT_HISTORY"


def test_tsm_resets_its_lookback_after_hourly_data_gap():
    from app.strategy_lab import generate_live_shadow_candidate_signals

    candles = synthetic_candles(300)
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
    raw_bars[-50:] = [
        {**row, "ts": row["ts"] + 3600}
        for row in raw_bars[-50:]
    ]
    signals = generate_live_shadow_candidate_signals(
        raw_bars,
        fast_threshold_bps=D("120"),
        slow_threshold_bps=D("180"),
    )
    assert signals[0]["signal_reason"] == "INSUFFICIENT_HISTORY"
    assert signals[0]["features"]["contiguous_h1_bars_used"] == 50
    assert signals[1]["signal_reason"] == "HOURLY_HISTORY_GAP_IN_FOURIER_WINDOW"



def test_compression_breakout_requires_prior_range_compression_and_volume_confirmation():
    from app.strategy_lab import generate_live_shadow_candidate_signals

    raw_bars = []
    for index in range(168):
        raw_bars.append({
            "ts": index * 3600,
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": "10",
        })
    for index in range(168, 192):
        raw_bars.append({
            "ts": index * 3600,
            "open": "100",
            "high": "100.25",
            "low": "99.75",
            "close": "100",
            "volume": "10",
        })
    raw_bars.append({
        "ts": 192 * 3600,
        "open": "100",
        "high": "100.6",
        "low": "99.9",
        "close": "100.5",
        "volume": "20",
    })

    candidates = generate_live_shadow_candidate_signals(
        raw_bars,
        fast_threshold_bps=D("120"),
        slow_threshold_bps=D("180"),
    )
    breakout = candidates[2]
    assert breakout["direction"] == "LONG"
    assert breakout["signal_reason"] == "QUALIFIED_COMPRESSION_BREAKOUT"
    assert D(str(breakout["features"]["range_compression_ratio"])) <= D("0.75")
    assert D(str(breakout["features"]["hourly_volume_ratio_vs_prior_24h_median"])) >= D("1.5")
    assert breakout["features"]["market_regime"] == "RANGING"
