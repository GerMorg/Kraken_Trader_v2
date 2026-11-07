from decimal import Decimal as D
from app.microstructure_research import match_microstructure_target_horizons

def bar(ts,mid="101"):
 return {"quality_status":"VALID","book_sample_count":3,"first_book_event_ts":ts-20,"last_book_event_ts":ts,
 "checksum_checks":3,"checksum_failures":0,"trade_id_gaps":0,"connection_gap_count":0,
 "quality_flags":[],"midprice_usd":mid,"spread_bps":"2"}

def test_matches_valid_near_target_mark_and_calculates_spot_cost_proxy():
 report=match_microstructure_target_horizons([{"observation_id":"x","pair":"XXBTZUSD","direction":"LONG",
 "decision_ts":1000,"target_epoch":2000,"entry_mid_price":"100","entry_spread_bps":"2","taker_fee_bps":"2"}],
 {"XBT/USD":[bar(1995)]},{"XXBTZUSD":"XBT/USD"},max_time_error_seconds=10)
 assert report["matched_observation_count"]==1
 assert report["matched_observations"][0]["target_time_offset_seconds"]==-5
 assert D(report["spot_net_cost_proxy"]["mean_bps"])==D("84")
 assert report["actual_fills"] is False

def test_target_match_never_uses_a_book_mark_after_the_target():
 report=match_microstructure_target_horizons([{"pair":"XBT/USD","direction":"LONG",
 "decision_ts":1000,"target_epoch":2000,"entry_mid_price":"100",
 "entry_spread_bps":"2","taker_fee_bps":"2"}],
 {"XBT/USD":[bar(1990,"101"),bar(2001,"120")]},{},max_time_error_seconds=30)
 assert report["matched_observation_count"]==1
 assert report["matched_observations"][0]["mark_event_ts"]==1990
 assert report["matched_observations"][0]["target_time_offset_seconds"]==-10
 assert D(report["gross_mark_return"]["mean_bps"]) == D("100")


def test_target_without_a_prior_qualified_book_mark_is_not_matched():
 report=match_microstructure_target_horizons([{"pair":"XBT/USD","direction":"LONG",
 "decision_ts":1000,"target_epoch":2000,"entry_mid_price":"100",
 "entry_spread_bps":"2","taker_fee_bps":"2"}],
 {"XBT/USD":[bar(2001,"101")]},{},max_time_error_seconds=30)
 assert report["matched_observation_count"]==0
 assert report["unmatched_reasons"]["TARGET_PRECEDES_FIRST_QUALIFIED_BOOK_MARK"]==1


def test_legacy_partial_and_distant_book_marks_are_rejected():
 legacy={"quality_status":"VALID","midprice_usd":"100","last_book_event_ts":2000}
 partial={**bar(2000),"quality_status":"PARTIAL"}
 report=match_microstructure_target_horizons([{"pair":"XXBTZUSD","direction":"SHORT","decision_ts":1000,
 "target_epoch":2000,"entry_mid_price":"100","entry_spread_bps":"2","taker_fee_bps":"2"}],
 {"XBT/USD":[legacy,partial,bar(2200)]},{"XXBTZUSD":"XBT/USD"},max_time_error_seconds=30)
 assert report["matched_observation_count"]==0
 assert report["unmatched_reasons"]["TARGET_PRECEDES_FIRST_QUALIFIED_BOOK_MARK"] == 1


def test_forward_exit_mark_must_not_precede_the_signal_decision():
 report=match_microstructure_target_horizons([{
  "candidate_id":"before-entry","pair":"XBT/USD","direction":"LONG",
  "decision_ts":1950,"target_epoch":2000,"entry_mid_price":"100",
  "entry_spread_bps":"2","taker_fee_bps":"2"
 }],{"XBT/USD":[bar(1900,"101")]},{},max_time_error_seconds=120)
 assert report["matched_observation_count"]==0
 assert report["unmatched_reasons"]["NO_POST_DECISION_BOOK_MARK_BEFORE_TARGET"]==1


def test_forward_report_exposes_actual_qualified_history_range():
 report=match_microstructure_target_horizons([{
  "candidate_id":"covered","pair":"XBT/USD","direction":"LONG",
  "decision_ts":1000,"target_epoch":2000,"entry_mid_price":"100",
  "entry_spread_bps":"2","taker_fee_bps":"2"
 }],{"XBT/USD":[bar(1500,"100.5"),bar(1995,"101")]},{},max_time_error_seconds=10)
 coverage=report["history_coverage_by_pair"]["XBT/USD"]
 assert coverage["qualified_book_mark_count"]==2
 assert coverage["first_qualified_book_event_ts"]==1500
 assert coverage["latest_qualified_book_event_ts"]==1995
 assert report["matched_observations"][0]["mark_event_ts"]==1995
