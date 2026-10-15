import io
from decimal import Decimal

from app.audit import AuditLogger
from app.config import Settings
from app.persistence import Persistence
from app.runtime import TraderRuntime, _open_position_rows


D = Decimal

def test_open_positions_parser_accepts_kraken_direct_result_shape():
    raw={"TX1":{"pair":"XXBTZUSD","type":"sell","posstatus":"open","vol":"1","vol_closed":"0"},
         "TX2":{"pair":"XETHZUSD","type":"buy","posstatus":"open","vol":"2","vol_closed":"1"}}
    rows=_open_position_rows(raw)
    assert set(rows)=={"TX1","TX2"}

def test_open_positions_parser_accepts_wrapped_shape_and_ignores_metadata():
    raw={"open":{"TX1":{"pair":"XXBTZUSD","type":"buy","posstatus":"open","vol":"1"}},"last":"abc"}
    rows=_open_position_rows(raw)
    assert list(rows)==["TX1"]


def make_runtime():
    db = Persistence(":memory:")
    audit = AuditLogger(path="/unwritable-test-path/events.jsonl", stream=io.StringIO())
    return TraderRuntime(Settings(), db, audit), db, audit


def account_snapshot(positions):
    return {
        "trade_balance": {"e": "1000", "mf": "500", "m": "100", "ml": "450"},
        "open_positions": {"open": positions},
        "open_orders": {"open": {}},
        "balance": {"ZUSD": "1000"},
    }


def test_risk_snapshot_uses_absolute_gross_and_directional_net_exposure():
    runtime, db, audit = make_runtime()
    snapshot = runtime._account_risk_snapshot(account_snapshot({
        "LONG": {"pair": "XBTUSD", "type": "buy", "value": "100",
                 "vol": "1", "posstatus": "open"},
        "SHORT": {"pair": "ETHUSD", "type": "sell", "value": "-30",
                  "vol": "2", "posstatus": "open"},
    }))
    assert snapshot.gross_exposure_usd == D("130")
    assert snapshot.net_exposure_usd == D("70")
    assert snapshot.complete
    assert not snapshot.reconciliation_ok
    audit.close()
    db.close()


def test_risk_snapshot_is_incomplete_when_position_value_is_missing():
    runtime, db, audit = make_runtime()
    snapshot = runtime._account_risk_snapshot(account_snapshot({
        "SHORT": {"pair": "ETHUSD", "type": "sell",
                  "vol": "2", "posstatus": "open"},
    }))
    assert not snapshot.complete
    assert not snapshot.reconciliation_ok
    audit.close()
    db.close()
