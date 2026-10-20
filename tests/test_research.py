from decimal import Decimal as D

from app.persistence import Persistence
from app.research import (
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
    assert rows[0]["margin_cost_break_even_bps"] == "83"
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
