from decimal import Decimal

from app.market import margin_leverage_for,parse_ohlc_rows,parse_tickers,usd_crypto_universe,volume_for_notional_usd

def test_market_universe_allows_only_online_usd_crypto_pair():
    raw={
      "XBTUSD":{"altname":"XBTUSD","wsname":"XBT/USD","aclass_base":"currency","base":"XXBT","quote":"ZUSD","status":"online",
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
      "ordermin":"0.1","lot_decimals":2,"pair_decimals":2,"aclass_base":"currency","leverage_buy":[4,5],"leverage_sell":[]}})[0]
    assert margin_leverage_for(instrument,"buy",3) is None
    assert margin_leverage_for(instrument,"sell",3) is None

def test_ohlc_parser_ignores_current_forming_candle():
    raw={"XBTUSD":[[1,"100","110","99","105","102","10",100],
                   [2,"105","112","104","110","108","12",110],
                   [3,"110","115","108","113","111","8",90]],"last":"3"}
    rows=parse_ohlc_rows(raw,"XBTUSD")
    assert len(rows)==2 and rows[-1]["close"]==Decimal("110")


def test_market_universe_fails_closed_for_unknown_asset_class():def test_market_universe_excludes_fiat_base_pairs_but_keeps_dynamic_crypto_usd_pairs():
    def pair(base, base_id, wsname):
        return {
            "altname": wsname.replace("/", ""),
            "wsname": wsname,
            "aclass_base": "currency",
            "base": base_id,
            "quote": "ZUSD",
            "status": "online",
            "ordermin": "0.001",
            "lot_decimals": 8,
            "pair_decimals": 2,
        }

    pairs = {
        "ZEURZUSD": pair("EUR", "ZEUR", "EUR/USD"),
        "ZGBPZUSD": pair("GBP", "ZGBP", "GBP/USD"),
        "ZCADZUSD": pair("CAD", "ZCAD", "CAD/USD"),
        "XBTZUSD": pair("XBT", "XXBT", "XBT/USD"),
        "NEWCOINZUSD": pair("NEWCOIN", "NEWCOIN", "NEWCOIN/USD"),
    }

    instruments = usd_crypto_universe(pairs)

    assert {item.wsname for item in instruments} == {"XBT/USD", "NEWCOIN/USD"}


def test_market_universe_rejects_fiat_asset_ids_even_when_symbol_is_an_alias():
    raw = {
        "FIATUSD": {
            "altname": "FIATUSD", "wsname": "TOK/USD", "aclass_base": "currency",
            "base": "ZGBP", "quote": "ZUSD", "status": "online",
            "ordermin": "1", "lot_decimals": 2, "pair_decimals": 2,
        },
    }
    assert usd_crypto_universe(raw) == []


def test_market_universe_fails_closed_for_unknown_asset_class():
    raw = {
        "UNKNOWNUSD": {"altname": "UNKNOWNUSD", "wsname": "UNKNOWN/USD",
                       "status": "online", "ordermin": "1",
                       "lot_decimals": 2, "pair_decimals": 2},
        "EQUITYUSD": {"altname": "EQUITYUSD", "wsname": "ABCx/USD",
                      "aclass_base": "tokenized_equity", "status": "online",
                      "ordermin": "1", "lot_decimals": 2, "pair_decimals": 2},
    }
    assert usd_crypto_universe(raw) == []


def test_volume_rounding_never_exceeds_requested_notional():
    volume = volume_for_notional_usd(Decimal("10"), Decimal("3"), 2)
    assert volume == Decimal("3.33")
    assert volume * Decimal("3") <= Decimal("10")


def test_volume_sizing_fails_closed_on_invalid_values():
    assert volume_for_notional_usd(Decimal("NaN"), Decimal("3"), 2) == Decimal("0")
    assert volume_for_notional_usd(Decimal("10"), Decimal("0"), 2) == Decimal("0")


def test_ticker_parser_ignores_malformed_empty_api_arrays():
    instrument = usd_crypto_universe({
        "XBTUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "aclass_base": "currency",
                   "status": "online", "ordermin": "0.001", "lot_decimals": 3,
                   "pair_decimals": 1},
    })[0]
    assert parse_tickers({"XBTUSD": {"c": [], "a": None, "b": [], "v": [], "p": []}},
                         [instrument]) == {}


def test_ohlc_parser_rejects_impossible_high_low_bars():
    raw = {
        "XBTUSD": [
            [1, "100", "99", "90", "105", "102", "10", 100],
            [2, "105", "112", "104", "110", "108", "12", 110],
            [3, "110", "115", "108", "113", "111", "8", 90],
        ],
        "last": "3",
    }
    rows = parse_ohlc_rows(raw, "XBTUSD")
    assert len(rows) == 1
    assert rows[0]["ts"] == 2


def test_ohlc_parser_rejects_ambiguous_alias_fallback_instead_of_using_first_market():
    from app.market import parse_ohlc_rows

    rows = [
        [1760000000, "100", "102", "99", "101", "0", "10"],
        [1760003600, "101", "103", "100", "102", "0", "11"],
    ]
    result = parse_ohlc_rows({"OTHERPAIR": rows, "THIRDPAIR": rows, "last": "cursor"}, "XBTUSD")
    assert result == []


def test_entry_sizing_reference_uses_ask_for_long_and_bid_for_short():
    from app.market import executable_reference_price
    from app.models import Ticker

    ticker = Ticker(
        "XBTUSD", Decimal("100"), Decimal("99"), Decimal("101"), Decimal("10000")
    )
    assert executable_reference_price(ticker, "LONG") == Decimal("101")
    assert executable_reference_price(ticker, "SHORT") == Decimal("99")
    assert executable_reference_price(ticker, "HOLD") == Decimal("0")


def test_entry_sizing_reference_fails_closed_on_invalid_quote():
    from app.market import executable_reference_price
    from app.models import Ticker

    invalid = Ticker(
        "XBTUSD", Decimal("100"), Decimal("102"), Decimal("101"), Decimal("10000")
    )
    assert executable_reference_price(invalid, "LONG") == Decimal("0")
    assert executable_reference_price(invalid, "SHORT") == Decimal("0")


def test_ticker_parser_rejects_missing_or_zero_24h_quote_turnover():
    instrument = usd_crypto_universe({
        "XBTUSD": {
            "altname": "XBTUSD", "wsname": "XBT/USD", "aclass_base": "currency",
            "status": "online", "ordermin": "0.001", "lot_decimals": 3,
            "pair_decimals": 1,
        },
    })[0]
    raw = {
        "XBTUSD": {
            "c": ["100", "100"], "a": ["101"], "b": ["99"],
            "v": ["0", "0"], "p": ["100", "100"],
        },
    }
    assert parse_tickers(raw, [instrument]) == {}


def test_quote_freshness_rejects_missing_future_nonfinite_and_stale_timestamps():
    from app.market import quote_is_fresh

    assert quote_is_fresh(100.0, 115.0, 15)
    assert not quote_is_fresh(100.0, 115.1, 15)
    assert not quote_is_fresh(None, 115.0, 15)
    assert not quote_is_fresh(116.0, 115.0, 15)
    assert not quote_is_fresh(float("nan"), 115.0, 15)
    assert not quote_is_fresh(100.0, 115.0, 0)
