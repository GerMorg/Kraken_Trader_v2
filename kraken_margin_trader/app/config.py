from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Mapping

D = Decimal
HARD_LIMITS = {
    "risk_per_trade_pct": D("0.5"), "risk_max_position_pct": D("20"),
    "risk_max_gross_pct": D("80"), "risk_max_net_pct": D("50"),
    "risk_max_margin_pct": D("25"), "risk_max_leverage": D("3"),
    "risk_max_open_positions": 4, "risk_daily_loss_pct": D("3"),
    "risk_max_drawdown_pct": D("8"), "risk_cash_reserve_pct": D("20"),
}

def _decimal(raw: Mapping[str, Any], key: str, default: str) -> Decimal:
    try:
        value = D(str(raw.get(key, default)))
        return value if value.is_finite() else D(default)
    except (InvalidOperation, ValueError, TypeError):
        return D(default)

def _int(raw: Mapping[str, Any], key: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(raw.get(key, default))
    except (ValueError, TypeError, OverflowError):
        value = default
    return max(lower, min(upper, value))

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
    mode: str = "SHADOW"
    live_enabled: bool = False
    kill_switch: bool = True
    validate_shadow_orders: bool = False
    cycle_interval_seconds: int = 300
    max_market_candidates: int = 12
    market_max_age_seconds: int = 120
    expected_holding_hours: int = 8
    strategy_fast_bps: Decimal = D("120")
    strategy_slow_bps: Decimal = D("180")
    risk_per_trade_pct: Decimal = D("0.5")
    risk_max_position_pct: Decimal = D("20")
    risk_max_gross_pct: Decimal = D("80")
    risk_max_net_pct: Decimal = D("50")
    risk_max_margin_pct: Decimal = D("25")
    risk_max_leverage: int = 3
    risk_max_open_positions: int = 4
    risk_daily_loss_pct: Decimal = D("3")
    risk_max_drawdown_pct: Decimal = D("8")
    risk_cash_reserve_pct: Decimal = D("20")
    tax_report_enabled: bool = True

    @property
    def credentials_present(self) -> bool:
        return bool(self.api_key.strip() and self.api_secret.strip())

    @property
    def live_requested(self) -> bool:
        return self.mode == "LIVE" and self.live_enabled and not self.kill_switch

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Settings:
        mode = str(raw.get("mode", "SHADOW")).strip().upper()
        if mode not in {"SHADOW", "PAPER", "LIVE"}:
            mode = "SHADOW"
        decimals: dict[str, Decimal] = {}
        for key, default in (
            ("risk_per_trade_pct", "0.5"), ("risk_max_position_pct", "20"),
            ("risk_max_gross_pct", "80"), ("risk_max_net_pct", "50"),
            ("risk_max_margin_pct", "25"), ("risk_daily_loss_pct", "3"),
            ("risk_max_drawdown_pct", "8"),
        ):
            decimals[key] = max(D("0"), min(_decimal(raw, key, default), D(str(HARD_LIMITS[key]))))
        reserve = max(D("20"), min(D("80"), _decimal(raw, "risk_cash_reserve_pct", "20")))
        return cls(
            api_key=str(raw.get("kraken_api_key", "") or ""),
            api_secret=str(raw.get("kraken_api_secret", "") or ""),
            mode=mode, live_enabled=_bool(raw, "live_enabled", False),
            kill_switch=_bool(raw, "kill_switch", True),
            validate_shadow_orders=_bool(raw, "validate_shadow_orders", False),
            cycle_interval_seconds=_int(raw, "cycle_interval_seconds", 300, 60, 3600),
            max_market_candidates=_int(raw, "max_market_candidates", 12, 1, 30),
            market_max_age_seconds=_int(raw, "market_max_age_seconds", 120, 15, 600),
            expected_holding_hours=_int(raw, "expected_holding_hours", 8, 1, 168),
            strategy_fast_bps=max(D("25"), min(D("1000"), _decimal(raw, "strategy_fast_bps", "120"))),
            strategy_slow_bps=max(D("50"), min(D("2000"), _decimal(raw, "strategy_slow_bps", "180"))),
            risk_per_trade_pct=decimals["risk_per_trade_pct"],
            risk_max_position_pct=decimals["risk_max_position_pct"],
            risk_max_gross_pct=decimals["risk_max_gross_pct"],
            risk_max_net_pct=decimals["risk_max_net_pct"],
            risk_max_margin_pct=decimals["risk_max_margin_pct"],
            risk_max_leverage=_int(raw, "risk_max_leverage", 3, 1, 3),
            risk_max_open_positions=_int(raw, "risk_max_open_positions", 4, 1, 4),
            risk_daily_loss_pct=decimals["risk_daily_loss_pct"],
            risk_max_drawdown_pct=decimals["risk_max_drawdown_pct"],
            risk_cash_reserve_pct=reserve,
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
            "mode": self.mode, "live_enabled": self.live_enabled, "kill_switch": self.kill_switch,
            "live_requested": self.live_requested, "internal_live_release": False,
            "credentials_present": self.credentials_present, "cycle_interval_seconds": self.cycle_interval_seconds,
            "risk_per_trade_pct": str(self.risk_per_trade_pct),
            "risk_max_position_pct": str(self.risk_max_position_pct),
            "risk_max_gross_pct": str(self.risk_max_gross_pct),
            "risk_max_net_pct": str(self.risk_max_net_pct),
            "risk_max_margin_pct": str(self.risk_max_margin_pct),
            "risk_max_leverage": self.risk_max_leverage, "risk_max_open_positions": self.risk_max_open_positions,
            "risk_daily_loss_pct": str(self.risk_daily_loss_pct),
            "risk_max_drawdown_pct": str(self.risk_max_drawdown_pct),
            "risk_cash_reserve_pct": str(self.risk_cash_reserve_pct),
        }
