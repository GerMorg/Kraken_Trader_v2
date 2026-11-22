from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re
import uuid
from typing import Any, Iterable
from urllib.parse import urlencode

from app.futures_api import FuturesAPIClient, FuturesAPIError, Transport

D = Decimal
_SYMBOL_RE = re.compile(r"PF_([A-Z0-9]{2,16})USD")
_ORDER_TYPES = {"mkt", "lmt", "post", "ioc", "stp", "take_profit"}
_SIDES = {"buy", "sell"}
_NON_CRYPTO_BASES = frozenset({
    "AAPLX", "ANT", "CRCLX", "GLDX", "GOOGLX", "HOODX", "MSTRX",
    "NVDAX", "QQQX", "SPCX", "SPYX", "TSLAX",
    "PAXG", "XAG", "XAU", "XAUT", "XCU",
})


class FuturesTradingError(RuntimeError):
    """Sanitized Futures demo trading error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FuturesOrderRequest:
    decision_id: str
    symbol: str
    side: str
    size: Decimal
    order_type: str
    reduce_only: bool
    notional_usd: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trigger_signal: str = "mark"
    client_order_id: str | None = None


@dataclass(frozen=True)
class FuturesOrderAcknowledgement:
    status: str
    exchange_order_id: str | None
    client_order_id: str
    raw_status: str


@dataclass(frozen=True)
class FuturesSignal:
    symbol: str
    direction: str
    momentum_24h_bps: Decimal
    momentum_72h_bps: Decimal
    reason: str
    strategy_version: str = "futures-tsm-0.1.0"

    @property
    def has_direction(self) -> bool:
        return self.direction in {"LONG", "SHORT"}


@dataclass(frozen=True)
class FuturesRiskResult:
    allowed: bool
    reasons: tuple[str, ...]
    proposed_notional_usd: Decimal
    margin_required_usd: Decimal
    effective_leverage: Decimal


@dataclass(frozen=True)
class FuturesRiskLimits:
    max_leverage: Decimal = D("1.5")
    max_position_pct: Decimal = D("20")
    max_margin_pct: Decimal = D("20")
    max_risk_per_trade_pct: Decimal = D("1.0")
    cash_reserve_pct: Decimal = D("20")
    max_open_positions: int = 1
    daily_loss_limit_pct: Decimal = D("3")
    max_drawdown_pct: Decimal = D("8")



def parse_closed_hourly_closes(
    candles: Iterable[dict[str, Any]],
    *,
    now_epoch_seconds: Decimal,
    minimum_bars: int = 73,
    maximum_last_bar_age_seconds: int = 4500,
) -> tuple[list[Decimal], int | None, str]:
    """Return a contiguous sequence of closed H1 prices or an explicit fail-closed reason."""
    if (
        not isinstance(now_epoch_seconds, Decimal)
        or not now_epoch_seconds.is_finite()
        or now_epoch_seconds <= 0
        or isinstance(minimum_bars, bool)
        or minimum_bars < 1
        or maximum_last_bar_age_seconds < 0
    ):
        return [], None, "FUTURES_CANDLE_TIME_INPUT_INVALID"

    rows: dict[int, Decimal] = {}
    try:
        for candle in candles:
            if not isinstance(candle, dict):
                return [], None, "FUTURES_CANDLE_ROW_INVALID"
            timestamp_raw = candle.get("time")
            close_raw = candle.get("close")
            timestamp = int(str(timestamp_raw))
            close = D(str(close_raw))
            if timestamp < 0 or timestamp % 3_600_000 != 0:
                return [], None, "FUTURES_CANDLE_TIMESTAMP_INVALID"
            if not close.is_finite() or close <= 0:
                return [], None, "FUTURES_CANDLE_CLOSE_INVALID"
            if timestamp in rows:
                return [], None, "FUTURES_CANDLE_DUPLICATE_TIMESTAMP"
            rows[timestamp] = close
    except (ArithmeticError, KeyError, TypeError, ValueError, OverflowError):
        return [], None, "FUTURES_CANDLE_ROW_INVALID"

    now_ms = int(now_epoch_seconds * D(1000))
    closed = [
        (timestamp, close)
        for timestamp, close in sorted(rows.items())
        if timestamp + 3_600_000 <= now_ms
    ]
    if not closed:
        return [], None, "FUTURES_CLOSED_CANDLES_UNAVAILABLE"

    latest_timestamp, _latest_close = closed[-1]
    latest_bar_age = now_epoch_seconds - (
        D(latest_timestamp) / D(1000) + D(3600)
    )
    if (
        not latest_bar_age.is_finite()
        or latest_bar_age < D("-60")
        or latest_bar_age > D(maximum_last_bar_age_seconds)
    ):
        return [], None, "FUTURES_CLOSED_CANDLES_STALE"

    contiguous: list[tuple[int, Decimal]] = [closed[-1]]
    for timestamp, close in reversed(closed[:-1]):
        if contiguous[-1][0] - timestamp != 3_600_000:
            break
        contiguous.append((timestamp, close))
    contiguous.reverse()
    if len(contiguous) < minimum_bars:
        return [], latest_timestamp // 1000, "FUTURES_HOURLY_HISTORY_GAP_OR_INSUFFICIENT"

    return (
        [close for _timestamp, close in contiguous],
        latest_timestamp // 1000,
        "READY",
    )

def generate_futures_signal(
    symbol: str,
    hourly_closes: Iterable[Decimal],
    *,
    fast_threshold_bps: Decimal = D("120"),
    slow_threshold_bps: Decimal = D("180"),
) -> FuturesSignal:
    """Independent H1 Futures decision rule; not coupled to Spot/Margin signals."""
    normalized = str(symbol).strip().upper()
    if not _SYMBOL_RE.fullmatch(normalized):
        return FuturesSignal(normalized, "NONE", D(0), D(0), "NOT_LINEAR_USD_PERPETUAL")
    prices = list(hourly_closes)
    if len(prices) < 73:
        return FuturesSignal(normalized, "NONE", D(0), D(0), "INSUFFICIENT_HOURLY_HISTORY")
    if (
        not isinstance(fast_threshold_bps, Decimal)
        or not fast_threshold_bps.is_finite()
        or fast_threshold_bps <= 0
        or not isinstance(slow_threshold_bps, Decimal)
        or not slow_threshold_bps.is_finite()
        or slow_threshold_bps <= 0
    ):
        return FuturesSignal(normalized, "NONE", D(0), D(0), "INVALID_FUTURES_STRATEGY_THRESHOLDS")
    if any(not isinstance(value, Decimal) or not value.is_finite() or value <= 0 for value in prices):
        return FuturesSignal(normalized, "NONE", D(0), D(0), "INVALID_PRICE_HISTORY")
    last = prices[-1]
    fast = (last / prices[-25] - D(1)) * D(10000)
    slow = (last / prices[-73] - D(1)) * D(10000)
    if fast >= fast_threshold_bps and slow >= slow_threshold_bps:
        return FuturesSignal(normalized, "LONG", fast, slow, "FUTURES_TREND_UP")
    if fast <= -fast_threshold_bps and slow <= -slow_threshold_bps:
        return FuturesSignal(normalized, "SHORT", fast, slow, "FUTURES_TREND_DOWN")
    return FuturesSignal(normalized, "NONE", fast, slow, "NO_CONFIRMED_FUTURES_TREND")


def evaluate_futures_entry(
    *,
    equity_usd: Decimal,
    free_margin_usd: Decimal,
    current_margin_usd: Decimal,
    open_positions: int,
    notional_usd: Decimal,
    leverage: Decimal,
    stop_distance_pct: Decimal | None,
    estimated_round_trip_cost_bps: Decimal | None,
    estimated_funding_bps: Decimal | None,
    exchange_initial_margin_usd: Decimal | None,
    daily_pnl_pct: Decimal | None,
    drawdown_pct: Decimal | None,
    costs_complete: bool,
    funding_complete: bool,
    edge_validated: bool,
    net_edge_bps: Decimal | None,
    limits: FuturesRiskLimits = FuturesRiskLimits(),
) -> FuturesRiskResult:
    reasons: list[str] = []
    required = (equity_usd, free_margin_usd, current_margin_usd, notional_usd, leverage)
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in required):
        reasons.append("FUTURES_RISK_INPUT_NON_FINITE")
    optional = (
        stop_distance_pct, estimated_round_trip_cost_bps, estimated_funding_bps,
        exchange_initial_margin_usd, daily_pnl_pct, drawdown_pct, net_edge_bps,
    )
    if any(value is not None and (not isinstance(value, Decimal) or not value.is_finite()) for value in optional):
        reasons.append("FUTURES_OPTIONAL_RISK_INPUT_NON_FINITE")
    if reasons:
        return FuturesRiskResult(False, tuple(reasons), D(0), D(0), leverage)

    if equity_usd <= 0 or free_margin_usd < 0 or current_margin_usd < 0 or notional_usd <= 0:
        reasons.append("FUTURES_EQUITY_OR_NOTIONAL_INVALID")
    if leverage < D(1) or leverage > limits.max_leverage:
        reasons.append("FUTURES_LEVERAGE_OUTSIDE_PILOT_CAP")
    if open_positions >= limits.max_open_positions:
        reasons.append("FUTURES_OPEN_POSITION_LIMIT")
    if not costs_complete or estimated_round_trip_cost_bps is None:
        reasons.append("FUTURES_COSTS_INCOMPLETE")
    if not funding_complete or estimated_funding_bps is None:
        reasons.append("FUTURES_FUNDING_INCOMPLETE")
    if exchange_initial_margin_usd is None or exchange_initial_margin_usd <= 0:
        reasons.append("FUTURES_EXCHANGE_INITIAL_MARGIN_UNKNOWN")
    if stop_distance_pct is None or stop_distance_pct <= 0:
        reasons.append("FUTURES_STOP_DISTANCE_UNKNOWN")
    if not edge_validated:
        reasons.append("FUTURES_EDGE_NOT_VALIDATED")
    if net_edge_bps is None or net_edge_bps <= 0:
        reasons.append("FUTURES_NO_POSITIVE_NET_EDGE")
    if daily_pnl_pct is None or daily_pnl_pct <= -limits.daily_loss_limit_pct:
        reasons.append("FUTURES_DAILY_LOSS_LIMIT_OR_BASELINE_UNKNOWN")
    if drawdown_pct is None or drawdown_pct >= limits.max_drawdown_pct:
        reasons.append("FUTURES_DRAWDOWN_LIMIT_OR_BASELINE_UNKNOWN")

    position_cap = equity_usd * limits.max_position_pct / D(100)
    margin_cap = equity_usd * limits.max_margin_pct / D(100)
    reserve_adjusted_margin = equity_usd * (D(100) - limits.cash_reserve_pct) / D(100)
    # Exact IM must be derived from the current Kraken instrument size tier.
    # Do not approximate Initial Margin as notional / leverage.
    margin_required = (
        exchange_initial_margin_usd
        if exchange_initial_margin_usd is not None and exchange_initial_margin_usd > 0
        else D(0)
    )
    if notional_usd > position_cap:
        reasons.append("FUTURES_POSITION_LIMIT")
    if current_margin_usd + margin_required > margin_cap:
        reasons.append("FUTURES_MARGIN_CAP")
    if margin_required > free_margin_usd:
        reasons.append("FUTURES_FREE_MARGIN_INSUFFICIENT")
    if current_margin_usd + margin_required > reserve_adjusted_margin:
        reasons.append("FUTURES_COLLATERAL_RESERVE_VIOLATION")
    if stop_distance_pct is not None and stop_distance_pct > 0:
        risk_budget = equity_usd * limits.max_risk_per_trade_pct / D(100)
        stressed_loss = notional_usd * (
            stop_distance_pct / D(100)
            + D("0.5") / D(100)
            + ((estimated_round_trip_cost_bps or D(0)) + abs(estimated_funding_bps or D(0))) / D(10000)
        )
        if stressed_loss > risk_budget:
            reasons.append("FUTURES_TRADE_RISK_BUDGET_EXCEEDED")

    return FuturesRiskResult(
        not reasons,
        tuple(dict.fromkeys(reasons)),
        notional_usd,
        margin_required,
        leverage,
    )


class FuturesDemoTradingClient:
    """Authenticated Kraken Futures trading client pinned to the demo host only."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        environment: str = "demo",
        timeout_seconds: float = 10.0,
        transport: Transport | None = None,
    ) -> None:
        if environment != "demo":
            raise ValueError("FUTURES_TRADING_DEMO_ONLY")
        if not api_key.strip() or not api_secret.strip():
            raise ValueError("FUTURES_DEMO_CREDENTIALS_REQUIRED")
        self._readonly = FuturesAPIClient(
            api_key=api_key,
            api_secret=api_secret,
            environment="demo",
            timeout_seconds=timeout_seconds,
            transport=transport,
        )

    @staticmethod
    def _decimal_string(value: Decimal, error_code: str) -> str:
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError(error_code)
        return format(value, "f")

    def _post(self, path: str, values: list[tuple[str, str]]) -> dict[str, Any]:
        return self._readonly._request_post(path, post_data=urlencode(values))

    def send_order(self, request: FuturesOrderRequest) -> FuturesOrderAcknowledgement:
        symbol = request.symbol.strip().upper()
        if not _SYMBOL_RE.fullmatch(symbol):
            raise ValueError("FUTURES_SYMBOL_NOT_LINEAR_USD_PERPETUAL")
        side = request.side.strip().lower()
        if side not in _SIDES:
            raise ValueError("FUTURES_ORDER_SIDE_INVALID")
        if request.order_type not in _ORDER_TYPES:
            raise ValueError("FUTURES_ORDER_TYPE_INVALID")
        if not isinstance(request.reduce_only, bool):
            raise ValueError("FUTURES_REDUCE_ONLY_MUST_BE_EXPLICIT")
        size = self._decimal_string(request.size, "FUTURES_ORDER_SIZE_INVALID")
        client_id = request.client_order_id or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", client_id):
            raise ValueError("FUTURES_CLIENT_ORDER_ID_INVALID")

        values = [
            ("orderType", request.order_type),
            ("symbol", symbol),
            ("side", side),
            ("size", size),
            ("cliOrdId", client_id),
            ("reduceOnly", "true" if request.reduce_only else "false"),
        ]
        if request.limit_price is not None:
            values.append(("limitPrice", self._decimal_string(request.limit_price, "FUTURES_LIMIT_PRICE_INVALID")))
        if request.stop_price is not None:
            values.append(("stopPrice", self._decimal_string(request.stop_price, "FUTURES_STOP_PRICE_INVALID")))
        if request.order_type in {"lmt", "post", "ioc"} and request.limit_price is None:
            raise ValueError("FUTURES_LIMIT_PRICE_REQUIRED")
        if request.order_type in {"stp", "take_profit"}:
            if request.stop_price is None or request.trigger_signal not in {"mark", "index", "last"}:
                raise ValueError("FUTURES_STOP_TRIGGER_CONFIGURATION_INVALID")
            values.append(("triggerSignal", request.trigger_signal))

        payload = self._post("/derivatives/api/v3/sendorder", values)
        send_status = payload.get("sendStatus")
        if not isinstance(send_status, dict) or not isinstance(send_status.get("status"), str):
            raise FuturesAPIError("FUTURES_RESPONSE_SCHEMA_INVALID")
        raw_status = str(send_status["status"])
        if payload.get("result") != "success":
            raise FuturesAPIError("FUTURES_API_REJECTED")
        if raw_status not in {"placed", "partiallyFilled", "filled"}:
            raise FuturesTradingError("FUTURES_ORDER_NOT_ACCEPTED_" + re.sub(r"[^A-Za-z0-9_]", "_", raw_status)[:48])
        order_id = send_status.get("order_id")
        return FuturesOrderAcknowledgement(
            status="ACKNOWLEDGED",
            exchange_order_id=str(order_id) if order_id else None,
            client_order_id=client_id,
            raw_status=raw_status,
        )

    def cancel_order(
        self, *, order_id: str | None = None, client_order_id: str | None = None
    ) -> dict[str, Any]:
        if bool(order_id) == bool(client_order_id):
            raise ValueError("FUTURES_CANCEL_REQUIRES_EXACTLY_ONE_ORDER_ID")
        key, value = ("order_id", order_id) if order_id else ("cliOrdId", client_order_id)
        payload = self._post("/derivatives/api/v3/cancelorder", [(key, str(value))])
        status = payload.get("cancelStatus")
        if payload.get("result") != "success" or not isinstance(status, dict):
            raise FuturesAPIError("FUTURES_CANCEL_RESPONSE_INVALID")
        return status

    def edit_order(
        self,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
        size: Decimal | None = None,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
    ) -> dict[str, Any]:
        if bool(order_id) == bool(client_order_id):
            raise ValueError("FUTURES_EDIT_REQUIRES_EXACTLY_ONE_ORDER_ID")
        if size is None and limit_price is None and stop_price is None:
            raise ValueError("FUTURES_EDIT_REQUIRES_CHANGE")
        values: list[tuple[str, str]] = []
        values.append(("orderId", order_id) if order_id else ("cliOrdId", str(client_order_id)))
        if size is not None:
            values.append(("size", self._decimal_string(size, "FUTURES_EDIT_SIZE_INVALID")))
        if limit_price is not None:
            values.append(("limitPrice", self._decimal_string(limit_price, "FUTURES_EDIT_LIMIT_PRICE_INVALID")))
        if stop_price is not None:
            values.append(("stopPrice", self._decimal_string(stop_price, "FUTURES_EDIT_STOP_PRICE_INVALID")))
        payload = self._post("/derivatives/api/v3/editorder", values)
        status = payload.get("editStatus")
        if payload.get("result") != "success" or not isinstance(status, dict):
            raise FuturesAPIError("FUTURES_EDIT_RESPONSE_INVALID")
        return status

    def get_open_orders(self) -> dict[str, Any]:
        return self._readonly.get_open_orders()

    def get_open_positions(self) -> dict[str, Any]:
        return self._readonly.get_open_positions()

    def get_fills(self) -> dict[str, Any]:
        return self._readonly.get_fills()


def derive_crypto_linear_usd_perpetual_symbols(
    futures_instruments: Iterable[dict[str, Any]],
    *,
    spot_usd_base_assets: Iterable[str],
) -> set[str]:
    """Intersect active linear perpetuals with Kraken Spot USD crypto markets.

    spot_usd_base_assets must be derived from the current Spot AssetPairs response.
    The intersection excludes xStocks, commodities, indices, and instruments that
    do not have a corresponding crypto Spot USD market.
    """
    spot_bases = {str(value).strip().upper() for value in spot_usd_base_assets}
    spot_bases.discard("")
    allowed: set[str] = set()
    for row in futures_instruments:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).strip().upper()
        match = _SYMBOL_RE.fullmatch(symbol)
        if match is None or row.get("tradeable") is not True:
            continue
        base = match.group(1)
        if base in spot_bases and base not in _NON_CRYPTO_BASES:
            allowed.add(symbol)
    return allowed


class FuturesDemoExecutionAdapter:
    """Demo-only order authority with durable decision idempotency and no blind retry."""

    def __init__(
        self,
        *,
        client: FuturesDemoTradingClient,
        persistence: Any,
        allowed_symbols: Iterable[str],
        enabled: bool = False,
    ) -> None:
        self.client = client
        self.persistence = persistence
        self.allowed_symbols = {str(value).upper() for value in allowed_symbols}
        self.enabled = bool(enabled)
        if not self.allowed_symbols:
            raise ValueError("FUTURES_DEMO_ALLOWED_SYMBOLS_REQUIRED")
        if any(not _SYMBOL_RE.fullmatch(symbol) for symbol in self.allowed_symbols):
            raise ValueError("FUTURES_DEMO_SYMBOL_ALLOWLIST_INVALID")

    def _submit(
        self, request: FuturesOrderRequest, *, risk_result: FuturesRiskResult | None
    ) -> dict[str, Any]:
        if not self.enabled:
            raise FuturesTradingError("FUTURES_DEMO_TRADING_DISABLED")
        if request.symbol.upper() not in self.allowed_symbols:
            raise FuturesTradingError("FUTURES_SYMBOL_NOT_IN_APPROVED_CRYPTO_UNIVERSE")
        if request.reduce_only is False:
            if risk_result is None or not risk_result.allowed:
                raise FuturesTradingError("FUTURES_ENTRY_RISK_GATE_BLOCKED")
            if (
                not isinstance(request.notional_usd, Decimal)
                or not request.notional_usd.is_finite()
                or request.notional_usd <= 0
                or request.notional_usd > risk_result.proposed_notional_usd
            ):
                raise FuturesTradingError("FUTURES_ENTRY_NOTIONAL_EXCEEDS_RISK_APPROVAL")

        intent_id = uuid.uuid4().hex
        client_id = request.client_order_id or uuid.uuid4().hex
        stored_request = FuturesOrderRequest(
            decision_id=request.decision_id,
            symbol=request.symbol.upper(),
            side=request.side.lower(),
            size=request.size,
            order_type=request.order_type,
            reduce_only=request.reduce_only,
            notional_usd=request.notional_usd,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            trigger_signal=request.trigger_signal,
            client_order_id=client_id,
        )
        created = self.persistence.create_futures_order_intent({
            "intent_id": intent_id,
            "decision_id": request.decision_id,
            "client_order_id": client_id,
            "symbol": stored_request.symbol,
            "side": stored_request.side,
            "size": str(stored_request.size),
            "order_type": stored_request.order_type,
            "reduce_only": stored_request.reduce_only,
            "request": {
                "decision_id": request.decision_id,
                "symbol": stored_request.symbol,
                "side": stored_request.side,
                "size": str(stored_request.size),
                "order_type": stored_request.order_type,
                "reduce_only": stored_request.reduce_only,
                "notional_usd": str(stored_request.notional_usd) if stored_request.notional_usd is not None else None,
                "limit_price": str(stored_request.limit_price) if stored_request.limit_price is not None else None,
                "stop_price": str(stored_request.stop_price) if stored_request.stop_price is not None else None,
                "trigger_signal": stored_request.trigger_signal,
                "client_order_id": client_id,
            },
        })
        if not created:
            raise FuturesTradingError("FUTURES_DECISION_OR_CLIENT_ID_ALREADY_RESERVED")
        self.persistence.transition_futures_order_intent(intent_id, "PRECHECK_PASSED")
        self.persistence.transition_futures_order_intent(intent_id, "SUBMITTING")
        try:
            acknowledgement = self.client.send_order(stored_request)
        except FuturesTradingError as exc:
            self.persistence.transition_futures_order_intent(
                intent_id, "REJECTED", {"error_code": exc.code}
            )
            raise
        except FuturesAPIError as exc:
            if exc.code.startswith("FUTURES_API_") or exc.code.startswith("FUTURES_HTTP_STATUS_4"):
                self.persistence.transition_futures_order_intent(
                    intent_id, "REJECTED", {"error_code": exc.code}
                )
                raise FuturesTradingError(exc.code) from None
            self.persistence.transition_futures_order_intent(
                intent_id, "UNKNOWN_RECONCILING", {"error_code": exc.code}
            )
            raise FuturesTradingError("FUTURES_SUBMIT_OUTCOME_UNKNOWN") from None
        except Exception:
            self.persistence.transition_futures_order_intent(
                intent_id, "UNKNOWN_RECONCILING", {"error_code": "UNEXPECTED_SUBMIT_ERROR"}
            )
            raise FuturesTradingError("FUTURES_SUBMIT_OUTCOME_UNKNOWN") from None

        self.persistence.transition_futures_order_intent(
            intent_id,
            "ACKNOWLEDGED",
            {
                "raw_status": acknowledgement.raw_status,
                "client_order_id": acknowledgement.client_order_id,
            },
            exchange_order_id=acknowledgement.exchange_order_id,
        )
        return {
            "intent_id": intent_id,
            "decision_id": request.decision_id,
            "client_order_id": acknowledgement.client_order_id,
            "exchange_order_id": acknowledgement.exchange_order_id,
            "state": "ACKNOWLEDGED",
            "fill_confirmed": False,
            "raw_status": acknowledgement.raw_status,
        }

    def submit_entry(
        self, request: FuturesOrderRequest, *, risk_result: FuturesRiskResult
    ) -> dict[str, Any]:
        if request.reduce_only is not False:
            raise FuturesTradingError("FUTURES_ENTRY_MUST_NOT_BE_REDUCE_ONLY")
        return self._submit(request, risk_result=risk_result)

    def submit_protective_stop(
        self,
        *,
        decision_id: str,
        symbol: str,
        position_side: str,
        size: Decimal,
        open_position_size: Decimal,
        mark_price: Decimal,
        stop_price: Decimal,
    ) -> dict[str, Any]:
        direction = position_side.upper()
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("FUTURES_POSITION_DIRECTION_INVALID")
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value <= 0
            for value in (size, open_position_size, mark_price, stop_price)
        ):
            raise ValueError("FUTURES_PROTECTIVE_STOP_INPUT_INVALID")
        if size > open_position_size:
            raise ValueError("FUTURES_PROTECTIVE_STOP_EXCEEDS_OPEN_POSITION")
        if direction == "LONG":
            if stop_price >= mark_price:
                raise ValueError("FUTURES_LONG_STOP_MUST_BE_BELOW_MARK")
            side = "sell"
        else:
            if stop_price <= mark_price:
                raise ValueError("FUTURES_SHORT_STOP_MUST_BE_ABOVE_MARK")
            side = "buy"
        request = FuturesOrderRequest(
            decision_id=decision_id,
            symbol=symbol,
            side=side,
            size=size,
            order_type="stp",
            reduce_only=True,
            stop_price=stop_price,
            trigger_signal="mark",
        )
        return self._submit(request, risk_result=None)

    def cancel_intent(self, intent_id: str) -> dict[str, Any]:
        stored = self.persistence.futures_order_intent(intent_id)
        if stored is None:
            raise FuturesTradingError("FUTURES_INTENT_NOT_FOUND")
        status = self.client.cancel_order(
            order_id=stored.get("exchange_order_id"),
            client_order_id=stored.get("client_order_id") if not stored.get("exchange_order_id") else None,
        )
        raw_status = str(status.get("status", "unknown"))
        if raw_status == "cancelled":
            self.persistence.transition_futures_order_intent(
                intent_id, "CANCELED", {"cancel_status": raw_status}
            )
            state = "CANCELED"
        else:
            self.persistence.transition_futures_order_intent(
                intent_id, "UNKNOWN_RECONCILING", {"cancel_status": raw_status}
            )
            state = "UNKNOWN_RECONCILING"
        return {"intent_id": intent_id, "state": state, "cancel_status": raw_status}

    def reconcile_intent(self, intent_id: str) -> dict[str, Any]:
        stored = self.persistence.futures_order_intent(intent_id)
        if stored is None:
            raise FuturesTradingError("FUTURES_INTENT_NOT_FOUND")
        open_orders = self.client.get_open_orders().get("openOrders")
        fills = self.client.get_fills().get("fills")
        positions = self.client.get_open_positions().get("openPositions")
        if not isinstance(open_orders, list) or not isinstance(fills, list) or not isinstance(positions, list):
            raise FuturesTradingError("FUTURES_RECONCILIATION_RESPONSE_INVALID")

        order_id = stored.get("exchange_order_id")
        client_id = stored.get("client_order_id")
        open_match = next(
            (
                row for row in open_orders
                if isinstance(row, dict)
                and (
                    (order_id and row.get("order_id") == order_id)
                    or (client_id and row.get("cliOrdId") == client_id)
                )
            ),
            None,
        )
        fill_rows = [
            row for row in fills
            if isinstance(row, dict)
            and (
                (order_id and row.get("order_id") == order_id)
                or (not order_id and client_id and row.get("cliOrdId") == client_id)
            )
        ]
        filled_size = sum(
            (
                D(str(row.get("size", "0")))
                for row in fill_rows
                if _finite_positive_or_zero(row.get("size"))
            ),
            D(0),
        )
        expected_size = D(str(stored["size"]))
        if open_match is not None:
            state = "PARTIALLY_FILLED" if filled_size > 0 else "OPEN"
        elif fill_rows and filled_size >= expected_size:
            state = "FILLED"
        elif fill_rows:
            state = "PARTIALLY_FILLED"
        else:
            state = "UNKNOWN_RECONCILING"

        current = str(stored["state"])
        if state != current:
            try:
                self.persistence.transition_futures_order_intent(
                    intent_id,
                    state,
                    {
                        "reconciliation": "CURRENT_OPEN_ORDERS_FILLS_POSITIONS",
                        "matched_open_order": open_match is not None,
                        "matched_fill_count": len(fill_rows),
                        "filled_size": str(filled_size),
                        "position_snapshot_count": len(positions),
                    },
                )
            except ValueError:
                state = current
        return {
            "intent_id": intent_id,
            "state": state,
            "matched_open_order": open_match is not None,
            "matched_fill_count": len(fill_rows),
            "filled_size": str(filled_size),
            "position_snapshot_count": len(positions),
            "fill_confirmed": bool(fill_rows),
        }


def _finite_positive_or_zero(value: Any) -> bool:
    try:
        parsed = D(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return False
    return parsed.is_finite() and parsed >= 0
