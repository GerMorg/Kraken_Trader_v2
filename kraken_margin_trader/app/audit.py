from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
from typing import Any, TextIO

from app import STRATEGY_VERSION, __version__

_SECRET_KEYS = {
    "api_key", "kraken_api_key", "api_secret", "kraken_api_secret", "secret",
    "token", "api_sign", "signature", "authorization", "headers",
}
_SECRET_PATTERN = re.compile(
    r"(?i)(api[-_ ]?(?:key|sign|secret)|authorization|bearer|token)\s*[:=]\s*[^\s,;]+"
)

# The file sink always receives the complete redacted record. These views only
# reduce repetitive fields in Home Assistant stdout; they never alter trading.
_COMPACT_FIELDS: dict[str, tuple[str, ...]] = {
    "MARKET_SIGNAL_EVALUATED": (
        "cycle_id", "pair", "direction", "reason", "momentum_fast_bps",
        "momentum_slow_bps", "trend_efficiency_24h", "trend_efficiency_72h",
        "trend_strength_score", "spread_bps", "score_is_calibrated_probability",
    ),
    "NO_TRADE": (
        "cycle_id", "pair", "direction", "reason", "reasons", "evidence_gaps",
        "gross_edge_proxy_bps", "gross_edge_proxy_kind",
        "gross_edge_proxy_is_expected_return", "edge_is_calibrated",
        "margin_cost_reasons", "spot_cost_reasons", "margin_cost_diagnostic",
        "spot_cost_diagnostic", "margin_fee_rates_confirmed",
        "release_gate_eligible", "no_new_position_risk", "product_type",
    ),
    "KRAKEN_HISTORY_SYNC": (
        "cycle_id", "source", "pages", "items_saved", "expected_count",
        "next_offset", "initial_backfill_complete", "generation_complete",
        "cached_until_refresh", "last_error", "quality_gap_count", "quality_gap_types",
    ),
    "KRAKEN_API_KEY_PERMISSION_CHECK": (
        "cycle_id", "api_key_metadata_complete", "api_key_expired",
        "api_key_expired_or_expiry_unknown", "api_key_expiry_unknown",
        "historical_query_window_restricted", "historical_query_window_unknown",
        "missing_required_query_permissions", "missing_order_write_permissions",
        "ready_for_account_reconciliation", "withdrawal_permissions_present",
    ),
    "KRAKEN_HISTORY_RECONCILED": (
        "cycle_id", "history_ready_for_entries", "evidence_gaps", "source_summaries",
    ),
    "KRAKEN_TRADE_LEDGER_RECONCILIATION": (
        "cycle_id", "status", "ledger_ids_referenced", "ledger_ids_requested",
        "missing_ledger_references", "missing_trade_references",
        "ledger_quality_gap_count", "trade_quality_gap_count", "trade_rows_recovered",
        "trade_rows_confirmed_empty", "trade_rows_queried", "trade_rows_query_missing",
        "api_errors",
    ),
    "MARKET_TICKERS_REFRESHED": ("cycle_id", "count", "captured_at"),
}

_REPORT_EVENTS = {
    "SHADOW_RESEARCH_SUMMARY",
    "WALLET_BALANCE_RECONCILIATION_SUMMARY",
    "OHLCV_BACKTEST_SCENARIO_REPORT",
}

_ALWAYS_FULL_EVENTS = {
    "STARTUP_BEGIN", "STARTUP_STATUS", "CYCLE_START", "CYCLE_SUMMARY",
    "CYCLE_BLOCKED", "CYCLE_FAILED_CIRCUIT_BREAKER", "HEALTHCHECK_OK",
    "SHUTDOWN_SIGNAL_RECEIVED", "SHUTDOWN_COMPLETE", "CIRCUIT_BREAKER_STATUS",
    "CIRCUIT_BREAKER_RECOVERED", "ACCOUNT_SNAPSHOT_PERSISTED",
    "ORDER_RECONCILIATION_SUMMARY", "POSITION_FILL_RECONCILIATION_SUMMARY",
    "POSITION_MANAGEMENT_DECISION", "LIVE_ORDER_REJECTED_PRE_SUBMIT",
    "LIVE_ORDER_SUBMISSION_RESULT", "SHADOW_ORDER_VALIDATION_OUTCOME_UNKNOWN",
    "SHADOW_ORDER_VALIDATION_FAILED", "SHADOW_ORDER_VALIDATED_NO_ORDER_SENT",
}


def _json_default(value: Any) -> str:
    return str(value)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in _SECRET_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_PATTERN.sub(r"\1=[REDACTED]", value)
    return value


def _rounded(value: Any, places: int) -> Any:
    """Round only the console representation; persisted audit values stay exact."""
    if value is None:
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return value
        quantum = Decimal(1).scaleb(-places)
        return format(number.quantize(quantum, rounding=ROUND_HALF_UP), "f")
    except (InvalidOperation, TypeError, ValueError):
        return value


def _compact_cost_diagnostic(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    fields = (
        "complete", "quality", "reasons", "trade_fee_round_trip_bps",
        "spread_round_trip_bps", "slippage_round_trip_bps", "rollover_count",
        "margin_open_fee_bps_on_extended_amount",
        "margin_rollover_fee_bps_per_event_on_extended_amount",
        "total_round_trip_bps",
    )
    result = {key: value[key] for key in fields if key in value}
    for key in (
        "trade_fee_round_trip_bps", "spread_round_trip_bps",
        "slippage_round_trip_bps", "margin_open_fee_bps_on_extended_amount",
        "margin_rollover_fee_bps_per_event_on_extended_amount", "total_round_trip_bps",
    ):
        if key in result and result[key] is not None:
            result[key] = _rounded(result[key], 2)
    return result


def _compact_payload(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    fields = _COMPACT_FIELDS.get(event)
    if fields is None:
        return payload
    result = {key: payload[key] for key in fields if key in payload}
    if event == "MARKET_SIGNAL_EVALUATED":
        for key in ("momentum_fast_bps", "momentum_slow_bps", "spread_bps"):
            if key in result:
                result[key] = _rounded(result[key], 2)
        for key in ("trend_efficiency_24h", "trend_efficiency_72h"):
            if key in result:
                result[key] = _rounded(result[key], 3)
        if "trend_strength_score" in result:
            result["trend_strength_score"] = _rounded(result["trend_strength_score"], 4)
        result["precision_note"] = "rounded_for_console; exact_values_in_audit_file"
    elif event == "NO_TRADE":
        if "gross_edge_proxy_bps" in result:
            result["gross_edge_proxy_bps"] = _rounded(result["gross_edge_proxy_bps"], 2)
        for key in ("margin_cost_diagnostic", "spot_cost_diagnostic"):
            if key in result:
                result[key] = _compact_cost_diagnostic(result[key])
        result["precision_note"] = "rounded_for_console; exact_values_in_audit_file"
    elif event == "KRAKEN_HISTORY_SYNC":
        if result.get("quality_gap_count", 0) or result.get("last_error"):
            result = payload
    elif event == "KRAKEN_API_KEY_PERMISSION_CHECK":
        if (
            result.get("api_key_metadata_complete") is False
            or result.get("api_key_expired") is True
            or result.get("api_key_expiry_unknown") is True
            or result.get("historical_query_window_restricted") is True
            or result.get("historical_query_window_unknown") is True
            or result.get("missing_required_query_permissions")
            or result.get("missing_order_write_permissions")
        ):
            result = payload
    elif event == "KRAKEN_TRADE_LEDGER_RECONCILIATION":
        if (
            result.get("status") not in {"COMPLETE", "OK", "RECONCILED"}
            or result.get("missing_ledger_references", 0)
            or result.get("missing_trade_references", 0)
            or result.get("ledger_quality_gap_count", 0)
            or result.get("trade_quality_gap_count", 0)
            or result.get("api_errors")
        ):
            result = payload
    if result is payload:
        return payload
    result["console_view"] = "compact"
    return result


def _report_signature(payload: dict[str, Any]) -> str:
    stable = {
        key: value for key, value in payload.items()
        if key not in {"cycle_id", "observation_id"}
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _unchanged_report_payload(event: str, payload: dict[str, Any], signature: str) -> dict[str, Any]:
    """Retain key controls/metrics while avoiding repeating a full unchanged report."""
    if event == "WALLET_BALANCE_RECONCILIATION_SUMMARY":
        fields = (
            "status", "reconciliation_scope", "assets_checked",
            "assets_matched_to_latest_ledger_balance", "assets_stable_without_ledger_activity",
            "stable_without_ledger_activity_count", "ledger_verified_asset_count",
            "mismatches", "complete", "complete_for_entries",
            "full_portfolio_reconciliation", "evidence_gaps",
        )
        summary = {key: payload[key] for key in fields if key in payload}
    elif event == "SHADOW_RESEARCH_SUMMARY":
        fields = (
            "research_schema", "strategy_version", "total_observations",
            "directional_observations", "settled_directional_observations",
            "costed_spot_proxy_observations", "pending_directional_observations",
            "incomplete_directional_observations", "long_observations",
            "short_observations", "validation_status", "holdout_sample_available_for_review",
            "release_gate_eligible", "overlapping_signal_horizons", "portfolio_pnl",
        )
        summary = {key: payload[key] for key in fields if key in payload}
        report_groups: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("spot_cost_proxy_all", ("mean_net_return_bps", "median_net_return_bps",
                                     "profit_factor", "win_rate_pct", "worst_net_return_bps",
                                     "sample_count")),
            ("margin_cost_diagnostics", ("status", "actual_margin_fee_evidence_available",
                                         "margin_net_return_calculated")),
            ("forward_timing_diagnostics", ("status", "sample_count", "invalid_timestamp_count")),
        )
        for group, metric_keys in report_groups:
            value = payload.get(group)
            if isinstance(value, dict):
                summary[group] = {key: value[key] for key in metric_keys if key in value}
    else:
        # Scenario reports evolve; keep short scalar metrics and validation flags,
        # without copying large nested sample collections into repeated console lines.
        summary = {}
        for key, value in payload.items():
            if key in {"cycle_id", "observation_id", "limitations"}:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                if not isinstance(value, str) or len(value) <= 100:
                    summary[key] = value
            elif isinstance(value, dict):
                preferred = {
                    name: item for name, item in value.items()
                    if name in {
                        "status", "sample_count", "mean_net_return_bps", "profit_factor",
                        "win_rate_pct", "release_gate_eligible", "validation_status",
                    }
                }
                if preferred:
                    summary[key] = preferred
            if len(summary) >= 24:
                break
    summary["console_view"] = "unchanged_report"
    summary["report_fingerprint"] = signature[:12]
    return summary


def _requires_full_console_record(event: str, payload: dict[str, Any]) -> bool:
    if event in _ALWAYS_FULL_EVENTS:
        return True
    if event in _REPORT_EVENTS:
        if event == "WALLET_BALANCE_RECONCILIATION_SUMMARY":
            return bool(payload.get("mismatches", 0) or payload.get("evidence_gaps"))
        return False
    if any(token in event for token in (
        "FAILED", "ERROR", "MISMATCH", "BLOCKED", "REJECTED", "AMBIGUOUS",
        "RECOVERED", "INCOMPLETE", "INVALID",
    )):
        return True
    if event == "KRAKEN_HISTORY_SYNC":
        return bool(payload.get("last_error") or payload.get("quality_gap_count", 0))
    if event == "KRAKEN_API_KEY_PERMISSION_CHECK":
        return (
            payload.get("api_key_metadata_complete") is not True
            or payload.get("api_key_expired") is True
            or payload.get("api_key_expiry_unknown") is True
            or payload.get("historical_query_window_restricted") is True
            or payload.get("historical_query_window_unknown") is True
            or bool(payload.get("missing_required_query_permissions"))
            or bool(payload.get("missing_order_write_permissions"))
        )
    if event == "KRAKEN_TRADE_LEDGER_RECONCILIATION":
        return (
            payload.get("status") not in {"COMPLETE", "OK", "RECONCILED"}
            or bool(payload.get("missing_ledger_references", 0))
            or bool(payload.get("missing_trade_references", 0))
            or bool(payload.get("ledger_quality_gap_count", 0))
            or bool(payload.get("trade_quality_gap_count", 0))
            or bool(payload.get("api_errors"))
        )
    return False


class AuditLogger:
    """Write full redacted audit records to JSONL and a concise view to stdout."""

    def __init__(
        self,
        path: str = "/data/logs/events.jsonl",
        *,
        stream: TextIO | None = None,
        max_bytes: int = 5_000_000,
        backup_count: int = 5,
    ) -> None:
        self.stream = stream or sys.stdout
        self.file_handler = None
        self._console_report_fingerprints: dict[str, str] = {}
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self.file_handler = RotatingFileHandler(
                target, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
        except OSError:
            self.file_handler = None

    def _console_record(self, record: dict[str, Any]) -> dict[str, Any]:
        event = str(record["event"])
        payload = record["payload"]
        if _requires_full_console_record(event, payload):
            return record

        if event in _REPORT_EVENTS:
            signature = _report_signature(payload)
            report_key = event
            if event == "OHLCV_BACKTEST_SCENARIO_REPORT":
                report_key = f"{event}:{payload.get('pair', 'unknown')}"
            previous_signature = self._console_report_fingerprints.get(report_key)
            self._console_report_fingerprints[report_key] = signature
            if previous_signature == signature:
                compact_record = dict(record)
                compact_record["payload"] = _unchanged_report_payload(event, payload, signature)
                return compact_record
            return record

        compact_payload = _compact_payload(event, payload)
        if compact_payload is payload:
            return record
        compact_record = dict(record)
        compact_record["payload"] = compact_payload
        return compact_record

    def emit(self, event: str, **payload: Any) -> dict[str, Any]:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "app_version": __version__,
            "strategy_version": STRATEGY_VERSION,
            "event": event,
            "payload": redact(payload),
        }
        # The file archive stays complete; the stdout view is presentation only.
        line = json.dumps(record, separators=(",", ":"), sort_keys=True, default=_json_default)
        console_record = self._console_record(record)
        console_line = json.dumps(
            console_record, separators=(",", ":"), sort_keys=True, default=_json_default
        )
        print(console_line, file=self.stream, flush=True)
        if self.file_handler is not None:
            log_record = logging.LogRecord(
                "kraken_trader_v2", logging.INFO, "", 0, line, (), None
            )
            if self.file_handler.shouldRollover(log_record):
                self.file_handler.doRollover()
            self.file_handler.emit(log_record)
        return record

    def close(self) -> None:
        if self.file_handler is not None:
            self.file_handler.close()
            self.file_handler = None
