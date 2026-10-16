from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import sqrt
from statistics import mean, pstdev
from typing import Literal, Sequence, cast

from app.strategy import generate_trend_signal
from app.orderbook_simulator import MarginCostEvidence, OrderBookRoundTrip, OrderBookSnapshot, simulate_order_book_round_trip

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
    margin_cost_quality: str | None = None
    margin_cost_evidence_ids: tuple[str, ...] = ()
    margin_cost_sources: tuple[str, ...] = ()

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
    trades = []
    index = 72
    current_equity = equity_usd
    # Require at least one bar after entry so a simulated trade cannot be
    # opened and closed against the same timestamp at the end of the dataset.
    while index + 2 < len(ordered) and current_equity > 0:
        signal=generate_trend_signal("SIM",[x.close for x in ordered[:index+1]],
                                     fast_threshold_bps=fast_threshold_bps,slow_threshold_bps=slow_threshold_bps)
        if not signal.has_direction:
            index+=1; continue
        entry_bar=ordered[index+1]
        if entry_bar.open<=0 or entry_bar.base_volume<=0:
            index+=1; continue
        equity_before_trade = current_equity
        desired = equity_before_trade * position_pct / D(100)
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
        rollovers=max(0,costs.holding_hours//4)
        margin_cost_bps = D(0)
        if costs.leverage > 1:
            # Conservative backtest projection uses full extended notional until
            # a historical position-specific Kraken credit amount is available.
            margin_cost_bps = (
                costs.margin_open_bps_on_extended_amount
                + D(rollovers) * costs.margin_rollover_bps_on_extended_amount
            )
        # Slippage is already embedded in the simulated executable entry/exit prices
        # above. Charge it there exactly once; the separate cost ledger adds fees,
        # quoted round-trip spread and margin financing only.
        total_cost = (
            D(2) * costs.taker_fee_bps_per_side
            + costs.spread_bps_round_trip
            + margin_cost_bps
        ) / D(100)
        gross_pnl = quantity * (exit_price - entry) * sign
        gross = gross_pnl / notional * D(100)
        total_cost_usd = notional * total_cost / D(100)
        net_pnl = gross_pnl - total_cost_usd
        net = net_pnl / notional * D(100)
        equity_return = net_pnl / equity_before_trade * D(100)
        trades.append(SimTrade(
            signal.direction, entry_bar.timestamp, exit_bar.timestamp, entry, exit_price,
            quantity, gross, total_cost, net, exit_reason, notional, gross_pnl,
            total_cost_usd, net_pnl, equity_return,
        ))
        current_equity += net_pnl
        index = max(index + 1, exit_index)
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
    peak_index = 0
    recovery_start: int | None = None
    max_recovery_trades = 0
    for index, ret in enumerate(returns, start=1):
        equity *= D(1) + ret
        if equity >= peak:
            if recovery_start is not None:
                max_recovery_trades = max(max_recovery_trades, index - recovery_start)
                recovery_start = None
            peak = equity
            peak_index = index
        else:
            if recovery_start is None:
                recovery_start = peak_index
            if peak > 0:
                drawdown = max(drawdown, (peak - equity) / peak * D(100))
    if recovery_start is not None:
        max_recovery_trades = max(max_recovery_trades, len(returns) - recovery_start)
    wins = sum((t.net_pnl_usd for t in trades if t.net_pnl_usd > 0), D(0))
    losses = -sum((t.net_pnl_usd for t in trades if t.net_pnl_usd < 0), D(0))
    factor: Decimal | str = wins / losses if losses else ("NO_LOSSES" if wins else D(0))
    floats = [float(x) for x in returns]
    deviation = pstdev(floats) if len(floats) > 1 else 0.0
    sharpe = mean(floats) / deviation * sqrt(len(floats)) if deviation else 0.0
    downside_values = [min(value, 0.0) for value in floats]
    downside_deviation = sqrt(sum(value * value for value in downside_values) / len(downside_values))
    sortino = mean(floats) / downside_deviation * sqrt(len(floats)) if downside_deviation else 0.0
    sorted_returns = sorted(returns)
    tail_count = max(1, int(len(sorted_returns) * 0.05))
    es95 = max(D(0), -sum(sorted_returns[:tail_count], D(0)) / D(tail_count) * D(100))
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
        "max_recovery_trades": max_recovery_trades,
        "expected_shortfall_95_equity_return_pct": es95,
        "max_consecutive_loss_trades": max_streak,
        "sortino_trade_proxy": D(str(sortino)),
        "long_net_pnl_usd": sum((t.net_pnl_usd for t in trades if t.direction == "LONG"), D(0)),
        "short_net_pnl_usd": sum((t.net_pnl_usd for t in trades if t.direction == "SHORT"), D(0)),
        "mean_holding_hours": sum((D(t.exit_time - t.entry_time) / D(3600) for t in trades), D(0)) / D(len(trades)),
        "sharpe_trade_proxy": D(str(sharpe)),
        "long_count": sum(t.direction == "LONG" for t in trades),
        "short_count": sum(t.direction == "SHORT" for t in trades),
        "status": "SCENARIO_ONLY_NOT_LIVE_VALIDATION",
    }



@dataclass(frozen=True)
class OrderBookReplayIssue:
    timestamp: int
    reason: str
    direction: str | None = None


@dataclass(frozen=True)
class OrderBookTrendReplay:
    trades: tuple[SimTrade, ...]
    issues: tuple[OrderBookReplayIssue, ...]
    status: str


def replay_trend_strategy_on_order_books(
    bars: Sequence[Bar],
    costs: BacktestCosts,
    order_books: Sequence[OrderBookSnapshot],
    *,
    margin_cost_evidence_by_entry_timestamp: dict[int, MarginCostEvidence] | None = None,
    quote_currency: str = "USD",
    equity_usd: Decimal = D("10000"),
    position_pct: Decimal = D("10"),
    fast_threshold_bps: Decimal = D("120"),
    slow_threshold_bps: Decimal = D("180"),
    max_holding_bars: int = 8,
    stop_pct: Decimal = D("3"),
    max_book_age_seconds: int = 60,
    max_book_level_participation: Decimal = D("1"),
) -> OrderBookTrendReplay:
    """Run TSM decisions with book-walk fills instead of OHLCV price proxies.

    Bars generate decisions and choose stop/time exit events; a book snapshot
    is selected only if it was captured at or before the event time. Stop
    events discovered from OHLC bars execute at the next bar's opening snapshot
    to avoid using a snapshot captured before the stop was observable.

    The replay halts on an unresolved exit or unknown margin financing so
    remaining risk is not silently treated as a closed, fully costed trade.
    This models marketable taker execution against displayed depth. It does not
    model maker queue priority, exchange latency, hidden liquidity, cancel/replace
    races, or exchange-specific reject behavior; results remain scenario research.
    """
    normalized_quote = quote_currency.strip().upper()
    if normalized_quote != "USD":
        raise ValueError("ORDERBOOK_REPLAY_REQUIRES_USD_QUOTE")
    if costs.leverage < 1 or costs.leverage > 3:
        raise ValueError("LEVERAGE_OUTSIDE_HARD_CAP")
    if max_book_age_seconds < 0 or max_holding_bars < 1:
        raise ValueError("INVALID_ORDERBOOK_REPLAY_TIME_LIMIT")
    if (
        not max_book_level_participation.is_finite()
        or max_book_level_participation <= 0
        or max_book_level_participation > 1
    ):
        raise ValueError("INVALID_ORDERBOOK_LEVEL_PARTICIPATION")
    if (
        not equity_usd.is_finite()
        or equity_usd <= 0
        or not position_pct.is_finite()
        or position_pct <= 0
        or position_pct > D("20")
    ):
        raise ValueError("INVALID_ORDERBOOK_REPLAY_EQUITY_OR_POSITION_PCT")
    if not stop_pct.is_finite() or stop_pct <= 0:
        raise ValueError("INVALID_ORDERBOOK_REPLAY_STOP")
    if any(
        not value.is_finite()
        for value in (
            fast_threshold_bps,
            slow_threshold_bps,
            costs.taker_fee_bps_per_side,
            costs.max_volume_participation,
            costs.minimum_order_usd,
        )
    ):
        raise ValueError("NON_FINITE_ORDERBOOK_REPLAY_INPUT")
    if (
        costs.taker_fee_bps_per_side < 0
        or costs.max_volume_participation <= 0
        or costs.max_volume_participation > 1
        or costs.minimum_order_usd <= 0
    ):
        raise ValueError("INVALID_ORDERBOOK_REPLAY_COST_OR_LIQUIDITY_INPUT")
    ordered = list(bars)
    if any(
        ordered[index].timestamp >= ordered[index + 1].timestamp
        for index in range(len(ordered) - 1)
    ):
        raise ValueError("BARS_MUST_BE_STRICTLY_CHRONOLOGICAL")
    for bar in ordered:
        values = (bar.open, bar.high, bar.low, bar.close, bar.base_volume)
        if any(not value.is_finite() for value in values):
            raise ValueError("NON_FINITE_OHLCV_DATA")
        if (
            min(bar.open, bar.high, bar.low, bar.close) <= 0
            or bar.base_volume < 0
            or bar.high < max(bar.open, bar.close, bar.low)
            or bar.low > min(bar.open, bar.close, bar.high)
        ):
            raise ValueError("INVALID_OHLCV_BAR")
    book_rows = sorted(order_books, key=lambda item: item.timestamp)
    if any(
        book_rows[index].timestamp == book_rows[index + 1].timestamp
        for index in range(len(book_rows) - 1)
    ):
        raise ValueError("DUPLICATE_ORDER_BOOK_TIMESTAMPS")

    def book_at(event_timestamp: int) -> OrderBookSnapshot | None:
        eligible = [item for item in book_rows if item.timestamp <= event_timestamp]
        if not eligible:
            return None
        latest = eligible[-1]
        if event_timestamp - latest.timestamp > max_book_age_seconds:
            return None
        return latest

    trades: list[SimTrade] = []
    issues: list[OrderBookReplayIssue] = []
    current_equity = equity_usd
    index = 72
    while index + 2 < len(ordered) and current_equity > 0:
        signal = generate_trend_signal(
            "SIM",
            [item.close for item in ordered[: index + 1]],
            fast_threshold_bps=fast_threshold_bps,
            slow_threshold_bps=slow_threshold_bps,
        )
        if not signal.has_direction:
            index += 1
            continue
        if signal.direction not in ("LONG", "SHORT"):
            issues.append(
                OrderBookReplayIssue(ordered[index].timestamp, "SIGNAL_DIRECTION_INVALID")
            )
            index += 1
            continue
        direction = cast(Literal["LONG", "SHORT"], signal.direction)

        entry_bar = ordered[index + 1]
        if entry_bar.open <= 0 or entry_bar.base_volume <= 0:
            issues.append(OrderBookReplayIssue(entry_bar.timestamp, "ENTRY_BAR_HAS_NO_VOLUME", signal.direction))
            index += 1
            continue
        entry_book = book_at(entry_bar.timestamp)
        if entry_book is None:
            issues.append(OrderBookReplayIssue(entry_bar.timestamp, "ENTRY_BOOK_MISSING_OR_STALE", signal.direction))
            index += 1
            continue

        desired_notional = current_equity * position_pct / D(100)
        candle_volume_notional = entry_bar.open * entry_bar.base_volume * costs.max_volume_participation
        target_notional = min(desired_notional, candle_volume_notional)
        if target_notional < costs.minimum_order_usd:
            issues.append(OrderBookReplayIssue(entry_bar.timestamp, "ORDER_BELOW_MINIMUM", signal.direction))
            index += 1
            continue

        last_exit_index = min(len(ordered) - 1, index + max(1, max_holding_bars))
        exit_index = last_exit_index
        exit_reason = "TIME_EXIT"
        entry_reference = (entry_book.bids[0].price + entry_book.asks[0].price) / D(2)
        stop_without_following_bar = False
        for probe_index, probe in enumerate(
            ordered[index + 1 : last_exit_index + 1],
            start=index + 1,
        ):
            adverse_pct = (
                (probe.low / entry_reference - D(1)) * D(100)
                if signal.direction == "LONG"
                else (entry_reference - probe.high) / entry_reference * D(100)
            )
            if adverse_pct <= -stop_pct:
                if probe_index + 1 >= len(ordered):
                    stop_without_following_bar = True
                else:
                    exit_index = probe_index + 1
                    exit_reason = "STOP_EXECUTED_NEXT_BAR"
                break
        if stop_without_following_bar:
            issues.append(
                OrderBookReplayIssue(
                    ordered[-1].timestamp,
                    "STOP_TRIGGERED_WITHOUT_LATER_EXECUTION_BOOK_POSITION_UNRESOLVED",
                    signal.direction,
                )
            )
            break

        exit_event_timestamp = ordered[exit_index].timestamp
        exit_book = book_at(exit_event_timestamp)
        if exit_book is None:
            issues.append(
                OrderBookReplayIssue(
                    exit_event_timestamp,
                    "EXIT_BOOK_MISSING_OR_STALE_POSITION_UNRESOLVED",
                    signal.direction,
                )
            )
            break

        margin_evidence = (
            margin_cost_evidence_by_entry_timestamp.get(entry_bar.timestamp)
            if margin_cost_evidence_by_entry_timestamp is not None
            else None
        )
        try:
            round_trip: OrderBookRoundTrip = simulate_order_book_round_trip(
                entry_book,
                exit_book,
                direction=direction,
                requested_notional_quote=target_notional,
                entry_fee_bps=costs.taker_fee_bps_per_side,
                exit_fee_bps=costs.taker_fee_bps_per_side,
                quote_currency=normalized_quote,
                is_margin=costs.leverage > 1,
                margin_cost_evidence=margin_evidence,
                max_level_participation=max_book_level_participation,
            )
        except ValueError as exc:
            issues.append(
                OrderBookReplayIssue(
                    entry_bar.timestamp,
                    f"ORDERBOOK_EXECUTION_REJECTED:{str(exc)}",
                    signal.direction,
                )
            )
            index += 1
            continue

        if round_trip.position_status == "NOT_OPENED":
            issues.append(OrderBookReplayIssue(entry_bar.timestamp, "ENTRY_UNFILLED", signal.direction))
            index += 1
            continue
        if round_trip.remaining_quantity > 0:
            issues.append(
                OrderBookReplayIssue(
                    exit_event_timestamp,
                    "EXIT_PARTIAL_POSITION_REMAINS_OPEN",
                    signal.direction,
                )
            )
            break
        if round_trip.net_realized_pnl_quote is None:
            issues.append(
                OrderBookReplayIssue(
                    exit_event_timestamp,
                    "NET_PNL_UNVERIFIABLE_MARGIN_COSTS",
                    signal.direction,
                )
            )
            break
        if (
            round_trip.entry is None
            or round_trip.exit is None
            or round_trip.entry.average_price is None
            or round_trip.exit.average_price is None
            or round_trip.gross_realized_pnl_quote is None
            or round_trip.margin_cost_quote is None
        ):
            issues.append(OrderBookReplayIssue(exit_event_timestamp, "EXECUTION_ACCOUNTING_INCOMPLETE", signal.direction))
            break

        trade_notional = round_trip.entry.average_price * round_trip.closed_quantity
        if trade_notional <= 0:
            issues.append(OrderBookReplayIssue(exit_event_timestamp, "EXECUTION_NOTIONAL_INVALID", signal.direction))
            break
        total_cost_quote = round_trip.trading_fees_quote + round_trip.margin_cost_quote
        gross_pnl = round_trip.gross_realized_pnl_quote
        net_pnl = round_trip.net_realized_pnl_quote
        trades.append(
            SimTrade(
                direction=direction,
                entry_time=entry_book.timestamp,
                exit_time=exit_book.timestamp,
                entry=round_trip.entry.average_price,
                exit=round_trip.exit.average_price,
                quantity=round_trip.closed_quantity,
                gross_return_pct=gross_pnl / trade_notional * D(100),
                total_cost_pct=total_cost_quote / trade_notional * D(100),
                net_return_pct=net_pnl / trade_notional * D(100),
                exit_reason=exit_reason,
                notional_usd=trade_notional,
                gross_pnl_usd=gross_pnl,
                total_cost_usd=total_cost_quote,
                net_pnl_usd=net_pnl,
                equity_return_pct=net_pnl / current_equity * D(100),
                margin_cost_quality=round_trip.margin_cost_evidence_quality,
                margin_cost_evidence_ids=round_trip.margin_cost_evidence_ids,
                margin_cost_sources=round_trip.margin_cost_sources,
            )
        )
        current_equity += net_pnl
        index = max(index + 1, exit_index)

    return OrderBookTrendReplay(
        tuple(trades),
        tuple(issues),
        "ORDERBOOK_SCENARIO_ONLY" if not issues else "INCOMPLETE_REQUIRES_REVIEW",
    )
