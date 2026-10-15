from decimal import Decimal

from app.market import margin_leverage_for,parse_ohlc_rows,usd_crypto_universe

def test_market_universe_allows_only_online_usd_crypto_pair():
    raw={
      "XBTUSD":{"altname":"XBTUSD","wsname":"XBT/USD","base":"XXBT","quote":"ZUSD","status":"online",
        "ordermin":"0.0001","lot_decimals":8,"pair_decimals":1,"leverage_buy":[2,3,4],"leverage_sell":[2,3,4]},
      "XBTEUR":{"altname":"XBTEUR","wsname":"XBT/EUR","status":"online","ordermin":"0.0001","lot_decimals":8,"pair_decimals":1},
      "TOKEN":{"altname":"TOKEN","wsname":"ABC/USD","aclass_base":"tokenized_asset","status":"online",
        "ordermin":"1","lot_decimals":2,"pair_decimals":2},
      "OFFLINE":{"altname":"OFFLINE","wsname":"ABC/USD","status":"cancel_only","ordermin":"1","lot_decimals":2,"pair_decimals":2}}
    instruments=usd_crypto_universe(raw)
    assert [i.wsname for i in instruments]==["XBT/USD"]
    assert margin_leverage_for(instruments[0],"buy",3)==2

def test_does_not_assume_unsupported_maximum_leverage():
    instrument=usd_crypto_universe({"AUSD":{"altname":"AUSD","wsname":"A/USD","status":"online",
      "ordermin":"0.1","lot_decimals":2,"pair_decimals":2,"leverage_buy":[4,5],"leverage_sell":[]}})[0]
    assert margin_leverage_for(instrument,"buy",3) is None
    assert margin_leverage_for(instrument,"sell",3) is None

def test_ohlc_parser_ignores_current_forming_candle():
    raw={"XBTUSD":[[1,"100","110","99","105","102","10",100],
                   [2,"105","112","104","110","108","12",110],
                   [3,"110","115","108","113","111","8",90]],"last":"3"}
    rows=parse_ohlc_rows(raw,"XBTUSD")
    assert len(rows)==2 and rows[-1]["close"]==Decimal("110")
