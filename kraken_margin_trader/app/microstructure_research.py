from __future__ import annotations

from bisect import bisect_right
from decimal import Decimal
from statistics import median
from typing import Any, Mapping, Sequence

D = Decimal


def _d(value: Any) -> Decimal | None:
    try:
        parsed = D(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _metrics(values: Sequence[Decimal]) -> dict[str, Any]:
    if not values:
        return {"sample_count": 0, "mean_bps": None, "median_bps": None, "profit_factor": None}
    wins = sum((value for value in values if value > 0), D(0))
    losses = -sum((value for value in values if value < 0), D(0))
    return {
        "sample_count": len(values),
        "mean_bps": str(sum(values, D(0)) / D(len(values))),
        "median_bps": str(D(str(median(values)))),
        "profit_factor": str(wins / losses) if losses else (
            "NO_LOSSES_IN_SAMPLE" if wins else None
        ),
    }


def _mean(values: Sequence[Decimal]) -> str | None:
    return str(sum(values, D(0)) / D(len(values))) if values else None


def _mark(row: dict[str, Any]) -> tuple[int, Decimal, Decimal | None] | None:
    if str(row.get("quality_status", "")).upper() != "VALID":
        return None
    try:
        first = int(row.get("first_book_event_ts", 0))
        last = int(row.get("last_book_event_ts", 0))
        valid = (
            int(row.get("book_sample_count", 0)) >= 2
            and first > 0
            and last - first >= 10
            and int(row.get("checksum_checks", 0)) > 0
            and int(row.get("checksum_failures", 0)) == 0
            and int(row.get("trade_id_gaps", 0)) == 0
            and int(row.get("connection_gap_count", 0)) == 0
            and not row.get("quality_flags")
        )
    except (TypeError, ValueError, OverflowError):
        return None
    mid = _d(row.get("midprice_usd"))
    spread = _d(row.get("spread_bps"))
    if not valid or mid is None or mid <= 0 or (spread is not None and spread < 0):
        return None
    return last, mid, spread


def _cohort_coverage(
    observations: Sequence[dict[str, Any]],
    indexed: Mapping[str, tuple[list[int], list[tuple[int, Decimal, Decimal | None]]]],
    pair_aliases: Mapping[str, str],
    matched: Sequence[dict[str, Any]],
    *,
    tolerance_seconds: int,
) -> dict[str, Any]:
    """Separate pre-collection labels from matured, observable in-feed targets."""
    overall = {
        "directional_observation_count": 0,
        "decision_before_first_qualified_mark": 0,
        "decision_in_feed_count": 0,
        "target_not_yet_matured_count": 0,
        "matured_in_feed_count": 0,
        "matured_matched_count": 0,
        "matured_unmatched_count": 0,
        "matured_match_coverage_pct": None,
    }
    by_pair: dict[str, dict[str, int]] = {}
    by_candidate: dict[str, dict[str, int]] = {}
    matched_ids = {str(row.get("candidate_id", "")) for row in matched}

    def bucket(target: dict[str, int], key: str) -> None:
        target[key] = target.get(key, 0) + 1

    for row in observations:
        if str(row.get("direction", "")).upper() not in {"LONG", "SHORT"}:
            continue
        overall["directional_observation_count"] += 1
        source_pair = str(row.get("pair", "")).strip()
        pair = str(pair_aliases.get(source_pair, source_pair)).strip().upper()
        candidate = f"{row.get('candidate_name', 'unknown')}:{row.get('candidate_version', 'unknown')}"
        pair_stats = by_pair.setdefault(pair, {})
        candidate_stats = by_candidate.setdefault(candidate, {})
        for stats in (pair_stats, candidate_stats):
            bucket(stats, "directional_observation_count")
        if pair not in indexed or not indexed[pair][0]:
            bucket(pair_stats, "no_qualified_history_count")
            bucket(candidate_stats, "no_qualified_history_count")
            continue
        timestamps = indexed[pair][0]
        first_mark, latest_mark = timestamps[0], timestamps[-1]
        try:
            decision = int(row["decision_ts"])
            target = int(row["target_epoch"])
        except (KeyError, TypeError, ValueError, OverflowError):
            bucket(pair_stats, "invalid_timestamp_count")
            bucket(candidate_stats, "invalid_timestamp_count")
            continue
        if decision < first_mark:
            overall["decision_before_first_qualified_mark"] += 1
            bucket(pair_stats, "decision_before_first_qualified_mark")
            bucket(candidate_stats, "decision_before_first_qualified_mark")
            continue
        overall["decision_in_feed_count"] += 1
        bucket(pair_stats, "decision_in_feed_count")
        bucket(candidate_stats, "decision_in_feed_count")
        if target > latest_mark + tolerance_seconds:
            overall["target_not_yet_matured_count"] += 1
            bucket(pair_stats, "target_not_yet_matured_count")
            bucket(candidate_stats, "target_not_yet_matured_count")
            continue
        overall["matured_in_feed_count"] += 1
        bucket(pair_stats, "matured_in_feed_count")
        bucket(candidate_stats, "matured_in_feed_count")
        if str(row.get("candidate_id", "")) in matched_ids:
            overall["matured_matched_count"] += 1
            bucket(pair_stats, "matured_matched_count")
            bucket(candidate_stats, "matured_matched_count")
        else:
            overall["matured_unmatched_count"] += 1
            bucket(pair_stats, "matured_unmatched_count")
            bucket(candidate_stats, "matured_unmatched_count")

    matured = overall["matured_in_feed_count"]
    overall["matured_match_coverage_pct"] = (
        str(D(overall["matured_matched_count"]) / D(matured) * D(100))
        if matured else None
    )
    for stats in [*by_pair.values(), *by_candidate.values()]:
        denominator = stats.get("matured_in_feed_count", 0)
        stats["matured_match_coverage_pct"] = (
            str(D(stats.get("matured_matched_count", 0)) / D(denominator) * D(100))
            if denominator else None
        )
    return {
        "method": "DECISION_AT_OR_AFTER_FIRST_QUALIFIED_MARK_AND_TARGET_MATURED_BY_LATEST_MARK",
        "maturity_tolerance_seconds": tolerance_seconds,
        "overall": overall,
        "by_pair": by_pair,
        "by_candidate": by_candidate,
        "pre_history_observations_excluded_from_in_feed_denominator": True,
        "unmatured_targets_excluded_from_matured_coverage_denominator": True,
    }


def match_microstructure_target_horizons(
    observations: Sequence[dict[str, Any]],
    bars_by_market_pair: Mapping[str, Sequence[dict[str, Any]]],
    pair_aliases: dict[str, str],
    *,
    max_time_error_seconds: int = 60,
    slippage_bps_per_side: Decimal = D("5"),
) -> dict[str, Any]:
    """Match labels to qualified minute midpoints; this is not an executable fill."""
    if (
        max_time_error_seconds < 0
        or not slippage_bps_per_side.is_finite()
        or slippage_bps_per_side < 0
    ):
        raise ValueError("MICROSTRUCTURE_TARGET_INPUT_INVALID")
    indexed: dict[str, tuple[list[int], list[tuple[int, Decimal, Decimal | None]]]] = {}
    for pair, bars in bars_by_market_pair.items():
        marks = sorted((mark for bar in bars if (mark := _mark(bar)) is not None), key=lambda x: x[0])
        indexed[pair.strip().upper()] = ([mark[0] for mark in marks], marks)

    matched: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    gross: list[Decimal] = []
    net: list[Decimal] = []
    offsets: list[int] = []
    cost_components: dict[str, list[Decimal]] = {
        "round_trip_taker_fee_bps": [],
        "entry_exit_spread_bps": [],
        "round_trip_slippage_bps": [],
        "total_estimated_spot_cost_bps": [],
    }
    directional = costed = 0

    def unmatched(reason: str) -> None:
        reasons[reason] = reasons.get(reason, 0) + 1

    for row in observations:
        direction = str(row.get("direction", "")).upper()
        if direction not in {"LONG", "SHORT"}:
            continue
        directional += 1
        source_pair = str(row.get("pair", "")).strip()
        pair = str(pair_aliases.get(source_pair, source_pair)).strip().upper()
        try:
            decision, target = int(row["decision_ts"]), int(row["target_epoch"])
        except (KeyError, TypeError, ValueError, OverflowError):
            unmatched("INVALID_TARGET_TIMESTAMP")
            continue
        entry = _d(row.get("entry_mid_price"))
        if decision <= 0 or target <= decision or entry is None or entry <= 0:
            unmatched("INVALID_ENTRY_OR_TARGET")
            continue
        if pair not in indexed:
            unmatched("NO_MICROSTRUCTURE_HISTORY_FOR_PAIR")
            continue
        timestamps, marks = indexed[pair]
        if not timestamps:
            unmatched("NO_QUALIFIED_VALID_BOOK_MARKS")
            continue
        position = bisect_right(timestamps, target) - 1
        if position < 0:
            unmatched("TARGET_PRECEDES_FIRST_QUALIFIED_BOOK_MARK")
            continue
        mark_ts, exit_mid, exit_spread = marks[position]
        if mark_ts < decision:
            unmatched("NO_POST_DECISION_BOOK_MARK_BEFORE_TARGET")
            continue
        offset = mark_ts - target
        if abs(offset) > max_time_error_seconds:
            unmatched("BOOK_MARK_OUTSIDE_TARGET_TOLERANCE")
            continue

        sign = D(1) if direction == "LONG" else D(-1)
        gross_return = sign * (exit_mid / entry - D(1)) * D(10000)
        fee = _d(row.get("taker_fee_bps"))
        entry_spread = _d(row.get("entry_spread_bps"))
        net_return = None
        components: dict[str, str | None] = {
            "round_trip_taker_fee_bps": None,
            "entry_exit_spread_bps": None,
            "round_trip_slippage_bps": None,
            "total_estimated_spot_cost_bps": None,
        }
        if (
            fee is not None and fee >= 0
            and entry_spread is not None and entry_spread >= 0
            and exit_spread is not None and exit_spread >= 0
        ):
            fee_cost = D(2) * fee
            spread_cost = (entry_spread + exit_spread) / D(2)
            slippage_cost = D(2) * slippage_bps_per_side
            total_cost = fee_cost + spread_cost + slippage_cost
            net_return = gross_return - total_cost
            components = {
                "round_trip_taker_fee_bps": str(fee_cost),
                "entry_exit_spread_bps": str(spread_cost),
                "round_trip_slippage_bps": str(slippage_cost),
                "total_estimated_spot_cost_bps": str(total_cost),
            }
            for name, value in components.items():
                cost_components[name].append(D(str(value)))
            net.append(net_return)
            costed += 1

        gross.append(gross_return)
        offsets.append(offset)
        matched.append({
            "candidate_id": str(row.get("candidate_id", row.get("observation_id", ""))),
            "candidate_name": str(row.get("candidate_name", row.get("strategy_version", "unknown"))),
            "candidate_version": str(row.get("candidate_version", row.get("strategy_version", "unknown"))),
            "pair": pair,
            "direction": direction,
            "decision_ts": decision,
            "target_epoch": target,
            "mark_event_ts": mark_ts,
            "target_time_offset_seconds": offset,
            "entry_mid_price": str(entry),
            "exit_midprice": str(exit_mid),
            "entry_spread_bps": str(entry_spread) if entry_spread is not None else None,
            "exit_spread_bps": str(exit_spread) if exit_spread is not None else None,
            "taker_fee_bps": str(fee) if fee is not None else None,
            "gross_return_bps": str(gross_return),
            "estimated_spot_cost_bps": str(gross_return - net_return) if net_return is not None else None,
            "cost_breakdown_bps": components,
            "spot_net_return_bps": str(net_return) if net_return is not None else None,
            "actual_fill": False,
            "portfolio_pnl": False,
        })

    groups: dict[str, dict[str, list[Decimal]]] = {}
    for row in matched:
        key = f"{row['candidate_name']}:{row['candidate_version']}"
        group = groups.setdefault(key, {"gross": [], "net": [], **{
            name: [] for name in cost_components
        }})
        group["gross"].append(D(str(row["gross_return_bps"])))
        if row["spot_net_return_bps"] is not None:
            group["net"].append(D(str(row["spot_net_return_bps"])))
            for name, value in row["cost_breakdown_bps"].items():
                if value is not None:
                    group[name].append(D(str(value)))

    absolute_offsets = [abs(value) for value in offsets]
    history_coverage_by_pair = {
        pair: {
            "qualified_book_mark_count": len(timestamps),
            "first_qualified_book_event_ts": timestamps[0] if timestamps else None,
            "latest_qualified_book_event_ts": timestamps[-1] if timestamps else None,
        }
        for pair, (timestamps, _marks) in indexed.items()
    }
    return {
        "status": "MINUTE_BOOK_MARKS_AVAILABLE" if matched else "WAITING_FOR_QUALIFIED_MINUTE_BOOK_MARKS",
        "method": "LAST_VALID_CHECKSUM_BOOK_MARK_AT_OR_BEFORE_TARGET",
        "target_tolerance_seconds": max_time_error_seconds,
        "slippage_bps_per_side_assumption": str(slippage_bps_per_side),
        "directional_observation_count": directional,
        "matched_observation_count": len(matched),
        "unmatched_observation_count": directional - len(matched),
        "match_coverage_pct": str(D(len(matched)) / D(directional) * D(100)) if directional else None,
        "gross_mark_return": _metrics(gross),
        "spot_net_cost_proxy": _metrics(net),
        "costed_match_count": costed,
        "cost_breakdown_bps": {
            name: {"sample_count": len(values), "mean_bps": _mean(values)}
            for name, values in cost_components.items()
        },
        "target_time_error_seconds": {
            "mean_absolute": str(D(sum(absolute_offsets)) / D(len(absolute_offsets))) if absolute_offsets else None,
            "median_absolute": str(D(str(median(absolute_offsets)))) if absolute_offsets else None,
            "maximum_absolute": max(absolute_offsets) if absolute_offsets else None,
            "mean_signed": str(D(sum(offsets)) / D(len(offsets))) if offsets else None,
        },
        "by_candidate": {
            name: {
                "gross": _metrics(values["gross"]),
                "spot_net_cost_proxy": _metrics(values["net"]),
                "cost_breakdown_bps": {
                    component: {
                        "sample_count": len(values[component]),
                        "mean_bps": _mean(values[component]),
                    }
                    for component in cost_components
                },
            }
            for name, values in groups.items()
        },
        "unmatched_reasons": reasons,
        "history_coverage_by_pair": history_coverage_by_pair,
        "cohort_coverage": _cohort_coverage(
            observations, indexed, pair_aliases, matched,
            tolerance_seconds=max_time_error_seconds,
        ),
        "matched_observations": matched,
        "actual_fills": False,
        "historical_order_book_walk_simulated": False,
        "portfolio_pnl": False,
        "release_gate_eligible": False,
        "limitations": [
            "Midpoint is a mark, not an executable fill; event-time book walk/queue priority is unavailable.",
            "Only a qualified book mark at or after the decision and at or before the target is eligible.",
            "Pre-collection decisions and targets not yet matured are excluded from the in-feed matured coverage denominator.",
            "A target before the first stored qualified book mark is outside the locally collected WS history; Kraken WS L2 history is not backfilled.",
            "Fee and spread components use observed inputs; slippage remains a fixed per-side assumption, not a measured fill cost.",
            "Spot costs are proxies; margin open, rollover, borrow and funding costs remain unknown and omitted.",
        ],
    }
