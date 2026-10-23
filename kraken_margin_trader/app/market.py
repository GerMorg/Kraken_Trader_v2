from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Iterable

from app.models import Instrument, Ticker

D = Decimal


FIAT_ASSET_CODES = frozenset({
    # ISO 4217 fiat currencies that can appear as an AssetPairs base code.
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN",
    "BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BRL",
    "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF", "CLP", "CNY",
    "COP", "CRC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP",
    "ERN", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD",
    "GNF", "GTQ", "GYD", "HKD", "HNL", "HTG", "HUF", "IDR", "ILS", "INR",
    "IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR", "KMF",
    "KPW", "KRW", "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL",
    "LYD", "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU", "MUR",
    "MVR", "MWK", "MXN", "MYR", "MZN", "NAD", "NGN", "NIO", "NOK", "NPR",
    "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG", "QAR",
    "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD",
    "SHP", "SLE", "SOS", "SRD", "SSP", "STN", "SVC", "SYP", "SZL", "THB",
    "TJS", "TMT", "TND", "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX",
    "USD", "UYU", "UZS", "VES", "VND", "VUV", "WST", "XAF", "XCD", "XOF",
    "XPF", "YER", "ZAR", "ZMW", "ZWG",
    # ISO special-purpose monetary units and precious metals are not crypto assets.
    "BOV", "CHE", "CHW", "KID", "USN", "XXX", "XBA", "XBB", "XBC", "XBD",
    "XTS", "CLF", "COU", "XAG", "XAU", "XDR", "XPD", "XPT", "XSU", "XUA", "UYI",
})


def _is_fiat_base(asset_symbol: Any, asset_id: Any = None) -> bool:
    """Reject fiat/special-purpose monetary assets while allowing dynamic crypto bases."""
    for raw in (asset_symbol, asset_id):
        token = str(raw or "").strip().upper()
        if not token:
            continue
        if token in FIAT_ASSET_CODES:
            return True
        # Kraken's legacy IDs commonly prefix a fiat currency with X or Z
        # (for example ZEUR / ZGBP); canonical crypto IDs such as XXBT remain allowed.
        if len(token) == 4 and token[0] in {"X", "Z"} and token[1:] in FIAT_ASSET_CODES:
            return True
    return False


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        result = D(str(value))
        return result if result.is_finite() else D(default)
    except (InvalidOperation, ValueError, TypeError):
        return D(default)

def _array_item(row: dict[str, Any], key: str, index: int = 0) -> Any:
    values = row.get(key)
    if not isinstance(values, (list, tuple)) or not values:
        return "0"
    return values[index] if index < len(values) else values[0]


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
        base_asset_id = raw.get("base")
        if (
            quote.upper() != "USD"
            or not base
            or base.upper() in {"USD", "USDC", "USDT", "EURC"}
            or _is_fiat_base(base, base_asset_id)
        ):
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
        last = _d(_array_item(row, "c"))
        ask = _d(_array_item(row, "a"))
        bid = _d(_array_item(row, "b"))
        base_vol = _d(_array_item(row, "v", 1))
        mean_price = _d(_array_item(row, "p", 1))
        ticker = Ticker(aliases[key], last, bid, ask, base_vol*mean_price)
        if (
            ticker.last > 0
            and ticker.bid > 0
            and ticker.ask >= ticker.bid
            and base_vol > 0
            and mean_price > 0
            and ticker.volume_quote_24h.is_finite()
            and ticker.volume_quote_24h > 0
        ):
            result[ticker.pair_id] = ticker
    return result

def quote_is_fresh(observed_at: float | Decimal | None, now: float | Decimal,
                   max_age_seconds: int) -> bool:
    """Check quote age without treating missing, future or non-finite times as fresh."""
    try:
        captured = D(str(observed_at))
        current = D(str(now))
        maximum_age = D(str(max_age_seconds))
    except (ArithmeticError, TypeError, ValueError):
        return False
    if (
        not captured.is_finite() or not current.is_finite()
        or not maximum_age.is_finite() or maximum_age <= 0
    ):
        return False
    age = current - captured
    return age.is_finite() and D(0) <= age <= maximum_age


def closed_bar_is_fresh(
    bar_open_epoch: int | float | Decimal | None,
    now_epoch: int | float | Decimal,
    *,
    interval_minutes: int = 60,
    max_age_seconds: int = 3900,
    future_tolerance_seconds: int = 60,
) -> bool:
    """Reject missing, stale or implausibly future closed-candle timestamps."""
    try:
        opened = D(str(bar_open_epoch))
        current = D(str(now_epoch))
        duration = D(interval_minutes) * D(60)
        maximum_age = D(max_age_seconds)
        future_tolerance = D(future_tolerance_seconds)
    except (ArithmeticError, TypeError, ValueError):
        return False
    if (
        not all(value.is_finite() for value in (
            opened, current, duration, maximum_age, future_tolerance
        ))
        or opened < 0
        or duration <= 0
        or maximum_age < 0
        or future_tolerance < 0
    ):
        return False
    age = current - (opened + duration)
    return age.is_finite() and -future_tolerance <= age <= maximum_age


def executable_reference_price(ticker: Ticker, direction: str) -> Decimal:
    """Return the executable quote side used for conservative entry sizing.

    Long entries pay the ask; short entries sell at the bid. The last-traded price
    is not a guaranteed execution price and can understate requested order notional.
    """
    normalized = direction.upper()
    if normalized == "LONG":
        price = ticker.ask
    elif normalized == "SHORT":
        price = ticker.bid
    else:
        return D(0)

    if (
        not ticker.bid.is_finite()
        or not ticker.ask.is_finite()
        or ticker.bid <= 0
        or ticker.ask < ticker.bid
        or not price.is_finite()
        or price <= 0
    ):
        return D(0)
    return price


def parse_ohlc_rows(raw_result: dict[str, Any], pair_id: str) -> list[dict[str, Any]]:
    rows = raw_result.get(pair_id)
    if rows is None:
        # Kraken may return an alias rather than pair_id for a single-pair query.
        # Accept that alias only if exactly one non-cursor result exists. Picking the
        # first of multiple rows can silently apply another market's prices to this pair.
        candidates = [value for key, value in raw_result.items() if key != "last"]
        if len(candidates) != 1:
            return []
        rows = candidates[0]
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
        if (
            min(op, high, low, close) <= 0
            or high < low
            or high < max(op, close)
            or low > min(op, close)
            or volume < 0
        ):
            continue
        parsed.append({"ts":ts,"open":op,"high":high,"low":low,"close":close,"volume":volume})
    return parsed

def volume_for_notional_usd(notional_usd: Decimal, last_price: Decimal, lot_decimals: int) -> Decimal:
    """Round a USD-notional-derived quantity down so rounding cannot exceed risk sizing."""
    if (
        not notional_usd.is_finite()
        or not last_price.is_finite()
        or notional_usd <= 0
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
