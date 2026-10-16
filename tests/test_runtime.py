import io
import time
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


def test_exchange_order_parser_requires_valid_order_mapping():
    from app.runtime import _exchange_order_rows

    parsed = _exchange_order_rows({
        "open": {"O1": {"status": "open"}, "O2": {"status": "pending"}},
    }, "open")
    assert parsed == {"O1": {"status": "open"}, "O2": {"status": "pending"}}
    assert _exchange_order_rows({"open": []}, "open") is None
    assert _exchange_order_rows({"open": {"": {"status": "open"}}}, "open") is None
    assert _exchange_order_rows({"open": {"O1": None}}, "open") is None


def make_runtime():
    db = Persistence(":memory:")
    audit = AuditLogger(path="/unwritable-test-path/events.jsonl", stream=io.StringIO())
    return TraderRuntime(Settings(), db, audit), db, audit


def account_snapshot(positions):
    return {
        "captured_at": time.time(),
        "trade_balance": {"e": "1000", "mf": "500", "m": "100", "ml": "450"},
        "open_positions": {"open": positions},
        "open_orders": {"open": {}},
        "closed_orders": {"closed": {}},
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
    assert snapshot.daily_pnl_pct is None
    assert snapshot.drawdown_pct is None
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


def test_zero_equity_is_not_replaced_by_unrealized_excluded_balance():
    runtime, db, audit = make_runtime()
    raw = account_snapshot({})
    raw["trade_balance"]["e"] = "0"
    raw["trade_balance"]["eb"] = "1000"
    snapshot = runtime._account_risk_snapshot(raw)
    assert snapshot.equity_usd == D("0")
    assert snapshot.complete
    audit.close()
    db.close()


def test_risk_snapshot_uses_actual_capture_time_and_rejects_stale_snapshot():
    runtime, db, audit = make_runtime()
    raw = account_snapshot({})
    raw["captured_at"] = time.time() - 3600
    snapshot = runtime._account_risk_snapshot(raw)
    assert snapshot.snapshot_age_seconds >= D("3600")
    assert snapshot.complete
    assert not snapshot.reconciliation_ok
    audit.close()
    db.close()


def test_risk_snapshot_rejects_missing_or_future_capture_timestamp():
    runtime, db, audit = make_runtime()
    missing = account_snapshot({})
    missing.pop("captured_at")
    assert not runtime._account_risk_snapshot(missing).complete

    future = account_snapshot({})
    future["captured_at"] = time.time() + 60
    future_snapshot = runtime._account_risk_snapshot(future)
    assert not future_snapshot.complete
    assert future_snapshot.snapshot_age_seconds < 0
    audit.close()
    db.close()


def test_non_finite_trade_balance_makes_snapshot_incomplete():
    runtime, db, audit = make_runtime()
    raw = account_snapshot({})
    raw["trade_balance"]["e"] = "NaN"
    snapshot = runtime._account_risk_snapshot(raw)
    assert not snapshot.complete
    assert snapshot.equity_usd == D("0")
    audit.close()
    db.close()


def test_runtime_refreshes_yearly_tax_report_without_claiming_complete_source_coverage(tmp_path):
    import json
    from datetime import datetime, timezone
    from app.tax import AustrianTaxReports

    runtime, db, audit = make_runtime()
    runtime.tax_reports = AustrianTaxReports(str(tmp_path))
    runtime._refresh_tax_report()
    year = datetime.now(timezone.utc).year
    report_path = tmp_path / str(year) / "income-tax-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["source_coverage_complete"] is False
    assert report["status"] == "INCOMPLETE_REQUIRES_MANUAL_REVIEW"

    db.record_tax_event({
        "event_id": "TAX-1", "ts": f"{year}-01-01T00:00:00Z", "year": year,
        "event_type": "MARGIN_EVENT", "verified": False, "manual_review": True,
    })
    runtime._refresh_tax_report()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["event_count"] == 1
    assert report["source_coverage_complete"] is False
    audit.close()
    db.close()


def test_runtime_generates_reports_for_all_imported_tax_years(tmp_path):
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.tax import AustrianTaxReports

    runtime, db, audit = make_runtime()
    runtime.tax_reports = AustrianTaxReports(str(tmp_path))
    current_year = datetime.now(ZoneInfo("Europe/Vienna")).year
    historical_year = current_year - 2

    for year in (historical_year, current_year):
        db.record_tax_event({
            "event_id": f"TAX-YEAR-{year}",
            "ts": f"{year}-01-01T12:00:00+00:00",
            "year": year,
            "event_type": "MARGIN_EVENT",
            "verified": False,
            "manual_review": True,
        })

    runtime._refresh_tax_report()

    for year in (historical_year, current_year):
        path = tmp_path / str(year) / "income-tax-report.json"
        assert path.is_file()
        report = json.loads(path.read_text(encoding="utf-8"))
        assert report["year"] == year
        assert report["event_count"] == 1
        assert report["source_coverage_complete"] is False

    # A new event in an already reported year must invalidate the cached signature.
    db.record_tax_event({
        "event_id": f"TAX-YEAR-{historical_year}-SECOND",
        "ts": f"{historical_year}-02-01T12:00:00+00:00",
        "year": historical_year,
        "event_type": "LEDGER_EVENT",
        "verified": False,
        "manual_review": True,
    })
    runtime._refresh_tax_report()
    historical_report = json.loads(
        (tmp_path / str(historical_year) / "income-tax-report.json").read_text(encoding="utf-8")
    )
    assert historical_report["event_count"] == 2
    assert historical_report["source_coverage_complete"] is False

    audit.close()
    db.close()


def test_tax_report_refresh_invalidates_when_existing_event_content_changes(tmp_path, monkeypatch):
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.tax import AustrianTaxReports

    runtime, db, audit = make_runtime()
    year = datetime.now(ZoneInfo("Europe/Vienna")).year
    event = {
        "event_id": "TAX-CORRECTED",
        "ts": f"{year}-06-01T12:00:00+00:00",
        "year": year,
        "event_type": "TRADE_FILL",
        "value_eur": None,
        "verified": False,
        "manual_review": True,
    }
    monkeypatch.setattr(db, "tax_event_years", lambda: [year])
    monkeypatch.setattr(
        db,
        "tax_events_for_year",
        lambda requested_year: [dict(event)] if requested_year == year else [],
    )
    runtime.tax_reports = AustrianTaxReports(str(tmp_path))

    runtime._refresh_tax_report()
    report_path = tmp_path / str(year) / "income-tax-report.json"
    first_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert first_report["events"][0]["value_eur"] is None
    assert first_report["events"][0]["verified"] is False

    # The event ID and row count stay constant; later reconciliation enriches it.
    event["value_eur"] = "123.45"
    event["verified"] = True
    event["manual_review"] = False
    runtime._refresh_tax_report()

    updated_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert updated_report["event_count"] == 1
    assert updated_report["events"][0]["value_eur"] == "123.45"
    assert updated_report["events"][0]["verified"] is True
    assert updated_report["events"][0]["manual_review"] is False
    audit.close()
    db.close()
