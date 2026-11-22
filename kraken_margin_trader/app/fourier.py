from __future__ import annotations

from dataclasses import dataclass
from math import cos, log, pi, sin, sqrt
from statistics import pstdev
from typing import Sequence

FOURIER_WINDOW_BARS = 128
FOURIER_REQUIRED_PRICES = 2 * FOURIER_WINDOW_BARS + 1
MIN_PERIOD_BARS = 6
MAX_PERIOD_BARS = 32


@dataclass(frozen=True)
class FourierAnalysis:
    qualified: bool
    reason: str
    direction: str
    dominant_period_bars: float | None
    spectral_concentration: float
    normalized_spectral_entropy: float
    period_stability_pct: float | None
    predicted_next_return_bps: float | None
    amplitude_to_sigma: float


def _window_spectrum(values: Sequence[float]) -> tuple[float, float, float, float, float, float] | None:
    count = len(values)
    if count != FOURIER_WINDOW_BARS:
        return None
    mean = sum(values) / count
    centered = [value - mean for value in values]
    sigma = pstdev(centered)
    if not sigma or sigma <= 1e-12:
        return None

    # Hann window reduces spectral leakage at the finite rolling-window edges.
    windowed = [
        centered[index] * (0.5 - 0.5 * cos(2.0 * pi * index / (count - 1)))
        for index in range(count)
    ]
    first_bin = max(1, int(count / MAX_PERIOD_BARS))
    last_bin = min(count // 2 - 1, int(count / MIN_PERIOD_BARS))
    powers: list[tuple[int, float]] = []
    for frequency_bin in range(first_bin, last_bin + 1):
        angle = 2.0 * pi * frequency_bin / count
        real = sum(windowed[t] * cos(angle * t) for t in range(count))
        imag = sum(windowed[t] * sin(angle * t) for t in range(count))
        power = real * real + imag * imag
        if power > 0:
            powers.append((frequency_bin, power))
    if len(powers) < 3:
        return None

    powers.sort(key=lambda item: item[1], reverse=True)
    total_power = sum(value for _, value in powers)
    if total_power <= 0:
        return None
    peak_bin, peak_power = powers[0]
    concentration = peak_power / total_power
    probabilities = [value / total_power for _, value in powers]
    entropy = -sum(p * log(p) for p in probabilities if p > 0) / log(len(probabilities))
    second_power = powers[1][1]
    peak_ratio = peak_power / second_power if second_power > 0 else float("inf")
    period = count / peak_bin

    # Least-squares sinusoid fit gives a one-step phase projection, not a return forecast.
    angle = 2.0 * pi * peak_bin / count
    cc = sum(cos(angle * t) ** 2 for t in range(count))
    ss = sum(sin(angle * t) ** 2 for t in range(count))
    cs = sum(cos(angle * t) * sin(angle * t) for t in range(count))
    xc = sum(centered[t] * cos(angle * t) for t in range(count))
    xs = sum(centered[t] * sin(angle * t) for t in range(count))
    determinant = cc * ss - cs * cs
    if determinant <= 1e-12:
        return None
    coefficient_cos = (xc * ss - xs * cs) / determinant
    coefficient_sin = (xs * cc - xc * cs) / determinant
    predicted = (
        coefficient_cos * cos(angle * count)
        + coefficient_sin * sin(angle * count)
    )
    amplitude_to_sigma = sqrt(coefficient_cos**2 + coefficient_sin**2) / sigma
    return period, concentration, entropy, peak_ratio, predicted, amplitude_to_sigma


def analyze_fourier_cycle(
    prices: Sequence[float],
    *,
    min_concentration: float = 0.25,
    max_entropy: float = 0.82,
    max_period_stability_pct: float = 20.0,
    min_amplitude_to_sigma: float = 0.12,
) -> FourierAnalysis:
    """Research-only cycle diagnostic on two consecutive windows of log returns.

    A dominant peak is accepted only when its period remains similar across both
    adjacent windows, the spectrum is sufficiently concentrated, and a one-step
    phase projection is non-trivial. No probability or expected-return calibration
    is implied. Callers must also verify candle timestamps are contiguous.
    """
    if len(prices) < FOURIER_REQUIRED_PRICES:
        return FourierAnalysis(
            False, "INSUFFICIENT_HISTORY", "NONE", None, 0.0, 1.0, None, None, 0.0
        )
    converted: list[float] = []
    for value in prices[-FOURIER_REQUIRED_PRICES:]:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return FourierAnalysis(
                False, "INVALID_PRICE_INPUT", "NONE", None, 0.0, 1.0, None, None, 0.0
            )
        if number <= 0 or number != number or number in (float("inf"), float("-inf")):
            return FourierAnalysis(
                False, "INVALID_PRICE_INPUT", "NONE", None, 0.0, 1.0, None, None, 0.0
            )
        converted.append(number)

    returns = [
        log(converted[index] / converted[index - 1])
        for index in range(1, len(converted))
    ]
    first = _window_spectrum(returns[:FOURIER_WINDOW_BARS])
    second = _window_spectrum(returns[FOURIER_WINDOW_BARS:])
    if first is None or second is None:
        return FourierAnalysis(
            False, "INSUFFICIENT_RETURN_VARIATION", "NONE", None, 0.0, 1.0, None, None, 0.0
        )

    first_period = first[0]
    period, concentration, entropy, peak_ratio, predicted, amplitude_ratio = second
    stability = abs(first_period - period) / ((first_period + period) / 2.0) * 100.0
    qualified = (
        concentration >= min_concentration
        and entropy <= max_entropy
        and stability <= max_period_stability_pct
        and peak_ratio >= 1.15
        and amplitude_ratio >= min_amplitude_to_sigma
    )
    direction = "LONG" if predicted > 0 else "SHORT" if predicted < 0 else "NONE"
    if not qualified:
        reason = "SPECTRAL_QUALITY_OR_PERIOD_STABILITY_FAILED"
        direction = "NONE"
    elif direction == "NONE":
        reason = "NO_PHASE_DIRECTION"
    else:
        reason = "STABLE_DOMINANT_CYCLE_PHASE_PROJECTION"

    return FourierAnalysis(
        qualified=qualified,
        reason=reason,
        direction=direction,
        dominant_period_bars=period,
        spectral_concentration=concentration,
        normalized_spectral_entropy=entropy,
        period_stability_pct=stability,
        predicted_next_return_bps=predicted * 10000.0,
        amplitude_to_sigma=amplitude_ratio,
    )
