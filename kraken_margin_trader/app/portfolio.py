from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

D = Decimal


@dataclass(frozen=True)
class PositionExposure:
    gross_usd: Decimal
    net_usd: Decimal
    complete: bool


def aggregate_position_exposure(rows: dict[str, dict[str, Any]]) -> PositionExposure:
    """Aggregate exchange-reported position values without trusting their sign convention."""
    gross = D(0)
    net = D(0)
    for row in rows.values():
        side = str(row.get("type", "")).strip().lower()
        try:
            raw_value = D(str(row["value"]))
        except (KeyError, ArithmeticError, TypeError, ValueError):
            return PositionExposure(gross, net, False)
        if not raw_value.is_finite() or side not in {"buy", "sell"}:
            return PositionExposure(gross, net, False)
        # Exposure magnitude is absolute; direction is supplied by the position side.
        gross += abs(raw_value)
        net += abs(raw_value) * (D(1) if side == "buy" else D(-1))
    return PositionExposure(gross, net, True)


@dataclass(frozen=True)
class MarginLot:
    txid: str
    pair: str
    side: str
    original_volume: Decimal
    closed_volume: Decimal
    remaining_volume: Decimal
    opened_at: Decimal
    cost: Decimal
    fee: Decimal
    margin_field: Decimal
    rollover_time: Decimal | None
    terms: str
    raw: dict[str,Any]

def parse_open_margin_positions(raw: dict[str,Any]) -> list[MarginLot]:
    positions=[]
    for txid,row in raw.items():
        if not isinstance(row,dict) or str(row.get("posstatus","")).lower()!="open":
            continue
        try:
            volume,closed,cost,fee,margin,opened=(D(str(row.get(k,"0"))) for k in ("vol","vol_closed","cost","fee","margin","time"))
            roll_raw=row.get("rollovertm")
            rollover=D(str(roll_raw)) if roll_raw not in (None,"") else None
        except (ArithmeticError,TypeError,ValueError):
            continue
        numeric_values = (volume, closed, cost, fee, margin, opened)
        if any(not value.is_finite() for value in numeric_values):
            continue
        if rollover is not None and (not rollover.is_finite() or rollover < 0):
            continue
        remaining = volume - closed
        side = str(row.get("type", "")).lower()
        pair = str(row.get("pair", "")).strip()
        if (
            not pair
            or side not in {"buy", "sell"}
            or volume <= 0
            or closed < 0
            or remaining <= 0
            or cost < 0
            or fee < 0
            or margin < 0
            or opened < 0
        ):
            continue
        positions.append(MarginLot(str(txid), pair,
          "LONG" if side == "buy" else "SHORT",
          volume,closed,remaining,opened,cost,fee,margin,rollover,str(row.get("terms","")),row))
    return sorted(positions,key=lambda lot:(lot.opened_at,lot.txid))

def resolve_close_quantity(*,requested: Decimal,tracked_pair_quantity: Decimal,
                           free_wallet_quantity: Decimal,product_type: str) -> Decimal:
    quantities = (requested, tracked_pair_quantity, free_wallet_quantity)
    if any(not value.is_finite() for value in quantities):
        raise ValueError("INVALID_OR_UNPROVEN_POSITION_QUANTITY")
    if requested<=0 or tracked_pair_quantity<=0 or free_wallet_quantity<0:
        raise ValueError("INVALID_OR_UNPROVEN_POSITION_QUANTITY")
    maximum=tracked_pair_quantity
    if product_type.upper()=="CASH_SPOT":
        maximum=min(maximum,free_wallet_quantity)
    elif product_type.upper() not in {"SPOT_MARGIN","MARGIN_SETTLEMENT"}:
        raise ValueError("UNKNOWN_PRODUCT_TYPE")
    if requested>maximum:
        raise ValueError("REQUEST_EXCEEDS_VERIFIED_CLOSEABLE_QUANTITY")
    return requested

def expected_eea_funding_currency(quote_currency: str) -> str | None:
    # Expected EEA policy only; actual loan amount/currency must come from position/ledger reconciliation.
    if quote_currency.upper()=="USD": return "USDC"
    if quote_currency.upper()=="EUR": return "EURC"
    return None
