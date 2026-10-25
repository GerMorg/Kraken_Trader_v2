from decimal import Decimal as D

from app.persistence import Persistence
from app.research import (
    settle_due_strategy_candidate_observations,
    summarize_strategy_candidate_research,
)
from app.strategy_lab import strategy_candidate_observation_id


def make_candidate(
    *,
    pair="XXBTZUSD",
    bar_ts=3600,
    decision_ts=7200,
    direction="LONG",
    candidate_name="current_tsm",
    candidate_version="tsm-0.2.0",
    fee="2",
):
    horizon = 8 * 3600
    return {
        "candidate_id": strategy_candidate_observation_id(
            pair=pair,
            interval_minutes=60,
            bar_ts=bar_ts,
            candidate_name=candidate_name,
            candidate_version=candidate_version,
            horizon_seconds=horizon,
        ),
        "pair": pair,
        "interval_minutes": 60,
        "bar_ts": bar_ts,
        "decision_ts": decision_ts,
        "candidate_name": candidate_name,
        "candidate_version": candidate_version,
        "direction": direction,
        "signal_reason": "TEST_SIGNAL" if direction != "NONE" else "TEST_NO_SIGNAL",
        "score": D("0.7"),
        "features": {"test_feature": "value"},
        "entry_mid_price": D("100"),
        "entry_spread_bps": D("2"),
        "taker_fee_bps": D(fee) if fee is not None else None,
        "horizon_seconds": horizon,
        "target_epoch": decision_ts + horizon,
    }


def record_bars(db):
    for timestamp in range(0, 43201, 3600):
        close = D("101") if timestamp == 36000 else D("100") + D(timestamp) / D("360000")
        db.record_bar(
            "XXBTZUSD", 60, timestamp, str(close), str(close * D("1.01")),
            str(close * D("0.99")), str(close), "10000",
        )


def test_strategy_candidate_observations_are_idempotent_and_separate_from_order_intents():
    db = Persistence(":memory:")
    observation = make_candidate()

    assert db.record_strategy_candidate_observation(observation) is True
    assert db.record_strategy_candidate_observation(observation) is False
    assert db.strategy_candidate_bar_is_complete("XXBTZUSD", 3600, expected_candidate_count=1)
    assert db.strategy_candidate_observations()[0]["features"] == {"test_feature": "value"}
    assert db.unresolved_intents() == []
    assert db.connection.execute(
        "SELECT COUNT(*) FROM intents"
    ).fetchone()[0] == 0
    db.close()


def test_no_direction_candidate_is_recorded_but_never_scheduled_for_settlement():
    db = Persistence(":memory:")
    observation = make_candidate(direction="NONE")

    assert db.record_strategy_candidate_observation(observation) is True
    assert db.pending_strategy_candidate_observations("XXBTZUSD") == []
    summary = summarize_strategy_candidate_research(db.strategy_candidate_observations())
    candidate = summary["candidates"][0]
    assert candidate["observation_count"] == 1
    assert candidate["directional_observation_count"] == 0
    assert candidate["no_direction_count"] == 1
    assert candidate["costed_outcome_count"] == 0
    assert summary["release_gate_eligible"] is False
    db.close()


def test_strategy_candidate_forward_settlement_uses_stored_bar_and_discloses_cost_proxy():
    db = Persistence(":memory:")
    observation = make_candidate()
    assert db.record_strategy_candidate_observation(observation)
    record_bars(db)

    events = settle_due_strategy_candidate_observations(
        db, pair="XXBTZUSD", current_exit_spread_bps=D("4")
    )

    assert len(events) == 1
    assert events[0]["event"] == "SHADOW_STRATEGY_CANDIDATE_OUTCOME_SETTLED"
    assert events[0]["payload"]["actual_fill"] is False
    row = db.strategy_candidate_observations()[0]
    assert row["status"] == "SETTLED_COST_ESTIMATE"
    assert D(row["gross_return_bps"]) == D("100")
    assert D(row["spot_cost_estimate_bps"]) == D("17")
    assert D(row["spot_net_return_bps"]) == D("83")
    assert "MARGIN_FEES_UNKNOWN" in row["cost_quality"]
    assert db.unresolved_intents() == []
    db.close()


def test_candidate_summary_separates_versions_and_never_promotes_a_strategy():
    db = Persistence(":memory:")
    current = make_candidate(candidate_name="current_tsm", candidate_version="tsm-0.2.0")
    alternate = make_candidate(
        candidate_name="mean_reversion_24h",
        candidate_version="mean-reversion-24h-0.1.0",
        direction="SHORT",
    )
    assert db.record_strategy_candidate_observation(current)
    assert db.record_strategy_candidate_observation(alternate)
    record_bars(db)
    settle_due_strategy_candidate_observations(
        db, pair="XXBTZUSD", current_exit_spread_bps=D("4")
    )

    summary = summarize_strategy_candidate_research(db.strategy_candidate_observations())

    assert summary["candidate_count"] == 2
    assert {row["candidate_name"] for row in summary["candidates"]} == {
        "current_tsm", "mean_reversion_24h"
    }
    assert all(row["costed_outcome_count"] == 1 for row in summary["candidates"])
    assert summary["actual_fills"] is False
    assert summary["portfolio_pnl"] is False
    assert summary["automatic_strategy_selection"] is False
    assert summary["parameter_promotion"] is False
    assert summary["release_gate_eligible"] is False
    db.close()


def test_candidate_id_changes_on_strategy_version_or_bar():
    original = make_candidate()
    new_version = make_candidate(candidate_version="tsm-0.3.0")
    next_bar = make_candidate(bar_ts=7200, decision_ts=10800)

    assert len({original["candidate_id"], new_version["candidate_id"], next_bar["candidate_id"]}) == 3
