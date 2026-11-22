from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from statistics import median
from typing import Any, Sequence

D = Decimal
_PRIOR_ALPHA = D("5")
_PRIOR_BETA = D("5")
_MINIMUM_REVIEW_SAMPLES = 30


def _integer(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = D(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def summarize_experience_calibration(
    observations: Sequence[dict[str, Any]],
    *,
    as_of_epoch: int,
) -> dict[str, Any]:
    """Summarize matured, non-overlapping shadow outcomes with neutral Beta shrinkage.

    This is an experience diagnostic, not a calibrated forecast probability, model
    promotion rule, executed-trade return, or portfolio-PnL calculation.
    """
    eligible_by_pair: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    excluded_unmatured = 0
    invalid_outcomes = 0

    for row in observations:
        if str(row.get("status", "")) != "SETTLED_COST_ESTIMATE":
            excluded_unmatured += 1
            continue
        decision_ts = _integer(row.get("decision_ts"))
        target_epoch = _integer(row.get("target_epoch"))
        if (
            decision_ts is None
            or target_epoch is None
            or target_epoch <= decision_ts
            or target_epoch >= as_of_epoch
        ):
            excluded_unmatured += 1
            continue
        if str(row.get("direction", "")).upper() not in {"LONG", "SHORT"}:
            excluded_unmatured += 1
            continue
        net_bps = _decimal(row.get("spot_net_return_bps"))
        if net_bps is None:
            invalid_outcomes += 1
            continue

        candidate_name = str(row.get("candidate_name", "unknown"))
        candidate_version = str(row.get("candidate_version", "unknown"))
        direction = str(row.get("direction", "")).upper()
        pair = str(row.get("pair", "unknown"))
        features = row.get("features")
        regime = (
            str(features.get("market_regime", "UNKNOWN")).upper()
            if isinstance(features, dict) else "UNKNOWN"
        )
        if regime not in {"TRENDING", "RANGING", "TRANSITIONAL", "UNKNOWN"}:
            regime = "UNKNOWN"
        key = (candidate_name, candidate_version, direction, pair)
        eligible_by_pair[key].append({
            "candidate_name": candidate_name,
            "candidate_version": candidate_version,
            "direction": direction,
            "pair": pair,
            "market_regime": regime,
            "decision_ts": decision_ts,
            "target_epoch": target_epoch,
            "candidate_id": str(row.get("candidate_id", "")),
            "net_bps": net_bps,
        })

    non_overlapping: list[dict[str, Any]] = []
    overlap_excluded = 0
    for rows in eligible_by_pair.values():
        last_target_epoch = -1
        ordered = sorted(
            rows,
            key=lambda item: (
                int(item["decision_ts"]),
                int(item["target_epoch"]),
                str(item["candidate_id"]),
            ),
        )
        for row in ordered:
            if int(row["decision_ts"]) < last_target_epoch:
                overlap_excluded += 1
                continue
            non_overlapping.append(row)
            last_target_epoch = int(row["target_epoch"])

    grouped: dict[tuple[str, str, str, str, str], list[Decimal]] = defaultdict(list)
    for row in non_overlapping:
        grouped[(
            str(row["candidate_name"]),
            str(row["candidate_version"]),
            str(row["direction"]),
            str(row["pair"]),
            str(row["market_regime"]),
        )].append(row["net_bps"])

    summaries: list[dict[str, Any]] = []
    for (name, version, direction, pair, regime), returns in sorted(grouped.items()):
        positive = sum(1 for value in returns if value > 0)
        negative = sum(1 for value in returns if value < 0)
        flat = len(returns) - positive - negative
        count = len(returns)
        raw_rate = D(positive) / D(count)
        smoothed_rate = (
            _PRIOR_ALPHA + D(positive)
        ) / (_PRIOR_ALPHA + _PRIOR_BETA + D(count))
        mean_net = sum(returns, D(0)) / D(count)
        median_net = median(returns)
        summaries.append({
            "candidate_name": name,
            "candidate_version": version,
            "direction": direction,
            "pair": pair,
            "market_regime": regime,
            "independent_same_pair_non_overlapping_sample_count": count,
            "positive_net_outcomes": positive,
            "negative_net_outcomes": negative,
            "flat_net_outcomes": flat,
            "raw_positive_rate_pct": str(raw_rate * D(100)),
            "beta_shrunk_positive_rate_estimate_pct": str(smoothed_rate * D(100)),
            "mean_net_return_proxy_bps": str(mean_net),
            "median_net_return_proxy_bps": str(median_net),
            "status": (
                "READY_FOR_WALK_FORWARD_REVIEW"
                if count >= _MINIMUM_REVIEW_SAMPLES else "INSUFFICIENT_SAMPLE"
            ),
            "probability_is_calibrated": False,
            "uses_actual_fills": False,
            "uses_portfolio_pnl": False,
        })

    return {
        "calibration_version": "beta-experience-shrinkage-v1",
        "status": (
            "SAMPLES_AVAILABLE_REVIEW_REQUIRED"
            if summaries else "INSUFFICIENT_MATURED_COSTED_OUTCOMES"
        ),
        "as_of_epoch": int(as_of_epoch),
        "input_observation_count": len(observations),
        "eligible_costed_non_overlapping_outcome_count": len(non_overlapping),
        "excluded_unmatured_or_non_costed_count": excluded_unmatured,
        "excluded_overlapping_same_pair_count": overlap_excluded,
        "invalid_costed_outcome_count": invalid_outcomes,
        "prior": {
            "type": "BETA",
            "alpha": str(_PRIOR_ALPHA),
            "beta": str(_PRIOR_BETA),
            "interpretation": "NEUTRAL_50_PERCENT_SHRINKAGE_PRIOR",
        },
        "minimum_independent_samples_for_walk_forward_review": _MINIMUM_REVIEW_SAMPLES,
        "groups": summaries,
        "limitations": [
            "The input outcome is an hourly-close forward spot-net cost proxy, not an executed fill.",
            "Only matured SETTLED_COST_ESTIMATE outcomes are considered; unknown costs are excluded.",
            "Overlapping labels for the same pair and candidate version/direction are excluded.",
            "Cross-pair market dependence, portfolio sizing, margin financing and liquidation are not modelled.",
            "The smoothed rate is an experience statistic, not a calibrated live probability or expected alpha.",
            "No strategy selection, parameter promotion, risk-budget update or order action is performed.",
        ],
        "automatic_strategy_selection": False,
        "parameter_promotion": False,
        "order_creation_allowed": False,
        "release_gate_eligible": False,
    }
