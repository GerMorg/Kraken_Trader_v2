from __future__ import annotations

from decimal import Decimal
import time
import uuid
from typing import Any

from app.audit import AuditLogger
from app.config import Settings
from app.costs import estimate_round_trip_cost, taker_fee_from_trade_volume
from app.kraken import KrakenAPIError, KrakenClient
from app.market import parse_ohlc_rows, parse_tickers, usd_crypto_universe
from app.models import Action, RiskSnapshot
from app.persistence import Persistence
from app.portfolio import aggregate_position_exposure
from app.risk import choose_trade_leverage, evaluate_entry, size_position_usd
from app.strategy import generate_trend_signal

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

class TraderRuntime:
    def __init__(self,settings: Settings,persistence: Persistence,audit: AuditLogger) -> None:
        self.settings,self.persistence,self.audit=settings,persistence,audit
        self.client=KrakenClient(settings.api_key,settings.api_secret,persistence.next_nonce)

    def _account_risk_snapshot(self,snapshot: dict[str,Any]) -> RiskSnapshot:
        trade=snapshot["trade_balance"]; rows=_open_position_rows(snapshot["open_positions"])
        orders=snapshot["open_orders"].get("open",{}); balance=snapshot["balance"]
        equity=_decimal(trade.get("e")) or _decimal(trade.get("eb")) or D(0)
        free=_decimal(trade.get("mf")) or D(0); used=_decimal(trade.get("m")) or D(0)
        margin_level=_decimal(trade.get("ml"))
        exposure=aggregate_position_exposure(rows)
        gross,net=exposure.gross_usd,exposure.net_usd
        complete=bool(trade.get("e") is not None and trade.get("mf") is not None and
                      isinstance(orders,dict) and isinstance(balance,dict) and exposure.complete and
                      all("vol" in row and "pair" in row and "posstatus" in row for row in rows.values()))
        # Verified drawdown/cashflow baseline and full portfolio reconciliation are not available yet.
        return RiskSnapshot(equity,free,used,gross,net,len(rows),D(0),D(0),margin_level,D(0),complete,False)

    def cycle(self) -> dict[str,Any]:
        cycle_id=str(uuid.uuid4()); started=time.time()
        counts={"universe":0,"candidates":0,"signals":0,"intents":0,"blocked":0}
        self.audit.emit("CYCLE_START",cycle_id=cycle_id,mode=self.settings.mode)
        try:
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
            ranked=sorted((i for i in instruments if i.pair_id in tickers),
                key=lambda i:tickers[i.pair_id].volume_quote_24h,reverse=True)
            candidates=ranked[:self.settings.max_market_candidates]; counts["candidates"]=len(candidates)
            account=None; snapshot=None; trade_volume={}
            if self.settings.credentials_present:
                account=self.client.account_snapshot(); rows=_open_position_rows(account["open_positions"])
                self.persistence.record_event("ACCOUNT_SNAPSHOT_CAPTURED",{
                    "open_positions":len(rows),"open_orders":len(account["open_orders"].get("open",{})),
                    "captured_at":account["captured_at"],"complete_for_entries":False})
                snapshot=self._account_risk_snapshot(account)
                trade_volume=self.client.trade_volume([i.altname for i in candidates])
            else:
                self.audit.emit("ACCOUNT_UNAVAILABLE_PUBLIC_ONLY",reason="API_CREDENTIALS_NOT_CONFIGURED")
            for instrument in candidates:
                ticker=tickers[instrument.pair_id]
                if ticker.spread_bps>D(40):
                    counts["blocked"]+=1
                    self.audit.emit("MARKET_FILTER_REJECTED",cycle_id=cycle_id,pair=instrument.wsname,
                                    reason="SPREAD_TOO_WIDE",spread_bps=ticker.spread_bps)
                    continue
                raw_ohlc=self.client.public("OHLC",{"pair":instrument.altname,"interval":60})
                bars=parse_ohlc_rows(raw_ohlc,instrument.pair_id)
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
                cost=estimate_round_trip_cost(is_margin=is_margin,taker_fee_bps=fee,spread_bps=ticker.spread_bps,
                    slippage_bps_per_side=D(5),holding_hours=self.settings.expected_holding_hours,
                    leverage=int(leverage or 1),margin_open_bps=None,margin_rollover_bps=None)
                edge=(gross_edge_proxy-cost.total_round_trip_bps
                      if cost.complete and cost.total_round_trip_bps is not None else None)

                # Margin is preferred when its fully costed net proxy remains positive. For LONG
                # only, a cost-complete spot plan is evaluated as a real alternative; unknown
                # margin rates must never be silently replaced by invented rates.
                if signal.direction=="LONG" and is_margin and (
                    not cost.complete or edge is None or edge<=0
                ):
                    spot_cost=estimate_round_trip_cost(is_margin=False,taker_fee_bps=fee,
                        spread_bps=ticker.spread_bps,slippage_bps_per_side=D(5),
                        holding_hours=self.settings.expected_holding_hours,leverage=1)
                    spot_edge=(gross_edge_proxy-spot_cost.total_round_trip_bps
                               if spot_cost.complete and spot_cost.total_round_trip_bps is not None else None)
                    if spot_cost.complete:
                        self.audit.emit("MARGIN_PLAN_NOT_SELECTED",cycle_id=cycle_id,pair=instrument.wsname,
                            reason="MARGIN_COSTS_UNVERIFIED_OR_MARGIN_NET_EDGE_NOT_POSITIVE",
                            margin_cost_reasons=list(cost.reasons),spot_cost_bps=spot_cost.total_round_trip_bps,
                            spot_edge_proxy_bps=spot_edge,edge_is_calibrated=False)
                        is_margin=False
                        leverage=1
                        cost=spot_cost
                        edge=spot_edge

                if not cost.complete or cost.total_round_trip_bps is None:
                    counts["blocked"]+=1
                    self.audit.emit("NO_TRADE",cycle_id=cycle_id,pair=instrument.wsname,
                        reason="COST_INPUTS_INCOMPLETE",cost_reasons=list(cost.reasons),
                        margin_fee_rates_confirmed=False,selected_product="SPOT_MARGIN" if is_margin else "CASH_SPOT")
                    continue
                if edge is None or edge<=0:
                    counts["blocked"]+=1
                    self.audit.emit("NO_TRADE",cycle_id=cycle_id,pair=instrument.wsname,
                        reason="NON_POSITIVE_UNCALIBRATED_EDGE_PROXY",edge_bps=edge,
                        cost_bps=cost.total_round_trip_bps,selected_product="SPOT_MARGIN" if is_margin else "CASH_SPOT")
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
                    proposed_notional_usd=proposed)
                if not gate.allowed:
                    counts["blocked"]+=1
                    self.audit.emit("RISK_GATE_REJECTED",cycle_id=cycle_id,pair=instrument.wsname,
                                    direction=signal.direction,reasons=list(gate.reasons))
                    continue
                if proposed<ticker.last*instrument.order_min:
                    counts["blocked"]+=1
                    self.audit.emit("NO_TRADE",cycle_id=cycle_id,pair=instrument.wsname,reason="BELOW_EXCHANGE_MINIMUM")
                    continue
                volume=(proposed/ticker.last).quantize(D(1).scaleb(-instrument.lot_decimals))
                action=(Action.OPEN_SPOT_LONG if signal.direction=="LONG" and not is_margin else
                        Action.OPEN_MARGIN_LONG if signal.direction=="LONG" else Action.OPEN_MARGIN_SHORT)
                intent={"intent_id":str(uuid.uuid4()),"decision_id":cycle_id+":"+instrument.pair_id+":"+signal.direction,
                    "client_order_id":uuid.uuid4().hex,"pair":instrument.pair_id,"action":action.value,
                    "side":signal.direction,"volume":str(volume),"leverage":int(leverage or 1) if is_margin else None,
                    "product_type":"SPOT_MARGIN" if is_margin else "CASH_SPOT",
                    "decision_evidence":{"cycle_id":cycle_id,"net_edge_is_uncalibrated_proxy":True,
                    "estimated_cost_bps":str(cost.total_round_trip_bps),"margin_fee_source":"UNCONFIRMED" if is_margin else "NOT_APPLICABLE"}}
                if self.persistence.create_intent(intent):
                    counts["intents"]+=1
                    self.audit.emit("SHADOW_INTENT_PERSISTED_NO_ORDER_SENT",**intent)
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
