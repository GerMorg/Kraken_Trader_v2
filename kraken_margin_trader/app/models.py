from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

D = Decimal

class Action(str, Enum):
    OPEN_SPOT_LONG = "OPEN_SPOT_LONG"
    OPEN_MARGIN_LONG = "OPEN_MARGIN_LONG"
    OPEN_MARGIN_SHORT = "OPEN_MARGIN_SHORT"
    INCREASE_LONG = "INCREASE_LONG"
    INCREASE_SHORT = "INCREASE_SHORT"
    REDUCE_LONG = "REDUCE_LONG"
    REDUCE_SHORT = "REDUCE_SHORT"
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"
    SETTLE_MARGIN_POSITION = "SETTLE_MARGIN_POSITION"
    NO_TRADE = "NO_TRADE"
    RISK_REDUCTION_REQUIRED = "RISK_REDUCTION_REQUIRED"

class OrderState(str, Enum):
    INTENT_CREATED = "INTENT_CREATED"
    PRECHECK_PASSED = "PRECHECK_PASSED"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"
    UNKNOWN_RECONCILING = "UNKNOWN_RECONCILING"

@dataclass(frozen=True)
class Instrument:
    pair_id: str
    altname: str
    wsname: str
    base: str
    quote: str
    status: str
    order_min: Decimal
    lot_decimals: int
    pair_decimals: int
    leverage_buy: tuple[int, ...]
    leverage_sell: tuple[int, ...]

    def leverages_for_order_side(self, side: str) -> tuple[int, ...]:
        return self.leverage_buy if side.lower() == "buy" else self.leverage_sell

@dataclass(frozen=True)
class Ticker:
    pair_id: str
    last: Decimal
    bid: Decimal
    ask: Decimal
    volume_quote_24h: Decimal

    @property
    def spread_bps(self) -> Decimal:
        mid = (self.bid + self.ask) / D("2")
        if mid <= 0 or self.ask < self.bid:
            return D("Infinity")
        return (self.ask - self.bid) / mid * D("10000")

@dataclass(frozen=True)
class TrendSignal:
    pair_id: str
    direction: str
    momentum_fast_bps: Decimal
    momentum_slow_bps: Decimal
    score: Decimal
    stop_distance_pct: Decimal
    reason: str
    trend_efficiency_24h: Decimal = D(0)
    trend_efficiency_72h: Decimal = D(0)

    @property
    def has_direction(self) -> bool:
        return self.direction in {"LONG", "SHORT"}

@dataclass(frozen=True)
class RiskSnapshot:
    equity_usd: Decimal
    free_collateral_usd: Decimal
    used_margin_usd: Decimal
    gross_exposure_usd: Decimal
    net_exposure_usd: Decimal
    open_positions: int
    daily_pnl_pct: Decimal | None
    drawdown_pct: Decimal | None
    margin_level_pct: Decimal | None
    snapshot_age_seconds: Decimal
    complete: bool
    reconciliation_ok: bool

def decimal_value(value: Any, fallback: str = "0") -> Decimal:
    try:
        result = D(str(value))
        return result if result.is_finite() else D(fallback)
    except (ArithmeticError, TypeError, ValueError):
        return D(fallback)
