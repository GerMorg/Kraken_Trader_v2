from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Iterable

from app.models import Instrument, Ticker

D = Decimal

def _d(value: Any, default: str = "0") -> Decimal:
    try:
        result = D(str(value))
        return result if result.is_finite() else D(default)
    except (InvalidOperation, ValueError, TypeError):
        return D(default)

def _leverage(raw: Any) -> tuple[int, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    values = set()
    for value in raw:
        try:
            level = int(str(value))
        except (ValueError, TypeError):
            continue
        if 2 <= level <= 100:
            values.add(level)
    return tuple(sorted(values))

def usd_crypto_universe(raw_pairs: dict[str, Any]) -> list[Instrument]:
    instruments = []
    for pair_id, raw in raw_pairs.items():
        if not isinstance(raw, dict) or str(raw.get("status","")).lower() != "online":
            continue
        asset_class = str(raw.get("aclass_base", raw.get("asset_class", ""))).strip().lower()
        # Only accept the exchange's explicit regular-currency base class. Unknown classes
        # fail closed so tokenized equities or newly introduced products cannot leak in.
        if asset_class != "currency":
            continue
        wsname = str(raw.get("wsname",""))
        if "/" not in wsname:
            continue
        base, quote = wsname.rsplit("/", 1)
        if quote.upper() != "USD" or not base or base.upper() in {"USD","USDC","USDT","EURC"}:
            continue
        try:
            minimum = _d(raw.get("ordermin","0"))
            lot_decimals, pair_decimals = int(raw.get("lot_decimals",8)), int(raw.get("pair_decimals",8))
        except (ValueError, TypeError):
            continue
        if minimum <= 0 or lot_decimals < 0 or pair_decimals < 0:
            continue
        instruments.append(Instrument(
            str(pair_id), str(raw.get("altname",pair_id)), wsname, base, "USD", "online",
            minimum, lot_decimals, pair_decimals, _leverage(raw.get("leverage_buy",[])),
            _leverage(raw.get("leverage_sell",[]))))
    return sorted(instruments, key=lambda item:item.wsname)

def parse_tickers(raw: dict[str, Any], instruments: Iterable[Instrument]) -> dict[str, Ticker]:
    result = {}
    aliases = {}
    for item in instruments:
        aliases[item.altname] = item.pair_id
        aliases[item.wsname] = item.pair_id
        aliases[item.pair_id] = item.pair_id
    for key, row in raw.items():
        if not isinstance(row, dict) or key not in aliases:
            continue
        last, ask, bid = _d(row.get("c",["0"])[0]), _d(row.get("a",["0"])[0]), _d(row.get("b",["0"])[0])
        volume, vwap = row.get("v",["0","0"]), row.get("p",["0","0"])
        base_vol = _d(volume[1] if len(volume)>1 else volume[0])
        mean_price = _d(vwap[1] if len(vwap)>1 else vwap[0])
        ticker = Ticker(aliases[key], last, bid, ask, base_vol*mean_price)
        if ticker.last > 0 and ticker.bid > 0 and ticker.ask >= ticker.bid:
            result[ticker.pair_id] = ticker
    return result

def parse_ohlc_rows(raw_result: dict[str, Any], pair_id: str) -> list[dict[str, Any]]:
    rows = raw_result.get(pair_id)
    if rows is None:
        candidates = [value for key,value in raw_result.items() if key != "last"]
        rows = candidates[0] if candidates else []
    if not isinstance(rows, list):
        return []
    parsed = []
    for row in rows[:-1]:  # current candle is still forming and cannot be treated as closed
        if not isinstance(row,(list,tuple)) or len(row)<7:
            continue
        try:
            ts = int(row[0])
            op,high,low,close,volume = (_d(row[i]) for i in (1,2,3,4,6))
        except (ValueError,TypeError):
            continue
        if min(op,high,low,close)<=0 or high<low or volume<0:
            continue
        parsed.append({"ts":ts,"open":op,"high":high,"low":low,"close":close,"volume":volume})
    return parsed

def volume_for_notional_usd(notional_usd: Decimal, last_price: Decimal, lot_decimals: int) -> Decimal:
    """Round a USD-notional-derived quantity down so rounding cannot exceed risk sizing."""
    if (
        not notional_usd.is_finite()
        or not last_price.is_finite()
        or notional_usd > 0
        or last_price <= 0
        or lot_decimals < 0
    ):
        return D(0)
    try:
        quantity = notional_usd / last_price
        quantum = D(1).scaleb(-lot_decimals)
        return quantity.quantize(quantum, rounding=ROUND_DOWN)
    except (ArithmeticError, InvalidOperation, ValueError):
        return D(0)


def margin_leverage_for(instrument: Instrument, order_side: str, hard_cap: int = 3) -> int | None:
    supported = [v for v in instrument.leverages_for_order_side(order_side) if 2<=v<=hard_cap]
    return min(supported) if supported else None
