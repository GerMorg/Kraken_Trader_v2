from app.persistence import Persistence

def test_quality_report_separates_multi_sample_valid_from_legacy_valid():
 db=Persistence(":memory:")
 base={"trade_count":1,"book_update_count":1,"checksum_checks":2,"checksum_failures":0,
 "trade_id_gaps":0,"connection_gap_count":0,"buy_notional_usd":"100","sell_notional_usd":"0",
 "signed_trade_notional_usd":"100","spread_bps":"2"}
 db.record_microstructure_bar({"pair":"XBT/USD","minute_ts":1800,"source":"KRAKEN_SPOT_WS_V2",
 "quality_status":"VALID",**base,"book_sample_count":2,"first_book_event_ts":1801,
 "last_book_event_ts":1820,"midprice_usd":"100","quality_flags":[]})
 db.record_microstructure_bar({"pair":"XBT/USD","minute_ts":1860,"source":"KRAKEN_SPOT_WS_V2",
 "quality_status":"VALID",**base,"quality_flags":[]})
 db.record_microstructure_bar({"pair":"ETH/USD","minute_ts":1800,"source":"KRAKEN_SPOT_WS_V2",
 "quality_status":"PARTIAL",**base,"quality_flags":["CONNECTION_GAP"]})
 report=db.microstructure_quality_summary(now_epoch=1900)
 assert report["total_bar_count"]==3
 assert report["status_counts"]=={"VALID":2,"PARTIAL":1}
 assert report["recent_usable_valid_bar_count"]==1
 assert report["recent_legacy_or_unqualified_valid_bar_count"]==1
 assert report["usable_quality_count_scope"]=="RECENT_WINDOW_ONLY"
 db.close()


def test_lifetime_counts_do_not_require_recent_json_quality_fields():
 db=Persistence(":memory:")
 base={"trade_count":0,"book_update_count":1,"checksum_checks":1,"checksum_failures":0,
       "trade_id_gaps":0,"connection_gap_count":0}
 db.record_microstructure_bar({"pair":"XBT/USD","minute_ts":1800,"source":"KRAKEN_SPOT_WS_V2",
       "quality_status":"VALID",**base,"book_sample_count":2,"first_book_event_ts":1801,
       "last_book_event_ts":1820,"midprice_usd":"100","quality_flags":[]})
 report=db.microstructure_quality_summary(now_epoch=100000,recent_window_minutes=1)
 assert report["total_bar_count"]==1
 assert report["status_counts"]=={"VALID":1,"PARTIAL":0}
 assert report["recent_status_counts"]=={"VALID":0,"PARTIAL":0}
 assert report["recent_usable_valid_bar_count"]==0
 db.close()
