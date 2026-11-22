from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Mapping

D = Decimal

# The Home Assistant setting is the only operator switch. A separate code-owned
# release gate remains closed until both exchange execution/protection lifecycles
# have passed their complete validation programme.
INTERNAL_LIVE_EXECUTION_RELEASED = False

# Expensive exploratory research is deliberately not part of the trading cycle.
# It may be re-enabled in a separate, explicitly scheduled research worker later.
BACKGROUND_RESEARCH_ENABLED = False

def _decimal(raw: Mapping[str, Any], key: str, default: str) -> Decimal:
    try:
        value = D(str(raw.get(key, default)))
        return value if value.is_finite() else D(default)
    except (InvalidOperation, ValueError, TypeError):
        return D(default)


def _positive_decimal(raw: Mapping[str, Any], key: str, default: str) -> Decimal:
    """Read an operator-defined percentage without a code-owned upper cap."""
    value = _decimal(raw, key, default)
    return value if value > 0 else D(default)


def _nonnegative_decimal(raw: Mapping[str, Any], key: str, default: str) -> Decimal:
    """Read an operator-defined non-negative risk threshold without an app cap."""
    value = _decimal(raw, key, default)
    return value if value >= 0 else D(default)


def _reserve_percentage(raw: Mapping[str, Any], key: str, default: str) -> Decimal:
    """A reserve is the one percentage with a natural 0..100% domain."""
    value = _decimal(raw, key, default)
    return value if D(0) <= value <= D(100) else D(default)


def _unit_interval_decimal(raw: Mapping[str, Any], key: str, default: str) -> Decimal:
    """Validate an efficiency threshold against its mathematical 0..1 domain."""
    value = _decimal(raw, key, default)
    return value if D(0) <= value <= D(1) else D(default)


def _positive_int(raw: Mapping[str, Any], key: str, default: int) -> int:
    try:
        value = int(raw.get(key, default))
    except (ValueError, TypeError, OverflowError):
        value = default
    return value if value >= 1 else default


def _nonnegative_int(raw: Mapping[str, Any], key: str, default: int) -> int:
    try:
        value = int(raw.get(key, default))
    except (ValueError, TypeError, OverflowError):
        value = default
    return value if value >= 0 else default


def _bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    api_secret: str = ""
    futures_api_key: str = ""
    futures_api_secret: str = ""
    futures_readonly_enabled: bool = False
    futures_max_market_candidates: int = 5
    futures_strategy_fast_bps: Decimal = D("120")
    futures_strategy_slow_bps: Decimal = D("180")
    real_orders_enabled: bool = False
    cycle_interval_seconds: int = 300
    max_market_candidates: int = 12
    market_max_age_seconds: int = 120
    expected_holding_hours: int = 8
    strategy_fast_bps: Decimal = D("120")
    strategy_slow_bps: Decimal = D("180")
    strategy_fast_efficiency_min: Decimal = D("0.20")
    strategy_slow_efficiency_min: Decimal = D("0.12")
    strategy_stop_min_pct: Decimal = D("1.5")
    strategy_stop_max_pct: Decimal = D("5.0")
    trailing_stop_offset_pct: Decimal = D("3.0")
    risk_per_trade_pct: Decimal = D("0.5")
    risk_max_position_pct: Decimal = D("20")
    risk_max_gross_pct: Decimal = D("80")
    risk_max_net_pct: Decimal = D("50")
    risk_max_margin_pct: Decimal = D("25")
    risk_max_leverage: int = 3
    risk_max_open_positions: int = 4
    risk_daily_loss_pct: Decimal = D("3")
    risk_max_drawdown_pct: Decimal = D("8")
    risk_min_margin_level_pct: Decimal = D("300")
    risk_cash_reserve_pct: Decimal = D("20")
    tax_report_enabled: bool = True

    @property
    def credentials_present(self) -> bool:
        return bool(self.api_key.strip() and self.api_secret.strip())

    @property
    def live_requested(self) -> bool:
        """Compatibility alias; there is no separate PAPER/SHADOW/LIVE mode."""
        return self.real_orders_enabled

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Settings:
        # Legacy options (mode/live_enabled/kill_switch) are intentionally ignored.
        # Missing real_orders_enabled always means simulation-only operation.
        # Risk budgets and exposure ceilings belong to the operator. They are
        # validated for finiteness/domain only; exchange instrument limits and
        # available collateral remain independent constraints.
        risk_per_trade_pct = _nonnegative_decimal(raw, "risk_per_trade_pct", "0.5")
        risk_max_position_pct = _nonnegative_decimal(raw, "risk_max_position_pct", "20")
        risk_max_gross_pct = _nonnegative_decimal(raw, "risk_max_gross_pct", "80")
        risk_max_net_pct = _nonnegative_decimal(raw, "risk_max_net_pct", "50")
        risk_max_margin_pct = _nonnegative_decimal(raw, "risk_max_margin_pct", "25")
        reserve = _reserve_percentage(raw, "risk_cash_reserve_pct", "20")
        strategy_stop_min = _positive_decimal(raw, "strategy_stop_min_pct", "1.5")
        strategy_stop_max = _positive_decimal(raw, "strategy_stop_max_pct", "5.0")
        if strategy_stop_max < strategy_stop_min:
            strategy_stop_min, strategy_stop_max = D("1.5"), D("5.0")
        return cls(
            api_key=str(raw.get("kraken_api_key", "") or ""),
            api_secret=str(raw.get("kraken_api_secret", "") or ""),
            futures_api_key=str(raw.get("kraken_futures_api_key", "") or ""),
            futures_api_secret=str(raw.get("kraken_futures_api_secret", "") or ""),
            futures_readonly_enabled=_bool(raw, "futures_readonly_enabled", False),
            futures_max_market_candidates=_positive_int(raw, "futures_max_market_candidates", 5),
            futures_strategy_fast_bps=_positive_decimal(
                raw, "futures_strategy_fast_bps", "120"
            ),
            futures_strategy_slow_bps=_positive_decimal(
                raw, "futures_strategy_slow_bps", "180"
            ),
            real_orders_enabled=_bool(raw, "real_orders_enabled", False),
            # Keep only a minimum cycle interval to protect Kraken's public API;
            # there are no code-owned upper bounds on polling, candidates or quote age.
            cycle_interval_seconds=max(60, _positive_int(raw, "cycle_interval_seconds", 300)),
            max_market_candidates=_positive_int(raw, "max_market_candidates", 12),
            market_max_age_seconds=max(15, _positive_int(raw, "market_max_age_seconds", 120)),
            expected_holding_hours=_positive_int(raw, "expected_holding_hours", 8),
            strategy_fast_bps=_positive_decimal(raw, "strategy_fast_bps", "120"),
            strategy_slow_bps=_positive_decimal(raw, "strategy_slow_bps", "180"),
            strategy_fast_efficiency_min=_unit_interval_decimal(
                raw, "strategy_fast_efficiency_min", "0.20"
            ),
            strategy_slow_efficiency_min=_unit_interval_decimal(
                raw, "strategy_slow_efficiency_min", "0.12"
            ),
            strategy_stop_min_pct=strategy_stop_min,
            strategy_stop_max_pct=strategy_stop_max,
            trailing_stop_offset_pct=_positive_decimal(raw, "trailing_stop_offset_pct", "3.0"),
            risk_per_trade_pct=risk_per_trade_pct,
            risk_max_position_pct=risk_max_position_pct,
            risk_max_gross_pct=risk_max_gross_pct,
            risk_max_net_pct=risk_max_net_pct,
            risk_max_margin_pct=risk_max_margin_pct,
            risk_max_leverage=_positive_int(raw, "risk_max_leverage", 3),
            risk_max_open_positions=_nonnegative_int(raw, "risk_max_open_positions", 4),
            risk_cash_reserve_pct=reserve,
            risk_daily_loss_pct=_positive_decimal(raw, "risk_daily_loss_pct", "3"),
            risk_max_drawdown_pct=_positive_decimal(raw, "risk_max_drawdown_pct", "8"),
            risk_min_margin_level_pct=_nonnegative_decimal(
                raw, "risk_min_margin_level_pct", "300"
            ),
            tax_report_enabled=_bool(raw, "tax_report_enabled", True),
        )

    @classmethod
    def from_file(cls, path: str = "/data/options.json") -> Settings:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls.from_mapping(value) if isinstance(value, dict) else cls()

    def safe_summary(self) -> dict[str, Any]:
        return {
            "real_orders_enabled": self.real_orders_enabled,
            "real_orders_requested": self.real_orders_enabled,
            "internal_live_release": INTERNAL_LIVE_EXECUTION_RELEASED,
            "background_research_enabled": BACKGROUND_RESEARCH_ENABLED,
            "credentials_present": self.credentials_present,
            "futures_readonly_enabled": self.futures_readonly_enabled,
            "futures_max_market_candidates": self.futures_max_market_candidates,
            "futures_strategy_fast_bps": str(self.futures_strategy_fast_bps),
            "futures_strategy_slow_bps": str(self.futures_strategy_slow_bps),
            "futures_credentials_present": bool(
                self.futures_api_key.strip() and self.futures_api_secret.strip()
            ),
            "cycle_interval_seconds": self.cycle_interval_seconds,
            "max_market_candidates": self.max_market_candidates,
            "market_max_age_seconds": self.market_max_age_seconds,
            "expected_holding_hours": self.expected_holding_hours,
            "strategy_fast_bps": str(self.strategy_fast_bps),
            "strategy_slow_bps": str(self.strategy_slow_bps),
            "strategy_fast_efficiency_min": str(self.strategy_fast_efficiency_min),
            "strategy_slow_efficiency_min": str(self.strategy_slow_efficiency_min),
            "strategy_stop_min_pct": str(self.strategy_stop_min_pct),
            "strategy_stop_max_pct": str(self.strategy_stop_max_pct),
            "trailing_stop_offset_pct": str(self.trailing_stop_offset_pct),
            "risk_per_trade_pct": str(self.risk_per_trade_pct),
            "risk_max_position_pct": str(self.risk_max_position_pct),
            "risk_max_gross_pct": str(self.risk_max_gross_pct),
            "risk_max_net_pct": str(self.risk_max_net_pct),
            "risk_max_margin_pct": str(self.risk_max_margin_pct),
            "risk_max_leverage": self.risk_max_leverage,
            "risk_max_open_positions": self.risk_max_open_positions,
            "risk_daily_loss_pct": str(self.risk_daily_loss_pct),
            "risk_max_drawdown_pct": str(self.risk_max_drawdown_pct),
            "risk_min_margin_level_pct": str(self.risk_min_margin_level_pct),
            "risk_cash_reserve_pct": str(self.risk_cash_reserve_pct),
        }
