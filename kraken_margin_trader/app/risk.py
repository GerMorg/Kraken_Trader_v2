from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings
from app.market import margin_leverage_for
from app.models import Instrument, RiskSnapshot, TrendSignal

D = Decimal


@dataclass(frozen=True)
class RiskResult:
    allowed: bool
    reasons: tuple[str, ...]
    notional_usd: Decimal
    leverage: int
    margin_required_usd: Decimal


def size_position_usd(
    *,
    equity_usd: Decimal,
    stop_distance_pct: Decimal,
    gap_buffer_pct: Decimal,
    cost_bps: Decimal,
    max_position_pct: Decimal = D("20"),
    risk_budget_pct: Decimal = D("0.5"),
    free_collateral_usd: Decimal | None = None,
    leverage: int = 1,
) -> Decimal:
    values = (
        equity_usd,
        stop_distance_pct,
        gap_buffer_pct,
        cost_bps,
        max_position_pct,
        risk_budget_pct,
    )
    if any(not value.is_finite() for value in values):
        return D(0)
    if (
        equity_usd <= 0
        or stop_distance_pct <= 0
        or gap_buffer_pct < 0
        or cost_bps < 0
        or max_position_pct <= 0
        or risk_budget_pct <= 0
        or leverage < 1
    ):
        return D(0)

    # Express every component in percentage points before dividing the risk budget.
    risk_fraction = (stop_distance_pct + gap_buffer_pct + cost_bps / D(100)) / D(100)
    if not risk_fraction.is_finite() or risk_fraction <= 0:
        return D(0)

    notional = min(
        equity_usd * risk_budget_pct / D(100) / risk_fraction,
        equity_usd * max_position_pct / D(100),
    )
    if leverage > 1:
        if free_collateral_usd is None or not free_collateral_usd.is_finite() or free_collateral_usd <= 0:
            return D(0)
        notional = min(notional, free_collateral_usd * D(leverage))
    return max(D(0), notional)


def choose_trade_leverage(
    instrument: Instrument,
    direction: str,
    settings: Settings,
) -> int | None:
    normalized = direction.upper()
    if normalized not in {"LONG", "SHORT"}:
        return None
    order_side = "buy" if normalized == "LONG" else "sell"
    return margin_leverage_for(instrument, order_side, settings.risk_max_leverage)


def evaluate_entry(
    *,
    instrument: Instrument,
    signal: TrendSignal,
    snapshot: RiskSnapshot,
    settings: Settings,
    cost_complete: bool,
    estimated_net_edge_bps: Decimal | None,
    leverage: int,
    proposed_notional_usd: Decimal,
) -> RiskResult:
    reasons: list[str] = []

    finite_values = (
        snapshot.equity_usd,
        snapshot.free_collateral_usd,
        snapshot.used_margin_usd,
        snapshot.gross_exposure_usd,
        snapshot.net_exposure_usd,
        snapshot.daily_pnl_pct,
        snapshot.drawdown_pct,
        snapshot.snapshot_age_seconds,
        proposed_notional_usd,
    )
    if any(not value.is_finite() for value in finite_values):
        reasons.append("RISK_INPUT_NON_FINITE")
    if (
        snapshot.free_collateral_usd < 0
        or snapshot.used_margin_usd < 0
        or snapshot.gross_exposure_usd < 0
        or snapshot.snapshot_age_seconds < 0
    ):
        reasons.append("RISK_INPUT_NEGATIVE")
    if snapshot.margin_level_pct is not None and not snapshot.margin_level_pct.is_finite():
        reasons.append("MARGIN_LEVEL_NON_FINITE")

    if not signal.has_direction:
        reasons.append("NO_DIRECTIONAL_SIGNAL")
    if not snapshot.complete:
        reasons.append("ACCOUNT_SNAPSHOT_INCOMPLETE")
    if not snapshot.reconciliation_ok:
        reasons.append("PORTFOLIO_RECONCILIATION_FAILED")
    if snapshot.snapshot_age_seconds > D(settings.market_max_age_seconds):
        reasons.append("ACCOUNT_SNAPSHOT_STALE")
    if snapshot.equity_usd <= 0:
        reasons.append("NON_POSITIVE_EQUITY")
    if not cost_complete:
        reasons.append("TOTAL_COSTS_UNVERIFIED")
    if estimated_net_edge_bps is None or not estimated_net_edge_bps.is_finite() or estimated_net_edge_bps <= 0:
        reasons.append("NO_POSITIVE_NET_EDGE")
    if leverage < 1 or leverage > settings.risk_max_leverage:
        reasons.append("LEVERAGE_OUTSIDE_CODE_CAP")
    if snapshot.open_positions >= settings.risk_max_open_positions:
        reasons.append("OPEN_POSITION_LIMIT")
    if snapshot.daily_pnl_pct <= -settings.risk_daily_loss_pct:
        reasons.append("DAILY_LOSS_LIMIT")
    if snapshot.drawdown_pct >= settings.risk_max_drawdown_pct:
        reasons.append("DRAWDOWN_LIMIT")
    if signal.direction == "LONG" and leverage > 1 and not instrument.leverage_buy:
        reasons.append("LONG_MARGIN_NOT_SUPPORTED")
    if signal.direction == "SHORT" and not instrument.leverage_sell:
        reasons.append("SHORT_MARGIN_NOT_SUPPORTED")

    position_cap = snapshot.equity_usd * settings.risk_max_position_pct / D(100)
    gross_cap = snapshot.equity_usd * settings.risk_max_gross_pct / D(100)
    net_cap = snapshot.equity_usd * settings.risk_max_net_pct / D(100)
    margin_cap = snapshot.equity_usd * settings.risk_max_margin_pct / D(100)
    margin_required = proposed_notional_usd / D(leverage) if leverage > 0 else proposed_notional_usd

    if proposed_notional_usd <= 0:
        reasons.append("NON_POSITIVE_ORDER_NOTIONAL")
    if proposed_notional_usd > position_cap:
        reasons.append("POSITION_LIMIT")
    if snapshot.gross_exposure_usd + proposed_notional_usd > gross_cap:
        reasons.append("GROSS_EXPOSURE_LIMIT")

    # Net exposure is directional: an opposite-side trade can hedge existing exposure.
    # Gross exposure is still increased by the full proposed notional above.
    direction_sign = D(1) if signal.direction == "LONG" else D(-1)
    projected_net = snapshot.net_exposure_usd + direction_sign * proposed_notional_usd
    if abs(projected_net) > net_cap:
        reasons.append("NET_EXPOSURE_LIMIT")

    # Keep a cash/collateral reserve even when Kraken reports more free collateral.
    reserve_factor = (D(100) - settings.risk_cash_reserve_pct) / D(100)
    reserve_adjusted_collateral = max(D(0), snapshot.equity_usd * reserve_factor)
    usable_collateral = min(snapshot.free_collateral_usd, reserve_adjusted_collateral)
    if margin_required > margin_cap or margin_required > usable_collateral:
        reasons.append("MARGIN_CAP_OR_COLLATERAL")
    if leverage > 1 and (
        snapshot.margin_level_pct is None or snapshot.margin_level_pct < D(300)
    ):
        reasons.append("MARGIN_LEVEL_UNKNOWN_OR_BELOW_APP_ENTRY_FLOOR")

    # Keep the rejection list stable and free of duplicate reasons for audit logs.
    return RiskResult(
        not reasons,
        tuple(dict.fromkeys(reasons)),
        proposed_notional_usd,
        leverage,
        margin_required,
    )
