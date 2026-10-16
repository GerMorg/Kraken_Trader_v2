from decimal import Decimal

from app.config import Settings
from app.models import Instrument,RiskSnapshot,TrendSignal
from app.risk import evaluate_entry,size_position_usd

D=Decimal

def snapshot(**overrides):
    values=dict(equity_usd=D("1000"),free_collateral_usd=D("500"),used_margin_usd=D("0"),
        gross_exposure_usd=D("0"),net_exposure_usd=D("0"),open_positions=0,daily_pnl_pct=D("0"),
        drawdown_pct=D("0"),margin_level_pct=D("500"),snapshot_age_seconds=D("0"),
        complete=True,reconciliation_ok=True)
    values.update(overrides); return RiskSnapshot(**values)

def instrument():
    return Instrument("XBTUSD","XBTUSD","XBT/USD","XBT","USD","online",D("0.0001"),8,1,(2,3),(2,3))

def signal(direction="LONG"):
    sign = D("1") if direction == "LONG" else D("-1")
    return TrendSignal("XBTUSD",direction,D("200")*sign,D("300")*sign,D("0.7"),D("2"),
        "TREND_UP" if direction=="LONG" else "TREND_DOWN")

def evaluate(direction, account, notional=D("100")):
    return evaluate_entry(instrument=instrument(),signal=signal(direction),snapshot=account,
        settings=Settings(),cost_complete=True,estimated_net_edge_bps=D("10"),leverage=2,
        proposed_notional_usd=notional)

def test_position_size_respects_stop_risk_and_position_ceiling():
    size=size_position_usd(equity_usd=D("1000"),stop_distance_pct=D("2"),gap_buffer_pct=D("1"),
       cost_bps=D("50"),max_position_pct=D("20"),risk_budget_pct=D("0.5"),
       free_collateral_usd=D("300"),leverage=2)
    assert D(0)<size<=D("200")

def test_missing_reconciliation_or_costs_rejects_trade():
    result=evaluate_entry(instrument=instrument(),signal=signal(),snapshot=snapshot(reconciliation_ok=False),
       settings=Settings(),cost_complete=False,estimated_net_edge_bps=None,leverage=2,proposed_notional_usd=D("100"))
    assert not result.allowed
    assert "PORTFOLIO_RECONCILIATION_FAILED" in result.reasons
    assert "TOTAL_COSTS_UNVERIFIED" in result.reasons

def test_daily_loss_and_margin_unknown_are_explicit_rejections():
    result=evaluate_entry(instrument=instrument(),signal=signal(),
       snapshot=snapshot(daily_pnl_pct=D("-3"),margin_level_pct=None),settings=Settings(),
       cost_complete=True,estimated_net_edge_bps=D("10"),leverage=2,proposed_notional_usd=D("100"))
    assert "DAILY_LOSS_LIMIT" in result.reasons
    assert "MARGIN_LEVEL_UNKNOWN_OR_BELOW_APP_ENTRY_FLOOR" in result.reasons

def test_short_trade_reduces_existing_long_net_exposure():
    account=snapshot(gross_exposure_usd=D("450"),net_exposure_usd=D("450"),open_positions=1)
    result=evaluate("SHORT",account,D("100"))
    assert result.allowed
    assert "NET_EXPOSURE_LIMIT" not in result.reasons

def test_same_side_trade_cannot_push_net_exposure_over_limit():
    account=snapshot(gross_exposure_usd=D("450"),net_exposure_usd=D("450"),open_positions=1)
    result=evaluate("LONG",account,D("100"))
    assert not result.allowed
    assert "NET_EXPOSURE_LIMIT" in result.reasons

def test_non_finite_risk_inputs_fail_closed():
    result=evaluate("LONG",snapshot(equity_usd=D("NaN")),D("100"))
    assert not result.allowed
    assert "RISK_INPUT_NON_FINITE" in result.reasons

def test_position_size_rejects_non_finite_or_invalid_limits():
    assert size_position_usd(equity_usd=D("NaN"),stop_distance_pct=D("2"),
        gap_buffer_pct=D("1"),cost_bps=D("10"))==D(0)
    assert size_position_usd(equity_usd=D("1000"),stop_distance_pct=D("2"),
        gap_buffer_pct=D("1"),cost_bps=D("10"),leverage=0)==D(0)


def test_unknown_daily_pnl_and_drawdown_baselines_fail_closed():
    result = evaluate(
        "LONG",
        snapshot(daily_pnl_pct=None, drawdown_pct=None),
        D("100"),
    )
    assert not result.allowed
    assert "DAILY_PNL_BASELINE_UNKNOWN" in result.reasons
    assert "DRAWDOWN_BASELINE_UNKNOWN" in result.reasons
