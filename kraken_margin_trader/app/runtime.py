from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
import time
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from app.audit import AuditLogger
from app.config import Settings
from app.costs import estimate_round_trip_cost, select_costed_plan, taker_fee_from_trade_volume
from app.kraken import (
    KrakenAPIError, KrakenAmbiguousError, KrakenClient,
    api_key_permission_gate_reasons,
)
from app.execution import LiveExecutionBlocked, OrderAuthority, OrderIntent
from app.history_sync import history_sources_ready, sync_history_source
from app.market import executable_reference_price, parse_ohlc_rows, parse_tickers, quote_is_fresh, usd_crypto_universe, volume_for_notional_usd
from app.models import Action, RiskSnapshot
from app.persistence import Persistence
from app.position_management import evaluate_position_exits
from app.portfolio import aggregate_position_exposure
from app.portfolio_reconciliation import reconcile_positions_to_fills, reconcile_wallet_balance_deltas
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

    def _account_risk_snapshot(self,snapshot: dict[str,Any]) -> RiskSnapshot:
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
        # Daily PnL and peak-equity drawdown need reconciled fills, fees and external transfers.
        # Until that ledger baseline is proven, represent both as unknown rather than zero.
        return RiskSnapshot(
            equity, free, used, gross, net, len(rows), None, None,
            margin_level, snapshot_age, complete, False,
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
        counts={"universe":0,"candidates":0,"signals":0,"intents":0,"blocked":0,"position_exits_checked":0,"position_reductions_recommended":0,"real_orders_sent":0}
        history_ready_for_entries = False
        history_evidence_gaps = ["ACCOUNT_HISTORY_NOT_CHECKED"]
        api_permissions_ready = False
        api_permission_gaps: list[str] = ["API_KEY_PERMISSION_CHECK_NOT_COMPLETED"]
        history_metadata_untrusted = False
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
                if not api_permissions_ready:
                    private_read_access_invalid = any(
                        reason in api_permission_gaps
                        for reason in (
                            "REQUIRED_QUERY_PERMISSION_MISSING",
                            "API_KEY_PERMISSIONS_UNKNOWN",
                            "API_KEY_ASSESSMENT_NOT_READY",
                        )
                    )
                    history_metadata_untrusted = any(
                        reason in api_permission_gaps
                        for reason in (
                            "API_KEY_EXPIRED_OR_EXPIRY_UNKNOWN",
                            "API_KEY_HISTORY_QUERY_WINDOW_RESTRICTED",
                            "API_KEY_METADATA_INCOMPLETE",
                        )
                    )
                    self.audit.emit(
                        "API_KEY_PERMISSION_GATE_BLOCKED",
                        cycle_id=cycle_id,
                        reasons=api_permission_gaps,
                        existing_position_management_continues=not private_read_access_invalid,
                        operator_action=(
                            "Correct API key scopes or history window; next cycle rechecks automatically."
                        ),
                    )
                    if private_read_access_invalid:
                        # Do not repeatedly call known-forbidden private endpoints and turn a
                        # recoverable configuration issue into a permanent KRAKEN_API breaker.
                        # Without reliable private reads, positions cannot be managed safely.
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
                    history_evidence_gaps.append("ACCOUNT_HISTORY_SYNC_SKIPPED_UNTRUSTED_KEY_METADATA")
                    self.audit.emit(
                        "KRAKEN_HISTORY_SYNC_SKIPPED",
                        cycle_id=cycle_id,
                        sources=["TRADES", "LEDGERS", "CLOSED_ORDERS"],
                        reasons=api_permission_gaps,
                        existing_position_monitoring_continues=True,
                        new_risk_allowed=False,
                    )
                else:
                    for history_source in ("TRADES", "LEDGERS", "CLOSED_ORDERS"):
                        history_summary=sync_history_source(
                            client=self.client,
                            persistence=self.persistence,
                            source=history_source,
                        )
                        history_summaries.append(history_summary)
                        self.audit.emit(
                            "KRAKEN_HISTORY_SYNC",
                            cycle_id=cycle_id,
                            **history_summary,
                        )
                    history_ready_for_entries, history_evidence_gaps = history_sources_ready(
                        history_summaries
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
                    include_closed_orders=not history_metadata_untrusted
                )
                persisted_snapshot = self.persistence.record_account_snapshot(account)
                self.audit.emit(
                    "ACCOUNT_SNAPSHOT_PERSISTED",
                    cycle_id=cycle_id,
                    **persisted_snapshot,
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
                elif history_metadata_untrusted:
                    # Do not call historical endpoints while queryFrom/queryTo or expiry
                    # metadata is unknown. Use already imported ledger events but keep
                    # reconciliation explicitly incomplete, which blocks new risk.
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
                self.audit.emit(
                    "WALLET_BALANCE_RECONCILIATION_SUMMARY",
                    cycle_id=cycle_id,
                    **wallet_reconciliation,
                    full_portfolio_reconciliation=False,
                    complete_for_entries=False,
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
                if not history_ready_for_entries or not all(
                    item["initial_backfill_complete"] for item in history_summaries
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
                        reconciliation = reconcile_order_intents(self.persistence)
                        self.audit.emit(
                            "ORDER_RECONCILIATION_SUMMARY",
                            cycle_id=cycle_id,
                            **reconciliation,
                        )
                        position_reconciliation = reconcile_positions_to_fills(self.persistence)
                        self.audit.emit(
                            "POSITION_FILL_RECONCILIATION_SUMMARY",
                            cycle_id=cycle_id,
                            **position_reconciliation,
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
                snapshot = self._account_risk_snapshot(account)
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
                signal=generate_trend_signal(instrument.pair_id,[b["close"] for b in bars],
                    fast_threshold_bps=self.settings.strategy_fast_bps,slow_threshold_bps=self.settings.strategy_slow_bps)
                self.audit.emit("MARKET_SIGNAL_EVALUATED",cycle_id=cycle_id,pair=instrument.wsname,
                    direction=signal.direction,reason=signal.reason,momentum_fast_bps=signal.momentum_fast_bps,
                    momentum_slow_bps=signal.momentum_slow_bps,score_is_calibrated_probability=False,
                    spread_bps=ticker.spread_bps)
                if not signal.has_direction: continue
                counts["signals"]+=1
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
                fee=taker_fee_from_trade_volume(trade_volume,(instrument.pair_id,instrument.altname,instrument.wsname))
                margin_leverage=choose_trade_leverage(instrument,signal.direction,self.settings)
                is_margin=signal.direction=="SHORT" or bool(margin_leverage)
                leverage=margin_leverage if is_margin else 1
                if signal.direction=="SHORT" and leverage is None:
                    counts["blocked"]+=1
                    self.audit.emit("NO_TRADE",cycle_id=cycle_id,pair=instrument.wsname,reason="MARGIN_SHORT_NOT_SUPPORTED")
                    continue
                if signal.direction=="LONG" and leverage is None:
                    is_margin=False; leverage=1
                gross_edge_proxy=max(
                    abs(signal.momentum_fast_bps),
                    abs(signal.momentum_slow_bps)/D(3),
                )*D("0.25")
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
                    self.audit.emit("NO_TRADE",cycle_id=cycle_id,pair=instrument.wsname,
                        reason="NON_POSITIVE_UNCALIBRATED_EDGE_PROXY" if any_complete else "COST_INPUTS_INCOMPLETE",
                        margin_cost_reasons=list(margin_cost.reasons),
                        spot_cost_reasons=list(spot_cost.reasons) if spot_cost is not None else [],
                        gross_edge_proxy_bps=gross_edge_proxy,edge_is_calibrated=False,
                        margin_fee_rates_confirmed=False)
                    continue
                is_margin=plan.product_type=="SPOT_MARGIN"
                leverage=plan.leverage
                cost=plan.cost
                edge=plan.net_edge_proxy_bps
                if not is_margin and margin_leverage is not None:
                    self.audit.emit("MARGIN_PLAN_NOT_SELECTED",cycle_id=cycle_id,pair=instrument.wsname,
                        reason=plan.reason,margin_cost_reasons=list(margin_cost.reasons),
                        spot_cost_bps=cost.total_round_trip_bps,spot_edge_proxy_bps=edge,
                        edge_is_calibrated=False)
                if not cost.complete or cost.total_round_trip_bps is None:
                    counts["blocked"]+=1
                    self.audit.emit("NO_TRADE",cycle_id=cycle_id,pair=instrument.wsname,
                        reason="SELECTED_PLAN_COSTS_INCOMPLETE",selected_product=plan.product_type)
                    continue
                if account is None or snapshot is None:
                    counts["blocked"]+=1
                    self.audit.emit("NO_TRADE",cycle_id=cycle_id,pair=instrument.wsname,reason="ACCOUNT_SNAPSHOT_REQUIRED")
                    continue
                if self.persistence.circuit_breakers():
                    counts["blocked"]+=1
                    self.audit.emit("NO_TRADE",cycle_id=cycle_id,pair=instrument.wsname,reason="PERSISTED_CIRCUIT_BREAKER_ACTIVE")
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
                                    direction=signal.direction,reasons=list(gate.reasons))
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
                intent={"intent_id":str(uuid.uuid4()),"decision_id":cycle_id+":"+instrument.pair_id+":"+signal.direction,
                    "client_order_id":uuid.uuid4().hex,"pair":instrument.pair_id,"action":action.value,
                    "side":signal.direction,"volume":str(volume),"leverage":int(leverage or 1) if is_margin else None,
                    "product_type":"SPOT_MARGIN" if is_margin else "CASH_SPOT",
                    "decision_evidence":{"cycle_id":cycle_id,"net_edge_is_uncalibrated_proxy":True,
                    "estimated_cost_bps":str(cost.total_round_trip_bps),"margin_fee_source":"UNCONFIRMED" if is_margin else "NOT_APPLICABLE"}}
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
            result = {
                "cycle_id": cycle_id,
                "status": "OK",
                "duration_seconds": round(time.time() - started, 3),
                **counts,
                "mode": self.settings.mode,
                "live_requested": self.settings.live_requested,
                "note": (
                    "Approved LIVE intents are routed to OrderAuthority when operator switches are active; "
                    "no fill is assumed from an acknowledged submission. Explicit history, cost, edge and "
                    "risk gates can still reject a candidate."
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
