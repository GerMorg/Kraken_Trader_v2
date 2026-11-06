from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import threading
import time
import zlib
from typing import Any

from app.persistence import Persistence

D = Decimal
WS_URL = "wss://ws.kraken.com/v2"
SOURCE = "KRAKEN_SPOT_WS_V2"
MAX_BOOK_DEPTH = 10


def _decimal(value: Any) -> Decimal | None:
    try:
        number = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _epoch(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp())


def _checksum_component(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.replace(".", "").lstrip("0")


def calculate_book_checksum(
    bids: dict[Decimal, Decimal],
    asks: dict[Decimal, Decimal],
) -> int:
    """Return the Kraken WS v2 CRC32 checksum for the top ten levels."""
    tokens: list[str] = []
    for side in (asks, bids):
        ordered = sorted(side.items(), key=lambda item: item[0], reverse=side is bids)
        for price, quantity in ordered[:MAX_BOOK_DEPTH]:
            tokens.append(_checksum_component(price))
            tokens.append(_checksum_component(quantity))
    return zlib.crc32("".join(tokens).encode("ascii")) & 0xFFFFFFFF


@dataclass
class _Book:
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    valid: bool = False
    last_event_epoch: int = 0
    checksum: int | None = None


@dataclass
class _Minute:
    pair: str
    minute_ts: int
    trade_count: int = 0
    buy_notional_usd: Decimal = D(0)
    sell_notional_usd: Decimal = D(0)
    signed_trade_notional_usd: Decimal = D(0)
    book_update_count: int = 0
    ofi_l1_base: Decimal = D(0)
    checksum_checks: int = 0
    ofi_l1_denominator_base: Decimal = D(0)
    checksum_failures: int = 0
    trade_id_gaps: int = 0
    connection_gap_count: int = 0
    bid_depth_usd: Decimal | None = None
    ask_depth_usd: Decimal | None = None
    spread_bps: Decimal | None = None
    depth_imbalance: Decimal | None = None
    microprice_deviation_bps: Decimal | None = None
    last_event_ts: int = 0
    book_verified: bool = False
    book_event_ts: int = 0
    quality_flags: set[str] = field(default_factory=set)


class MarketMicrostructureCollector:
    """Public Kraken WS v2 observer; never creates trading intents or orders.

    Raw messages are processed in memory and reduced to immutable one-minute
    feature records. The websocket worker owns its connection; SQLite writes are
    serialized by Persistence's lock.
    """

    def __init__(self, persistence: Persistence, *, enabled: bool = True) -> None:
        self.persistence = persistence
        self.enabled = enabled
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._symbols_changed = threading.Event()
        self._symbols: tuple[str, ...] = ()
        self._internal_to_ws_symbol: dict[str, str] = {}
        self._ws_to_internal_symbol: dict[str, str] = {}
        self._expected_subscriptions: set[tuple[str, str]] = set()
        self._acknowledged_subscriptions: set[tuple[str, str]] = set()
        self._rejected_subscriptions: dict[tuple[str, str], str] = {}
        self._books: dict[str, _Book] = {}
        self._minutes: dict[tuple[str, int], _Minute] = {}
        self._last_trade_id: dict[str, int] = {}
        self._metrics: dict[str, Any] = {
            "connection_state": "DISABLED" if not enabled else "WAITING_FOR_SYMBOLS",
            "connected": False,
            "messages": 0,
            "parse_errors": 0,
            "book_snapshots": 0,
            "book_updates": 0,
            "trades": 0,
            "minute_bars_saved": 0,
            "checksum_checks": 0,
            "checksum_failures": 0,
            "trade_id_gaps": 0,
            "connection_gaps": 0,
            "reconnects": 0,
            "subscription_expected_count": 0,
            "subscription_acknowledged_count": 0,
            "subscription_rejected_count": 0,
            "subscription_pending_count": 0,
            "subscription_coverage_pct": 0.0,
            "last_subscription_error": None,
            "last_subscription_channel": None,
            "last_subscription_symbol": None,
            "last_message_epoch": None,
            "last_error_type": None,
        }
        self._thread: threading.Thread | None = None
        if enabled:
            self._thread = threading.Thread(
                target=self._run,
                name="kraken-microstructure-ws",
                daemon=True,
            )
            self._thread.start()

    @staticmethod
    def _valid_pair(pair: str) -> bool:
        parts = pair.strip().upper().rsplit("/", 1)
        fiat = {"USD", "EUR", "GBP", "CAD", "AUD", "CHF", "JPY"}
        return (
            len(parts) == 2
            and bool(parts[0])
            and parts[0] not in fiat
            and parts[1] == "USD"
        )

    @staticmethod
    def to_ws_v2_symbol(pair: str) -> str:
        """Translate REST wsname symbols to Kraken Spot WebSocket v2 symbols."""
        normalized = pair.strip().upper()
        parts = normalized.rsplit("/", 1)
        if len(parts) != 2:
            return normalized
        base, quote = parts
        if base == "XBT":
            base = "BTC"
        return f"{base}/{quote}"

    def _internal_symbol(self, ws_symbol: str) -> str:
        normalized = str(ws_symbol).strip().upper()
        with self._lock:
            mapped = self._ws_to_internal_symbol.get(normalized)
        if mapped is not None:
            return mapped
        if normalized.startswith("BTC/"):
            return "XBT/" + normalized.split("/", 1)[1]
        return normalized

    def _refresh_subscription_metrics_locked(self) -> None:
        expected = len(self._expected_subscriptions)
        acknowledged = len(self._acknowledged_subscriptions)
        rejected = len(self._rejected_subscriptions)
        pending = max(0, expected - acknowledged - rejected)
        self._metrics["subscription_expected_count"] = expected
        self._metrics["subscription_acknowledged_count"] = acknowledged
        self._metrics["subscription_rejected_count"] = rejected
        self._metrics["subscription_pending_count"] = pending
        self._metrics["subscription_coverage_pct"] = (
            round(100.0 * acknowledged / expected, 1) if expected else 0.0
        )
        if self._metrics["connected"]:
            if pending:
                self._metrics["connection_state"] = "SUBSCRIBING"
            elif rejected:
                self._metrics["connection_state"] = "CONNECTED_PARTIAL"
            elif expected and acknowledged == expected:
                self._metrics["connection_state"] = "CONNECTED"
                if self._metrics["last_error_type"] in {
                    None, "SUBSCRIPTION_REJECTED", "SUBSCRIPTION_ACK_TIMEOUT"
                }:
                    self._metrics["last_error_type"] = None
                    self._metrics["last_subscription_error"] = None
                    self._metrics["last_subscription_channel"] = None
                    self._metrics["last_subscription_symbol"] = None

    def _prepare_subscriptions(self, symbols: tuple[str, ...]) -> list[str]:
        internal_to_ws = {pair: self.to_ws_v2_symbol(pair) for pair in symbols}
        ws_to_internal = {ws_symbol: pair for pair, ws_symbol in internal_to_ws.items()}
        ws_symbols = list(dict.fromkeys(internal_to_ws.values()))
        with self._lock:
            self._internal_to_ws_symbol = internal_to_ws
            self._ws_to_internal_symbol = ws_to_internal
            self._expected_subscriptions = {
                (channel, symbol)
                for channel in ("book", "trade")
                for symbol in ws_symbols
            }
            self._acknowledged_subscriptions.clear()
            self._rejected_subscriptions.clear()
            self._refresh_subscription_metrics_locked()
        return ws_symbols

    def update_symbols(self, symbols: list[str]) -> None:
        """Replace the USD crypto universe, preserving REST aliases internally."""
        normalized: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            pair = str(symbol).strip().upper()
            if self._valid_pair(pair) and pair not in seen:
                normalized.append(pair)
                seen.add(pair)
            if len(normalized) >= 20:
                break
        current = tuple(normalized)
        with self._lock:
            if current == self._symbols:
                return
            self._symbols = current
            if self.enabled:
                self._symbols_changed.set()

    def desired_symbols(self) -> tuple[str, ...]:
        with self._lock:
            return self._symbols

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                **self._metrics,
                "subscribed_symbol_count": len(self._symbols),
                "subscribed_symbols": list(self._symbols),
                "websocket_symbols": list(self._internal_to_ws_symbol.values()),
                "source": SOURCE,
                "aggregation": "ONE_MINUTE",
                "book_depth": MAX_BOOK_DEPTH,
                "checksum_required_for_valid_book": True,
                "actual_fills": False,
                "portfolio_pnl": False,
                "order_creation_allowed": False,
                "release_gate_eligible": False,
            }

    def process_message(self, raw: str | bytes | dict[str, Any]) -> bool:
        """Process one server message. False means reconnect for a clean snapshot."""
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                with self._lock:
                    self._metrics["parse_errors"] += 1
                    self._metrics["last_error_type"] = "MESSAGE_PARSE_ERROR"
                return True
        try:
            message = (
                raw if isinstance(raw, dict)
                else json.loads(raw, parse_float=D, parse_int=int)
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            with self._lock:
                self._metrics["parse_errors"] += 1
                self._metrics["last_error_type"] = "MESSAGE_PARSE_ERROR"
            return True
        if not isinstance(message, dict):
            return True

        channel = message.get("channel")
        message_type = message.get("type")
        data = message.get("data")
        with self._lock:
            self._metrics["messages"] += 1
            self._metrics["last_message_epoch"] = int(time.time())

        if message.get("method") == "subscribe" and isinstance(message.get("success"), bool):
            result = message.get("result")
            result = result if isinstance(result, dict) else {}
            subscription_channel = str(
                result.get("channel") or message.get("channel") or ""
            ).strip().lower()
            subscription_symbol = str(
                result.get("symbol") or message.get("symbol") or ""
            ).strip().upper()
            error = str(message.get("error") or "SUBSCRIPTION_REJECTED")[:240]
            if not subscription_symbol:
                for token in error.upper().split():
                    candidate = token.strip(".,;:'\"()[]{}")
                    if self._valid_pair(candidate):
                        subscription_symbol = candidate
                        break
            with self._lock:
                key = (subscription_channel, subscription_symbol)
                if message["success"] is True:
                    if key in self._expected_subscriptions:
                        self._acknowledged_subscriptions.add(key)
                        self._rejected_subscriptions.pop(key, None)
                    self._refresh_subscription_metrics_locked()
                else:
                    self._metrics["last_error_type"] = "SUBSCRIPTION_REJECTED"
                    self._metrics["last_subscription_error"] = error
                    self._metrics["last_subscription_channel"] = subscription_channel or None
                    self._metrics["last_subscription_symbol"] = subscription_symbol or None
                    rejected_keys = (
                        [key] if subscription_channel and subscription_symbol
                        else [
                            expected_key for expected_key in self._expected_subscriptions
                            if subscription_symbol and expected_key[1] == subscription_symbol
                        ]
                    )
                    for rejected_key in rejected_keys:
                        if rejected_key in self._expected_subscriptions:
                            self._rejected_subscriptions[rejected_key] = error
                    self._refresh_subscription_metrics_locked()
            # Keep healthy symbols connected; a rejected pair must not cause a reconnect storm.
            return True

        if channel not in {"book", "trade"}:
            return True
        if not isinstance(data, list):
            with self._lock:
                self._metrics["parse_errors"] += 1
                self._metrics["last_error_type"] = "MESSAGE_DATA_INVALID"
            return False

        for item in data:
            if not isinstance(item, dict):
                continue
            if channel == "book":
                if not self._apply_book(item, str(message_type or "")):
                    return False
            elif channel == "trade":
                self._apply_trades(item)
        self.flush_due()
        return True

    def _minute(self, pair: str, epoch: int) -> _Minute:
        minute_ts = (epoch // 60) * 60
        key = (pair, minute_ts)
        current = self._minutes.get(key)
        if current is None:
            current = _Minute(pair=pair, minute_ts=minute_ts)
            self._minutes[key] = current
        return current

    @staticmethod
    def _levels(raw: Any) -> dict[Decimal, Decimal] | None:
        if not isinstance(raw, list):
            return None
        result: dict[Decimal, Decimal] = {}
        for level in raw:
            if not isinstance(level, dict):
                return None
            price = _decimal(level.get("price"))
            quantity = _decimal(level.get("qty"))
            if price is None or quantity is None or price <= 0 or quantity <= 0:
                return None
            result[price] = quantity
        if not result:
            return None
        return result

    @staticmethod
    def _trim(book: dict[Decimal, Decimal], *, bids: bool) -> dict[Decimal, Decimal]:
        keys = sorted(book, reverse=bids)[:MAX_BOOK_DEPTH]
        return {price: book[price] for price in keys}

    def _apply_book(self, item: dict[str, Any], message_type: str) -> bool:
        pair = self._internal_symbol(str(item.get("symbol", "")))
        if not self._valid_pair(pair):
            return True
        timestamp = _epoch(item.get("timestamp"))
        if timestamp is None:
            return True

        with self._lock:
            book = self._books.setdefault(pair, _Book())
            current_minute = self._minute(pair, timestamp)
            previous_bid = max(book.bids) if book.bids else None
            previous_ask = min(book.asks) if book.asks else None
            previous_bid_qty = book.bids.get(previous_bid, D(0)) if previous_bid is not None else D(0)
            previous_ask_qty = book.asks.get(previous_ask, D(0)) if previous_ask is not None else D(0)

            is_snapshot = message_type == "snapshot"
            if is_snapshot:
                bids = self._levels(item.get("bids"))
                asks = self._levels(item.get("asks"))
                if bids is None or asks is None:
                    book.valid = False
                    current_minute.book_verified = False
                    current_minute.quality_flags.add("INVALID_BOOK_SNAPSHOT")
                    self._metrics["last_error_type"] = "INVALID_BOOK_SNAPSHOT"
                    return False
                book.bids = self._trim(bids, bids=True)
                book.asks = self._trim(asks, bids=False)
            elif message_type == "update":
                if not book.valid:
                    current_minute.book_verified = False
                    current_minute.quality_flags.add("UPDATE_WITHOUT_VALID_SNAPSHOT")
                    self._metrics["last_error_type"] = "UPDATE_WITHOUT_VALID_SNAPSHOT"
                    return False
                for key, side in (("bids", book.bids), ("asks", book.asks)):
                    changes = item.get(key, [])
                    if not isinstance(changes, list):
                        book.valid = False
                        current_minute.book_verified = False
                        current_minute.quality_flags.add("INVALID_BOOK_UPDATE")
                        self._metrics["last_error_type"] = "INVALID_BOOK_UPDATE"
                        return False
                    for level in changes:
                        if not isinstance(level, dict):
                            book.valid = False
                            current_minute.book_verified = False
                            current_minute.quality_flags.add("INVALID_BOOK_UPDATE")
                            self._metrics["last_error_type"] = "INVALID_BOOK_UPDATE"
                            return False
                        price = _decimal(level.get("price"))
                        quantity = _decimal(level.get("qty"))
                        if price is None or quantity is None or price <= 0 or quantity < 0:
                            book.valid = False
                            current_minute.book_verified = False
                            current_minute.quality_flags.add("INVALID_BOOK_UPDATE")
                            self._metrics["last_error_type"] = "INVALID_BOOK_UPDATE"
                            return False
                        if quantity == 0:
                            side.pop(price, None)
                        else:
                            side[price] = quantity
                book.bids = self._trim(book.bids, bids=True)
                book.asks = self._trim(book.asks, bids=False)
            else:
                return True

            best_bid = max(book.bids) if book.bids else None
            best_ask = min(book.asks) if book.asks else None
            if best_bid is None or best_ask is None or best_bid >= best_ask:
                book.valid = False
                current_minute.book_verified = False
                current_minute.quality_flags.add("CROSSED_OR_EMPTY_BOOK")
                self._metrics["last_error_type"] = "CROSSED_OR_EMPTY_BOOK"
                return False

            checksum_value = item.get("checksum")
            if checksum_value is None:
                book.valid = False
                current_minute.book_verified = False
                current_minute.quality_flags.add("BOOK_CHECKSUM_MISSING")
                self._metrics["last_error_type"] = "BOOK_CHECKSUM_MISSING"
                return False
            try:
                expected_checksum = int(checksum_value)
            except (ValueError, TypeError, OverflowError):
                book.valid = False
                current_minute.book_verified = False
                current_minute.quality_flags.add("BOOK_CHECKSUM_INVALID")
                self._metrics["last_error_type"] = "BOOK_CHECKSUM_INVALID"
                return False

            actual_checksum = calculate_book_checksum(book.bids, book.asks)
            current_minute.checksum_checks += 1
            self._metrics["checksum_checks"] += 1
            if actual_checksum != expected_checksum:
                book.valid = False
                current_minute.checksum_failures += 1
                current_minute.book_verified = False
                current_minute.quality_flags.add("BOOK_CHECKSUM_MISMATCH")
                self._metrics["checksum_failures"] += 1
                self._metrics["last_error_type"] = "BOOK_CHECKSUM_MISMATCH"
                return False

            book.valid = True
            book.last_event_epoch = timestamp
            book.checksum = actual_checksum
            current_minute.book_verified = True
            current_minute.book_event_ts = timestamp
            current_minute.last_event_ts = max(current_minute.last_event_ts, timestamp)
            if is_snapshot:
                self._metrics["book_snapshots"] += 1
            else:
                self._metrics["book_updates"] += 1
                current_minute.book_update_count += 1
                if previous_bid is not None and previous_ask is not None:
                    new_bid_qty = book.bids.get(best_bid, D(0))
                    new_ask_qty = book.asks.get(best_ask, D(0))
                    bid_flow = (
                        new_bid_qty if best_bid > previous_bid
                        else -previous_bid_qty if best_bid < previous_bid
                        else new_bid_qty - previous_bid_qty
                    )
                    ask_flow = (
                        -new_ask_qty if best_ask < previous_ask
                        else previous_ask_qty if best_ask > previous_ask
                        else previous_ask_qty - new_ask_qty
                    )
                    current_minute.ofi_l1_base += bid_flow + ask_flow
                    current_minute.ofi_l1_denominator_base += (
                        previous_bid_qty + previous_ask_qty
                    )

            bid_depth = sum((price * quantity for price, quantity in book.bids.items()), D(0))
            ask_depth = sum((price * quantity for price, quantity in book.asks.items()), D(0))
            total_depth = bid_depth + ask_depth
            spread_bps = ((best_ask - best_bid) / ((best_ask + best_bid) / D(2))) * D(10000)
            top_bid_qty = book.bids[best_bid]
            top_ask_qty = book.asks[best_ask]
            total_top_qty = top_bid_qty + top_ask_qty
            microprice = (
                best_ask * top_bid_qty + best_bid * top_ask_qty
            ) / total_top_qty if total_top_qty > 0 else (best_bid + best_ask) / D(2)
            mid = (best_bid + best_ask) / D(2)

            current_minute.bid_depth_usd = bid_depth
            current_minute.ask_depth_usd = ask_depth
            current_minute.spread_bps = spread_bps
            current_minute.depth_imbalance = (
                (bid_depth - ask_depth) / total_depth if total_depth > 0 else None
            )
            current_minute.microprice_deviation_bps = (
                (microprice - mid) / mid * D(10000) if mid > 0 else None
            )
            return True

    def _apply_trades(self, item: dict[str, Any]) -> None:
        pair = self._internal_symbol(str(item.get("symbol", "")))
        if not self._valid_pair(pair):
            return
        epoch = _epoch(item.get("timestamp"))
        price = _decimal(item.get("price"))
        quantity = _decimal(item.get("qty"))
        side = str(item.get("side", "")).lower()
        if (
            epoch is None or price is None or quantity is None
            or price <= 0 or quantity <= 0 or side not in {"buy", "sell"}
        ):
            return
        notional = price * quantity
        trade_id_raw = item.get("trade_id")
        with self._lock:
            current = self._minute(pair, epoch)
            current.trade_count += 1
            current.last_event_ts = max(current.last_event_ts, epoch)
            if side == "buy":
                current.buy_notional_usd += notional
                current.signed_trade_notional_usd += notional
            else:
                current.sell_notional_usd += notional
                current.signed_trade_notional_usd -= notional
            self._metrics["trades"] += 1
            if trade_id_raw is not None:
                try:
                    trade_id = int(trade_id_raw)
                except (TypeError, ValueError, OverflowError):
                    current.quality_flags.add("INVALID_TRADE_ID")
                    return
                previous = self._last_trade_id.get(pair)
                if previous is None:
                    current.quality_flags.add("TRADE_ID_BASELINE_UNAVAILABLE")
                if previous is not None and trade_id > previous + 1:
                    missing = trade_id - previous - 1
                    current.trade_id_gaps += missing
                    current.quality_flags.add("TRADE_ID_GAP")
                    self._metrics["trade_id_gaps"] += missing
                elif previous is not None and trade_id <= previous:
                    current.quality_flags.add("TRADE_ID_NON_MONOTONIC")
                if previous is None or trade_id > previous:
                    self._last_trade_id[pair] = trade_id

    def _mark_connection_gap(self) -> None:
        with self._lock:
            self._metrics["connection_gaps"] += 1
            for book in self._books.values():
                book.valid = False
            for aggregate in self._minutes.values():
                aggregate.connection_gap_count += 1
                aggregate.quality_flags.add("CONNECTION_GAP")

    def flush_due(self, now_epoch: int | None = None) -> int:
        """Persist completed minute bars only; the current, incomplete minute stays open."""
        now = int(time.time()) if now_epoch is None else int(now_epoch)
        due: list[tuple[str, int]] = []
        with self._lock:
            due = [
                key for key, value in self._minutes.items()
                if value.minute_ts + 60 <= now
            ]
            aggregates = [self._minutes.pop(key) for key in sorted(due)]
        saved = 0
        for aggregate in aggregates:
            total_trade_notional = aggregate.buy_notional_usd + aggregate.sell_notional_usd
            trade_imbalance = (
                aggregate.signed_trade_notional_usd / total_trade_notional
                if total_trade_notional > 0 else None
            )
            ofi_imbalance = (
                aggregate.ofi_l1_base / aggregate.ofi_l1_denominator_base
                if aggregate.ofi_l1_denominator_base > 0 else None
            )
            book_fresh = (
                aggregate.book_event_ts > 0
                and aggregate.minute_ts + 60 - aggregate.book_event_ts <= 120
            )
            quality_status = (
                "VALID" if aggregate.book_verified and book_fresh
                and aggregate.checksum_checks > 0
                and aggregate.checksum_failures == 0
                and aggregate.trade_id_gaps == 0
                and aggregate.connection_gap_count == 0
                and not aggregate.quality_flags
                else "PARTIAL"
            )
            payload = {
                "pair": aggregate.pair,
                "minute_ts": aggregate.minute_ts,
                "source": SOURCE,
                "trade_count": aggregate.trade_count,
                "buy_notional_usd": str(aggregate.buy_notional_usd),
                "sell_notional_usd": str(aggregate.sell_notional_usd),
                "signed_trade_notional_usd": str(aggregate.signed_trade_notional_usd),
                "trade_imbalance": str(trade_imbalance) if trade_imbalance is not None else None,
                "book_update_count": aggregate.book_update_count,
                "ofi_l1_base": str(aggregate.ofi_l1_base),
                "ofi_l1_imbalance": str(ofi_imbalance) if ofi_imbalance is not None else None,
                "spread_bps": str(aggregate.spread_bps) if aggregate.spread_bps is not None else None,
                "depth_imbalance": (
                    str(aggregate.depth_imbalance)
                    if aggregate.depth_imbalance is not None else None
                ),
                "microprice_deviation_bps": (
                    str(aggregate.microprice_deviation_bps)
                    if aggregate.microprice_deviation_bps is not None else None
                ),
                "bid_depth_usd": str(aggregate.bid_depth_usd) if aggregate.bid_depth_usd is not None else None,
                "ask_depth_usd": str(aggregate.ask_depth_usd) if aggregate.ask_depth_usd is not None else None,
                "checksum_checks": aggregate.checksum_checks,
                "checksum_failures": aggregate.checksum_failures,
                "trade_id_gaps": aggregate.trade_id_gaps,
                "connection_gap_count": aggregate.connection_gap_count,
                "last_event_ts": aggregate.last_event_ts,
                "book_event_ts": aggregate.book_event_ts,
                "quality_status": quality_status,
                "quality_flags": sorted(aggregate.quality_flags),
                "actual_fills": False,
                "portfolio_pnl": False,
                "order_creation_allowed": False,
                "release_gate_eligible": False,
            }
            try:
                if self.persistence.record_microstructure_bar(payload):
                    saved += 1
            except (ArithmeticError, TypeError, ValueError):
                with self._lock:
                    self._metrics["last_error_type"] = "MICROSTRUCTURE_PERSISTENCE_REJECTED"
        if saved:
            with self._lock:
                self._metrics["minute_bars_saved"] += saved
        return saved

    def _run(self) -> None:
        backoff = 1.0
        had_connection = False
        while not self._stop.is_set():
            symbols = self.desired_symbols()
            if not symbols:
                self._symbols_changed.wait(2.0)
                continue
            self._symbols_changed.clear()
            if self.desired_symbols() != symbols:
                continue
            try:
                from websockets.sync.client import connect

                ws_symbols = self._prepare_subscriptions(symbols)
                with connect(
                    WS_URL,
                    open_timeout=10,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=4_194_304,
                    proxy=None,
                ) as websocket:
                    with self._lock:
                        self._metrics["connection_state"] = "SUBSCRIBING"
                        self._metrics["connected"] = True
                        self._refresh_subscription_metrics_locked()
                        if had_connection:
                            self._metrics["reconnects"] += 1
                    had_connection = True
                    subscription_started = time.monotonic()
                    websocket.send(json.dumps({
                        "method": "subscribe",
                        "params": {
                            "channel": "book",
                            "symbol": ws_symbols,
                            "depth": MAX_BOOK_DEPTH,
                            "snapshot": True,
                        },
                    }))
                    websocket.send(json.dumps({
                        "method": "subscribe",
                        "params": {
                            "channel": "trade",
                            "symbol": ws_symbols,
                            "snapshot": False,
                        },
                    }))
                    while not self._stop.is_set():
                        with self._lock:
                            pending_subscriptions = self._metrics["subscription_pending_count"]
                        if (
                            pending_subscriptions
                            and time.monotonic() - subscription_started > 20.0
                        ):
                            with self._lock:
                                self._metrics["last_error_type"] = "SUBSCRIPTION_ACK_TIMEOUT"
                            break
                        if self._symbols_changed.is_set():
                            self._symbols_changed.clear()
                            break
                        try:
                            raw = websocket.recv(timeout=1.0)
                        except TimeoutError:
                            self.flush_due()
                            continue
                        if not self.process_message(raw):
                            break
                        with self._lock:
                            pending_subscriptions = self._metrics["subscription_pending_count"]
                        if pending_subscriptions == 0:
                            backoff = 1.0
                        self.flush_due()
            except Exception as exc:
                with self._lock:
                    self._metrics["last_error_type"] = type(exc).__name__
            finally:
                with self._lock:
                    was_connected = bool(self._metrics["connected"])
                    self._metrics["connected"] = False
                    self._metrics["connection_state"] = (
                        "STOPPING" if self._stop.is_set() else "RECONNECTING"
                    )
                if was_connected and not self._stop.is_set():
                    self._mark_connection_gap()
                self.flush_due()
            if not self._stop.is_set() and not self._symbols_changed.is_set():
                self._symbols_changed.wait(backoff)
                backoff = min(backoff * 2, 30.0)
        self.flush_due()
        with self._lock:
            self._metrics["connected"] = False
            self._metrics["connection_state"] = "STOPPED"

    def close(self) -> None:
        self._stop.set()
        self._symbols_changed.set()
        if self._thread is not None:
            self._thread.join(timeout=15.0)
