from decimal import Decimal as D

from app.persistence import Persistence
from app.research import (
    backfill_target_open_outcomes,
    estimate_forward_outcome,
    observation_id,
    settle_due_observations,
    summarize_shadow_research,
)


def make_observation(
    *,
    direction="LONG",
    decision_ts=1000,
    target_epoch=29800,
    bar_ts=0,
    fee="2",
    pair="XXBTZUSD",
):
    return {
        "observation_id": observation_id(
            pair=pair,
            interval_minutes=60,
            bar_ts=bar_ts,
            strategy_version="tsm-test",
            fast_threshold_bps=D("120"),
            slow_threshold_bps=D("180"),
            horizon_seconds=28800,
        ),
        "pair": pair,
        "interval_minutes": 60,
        "bar_ts": bar_ts,
        "decision_ts": decision_ts,
        "strategy_version": "tsm-test",
        "fast_threshold_bps": D("120"),
        "slow_threshold_bps": D("180"),
        "direction": direction,
        "signal_reason": "TREND_UP" if direction == "LONG" else "NO_CONFIRMED_TREND",
        "momentum_fast_bps": D("200"),
        "momentum_slow_bps": D("220"),
        "signal_score": D("0.5"),
        "trend_efficiency_24h": D("0.75"),
        "trend_efficiency_72h": D("0.65"),
        "stop_distance_pct": D("3"),
        "entry_mid_price": D("100"),
        "entry_spread_bps": D("2"),
        "taker_fee_bps": D(fee) if fee is not None else None,
        "horizon_seconds": 28800,
        "target_epoch": target_epoch,
    }


def record_bar(db, pair, ts, close):
    db.record_bar(
        pair, 60, ts, str(close), str(D(str(close)) + D("1")),
        str(D(str(close)) - D("1")), str(close), "100000",
    )


def test_forward_outcome_calculates_cost_proxy_and_margin_break_even_not_margin_pnl():
    outcome = estimate_forward_outcome(
        direction="LONG",
        entry_mid_price=D("100"),
        exit_close_price=D("101"),
        entry_spread_bps=D("2"),
        exit_spread_bps=D("4"),
        taker_fee_bps=D("2"),
    )

    assert outcome["gross_return_bps"] == D("100")
    assert outcome["spot_cost_estimate_bps"] == D("17")
    assert outcome["spot_net_return_bps"] == D("83")
    assert outcome["margin_cost_break_even_bps"] == D("83")
    assert "MARGIN_FEES_UNKNOWN" in outcome["cost_quality"]


def test_forward_outcome_handles_short_direction_and_unknown_taker_fee():
    outcome = estimate_forward_outcome(
        direction="SHORT",
        entry_mid_price=D("100"),
        exit_close_price=D("99"),
        entry_spread_bps=D("2"),
        exit_spread_bps=D("2"),
        taker_fee_bps=None,
    )

    assert outcome["gross_return_bps"] == D("100")
    assert outcome["spot_cost_estimate_bps"] is None
    assert outcome["spot_net_return_bps"] is None
    assert outcome["margin_cost_break_even_bps"] is None


def test_research_persists_trend_efficiency_features_and_rejects_out_of_range():
    db = Persistence(":memory:")
    observation = make_observation()
    assert db.record_research_observation(observation)
    row = db.research_observations()[0]
    assert D(row["trend_efficiency_24h"]) == D("0.75")
    assert D(row["trend_efficiency_72h"]) == D("0.65")
    db.close()

    invalid_db = Persistence(":memory:")
    invalid = make_observation()
    invalid["observation_id"] = "invalid-efficiency"
    invalid["bar_ts"] = 100
    invalid["trend_efficiency_24h"] = D("1.01")
    try:
        invalid_db.record_research_observation(invalid)
        assert False, "expected invalid trend efficiency to be rejected"
    except ValueError as exc:
        assert "TREND_EFFICIENCY_OUT_OF_RANGE" in str(exc)
    invalid_db.close()


def test_research_observation_is_deduplicated_and_missing_fee_can_be_enriched():
    db = Persistence(":memory:")
    initial = make_observation(fee=None)
    assert db.record_research_observation(initial) is True
    enriched = make_observation(fee="2")
    assert db.record_research_observation(enriched) is False

    rows = db.pending_research_observations("XXBTZUSD")
    assert len(rows) == 1
    assert rows[0]["taker_fee_bps"] == "2"
    db.close()


def test_forward_observation_settles_only_when_target_hourly_bar_exists():
    db = Persistence(":memory:")
    observation = make_observation()
    assert db.record_research_observation(observation)
    record_bar(db, "XXBTZUSD", 28800, "101")

    events = settle_due_observations(
        db, pair="XXBTZUSD", current_exit_spread_bps=D("4")
    )

    assert len(events) == 1
    assert events[0]["event"] == "SHADOW_RESEARCH_OUTCOME_SETTLED"
    rows = db.research_observations()
    assert rows[0]["status"] == "SETTLED_COST_ESTIMATE"
    assert D(rows[0]["gross_return_bps"]) == D("100")
    assert D(rows[0]["spot_net_return_bps"]) == D("83")
    assert D(rows[0]["margin_cost_break_even_bps"]) == D("83")
    assert rows[0]["target_open_reference_ts"] == 28800
    assert D(rows[0]["target_open_price"]) == D("101")
    assert D(rows[0]["target_open_gross_return_bps"]) == D("100")
    db.close()


def test_missing_target_bar_is_marked_incomplete_not_given_a_guessed_outcome():
    db = Persistence(":memory:")
    observation = make_observation()
    assert db.record_research_observation(observation)
    record_bar(db, "XXBTZUSD", 36000, "105")

    events = settle_due_observations(
        db, pair="XXBTZUSD", current_exit_spread_bps=D("3")
    )

    assert len(events) == 1
    assert events[0]["event"] == "SHADOW_RESEARCH_OUTCOME_INCOMPLETE"
    row = db.research_observations()[0]
    assert row["status"] == "INCOMPLETE_MISSING_TARGET_BAR"
    assert row["gross_return_bps"] is None
    assert row["spot_net_return_bps"] is None
    assert row["target_open_reference_ts"] is None
    assert row["target_open_price"] is None
    assert row["target_open_gross_return_bps"] is None
    db.close()


def test_no_direction_is_recorded_but_not_counted_as_a_forward_trade_label():
    db = Persistence(":memory:")
    observation = make_observation(direction="NONE", fee="2")
    assert db.record_research_observation(observation)
    assert db.pending_research_observations("XXBTZUSD") == []
    summary = summarize_shadow_research(db.research_observations())

    assert summary["total_observations"] == 1
    assert summary["directional_observations"] == 0
    assert summary["settled_directional_observations"] == 0
    assert summary["validation_status"] == "INSUFFICIENT_FORWARD_SAMPLE"
    assert summary["release_gate_eligible"] is False
    db.close()


def test_research_summary_uses_chronological_holdout_and_never_releases_live_gate():
    rows = []
    for index in range(100):
        rows.append({
            "observation_id": str(index),
            "decision_ts": index,
            "strategy_version": "tsm-test",
            "direction": "LONG" if index % 2 == 0 else "SHORT",
            "status": "SETTLED_COST_ESTIMATE",
            "spot_net_return_bps": str(D("10") if index % 3 else D("-5")),
            "margin_cost_break_even_bps": str(D("10") if index % 3 else D("-5")),
        })

    summary = summarize_shadow_research(rows)

    assert summary["validation_status"] == "HOLDOUT_AVAILABLE_REVIEW_REQUIRED"
    assert summary["chronological_splits"]["train_first_60_percent"]["sample_count"] == 60
    assert summary["chronological_splits"]["validation_next_20_percent"]["sample_count"] == 20
    assert summary["chronological_splits"]["test_last_20_percent"]["sample_count"] == 20
    assert summary["margin_cost_diagnostics"]["margin_net_return_calculated"] is False
    assert len(summary["margin_cost_diagnostics"]["fee_budget_sensitivity"]) == 4
    assert all(
        item["status"] == "SENSITIVITY_ASSUMPTION_NOT_KRAKEN_QUOTE"
        for item in summary["margin_cost_diagnostics"]["fee_budget_sensitivity"]
    )
    assert summary["release_gate_eligible"] is False
    assert summary["portfolio_pnl"] is False


def test_non_overlapping_forward_diagnostic_excludes_same_pair_overlap_only():
    rows = [
        {
            "observation_id": "btc-first",
            "pair": "XXBTZUSD",
            "decision_ts": 1000,
            "target_epoch": 29800,
            "horizon_seconds": 28800,
            "strategy_version": "tsm-test",
            "direction": "LONG",
            "status": "SETTLED_COST_ESTIMATE",
            "spot_net_return_bps": "100",
            "margin_cost_break_even_bps": "100",
        },
        {
            "observation_id": "btc-overlap",
            "pair": "XXBTZUSD",
            "decision_ts": 2000,
            "target_epoch": 30800,
            "horizon_seconds": 28800,
            "strategy_version": "tsm-test",
            "direction": "SHORT",
            "status": "SETTLED_COST_ESTIMATE",
            "spot_net_return_bps": "500",
            "margin_cost_break_even_bps": "500",
        },
        {
            "observation_id": "btc-after-horizon",
            "pair": "XXBTZUSD",
            "decision_ts": 29800,
            "target_epoch": 58600,
            "horizon_seconds": 28800,
            "strategy_version": "tsm-test",
            "direction": "SHORT",
            "status": "SETTLED_COST_ESTIMATE",
            "spot_net_return_bps": "-20",
            "margin_cost_break_even_bps": "-20",
        },
        {
            "observation_id": "eth-independent-stream",
            "pair": "XETHZUSD",
            "decision_ts": 2000,
            "target_epoch": 30800,
            "horizon_seconds": 28800,
            "strategy_version": "tsm-test",
            "direction": "LONG",
            "status": "SETTLED_COST_ESTIMATE",
            "spot_net_return_bps": "50",
            "margin_cost_break_even_bps": "50",
        },
    ]

    summary = summarize_shadow_research(rows, strategy_version="tsm-test")
    diagnostic = summary["non_overlapping_costed_forward_diagnostics"]

    assert diagnostic["candidate_costed_observations"] == 4
    assert diagnostic["observation_count"] == 3
    assert diagnostic["overlap_excluded_count"] == 1
    assert diagnostic["invalid_timing_excluded_count"] == 0
    assert diagnostic["spot_cost_proxy_all"]["sample_count"] == 3
    assert diagnostic["spot_cost_proxy_by_direction"]["LONG"]["sample_count"] == 2
    assert diagnostic["spot_cost_proxy_by_direction"]["SHORT"]["sample_count"] == 1
    assert diagnostic["status"] == "AVAILABLE_DIAGNOSTIC_ONLY"
    assert diagnostic["cross_pair_observations_independent"] is False
    assert diagnostic["portfolio_pnl"] is False
    assert diagnostic["release_gate_eligible"] is False
    assert summary["release_gate_eligible"] is False


def test_research_summary_never_combines_different_strategy_versions():
    rows = []
    for index in range(30):
        rows.append({
            "observation_id": f"old-{index}",
            "decision_ts": index,
            "strategy_version": "tsm-0.1.2",
            "direction": "LONG",
            "status": "SETTLED_COST_ESTIMATE",
            "spot_net_return_bps": "-100",
            "margin_cost_break_even_bps": "-100",
        })
    for index in range(5):
        rows.append({
            "observation_id": f"new-{index}",
            "decision_ts": 100 + index,
            "strategy_version": "tsm-0.2.0",
            "direction": "LONG",
            "status": "SETTLED_COST_ESTIMATE",
            "spot_net_return_bps": "10",
            "margin_cost_break_even_bps": "10",
            "trend_efficiency_24h": "0.8",
            "trend_efficiency_72h": "0.7",
        })

    summary = summarize_shadow_research(rows, strategy_version="tsm-0.2.0")
    assert summary["strategy_version"] == "tsm-0.2.0"
    assert summary["total_observations"] == 5
    assert summary["costed_spot_proxy_observations"] == 5
    assert D(summary["spot_cost_proxy_all"]["mean_net_return_bps"]) == D("10")
    assert D(summary["trend_quality_diagnostics"]["directional_efficiency_24h"]["mean"]) == D("0.8")
    assert D(summary["trend_quality_diagnostics"]["directional_efficiency_72h"]["mean"]) == D("0.7")

    automatic = summarize_shadow_research(rows)
    assert automatic["strategy_version"] == "tsm-0.2.0"
    assert automatic["total_observations"] == 5


def test_existing_schema_is_migrated_to_store_trend_efficiency(tmp_path):
    import sqlite3

    path = str(tmp_path / "research-schema.sqlite3")
    db = Persistence(path)
    observation = make_observation()
    observation.pop("trend_efficiency_24h")
    observation.pop("trend_efficiency_72h")
    assert db.record_research_observation(observation)
    db.close()

    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE research_observations DROP COLUMN trend_efficiency_24h")
    connection.execute("ALTER TABLE research_observations DROP COLUMN trend_efficiency_72h")
    connection.execute("UPDATE schema_meta SET value='6' WHERE key='schema_version'")
    connection.commit()
    connection.close()

    reopened = Persistence(path)
    columns = {
        row["name"] for row in reopened.connection.execute(
            "PRAGMA table_info(research_observations)"
        )
    }
    assert "trend_efficiency_24h" in columns
    assert "trend_efficiency_72h" in columns
    stored = reopened.research_observations()
    assert len(stored) == 1
    assert stored[0]["trend_efficiency_24h"] is None
    assert stored[0]["trend_efficiency_72h"] is None
    version = reopened.connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()["value"]
    assert version == "9"
    reopened.close()


def test_gross_forward_metrics_separate_market_movement_from_costs_and_report_timing():
    rows = []
    samples = [
        ("obs-a", 1000, "100", "60"),
        ("obs-b", 2000, "-200", "-240"),
        ("obs-c", 3000, "50", "10"),
    ]
    for observation_id_value, decision_ts, gross, net in samples:
        target_epoch = decision_ts + 28800
        rows.append({
            "observation_id": observation_id_value,
            "decision_ts": decision_ts,
            "target_epoch": target_epoch,
            "outcome_bar_ts": 28800,
            "interval_minutes": 60,
            "horizon_seconds": 28800,
            "strategy_version": "tsm-0.2.0",
            "direction": "LONG",
            "status": "SETTLED_COST_ESTIMATE",
            "gross_return_bps": gross,
            "spot_net_return_bps": net,
            "margin_cost_break_even_bps": net,
        })

    summary = summarize_shadow_research(rows, strategy_version="tsm-0.2.0")
    assert summary["gross_forward_proxy_all"]["sample_count"] == 3
    assert D(summary["gross_forward_proxy_all"]["mean_gross_return_bps"]) == D("-16.66666666666666666666666667")
    assert D(summary["spot_cost_proxy_all"]["mean_net_return_bps"]) == D("-56.66666666666666666666666667")
    assert summary["gross_forward_chronological_splits"]["train_first_60_percent"]["sample_count"] == 1
    assert summary["gross_forward_chronological_splits"]["validation_next_20_percent"]["sample_count"] == 1
    assert summary["gross_forward_chronological_splits"]["test_last_20_percent"]["sample_count"] == 1

    timing = summary["forward_timing_diagnostics"]
    assert timing["sample_count"] == 3
    assert timing["invalid_timestamp_count"] == 0
    assert timing["target_horizon_overshoot_seconds"]["maximum"] == 2600
    assert timing["target_horizon_overshoot_seconds"]["median"] == "1600"
    assert timing["status"] == "HOURLY_CLOSE_LABEL_NOT_EXACT_HORIZON"


def test_forward_timing_diagnostics_marks_invalid_timestamp_rows():
    summary = summarize_shadow_research([{
        "observation_id": "bad-time",
        "decision_ts": 1000,
        "target_epoch": 29800,
        "outcome_bar_ts": 0,
        "interval_minutes": 60,
        "strategy_version": "tsm-0.2.0",
        "direction": "LONG",
        "status": "SETTLED_COST_ESTIMATE",
        "gross_return_bps": "1",
        "spot_net_return_bps": "1",
        "margin_cost_break_even_bps": "1",
    }], strategy_version="tsm-0.2.0")
    assert summary["forward_timing_diagnostics"]["sample_count"] == 0
    assert summary["forward_timing_diagnostics"]["invalid_timestamp_count"] == 1


def synthetic_raw_bars(count=600, *, hourly_gap_at=None):
    result = []
    for index in range(count):
        timestamp = index * 3600
        if hourly_gap_at is not None and index >= hourly_gap_at:
            timestamp += 3600
        close = D("100") + D(index) / D("5")
        result.append({
            "ts": timestamp,
            "open": close,
            "high": close * D("1.01"),
            "low": close * D("0.99"),
            "close": close,
            "volume": D("10000"),
        })
    return result


def test_ohlcv_backtest_scenarios_run_chronologically_and_are_explicitly_non_live():
    from app.research import run_ohlcv_backtest_scenarios

    report = run_ohlcv_backtest_scenarios(
        synthetic_raw_bars(),
        pair="XBT/USD",
        taker_fee_bps=D("20"),
        current_spread_bps=D("2"),
        holding_hours=8,
        fast_threshold_bps=D("120"),
        slow_threshold_bps=D("180"),
        stop_pct=D("3"),
    )

    assert report["status"] == "SCENARIO_ONLY_NOT_LIVE_VALIDATION"
    assert report["chronological_split_fractions"] == {
        "train": "60%", "validation": "20%", "test": "20%"
    }
    assert report["margin_cost_budgets_are_exchange_quotes"] is False
    assert report["exit_model_version"] == "ohlcv-stop-first-partial-take-profit-time-exit-v2"
    assert report["intrabar_ambiguity_policy"] == (
        "STOP_FIRST_WHEN_STOP_AND_TAKE_PROFIT_TOUCH_SAME_HOURLY_BAR"
    )
    assert report["time_exit_policy"] == (
        "EXIT_AT_SCHEDULED_BAR_OPEN; DO_NOT_INSPECT_ITS_LATER_HIGH_LOW"
    )
    assert report["partial_take_profit_policy"] == {
        "trigger_pct": "3.5",
        "fraction_of_remaining_position": "0.5",
        "maximum_partial_take_profit_events_per_trade": 1,
    }
    assert len(report["scenario_results"]) == 4
    assert [item["assumed_combined_margin_fee_budget_bps"] for item in report["scenario_results"]] == [
        "5", "10", "20", "40"
    ]
    assert all(
        item["status"] == "SENSITIVITY_ASSUMPTION_NOT_KRAKEN_QUOTE"
        for item in report["scenario_results"]
    )
    assert report["scenario_results"][0]["splits"]["test_last_20_percent"]["end_bar_ts"] > (
        report["scenario_results"][0]["splits"]["validation_next_20_percent"]["end_bar_ts"]
    )


def test_ohlcv_backtest_scenarios_refuse_hourly_gaps_and_missing_fee():
    from app.research import run_ohlcv_backtest_scenarios

    args = {
        "pair": "LOW-VOLUME/USD",
        "taker_fee_bps": D("20"),
        "current_spread_bps": D("2"),
        "holding_hours": 8,
        "fast_threshold_bps": D("120"),
        "slow_threshold_bps": D("180"),
        "stop_pct": D("3"),
    }
    gap_report = run_ohlcv_backtest_scenarios(
        synthetic_raw_bars(hourly_gap_at=400), **args
    )
    assert gap_report["status"] == "INCOMPLETE_HOURLY_BAR_GAPS"

    fee_unknown_report = run_ohlcv_backtest_scenarios(
        synthetic_raw_bars(), **{**args, "taker_fee_bps": None}
    )
    assert fee_unknown_report["status"] == "SKIPPED_TAKER_FEE_UNKNOWN"



def test_target_open_backfill_adds_comparator_without_overwriting_primary_outcome():
    db = Persistence(":memory:")
    observation = make_observation(fee="2")
    assert db.record_research_observation(observation)
    db.record_bar(
        "XXBTZUSD", 60, 28800, "102", "106", "101", "105", "100000"
    )

    # Simulate an outcome settled by the older schema/version; primary close-based
    # values must survive while the comparator is backfilled from the stored bar open.
    assert db.settle_research_observation(observation["observation_id"], {
        "status": "SETTLED_COST_ESTIMATE",
        "outcome_bar_ts": 28800,
        "exit_close_price": "105",
        "exit_spread_bps": "4",
        "gross_return_bps": "500",
        "spot_cost_estimate_bps": "17",
        "spot_net_return_bps": "483",
        "margin_cost_break_even_bps": "483",
        "cost_quality": "LEGACY_SETTLED_LABEL",
        "target_open_reference_ts": None,
        "target_open_price": None,
        "target_open_gross_return_bps": None,
    })
    before = db.research_observations()[0]
    summary = backfill_target_open_outcomes(db)
    after = db.research_observations()[0]

    assert summary["updated"] == 1
    assert summary["primary_outcomes_overwritten"] is False
    assert after["gross_return_bps"] == before["gross_return_bps"] == "500"
    assert after["spot_net_return_bps"] == before["spot_net_return_bps"] == "483"
    assert after["target_open_reference_ts"] == 28800
    assert D(after["target_open_price"]) == D("102")
    assert D(after["target_open_gross_return_bps"]) == D("200")
    assert backfill_target_open_outcomes(db)["updated"] == 0

    metrics = summarize_shadow_research(db.research_observations(), strategy_version="tsm-test")
    assert metrics["gross_forward_proxy_all"]["mean_gross_return_bps"] == "500"
    assert D(metrics["target_open_gross_proxy_all"]["mean_gross_return_bps"]) == D("200")
    assert metrics["target_open_timing_diagnostics"]["sample_count"] == 1
    assert metrics["target_open_timing_diagnostics"]["target_reference_lead_seconds"]["mean"] == "1000"
    assert metrics["target_open_timing_diagnostics"]["actual_horizon_seconds"]["mean"] == "27800"
    db.close()


def test_target_open_bar_lookup_uses_new_candle_at_exact_hour_boundary():
    db = Persistence(":memory:")
    db.record_bar("XXBTZUSD", 60, 0, "100", "102", "99", "101", "10")
    assert db.research_bar_open_at_target_epoch("XXBTZUSD", 60, 3600) is None
    db.record_bar("XXBTZUSD", 60, 3600, "101", "103", "100", "102", "10")
    bar = db.research_bar_open_at_target_epoch("XXBTZUSD", 60, 3600)
    assert bar is not None
    assert bar["ts"] == 3600
    assert D(bar["open"]) == D("101")
    db.close()
