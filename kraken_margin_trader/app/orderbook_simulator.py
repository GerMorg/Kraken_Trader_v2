from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

D = Decimal
OrderSide = Literal["BUY", "SELL"]
ExecutionStatus = Literal["FILLED", "PARTIALLY_FILLED", "UNFILLED"]


@dataclass(frozen=True)
class BookLevel:
    """One displayed order-book level in base quantity and quote price."""

    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if (
            not self.price.is_finite()
            or not self.quantity.is_finite()
            or self.price <= 0
            or self.quantity <= 0
        ):
            raise ValueError("ORDER_BOOK_LEVEL_MUST_BE_POSITIVE_AND_FINITE")


@dataclass(frozen=True)
class OrderBookSnapshot:
    """Immutable, time-stamped L2 snapshot; levels must be best-price first."""

    timestamp: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    def __post_init__(self) -> None:
        if self.timestamp <= 0:
            raise ValueError("ORDER_BOOK_TIMESTAMP_INVALID")
        if not self.bids or not self.asks:
            raise ValueError("ORDER_BOOK_SIDE_EMPTY")
        if any(
            self.bids[index].price <= self.bids[index + 1].price
            for index in range(len(self.bids) - 1)
        ):
            raise ValueError("BID_LEVELS_NOT_STRICTLY_DESCENDING")
        if any(
            self.asks[index].price >= self.asks[index + 1].price
            for index in range(len(self.asks) - 1)
        ):
            raise ValueError("ASK_LEVELS_NOT_STRICTLY_ASCENDING")
        if self.bids[0].price >= self.asks[0].price:
            raise ValueError("ORDER_BOOK_CROSSED_OR_LOCKED")


@dataclass(frozen=True)
class ExecutionSlice:
    price: Decimal
    quantity: Decimal
    quote_amount: Decimal


@dataclass(frozen=True)
class OrderBookExecution:
    side: OrderSide
    status: ExecutionStatus
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    quote_amount: Decimal
    average_price: Decimal | None
    fee_bps: Decimal
    fee_quote: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal | None
    levels_consumed: int
    slices: tuple[ExecutionSlice, ...]
    snapshot_timestamp: int

    @property
    def complete(self) -> bool:
        return self.status == "FILLED"


def simulate_order_book_execution(
    book: OrderBookSnapshot,
    *,
    side: OrderSide,
    quantity: Decimal,
    fee_bps: Decimal,
    max_level_participation: Decimal = D("1"),
    limit_price: Decimal | None = None,
) -> OrderBookExecution:
    """Walk the displayed book conservatively and preserve any unfilled quantity.

    BUY consumes asks from lowest to highest; SELL consumes bids from highest
    to lowest. A supplied limit price caps execution at or better than that
    price. Participation is a per-visible-level cap, not a claim about hidden
    liquidity or queue priority. Fees are expressed in basis points of executed
    quote notional and are returned separately from slippage.
    """
    if side not in ("BUY", "SELL"):
        raise ValueError("ORDER_SIDE_INVALID")
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("ORDER_QUANTITY_MUST_BE_POSITIVE_AND_FINITE")
    if not fee_bps.is_finite() or fee_bps < 0:
        raise ValueError("ORDER_FEE_BPS_INVALID")
    if (
        not max_level_participation.is_finite()
        or max_level_participation <= 0
        or max_level_participation > 1
    ):
        raise ValueError("ORDER_BOOK_PARTICIPATION_OUT_OF_RANGE")
    if limit_price is not None and (not limit_price.is_finite() or limit_price <= 0):
        raise ValueError("ORDER_LIMIT_PRICE_INVALID")

    levels = book.asks if side == "BUY" else book.bids
    if side == "BUY" and limit_price is not None:
        if limit_price < book.asks[0].price:
            eligible_levels: tuple[BookLevel, ...] = ()
        else:
            eligible_levels = tuple(level for level in levels if level.price <= limit_price)
    elif side == "SELL" and limit_price is not None:
        if limit_price > book.bids[0].price:
            eligible_levels = ()
        else:
            eligible_levels = tuple(level for level in levels if level.price >= limit_price)
    else:
        eligible_levels = levels

    remaining = quantity
    slices: list[ExecutionSlice] = []
    for level in eligible_levels:
        if remaining <= 0:
            break
        available = level.quantity * max_level_participation
        filled_here = min(remaining, available)
        if filled_here <= 0:
            continue
        quote_amount = filled_here * level.price
        slices.append(ExecutionSlice(level.price, filled_here, quote_amount))
        remaining -= filled_here

    filled = quantity - remaining
    quote_total = sum((item.quote_amount for item in slices), D(0))
    average_price = quote_total / filled if filled > 0 else None
    top_price = book.asks[0].price if side == "BUY" else book.bids[0].price
    midpoint = (book.asks[0].price + book.bids[0].price) / D(2)
    spread_bps = (book.asks[0].price - book.bids[0].price) / midpoint * D(10000)
    slippage_bps: Decimal | None = None
    if average_price is not None:
        if side == "BUY":
            slippage_bps = (average_price / top_price - D(1)) * D(10000)
        else:
            slippage_bps = (D(1) - average_price / top_price) * D(10000)
        # Decimal arithmetic can produce tiny negative artifacts only if an
        # upstream order book violates ordering; keep the result fail-closed.
        if not slippage_bps.is_finite() or slippage_bps < 0:
            raise ValueError("ORDER_BOOK_SLIPPAGE_CALCULATION_INVALID")

    fee_quote = quote_total * fee_bps / D(10000)
    status: ExecutionStatus
    if filled == 0:
        status = "UNFILLED"
    elif remaining == 0:
        status = "FILLED"
    else:
        status = "PARTIALLY_FILLED"

    return OrderBookExecution(
        side=side,
        status=status,
        requested_quantity=quantity,
        filled_quantity=filled,
        remaining_quantity=remaining,
        quote_amount=quote_total,
        average_price=average_price,
        fee_bps=fee_bps,
        fee_quote=fee_quote,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        levels_consumed=len(slices),
        slices=tuple(slices),
        snapshot_timestamp=book.timestamp,
    )



@dataclass(frozen=True)
class OrderBookRoundTrip:
    """Realized quote-currency accounting for a book-walk entry and exit.

    Net PnL is deliberately unknown for margin unless an evidenced financing
    cost in the same quote currency is supplied. This avoids silently treating
    an EEA USDC liability as USD or treating an unknown rollover cost as zero.
    """

    direction: Literal["LONG", "SHORT"]
    position_status: Literal[
        "NOT_OPENED", "OPEN", "PARTIALLY_CLOSED", "CLOSED", "CLOSED_COST_INCOMPLETE"
    ]
    requested_notional_quote: Decimal
    target_quantity: Decimal
    entered_quantity: Decimal
    closed_quantity: Decimal
    remaining_quantity: Decimal
    entry: OrderBookExecution | None
    exit: OrderBookExecution | None
    gross_realized_pnl_quote: Decimal | None
    trading_fees_quote: Decimal
    financing_cost_quote: Decimal | None
    net_realized_pnl_quote: Decimal | None
    entry_target_filled: bool
    quote_currency: str


def simulate_order_book_round_trip(
    entry_book: OrderBookSnapshot,
    exit_book: OrderBookSnapshot,
    *,
    direction: Literal["LONG", "SHORT"],
    requested_notional_quote: Decimal,
    entry_fee_bps: Decimal,
    exit_fee_bps: Decimal,
    quote_currency: str,
    is_margin: bool = False,
    financing_cost_quote: Decimal | None = None,
    max_level_participation: Decimal = D("1"),
) -> OrderBookRoundTrip:
    """Replay a long/short round trip against two chronological L2 snapshots.

    financing_cost_quote must be an evidenced amount already expressed in
    quote_currency. For margin, omitting it prevents net PnL from being
    reported. EEA USDC financing must therefore be valued explicitly before
    it can be combined with a USD quote PnL.
    """
    if direction not in ("LONG", "SHORT"):
        raise ValueError("ROUND_TRIP_DIRECTION_INVALID")
    if exit_book.timestamp <= entry_book.timestamp:
        raise ValueError("ROUND_TRIP_BOOKS_NOT_CHRONOLOGICAL")
    if not requested_notional_quote.is_finite() or requested_notional_quote <= 0:
        raise ValueError("ROUND_TRIP_NOTIONAL_INVALID")
    normalized_quote = quote_currency.strip().upper()
    if not normalized_quote:
        raise ValueError("ROUND_TRIP_QUOTE_CURRENCY_REQUIRED")
    if financing_cost_quote is not None and (
        not financing_cost_quote.is_finite() or financing_cost_quote < 0
    ):
        raise ValueError("ROUND_TRIP_FINANCING_COST_INVALID")
    if not is_margin and financing_cost_quote not in (None, D(0)):
        raise ValueError("SPOT_ROUND_TRIP_CANNOT_HAVE_MARGIN_FINANCING")

    midpoint = (entry_book.bids[0].price + entry_book.asks[0].price) / D(2)
    target_quantity = requested_notional_quote / midpoint
    entry_side: OrderSide = "BUY" if direction == "LONG" else "SELL"
    exit_side: OrderSide = "SELL" if direction == "LONG" else "BUY"
    entry = simulate_order_book_execution(
        entry_book,
        side=entry_side,
        quantity=target_quantity,
        fee_bps=entry_fee_bps,
        max_level_participation=max_level_participation,
    )
    if entry.filled_quantity == 0:
        return OrderBookRoundTrip(
            direction, "NOT_OPENED", requested_notional_quote, target_quantity,
            D(0), D(0), D(0), entry, None, None, D(0),
            D(0) if not is_margin else financing_cost_quote,
            None, False, normalized_quote,
        )

    exit_result = simulate_order_book_execution(
        exit_book,
        side=exit_side,
        quantity=entry.filled_quantity,
        fee_bps=exit_fee_bps,
        max_level_participation=max_level_participation,
    )
    closed_quantity = exit_result.filled_quantity
    remaining_quantity = entry.filled_quantity - closed_quantity
    gross_pnl: Decimal | None = None
    trading_fees = D(0)
    allocated_financing: Decimal | None = D(0)
    net_pnl: Decimal | None = None

    if closed_quantity > 0 and entry.average_price is not None:
        entry_quote_for_closed = entry.average_price * closed_quantity
        if direction == "LONG":
            gross_pnl = exit_result.quote_amount - entry_quote_for_closed
        else:
            gross_pnl = entry_quote_for_closed - exit_result.quote_amount
        entry_fee_allocated = entry.fee_quote * closed_quantity / entry.filled_quantity
        trading_fees = entry_fee_allocated + exit_result.fee_quote
        if is_margin:
            if financing_cost_quote is None:
                allocated_financing = None
            else:
                allocated_financing = (
                    financing_cost_quote * closed_quantity / entry.filled_quantity
                )
        else:
            allocated_financing = D(0)
        if allocated_financing is not None:
            net_pnl = gross_pnl - trading_fees - allocated_financing

    if remaining_quantity > 0:
        status: Literal[
            "NOT_OPENED", "OPEN", "PARTIALLY_CLOSED", "CLOSED", "CLOSED_COST_INCOMPLETE"
        ] = "PARTIALLY_CLOSED" if closed_quantity > 0 else "OPEN"
    elif is_margin and financing_cost_quote is None:
        status = "CLOSED_COST_INCOMPLETE"
    else:
        status = "CLOSED"

    return OrderBookRoundTrip(
        direction=direction,
        position_status=status,
        requested_notional_quote=requested_notional_quote,
        target_quantity=target_quantity,
        entered_quantity=entry.filled_quantity,
        closed_quantity=closed_quantity,
        remaining_quantity=remaining_quantity,
        entry=entry,
        exit=exit_result,
        gross_realized_pnl_quote=gross_pnl,
        trading_fees_quote=trading_fees,
        financing_cost_quote=allocated_financing,
        net_realized_pnl_quote=net_pnl,
        entry_target_filled=entry.complete,
        quote_currency=normalized_quote,
    )
