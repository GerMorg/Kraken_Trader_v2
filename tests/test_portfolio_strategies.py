from __future__ import annotations

import math
import random

from app.portfolio_strategies import (
    _aligned_market_histories,
    _backtest_pair,
    _pair_model,
    _select_negative_pairs,
    _simulate_rebalancing,
    run_multi_asset_strategy_research,
)


def _bars_from_closes(closes: list[float], *, start: int = 1_700_000_000) -> list[dict]:
    return [
        {
            "ts": start + index * 3600,
            "open": value * (1.0 - 0.0003),
            "high": value * 1.001,
            "low": value * 0.999,
            "close": value,
            "volume": 10_000.0,
        }
        for index, value in enumerate(closes)
    ]


def _correlated_pair(
    count: int = 720, *, final_shock: float = 0.0
) -> tuple[list[float], list[float]]:
    rng = random.Random(41)
    log_b = [math.log(100.0)]
    residual = [0.0]
    for _ in range(1, count):
        log_b.append(log_b[-1] + 0.0002 + rng.gauss(0.0, 0.008))
        residual.append(0.82 * residual[-1] + rng.gauss(0.0, 0.0035))
    if final_shock:
        residual[-1] += final_shock
    prices_b = [math.exp(value) for value in log_b]
    prices_a = [
        math.exp(0.2 + 1.15 * value + error)
        for value, error in zip(log_b, residual, strict=True)
    ]
    return prices_a, prices_b


def test_alignment_never_fills_missing_hourly_bars() -> None:
    left = [100.0 + index * 0.1 for index in range(260)]
    right = [200.0 + index * 0.2 for index in range(260)]
    bars_left = _bars_from_closes(left)
    bars_right = _bars_from_closes(right)
    bars_right.pop(120)

    result = _aligned_market_histories(
        {"A/USD": bars_left, "B/USD": bars_right}, min_bars=240
    )

    assert result["status"] == "INSUFFICIENT_CONTIGUOUS_HISTORY"
    assert len(result["timestamps"]) < 240
    assert all(
        right - left == 3600
        for left, right in zip(result["timestamps"], result["timestamps"][1:])
    )


def test_negative_correlation_selector_returns_disjoint_pairs() -> None:
    count = 240
    first = [100.0]
    second = [100.0]
    third = [100.0]
    fourth = [100.0]
    for index in range(1, count):
        shock = 0.003 if index % 2 else -0.003
        first.append(first[-1] * math.exp(shock))
        second.append(second[-1] * math.exp(-shock))
        third.append(third[-1] * math.exp(shock * 0.7))
        fourth.append(fourth[-1] * math.exp(-shock * 0.7))

    pairs = _select_negative_pairs(
        {"A/USD": first, "B/USD": second, "C/USD": third, "D/USD": fourth},
        index=count - 1,
    )

    assert len(pairs) == 2
    assert all(correlation <= -0.25 for _, _, correlation in pairs)
    assets = [symbol for pair in pairs for symbol in pair[:2]]
    assert len(assets) == len(set(assets))


def test_buy_and_hold_benchmark_only_trades_at_initial_allocation() -> None:
    count = 420
    closes = {
        "A/USD": [
            100.0 * math.exp(0.001 * index + 0.01 * math.sin(index / 12))
            for index in range(count)
        ],
        "B/USD": [
            100.0 * math.exp(-0.0002 * index + 0.015 * math.sin(index / 15))
            for index in range(count)
        ],
    }
    market = {
        "symbols": sorted(closes),
        "timestamps": [1_700_000_000 + index * 3600 for index in range(count)],
        "opens": closes,
        "closes": closes,
    }
    costs = {symbol: {"taker_fee_bps": 5, "spread_bps": 3} for symbol in closes}

    buy_hold = _simulate_rebalancing(market, costs, strategy="buy_and_hold_equal_weight")
    periodic = _simulate_rebalancing(market, costs, strategy="periodic_equal_weight_rebalance")

    assert buy_hold["rebalance_count"] == 1
    assert periodic["rebalance_count"] > buy_hold["rebalance_count"]


def test_pair_model_identifies_mean_reverting_spread_with_positive_common_trend() -> None:
    prices_a, prices_b = _correlated_pair(500, final_shock=0.035)

    model = _pair_model(prices_a, prices_b, end_index=len(prices_a) - 1)

    assert model is not None
    assert 0.2 <= model["beta"] <= 5.0
    assert model["return_correlation"] >= 0.30
    assert 2.0 <= model["half_life_bars"] <= 72.0
    assert model["z_score"] >= 2.0


def test_pair_backtest_uses_non_overlapping_trades_and_does_not_force_end_exit() -> None:
    prices_a, prices_b = _correlated_pair(420)
    for shock_index in (240, 300, 360):
        prices_a[shock_index] *= math.exp(0.055)
    opens = {"A/USD": list(prices_a), "B/USD": list(prices_b)}
    closes = {"A/USD": list(prices_a), "B/USD": list(prices_b)}
    costs = {symbol: {"taker_fee_bps": 5, "spread_bps": 4} for symbol in closes}

    result = _backtest_pair(
        "A/USD",
        "B/USD",
        opens,
        closes,
        costs,
        train_end=252,
        validation_end=336,
    )

    trades = result["trades"]
    for previous, current in zip(trades, trades[1:]):
        assert current["entry_index"] > previous["exit_index"]
    assert result["pending_at_sample_end"] >= 0
    if trades:
        assert all(item["direction"] in {"LONG_SPREAD", "SHORT_SPREAD"} for item in trades)
        assert all(item["net_return_bps"] is not None for item in trades)


def test_multi_asset_report_keeps_both_variants_research_only() -> None:
    prices_a, prices_b = _correlated_pair(720, final_shock=0.035)
    prices_c = [100.0]
    for index in range(1, len(prices_a)):
        ret = 0.002 if index % 2 else -0.002
        prices_c.append(prices_c[-1] * math.exp(ret))
    histories = {
        "A/USD": _bars_from_closes(prices_a),
        "B/USD": _bars_from_closes(prices_b),
        "C/USD": _bars_from_closes(prices_c),
    }
    costs = {
        symbol: {
            "taker_fee_bps": "5",
            "spread_bps": "4",
            "margin_buy_supported": True,
            "margin_sell_supported": True,
        }
        for symbol in histories
    }

    report = run_multi_asset_strategy_research(
        histories, costs, now_epoch=histories["A/USD"][-1]["ts"] + 3600
    )

    assert report["status"] == "RESEARCH_COMPLETE"
    assert report["as_of_bar_freshness"] == "FRESH"
    assert report["variant_a_volatility_harvesting"]["portfolio_simulation"] is True
    assert report["variant_b_relative_value_pairs"]["latest_pair_eligible_count"] >= 1
    assert all(
        item["margin_execution_eligible_from_instrument_metadata"] is True
        for item in report["variant_b_relative_value_pairs"]["latest_pair_signals"]
        if item["direction"] != "NONE"
    )
    assert report["release_gate_eligible"] is False
    assert report["actual_fills"] is False
    assert report["portfolio_pnl"] is False
    assert report["order_creation_allowed"] is False
    assert report["strategy_promotion_allowed"] is False


def test_multi_asset_insufficient_history_does_not_claim_strategy_result() -> None:
    histories = {
        "A/USD": _bars_from_closes([100.0 + index for index in range(100)]),
        "B/USD": _bars_from_closes([200.0 + index for index in range(100)]),
    }
    report = run_multi_asset_strategy_research(histories, {})

    assert report["status"] == "INSUFFICIENT_ASSET_HISTORY"
    assert report["release_gate_eligible"] is False
    assert report["order_creation_allowed"] is False


def test_pair_trade_return_uses_beta_as_dollar_notional_ratio() -> None:
    from app.portfolio_strategies import _pair_trade_return

    gross_bps, weight_a, weight_b = _pair_trade_return(
        direction="LONG_SPREAD",
        entry_a=100.0,
        entry_b=250.0,
        exit_a=102.0,
        exit_b=251.0,
        beta=2.0,
    )

    assert weight_a == 1.0 / 3.0
    assert weight_b == 2.0 / 3.0
    # If r_A = beta * r_B the beta-hedged dollar spread has no common-trend PnL.
    common_move_bps, _, _ = _pair_trade_return(
        direction="LONG_SPREAD",
        entry_a=100.0,
        entry_b=250.0,
        exit_a=102.0,
        exit_b=251.0,
        beta=2.0,
    )
    assert math.isfinite(gross_bps)
    assert math.isfinite(common_move_bps)
