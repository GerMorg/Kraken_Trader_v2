from __future__ import annotations

from math import exp, pi, sin

from app.fourier import FOURIER_REQUIRED_PRICES, analyze_fourier_cycle


def cyclic_prices(period: int = 16, *, split_period: int | None = None) -> list[float]:
    prices = [100.0]
    for index in range(FOURIER_REQUIRED_PRICES - 1):
        active_period = period if split_period is None or index < 128 else split_period
        return_value = 0.008 * sin(2.0 * pi * index / active_period)
        prices.append(prices[-1] * exp(return_value))
    return prices


def test_fourier_detects_stable_synthetic_cycle_as_research_only_signal():
    result = analyze_fourier_cycle(cyclic_prices(period=16))
    assert result.qualified is True
    assert result.reason == "STABLE_DOMINANT_CYCLE_PHASE_PROJECTION"
    assert result.direction in {"LONG", "SHORT"}
    assert abs(result.dominant_period_bars - 16.0) <= 1.0
    assert result.spectral_concentration >= 0.25
    assert result.period_stability_pct <= 20.0
    assert result.predicted_next_return_bps is not None


def test_fourier_rejects_period_shift_between_adjacent_windows():
    result = analyze_fourier_cycle(cyclic_prices(period=12, split_period=28))
    assert result.qualified is False
    assert result.direction == "NONE"
    assert result.reason == "SPECTRAL_QUALITY_OR_PERIOD_STABILITY_FAILED"


def test_fourier_rejects_short_flat_and_invalid_price_history():
    short = analyze_fourier_cycle([100.0] * (FOURIER_REQUIRED_PRICES - 1))
    flat = analyze_fourier_cycle([100.0] * FOURIER_REQUIRED_PRICES)
    invalid = analyze_fourier_cycle(
        [100.0] * (FOURIER_REQUIRED_PRICES - 1) + [float("nan")]
    )
    assert short.reason == "INSUFFICIENT_HISTORY"
    assert flat.reason == "INSUFFICIENT_RETURN_VARIATION"
    assert invalid.reason == "INVALID_PRICE_INPUT"
    assert not short.qualified and not flat.qualified and not invalid.qualified
