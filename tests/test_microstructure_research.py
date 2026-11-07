from decimal import Decimal as D

from app.microstructure_research import match_microstructure_target_horizons
from app.research import summarize_strategy_candidate_research


def bar(ts: int, mid: str = "101") -> dict:
    return {
        "quality_status": "VALID",
        "book_sample_count": 3,
        "first_book_event_ts": ts - 20,
        "last_book_event_ts": ts,
        "checksum_checks": 3,
        "checksum_failures": 0,
        "trade_id_gaps": 0,
        "connection_gap_count": 0,
        "quality_flags": [],
        "midprice_usd": mid,
        "spread_bps": "2",
    }


def test_matches_valid_near_target_mark_and_calculates_spot_cost_proxy():
    report = match_microstructure_target_horizons(
        [{
            "observation_id": "x", "pair": "XXBTZUSD", "direction": "LONG",
            "decision_ts": 1000, "target_epoch": 2000, "entry_mid_price": "100",
            "entry_spread_bps": "2", "taker_fee_bps": "2",
        }],
        {"XBT/USD": [bar(1995)]}, {"XXBTZUSD": "XBT/USD"}, max_time_error_seconds=10,
    )
    matched = report["matched_observations"][0]
    assert report["matched_observation_count"] == 1
    assert matched["target_time_offset_seconds"] == -5
    assert D(report["spot_net_cost_proxy"]["mean_bps"]) == D("84")
    assert matched["cost_breakdown_bps"] == {
        "round_trip_taker_fee_bps": "4",
        "entry_exit_spread_bps": "2",
        "round_trip_slippage_bps": "10",
        "total_estimated_spot_cost_bps": "16",
    }
    assert report["cost_breakdown_bps"]["total_estimated_spot_cost_bps"]["mean_bps"] == "16"
    assert report["actual_fills"] is False


def test_target_match_never_uses_a_book_mark_after_the_target():
    report = match_microstructure_target_horizons(
        [{
            "pair": "XBT/USD", "direction": "LONG", "decision_ts": 1000,
            "target_epoch": 2000, "entry_mid_price": "100",
            "entry_spread_bps": "2", "taker_fee_bps": "2",
        }],
        {"XBT/USD": [bar(1990, "101"), bar(2001, "120")]}, {},
        max_time_error_seconds=30,
    )
    assert report["matched_observation_count"] == 1
    assert report["matched_observations"][0]["mark_event_ts"] == 1990
    assert report["matched_observations"][0]["target_time_offset_seconds"] == -10
    assert D(report["gross_mark_return"]["mean_bps"]) == D("100")


def test_target_without_a_prior_qualified_book_mark_is_not_matched():
    report = match_microstructure_target_horizons(
        [{
            "pair": "XBT/USD", "direction": "LONG", "decision_ts": 1000,
            "target_epoch": 2000, "entry_mid_price": "100",
            "entry_spread_bps": "2", "taker_fee_bps": "2",
        }],
        {"XBT/USD": [bar(2001, "101")]}, {}, max_time_error_seconds=30,
    )
    assert report["matched_observation_count"] == 0
    assert report["unmatched_reasons"]["TARGET_PRECEDES_FIRST_QUALIFIED_BOOK_MARK"] == 1


def test_legacy_partial_and_distant_book_marks_are_rejected():
    legacy = {"quality_status": "VALID", "midprice_usd": "100", "last_book_event_ts": 2000}
    partial = {**bar(2000), "quality_status": "PARTIAL"}
    report = match_microstructure_target_horizons(
        [{
            "pair": "XXBTZUSD", "direction": "SHORT", "decision_ts": 1000,
            "target_epoch": 2000, "entry_mid_price": "100",
            "entry_spread_bps": "2", "taker_fee_bps": "2",
        }],
        {"XBT/USD": [legacy, partial, bar(2200)]}, {"XXBTZUSD": "XBT/USD"},
        max_time_error_seconds=30,
    )
    assert report["matched_observation_count"] == 0
    assert report["unmatched_reasons"]["TARGET_PRECEDES_FIRST_QUALIFIED_BOOK_MARK"] == 1


def test_forward_exit_mark_must_not_precede_the_signal_decision():
    report = match_microstructure_target_horizons(
        [{
            "candidate_id": "before-entry", "pair": "XBT/USD", "direction": "LONG",
            "decision_ts": 1950, "target_epoch": 2000, "entry_mid_price": "100",
            "entry_spread_bps": "2", "taker_fee_bps": "2",
        }],
        {"XBT/USD": [bar(1900, "101")]}, {}, max_time_error_seconds=120,
    )
    assert report["matched_observation_count"] == 0
    assert report["unmatched_reasons"]["NO_POST_DECISION_BOOK_MARK_BEFORE_TARGET"] == 1


def test_forward_report_exposes_actual_qualified_history_range():
    report = match_microstructure_target_horizons(
        [{
            "candidate_id": "covered", "pair": "XBT/USD", "direction": "LONG",
            "decision_ts": 1000, "target_epoch": 2000, "entry_mid_price": "100",
            "entry_spread_bps": "2", "taker_fee_bps": "2",
        }],
        {"XBT/USD": [bar(1500, "100.5"), bar(1995, "101")]}, {},
        max_time_error_seconds=10,
    )
    coverage = report["history_coverage_by_pair"]["XBT/USD"]
    assert coverage["qualified_book_mark_count"] == 2
    assert coverage["first_qualified_book_event_ts"] == 1500
    assert coverage["latest_qualified_book_event_ts"] == 1995
    assert report["matched_observations"][0]["mark_event_ts"] == 1995


def test_cohort_coverage_excludes_pre_history_and_unmatured_targets():
    report = match_microstructure_target_horizons(
        [
            {
                "candidate_id": "pre-history", "candidate_name": "mr",
                "candidate_version": "0.1", "pair": "XBT/USD", "direction": "LONG",
                "decision_ts": 900, "target_epoch": 1000, "entry_mid_price": "100",
                "entry_spread_bps": "2", "taker_fee_bps": "2",
            },
            {
                "candidate_id": "matured", "candidate_name": "mr",
                "candidate_version": "0.1", "pair": "XBT/USD", "direction": "SHORT",
                "decision_ts": 1100, "target_epoch": 2000, "entry_mid_price": "100",
                "entry_spread_bps": "2", "taker_fee_bps": "2",
            },
            {
                "candidate_id": "not-matured", "candidate_name": "mr",
                "candidate_version": "0.1", "pair": "XBT/USD", "direction": "LONG",
                "decision_ts": 1200, "target_epoch": 3000, "entry_mid_price": "100",
                "entry_spread_bps": "2", "taker_fee_bps": "2",
            },
        ],
        {"XBT/USD": [bar(1000, "100"), bar(1995, "101")]}, {},
        max_time_error_seconds=10,
    )
    overall = report["cohort_coverage"]["overall"]
    assert overall["directional_observation_count"] == 3
    assert overall["decision_before_first_qualified_mark"] == 1
    assert overall["decision_in_feed_count"] == 2
    assert overall["target_not_yet_matured_count"] == 1
    assert overall["matured_in_feed_count"] == 1
    assert overall["matured_matched_count"] == 1
    assert overall["matured_match_coverage_pct"] == "100"
    assert report["cohort_coverage"]["by_candidate"]["mr:0.1"]["matured_in_feed_count"] == 1


def test_cost_components_are_not_reported_when_required_inputs_are_missing():
    report = match_microstructure_target_horizons(
        [{
            "candidate_id": "missing-fee", "pair": "XBT/USD", "direction": "LONG",
            "decision_ts": 1000, "target_epoch": 2000, "entry_mid_price": "100",
            "entry_spread_bps": "2",
        }],
        {"XBT/USD": [bar(1995, "101")]}, {}, max_time_error_seconds=10,
    )
    assert report["matched_observation_count"] == 1
    assert report["costed_match_count"] == 0
    assert report["matched_observations"][0]["cost_breakdown_bps"]["total_estimated_spot_cost_bps"] is None



def test_mean_reversion_summary_splits_direction_and_recorded_regime_features():
    observations = [
        {
            "candidate_id": "mr-long", "candidate_name": "mean_reversion_24h",
            "candidate_version": "0.2.0", "direction": "LONG",
            "signal_reason": "MEAN_REVERSION_ZSCORE_LOW_RANGE_REGIME",
            "status": "SETTLED_COST_ESTIMATE", "spot_net_return_bps": "12",
            "features": {"path_efficiency_24h": 0.1, "z_score": -2.3},
        },
        {
            "candidate_id": "mr-short", "candidate_name": "mean_reversion_24h",
            "candidate_version": "0.2.0", "direction": "SHORT",
            "signal_reason": "MEAN_REVERSION_ZSCORE_HIGH_RANGE_REGIME",
            "status": "SETTLED_COST_ESTIMATE", "spot_net_return_bps": "-5",
            "features": {"path_efficiency_24h": 0.2, "z_score": 2.8},
        },
        {
            "candidate_id": "mr-filtered", "candidate_name": "mean_reversion_24h",
            "candidate_version": "0.2.0", "direction": "NONE",
            "signal_reason": "MEAN_REVERSION_FILTERED_TRENDING_REGIME",
            "status": "NO_DIRECTION", "features": {"path_efficiency_24h": 0.6},
        },
    ]
    diagnostics = summarize_strategy_candidate_research(observations)["mean_reversion_diagnostics"]
    assert diagnostics["candidate_observation_count"] == 3
    assert diagnostics["directional_costed_outcome_count"] == 2
    assert diagnostics["directional_costed_outcomes_by_direction"]["LONG"]["mean_net_return_bps"] == 12.0
    assert diagnostics["directional_costed_outcomes_by_direction"]["SHORT"]["mean_net_return_bps"] == -5.0
    assert diagnostics["path_efficiency_regime_observation_counts"] == {
        "RANGE_FILTERED_IN": 2, "TRENDING_FILTERED_OUT": 1,
    }
    assert diagnostics["costed_outcomes_by_z_score_band"]["2_5_TO_3"]["sample_count"] == 1
    assert diagnostics["release_gate_eligible"] is False
