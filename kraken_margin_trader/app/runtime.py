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
from app.kraken import KrakenAPIError, KrakenClient
from app.history_sync import sync_history_source
from app.market import executable_reference_price, parse_ohlc_rows, parse_tickers, quote_is_fresh, usd_crypto_universe, volume_for_notional_usd
from app.models import Action, RiskSnapshot
from app.persistence import Persistence
from app.position_management import evaluate_position_exits
from app.portfolio import aggregate_position_exposure
from app.portfolio_reconciliation import reconcile_positions_to_fills
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
        counts={"universe":0,"candidates":0,"signals":0,"intents":0,"blocked":0,"position_exits_checked":0,"position_reductions_recommended":0}
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
                history_summaries=[]
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
                # Fetch the account/position snapshot after history network calls so
                # the risk gate uses a genuinely recent snapshot.
                account = self.client.account_snapshot()
                persisted_snapshot = self.persistence.record_account_snapshot(account)
                self.audit.emit(
                    "ACCOUNT_SNAPSHOT_PERSISTED",
                    cycle_id=cycle_id,
                    **persisted_snapshot,
                )
                if not persisted_snapshot["positions_shape_complete"]:
                    self.persistence.trip_circuit_breaker(
                        "ACCOUNT_SNAPSHOT",
                        "POSITION_SNAPSHOT_SHAPE_INVALID",
                        {"cycle_id": cycle_id, "snapshot_id": persisted_snapshot["snapshot_id"]},
                    )
                if not all(item["initial_backfill_complete"] for item in history_summaries):
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
                closed_orders = _exchange_order_rows(account.get("closed_orders"), "closed")
                if open_orders is None or closed_orders is None:
                    self.persistence.trip_circuit_breaker(
                        "ORDER_SNAPSHOT", "ORDER_RESPONSE_SHAPE_INVALID",
                        {"cycle_id": cycle_id},
                    )
                    self.audit.emit(
                        "EXCHANGE_ORDER_SNAPSHOT_INVALID",
                        cycle_id=cycle_id,
                        open_orders_valid=open_orders is not None,
                        closed_orders_valid=closed_orders is not None,
                    )
                else:
                    for order_id, order in open_orders.items():
                        self.persistence.record_exchange_order(order_id, order, source="OPEN")
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
                trade_volume=self.client.trade_volume([i.altname for i in candidates])
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
                if self.persistence.create_intent(intent, initial_state="SHADOW_ONLY"):
                    counts["intents"] += 1
                    self.audit.emit(
                        "SHADOW_INTENT_PERSISTED_NO_ORDER_SENT",
                        state="SHADOW_ONLY",
                        **intent,
                    )
            result={"cycle_id":cycle_id,"status":"OK","duration_seconds":round(time.time()-started,3),
                **counts,"mode":self.settings.mode,"real_orders_sent":0,
                "note":"No live execution; account reconciliation and dynamic margin costs remain gates."}
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
