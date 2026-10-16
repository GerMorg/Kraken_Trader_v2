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
    margin_open_bps_on_extended_amount: Decimal
    margin_rollover_bps_on_extended_amount: Decimal
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
    notional_usd: Decimal
    gross_pnl_usd: Decimal
    total_cost_usd: Decimal
    net_pnl_usd: Decimal
    equity_return_pct: Decimal

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
    if costs.leverage < 1 or costs.leverage > 3:
        raise ValueError("LEVERAGE_OUTSIDE_HARD_CAP")
    policy_values = (
        equity_usd, position_pct, fast_threshold_bps, slow_threshold_bps, stop_pct,
        costs.taker_fee_bps_per_side, costs.spread_bps_round_trip,
        costs.slippage_bps_per_side, costs.margin_open_bps_on_extended_amount,
        costs.margin_rollover_bps_on_extended_amount, costs.max_volume_participation,
        costs.minimum_order_usd,
    )
    if any(not value.is_finite() for value in policy_values):
        raise ValueError("NON_FINITE_SIMULATOR_INPUT")
    if equity_usd <= 0 or position_pct <= 0 or position_pct > D("20"):
        raise ValueError("INVALID_SIMULATOR_EQUITY_OR_POSITION_PCT")
    if max_holding_bars < 1 or stop_pct <= 0:
        raise ValueError("INVALID_SIMULATOR_EXIT_POLICY")
    if (
        costs.minimum_order_usd <= 0
        or costs.max_volume_participation <= 0
        or costs.max_volume_participation > D("1")
    ):
        raise ValueError("INVALID_SIMULATOR_LIQUIDITY_LIMITS")
    if min(
        costs.taker_fee_bps_per_side, costs.spread_bps_round_trip,
        costs.slippage_bps_per_side, costs.margin_open_bps_on_extended_amount,
        costs.margin_rollover_bps_on_extended_amount,
    ) < 0:
        raise ValueError("NEGATIVE_SIMULATOR_COST")
    ordered = list(bars)
    if any(ordered[i].timestamp >= ordered[i+1].timestamp for i in range(len(ordered)-1)):
        raise ValueError("BARS_MUST_BE_STRICTLY_CHRONOLOGICAL")
    for bar in ordered:
        prices = (bar.open, bar.high, bar.low, bar.close, bar.base_volume)
        if any(not value.is_finite() for value in prices):
            raise ValueError("NON_FINITE_OHLCV_DATA")
        if (
            min(bar.open, bar.high, bar.low, bar.close) <= 0
            or bar.base_volume < 0
            or bar.high < max(bar.open, bar.close, bar.low)
            or bar.low > min(bar.open, bar.close, bar.high)
        ):
            raise ValueError("INVALID_OHLCV_BAR")
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
            adverse=((probe.low / entry - D(1)) * D(100) if signal.direction == "LONG"
                     else (entry - probe.high) / entry * D(100))
            if adverse<=-stop_pct:
                trigger=entry*(D(1)-stop_pct/D(100) if signal.direction=="LONG" else D(1)+stop_pct/D(100))
                exit_price=min(trigger,probe.open) if signal.direction=="LONG" else max(trigger,probe.open)
                exit_index=probe_index; exit_reason="STOP_GAP_CONSERVATIVE"; break
        exit_bar=ordered[exit_index]
        exit_price-=sign*exit_price*costs.slippage_bps_per_side/D(10000)
        gross=((exit_price/entry)-D(1))*sign*D(100)
        rollovers=max(0,costs.holding_hours//4)
        margin_cost_bps = D(0)
        if costs.leverage > 1:
            # Conservative backtest projection uses full extended notional until
            # a historical position-specific Kraken credit amount is available.
            margin_cost_bps = (
                costs.margin_open_bps_on_extended_amount
                + D(rollovers) * costs.margin_rollover_bps_on_extended_amount
            )
        total_cost = (
            D(2) * costs.taker_fee_bps_per_side
            + costs.spread_bps_round_trip
            + D(2) * costs.slippage_bps_per_side
            + margin_cost_bps
        ) / D(100)
        net = gross - total_cost
        gross_pnl = notional * gross / D(100)
        total_cost_usd = notional * total_cost / D(100)
        net_pnl = gross_pnl - total_cost_usd
        equity_return = net_pnl / equity_usd * D(100)
        trades.append(SimTrade(
            signal.direction, entry_bar.timestamp, exit_bar.timestamp, entry, exit_price,
            quantity, gross, total_cost, net, exit_reason, notional, gross_pnl,
            total_cost_usd, net_pnl, equity_return,
        ))
        index=max(index+1,exit_index)
    return trades

def summary_metrics(trades: Sequence[SimTrade]) -> dict[str,Decimal|int|str]:
    if not trades:
        return {"trade_count":0,"net_expectancy_pct":D(0),"profit_factor":D(0),"max_drawdown_pct":D(0),
                "sharpe_trade_proxy":D(0),"long_count":0,"short_count":0,"status":"NO_TRADES"}
    # Equity returns are based on actual simulated USD PnL divided by seed equity,
    # not raw position return; this prevents a 10%-equity position being treated as
    # though it represented 100% of the portfolio.
    returns = [t.equity_return_pct / D(100) for t in trades]
    average = sum(returns, D(0)) / D(len(returns))
    equity = peak = D(1)
    drawdown = D(0)
    for ret in returns:
        equity *= D(1) + ret
        peak = max(peak, equity)
        if peak > 0:
            drawdown = max(drawdown, (peak - equity) / peak * D(100))
    wins = sum((t.net_pnl_usd for t in trades if t.net_pnl_usd > 0), D(0))
    losses = -sum((t.net_pnl_usd for t in trades if t.net_pnl_usd < 0), D(0))
    factor: Decimal | str = wins / losses if losses else ("NO_LOSSES" if wins else D(0))
    floats = [float(x) for x in returns]
    deviation = pstdev(floats) if len(floats) > 1 else 0.0
    sharpe = mean(floats) / deviation * sqrt(len(floats)) if deviation else 0.0
    sorted_returns = sorted(returns)
    tail_count = max(1, int(len(sorted_returns) * 0.05))
    es95 = -sum(sorted_returns[:tail_count], D(0)) / D(tail_count) * D(100)
    loss_streak = max_streak = 0
    for item in trades:
        if item.net_pnl_usd < 0:
            loss_streak += 1
            max_streak = max(max_streak, loss_streak)
        else:
            loss_streak = 0
    total_costs = sum((t.total_cost_usd for t in trades), D(0))
    gross_pnl = sum((t.gross_pnl_usd for t in trades), D(0))
    net_pnl = sum((t.net_pnl_usd for t in trades), D(0))
    turnover = sum((t.notional_usd * D(2) for t in trades), D(0))
    cost_share = (
        total_costs / abs(gross_pnl) * D(100) if gross_pnl != 0
        else (D(100) if total_costs > 0 else D(0))
    )
    return {
        "trade_count": len(trades),
        "net_expectancy_pct": average * D(100),
        "gross_pnl_usd": gross_pnl,
        "total_costs_usd": total_costs,
        "net_pnl_usd": net_pnl,
        "cost_share_of_absolute_gross_pnl_pct": cost_share,
        "turnover_usd": turnover,
        "profit_factor": factor,
        "max_drawdown_pct": drawdown,
        "expected_shortfall_95_equity_return_pct": es95,
        "max_consecutive_loss_trades": max_streak,
        "sharpe_trade_proxy": D(str(sharpe)),
        "long_count": sum(t.direction == "LONG" for t in trades),
        "short_count": sum(t.direction == "SHORT" for t in trades),
        "status": "SCENARIO_ONLY_NOT_LIVE_VALIDATION",
    }
