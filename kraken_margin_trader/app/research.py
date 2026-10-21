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


def _metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    returns = [
        D(str(row["spot_net_return_bps"]))
        for row in rows
        if row.get("spot_net_return_bps") is not None
        and D(str(row["spot_net_return_bps"])).is_finite()
    ]
    if not returns:
        return {
            "sample_count": 0,
            "mean_net_return_bps": None,
            "median_net_return_bps": None,
            "win_rate_pct": None,
            "profit_factor": None,
            "worst_net_return_bps": None,
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
        "mean_net_return_bps": str(sum(returns, D(0)) / D(len(returns))),
        "median_net_return_bps": str(D(str(median(returns)))),
        "win_rate_pct": str(D(sum(1 for value in returns if value > 0)) / D(len(returns)) * D(100)),
        "profit_factor": factor,
        "worst_net_return_bps": str(min(returns)),
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
    incomplete = sum(
        1 for row in directional if str(row.get("status", "")).startswith("INCOMPLETE")
    )
    no_fee_settled = sum(1 for row in settled if row.get("spot_net_return_bps") is None)
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
        "research_schema": "shadow-forward-v1",
        "strategy_version": str(ordered[-1].get("strategy_version", "unknown")) if ordered else "unknown",
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
            "Entry is ticker mid; outcomes use the nearest closed hourly bar at the target horizon.",
            "Exit spread is the current ticker spread when the target bar becomes available, not historical order-book evidence.",
            "Slippage is a fixed 5 bps per side research assumption.",
            "Margin opening and rollover fees are unknown; margin net returns are not computed.",
            "This report cannot calibrate edge or release live order execution automatically.",
        ],
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
