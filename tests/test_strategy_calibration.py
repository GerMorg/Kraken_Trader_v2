from decimal import Decimal as D

from app.strategy_calibration import summarize_experience_calibration


def row(
    candidate_id: str,
    *,
    pair: str = "XBT/USD",
    decision_ts: int = 100,
    target_epoch: int = 108,
    direction: str = "LONG",
    status: str = "SETTLED_COST_ESTIMATE",
    net_bps: str | None = "10",
    regime: str = "TRENDING",
) -> dict:
    return {
        "candidate_id": candidate_id,
        "candidate_name": "current_tsm",
        "candidate_version": "tsm-0.2.0",
        "pair": pair,
        "decision_ts": decision_ts,
        "target_epoch": target_epoch,
        "direction": direction,
        "status": status,
        "spot_net_return_bps": net_bps,
        "features": {"market_regime": regime},
    }


def test_calibration_uses_only_matured_costed_labels_and_excludes_same_pair_overlap():
    observations = [
        row("a", decision_ts=100, target_epoch=108, net_bps="20"),
        row("b", decision_ts=104, target_epoch=112, net_bps="40"),
        row("c", pair="ETH/USD", decision_ts=100, target_epoch=108, net_bps="-10"),
        row("d", decision_ts=120, target_epoch=128, status="PENDING", net_bps=None),
        row("e", decision_ts=120, target_epoch=128, status="SETTLED_GROSS_ONLY", net_bps=None),
        row("f", decision_ts=190, target_epoch=210, net_bps="5"),
    ]
    summary = summarize_experience_calibration(observations, as_of_epoch=200)

    assert summary["eligible_costed_non_overlapping_outcome_count"] == 2
    assert summary["excluded_overlapping_same_pair_count"] == 1
    assert summary["excluded_unmatured_or_non_costed_count"] == 3
    assert len(summary["groups"]) == 2
    assert summary["order_creation_allowed"] is False
    assert all(item["probability_is_calibrated"] is False for item in summary["groups"])


def test_calibration_uses_neutral_beta_shrinkage_and_separates_regimes():
    observations = [
        row("a", decision_ts=10, target_epoch=18, net_bps="10", regime="TRENDING"),
        row("b", pair="ETH/USD", decision_ts=10, target_epoch=18, net_bps="-5", regime="RANGING"),
    ]
    summary = summarize_experience_calibration(observations, as_of_epoch=100)
    by_regime = {item["market_regime"]: item for item in summary["groups"]}

    assert D(by_regime["TRENDING"]["raw_positive_rate_pct"]) == D("100")
    assert D(by_regime["TRENDING"]["beta_shrunk_positive_rate_estimate_pct"]) == (
        D(6) / D(11) * D(100)
    )
    assert D(by_regime["RANGING"]["beta_shrunk_positive_rate_estimate_pct"]) == (
        D(5) / D(11) * D(100)
    )
    assert by_regime["TRENDING"]["status"] == "INSUFFICIENT_SAMPLE"


def test_calibration_with_no_completed_costed_outcomes_is_explicitly_empty():
    summary = summarize_experience_calibration(
        [row("a", status="PENDING", target_epoch=200, net_bps=None)],
        as_of_epoch=100,
    )
    assert summary["status"] == "INSUFFICIENT_MATURED_COSTED_OUTCOMES"
    assert summary["groups"] == []
