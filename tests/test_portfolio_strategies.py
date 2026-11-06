from __future__ import annotations

import math
import random

from app.portfolio_strategies import (
    _aligned_market_histories,
    _backtest_pair,
    _pair_model,
    _select_negative_pairs,
    _simulate_rebalancing,
    _pair_portfolio_simulation,
    _current_order_book_cost_diagnostics,
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
    opens = {
        "A/USD": [price * 1.001 for price in prices_a],
        "B/USD": [price * 0.999 for price in prices_b],
    }
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
        assert all(item["exit_index"] == item["exit_trigger_index"] + 1 for item in trades)
        assert all(
            item["exit_execution_reference"] == "NEXT_H1_OPEN_AFTER_CLOSE_SIGNAL"
            for item in trades
        )
        assert all(
            item["exit_price_a"] == opens["A/USD"][item["exit_index"]]
            for item in trades
        )


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
    assert len(
        report["variant_a_volatility_harvesting"]["negative_correlation_threshold_sensitivity"]
    ) == 4
    assert len(
        report["variant_a_volatility_harvesting"]["primary_threshold_slippage_sensitivity"]
    ) == 5
    assert report["variant_b_relative_value_pairs"]["portfolio_simulation"] is True
    assert len(
        report["variant_b_relative_value_pairs"]["concurrent_portfolio_slippage_sensitivity"]
    ) == 5
    assert report["variant_c_cross_sectional_momentum"]["actual_fills"] is False
    assert report["variant_c_cross_sectional_momentum"]["portfolio_pnl"] is False
    assert len(
        report["variant_c_cross_sectional_momentum"]["slippage_sensitivity"]
    ) == 5
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


def test_pair_portfolio_simulation_limits_slots_and_prevents_double_asset_exposure() -> None:
    prices = [100.0 + index for index in range(240)]
    opens = {symbol: list(prices) for symbol in ("A/USD", "B/USD", "C/USD", "D/USD")}
    closes = {symbol: list(prices) for symbol in ("A/USD", "B/USD", "C/USD", "D/USD")}
    common = {
        "direction": "LONG_SPREAD",
        "entry_index": 180,
        "exit_index": 200,
        "entry_price_a": 280.0,
        "entry_price_b": 280.0,
        "leg_weight_a": 0.5,
        "leg_weight_b": 0.5,
        "fee_per_side_bps": 1.0,
        "half_spread_per_side_bps": 0.5,
        "split": "train",
    }
    trades = [
        {**common, "symbol_a": "A/USD", "symbol_b": "B/USD", "entry_z_score": 3.0},
        {**common, "symbol_a": "C/USD", "symbol_b": "D/USD", "entry_z_score": 2.5},
        {**common, "symbol_a": "A/USD", "symbol_b": "C/USD", "entry_z_score": 2.0},
    ]

    result = _pair_portfolio_simulation(
        trades,
        opens,
        closes,
        bar_count=len(prices),
        train_end=210,
        validation_end=230,
        slippage_bps_per_side=5.0,
    )

    assert result["selected_trade_count"] == 2
    assert result["skipped_due_to_asset_overlap"] == 1
    assert result["observed_max_concurrent_pairs"] == 2
    assert result["maximum_combined_gross_notional_pct_of_initial_equity"] == 100.0
    assert result["spot_net_portfolio"] is not None
    assert result["portfolio_pnl"] is False
    assert result["actual_fills"] is False


def test_current_l2_cost_diagnostics_use_depth_and_flag_not_fills() -> None:
    snapshot = {
        "snapshot_ts": 1_700_000_000,
        "bids": [[str(100.0 - index * 0.01), "100"] for index in range(10)],
        "asks": [[str(100.1 + index * 0.01), "100"] for index in range(10)],
    }

    result = _current_order_book_cost_diagnostics({
        "A/USD": {"taker_fee_bps": "5", "order_book_l2": snapshot}
    })

    diagnostics = result["by_symbol"]["A/USD"]
    assert diagnostics["status"] == "CURRENT_L2_COST_ESTIMATE"
    assert diagnostics["round_trip_assumption"] == "SAME_SNAPSHOT_BUY_AND_SELL; NOT_A_FILL"
    assert all(
        item["status"] == "VISIBLE_DEPTH_FILLED"
        for item in diagnostics["notional_scenarios"]
    )
    assert result["historical_cost_evidence"] is False
    assert result["actual_fills"] is False


def test_pair_trade_return_uses_beta_as_dollar_notional_ratio() -> None:
    from app.portfolio_strategies import _pair_trade_return

    common_move_bps, weight_a, weight_b = _pair_trade_return(
        direction="LONG_SPREAD",
        entry_a=100.0,
        entry_b=250.0,
        exit_a=102.0,
        exit_b=252.5,
        beta=2.0,
    )

    assert weight_a == 1.0 / 3.0
    assert weight_b == 2.0 / 3.0
    # If r_A = beta * r_B the beta-hedged dollar spread cancels common movement.
    assert abs(common_move_bps) < 1e-8



def test_cross_sectional_momentum_uses_lagged_close_and_balanced_long_short_book():
    from app.portfolio_strategies import _simulate_cross_sectional_momentum

    count = 120
    trends = {
        "A/USD": 0.0030,
        "B/USD": 0.0020,
        "C/USD": 0.0010,
        "D/USD": -0.0010,
        "E/USD": -0.0020,
        "F/USD": -0.0030,
    }
    closes = {
        symbol: [100.0 * math.exp(rate * index) for index in range(count)]
        for symbol, rate in trends.items()
    }
    opens = {symbol: list(values) for symbol, values in closes.items()}
    market = {
        "symbols": sorted(closes),
        "timestamps": [1_700_000_000 + index * 3600 for index in range(count)],
        "opens": opens,
        "closes": closes,
    }
    costs = {
        symbol: {
            "taker_fee_bps": "5",
            "maker_fee_bps": "2",
            "spread_bps": "4",
        }
        for symbol in closes
    }

    report = _simulate_cross_sectional_momentum(market, costs)

    assert report["status"] == "CANDIDATE_RESEARCH"
    assert report["method"]["selection_uses_only_closed_decision_bar"] is True
    assert report["method"]["execution_reference"] == "NEXT_H1_OPEN_TO_NEXT_H1_OPEN"
    assert report["method"]["gross_exposure_pct"] == 80.0
    assert report["method"]["net_exposure_pct"] == 0.0
    assert report["latest_long_candidates"] == ["A/USD", "B/USD", "C/USD"]
    assert report["latest_short_candidates"] == ["F/USD", "E/USD", "D/USD"]
    assert report["invested_rebalance_count"] > 0
    assert report["gross"]["total_return_pct"] > 0
    assert report["costed_proxy"] is not None
    assert report["margin_shorting_required"] is True
    assert report["margin_net_returns_calculated"] is False
    assert report["actual_fills"] is False
    assert report["portfolio_pnl"] is False


def test_cross_sectional_momentum_maker_reference_is_separate_from_taker_baseline():
    from app.portfolio_strategies import _simulate_cross_sectional_momentum

    count = 100
    trends = {
        "A/USD": 0.003, "B/USD": 0.002, "C/USD": 0.001,
        "D/USD": -0.001, "E/USD": -0.002, "F/USD": -0.003,
    }
    closes = {
        symbol: [100.0 * math.exp(rate * index) for index in range(count)]
        for symbol, rate in trends.items()
    }
    market = {
        "symbols": sorted(closes),
        "timestamps": [1_700_000_000 + index * 3600 for index in range(count)],
        "opens": closes,
        "closes": closes,
    }
    costs = {
        symbol: {"taker_fee_bps": "10", "maker_fee_bps": "2", "spread_bps": "4"}
        for symbol in closes
    }
    taker = _simulate_cross_sectional_momentum(market, costs, fee_type="taker")
    maker = _simulate_cross_sectional_momentum(market, costs, fee_type="maker_reference")
    assert taker["fee_basis"] == "KRAKEN_TAKER_FEE"
    assert maker["fee_basis"] == "HYPOTHETICAL_MAKER_FEE_REFERENCE_NO_FILL_ASSUMPTION"
    assert maker["costed_proxy"]["total_return_pct"] >= taker["costed_proxy"]["total_return_pct"]
