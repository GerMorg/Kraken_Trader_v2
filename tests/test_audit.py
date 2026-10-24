import io
import json

from app.audit import AuditLogger, redact


def _console_records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_secret_mapping_keys_are_redacted():
    assert redact({"api_key": "not-for-logs", "message": "okay"}) == {
        "api_key": "[REDACTED]",
        "message": "okay",
    }


def test_structured_log_is_json_and_does_not_contain_secret(tmp_path):
    stream = io.StringIO()
    log_path = tmp_path / "events.jsonl"
    logger = AuditLogger(path=str(log_path), stream=stream)
    try:
        returned = logger.emit("TEST", api_secret="highly-secret-value", status="ok")
    finally:
        logger.close()

    console_record = _console_records(stream)[0]
    file_record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert "highly-secret-value" not in stream.getvalue()
    assert console_record["payload"]["api_secret"] == "[REDACTED]"
    assert file_record["payload"]["api_secret"] == "[REDACTED]"
    assert returned["payload"]["api_secret"] == "[REDACTED]"


def test_market_signal_console_is_compact_but_file_keeps_exact_values(tmp_path):
    stream = io.StringIO()
    log_path = tmp_path / "events.jsonl"
    logger = AuditLogger(path=str(log_path), stream=stream)
    exact_momentum = "341.2804176783073805655451900"
    try:
        returned = logger.emit(
            "MARKET_SIGNAL_EVALUATED",
            cycle_id="cycle-1",
            pair="SOL/USD",
            direction="LONG",
            reason="TREND_UP",
            momentum_fast_bps=exact_momentum,
            momentum_slow_bps="-620.4881117954831445791111990",
            trend_efficiency_24h="0.364456789",
            trend_efficiency_72h="0.2156789",
            trend_strength_score="0.12345678",
            spread_bps="0.956160061194243916",
            score_is_calibrated_probability=False,
            internal_debug_blob="verbose detail",
        )
    finally:
        logger.close()

    console = _console_records(stream)[0]
    archived = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert console["event"] == "MARKET_SIGNAL_EVALUATED"
    assert console["payload"]["momentum_fast_bps"] == "341.28"
    assert console["payload"]["trend_efficiency_24h"] == "0.364"
    assert console["payload"]["score_is_calibrated_probability"] is False
    assert console["payload"]["precision_note"] == "rounded_for_console; exact_values_in_audit_file"
    assert "internal_debug_blob" not in console["payload"]
    assert archived["payload"]["momentum_fast_bps"] == exact_momentum
    assert archived["payload"]["internal_debug_blob"] == "verbose detail"
    assert returned["payload"]["momentum_fast_bps"] == exact_momentum


def test_no_trade_console_preserves_reason_and_cost_blockers(tmp_path):
    stream = io.StringIO()
    logger = AuditLogger(path=str(tmp_path / "events.jsonl"), stream=stream)
    try:
        logger.emit(
            "NO_TRADE",
            cycle_id="cycle-1",
            pair="SUI/USD",
            direction="SHORT",
            reason="MARGIN_COSTS_UNKNOWN_AND_EDGE_UNVALIDATED",
            gross_edge_proxy_bps="115.5589123867",
            gross_edge_proxy_kind="UNVALIDATED_LINEAR_MOMENTUM_HEURISTIC",
            gross_edge_proxy_is_expected_return=False,
            edge_is_calibrated=False,
            margin_cost_reasons=["MARGIN_OPEN_FEE_UNKNOWN", "MARGIN_ROLLOVER_FEE_UNKNOWN"],
            margin_cost_diagnostic={
                "complete": False,
                "quality": "INCOMPLETE",
                "reasons": ["MARGIN_OPEN_FEE_UNKNOWN", "MARGIN_ROLLOVER_FEE_UNKNOWN"],
                "trade_fee_round_trip_bps": "120.0000",
                "spread_round_trip_bps": "1.052797810180554824",
                "slippage_round_trip_bps": "10",
                "rollover_count": 2,
                "total_round_trip_bps": None,
                "large_unneeded_detail": "x" * 1000,
            },
            release_gate_eligible=False,
            minimum_volume="0.01",
            calculated_volume="0.009",
        )
    finally:
        logger.close()

    payload = _console_records(stream)[0]["payload"]
    assert payload["reason"] == "MARGIN_COSTS_UNKNOWN_AND_EDGE_UNVALIDATED"
    assert payload["minimum_volume"] == "0.01"
    assert payload["calculated_volume"] == "0.009"
    assert payload["margin_cost_reasons"] == [
        "MARGIN_OPEN_FEE_UNKNOWN",
        "MARGIN_ROLLOVER_FEE_UNKNOWN",
    ]
    assert payload["gross_edge_proxy_is_expected_return"] is False
    assert payload["margin_cost_diagnostic"]["complete"] is False
    assert payload["margin_cost_diagnostic"]["total_round_trip_bps"] is None
    assert "large_unneeded_detail" not in payload["margin_cost_diagnostic"]


def test_unchanged_research_summary_is_compact_only_on_console(tmp_path):
    stream = io.StringIO()
    log_path = tmp_path / "events.jsonl"
    logger = AuditLogger(path=str(log_path), stream=stream)
    report = {
        "strategy_version": "tsm-0.2.0",
        "total_observations": 840,
        "directional_observations": 154,
        "settled_directional_observations": 76,
        "costed_spot_proxy_observations": 76,
        "pending_directional_observations": 78,
        "validation_status": "INSUFFICIENT_FORWARD_SAMPLE",
        "holdout_sample_available_for_review": False,
        "release_gate_eligible": False,
        "overlapping_signal_horizons": True,
        "portfolio_pnl": False,
        "spot_cost_proxy_all": {
            "mean_net_return_bps": "-422.014745",
            "profit_factor": "0.022114",
            "sample_count": 76,
        },
        "limitations": ["proxy only", "not portfolio PnL"],
        "big_nested_detail": {"lots": ["x" * 3000]},
    }
    try:
        logger.emit("SHADOW_RESEARCH_SUMMARY", **report)
        logger.emit("SHADOW_RESEARCH_SUMMARY", **report)
    finally:
        logger.close()

    console = _console_records(stream)
    archived = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(console) == 2
    assert "limitations" in console[0]["payload"]
    assert console[1]["payload"]["console_view"] == "unchanged_report"
    assert console[1]["payload"]["validation_status"] == "INSUFFICIENT_FORWARD_SAMPLE"
    assert console[1]["payload"]["release_gate_eligible"] is False
    assert console[1]["payload"]["spot_cost_proxy_all"]["profit_factor"] == "0.022114"
    assert "limitations" not in console[1]["payload"]
    assert len(archived) == 2
    assert archived[0]["payload"]["big_nested_detail"] == report["big_nested_detail"]
    assert archived[1]["payload"]["limitations"] == report["limitations"]


def test_changed_report_is_emitted_in_full_and_wallet_mismatch_is_never_hidden(tmp_path):
    stream = io.StringIO()
    logger = AuditLogger(path=str(tmp_path / "events.jsonl"), stream=stream)
    try:
        logger.emit(
            "SHADOW_RESEARCH_SUMMARY",
            validation_status="INSUFFICIENT_FORWARD_SAMPLE",
            total_observations=10,
            limitations=["first"],
        )
        logger.emit(
            "SHADOW_RESEARCH_SUMMARY",
            validation_status="INSUFFICIENT_FORWARD_SAMPLE",
            total_observations=11,
            limitations=["changed"],
        )
        wallet = {
            "status": "WALLET_BALANCE_MISMATCH",
            "cycle_id": "cycle-3",
            "assets_checked": 2,
            "mismatches": 1,
            "complete_for_entries": False,
            "full_portfolio_reconciliation": False,
            "evidence_gaps": ["ZUSD:BALANCE_DIFFERS_FROM_LEDGER"],
            "asset_results": [
                {"asset": "ZUSD", "difference": "1.25", "status": "MISMATCH"}
            ],
        }
        logger.emit("WALLET_BALANCE_RECONCILIATION_SUMMARY", **wallet)
        logger.emit("WALLET_BALANCE_RECONCILIATION_SUMMARY", **wallet)
    finally:
        logger.close()

    console = _console_records(stream)
    assert console[1]["payload"]["limitations"] == ["changed"]
    assert console[2]["payload"]["asset_results"][0]["asset"] == "ZUSD"
    assert console[3]["payload"]["asset_results"][0]["difference"] == "1.25"
