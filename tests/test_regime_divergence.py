from __future__ import annotations

import math

from app.regime_divergence import run_regime_divergence_research


def make_market(count: int = 600) -> tuple[dict, dict]:
    symbols = ["XBT/USD", "ETH/USD", "SOL/USD", "XRP/USD"]
    opens = {symbol: [] for symbol in symbols}
    closes = {symbol: [] for symbol in symbols}
    prices = {symbol: 100.0 + index * 0.01 for index, symbol in enumerate(symbols)}
    timestamps = [1_700_000_000 + index * 3600 for index in range(count)]
    for index in range(count):
        common = 0.0005 * math.sin(index / 19.0)
        for offset, symbol in enumerate(symbols):
            shock = 0.0
            if symbol == "SOL/USD" and index % 53 in {0, 1, 2, 3, 4, 5}:
                shock = -0.012 if index % 53 < 3 else 0.006
            move = common + 0.0008 * math.sin(index / (5.0 + offset)) + shock
            opening = prices[symbol]
            closing = opening * (1.0 + move)
            opens[symbol].append(opening)
            closes[symbol].append(closing)
            prices[symbol] = closing
    histories = {
        "symbols": symbols,
        "timestamps": timestamps,
        "opens": opens,
        "closes": closes,
    }
    costs = {
        symbol: {"taker_fee_bps": "40", "spread_bps": "3"}
        for symbol in symbols
    }
    return histories, costs


def test_regime_divergence_research_is_isolated_and_cost_aware():
    market, costs = make_market()
    result = run_regime_divergence_research(market, costs)

    assert result["status"] == "RESEARCH_ONLY_NO_EXECUTION_SIMULATION"
    assert result["candidate_name"] == "regime_conditioned_residual_recoil"
    assert result["release_gate_eligible"] is False
    assert result["strategy_promotion_allowed"] is False
    assert result["order_creation_allowed"] is False
    assert result["actual_fills"] is False
    assert result["portfolio_pnl"] is False
    assert set(result["chronological_splits"]) == {"train", "validation", "test"}
    assert len(result["slippage_sensitivity"]) == 5
    assert result["method"]["future_data_used_for_signal"] is False
    assert result["method"]["market_volatility_window_bars"] == 24
    assert result["method"]["market_volatility_reference_windows"] == 72
    assert "market_regime_filter_excluded_count" in result


def test_regime_divergence_requires_synchronized_history():
    market, costs = make_market(120)
    result = run_regime_divergence_research(market, costs)

    assert result["status"] == "INSUFFICIENT_SYNCHRONIZED_HISTORY"
    assert result["release_gate_eligible"] is False
    assert result["order_creation_allowed"] is False
