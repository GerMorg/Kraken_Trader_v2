from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import time
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from app import STRATEGY_VERSION
from app.audit import AuditLogger
from app.config import INTERNAL_LIVE_EXECUTION_RELEASED, Settings
from app.costs import cost_diagnostic, estimate_round_trip_cost, select_costed_plan, taker_fee_from_trade_volume
from app.kraken import (
    KrakenAPIError, KrakenAmbiguousError, KrakenClient,
    api_key_permission_gate_reasons,
)
from app.execution import LiveExecutionBlocked, OrderAuthority, OrderIntent, signal_decision_id
from app.entry_guards import entry_pair_exposure
from app.history_sync import history_sources_ready, reconcile_trade_ledger_coverage, sync_history_source
from app.market import closed_bar_is_fresh, executable_reference_price, parse_ohlc_rows, parse_tickers, quote_is_fresh, usd_crypto_universe, volume_for_notional_usd
from app.models import Action, RiskSnapshot
from app.persistence import Persistence
from app.position_management import evaluate_position_exits
from app.research import (
    backfill_target_open_outcomes,
    observation_id,
    run_ohlcv_backtest_scenarios,
    settle_due_observations,
    summarize_shadow_research,
)
from app.portfolio import aggregate_position_exposure
from app.portfolio_reconciliation import (
    assess_entry_reconciliation,
    reconcile_positions_to_fills,
    reconcile_wallet_balance_deltas,
)
from app.reconciliation import reconcile_order_intents
from app.risk import choose_trade_leverage, evaluate_entry, size_position_usd
from app.strategy import generate_trend_signal
from app.tax import AustrianTaxReports

D=Decimal

def _decimal(value: Any) -> Decimal | None:
    try:
        result=D(str(value))
        return result if result.is_finite() else None
    except (ArithmeticError,TypeError,ValueError):
        return None

def _external_flow_adjustment(
    ledger_events: list[dict[str, Any]],
    start_epoch: Decimal | None,
    end_epoch: Decimal,
) -> dict[str, Any]:
    """Value external wallet flows only when the ledger denomination is USD-like.

    Unknown non-USD transfers trigger a transparent baseline rebase instead of
    pretending their value is zero or blocking entries forever.
    """
    result: dict[str, Any] = {
        "usd_delta": D(0),
        "complete": True,
        "ledger_count": 0,
        "unvalued_count": 0,
        "unvalued_assets": [],
    }
    if start_epoch is None:
        return result

    for entry in ledger_events:
        if not isinstance(entry, dict):
            continue
        envelope = entry.get("raw")
        raw = envelope.get("raw") if isinstance(envelope, dict) else None
        if not isinstance(raw, dict):
            raw = {}
        timestamp = _decimal(raw.get("time"))
        if timestamp is None:
            raw_ts = str(entry.get("ts", "")).strip()
            try:
                parsed = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                timestamp = D(str(parsed.timestamp()))
            except (TypeError, ValueError, OverflowError):
                continue
        if timestamp <= start_epoch or timestamp > end_epoch:
            continue
        event_type = str(entry.get("event_type") or raw.get("type") or "").strip().lower()
        if event_type not in {"deposit", "withdrawal"}:
            continue
        result["ledger_count"] += 1
        asset = str(entry.get("asset") or raw.get("asset") or "").strip().upper()
        amount = _decimal(entry.get("amount", raw.get("amount")))
        fee = _decimal(entry.get("fee", raw.get("fee", "0")))
        if amount is None or fee is None or fee < 0:
            result["complete"] = False
            result["unvalued_count"] += 1
            if asset and asset not in result["unvalued_assets"]:
                result["unvalued_assets"].append(asset)
            continue
        if asset not in {"ZUSD", "USD", "USDC"}:
            result["complete"] = False
            result["unvalued_count"] += 1
            if asset and asset not in result["unvalued_assets"]:
                result["unvalued_assets"].append(asset)
            continue
        # Kraken ledger amount is signed; fee is an additional debit.
        result["usd_delta"] += amount - fee

    result["usd_delta"] = D(str(result["usd_delta"]))
    return result


def _resolve_clean_reconciliation_breaker(
    persistence: Persistence,
    audit: AuditLogger,
    *,
    name: str,
    cycle_id: str,
    verified_by: str,
    clean: bool,
) -> bool:
    """Resolve only the current breaker from this reconciliation subsystem after a clean pass."""
    if not clean:
        return False
    recovered = False
    for breaker in persistence.circuit_breakers():
        if breaker.get("name") != name:
            continue
        reason = str(breaker.get("reason", "")).strip()
        if not reason:
            continue
        detail = {
            "cycle_id": cycle_id,
            "recovery_method": "SUCCESSFUL_CURRENT_RECONCILIATION",
            "verified_by": verified_by,
            "recovered_reason": reason,
        }
        if persistence.resolve_circuit_breaker(name, reason, detail):
            audit.emit(
                "CIRCUIT_BREAKER_RECOVERED",
                cycle_id=cycle_id,
                name=name,
                previous_reason=reason,
                recovery_method="SUCCESSFUL_CURRENT_RECONCILIATION",
                verified_by=verified_by,
            )
            recovered = True
    return recovered


def _recover_transient_public_network_breaker(
    persistence: Persistence,
    audit: AuditLogger,
    *,
    cycle_id: str,
    system_status: str,
    instruments_count: int,
    initial_ticker_count: int,
    refreshed_ticker_count: int,
    credentials_present: bool,
    api_permissions_ready: bool,
    history_ready_for_entries: bool,
    history_evidence_gaps: list[str],
    account_snapshot_available: bool,
) -> bool:
    """Resolve only a stale public-network breaker after a successful account-read cycle.

    This narrowly handles KRAKEN_PUBLIC_NETWORK_ERROR. Auth, history, account,
    reconciliation, cost, strategy and other risk gates remain independently active.
    """
    checks = {
        "system_status_online": str(system_status).lower() == "online",
        "usd_instruments_available": instruments_count > 0,
        "initial_ticker_snapshot_nonempty": initial_ticker_count > 0,
        "refreshed_ticker_snapshot_nonempty": refreshed_ticker_count > 0,
        "api_credentials_present": credentials_present,
        "private_api_permissions_ready": api_permissions_ready,
        "history_ready_for_entries": history_ready_for_entries,
        "history_evidence_gaps_empty": not history_evidence_gaps,
        "account_snapshot_available": account_snapshot_available,
    }
    if not all(checks.values()):
        return False

    for breaker in persistence.circuit_breakers():
        if (
            breaker.get("name") != "KRAKEN_API"
            or breaker.get("reason") != "KRAKEN_PUBLIC_NETWORK_ERROR"
        ):
            continue
        verified_checks = [name for name, passed in checks.items() if passed]
        recovery_detail = {
            "cycle_id": cycle_id,
            "recovery_method": "SUCCESSFUL_PUBLIC_AND_ACCOUNT_READ_CYCLE",
            "verified_checks": verified_checks,
        }
        if persistence.resolve_circuit_breaker(
            "KRAKEN_API",
            "KRAKEN_PUBLIC_NETWORK_ERROR",
            recovery_detail,
        ):
            audit.emit(
                "CIRCUIT_BREAKER_RECOVERED",
                cycle_id=cycle_id,
                name="KRAKEN_API",
                previous_reason="KRAKEN_PUBLIC_NETWORK_ERROR",
                recovery_method="SUCCESSFUL_PUBLIC_AND_ACCOUNT_READ_CYCLE",
                verified_checks=verified_checks,
                other_entry_gates_remain_in_force=True,
            )
            return True
    return False


def _open_position_rows(response: dict[str,Any]) -> dict[str,dict[str,Any]]:
    wrapped=response.get("open")
    source=wrapped if isinstance(wrapped,dict) else response
    return {str(key):row for key,row in source.items()
            if isinstance(row,dict) and "pair" in row and "type" in row}


def _exchange_order_rows(response: Any, key: str) -> dict[str, dict[str, Any]] | None:
    if not isinstance(response, dict):
        return None
    rows = response.get(key)
    if not isinstance(rows, dict):
        return None
    if any(not str(order_id).strip() or not isinstance(row, dict)
           for order_id, row in rows.items()):
        return None
    return {str(order_id): row for order_id, row in rows.items()}


def _circuit_breaker_diagnostics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose actionable breaker causes without dumping arbitrary persisted detail."""
    diagnostics: list[dict[str, Any]] = []
    for row in rows:
        detail = row.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        item: dict[str, Any] = {
            "name": str(row.get("name", "UNKNOWN")),
            "reason": str(row.get("reason", "UNKNOWN")),
            "ts": row.get("ts"),
        }
        for key in (
            "cycle_id", "status", "code", "exception_type", "snapshot_id", "evidence_gaps",
        ):
            if key in detail:
                item[key] = detail[key]
        asset_results = detail.get("asset_results")
        if isinstance(asset_results, list):
            item["asset_result_count"] = len(asset_results)
            mismatches = [
                {
                    "asset": result.get("asset"),
                    "status": result.get("status"),
                    "difference": result.get("difference"),
                }
                for result in asset_results
                if isinstance(result, dict)
                and str(result.get("status", "")) not in {"MATCHED", "BALANCE_STABLE"}
            ]
            if mismatches:
                item["mismatched_assets"] = mismatches[:20]
        if not any(key in item for key in (
            "cycle_id", "status", "code", "exception_type", "snapshot_id",
            "evidence_gaps", "asset_result_count",
        )) and detail:
            item["detail_keys"] = sorted(str(key) for key in detail)[:20]
        diagnostics.append(item)
    return diagnostics


class TraderRuntime:
    def __init__(self,settings: Settings,persistence: Persistence,audit: AuditLogger) -> None:
        self.settings,self.persistence,self.audit=settings,persistence,audit
        self.client=KrakenClient(settings.api_key,settings.api_secret,persistence.next_nonce)
        self.tax_reports=AustrianTaxReports()
        self._last_tax_report_signature: dict[int, tuple[str, ...]] = {}

    @staticmethod
    def _order_intent_from_mapping(intent: dict[str, Any]) -> OrderIntent:
        """Convert an audited runtime intent into the typed execution contract."""
        return OrderIntent(
            intent_id=str(intent["intent_id"]),
            decision_id=str(intent["decision_id"]),
            client_order_id=str(intent["client_order_id"]),
            pair=str(intent["pair"]),
            action=Action(str(intent["action"])),
            side=str(intent["side"]).upper(),
            volume=D(str(intent["volume"])),
            product_type=str(intent["product_type"]).upper(),
            leverage=int(intent["leverage"]) if intent.get("leverage") is not None else None,
            decision_evidence=dict(intent.get("decision_evidence", {})),
        )

    def _validate_shadow_order(self, intent: dict[str, Any], instrument: Any) -> dict[str, Any]:
        """Ask Kraken to validate a proposed order without placing a live order."""
        authority = OrderAuthority(self.client, self.persistence, self.settings, self.audit)
        return authority.execute(
            self._order_intent_from_mapping(intent),
            instrument,
            prechecks_passed=True,
            validate_only=True,
        )

    def _fetch_fresh_ticker(self, instrument: Any) -> Any | None:
        """Fetch this market's executable quote immediately before entry evaluation."""
        response = self.client.public("Ticker", {"pair": instrument.altname})
        return parse_tickers(response, [instrument]).get(instrument.pair_id)

    def _account_risk_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        reconciliation_ok: bool = False,
        equity_risk_state: dict[str, Any] | None = None,
    ) -> RiskSnapshot:
        trade=snapshot["trade_balance"]; rows=_open_position_rows(snapshot["open_positions"])
        orders=snapshot["open_orders"].get("open",{}); balance=snapshot["balance"]
        equity_value=_decimal(trade.get("e"))
        free_value=_decimal(trade.get("mf"))
        used_value=_decimal(trade.get("m"))
        equity=equity_value if equity_value is not None else D(0)
        free=free_value if free_value is not None else D(0)
        used=used_value if used_value is not None else D(0)
        margin_level=_decimal(trade.get("ml"))
        captured_at = _decimal(snapshot.get("captured_at"))
        snapshot_age = (
            D(str(time.time())) - captured_at
            if captured_at is not None and captured_at >= 0
            else D("Infinity")
        )
        exposure=aggregate_position_exposure(rows)
        gross,net=exposure.gross_usd,exposure.net_usd
        complete=bool(
            equity_value is not None and free_value is not None and used_value is not None
            and captured_at is not None and snapshot_age.is_finite() and snapshot_age >= 0
            and isinstance(orders,dict) and isinstance(balance,dict)
            and isinstance(snapshot.get("closed_orders"), dict)
            and isinstance(trade, dict) and exposure.complete
            and all("vol" in row and "pair" in row and "posstatus" in row for row in rows.values())
        )
        daily_pnl = (
            _decimal(equity_risk_state.get("daily_pnl_pct"))
            if equity_risk_state is not None else None
        )
        drawdown = (
            _decimal(equity_risk_state.get("drawdown_pct"))
            if equity_risk_state is not None else None
        )
        return RiskSnapshot(
            equity, free, used, gross, net, len(rows), daily_pnl, drawdown,
            margin_level, snapshot_age, complete, bool(reconciliation_ok),
        )

    def _refresh_tax_report(self) -> None:
        if not self.settings.tax_report_enabled:
            return

        # Austrian tax years follow Europe/Vienna. Imported history can span many years;
        # producing only the current-year report silently left historical years uneported.
        current_year = datetime.now(ZoneInfo("Europe/Vienna")).year
        years = sorted(set(self.persistence.tax_event_years()) | {current_year})
        for year in years:
            events = self.persistence.tax_events_for_year(year)
            # Event IDs/counts alone are insufficient: reconciliations may enrich an
            # existing event with fees, EUR valuation, ledger links, or review status.
            # Canonical full-record fingerprints ensure those corrections regenerate
            # the exported files without depending on database row order.
            signature = tuple(sorted(
                json.dumps(
                    dict(event),
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                    separators=(",", ":"),
                )
                for event in events
            ))
            if self._last_tax_report_signature.get(year) == signature:
                continue
            try:
                result = self.tax_reports.generate_year(
                    year,
                    events,
                    source_coverage_complete=False,
                )
            except (OSError, ValueError) as exc:
                self.audit.emit(
                    "TAX_REPORT_FAILED",
                    year=year,
                    exception_type=type(exc).__name__,
                    source_coverage_complete=False,
                )
                continue
            self._last_tax_report_signature[year] = signature
            self.audit.emit(
                "TAX_REPORT_GENERATED",
                year=year,
                event_count=result["event_count"],
                status=result["status"],
                source_coverage_complete=False,
            )

    def cycle(self) -> dict[str,Any]:
        cycle_id=str(uuid.uuid4()); started=time.time()
        counts={"universe":0,"candidates":0,"signals":0,"intents":0,"blocked":0,"position_exits_checked":0,"position_reductions_recommended":0,"real_orders_sent":0,"research_observations_recorded":0,"research_outcomes_settled":0,"research_outcomes_incomplete":0}
        history_ready_for_entries = False
        history_evidence_gaps = ["ACCOUNT_HISTORY_NOT_CHECKED"]
        api_permissions_ready = False
        api_permission_gaps: list[str] = ["API_KEY_PERMISSION_CHECK_NOT_COMPLETED"]
        history_metadata_untrusted = False
        history_window_unknown = False
        closed_history_snapshot_allowed = False
        wallet_reconciliation: dict[str, Any] | None = None
        order_reconciliation: dict[str, Any] | None = None
        position_reconciliation: dict[str, Any] | None = None
        self.audit.emit("CYCLE_START",cycle_id=cycle_id,mode=self.settings.mode)
        try:
            self._refresh_tax_report()
            status=str(self.client.public("SystemStatus").get("status","unknown")).lower()
            if status!="online":
                self.persistence.trip_circuit_breaker("KRAKEN_STATUS","KRAKEN_NOT_ONLINE",{"status":status})
                self.audit.emit("CYCLE_BLOCKED",cycle_id=cycle_id,reason="KRAKEN_NOT_ONLINE",status=status)
                return {"cycle_id":cycle_id,"status":"BLOCKED","reason":"KRAKEN_NOT_ONLINE"}
            instruments=usd_crypto_universe(self.client.public("AssetPairs")); counts["universe"]=len(instruments)
            if not instruments:
                self.persistence.trip_circuit_breaker("MARKET_UNIVERSE","NO_VALID_USD_MARKETS")
                return {"cycle_id":cycle_id,"status":"BLOCKED","reason":"NO_VALID_USD_MARKETS"}
            tickers=parse_tickers(self.client.public("Ticker"),instruments)
            initial_ticker_count = len(tickers)
            ticker_observed_at = time.time()
            ticker_times = {pair_id: ticker_observed_at for pair_id in tickers}
            ranked=sorted((i for i in instruments if i.pair_id in tickers),
                key=lambda i:tickers[i.pair_id].volume_quote_24h,reverse=True)
            candidates=ranked[:self.settings.max_market_candidates]; counts["candidates"]=len(candidates)
            account=None; snapshot=None; trade_volume={}
            if self.settings.credentials_present:
                # GetApiKeyInfo is explicitly permissioned for none of the data scopes.
                # Use an allowlisted assessment: the API response includes the API key
                # itself and must never be copied to an audit event.
                try:
                    key_assessment = self.client.api_key_permission_assessment()
                except KrakenAPIError as exc:
                    # This preflight runs before any account/history endpoint and no order
                    # can be sent. Keep known key/network preflight failures retryable:
                    # a corrected key or recovered API must not be trapped by a stale
                    # persistent generic API circuit breaker.
                    result = {
                        "cycle_id": cycle_id,
                        "status": "BLOCKED",
                        "reason": "API_KEY_PERMISSION_CHECK_FAILED",
                        "permission_error_code": exc.code,
                        "real_orders_sent": 0,
                    }
                    self.audit.emit(
                        "API_KEY_PERMISSION_CHECK_FAILED",
                        cycle_id=cycle_id,
                        code=exc.code,
                        entries_blocked=True,
                        existing_position_management_continues=False,
                        next_cycle_retries_preflight=True,
                    )
                    self.persistence.record_event("CYCLE_SUMMARY", result)
                    self.audit.emit("CYCLE_SUMMARY", **result)
                    return result
                self.audit.emit(
                    "KRAKEN_API_KEY_PERMISSION_CHECK",
                    cycle_id=cycle_id,
                    **key_assessment,
                )
                api_permission_gaps = api_key_permission_gate_reasons(
                    key_assessment,
                    validate_shadow_orders=self.settings.validate_shadow_orders,
                    live_requested=self.settings.live_requested,
                )
                api_permissions_ready = not api_permission_gaps
                history_metadata_untrusted = bool(
                    key_assessment.get("api_key_expired")
                    or key_assessment.get("historical_query_window_restricted")
                )
                history_window_unknown = bool(
                    key_assessment.get("api_key_expiry_unknown")
                    or key_assessment.get("historical_query_window_unknown")
                )
                if key_assessment.get("api_key_metadata_complete") is not True:
                    self.audit.emit(
                        "KRAKEN_API_KEY_METADATA_INCOMPLETE",
                        cycle_id=cycle_id,
                        missing_fields=key_assessment.get("api_key_metadata_fields_missing", []),
                        expiry_unknown=key_assessment.get("api_key_expiry_unknown", True),
                        history_window_unknown=key_assessment.get("historical_query_window_unknown", True),
                        history_api_probe_enabled=True,
                        new_risk_requires_complete_history=True,
                    )
                if not api_permissions_ready:
                    private_read_access_invalid = any(
                        reason in api_permission_gaps
                        for reason in (
                            "REQUIRED_QUERY_PERMISSION_MISSING",
                            "API_KEY_PERMISSIONS_UNKNOWN",
                            "API_KEY_ASSESSMENT_NOT_READY",
                        )
                    )
                    self.audit.emit(
                        "API_KEY_PERMISSION_GATE_BLOCKED",
                        cycle_id=cycle_id,
                        reasons=api_permission_gaps,
                        existing_position_management_continues=not private_read_access_invalid,
                        operator_action=(
                            "Correct missing scopes or explicit key restrictions; missing optional metadata "
                            "is checked by querying the actual history endpoints."
                        ),
                    )
                    if private_read_access_invalid:
                        # Without required read scopes, current account/position data cannot be trusted.
                        result = {
                            "cycle_id": cycle_id,
                            "status": "BLOCKED",
                            "reason": "API_KEY_PRIVATE_READ_ACCESS_INVALID",
                            "permission_gaps": api_permission_gaps,
                            "real_orders_sent": 0,
                        }
                        self.persistence.record_event("CYCLE_SUMMARY", result)
                        self.audit.emit("CYCLE_SUMMARY", **result)
                        return result
                history_summaries=[]
                if history_metadata_untrusted:
                    history_ready_for_entries = False
                    history_evidence_gaps = list(api_permission_gaps)
                    history_evidence_gaps.append("ACCOUNT_HISTORY_SYNC_SKIPPED_EXPLICIT_KEY_RESTRICTION")
                    self.audit.emit(
                        "KRAKEN_HISTORY_SYNC_SKIPPED",
                        cycle_id=cycle_id,
                        sources=["TRADES", "LEDGERS", "CLOSED_ORDERS"],
                        reasons=api_permission_gaps,
                        explicit_key_restriction=True,
                        existing_position_management_continues=True,
                        new_risk_allowed=False,
                    )
                else:
                    for history_source in ("TRADES", "LEDGERS", "CLOSED_ORDERS"):
                        try:
                            history_summary=sync_history_source(
                                client=self.client,
                                persistence=self.persistence,
                                source=history_source,
                            )
                        except KrakenAPIError as exc:
                            state = self.persistence.history_sync_state(history_source)
                            if state is not None:
                                state["generation_complete"] = False
                                state["last_error"] = exc.code
                                self.persistence.save_history_sync_state(state)
                            history_summary = {
                                "source": history_source,
                                "pages": 0,
                                "items_saved": 0,
                                "expected_count": state.get("expected_count") if state else None,
                                "next_offset": state.get("next_offset", 0) if state else 0,
                                "generation_complete": False,
                                "initial_backfill_complete": bool(
                                    state.get("initial_backfill_complete", False) if state else False
                                ),
                                "cached_until_refresh": False,
                                "last_error": exc.code,
                                "quality_gaps": list(state.get("quality_gaps", [])) if state else [],
                                "quality_gap_count": len(state.get("quality_gaps", [])) if state else 0,
                                "quality_gap_types": sorted({
                                    str(gap).split(":", 1)[0]
                                    for gap in (state.get("quality_gaps", []) if state else [])
                                }),
                            }
                            self.audit.emit(
                                "KRAKEN_HISTORY_SYNC_FAILED",
                                cycle_id=cycle_id,
                                source=history_source,
                                code=exc.code,
                                retry_next_cycle=True,
                                existing_position_monitoring_continues=True,
                                new_risk_allowed=False,
                            )
                        history_summaries.append(history_summary)
                        self.audit.emit(
                            "KRAKEN_HISTORY_SYNC",
                            cycle_id=cycle_id,
                            **history_summary,
                        )
                    coverage_summary = reconcile_trade_ledger_coverage(
                        client=self.client,
                        persistence=self.persistence,
                    )
                    self.audit.emit(
                        "KRAKEN_TRADE_LEDGER_RECONCILIATION",
                        cycle_id=cycle_id,
                        **coverage_summary,
                    )
                    # Targeted queries may correct fill/ledger records or reopen the
                    # full ledger backfill. Refresh summaries before deciding on risk.
                    for source_summary in history_summaries:
                        source_name = str(source_summary.get("source", "")).upper()
                        saved_state = self.persistence.history_sync_state(source_name)
                        if saved_state is None:
                            continue
                        source_summary["next_offset"] = saved_state.get("next_offset", 0)
                        source_summary["expected_count"] = saved_state.get("expected_count")
                        source_summary["initial_backfill_complete"] = bool(
                            saved_state.get("initial_backfill_complete", False)
                        )
                        source_summary["generation_complete"] = bool(
                            saved_state.get("generation_complete", False)
                        )
                        source_summary["last_error"] = saved_state.get("last_error")
                        saved_gaps = saved_state.get("quality_gaps", [])
                        source_summary["quality_gaps"] = (
                            list(saved_gaps) if isinstance(saved_gaps, list) else []
                        )
                        source_summary["quality_gap_count"] = len(source_summary["quality_gaps"])
                        source_summary["quality_gap_types"] = sorted({
                            str(gap).split(":", 1)[0]
                            for gap in source_summary["quality_gaps"]
                        })
                    history_ready_for_entries, history_evidence_gaps = history_sources_ready(
                        history_summaries
                    )
                    closed_history_snapshot_allowed = any(
                        item.get("source") == "CLOSED_ORDERS" and not item.get("last_error")
                        for item in history_summaries
                    )
                self.audit.emit(
                    "KRAKEN_HISTORY_RECONCILED",
                    cycle_id=cycle_id,
                    history_ready_for_entries=history_ready_for_entries,
                    evidence_gaps=history_evidence_gaps,
                    source_summaries=[
                        {
                            "source": item.get("source"),
                            "initial_backfill_complete": item.get("initial_backfill_complete"),
                            "generation_complete": item.get("generation_complete"),
                            "last_error": item.get("last_error"),
                            "quality_gap_count": item.get("quality_gap_count", 0),
                            "quality_gap_types": item.get("quality_gap_types", []),
                        }
                        for item in history_summaries
                    ],
                )
                if not history_ready_for_entries:
                    self.audit.emit(
                        "NEW_RISK_BLOCKED_HISTORY_INCOMPLETE",
                        cycle_id=cycle_id,
                        evidence_gaps=history_evidence_gaps,
                        source_summaries=[
                            {
                                "source": item.get("source"),
                                "initial_backfill_complete": item.get("initial_backfill_complete"),
                                "generation_complete": item.get("generation_complete"),
                                "last_error": item.get("last_error"),
                                "quality_gap_count": item.get("quality_gap_count", 0),
                                "quality_gap_types": item.get("quality_gap_types", []),
                            }
                            for item in history_summaries
                        ],
                        existing_position_management_continues=True,
                    )
                # Keep the previous snapshot as an immutable delta baseline before
                # persisting the just-fetched account state.
                previous_account_snapshot = self.persistence.latest_account_snapshot()
                # Fetch the account/position snapshot after history network calls so
                # the risk gate uses a genuinely recent snapshot.
                account = self.client.account_snapshot(
                    include_closed_orders=(
                        not history_metadata_untrusted
                        and (not history_window_unknown or closed_history_snapshot_allowed)
                    )
                )
                persisted_snapshot = self.persistence.record_account_snapshot(account)
                self.audit.emit(
                    "ACCOUNT_SNAPSHOT_PERSISTED",
                    cycle_id=cycle_id,
                    **persisted_snapshot,
                )
                _resolve_clean_reconciliation_breaker(
                    self.persistence, self.audit,
                    name="ACCOUNT_SNAPSHOT", cycle_id=cycle_id,
                    verified_by="CURRENT_ACCOUNT_SNAPSHOT",
                    clean=persisted_snapshot.get("positions_shape_complete") is True,
                )
                previous_snapshot_time = _decimal(
                    previous_account_snapshot.get("captured_at")
                ) if previous_account_snapshot is not None else None
                if previous_account_snapshot is None:
                    wallet_reconciliation = reconcile_wallet_balance_deltas(
                        None, account, [], ledger_history_complete=False,
                    )
                elif previous_snapshot_time is None:
                    wallet_reconciliation = {
                        "status": "PREVIOUS_SNAPSHOT_TIMESTAMP_INVALID",
                        "complete": False,
                        "full_portfolio_reconciliation": False,
                        "assets_checked": 0,
                        "assets_matched_to_latest_ledger_balance": 0,
                        "assets_stable_without_ledger_activity": 0,
                        "mismatches": 0,
                        "evidence_gaps": ["PREVIOUS_SNAPSHOT_TIMESTAMP_INVALID"],
                        "asset_results": [],
                    }
                elif history_metadata_untrusted or history_window_unknown:
                    # Do not make a second overlapping historical query when key restrictions
                    # are explicit or the range metadata is absent. Keep the wallet delta
                    # reconciliation incomplete even if some prior ledger rows are available.
                    wallet_reconciliation = reconcile_wallet_balance_deltas(
                        previous_account_snapshot,
                        account,
                        self.persistence.ledger_events(),
                        ledger_history_complete=False,
                    )
                else:
                    # A second, bounded ledger query after Balance() is needed: a
                    # pre-snapshot ledger query alone cannot cover movements that
                    # occurred while account endpoints were being read.
                    ledger_window = sync_history_source(
                        client=self.client,
                        persistence=self.persistence,
                        source="LEDGERS",
                        page_budget=10,
                        now_epoch=int(time.time()),
                        force_refresh=True,
                        start_epoch_override=int(previous_snapshot_time),
                    )
                    ledger_history_complete = bool(
                        ledger_window.get("initial_backfill_complete")
                        and ledger_window.get("generation_complete")
                        and ledger_window.get("last_error") is None
                    )
                    self.audit.emit(
                        "KRAKEN_LEDGER_WINDOW_SYNC",
                        cycle_id=cycle_id,
                        **ledger_window,
                        reconciliation_window_start=str(previous_snapshot_time),
                    )
                    wallet_reconciliation = reconcile_wallet_balance_deltas(
                        previous_account_snapshot,
                        account,
                        self.persistence.ledger_events(),
                        ledger_history_complete=ledger_history_complete,
                    )
                if wallet_reconciliation.get("mismatches", 0) > 0:
                    self.persistence.trip_circuit_breaker(
                        "WALLET_BALANCE_RECONCILIATION",
                        "WALLET_BALANCE_MISMATCH",
                        {
                            "cycle_id": cycle_id,
                            "status": wallet_reconciliation.get("status"),
                            "evidence_gaps": wallet_reconciliation.get("evidence_gaps", []),
                            "asset_results": wallet_reconciliation.get("asset_results", []),
                        },
                    )
                if not persisted_snapshot["positions_shape_complete"]:
                    self.persistence.trip_circuit_breaker(
                        "ACCOUNT_SNAPSHOT",
                        "POSITION_SNAPSHOT_SHAPE_INVALID",
                        {"cycle_id": cycle_id, "snapshot_id": persisted_snapshot["snapshot_id"]},
                    )
                if history_summaries and not all(
                    item.get("initial_backfill_complete") is True for item in history_summaries
                ):
                    self.audit.emit(
                        "KRAKEN_HISTORY_BACKFILL_INCOMPLETE",
                        cycle_id=cycle_id,
                        sources=[
                            {
                                "source": item["source"],
                                "next_offset": item["next_offset"],
                                "expected_count": item["expected_count"],
                                "last_error": item["last_error"],
                            }
                            for item in history_summaries
                        ],
                        reconciliation_complete=False,
                    )
                self._refresh_tax_report()
                # The original ticker snapshot predates the history backfill and private
                # account calls above. Refresh it before deriving position-exit marks.
                refreshed_tickers = parse_tickers(self.client.public("Ticker"), instruments)
                tickers = refreshed_tickers
                ticker_observed_at = time.time()
                ticker_times = {pair_id: ticker_observed_at for pair_id in tickers}
                self.audit.emit(
                    "MARKET_TICKERS_REFRESHED",
                    cycle_id=cycle_id,
                    count=len(tickers),
                    captured_at=time.time(),
                )
                if not tickers:
                    self.persistence.trip_circuit_breaker(
                        "MARKET_DATA", "MARKET_TICKER_REFRESH_EMPTY", {"cycle_id": cycle_id}
                    )
                rows = _open_position_rows(account["open_positions"])
                open_orders = _exchange_order_rows(account.get("open_orders"), "open")
                closed_history_included = account.get("closed_orders") is not None
                closed_orders = (
                    _exchange_order_rows(account.get("closed_orders"), "closed")
                    if closed_history_included else None
                )
                if open_orders is None or (closed_history_included and closed_orders is None):
                    self.persistence.trip_circuit_breaker(
                        "ORDER_SNAPSHOT", "ORDER_RESPONSE_SHAPE_INVALID",
                        {"cycle_id": cycle_id},
                    )
                    self.audit.emit(
                        "EXCHANGE_ORDER_SNAPSHOT_INVALID",
                        cycle_id=cycle_id,
                        open_orders_valid=open_orders is not None,
                        closed_orders_valid=(
                            closed_orders is not None if closed_history_included else None
                        ),
                        closed_history_included=closed_history_included,
                    )
                else:
                    for order_id, order in open_orders.items():
                        self.persistence.record_exchange_order(order_id, order, source="OPEN")
                    if closed_history_included and closed_orders is not None:
                        for order_id, order in closed_orders.items():
                            self.persistence.record_exchange_order(order_id, order, source="CLOSED")
                        order_reconciliation = reconcile_order_intents(self.persistence)
                        self.audit.emit(
                            "ORDER_RECONCILIATION_SUMMARY",
                            cycle_id=cycle_id,
                            **order_reconciliation,
                        )
                        _resolve_clean_reconciliation_breaker(
                            self.persistence, self.audit,
                            name="ORDER_RECONCILIATION", cycle_id=cycle_id,
                            verified_by="ORDER_RECONCILIATION_SUMMARY",
                            clean=all(order_reconciliation.get(key, 0) == 0 for key in (
                                "ambiguous_matches", "invalid_exchange_states",
                                "unmatched_open_orders", "unmatched_submitted_intents",
                            )),
                        )
                        position_reconciliation = reconcile_positions_to_fills(self.persistence)
                        self.audit.emit(
                            "POSITION_FILL_RECONCILIATION_SUMMARY",
                            cycle_id=cycle_id,
                            **position_reconciliation,
                        )
                        _resolve_clean_reconciliation_breaker(
                            self.persistence, self.audit,
                            name="POSITION_FILL_RECONCILIATION", cycle_id=cycle_id,
                            verified_by="POSITION_FILL_RECONCILIATION_SUMMARY",
                            clean=position_reconciliation.get("position_fill_reconciliation_complete") is True,
                        )
                    else:
                        self.audit.emit(
                            "ORDER_RECONCILIATION_SKIPPED_HISTORY_UNAVAILABLE",
                            cycle_id=cycle_id,
                            open_orders_recorded=True,
                            closed_orders_included=False,
                            existing_position_monitoring_continues=True,
                            new_risk_allowed=False,
                        )
                self.persistence.record_event("ACCOUNT_SNAPSHOT_CAPTURED", {
                    "open_positions": len(rows),
                    "open_orders": len(open_orders) if open_orders is not None else None,
                    "closed_orders": len(closed_orders) if closed_orders is not None else None,
                    "captured_at": account["captured_at"],
                    "api_queries_succeeded": True,
                    "reconciliation_complete": False,
                    "complete_for_entries": False,
                })
                captured_epoch = _decimal(account.get("captured_at"))
                equity_value = _decimal(
                    account.get("trade_balance", {}).get("e")
                    if isinstance(account.get("trade_balance"), dict) else None
                )
                equity_risk_state: dict[str, Any] | None = None
                flow_diagnostics: dict[str, Any] = {
                    "usd_delta": D(0), "complete": True, "ledger_count": 0,
                    "unvalued_count": 0, "unvalued_assets": [],
                }
                if captured_epoch is not None and equity_value is not None and equity_value > 0:
                    flow_diagnostics = _external_flow_adjustment(
                        self.persistence.ledger_events(),
                        previous_snapshot_time,
                        captured_epoch,
                    )
                    try:
                        local_day = datetime.fromtimestamp(
                            float(captured_epoch), tz=ZoneInfo("Europe/Vienna")
                        ).date().isoformat()
                        equity_risk_state = self.persistence.update_risk_equity_state(
                            equity=equity_value,
                            captured_epoch=captured_epoch,
                            trading_day=local_day,
                            external_flow_usd=flow_diagnostics["usd_delta"],
                            external_flow_valued=(
                                True
                                if previous_account_snapshot is None
                                else (
                                    previous_snapshot_time is not None
                                    and flow_diagnostics["complete"]
                                    and wallet_reconciliation is not None
                                    and wallet_reconciliation.get("complete") is True
                                    and wallet_reconciliation.get("mismatches", 0) == 0
                                    and wallet_reconciliation.get("status") in {
                                        "WALLET_BALANCES_MATCH_LEDGER_WINDOW",
                                        "WALLET_BALANCES_STABLE_NO_LEDGER_ACTIVITY",
                                    }
                                )
                            ),
                        )
                    except (ArithmeticError, TypeError, ValueError, OverflowError) as exc:
                        self.audit.emit(
                            "EQUITY_RISK_BASELINE_FAILED",
                            cycle_id=cycle_id,
                            exception_type=type(exc).__name__,
                            baseline_available=False,
                        )
                    if equity_risk_state is not None:
                        self.audit.emit(
                            "EQUITY_RISK_BASELINE_UPDATED",
                            cycle_id=cycle_id,
                            **equity_risk_state,
                            external_flow_ledger_count=flow_diagnostics["ledger_count"],
                            external_flow_unvalued_count=flow_diagnostics["unvalued_count"],
                            external_flow_unvalued_assets=flow_diagnostics["unvalued_assets"],
                        )
                _resolve_clean_reconciliation_breaker(
                    self.persistence, self.audit,
                    name="WALLET_BALANCE_RECONCILIATION", cycle_id=cycle_id,
                    verified_by="WALLET_BALANCE_RECONCILIATION_SUMMARY",
                    clean=(
                        wallet_reconciliation is not None
                        and wallet_reconciliation.get("complete") is True
                        and wallet_reconciliation.get("mismatches", 0) == 0
                        and wallet_reconciliation.get("status") in {
                            "WALLET_BALANCES_MATCH_LEDGER_WINDOW",
                            "WALLET_BALANCES_STABLE_NO_LEDGER_ACTIVITY",
                        }
                    ),
                )
                active_reconciliation_breakers = [
                    str(row.get("name", ""))
                    for row in self.persistence.circuit_breakers()
                    if row.get("name") in {
                        "WALLET_BALANCE_RECONCILIATION",
                        "ORDER_RECONCILIATION",
                        "POSITION_FILL_RECONCILIATION",
                        "ACCOUNT_SNAPSHOT",
                    }
                ]
                entry_reconciliation = assess_entry_reconciliation(
                    account,
                    persisted_snapshot=persisted_snapshot,
                    wallet_reconciliation=wallet_reconciliation or {},
                    history_ready=history_ready_for_entries,
                    order_reconciliation=order_reconciliation or {},
                    position_reconciliation=position_reconciliation or {},
                    active_reconciliation_breakers=active_reconciliation_breakers,
                    equity_baseline_ready=(
                        equity_risk_state is not None
                        and equity_risk_state.get("daily_pnl_pct") is not None
                        and equity_risk_state.get("drawdown_pct") is not None
                    ),
                    closed_history_included=closed_history_included,
                )
                self.audit.emit(
                    "PORTFOLIO_ENTRY_RECONCILIATION",
                    cycle_id=cycle_id,
                    **entry_reconciliation,
                )
                if wallet_reconciliation is not None:
                    wallet_report = {
                        **wallet_reconciliation,
                        "full_portfolio_reconciliation": False,
                        "complete_for_entries": entry_reconciliation["entry_reconciliation_ok"],
                        "entry_reconciliation_status": entry_reconciliation["status"],
                    }
                    self.audit.emit(
                        "WALLET_BALANCE_RECONCILIATION_SUMMARY",
                        cycle_id=cycle_id,
                        **wallet_report,
                    )
                snapshot = self._account_risk_snapshot(
                    account,
                    reconciliation_ok=bool(entry_reconciliation["entry_reconciliation_ok"]),
                    equity_risk_state=equity_risk_state,
                )
                exit_decisions = evaluate_position_exits(
                    account["open_positions"],
                    tickers,
                    {item.pair_id: item for item in instruments},
                    take_profit_pct=self.settings.position_take_profit_pct,
                    take_profit_fraction=self.settings.position_take_profit_fraction,
                    stop_loss_pct=self.settings.position_stop_loss_pct,
                )
                counts["position_exits_checked"] = len(exit_decisions)
                for decision in exit_decisions:
                    if decision.get("action") in {"REDUCE_LONG", "REDUCE_SHORT", "CLOSE_LONG", "CLOSE_SHORT"}:
                        counts["position_reductions_recommended"] += 1
                    self.persistence.record_event("POSITION_MANAGEMENT_DECISION", {
                        "cycle_id": cycle_id,
                        **decision,
                    })
                    self.audit.emit(
                        "POSITION_MANAGEMENT_DECISION",
                        cycle_id=cycle_id,
                        **decision,
                    )
                trade_volume = (
                    self.client.trade_volume([i.altname for i in candidates])
                    if api_permissions_ready and history_ready_for_entries else {}
                )
            else:
                self.audit.emit("ACCOUNT_UNAVAILABLE_PUBLIC_ONLY",reason="API_CREDENTIALS_NOT_CONFIGURED")
            recovered_transient_public_network_breaker = _recover_transient_public_network_breaker(
                self.persistence,
                self.audit,
                cycle_id=cycle_id,
                system_status=status,
                instruments_count=len(instruments),
                initial_ticker_count=initial_ticker_count,
                refreshed_ticker_count=len(tickers),
                credentials_present=self.settings.credentials_present,
                api_permissions_ready=api_permissions_ready,
                history_ready_for_entries=history_ready_for_entries,
                history_evidence_gaps=history_evidence_gaps,
                account_snapshot_available=account is not None,
            )
            active_breaker_rows = self.persistence.circuit_breakers()
            active_breaker_diagnostics = _circuit_breaker_diagnostics(active_breaker_rows)
            self.audit.emit(
                "CIRCUIT_BREAKER_STATUS",
                cycle_id=cycle_id,
                active=bool(active_breaker_rows),
                count=len(active_breaker_rows),
                new_entries_blocked=bool(active_breaker_rows),
                recovered_transient_public_network_breaker=recovered_transient_public_network_breaker,
                breakers=active_breaker_diagnostics,
            )
            for instrument in candidates:
                if account is not None:
                    try:
                        fresh_ticker = self._fetch_fresh_ticker(instrument)
                    except KrakenAPIError:
                        raise
                    if fresh_ticker is None:
                        counts["blocked"] += 1
                        self.audit.emit(
                            "NO_TRADE",
                            cycle_id=cycle_id,
                            pair=instrument.wsname,
                            reason="MARKET_TICKER_REFRESH_INVALID",
                        )
                        continue
                    ticker = fresh_ticker
                    ticker_times[instrument.pair_id] = time.time()
                    tickers[instrument.pair_id] = fresh_ticker
                else:
                    ticker = tickers.get(instrument.pair_id)
                    if ticker is None:
                        counts["blocked"] += 1
                        self.audit.emit(
                            "NO_TRADE",
                            cycle_id=cycle_id,
                            pair=instrument.wsname,
                            reason="MARKET_TICKER_MISSING",
                        )
                        continue
                if ticker.spread_bps>D(40):
                    counts["blocked"]+=1
                    self.audit.emit("MARKET_FILTER_REJECTED",cycle_id=cycle_id,pair=instrument.wsname,
                                    reason="SPREAD_TOO_WIDE",spread_bps=ticker.spread_bps)
                    continue
                quote_observed_at = ticker_times.get(instrument.pair_id)
                raw_ohlc=self.client.public("OHLC",{"pair":instrument.altname,"interval":60})
                bars=parse_ohlc_rows(raw_ohlc,instrument.pair_id)
                if not quote_is_fresh(
                    quote_observed_at, time.time(), self.settings.market_max_age_seconds
                ):
                    counts["blocked"] += 1
                    self.audit.emit(
                        "NO_TRADE",
                        cycle_id=cycle_id,
                        pair=instrument.wsname,
                        reason="MARKET_QUOTE_STALE",
                        quote_observed_at=quote_observed_at,
                        max_age_seconds=self.settings.market_max_age_seconds,
                    )
                    continue
                for bar in bars:
                    self.persistence.record_bar(instrument.pair_id,60,bar["ts"],str(bar["open"]),str(bar["high"]),
                                                str(bar["low"]),str(bar["close"]),str(bar["volume"]))
                try:
                    settlement_events = settle_due_observations(
                        self.persistence,
                        pair=instrument.pair_id,
                        current_exit_spread_bps=ticker.spread_bps,
                        interval_minutes=60,
                    )
                    for research_event in settlement_events:
                        event_name = research_event["event"]
                        payload = research_event["payload"]
                        self.persistence.record_event(event_name, payload)
                        self.audit.emit(event_name, **payload)
                        if event_name == "SHADOW_RESEARCH_OUTCOME_SETTLED":
                            counts["research_outcomes_settled"] += 1
                        elif event_name == "SHADOW_RESEARCH_OUTCOME_INCOMPLETE":
                            counts["research_outcomes_incomplete"] += 1
                except (ArithmeticError, TypeError, ValueError) as exc:
                    self.audit.emit(
                        "SHADOW_RESEARCH_SETTLEMENT_ERROR",
                        cycle_id=cycle_id,
                        pair=instrument.wsname,
                        exception_type=type(exc).__name__,
                        reason=str(exc)[:120],
                    )
                signal=generate_trend_signal(instrument.pair_id,[b["close"] for b in bars],
                    fast_threshold_bps=self.settings.strategy_fast_bps,slow_threshold_bps=self.settings.strategy_slow_bps)
                self.audit.emit("MARKET_SIGNAL_EVALUATED",cycle_id=cycle_id,pair=instrument.wsname,
                    direction=signal.direction,reason=signal.reason,momentum_fast_bps=signal.momentum_fast_bps,
                    momentum_slow_bps=signal.momentum_slow_bps,trend_efficiency_24h=signal.trend_efficiency_24h,
                    trend_efficiency_72h=signal.trend_efficiency_72h,trend_strength_score=signal.score,
                    score_is_calibrated_probability=False,spread_bps=ticker.spread_bps)
                fee=taker_fee_from_trade_volume(trade_volume,(instrument.pair_id,instrument.altname,instrument.wsname))
                if bars and signal.reason not in {"INSUFFICIENT_HISTORY", "INVALID_PRICE_HISTORY"}:
                    decision_ts = int(time.time())
                    latest_bar_ts = int(bars[-1]["ts"])
                    bar_close_epoch = latest_bar_ts + 3600
                    bar_age_seconds = decision_ts - bar_close_epoch
                    if -60 <= bar_age_seconds <= 3900:
                        entry_mid = (ticker.bid + ticker.ask) / D(2)
                        observation = {
                            "observation_id": observation_id(
                                pair=instrument.pair_id,
                                interval_minutes=60,
                                bar_ts=latest_bar_ts,
                                strategy_version=STRATEGY_VERSION,
                                fast_threshold_bps=self.settings.strategy_fast_bps,
                                slow_threshold_bps=self.settings.strategy_slow_bps,
                                horizon_seconds=self.settings.expected_holding_hours * 3600,
                            ),
                            "pair": instrument.pair_id,
                            "interval_minutes": 60,
                            "bar_ts": latest_bar_ts,
                            "decision_ts": decision_ts,
                            "strategy_version": STRATEGY_VERSION,
                            "fast_threshold_bps": self.settings.strategy_fast_bps,
                            "slow_threshold_bps": self.settings.strategy_slow_bps,
                            "direction": signal.direction,
                            "signal_reason": signal.reason,
                            "momentum_fast_bps": signal.momentum_fast_bps,
                            "momentum_slow_bps": signal.momentum_slow_bps,
                            "signal_score": signal.score,
                            "trend_efficiency_24h": signal.trend_efficiency_24h,
                            "trend_efficiency_72h": signal.trend_efficiency_72h,
                            "stop_distance_pct": signal.stop_distance_pct,
                            "entry_mid_price": entry_mid,
                            "entry_spread_bps": ticker.spread_bps,
                            "taker_fee_bps": fee,
                            "horizon_seconds": self.settings.expected_holding_hours * 3600,
                            "target_epoch": decision_ts + self.settings.expected_holding_hours * 3600,
                        }
                        try:
                            recorded = self.persistence.record_research_observation(observation)
                            if recorded:
                                counts["research_observations_recorded"] += 1
                                event_payload = {
                                    "cycle_id": cycle_id,
                                    "observation_id": observation["observation_id"],
                                    "pair": instrument.wsname,
                                    "strategy_version": STRATEGY_VERSION,
                                    "signal_bar_ts": latest_bar_ts,
                                    "decision_ts": decision_ts,
                                    "direction": signal.direction,
                                    "reason": signal.reason,
                                    "trend_efficiency_24h": str(signal.trend_efficiency_24h),
                                    "trend_efficiency_72h": str(signal.trend_efficiency_72h),
                                    "trend_strength_score": str(signal.score),
                                    "entry_mid_price": str(entry_mid),
                                    "entry_spread_bps": str(ticker.spread_bps),
                                    "taker_fee_bps": str(fee) if fee is not None else None,
                                    "taker_fee_source": "KRAKEN_TRADE_VOLUME" if fee is not None else "UNKNOWN",
                                    "target_epoch": observation["target_epoch"],
                                    "outcome_model": "FORWARD_HOURLY_BAR_CLOSE_COST_ESTIMATE",
                                    "margin_cost_status": "UNKNOWN_BREAK_EVEN_ONLY",
                                    "actual_fill": False,
                                    "portfolio_pnl": False,
                                }
                                self.persistence.record_event("SHADOW_RESEARCH_OBSERVATION_RECORDED", event_payload)
                                self.audit.emit("SHADOW_RESEARCH_OBSERVATION_RECORDED", **event_payload)
                                margin_supported_both_directions = (
                                    any(level >= 2 for level in instrument.leverage_buy)
                                    and any(level >= 2 for level in instrument.leverage_sell)
                                )
                                if fee is not None and margin_supported_both_directions:
                                    try:
                                        backtest_report = run_ohlcv_backtest_scenarios(
                                            bars,
                                            pair=instrument.wsname,
                                            taker_fee_bps=fee,
                                            current_spread_bps=ticker.spread_bps,
                                            holding_hours=self.settings.expected_holding_hours,
                                            fast_threshold_bps=self.settings.strategy_fast_bps,
                                            slow_threshold_bps=self.settings.strategy_slow_bps,
                                            stop_pct=self.settings.position_stop_loss_pct,
                                            take_profit_pct=self.settings.position_take_profit_pct,
                                            take_profit_fraction=self.settings.position_take_profit_fraction,
                                            strategy_version=STRATEGY_VERSION,
                                        )
                                        backtest_payload = {
                                            "cycle_id": cycle_id,
                                            "observation_id": observation["observation_id"],
                                            **backtest_report,
                                        }
                                        self.persistence.record_event(
                                            "OHLCV_BACKTEST_SCENARIO_REPORT", backtest_payload
                                        )
                                        self.audit.emit(
                                            "OHLCV_BACKTEST_SCENARIO_REPORT", **backtest_payload
                                        )
                                    except (ArithmeticError, TypeError, ValueError) as exc:
                                        self.audit.emit(
                                            "OHLCV_BACKTEST_SCENARIO_ERROR",
                                            cycle_id=cycle_id,
                                            pair=instrument.wsname,
                                            exception_type=type(exc).__name__,
                                            reason=str(exc)[:120],
                                            release_gate_eligible=False,
                                        )
                                else:
                                    self.audit.emit(
                                        "OHLCV_BACKTEST_SCENARIO_SKIPPED",
                                        cycle_id=cycle_id,
                                        pair=instrument.wsname,
                                        reason=(
                                            "CURRENT_TAKER_FEE_UNKNOWN" if fee is None
                                            else "MARGIN_NOT_SUPPORTED_IN_BOTH_DIRECTIONS"
                                        ),
                                    )
                        except (ArithmeticError, TypeError, ValueError) as exc:
                            self.audit.emit(
                                "SHADOW_RESEARCH_OBSERVATION_REJECTED",
                                cycle_id=cycle_id,
                                pair=instrument.wsname,
                                exception_type=type(exc).__name__,
                                reason=str(exc)[:120],
                            )
                    else:
                        self.audit.emit(
                            "SHADOW_RESEARCH_OBSERVATION_SKIPPED",
                            cycle_id=cycle_id,
                            pair=instrument.wsname,
                            reason="LATEST_CLOSED_BAR_STALE",
                            signal_bar_ts=latest_bar_ts,
                            bar_age_seconds=bar_age_seconds,
                        )
                if not signal.has_direction:
                    continue
                counts["signals"] += 1
                signal_bar_ts = int(bars[-1]["ts"]) if bars else -1
                signal_time_epoch = time.time()
                signal_bar_age_seconds = (
                    signal_time_epoch - (signal_bar_ts + 3600)
                    if signal_bar_ts >= 0 else None
                )
                if not closed_bar_is_fresh(
                    signal_bar_ts,
                    signal_time_epoch,
                    interval_minutes=60,
                    max_age_seconds=3900,
                    future_tolerance_seconds=60,
                ):
                    counts["blocked"] += 1
                    self.audit.emit(
                        "NO_TRADE",
                        cycle_id=cycle_id,
                        pair=instrument.wsname,
                        direction=signal.direction,
                        reason="LATEST_CLOSED_BAR_STALE_OR_INVALID",
                        signal_bar_ts=signal_bar_ts if signal_bar_ts >= 0 else None,
                        bar_age_seconds=signal_bar_age_seconds,
                        max_age_seconds=3900,
                        no_new_position_risk=True,
                    )
                    continue
                entry_decision_id = signal_decision_id(
                    strategy_version=STRATEGY_VERSION,
                    pair_id=instrument.pair_id,
                    signal_bar_ts=signal_bar_ts,
                    direction=signal.direction,
                )
                prior_intent = self.persistence.intent_for_decision(entry_decision_id)
                if prior_intent is not None:
                    counts["blocked"] += 1
                    self.audit.emit(
                        "ENTRY_THESIS_ALREADY_ATTEMPTED",
                        cycle_id=cycle_id,
                        pair=instrument.wsname,
                        direction=signal.direction,
                        strategy_version=STRATEGY_VERSION,
                        signal_bar_ts=signal_bar_ts,
                        decision_id=entry_decision_id,
                        existing_intent_id=prior_intent.get("intent_id"),
                        existing_intent_state=prior_intent.get("state"),
                        first_attempt_at=prior_intent.get("created_at"),
                        retry_blocked=True,
                    )
                    continue
                if account is not None:
                    pair_exposure = entry_pair_exposure(
                        instrument,
                        open_positions=account.get("open_positions"),
                        open_orders=account.get("open_orders"),
                    )
                    if pair_exposure["blocked"]:
                        counts["blocked"] += 1
                        self.audit.emit(
                            "ENTRY_BLOCKED_EXISTING_PAIR_EXPOSURE",
                            cycle_id=cycle_id,
                            pair=instrument.wsname,
                            direction=signal.direction,
                            strategy_version=STRATEGY_VERSION,
                            signal_bar_ts=signal_bar_ts,
                            **pair_exposure,
                        )
                        continue
                if account is not None and not api_permissions_ready:
                    counts["blocked"] += 1
                    self.audit.emit(
                        "NO_TRADE",
                        cycle_id=cycle_id,
                        pair=instrument.wsname,
                        direction=signal.direction,
                        reason="API_KEY_PERMISSION_GATE_BLOCKED",
                        evidence_gaps=api_permission_gaps,
                        no_new_position_risk=True,
                    )
                    continue
                if account is not None and not history_ready_for_entries:
                    counts["blocked"] += 1
                    self.audit.emit(
                        "NO_TRADE",
                        cycle_id=cycle_id,
                        pair=instrument.wsname,
                        direction=signal.direction,
                        reason="ACCOUNT_HISTORY_EVIDENCE_INCOMPLETE",
                        evidence_gaps=history_evidence_gaps,
                        no_new_position_risk=True,
                    )
                    continue
                margin_leverage=choose_trade_leverage(instrument,signal.direction,self.settings)
                is_margin=signal.direction=="SHORT" or bool(margin_leverage)
                leverage=margin_leverage if is_margin else 1
                if signal.direction=="SHORT" and leverage is None:
                    counts["blocked"]+=1
                    self.audit.emit("NO_TRADE",cycle_id=cycle_id,pair=instrument.wsname,reason="MARGIN_SHORT_NOT_SUPPORTED")
                    continue
                if signal.direction=="LONG" and leverage is None:
                    is_margin=False; leverage=1
                # Historical momentum magnitude is only a diagnostic proxy. It has not
                # been calibrated to expected returns and must never be described as edge.
                gross_edge_proxy=max(
                    abs(signal.momentum_fast_bps),
                    abs(signal.momentum_slow_bps)/D(3),
                )*D("0.25")
                gross_edge_proxy_kind = "UNVALIDATED_LINEAR_MOMENTUM_HEURISTIC"
                margin_cost=estimate_round_trip_cost(is_margin=True,taker_fee_bps=fee,
                    spread_bps=ticker.spread_bps,slippage_bps_per_side=D(5),
                    holding_hours=self.settings.expected_holding_hours,
                    leverage=int(margin_leverage or 1),margin_open_bps=None,margin_rollover_bps=None)
                spot_cost=estimate_round_trip_cost(is_margin=False,taker_fee_bps=fee,
                    spread_bps=ticker.spread_bps,slippage_bps_per_side=D(5),
                    holding_hours=self.settings.expected_holding_hours,leverage=1) if signal.direction=="LONG" else None
                plan=select_costed_plan(direction=signal.direction,margin_leverage=margin_leverage,
                    gross_edge_proxy_bps=gross_edge_proxy,margin_cost=margin_cost,spot_cost=spot_cost)
                if plan is None:
                    cost_candidates=[margin_cost]+([spot_cost] if spot_cost is not None else [])
                    any_complete=any(c.complete and c.total_round_trip_bps is not None for c in cost_candidates)
                    counts["blocked"]+=1
                    if (
                        spot_cost is not None
                        and spot_cost.total_round_trip_bps is not None
                        and gross_edge_proxy <= spot_cost.total_round_trip_bps
                    ):
                        no_trade_reason = "MOMENTUM_PROXY_BELOW_ESTIMATED_SPOT_COSTS"
                    elif not margin_cost.complete and (
                        signal.direction == "SHORT" or spot_cost is None
                    ):
                        no_trade_reason = "MARGIN_COSTS_UNKNOWN_AND_EDGE_UNVALIDATED"
                    elif not any_complete:
                        no_trade_reason = "COST_INPUTS_INCOMPLETE_AND_EDGE_UNVALIDATED"
                    else:
                        no_trade_reason = "NO_POSITIVE_COSTED_PLAN_FROM_UNVALIDATED_PROXY"
                    self.audit.emit("NO_TRADE",cycle_id=cycle_id,pair=instrument.wsname,
                        reason=no_trade_reason,
                        margin_cost_reasons=list(margin_cost.reasons),
                        margin_cost_diagnostic=cost_diagnostic(margin_cost),
                        spot_cost_reasons=list(spot_cost.reasons) if spot_cost is not None else [],
                        spot_cost_diagnostic=cost_diagnostic(spot_cost),
                        gross_edge_proxy_bps=gross_edge_proxy,
                        gross_edge_proxy_kind=gross_edge_proxy_kind,
                        gross_edge_proxy_is_expected_return=False,
                        edge_is_calibrated=False,margin_fee_rates_confirmed=False,
                        release_gate_eligible=False)
                    continue
                is_margin=plan.product_type=="SPOT_MARGIN"
                leverage=plan.leverage
                cost=plan.cost
                edge=plan.net_edge_proxy_bps
                if not is_margin and margin_leverage is not None:
                    self.audit.emit("MARGIN_PLAN_NOT_SELECTED",cycle_id=cycle_id,pair=instrument.wsname,
                        reason=plan.reason,margin_cost_reasons=list(margin_cost.reasons),
                        margin_cost_diagnostic=cost_diagnostic(margin_cost),
                        spot_cost_bps=cost.total_round_trip_bps,
                        spot_cost_diagnostic=cost_diagnostic(cost),spot_edge_proxy_bps=edge,
                        gross_edge_proxy_bps=gross_edge_proxy,
                        gross_edge_proxy_kind=gross_edge_proxy_kind,
                        gross_edge_proxy_is_expected_return=False,
                        edge_is_calibrated=False,release_gate_eligible=False)
                if not cost.complete or cost.total_round_trip_bps is None:
                    counts["blocked"]+=1
                    self.audit.emit("NO_TRADE",cycle_id=cycle_id,pair=instrument.wsname,
                        reason="SELECTED_PLAN_COSTS_INCOMPLETE",selected_product=plan.product_type)
                    continue
                if account is None or snapshot is None:
                    counts["blocked"]+=1
                    self.audit.emit("NO_TRADE",cycle_id=cycle_id,pair=instrument.wsname,reason="ACCOUNT_SNAPSHOT_REQUIRED")
                    continue
                if active_breaker_rows:
                    counts["blocked"] += 1
                    self.audit.emit(
                        "NO_TRADE",
                        cycle_id=cycle_id,
                        pair=instrument.wsname,
                        reason="PERSISTED_CIRCUIT_BREAKER_ACTIVE",
                        active_circuit_breakers=active_breaker_diagnostics,
                        no_new_position_risk=True,
                    )
                    continue
                proposed=size_position_usd(equity_usd=snapshot.equity_usd,stop_distance_pct=signal.stop_distance_pct,
                    gap_buffer_pct=max(D("0.5"),signal.stop_distance_pct*D("0.5")),cost_bps=cost.total_round_trip_bps,
                    max_position_pct=self.settings.risk_max_position_pct,risk_budget_pct=self.settings.risk_per_trade_pct,
                    free_collateral_usd=snapshot.free_collateral_usd,leverage=int(leverage or 1))
                gate=evaluate_entry(instrument=instrument,signal=signal,snapshot=snapshot,settings=self.settings,
                    cost_complete=cost.complete,estimated_net_edge_bps=edge,leverage=int(leverage or 1),
                    proposed_notional_usd=proposed,edge_calibrated=False)
                if not gate.allowed:
                    counts["blocked"]+=1
                    self.audit.emit("RISK_GATE_REJECTED",cycle_id=cycle_id,pair=instrument.wsname,
                                    direction=signal.direction,reasons=list(gate.reasons),
                                    product_type=plan.product_type,leverage=leverage,
                                    gross_edge_proxy_bps=gross_edge_proxy,
                                    gross_edge_proxy_kind=gross_edge_proxy_kind,
                                    gross_edge_proxy_is_expected_return=False,
                                    selected_cost_diagnostic=cost_diagnostic(cost),
                                    edge_is_calibrated=False,release_gate_eligible=False)
                    continue
                execution_reference_price = executable_reference_price(ticker, signal.direction)
                volume=volume_for_notional_usd(
                    proposed, execution_reference_price, instrument.lot_decimals
                )
                if execution_reference_price <= 0:
                    counts["blocked"]+=1
                    self.audit.emit(
                        "NO_TRADE", cycle_id=cycle_id, pair=instrument.wsname,
                        reason="EXECUTABLE_REFERENCE_PRICE_INVALID",
                        direction=signal.direction,
                    )
                    continue
                if volume < instrument.order_min or volume <= 0:
                    counts["blocked"]+=1
                    self.audit.emit("NO_TRADE",cycle_id=cycle_id,pair=instrument.wsname,
                        reason="BELOW_EXCHANGE_MINIMUM_OR_ROUNDED_TO_ZERO",proposed_notional_usd=proposed,
                        calculated_volume=volume,minimum_volume=instrument.order_min)
                    continue
                action=(Action.OPEN_SPOT_LONG if signal.direction=="LONG" and not is_margin else
                        Action.OPEN_MARGIN_LONG if signal.direction=="LONG" else Action.OPEN_MARGIN_SHORT)
                intent={"intent_id":str(uuid.uuid4()),"decision_id":entry_decision_id,
                    "client_order_id":uuid.uuid4().hex,"pair":instrument.pair_id,"action":action.value,
                    "side":signal.direction,"volume":str(volume),"leverage":int(leverage or 1) if is_margin else None,
                    "product_type":"SPOT_MARGIN" if is_margin else "CASH_SPOT",
                    "decision_evidence":{"cycle_id":cycle_id,"strategy_version":STRATEGY_VERSION,
                    "signal_bar_ts":signal_bar_ts,"idempotency_scope":"ONE_ENTRY_THESIS_PER_PAIR_DIRECTION_CLOSED_HOURLY_BAR",
                    "net_edge_is_uncalibrated_proxy":True,"estimated_cost_bps":str(cost.total_round_trip_bps),
                    "margin_fee_source":"UNCONFIRMED" if is_margin else "NOT_APPLICABLE"}}
                if self.settings.live_requested:
                    authority = OrderAuthority(
                        self.client, self.persistence, self.settings, self.audit
                    )
                    try:
                        submission = authority.execute(
                            self._order_intent_from_mapping(intent),
                            instrument,
                            prechecks_passed=gate.allowed,
                            validate_only=False,
                        )
                    except (LiveExecutionBlocked, ValueError) as exc:
                        counts["blocked"] += 1
                        self.audit.emit(
                            "LIVE_ORDER_REJECTED_PRE_SUBMIT",
                            cycle_id=cycle_id,
                            pair=instrument.wsname,
                            intent_id=intent["intent_id"],
                            exception_type=type(exc).__name__,
                            code=str(exc)[:200],
                            real_order_submitted=False,
                        )
                        continue
                    counts["intents"] += 1
                    if submission.get("state") == "ACKNOWLEDGED":
                        counts["real_orders_sent"] += 1
                    self.audit.emit(
                        "LIVE_ORDER_SUBMISSION_RESULT",
                        cycle_id=cycle_id,
                        pair=instrument.wsname,
                        intent_id=intent["intent_id"],
                        order_state=submission.get("state"),
                        real_order_submitted=submission.get("state") == "ACKNOWLEDGED",
                        fill_confirmed=False,
                        result=submission.get("result", {}),
                    )
                elif self.settings.validate_shadow_orders:
                    try:
                        validation = self._validate_shadow_order(intent, instrument)
                    except KrakenAmbiguousError as exc:
                        counts["blocked"] += 1
                        self.audit.emit(
                            "SHADOW_ORDER_VALIDATION_OUTCOME_UNKNOWN",
                            cycle_id=cycle_id,
                            pair=instrument.wsname,
                            intent_id=intent["intent_id"],
                            code=exc.code,
                            no_live_order_submitted=True,
                        )
                        continue
                    except (KrakenAPIError, ValueError) as exc:
                        counts["blocked"] += 1
                        self.audit.emit(
                            "SHADOW_ORDER_VALIDATION_FAILED",
                            cycle_id=cycle_id,
                            pair=instrument.wsname,
                            intent_id=intent["intent_id"],
                            exception_type=type(exc).__name__,
                            code=getattr(exc, "code", str(exc))[:200],
                            no_live_order_submitted=True,
                        )
                        continue
                    counts["intents"] += 1
                    self.audit.emit(
                        "SHADOW_ORDER_VALIDATED_NO_ORDER_SENT",
                        cycle_id=cycle_id,
                        pair=instrument.wsname,
                        intent_id=intent["intent_id"],
                        validation_state=validation.get("state"),
                        no_live_order_submitted=True,
                    )
                elif self.persistence.create_intent(intent, initial_state="SHADOW_ONLY"):
                    counts["intents"] += 1
                    self.audit.emit(
                        "SHADOW_INTENT_PERSISTED_NO_ORDER_SENT",
                        state="SHADOW_ONLY",
                        **intent,
                    )
            try:
                target_open_backfill = backfill_target_open_outcomes(self.persistence)
                if target_open_backfill["updated"] > 0:
                    backfill_payload = {"cycle_id": cycle_id, **target_open_backfill}
                    self.persistence.record_event(
                        "SHADOW_RESEARCH_TARGET_OPEN_BACKFILL", backfill_payload
                    )
                    self.audit.emit(
                        "SHADOW_RESEARCH_TARGET_OPEN_BACKFILL", **backfill_payload
                    )
            except (ArithmeticError, KeyError, TypeError, ValueError, OverflowError) as exc:
                self.audit.emit(
                    "SHADOW_RESEARCH_TARGET_OPEN_BACKFILL_ERROR",
                    cycle_id=cycle_id,
                    exception_type=type(exc).__name__,
                    reason=str(exc)[:120],
                    primary_outcomes_unchanged=True,
                )
            research_summary = summarize_shadow_research(
                self.persistence.research_observations(limit=5000),
                strategy_version=STRATEGY_VERSION,
            )
            self.persistence.record_event("SHADOW_RESEARCH_SUMMARY", research_summary)
            self.audit.emit("SHADOW_RESEARCH_SUMMARY", **research_summary)
            final_breaker_rows = self.persistence.circuit_breakers()
            final_breaker_diagnostics = _circuit_breaker_diagnostics(final_breaker_rows)
            result = {
                "cycle_id": cycle_id,
                "status": "OK",
                "duration_seconds": round(time.time() - started, 3),
                **counts,
                "mode": self.settings.mode,
                "live_requested": self.settings.live_requested,
                "internal_live_release": INTERNAL_LIVE_EXECUTION_RELEASED,
                "history_ready_for_entries": history_ready_for_entries,
                "history_evidence_gaps": list(history_evidence_gaps),
                "active_circuit_breaker_count": len(final_breaker_rows),
                "active_circuit_breakers": final_breaker_diagnostics,
                "recovered_transient_public_network_breaker": recovered_transient_public_network_breaker,
                "shadow_research_validation_status": research_summary["validation_status"],
                "shadow_research_costed_sample_count": research_summary["costed_spot_proxy_observations"],
                "shadow_research_holdout_available": research_summary["holdout_sample_available_for_review"],
                "shadow_research_release_gate_eligible": research_summary["release_gate_eligible"],
                "note": (
                    "Real order submission also requires the internal release gate; "
                    "INTERNAL_LIVE_EXECUTION_RELEASED is false in this version. "
                    "No fill is assumed from an acknowledged submission."
                ),
            }
            self.persistence.record_event("CYCLE_SUMMARY",result); self.audit.emit("CYCLE_SUMMARY",**result)
            return result
        except KrakenAPIError as exc:
            self.persistence.trip_circuit_breaker("KRAKEN_API",exc.code,{"cycle_id":cycle_id})
            self.audit.emit("CYCLE_FAILED_CIRCUIT_BREAKER",cycle_id=cycle_id,code=exc.code)
            return {"cycle_id":cycle_id,"status":"FAILED","reason":exc.code}
        except Exception as exc:
            self.persistence.trip_circuit_breaker("UNHANDLED_RUNTIME","CYCLE_EXCEPTION",
                {"cycle_id":cycle_id,"exception_type":type(exc).__name__})
            self.audit.emit("CYCLE_FAILED_CIRCUIT_BREAKER",cycle_id=cycle_id,exception_type=type(exc).__name__)
            return {"cycle_id":cycle_id,"status":"FAILED","reason":type(exc).__name__}
