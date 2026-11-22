from __future__ import annotations

import base64
import json
from decimal import Decimal as D
from urllib.request import Request

import pytest

from app.futures_api import build_futures_authent
from app.futures_trading import (
    FuturesDemoExecutionAdapter,
    FuturesDemoTradingClient,
    FuturesOrderRequest,
    FuturesRiskLimits,
    FuturesTradingError,
    derive_crypto_linear_usd_perpetual_symbols,
    evaluate_futures_entry,
    generate_futures_signal,
    parse_closed_hourly_closes,
)
from app.persistence import Persistence

TEST_SECRET = base64.b64encode(b"demo-secret").decode("ascii")
TEST_KEY = "demo-key"


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[Request] = []

    def __call__(self, request: Request, timeout: float):
        self.requests.append(request)
        if not self.responses:
            raise TimeoutError("no response")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return 200, json.dumps(response).encode("utf-8")


def accepted_response():
    return {"result": "success", "sendStatus": {"status": "placed", "order_id": "demo-order-1"}}


def allowed_risk():
    return evaluate_futures_entry(
        equity_usd=D("1000"),
        free_margin_usd=D("400"),
        current_margin_usd=D("0"),
        open_positions=0,
        notional_usd=D("150"),
        leverage=D("1.5"),
        stop_distance_pct=D("2"),
        estimated_round_trip_cost_bps=D("10"),
        estimated_funding_bps=D("5"),
        exchange_initial_margin_usd=D("100"),
        daily_pnl_pct=D("0"),
        drawdown_pct=D("0"),
        costs_complete=True,
        funding_complete=True,
        edge_validated=True,
        net_edge_bps=D("30"),
    )


def test_futures_signal_is_separate_and_rejects_non_linear_perpetual_symbols():
    prices = [D("100") * (D("1.003") ** index) for index in range(73)]
    valid = generate_futures_signal("PF_XBTUSD", prices)
    invalid = generate_futures_signal("PI_XBTUSD", prices)
    assert valid.direction == "LONG"
    assert valid.reason == "FUTURES_TREND_UP"
    assert invalid.direction == "NONE"
    assert invalid.reason == "NOT_LINEAR_USD_PERPETUAL"


def test_futures_risk_requires_calibrated_edge_cost_and_funding_evidence():
    blocked = evaluate_futures_entry(
        equity_usd=D("1000"), free_margin_usd=D("500"), current_margin_usd=D("0"),
        open_positions=0, notional_usd=D("100"), leverage=D("1.5"),
        stop_distance_pct=D("2"), estimated_round_trip_cost_bps=None,
        estimated_funding_bps=None, exchange_initial_margin_usd=None,
        daily_pnl_pct=D("0"), drawdown_pct=D("0"),
        costs_complete=False, funding_complete=False, edge_validated=False,
        net_edge_bps=None,
    )
    assert blocked.allowed is False
    assert "FUTURES_COSTS_INCOMPLETE" in blocked.reasons
    assert "FUTURES_FUNDING_INCOMPLETE" in blocked.reasons
    assert "FUTURES_EDGE_NOT_VALIDATED" in blocked.reasons


def test_futures_risk_caps_leverage_and_position_size():
    limits = FuturesRiskLimits()
    blocked = evaluate_futures_entry(
        equity_usd=D("1000"), free_margin_usd=D("900"), current_margin_usd=D("0"),
        open_positions=0, notional_usd=D("300"), leverage=D("3"),
        stop_distance_pct=D("1"), estimated_round_trip_cost_bps=D("5"),
        estimated_funding_bps=D("5"), exchange_initial_margin_usd=D("200"),
        daily_pnl_pct=D("0"), drawdown_pct=D("0"),
        costs_complete=True, funding_complete=True, edge_validated=True,
        net_edge_bps=D("100"), limits=limits,
    )
    assert blocked.allowed is False
    assert "FUTURES_LEVERAGE_OUTSIDE_PILOT_CAP" in blocked.reasons
    assert "FUTURES_POSITION_LIMIT" in blocked.reasons


def test_futures_demo_order_is_signed_with_exact_form_body():
    transport = FakeTransport([accepted_response()])
    client = FuturesDemoTradingClient(
        api_key=TEST_KEY, api_secret=TEST_SECRET, transport=transport
    )
    request = FuturesOrderRequest(
        "decision-1", "PF_XBTUSD", "buy", D("0.01"), "mkt", False
    )
    result = client.send_order(request)
    sent = transport.requests[0]
    body = sent.data.decode("ascii")
    assert result.status == "ACKNOWLEDGED"
    assert result.exchange_order_id == "demo-order-1"
    assert sent.get_method() == "POST"
    assert sent.full_url == "https://demo-futures.kraken.com/derivatives/api/v3/sendorder"
    assert sent.get_header("Apikey") == TEST_KEY
    assert sent.get_header("Authent") == build_futures_authent(
        TEST_SECRET, "/derivatives/api/v3/sendorder", post_data=body
    )
    assert "reduceOnly=false" in body


def test_demo_client_rejects_production_and_non_allowlisted_symbol():
    with pytest.raises(ValueError, match="FUTURES_TRADING_DEMO_ONLY"):
        FuturesDemoTradingClient(
            api_key=TEST_KEY, api_secret=TEST_SECRET, environment="production"
        )
    transport = FakeTransport([accepted_response()])
    client = FuturesDemoTradingClient(
        api_key=TEST_KEY, api_secret=TEST_SECRET, transport=transport
    )
    with pytest.raises(ValueError, match="FUTURES_SYMBOL_NOT_LINEAR"):
        client.send_order(FuturesOrderRequest(
            "d", "PI_XBTUSD", "buy", D("1"), "mkt", False
        ))
    assert transport.requests == []


def test_demo_adapter_persists_ack_and_blocks_duplicate_decisions():
    transport = FakeTransport([accepted_response()])
    client = FuturesDemoTradingClient(
        api_key=TEST_KEY, api_secret=TEST_SECRET, transport=transport
    )
    db = Persistence(":memory:")
    adapter = FuturesDemoExecutionAdapter(
        client=client, persistence=db, allowed_symbols={"PF_XBTUSD"}, enabled=True
    )
    request = FuturesOrderRequest(
        "decision-unique", "PF_XBTUSD", "buy", D("0.01"), "mkt", False,
        notional_usd=D("150")
    )
    result = adapter.submit_entry(request, risk_result=allowed_risk())
    assert result["state"] == "ACKNOWLEDGED"
    assert result["fill_confirmed"] is False
    stored = db.futures_order_intent(result["intent_id"])
    assert stored["state"] == "ACKNOWLEDGED"
    with pytest.raises(FuturesTradingError, match="FUTURES_DECISION_OR_CLIENT_ID_ALREADY_RESERVED"):
        adapter.submit_entry(request, risk_result=allowed_risk())
    assert len(transport.requests) == 1
    db.close()


def test_timeout_after_submit_becomes_unknown_without_retry():
    transport = FakeTransport([TimeoutError("timeout")])
    client = FuturesDemoTradingClient(
        api_key=TEST_KEY, api_secret=TEST_SECRET, transport=transport
    )
    db = Persistence(":memory:")
    adapter = FuturesDemoExecutionAdapter(
        client=client, persistence=db, allowed_symbols={"PF_XBTUSD"}, enabled=True
    )
    request = FuturesOrderRequest(
        "decision-timeout", "PF_XBTUSD", "buy", D("0.01"), "mkt", False,
        notional_usd=D("150")
    )
    with pytest.raises(FuturesTradingError, match="FUTURES_SUBMIT_OUTCOME_UNKNOWN"):
        adapter.submit_entry(request, risk_result=allowed_risk())
    unresolved = db.unresolved_futures_order_intents()
    assert len(unresolved) == 1
    assert unresolved[0]["state"] == "UNKNOWN_RECONCILING"
    assert len(transport.requests) == 1
    db.close()


def test_reduce_only_stop_uses_opposite_side_and_not_more_than_position():
    transport = FakeTransport([accepted_response()])
    client = FuturesDemoTradingClient(
        api_key=TEST_KEY, api_secret=TEST_SECRET, transport=transport
    )
    db = Persistence(":memory:")
    adapter = FuturesDemoExecutionAdapter(
        client=client, persistence=db, allowed_symbols={"PF_XBTUSD"}, enabled=True
    )
    result = adapter.submit_protective_stop(
        decision_id="stop-1",
        symbol="PF_XBTUSD",
        position_side="LONG",
        size=D("0.05"),
        open_position_size=D("0.05"),
        mark_price=D("100"),
        stop_price=D("95"),
    )
    body = transport.requests[0].data.decode("ascii")
    assert result["state"] == "ACKNOWLEDGED"
    assert "side=sell" in body
    assert "reduceOnly=true" in body
    assert "triggerSignal=mark" in body
    with pytest.raises(ValueError, match="FUTURES_PROTECTIVE_STOP_EXCEEDS"):
        adapter.submit_protective_stop(
            decision_id="stop-too-large", symbol="PF_XBTUSD",
            position_side="LONG", size=D("0.06"),
            open_position_size=D("0.05"), mark_price=D("100"), stop_price=D("95"),
        )
    db.close()



def test_futures_allowlist_intersects_current_crypto_spot_usd_assets():
    instruments = [
        {"symbol": "PF_XBTUSD", "tradeable": True},
        {"symbol": "PF_ETHUSD", "tradeable": True},
        {"symbol": "PF_AAPLXUSD", "tradeable": True},
        {"symbol": "PF_XAUUSD", "tradeable": True},
        {"symbol": "FF_XBTUSD_261225", "tradeable": True},
        {"symbol": "PF_XRPUSD", "tradeable": False},
    ]
    allowed = derive_crypto_linear_usd_perpetual_symbols(
        instruments,
        spot_usd_base_assets={"XBT", "ETH", "XRP", "AAPLX", "XAU"},
    )
    assert allowed == {"PF_XBTUSD", "PF_ETHUSD"}



def test_futures_risk_requires_exchange_initial_margin_from_current_tier():
    blocked = evaluate_futures_entry(
        equity_usd=D("1000"), free_margin_usd=D("500"), current_margin_usd=D("0"),
        open_positions=0, notional_usd=D("100"), leverage=D("1.5"),
        stop_distance_pct=D("2"), estimated_round_trip_cost_bps=D("5"),
        estimated_funding_bps=D("5"), exchange_initial_margin_usd=None,
        daily_pnl_pct=D("0"), drawdown_pct=D("0"), costs_complete=True,
        funding_complete=True, edge_validated=True, net_edge_bps=D("100"),
    )
    assert blocked.allowed is False
    assert "FUTURES_EXCHANGE_INITIAL_MARGIN_UNKNOWN" in blocked.reasons



def test_futures_market_order_immediate_fill_is_acknowledged_not_rejected():
    transport = FakeTransport([{
        "result": "success",
        "sendStatus": {"status": "filled", "order_id": "demo-order-filled"},
    }])
    client = FuturesDemoTradingClient(
        api_key=TEST_KEY, api_secret=TEST_SECRET, transport=transport
    )
    result = client.send_order(FuturesOrderRequest(
        "immediate-fill", "PF_XBTUSD", "buy", D("0.01"), "mkt", False
    ))
    assert result.status == "ACKNOWLEDGED"
    assert result.raw_status == "filled"
    assert result.exchange_order_id == "demo-order-filled"



def test_futures_risk_blocks_when_exchange_margin_tier_exceeds_available_collateral():
    blocked = evaluate_futures_entry(
        equity_usd=D("1000"), free_margin_usd=D("50"), current_margin_usd=D("0"),
        open_positions=0, notional_usd=D("150"), leverage=D("1.5"),
        stop_distance_pct=D("2"), estimated_round_trip_cost_bps=D("10"),
        estimated_funding_bps=D("5"), exchange_initial_margin_usd=D("100"),
        daily_pnl_pct=D("0"), drawdown_pct=D("0"), costs_complete=True,
        funding_complete=True, edge_validated=True, net_edge_bps=D("30"),
    )
    assert blocked.allowed is False
    assert "FUTURES_FREE_MARGIN_INSUFFICIENT" in blocked.reasons


def test_futures_hourly_parser_uses_only_closed_contiguous_recent_candles():
    now = D(str(int(__import__("time").time())))
    last_open = (int(now) // 3600) * 3600 - 3600
    candles = []
    for index in range(80):
        close = D("100") * (D("1.003") ** index)
        candles.append({
            "time": (last_open - (79 - index) * 3600) * 1000,
            "open": str(close),
            "high": str(close * D("1.001")),
            "low": str(close * D("0.999")),
            "close": str(close),
        })
    # The currently forming H1 candle must not enter the signal window.
    candles.append({
        "time": (last_open + 3600) * 1000,
        "open": "999",
        "high": "1000",
        "low": "998",
        "close": "999",
    })

    closes, last_bar_epoch, status = parse_closed_hourly_closes(
        candles,
        now_epoch_seconds=now,
    )

    assert status == "READY"
    assert len(closes) == 80
    assert last_bar_epoch == last_open
    assert generate_futures_signal("PF_XBTUSD", closes).direction == "LONG"


def test_futures_hourly_parser_fails_closed_on_gap_and_stale_history():
    now = D(str(int(__import__("time").time())))
    last_open = (int(now) // 3600) * 3600 - 3600
    candles = []
    for index in range(80):
        close = D("100") * (D("1.002") ** index)
        candles.append({
            "time": (last_open - (79 - index) * 3600) * 1000,
            "open": str(close),
            "high": str(close * D("1.001")),
            "low": str(close * D("0.999")),
            "close": str(close),
        })
    gap_candles = [row for i, row in enumerate(candles) if i != 30]
    _closes, _last, gap_status = parse_closed_hourly_closes(
        gap_candles,
        now_epoch_seconds=now,
    )
    stale_candles = [
        {**row, "time": int(row["time"]) - 3 * 3600 * 1000}
        for row in candles
    ]
    _closes, _last, stale_status = parse_closed_hourly_closes(
        stale_candles,
        now_epoch_seconds=now,
    )

    assert gap_status == "FUTURES_HOURLY_HISTORY_GAP_OR_INSUFFICIENT"
    assert stale_status == "FUTURES_CLOSED_CANDLES_STALE"


def test_futures_momentum_thresholds_are_configurable():
    rising = [D("100") * (D("1.003") ** index) for index in range(90)]
    signal = generate_futures_signal(
        "PF_XBTUSD",
        rising,
        fast_threshold_bps=D("20"),
        slow_threshold_bps=D("40"),
    )
    assert signal.direction == "LONG"


def test_invalid_futures_momentum_thresholds_fail_closed():
    rising = [D("100") * (D("1.003") ** index) for index in range(90)]
    signal = generate_futures_signal(
        "PF_XBTUSD",
        rising,
        fast_threshold_bps=D("NaN"),
    )
    assert signal.direction == "NONE"
    assert signal.reason == "INVALID_FUTURES_STRATEGY_THRESHOLDS"
