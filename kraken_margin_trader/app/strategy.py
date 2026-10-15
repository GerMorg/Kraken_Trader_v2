from __future__ import annotations

from decimal import Decimal
import math
from statistics import pstdev
from typing import Sequence

from app.models import TrendSignal

D = Decimal

def generate_trend_signal(pair_id: str, closes: Sequence[Decimal], *,
                          fast_threshold_bps: Decimal = D("120"),
                          slow_threshold_bps: Decimal = D("180")) -> TrendSignal:
    if len(closes)<73:
        return TrendSignal(pair_id,"NONE",D(0),D(0),D(0),D(0),"INSUFFICIENT_HISTORY")
    prices=list(closes)
    if any(not x.is_finite() or x<=0 for x in prices[-73:]):
        return TrendSignal(pair_id,"NONE",D(0),D(0),D(0),D(0),"INVALID_PRICE_HISTORY")
    last=prices[-1]
    fast=(last/prices[-25]-D(1))*D(10000)
    slow=(last/prices[-73]-D(1))*D(10000)
    fast_mean=sum(prices[-24:],D(0))/D(24)
    slow_mean=sum(prices[-72:],D(0))/D(72)
    returns=[float(prices[i]/prices[i-1]-D(1)) for i in range(len(prices)-24,len(prices)) if prices[i-1]>0]
    sigma=D(str(pstdev(returns)*math.sqrt(24)*100)) if returns else D(0)
    stop=max(D("1.5"),min(D("5.0"),sigma*D(2)))
    if fast>=fast_threshold_bps and slow>=slow_threshold_bps and last>fast_mean>slow_mean:
        return TrendSignal(pair_id,"LONG",fast,slow,min(D(1),fast/D(500)+slow/D(2000)),stop,"TREND_UP")
    if fast<=-fast_threshold_bps and slow<=-slow_threshold_bps and last<fast_mean<slow_mean:
        return TrendSignal(pair_id,"SHORT",fast,slow,min(D(1),abs(fast)/D(500)+abs(slow)/D(2000)),stop,"TREND_DOWN")
    return TrendSignal(pair_id,"NONE",fast,slow,D(0),stop,"NO_CONFIRMED_TREND")

def take_profit_quantity(position_quantity: Decimal, fraction: Decimal = D("0.5")) -> Decimal:
    if position_quantity<=0:
        raise ValueError("POSITION_QUANTITY_MUST_BE_POSITIVE")
    if not 0<fraction<1:
        raise ValueError("PARTIAL_FRACTION_MUST_BE_BETWEEN_ZERO_AND_ONE")
    return position_quantity*fraction

def profit_trigger_reached(entry_price: Decimal, current_price: Decimal,
                            side: str, trigger_pct: Decimal) -> bool:
    if entry_price<=0 or current_price<=0 or trigger_pct<=0:
        return False
    direction=D(1) if side.upper()=="LONG" else D(-1)
    return ((current_price/entry_price-D(1))*direction*D(100))>=trigger_pct