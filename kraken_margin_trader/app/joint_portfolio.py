from __future__ import annotations
from collections import Counter,defaultdict
from decimal import Decimal
from typing import Any,Sequence
D=Decimal

def _scenario(rows:Sequence[dict[str,Any]],equity0:Decimal,budget:Decimal,
 max_gross:Decimal,max_net:Decimal,max_pos:Decimal,max_open:int)->dict[str,Any]:
    groups:dict[tuple[str,int],list[dict[str,Any]]]=defaultdict(list); invalid=0
    for row in rows:
        try:
            pair=str(row["pair"]).upper(); decision=int(row["decision_ts"]); mark=int(row["mark_event_ts"])
            direction=str(row["direction"]).upper(); ret=D(str(row["spot_net_return_bps"]))
        except (KeyError,TypeError,ValueError,ArithmeticError,OverflowError):
            invalid+=1; continue
        if not pair or decision<=0 or mark<=decision or direction not in {"LONG","SHORT"} or not ret.is_finite() or not row.get("candidate_id"):
            invalid+=1; continue
        groups[(pair,decision)].append({**row,"_net":ret})
    collisions={k for k,v in groups.items() if len(v)>1}
    excluded=sum(len(groups[k]) for k in collisions)
    events=[]; eligible=0
    for key,vals in groups.items():
        if key in collisions: continue
        row=vals[0]; cid=str(row["candidate_id"])
        events.extend([(int(row["decision_ts"]),1,cid,row),(int(row["mark_event_ts"]),0,cid,row)])
        eligible+=1
    events.sort(key=lambda x:(x[0],x[1],x[2]))
    equity=peak=equity0; gross_exp=net_exp=D(0); positions={}; pairs=set(); pnl_rows=[]
    skipped:Counter[str]=Counter(); max_dd=D(0); accepted=0
    for _,kind,cid,row in events:
        if kind==0:
            pos=positions.pop(cid,None)
            if pos is None: continue
            pnl=pos["notional"]*pos["ret"]/D(10000); equity+=pnl; pnl_rows.append(pnl)
            gross_exp-=pos["notional"]; net_exp-=pos["signed"]; pairs.discard(pos["pair"])
            peak=max(peak,equity)
            if peak>0: max_dd=max(max_dd,(peak-equity)/peak*100)
            continue
        pair=str(row["pair"]).upper()
        if pair in pairs: skipped["PAIR_ALREADY_EXPOSED"]+=1; continue
        if len(positions)>=max_open: skipped["MAX_OPEN_POSITIONS"]+=1; continue
        if equity<=0: skipped["NONPOSITIVE_EQUITY"]+=1; continue
        direction=str(row["direction"]).upper(); sign=D(1) if direction=="LONG" else D(-1)
        notional=min(equity*max_pos/100,equity*max_gross/100-gross_exp,equity*max_net/100-sign*net_exp)
        if notional<=0: skipped["PORTFOLIO_EXPOSURE_LIMIT"]+=1; continue
        ret=row["_net"]-(budget if direction=="SHORT" else D(0)); signed=sign*notional
        positions[cid]={"pair":pair,"notional":notional,"signed":signed,"ret":ret}
        pairs.add(pair); gross_exp+=notional; net_exp+=signed; accepted+=1
    wins=sum((x for x in pnl_rows if x>0),D(0)); losses=-sum((x for x in pnl_rows if x<0),D(0))
    return {"status":"SIMULATED_SHARED_EQUITY_DIAGNOSTIC" if accepted else "NO_ELIGIBLE_NONCOLLIDING_TRADES",
      "initial_equity_usd":str(equity0),"final_realized_equity_usd":str(equity),
      "realized_return_pct":str((equity/equity0-D(1))*100),"max_realized_close_to_close_drawdown_pct":str(max_dd),
      "profit_factor_on_realized_pnl":str(wins/losses) if losses else ("NO_REALIZED_LOSSES" if wins else None),
      "accepted_trade_count":accepted,"eligible_noncolliding_candidate_count":eligible,
      "same_pair_same_decision_collisions_excluded":excluded,"invalid_candidate_rows":invalid,
      "skipped_by_reason":dict(skipped),"open_positions_at_end":len(positions),"max_open_positions":max_open,
      "max_gross_exposure_pct":str(max_gross),"max_absolute_net_exposure_pct":str(max_net),
      "max_position_pct":str(max_pos),"short_margin_fee_budget_bps_round_trip_assumption":str(budget),
      "actual_margin_fee_evidence_available":False,"actual_fills":False,"actual_portfolio_pnl":False,
      "simulated_portfolio_pnl":True,"release_gate_eligible":False,
      "limitations":["Candidate labels are observations, not historical executed fills.",
       "Same-pair candidate conflicts at one decision time are all excluded.",
       "One shared equity, pair lock and gross/net/position-count caps are simulated.",
       "No intratrade mark-to-market, liquidation, pair-correlation or borrow-availability model.",
       "Short margin fee budget is a sensitivity assumption, not a Kraken quote."]}

def simulate_shared_equity_candidate_portfolio(
 matched_observations:Sequence[dict[str,Any]],*,initial_equity_usd:Decimal=D("10000"),
 max_gross_pct:Decimal=D("80"),max_net_pct:Decimal=D("50"),max_position_pct:Decimal=D("20"),
 max_open_positions:int=4,margin_fee_budgets_bps:Sequence[Decimal]=(D("5"),D("10"),D("20"),D("40"))
)->dict[str,Any]:
    vals=(initial_equity_usd,max_gross_pct,max_net_pct,max_position_pct)
    if any(not x.is_finite() for x in vals): raise ValueError("JOINT_PORTFOLIO_INPUT_NONFINITE")
    if initial_equity_usd<=0 or not D(0)<max_gross_pct<=100 or not D(0)<max_net_pct<=max_gross_pct or not D(0)<max_position_pct<=max_gross_pct or max_open_positions<1:
        raise ValueError("JOINT_PORTFOLIO_LIMITS_INVALID")
    scenarios=[]
    for budget in margin_fee_budgets_bps:
        if not budget.is_finite() or budget<0: raise ValueError("JOINT_PORTFOLIO_MARGIN_BUDGET_INVALID")
        scenarios.append({"margin_fee_budget_bps_round_trip":str(budget),
          "results":_scenario(matched_observations,initial_equity_usd,budget,max_gross_pct,max_net_pct,max_position_pct,max_open_positions),
          "status":"ASSUMED_MARGIN_COST_SCENARIO_NOT_KRAKEN_QUOTE"})
    return {"status":"SHARED_EQUITY_SCENARIOS_AVAILABLE" if scenarios else "NO_SCENARIOS",
      "method":"EVENT_DRIVEN_SHARED_EQUITY_LABEL_SIMULATION","candidate_observation_count":len(matched_observations),
      "initial_equity_is_normalized_research_capital":True,"scenarios":scenarios,
      "actual_fills":False,"actual_portfolio_pnl":False,"simulated_portfolio_pnl":True,"release_gate_eligible":False}
