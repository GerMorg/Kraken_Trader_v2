from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from statistics import median
from typing import Any, Sequence

D = Decimal
RESEARCH_SLIPPAGE_BPS_PER_SIDE = D("5")
MINIMUM_COSTED_FORWARD_OBSERVATIONS = 100
MINIMUM_HOLDOUT_OBSERVATIONS_FOR_REVIEW = 20


def observation_id(
    *,
    pair: str,
    interval_minutes: int,
    bar_ts: int,
    strategy_version: str,
    fast_threshold_bps: Decimal,
    slow_threshold_bps: Decimal,
    horizon_seconds: int,
) -> str:
    identity = "|".join(
        (
            pair,
            str(interval_minutes),
            str(bar_ts),
            strategy_version,
            str(fast_threshold_bps),
            str(slow_threshold_bps),
            str(horizon_seconds),
        )
    )
    return sha256(identity.encode("utf-8")).hexdigest()


def estimate_forward_outcome(
    *,
    direction: str,
    entry_mid_price: Decimal,
    exit_close_price: Decimal,
    entry_spread_bps: Decimal,
    exit_spread_bps: Decimal,
    taker_fee_bps: Decimal | None,
    slippage_bps_per_side: Decimal = RESEARCH_SLIPPAGE_BPS_PER_SIDE,
) -> dict[str, Any]:
    """Calculate a forward signal label, not actual fills or portfolio PnL."""
    normalized_direction = direction.upper()
    values = (
        entry_mid_price,
        exit_close_price,
        entry_spread_bps,
        exit_spread_bps,
        slippage_bps_per_side,
    )
    if normalized_direction not in {"LONG", "SHORT"}:
        raise ValueError("RESEARCH_DIRECTION_NOT_DIRECTIONAL")
    if any(not value.is_finite() for value in values):
        raise ValueError("RESEARCH_OUTCOME_INPUT_NON_FINITE")
    if entry_mid_price <= 0 or exit_close_price <= 0:
        raise ValueError("RESEARCH_OUTCOME_PRICE_INVALID")
    if entry_spread_bps < 0 or exit_spread_bps < 0 or slippage_bps_per_side < 0:
        raise ValueError("RESEARCH_OUTCOME_COST_INPUT_NEGATIVE")
    if taker_fee_bps is not None and (
        not taker_fee_bps.is_finite() or taker_fee_bps < 0
    ):
        raise ValueError("RESEARCH_TAKER_FEE_INVALID")

    sign = D(1) if normalized_direction == "LONG" else D(-1)
    gross_return_bps = sign * (exit_close_price / entry_mid_price - D(1)) * D(10000)
    outcome: dict[str, Any] = {
        "gross_return_bps": gross_return_bps,
        "spot_cost_estimate_bps": None,
        "spot_net_return_bps": None,
        "margin_cost_break_even_bps": None,
        "cost_quality": "GROSS_ONLY_TAKER_FEE_UNKNOWN",
    }
    if taker_fee_bps is None:
        return outcome

    # Mid-to-mid price movement: pay half the observed spread on each side.
    # The exit spread is observed in the current ticker when the delayed target bar
    # becomes available, so it is an estimate—not historical executable-book evidence.
    spread_cost_bps = (entry_spread_bps + exit_spread_bps) / D(2)
    spot_cost_bps = D(2) * taker_fee_bps + spread_cost_bps + D(2) * slippage_bps_per_side
    spot_net_bps = gross_return_bps - spot_cost_bps
    outcome.update(
        {
            "spot_cost_estimate_bps": spot_cost_bps,
            "spot_net_return_bps": spot_net_bps,
            # Maximum combined opening + rollover cost before estimated margin net
            # expectancy turns non-positive. It is not a quoted Kraken fee.
            "margin_cost_break_even_bps": spot_net_bps,
            "cost_quality": (
                "KRAKEN_TRADE_VOLUME_TAKER_FEE; "
                "ENTRY_AND_CURRENT_EXIT_SPREAD_ESTIMATE; "
                "ASSUMED_SLIPPAGE_5BPS_PER_SIDE; "
                "MARGIN_FEES_UNKNOWN; NO_HISTORICAL_L2"
            ),
        }
    )
    return outcome


def run_ohlcv_backtest_scenarios(
    raw_bars: Sequence[dict[str, Any]],
    *,
    pair: str,
    taker_fee_bps: Decimal | None,
    current_spread_bps: Decimal,
    holding_hours: int,
    fast_threshold_bps: Decimal,
    slow_threshold_bps: Decimal,
    stop_pct: Decimal,
    strategy_version: str = "tsm-0.1.2",
) -> dict[str, Any]:
    """Run the existing OHLCV simulator on chronological splits and margin-cost budgets.

    Rates in the scenario grid are total round-trip margin-fee budgets, not Kraken
    quotes. Current spread and taker-fee tier are proxies for historical costs.
    """
    from app.backtest import (
        BacktestCosts,
        Bar,
        chronological_split,
        simulate_trend_trades,
        summary_metrics,
    )

    base: dict[str, Any] = {
        "pair": pair,
        "status": "SCENARIO_ONLY_NOT_LIVE_VALIDATION",
        "actual_fills": False,
        "portfolio_pnl": False,
        "parameter_promotion": False,
        "strategy_version": strategy_version,
        "cost_source_quality": "CURRENT_FEE_AND_SPREAD_PROXIES;MARGIN_FEE_BUDGET_SENSITIVITY",
        "taker_fee_bps_per_side": str(taker_fee_bps) if taker_fee_bps is not None else None,
        "current_spread_bps_assumed_for_history": str(current_spread_bps),
        "slippage_bps_per_side_assumption": str(RESEARCH_SLIPPAGE_BPS_PER_SIDE),
        "margin_cost_budgets_are_exchange_quotes": False,
        "leverage_assumption": 2,
        "position_sizing_assumption_pct_of_equity": "10",
        "holding_hours": int(holding_hours),
        "fast_threshold_bps": str(fast_threshold_bps),
        "slow_threshold_bps": str(slow_threshold_bps),
        "stop_pct": str(stop_pct),
    }
    if taker_fee_bps is None:
        return {**base, "status": "SKIPPED_TAKER_FEE_UNKNOWN"}
    if (
        not taker_fee_bps.is_finite() or taker_fee_bps < 0
        or not current_spread_bps.is_finite() or current_spread_bps < 0
        or holding_hours < 1
    ):
        return {**base, "status": "SKIPPED_INVALID_COST_OR_HORIZON_INPUT"}
    if len(raw_bars) < 500:
        return {
            **base,
            "status": "INSUFFICIENT_CONTIGUOUS_HISTORY",
            "bar_count": len(raw_bars),
            "required_bar_count": 500,
        }

    bars = [
        Bar(
            timestamp=int(item["ts"]),
            open=D(str(item["open"])),
            high=D(str(item["high"])),
            low=D(str(item["low"])),
            close=D(str(item["close"])),
            base_volume=D(str(item["volume"])),
        )
        for item in raw_bars
    ]
    if any(bars[index].timestamp >= bars[index + 1].timestamp for index in range(len(bars) - 1)):
        return {**base, "status": "INVALID_BAR_ORDER", "bar_count": len(bars)}
    gap_count = sum(
        1 for index in range(len(bars) - 1)
        if bars[index + 1].timestamp - bars[index].timestamp != 3600
    )
    if gap_count:
        return {
            **base,
            "status": "INCOMPLETE_HOURLY_BAR_GAPS",
            "bar_count": len(bars),
            "hourly_gap_count": gap_count,
        }
    try:
        train_bars, validation_bars, test_bars = chronological_split(bars)
    except ValueError as exc:
        return {**base, "status": "CHRONOLOGICAL_SPLIT_FAILED", "reason": str(exc)}

    # The OHLCV simulator enters at the bar after the signal and exits after
    # max_holding_bars counted from that entry bar.
    rollover_count = max(0, int(holding_hours) // 4)
    event_count = 1 + rollover_count
    scenario_results: list[dict[str, Any]] = []
    for total_margin_budget_bps in (D("5"), D("10"), D("20"), D("40")):
        per_fee_event = total_margin_budget_bps / D(event_count)
        costs = BacktestCosts(
            taker_fee_bps_per_side=taker_fee_bps,
            spread_bps_round_trip=current_spread_bps,
            slippage_bps_per_side=RESEARCH_SLIPPAGE_BPS_PER_SIDE,
            margin_open_bps_on_extended_amount=per_fee_event,
            margin_rollover_bps_on_extended_amount=per_fee_event,
            holding_hours=int(holding_hours),
            leverage=2,
            max_volume_participation=D("0.01"),
            minimum_order_usd=D("5"),
        )
        split_results: dict[str, Any] = {}
        for split_name, split_bars in (
            ("train_first_60_percent", train_bars),
            ("validation_next_20_percent", validation_bars),
            ("test_last_20_percent", test_bars),
        ):
            trades = simulate_trend_trades(
                split_bars,
                costs,
                equity_usd=D("10000"),
                position_pct=D("10"),
                fast_threshold_bps=fast_threshold_bps,
                slow_threshold_bps=slow_threshold_bps,
                max_holding_bars=int(holding_hours),
                stop_pct=stop_pct,
            )
            split_results[split_name] = {
                **summary_metrics(trades),
                "start_bar_ts": split_bars[0].timestamp if split_bars else None,
                "end_bar_ts": split_bars[-1].timestamp if split_bars else None,
            }
        scenario_results.append({
            "assumed_combined_margin_fee_budget_bps": str(total_margin_budget_bps),
            "assumed_opening_and_each_rollover_fee_bps": str(per_fee_event),
            "assumed_rollover_count_for_full_holding_period": rollover_count,
            "splits": split_results,
            "status": "SENSITIVITY_ASSUMPTION_NOT_KRAKEN_QUOTE",
        })

    return {
        **base,
        "status": "SCENARIO_ONLY_NOT_LIVE_VALIDATION",
        "bar_count": len(bars),
        "first_bar_ts": bars[0].timestamp,
        "last_bar_ts": bars[-1].timestamp,
        "chronological_split_fractions": {"train": "60%", "validation": "20%", "test": "20%"},
        "historical_spread_source": "CURRENT_TICKER_PROXY_NOT_HISTORICAL_SPREAD",
        "historical_taker_fee_source": "CURRENT_TRADE_VOLUME_TIER_NOT_HISTORICAL_TIER",
        "simulator_method": "OHLCV_NEXT_BAR_OPEN_VOLUME_PARTICIPATION_PROXY",
        "scenario_results": scenario_results,
        "limitations": [
            "Uses current spread and current account fee tier as proxies for historical costs.",
            "Margin fee budgets are sensitivity assumptions and not dynamic Kraken rates.",
            "Uses 2x leverage and 10% position sizing rather than the live risk sizing calculation.",
            "Does not model historical order-book depth, queue position, actual fills, cancellations, latency or per-order margin eligibility.",
            "Results are research scenarios, not actual orders, fills, portfolio PnL or proof of profitability.",
        ],
    }


def _metrics_for_field(
    rows: Sequence[dict[str, Any]],
    field: str,
    return_kind: str,
) -> dict[str, Any]:
    returns: list[Decimal] = []
    for row in rows:
        raw = row.get(field)
        if raw is None:
            continue
        try:
            value = D(str(raw))
        except (ArithmeticError, TypeError, ValueError):
            continue
        if value.is_finite():
            returns.append(value)
    if not returns:
        return {
            "sample_count": 0,
            f"mean_{return_kind}_return_bps": None,
            f"median_{return_kind}_return_bps": None,
            "win_rate_pct": None,
            "profit_factor": None,
            f"worst_{return_kind}_return_bps": None,
        }

    wins = sum((value for value in returns if value > 0), D(0))
    losses = -sum((value for value in returns if value < 0), D(0))
    if losses:
        factor: str | None = str(wins / losses)
    elif wins:
        factor = "NO_LOSSES_IN_SAMPLE"
    else:
        factor = "ZERO_GROSS_WIN_AND_LOSS"
    return {
        "sample_count": len(returns),
        f"mean_{return_kind}_return_bps": str(sum(returns, D(0)) / D(len(returns))),
        f"median_{return_kind}_return_bps": str(D(str(median(returns)))),
        "win_rate_pct": str(D(sum(1 for value in returns if value > 0)) / D(len(returns)) * D(100)),
        "profit_factor": factor,
        f"worst_{return_kind}_return_bps": str(min(returns)),
    }


def _metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Backward-compatible metric keys for the costed spot proxy series."""
    return _metrics_for_field(rows, "spot_net_return_bps", "net")


def _forward_timing_diagnostics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Quantify the unavoidable timing error when labels use hourly candle closes."""
    overshoots: list[int] = []
    realized_horizons: list[int] = []
    invalid_timestamps = 0
    for row in rows:
        if row.get("outcome_bar_ts") is None:
            continue
        try:
            decision_ts = int(row["decision_ts"])
            target_epoch = int(row["target_epoch"])
            outcome_bar_ts = int(row["outcome_bar_ts"])
            interval_seconds = int(row.get("interval_minutes", 60)) * 60
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid_timestamps += 1
            continue
        if interval_seconds <= 0:
            invalid_timestamps += 1
            continue
        bar_close_epoch = outcome_bar_ts + interval_seconds
        overshoot = bar_close_epoch - target_epoch
        actual_horizon = bar_close_epoch - decision_ts
        if overshoot < 0 or overshoot > interval_seconds or actual_horizon <= 0:
            invalid_timestamps += 1
            continue
        overshoots.append(overshoot)
        realized_horizons.append(actual_horizon)
    return {
        "sample_count": len(overshoots),
        "invalid_timestamp_count": invalid_timestamps,
        "target_horizon_overshoot_seconds": {
            "mean": str(D(sum(overshoots)) / D(len(overshoots))) if overshoots else None,
            "median": str(D(str(median(overshoots)))) if overshoots else None,
            "maximum": max(overshoots) if overshoots else None,
        },
        "actual_horizon_seconds": {
            "mean": str(D(sum(realized_horizons)) / D(len(realized_horizons)))
            if realized_horizons else None,
            "minimum": min(realized_horizons) if realized_horizons else None,
            "maximum": max(realized_horizons) if realized_horizons else None,
        },
        "status": "HOURLY_CLOSE_LABEL_NOT_EXACT_HORIZON",
    }


def _target_open_timing_diagnostics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Measure gross labels at a stored hourly bar open at or before the target epoch."""
    lead_seconds: list[int] = []
    actual_horizons: list[int] = []
    invalid_timestamps = 0
    for row in rows:
        if row.get("target_open_reference_ts") is None:
            continue
        try:
            decision_ts = int(row["decision_ts"])
            target_epoch = int(row["target_epoch"])
            reference_ts = int(row["target_open_reference_ts"])
            interval_seconds = int(row.get("interval_minutes", 60)) * 60
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid_timestamps += 1
            continue
        lead = target_epoch - reference_ts
        actual_horizon = reference_ts - decision_ts
        if interval_seconds <= 0 or lead < 0 or lead >= interval_seconds or actual_horizon <= 0:
            invalid_timestamps += 1
            continue
        lead_seconds.append(lead)
        actual_horizons.append(actual_horizon)
    return {
        "sample_count": len(lead_seconds),
        "invalid_timestamp_count": invalid_timestamps,
        "target_reference_lead_seconds": {
            "mean": str(D(sum(lead_seconds)) / D(len(lead_seconds))) if lead_seconds else None,
            "median": str(D(str(median(lead_seconds)))) if lead_seconds else None,
            "maximum": max(lead_seconds) if lead_seconds else None,
        },
        "actual_horizon_seconds": {
            "mean": str(D(sum(actual_horizons)) / D(len(actual_horizons)))
            if actual_horizons else None,
            "minimum": min(actual_horizons) if actual_horizons else None,
            "maximum": max(actual_horizons) if actual_horizons else None,
        },
        "status": "HOURLY_BAR_OPEN_AT_OR_BEFORE_TARGET_NOT_EXACT_HORIZON",
    }


def _feature_metrics(
    rows: Sequence[dict[str, Any]],
    field: str,
) -> dict[str, Any]:
    values: list[Decimal] = []
    for row in rows:
        raw = row.get(field)
        if raw is None:
            continue
        try:
            value = D(str(raw))
        except (ArithmeticError, TypeError, ValueError):
            continue
        if value.is_finite() and D(0) <= value <= D(1):
            values.append(value)
    return {
        "sample_count": len(values),
        "mean": str(sum(values, D(0)) / D(len(values))) if values else None,
        "median": str(D(str(median(values)))) if values else None,
    }


def summarize_shadow_research(
    observations: Sequence[dict[str, Any]],
    *,
    strategy_version: str | None = None,
) -> dict[str, Any]:
    """Summarize one strategy version only; never blend outcomes across rule changes."""
    active_version = strategy_version
    if active_version is None and observations:
        latest = max(
            observations,
            key=lambda row: (
                int(row.get("decision_ts", 0)),
                str(row.get("observation_id", "")),
            ),
        )
        active_version = str(latest.get("strategy_version", "unknown"))
    version_rows = [
        row for row in observations
        if active_version is None
        or str(row.get("strategy_version", "unknown")) == active_version
    ]
    ordered = sorted(
        version_rows,
        key=lambda row: (int(row.get("decision_ts", 0)), str(row.get("observation_id", ""))),
    )
    directional = [row for row in ordered if row.get("direction") in {"LONG", "SHORT"}]
    settled = [
        row for row in directional
        if row.get("status") in {"SETTLED_COST_ESTIMATE", "SETTLED_GROSS_ONLY"}
    ]
    costed = [
        row for row in settled
        if row.get("spot_net_return_bps") is not None
    ]
    margin_break_even = [
        D(str(row["margin_cost_break_even_bps"]))
        for row in costed
        if row.get("margin_cost_break_even_bps") is not None
        and D(str(row["margin_cost_break_even_bps"])).is_finite()
    ]
    n = len(costed)
    gross_settled = [
        row for row in settled
        if row.get("gross_return_bps") is not None
    ]
    target_open_settled = [
        row for row in settled if row.get("target_open_gross_return_bps") is not None
    ]
    target_open_times = sorted({
        int(row.get("decision_ts", 0)) for row in target_open_settled
    })
    target_open_train_end = int(len(target_open_times) * 0.60)
    target_open_validation_end = int(len(target_open_times) * 0.80)
    target_open_train_times = set(target_open_times[:target_open_train_end])
    target_open_validation_times = set(
        target_open_times[target_open_train_end:target_open_validation_end]
    )
    target_open_test_times = set(target_open_times[target_open_validation_end:])
    target_open_train = [
        row for row in target_open_settled
        if int(row.get("decision_ts", 0)) in target_open_train_times
    ]
    target_open_validation = [
        row for row in target_open_settled
        if int(row.get("decision_ts", 0)) in target_open_validation_times
    ]
    target_open_test = [
        row for row in target_open_settled
        if int(row.get("decision_ts", 0)) in target_open_test_times
    ]
    gross_times = sorted({int(row.get("decision_ts", 0)) for row in gross_settled})
    gross_train_end = int(len(gross_times) * 0.60)
    gross_validation_end = int(len(gross_times) * 0.80)
    gross_train_times = set(gross_times[:gross_train_end])
    gross_validation_times = set(gross_times[gross_train_end:gross_validation_end])
    gross_test_times = set(gross_times[gross_validation_end:])
    gross_train = [
        row for row in gross_settled if int(row.get("decision_ts", 0)) in gross_train_times
    ]
    gross_validation = [
        row for row in gross_settled
        if int(row.get("decision_ts", 0)) in gross_validation_times
    ]
    gross_test = [
        row for row in gross_settled if int(row.get("decision_ts", 0)) in gross_test_times
    ]
    # Split by unique decision timestamps so observations from the same market
    # cycle cannot leak across train, validation and test partitions.
    unique_decision_times = sorted({int(row.get("decision_ts", 0)) for row in costed})
    train_time_end = int(len(unique_decision_times) * 0.60)
    validation_time_end = int(len(unique_decision_times) * 0.80)
    train_times = set(unique_decision_times[:train_time_end])
    validation_times = set(unique_decision_times[train_time_end:validation_time_end])
    test_times = set(unique_decision_times[validation_time_end:])
    train = [row for row in costed if int(row.get("decision_ts", 0)) in train_times]
    validation = [row for row in costed if int(row.get("decision_ts", 0)) in validation_times]
    test = [row for row in costed if int(row.get("decision_ts", 0)) in test_times]
    holdout_ready = (
        n >= MINIMUM_COSTED_FORWARD_OBSERVATIONS
        and len(test) >= MINIMUM_HOLDOUT_OBSERVATIONS_FOR_REVIEW
    )
    no_direction = [row for row in ordered if row.get("direction") == "NONE"]
    incomplete = sum(
        1 for row in directional if str(row.get("status", "")).startswith("INCOMPLETE")
    )
    no_fee_settled = sum(1 for row in settled if row.get("spot_net_return_bps") is None)
    efficiency_filter_rejections = sum(
        1 for row in no_direction
        if str(row.get("signal_reason", "")) == "TREND_EFFICIENCY_TOO_LOW"
    )
    margin_fee_sensitivity = []
    for assumed_combined_margin_fee_bps in (D("5"), D("10"), D("20"), D("40")):
        positive_after_margin_cost = sum(
            1 for value in margin_break_even if value > assumed_combined_margin_fee_bps
        )
        average_net_proxy = (
            sum(
                (value - assumed_combined_margin_fee_bps for value in margin_break_even),
                D(0),
            ) / D(len(margin_break_even))
            if margin_break_even else None
        )
        margin_fee_sensitivity.append({
            "assumed_combined_margin_fee_budget_bps": str(assumed_combined_margin_fee_bps),
            "positive_sample_count": positive_after_margin_cost,
            "sample_count": len(margin_break_even),
            "positive_pct": (
                str(D(positive_after_margin_cost) / D(len(margin_break_even)) * D(100))
                if margin_break_even else None
            ),
            "mean_net_proxy_after_assumed_margin_fee_bps": (
                str(average_net_proxy) if average_net_proxy is not None else None
            ),
            "status": "SENSITIVITY_ASSUMPTION_NOT_KRAKEN_QUOTE",
        })

    return {
        "research_schema": "shadow-forward-v2",
        "strategy_version": (
            active_version
            if active_version is not None
            else str(ordered[-1].get("strategy_version", "unknown")) if ordered else "unknown"
        ),
        "total_observations": len(ordered),
        "directional_observations": len(directional),
        "long_observations": sum(1 for row in directional if row.get("direction") == "LONG"),
        "short_observations": sum(1 for row in directional if row.get("direction") == "SHORT"),
        "settled_directional_observations": len(settled),
        "costed_spot_proxy_observations": n,
        "pending_directional_observations": sum(1 for row in directional if row.get("status") == "PENDING"),
        "incomplete_directional_observations": incomplete,
        "settled_without_taker_fee": no_fee_settled,
        "spot_cost_proxy_all": _metrics(costed),
        "spot_cost_proxy_by_direction": {
            direction: _metrics([row for row in costed if row.get("direction") == direction])
            for direction in ("LONG", "SHORT")
        },
        "gross_forward_proxy_all": _metrics_for_field(
            gross_settled, "gross_return_bps", "gross"
        ),
        "gross_forward_proxy_by_direction": {
            direction: _metrics_for_field(
                [row for row in gross_settled if row.get("direction") == direction],
                "gross_return_bps",
                "gross",
            )
            for direction in ("LONG", "SHORT")
        },
        "gross_forward_chronological_splits": {
            "train_first_60_percent": _metrics_for_field(
                gross_train, "gross_return_bps", "gross"
            ),
            "validation_next_20_percent": _metrics_for_field(
                gross_validation, "gross_return_bps", "gross"
            ),
            "test_last_20_percent": _metrics_for_field(
                gross_test, "gross_return_bps", "gross"
            ),
        },
        "target_open_gross_proxy_all": _metrics_for_field(
            target_open_settled, "target_open_gross_return_bps", "gross"
        ),
        "target_open_gross_proxy_by_direction": {
            direction: _metrics_for_field(
                [row for row in target_open_settled if row.get("direction") == direction],
                "target_open_gross_return_bps",
                "gross",
            )
            for direction in ("LONG", "SHORT")
        },
        "target_open_gross_chronological_splits": {
            "train_first_60_percent": _metrics_for_field(
                target_open_train, "target_open_gross_return_bps", "gross"
            ),
            "validation_next_20_percent": _metrics_for_field(
                target_open_validation, "target_open_gross_return_bps", "gross"
            ),
            "test_last_20_percent": _metrics_for_field(
                target_open_test, "target_open_gross_return_bps", "gross"
            ),
        },
        "target_open_timing_diagnostics": _target_open_timing_diagnostics(
            target_open_settled
        ),
        "forward_timing_diagnostics": _forward_timing_diagnostics(settled),
        "trend_quality_diagnostics": {
            "efficiency_filter_rejection_count": efficiency_filter_rejections,
            "directional_efficiency_24h": _feature_metrics(directional, "trend_efficiency_24h"),
            "directional_efficiency_72h": _feature_metrics(directional, "trend_efficiency_72h"),
            "no_direction_efficiency_24h": _feature_metrics(no_direction, "trend_efficiency_24h"),
            "no_direction_efficiency_72h": _feature_metrics(no_direction, "trend_efficiency_72h"),
            "legacy_missing_features_excluded": True,
        },
        "chronological_splits": {
            "train_first_60_percent": _metrics(train),
            "validation_next_20_percent": _metrics(validation),
            "test_last_20_percent": _metrics(test),
        },
        "margin_cost_diagnostics": {
            "actual_margin_fee_evidence_available": False,
            "break_even_observation_count": len(margin_break_even),
            "median_break_even_margin_cost_bps": (
                str(D(str(median(margin_break_even)))) if margin_break_even else None
            ),
            "positive_break_even_budget_count": sum(1 for value in margin_break_even if value > 0),
            "margin_net_return_calculated": False,
            "fee_budget_sensitivity": margin_fee_sensitivity,
            "status": "BREAK_EVEN_ONLY_MARGIN_OPEN_AND_ROLLOVER_FEES_UNKNOWN",
        },
        "holdout_sample_available_for_review": holdout_ready,
        "validation_status": (
            "HOLDOUT_AVAILABLE_REVIEW_REQUIRED"
            if holdout_ready
            else "INSUFFICIENT_FORWARD_SAMPLE"
        ),
        "release_gate_eligible": False,
        "portfolio_pnl": False,
        "overlapping_signal_horizons": True,
        "limitations": [
            "Forward signal labels overlap across time and are not independent trades.",
            "These labels do not simulate the runtime stop-loss, take-profit, partial-reduction, or exit-order state machine.",
            "Primary forward outcome uses the close of the stored hourly bar covering the target epoch; target-close overshoot is reported separately.",
            "A separate target-open gross series uses the open of the hourly bar strictly covering the target epoch. It is at or before target and avoids measuring an extra candle close beyond target; it is not an exact-horizon price.",
            "The target-open series is gross-only and does not reuse current spread or fee assumptions as a historical net return.",
            "Primary spot net proxy uses the current ticker spread when the target bar becomes available, not historical order-book evidence.",
            "Slippage is a fixed 5 bps per side research assumption.",
            "Margin opening and rollover fees are unknown; margin net returns are not computed.",
            "This report cannot calibrate edge or release live order execution automatically.",
        ],
    }


def backfill_target_open_outcomes(persistence: Any, *, limit: int = 5000) -> dict[str, Any]:
    """Add a gross-only target-open label without changing the primary stored outcome."""
    rows = persistence.research_observations_missing_target_open(limit=limit)
    updated = 0
    unavailable = 0
    invalid = 0
    offsets: list[int] = []
    for row in rows:
        try:
            pair = str(row["pair"])
            interval_minutes = int(row["interval_minutes"])
            target_epoch = int(row["target_epoch"])
            bar = persistence.research_bar_open_at_target_epoch(
                pair, interval_minutes, target_epoch
            )
            if bar is None:
                unavailable += 1
                continue
            direction = str(row["direction"]).upper()
            if direction not in {"LONG", "SHORT"}:
                invalid += 1
                continue
            entry = D(str(row["entry_mid_price"]))
            reference = D(str(bar["open"]))
            reference_ts = int(bar["ts"])
            if not entry.is_finite() or entry <= 0 or not reference.is_finite() or reference <= 0:
                invalid += 1
                continue
            sign = D(1) if direction == "LONG" else D(-1)
            gross = sign * (reference / entry - D(1)) * D(10000)
            if persistence.record_research_target_open_outcome(
                str(row["observation_id"]),
                reference_ts=reference_ts,
                reference_price=reference,
                gross_return_bps=gross,
            ):
                updated += 1
                offsets.append(target_epoch - reference_ts)
        except (ArithmeticError, KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
    return {
        "observations_checked": len(rows),
        "updated": updated,
        "target_bar_unavailable": unavailable,
        "invalid_observations": invalid,
        "target_reference_lead_seconds": {
            "mean": str(D(sum(offsets)) / D(len(offsets))) if offsets else None,
            "minimum": min(offsets) if offsets else None,
            "maximum": max(offsets) if offsets else None,
        },
        "outcome_model": "TARGET_HOURLY_BAR_OPEN_AT_OR_BEFORE_TARGET_GROSS_ONLY",
        "primary_outcomes_overwritten": False,
        "status": "BACKFILL_UPDATED" if updated else "NO_ELIGIBLE_BACKFILL_ROWS",
    }


def settle_due_observations(
    persistence: Any,
    *,
    pair: str,
    current_exit_spread_bps: Decimal,
    interval_minutes: int = 60,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Settle forward labels only against a stored candle covering the target time."""
    if not current_exit_spread_bps.is_finite() or current_exit_spread_bps < 0:
        return []
    results: list[dict[str, Any]] = []
    for row in persistence.pending_research_observations(pair, interval_minutes, limit):
        target_epoch = int(row["target_epoch"])
        target_bar = persistence.research_bar_covering_epoch(
            pair, interval_minutes, target_epoch
        )
        if target_bar is None:
            newest_bar_ts = persistence.latest_bar_timestamp(pair, interval_minutes)
            if newest_bar_ts is None or newest_bar_ts < target_epoch:
                continue
            outcome: dict[str, Any] = {
                "status": "INCOMPLETE_MISSING_TARGET_BAR",
                "outcome_bar_ts": None,
                "exit_close_price": None,
                "exit_spread_bps": str(current_exit_spread_bps),
                "gross_return_bps": None,
                "spot_cost_estimate_bps": None,
                "spot_net_return_bps": None,
                "margin_cost_break_even_bps": None,
                "cost_quality": "TARGET_HOURLY_BAR_MISSING;NO_OUTCOME_INFERRED",
            }
            if persistence.settle_research_observation(row["observation_id"], outcome):
                results.append({
                    "event": "SHADOW_RESEARCH_OUTCOME_INCOMPLETE",
                    "payload": {
                        "observation_id": row["observation_id"],
                        "pair": pair,
                        "direction": row["direction"],
                        "target_epoch": target_epoch,
                        "latest_bar_ts": newest_bar_ts,
                        "reason": "TARGET_HOURLY_BAR_MISSING",
                    },
                })
            continue

        fee = D(str(row["taker_fee_bps"])) if row.get("taker_fee_bps") is not None else None
        estimate = estimate_forward_outcome(
            direction=str(row["direction"]),
            entry_mid_price=D(str(row["entry_mid_price"])),
            exit_close_price=D(str(target_bar["close"])),
            entry_spread_bps=D(str(row["entry_spread_bps"])),
            exit_spread_bps=current_exit_spread_bps,
            taker_fee_bps=fee,
        )
        status = "SETTLED_COST_ESTIMATE" if fee is not None else "SETTLED_GROSS_ONLY"
        outcome = {
            "status": status,
            "outcome_bar_ts": int(target_bar["ts"]),
            "exit_close_price": str(target_bar["close"]),
            "exit_spread_bps": str(current_exit_spread_bps),
            "gross_return_bps": estimate["gross_return_bps"],
            "spot_cost_estimate_bps": estimate["spot_cost_estimate_bps"],
            "spot_net_return_bps": estimate["spot_net_return_bps"],
            "margin_cost_break_even_bps": estimate["margin_cost_break_even_bps"],
            "cost_quality": str(estimate["cost_quality"]),
        }
        if persistence.settle_research_observation(row["observation_id"], outcome):
            results.append({
                "event": "SHADOW_RESEARCH_OUTCOME_SETTLED",
                "payload": {
                    "observation_id": row["observation_id"],
                    "pair": pair,
                    "direction": row["direction"],
                    "decision_ts": int(row["decision_ts"]),
                    "target_epoch": target_epoch,
                    "outcome_bar_ts": int(target_bar["ts"]),
                    "gross_return_bps": str(estimate["gross_return_bps"]),
                    "spot_cost_estimate_bps": (
                        str(estimate["spot_cost_estimate_bps"])
                        if estimate["spot_cost_estimate_bps"] is not None else None
                    ),
                    "spot_net_return_bps": (
                        str(estimate["spot_net_return_bps"])
                        if estimate["spot_net_return_bps"] is not None else None
                    ),
                    "margin_cost_break_even_bps": (
                        str(estimate["margin_cost_break_even_bps"])
                        if estimate["margin_cost_break_even_bps"] is not None else None
                    ),
                    "status": status,
                    "cost_quality": estimate["cost_quality"],
                    "portfolio_pnl": False,
                    "actual_fill": False,
                },
            })
    return results
