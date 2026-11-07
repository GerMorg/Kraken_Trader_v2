from decimal import Decimal as D
import pytest
from app.joint_portfolio import simulate_shared_equity_candidate_portfolio

def row(cid,pair,direction,decision,exit_ts,ret="100"):
 return {"candidate_id":cid,"pair":pair,"direction":direction,"decision_ts":decision,"mark_event_ts":exit_ts,"spot_net_return_bps":ret}

def test_shared_equity_portfolio_respects_position_cap():
 report=simulate_shared_equity_candidate_portfolio([
 row("a","XBT/USD","LONG",100,200),row("b","ETH/USD","SHORT",100,210),
 row("c","SOL/USD","LONG",110,220),row("d","XRP/USD","LONG",120,230),
 row("e","NEAR/USD","LONG",130,240)],initial_equity_usd=D("1000"),margin_fee_budgets_bps=[D("10")])
 result=report["scenarios"][0]["results"]
 assert result["accepted_trade_count"]==4
 assert result["skipped_by_reason"]["MAX_OPEN_POSITIONS"]==1
 assert D(result["final_realized_equity_usd"])>D("1000")
 assert result["actual_portfolio_pnl"] is False

def test_same_pair_candidate_collision_excludes_both():
 result=simulate_shared_equity_candidate_portfolio([
 row("one","XBT/USD","LONG",100,200),row("two","XBT/USD","SHORT",100,200)],
 margin_fee_budgets_bps=[D("5")])["scenarios"][0]["results"]
 assert result["accepted_trade_count"]==0
 assert result["same_pair_same_decision_collisions_excluded"]==2

def test_invalid_shared_portfolio_caps_are_rejected():
 with pytest.raises(ValueError,match="JOINT_PORTFOLIO_LIMITS_INVALID"):
  simulate_shared_equity_candidate_portfolio([],max_gross_pct=D("80"),max_net_pct=D("90"))
