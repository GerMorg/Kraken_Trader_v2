from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import sqrt
from statistics import mean, pstdev
from typing import Sequence

from app.strategy import generate_trend_signal

D=Decimal

@dataclass(frozen=True)
class Bar:
    timestamp: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal

@dataclass(frozen=True)
class BacktestCosts:
    taker_fee_bps_per_side: Decimal
    spread_bps_round_trip: Decimal
    slippage_bps_per_side: Decimal
    margin_open_bps_on_borrowed: Decimal
    margin_rollover_bps_on_borrowed: Decimal
    holding_hours: int=8
    leverage: int=2
    max_volume_participation: Decimal=D("0.01")
    minimum_order_usd: Decimal=D("5")

@dataclass(frozen=True)
class SimTrade:
    direction: str
    entry_time: int
    exit_time: int
    entry: Decimal
    exit: Decimal
    quantity: Decimal
    gross_return_pct: Decimal
    total_cost_pct: Decimal
    net_return_pct: Decimal
    exit_reason: str

def chronological_split(values: Sequence[Bar], train_fraction: float=0.6,
                        validation_fraction: float=0.2) -> tuple[list[Bar],list[Bar],list[Bar]]:
    if not (0<train_fraction<1 and 0<validation_fraction<1 and train_fraction+validation_fraction<1):
        raise ValueError("INVALID_CHRONOLOGICAL_SPLIT_FRACTIONS")
    ordered=list(values)
    if any(ordered[i].timestamp>=ordered[i+1].timestamp for i in range(len(ordered)-1)):
        raise ValueError("BARS_MUST_BE_STRICTLY_CHRONOLOGICAL")
    n=len(ordered); a=int(n*train_fraction); b=int(n*(train_fraction+validation_fraction))
    if a==0 or b<=a or b>=n:
        raise ValueError("INSUFFICIENT_BARS_FOR_CHRONOLOGICAL_SPLIT")
    return ordered[:a],ordered[a:b],ordered[b:]

def simulate_trend_trades(bars: Sequence[Bar], costs: BacktestCosts, *,
                           equity_usd: Decimal=D("10000"), position_pct: Decimal=D("10"),
                           fast_threshold_bps: Decimal=D("120"), slow_threshold_bps: Decimal=D("180"),
                           max_holding_bars: int=8, stop_pct: Decimal=D("3")) -> list[SimTrade]:
    if costs.leverage<1 or costs.leverage>3:
        raise ValueError("LEVERAGE_OUTSIDE_HARD_CAP")
    if costs.minimum_order_usd<=0 or costs.max_volume_participation<=0:
        raise ValueError("INVALID_SIMULATOR_LIQUIDITY_LIMITS")
    if min(costs.taker_fee_bps_per_side,costs.spread_bps_round_trip,costs.slippage_bps_per_side,
           costs.margin_open_bps_on_borrowed,costs.margin_rollover_bps_on_borrowed)<0:
        raise ValueError("NEGATIVE_SIMULATOR_COST")
    ordered=list(bars)
    if any(ordered[i].timestamp>=ordered[i+1].timestamp for i in range(len(ordered)-1)):
        raise ValueError("BARS_MUST_BE_STRICTLY_CHRONOLOGICAL")
    trades=[]; index=72
    while index+1<len(ordered):
        signal=generate_trend_signal("SIM",[x.close for x in ordered[:index+1]],
                                     fast_threshold_bps=fast_threshold_bps,slow_threshold_bps=slow_threshold_bps)
        if not signal.has_direction:
            index+=1; continue
        entry_bar=ordered[index+1]
        if entry_bar.open<=0 or entry_bar.base_volume<=0:
            index+=1; continue
        desired=equity_usd*position_pct/D(100)
        # Bar volume is only a proxy for reachable depth; this model is explicitly not an order-book simulator.
        notional=min(desired,entry_bar.open*entry_bar.base_volume*costs.max_volume_participation)
        if notional<costs.minimum_order_usd:
            index+=1; continue
        quantity=notional/entry_bar.open
        sign=D(1) if signal.direction=="LONG" else D(-1)
        entry=entry_bar.open+sign*entry_bar.open*costs.slippage_bps_per_side/D(10000)
        end=min(len(ordered)-1,index+max(1,max_holding_bars))
        exit_price=ordered[end].open; exit_reason="TIME_EXIT"; exit_index=end
        for probe_index,probe in enumerate(ordered[index+1:end+1],start=index+1):
            adverse=((probe.low/entry-D(1))*D(100) if signal.direction=="LONG"
                     else (entry/probe.high-D(1))*D(100))
            if adverse<=-stop_pct:
                trigger=entry*(D(1)-stop_pct/D(100) if signal.direction=="LONG" else D(1)+stop_pct/D(100))
                exit_price=min(trigger,probe.open) if signal.direction=="LONG" else max(trigger,probe.open)
                exit_index=probe_index; exit_reason="STOP_GAP_CONSERVATIVE"; break
        exit_bar=ordered[exit_index]
        exit_price-=sign*exit_price*costs.slippage_bps_per_side/D(10000)
        gross=((exit_price/entry)-D(1))*sign*D(100)
        rollovers=max(0,costs.holding_hours//4)
        borrowed=D(1)-D(1)/D(max(1,costs.leverage)) if costs.leverage>1 else D(0)
        total_cost=(D(2)*costs.taker_fee_bps_per_side+costs.spread_bps_round_trip+
                    D(2)*costs.slippage_bps_per_side)/D(100)+borrowed*(
                    costs.margin_open_bps_on_borrowed+D(rollovers)*costs.margin_rollover_bps_on_borrowed)/D(100)
        trades.append(SimTrade(signal.direction,entry_bar.timestamp,exit_bar.timestamp,entry,exit_price,
                               quantity,gross,total_cost,gross-total_cost,exit_reason))
        index=max(index+1,exit_index)
    return trades

def summary_metrics(trades: Sequence[SimTrade]) -> dict[str,Decimal|int|str]:
    if not trades:
        return {"trade_count":0,"net_expectancy_pct":D(0),"profit_factor":D(0),"max_drawdown_pct":D(0),
                "sharpe_trade_proxy":D(0),"long_count":0,"short_count":0,"status":"NO_TRADES"}
    returns=[t.net_return_pct/D(100) for t in trades]
    average=sum(returns,D(0))/D(len(returns))
    equity=peak=D(1); drawdown=D(0)
    for ret in returns:
        equity*=D(1)+ret; peak=max(peak,equity)
        if peak>0: drawdown=max(drawdown,(peak-equity)/peak*D(100))
    wins=sum((x for x in returns if x>0),D(0)); losses=-sum((x for x in returns if x<0),D(0))
    factor=wins/losses if losses else (D("Infinity") if wins else D(0))
    floats=[float(x) for x in returns]; deviation=pstdev(floats) if len(floats)>1 else 0.0
    sharpe=mean(floats)/deviation*sqrt(len(floats)) if deviation else 0.0
    return {"trade_count":len(trades),"net_expectancy_pct":average*D(100),"profit_factor":factor,
            "max_drawdown_pct":drawdown,"sharpe_trade_proxy":D(str(sharpe)),
            "long_count":sum(t.direction=="LONG" for t in trades),
            "short_count":sum(t.direction=="SHORT" for t in trades),
            "status":"SCENARIO_ONLY_NOT_LIVE_VALIDATION"}