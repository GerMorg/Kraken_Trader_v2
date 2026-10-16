from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Mapping

from app.models import Instrument, Ticker
from app.strategy import take_profit_quantity

D = Decimal


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = D(str(value))
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _decision(
    position_txid: str,
    pair: str | None,
    status: str,
    reason: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "position_txid": position_txid,
        "pair": pair,
        "status": status,
        "reason": reason,
        "action": None,
        "recommended_volume": None,
        "metric": "GROSS_UNDERLYING_PRICE_MOVE_PCT",
        "gross_price_move_only": True,
        "fees_and_financing_included": False,
        "execution_mode": "SHADOW_ONLY",
        "exchange_native_protection": False,
        **details,
    }


def evaluate_position_exits(
    raw_positions: Mapping[str, Any],
    tickers: Mapping[str, Ticker],
    instruments: Mapping[str, Instrument],
    *,
    take_profit_pct: Decimal,
    take_profit_fraction: Decimal,
    stop_loss_pct: Decimal,
) -> list[dict[str, Any]]:
    """Create shadow-only reduce/close plans for each individual Kraken margin lot.

    This is a risk-management hypothesis using the current executable side of the
    ticker, not a net-PnL calculation. It deliberately cannot send orders.
    """
    thresholds = (take_profit_pct, take_profit_fraction, stop_loss_pct)
    if any(not value.is_finite() for value in thresholds):
        raise ValueError("POSITION_EXIT_POLICY_VALUES_MUST_BE_FINITE")
    if take_profit_pct <= 0 or stop_loss_pct <= 0 or not D(0) < take_profit_fraction < D(1):
        raise ValueError("POSITION_EXIT_POLICY_VALUES_OUT_OF_RANGE")

    source: Any = raw_positions.get("open", raw_positions)
    if not isinstance(source, dict):
        return [_decision("", None, "BLOCKED", "POSITION_RESPONSE_SHAPE_INVALID")]

    recommendations: list[dict[str, Any]] = []
    for position_txid, row in source.items():
        txid = str(position_txid).strip()
        if not txid or not isinstance(row, dict):
            recommendations.append(_decision(
                txid, None, "BLOCKED", "POSITION_ROW_INVALID",
            ))
            continue

        pair = str(row.get("pair", "")).strip()
        side = str(row.get("type", "")).strip().lower()
        if not pair or side not in {"buy", "sell"} or str(row.get("posstatus", "")).lower() != "open":
            recommendations.append(_decision(
                txid, pair or None, "BLOCKED", "POSITION_IDENTITY_OR_STATUS_INVALID",
            ))
            continue

        instrument = instruments.get(pair)
        if instrument is None:
            normalized_pair = pair.casefold()
            instrument = next(
                (
                    candidate for candidate in instruments.values()
                    if normalized_pair in {
                        str(candidate.pair_id).casefold(),
                        str(candidate.altname).casefold(),
                        str(candidate.wsname).casefold(),
                    }
                ),
                None,
            )
        ticker = tickers.get(pair)
        if ticker is None and instrument is not None:
            ticker = tickers.get(instrument.pair_id)
        if ticker is None and instrument is not None:
            ticker = next(
                (
                    candidate for candidate in tickers.values()
                    if str(candidate.pair_id).casefold() == str(instrument.pair_id).casefold()
                ),
                None,
            )
        if instrument is None or instrument.quote.upper() != "USD":
            recommendations.append(_decision(
                txid, pair, "BLOCKED", "EXISTING_POSITION_OUTSIDE_SUPPORTED_USD_UNIVERSE",
            ))
            continue
        if ticker is None:
            recommendations.append(_decision(
                txid, pair, "BLOCKED", "CURRENT_POSITION_TICKER_MISSING",
            ))
            continue

        volume = _decimal(row.get("vol"))
        closed = _decimal(row.get("vol_closed"))
        cost = _decimal(row.get("cost"))
        exit_price = ticker.bid if side == "buy" else ticker.ask
        if (
            volume is None or closed is None or cost is None
            or not exit_price.is_finite() or exit_price <= 0
            or volume <= 0 or closed < 0 or closed > volume or cost <= 0
        ):
            recommendations.append(_decision(
                txid, pair, "BLOCKED", "POSITION_QUANTITY_OR_PRICE_INVALID",
            ))
            continue

        remaining = volume - closed
        if remaining <= 0:
            recommendations.append(_decision(
                txid, pair, "BLOCKED", "OPEN_POSITION_HAS_NO_REMAINING_VOLUME",
                volume=str(volume), volume_closed=str(closed),
            ))
            continue

        entry_price = cost / volume
        if not entry_price.is_finite() or entry_price <= 0:
            recommendations.append(_decision(
                txid, pair, "BLOCKED", "POSITION_ENTRY_PRICE_UNVERIFIABLE",
            ))
            continue

        # Kraken uses buy/sell to describe the entry side of a margin position.
        # Use bid for a long exit and ask for a short exit to include the spread side.
        price_move_pct = (
            (exit_price - entry_price) / entry_price * D(100)
            if side == "buy"
            else (entry_price - exit_price) / entry_price * D(100)
        )
        direction = "LONG" if side == "buy" else "SHORT"
        if price_move_pct <= -stop_loss_pct:
            action = "CLOSE_LONG" if side == "buy" else "CLOSE_SHORT"
            requested = remaining
            reason = "HARD_STOP_PRICE_MOVE"
            status = "RISK_REDUCTION_RECOMMENDED"
        elif price_move_pct >= take_profit_pct:
            action = "REDUCE_LONG" if side == "buy" else "REDUCE_SHORT"
            try:
                requested = take_profit_quantity(remaining, take_profit_fraction)
            except ValueError:
                recommendations.append(_decision(
                    txid, pair, "BLOCKED", "PARTIAL_EXIT_QUANTITY_INVALID",
                    direction=direction, price_move_pct=str(price_move_pct),
                ))
                continue
            reason = "PARTIAL_TAKE_PROFIT_PRICE_MOVE"
            status = "PARTIAL_TAKE_PROFIT_RECOMMENDED"
        else:
            recommendations.append(_decision(
                txid, pair, "NO_ACTION", "EXIT_TRIGGER_NOT_REACHED",
                direction=direction, entry_price=str(entry_price),
                exit_price=str(exit_price), price_move_pct=str(price_move_pct),
                remaining_volume=str(remaining),
                take_profit_trigger_pct=str(take_profit_pct),
                stop_loss_trigger_pct=str(stop_loss_pct),
            ))
            continue

        quantum = D(1).scaleb(-instrument.lot_decimals)
        try:
            quantity = requested.quantize(quantum, rounding=ROUND_DOWN)
        except (InvalidOperation, ValueError):
            quantity = D(0)
        if quantity <= 0 or quantity < instrument.order_min:
            recommendations.append(_decision(
                txid, pair, "BLOCKED_BELOW_MINIMUM", "EXIT_QUANTITY_BELOW_EXCHANGE_MINIMUM",
                direction=direction, entry_price=str(entry_price),
                exit_price=str(exit_price), price_move_pct=str(price_move_pct),
                remaining_volume=str(remaining), desired_volume=str(requested),
                exchange_minimum=str(instrument.order_min),
                take_profit_trigger_pct=str(take_profit_pct),
                stop_loss_trigger_pct=str(stop_loss_pct),
            ))
            continue

        recommendations.append(_decision(
            txid, pair, status, reason,
            action=action, recommended_volume=str(quantity),
            direction=direction, entry_price=str(entry_price),
            exit_price=str(exit_price), price_move_pct=str(price_move_pct),
            original_volume=str(volume), volume_closed=str(closed),
            remaining_volume=str(remaining),
            take_profit_trigger_pct=str(take_profit_pct),
            take_profit_fraction=str(take_profit_fraction),
            stop_loss_trigger_pct=str(stop_loss_pct),
            order_minimum=str(instrument.order_min),
            lot_decimals=instrument.lot_decimals,
        ))
    return recommendations
