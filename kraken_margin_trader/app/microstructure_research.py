from __future__ import annotations
from bisect import bisect_left
from decimal import Decimal
from statistics import median
from typing import Any, Mapping, Sequence
D=Decimal

def _d(value:Any)->Decimal|None:
    try: x=D(str(value))
    except (ArithmeticError,TypeError,ValueError): return None
    return x if x.is_finite() else None

def _metrics(values:Sequence[Decimal])->dict[str,Any]:
    if not values: return {"sample_count":0,"mean_bps":None,"median_bps":None,"profit_factor":None}
    wins=sum((v for v in values if v>0),D(0)); losses=-sum((v for v in values if v<0),D(0))
    return {"sample_count":len(values),"mean_bps":str(sum(values,D(0))/D(len(values))),
            "median_bps":str(D(str(median(values)))),
            "profit_factor":str(wins/losses) if losses else ("NO_LOSSES_IN_SAMPLE" if wins else None)}

def _mark(row:dict[str,Any])->tuple[int,Decimal,Decimal|None]|None:
    if str(row.get("quality_status","")).upper()!="VALID": return None
    try:
        first,last=int(row.get("first_book_event_ts",0)),int(row.get("last_book_event_ts",0))
        ok=(int(row.get("book_sample_count",0))>=2 and first>0 and last-first>=10
            and int(row.get("checksum_checks",0))>0 and int(row.get("checksum_failures",0))==0
            and int(row.get("trade_id_gaps",0))==0 and int(row.get("connection_gap_count",0))==0
            and not row.get("quality_flags"))
    except (TypeError,ValueError,OverflowError): return None
    mid,spread=_d(row.get("midprice_usd")),_d(row.get("spread_bps"))
    if not ok or mid is None or mid<=0 or (spread is not None and spread<0): return None
    return last,mid,spread

def match_microstructure_target_horizons(
 observations:Sequence[dict[str,Any]], bars_by_market_pair:Mapping[str, Sequence[dict[str, Any]]],
 pair_aliases:dict[str,str], *, max_time_error_seconds:int=60,
 slippage_bps_per_side:Decimal=D("5")
)->dict[str,Any]:
    """Match labels to qualified minute midpoints; this is not an executable fill."""
    if max_time_error_seconds<0 or not slippage_bps_per_side.is_finite() or slippage_bps_per_side<0:
        raise ValueError("MICROSTRUCTURE_TARGET_INPUT_INVALID")
    indexed: dict[str, tuple[list[int], list[tuple[int, Decimal, Decimal | None]]]] = {}
    for pair,bars in bars_by_market_pair.items():
        marks=sorted((m for b in bars if (m:=_mark(b)) is not None),key=lambda x:x[0])
        indexed[pair.strip().upper()]=([m[0] for m in marks],marks)
    matched: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    gross: list[Decimal] = []
    net: list[Decimal] = []
    offsets: list[int] = []
    directional = costed = 0
    for row in observations:
        direction=str(row.get("direction","")).upper()
        if direction not in {"LONG","SHORT"}: continue
        directional+=1
        src=str(row.get("pair","")).strip(); pair=str(pair_aliases.get(src,src)).strip().upper()
        try: decision,target=int(row["decision_ts"]),int(row["target_epoch"])
        except (KeyError,TypeError,ValueError,OverflowError):
            reasons["INVALID_TARGET_TIMESTAMP"]=reasons.get("INVALID_TARGET_TIMESTAMP",0)+1; continue
        entry=_d(row.get("entry_mid_price"))
        if decision<=0 or target<=decision or entry is None or entry<=0:
            reasons["INVALID_ENTRY_OR_TARGET"]=reasons.get("INVALID_ENTRY_OR_TARGET",0)+1; continue
        if pair not in indexed:
            reasons["NO_MICROSTRUCTURE_PAIR_HISTORY"]=reasons.get("NO_MICROSTRUCTURE_PAIR_HISTORY",0)+1; continue
        ts,marks=indexed[pair]
        if not ts:
            reasons["NO_QUALIFIED_VALID_BOOK_MARKS"]=reasons.get("NO_QUALIFIED_VALID_BOOK_MARKS",0)+1; continue
        pos=bisect_left(ts,target)
        nearby=([marks[pos]] if pos<len(marks) else [])+([marks[pos-1]] if pos>0 else [])
        mt,exit_mid,exit_spread=min(nearby,key=lambda x:abs(x[0]-target))
        offset=mt-target
        if abs(offset)>max_time_error_seconds:
            reasons["BOOK_MARK_OUTSIDE_TARGET_TOLERANCE"]=reasons.get("BOOK_MARK_OUTSIDE_TARGET_TOLERANCE",0)+1; continue
        sign=D(1) if direction=="LONG" else D(-1)
        gr=sign*(exit_mid/entry-D(1))*D(10000)
        fee,entry_spread=_d(row.get("taker_fee_bps")),_d(row.get("entry_spread_bps"))
        nr=None
        if fee is not None and fee>=0 and entry_spread is not None and entry_spread>=0 and exit_spread is not None:
            nr=gr-(D(2)*fee+(entry_spread+exit_spread)/D(2)+D(2)*slippage_bps_per_side)
            net.append(nr); costed+=1
        gross.append(gr); offsets.append(offset)
        matched.append({"candidate_id":str(row.get("candidate_id",row.get("observation_id",""))),
            "candidate_name":str(row.get("candidate_name",row.get("strategy_version","unknown"))),
            "candidate_version":str(row.get("candidate_version",row.get("strategy_version","unknown"))),
            "pair":pair,"direction":direction,"decision_ts":decision,"target_epoch":target,
            "mark_event_ts":mt,"target_time_offset_seconds":offset,"gross_return_bps":str(gr),
            "spot_net_return_bps":str(nr) if nr is not None else None,"actual_fill":False,"portfolio_pnl":False})
    groups: dict[str, dict[str, list[Decimal]]] = {}
    for row in matched:
        key=f"{row['candidate_name']}:{row['candidate_version']}"
        grp=groups.setdefault(key,{"gross":[],"net":[]})
        grp["gross"].append(D(str(row["gross_return_bps"])))
        if row["spot_net_return_bps"] is not None: grp["net"].append(D(str(row["spot_net_return_bps"])))
    abs_offsets=[abs(x) for x in offsets]
    return {"status":"MINUTE_BOOK_MARKS_AVAILABLE" if matched else "WAITING_FOR_QUALIFIED_MINUTE_BOOK_MARKS",
        "method":"NEAREST_VALID_CHECKSUM_BOOK_MARK_WITHIN_TARGET_TOLERANCE",
        "target_tolerance_seconds":max_time_error_seconds,
        "slippage_bps_per_side_assumption":str(slippage_bps_per_side),
        "directional_observation_count":directional,"matched_observation_count":len(matched),
        "unmatched_observation_count":directional-len(matched),
        "match_coverage_pct":str(D(len(matched))/D(directional)*100) if directional else None,
        "gross_mark_return":_metrics(gross),"spot_net_cost_proxy":_metrics(net),"costed_match_count":costed,
        "target_time_error_seconds":{"mean_absolute":str(D(sum(abs_offsets))/D(len(abs_offsets))) if abs_offsets else None,
          "median_absolute":str(D(str(median(abs_offsets)))) if abs_offsets else None,
          "maximum_absolute":max(abs_offsets) if abs_offsets else None},
        "by_candidate":{k:{"gross":_metrics(v["gross"]),"spot_net_cost_proxy":_metrics(v["net"])} for k,v in groups.items()},
        "unmatched_reasons":reasons,"matched_observations":matched,
        "actual_fills":False,"historical_order_book_walk_simulated":False,
        "portfolio_pnl":False,"release_gate_eligible":False,
        "limitations":["Midpoint is a mark, not an executable fill; event-time book walk/queue priority is unavailable.",
          "Nearest minute mark may be before or after target within the stated tolerance.",
          "Spot costs require fee/spread inputs and assume fixed slippage; margin costs are unknown and omitted."]}

