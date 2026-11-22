import io
import json
import sqlite3
import time
from decimal import Decimal

from app.models import Instrument

from app.audit import AuditLogger
from app.config import Settings
from app.persistence import Persistence
from app.runtime import (
    TraderRuntime,
    _futures_account_summary,
    _runtime_failure_context,
    _resolve_online_kraken_status_breaker,
    _circuit_breaker_diagnostics,
    _open_position_rows,
    _recover_transient_public_network_breaker,
    _recover_transient_market_data_breaker,
    _recover_transient_sqlite_cantopen_research_read_breaker,
    _resolve_clean_reconciliation_breaker,
)


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


def test_circuit_breaker_diagnostics_include_reason_and_safe_context_only():
    diagnostics = _circuit_breaker_diagnostics([{
        "name": "KRAKEN_API",
        "reason": "EAPI:Invalid nonce",
        "ts": "2026-10-20T16:00:00Z",
        "active": True,
        "detail": {
            "cycle_id": "cycle-123",
            "code": "EAPI:Invalid nonce",
            "api_secret": "must-not-be-emitted",
        },
    }])

    assert diagnostics == [{
        "name": "KRAKEN_API",
        "reason": "EAPI:Invalid nonce",
        "ts": "2026-10-20T16:00:00Z",
        "cycle_id": "cycle-123",
        "code": "EAPI:Invalid nonce",
    }]
    assert "must-not-be-emitted" not in str(diagnostics)
    assert "api_secret" not in str(diagnostics)


def test_circuit_breaker_diagnostics_expose_safe_sqlite_failure_context():
    diagnostics = _circuit_breaker_diagnostics([{
        "name": "UNHANDLED_RUNTIME",
        "reason": "CYCLE_EXCEPTION",
        "ts": "2026-10-30T18:35:08.285234+00:00",
        "detail": {
            "cycle_id": "failed-cycle",
            "exception_type": "OperationalError",
            "failure_stage": "FINALIZE_CYCLE_SUMMARY_WRITE",
            "failure_location": {
                "file": "persistence.py",
                "line": 842,
                "function": "record_event",
                "api_secret": "must-not-be-emitted",
            },
            "sqlite_error_code": 10,
            "sqlite_error_name": "SQLITE_IOERR",
            "sqlite_error_message": "disk I/O error",
            "api_secret": "must-not-be-emitted",
        },
    }])

    assert diagnostics == [{
        "name": "UNHANDLED_RUNTIME",
        "reason": "CYCLE_EXCEPTION",
        "ts": "2026-10-30T18:35:08.285234+00:00",
        "cycle_id": "failed-cycle",
        "exception_type": "OperationalError",
        "failure_stage": "FINALIZE_CYCLE_SUMMARY_WRITE",
        "sqlite_error_code": 10,
        "sqlite_error_name": "SQLITE_IOERR",
        "failure_location": {
            "file": "persistence.py",
            "function": "record_event",
            "line": 842,
        },
        "sqlite_error_message": "disk I/O error",
    }]
    assert "must-not-be-emitted" not in str(diagnostics)
    assert "api_secret" not in str(diagnostics)



def test_circuit_breaker_diagnostics_expose_persisted_cantopen_storage_snapshot_only():
    storage_snapshot = {
        "process_uid": 10001,
        "process_gid": 10001,
        "database_path": "/data/state/kraken_trader.sqlite3",
        "database_parent": {
            "exists": True, "is_directory": True, "mode": "0o700",
            "uid": 10001, "gid": 10001, "readable": True, "writable": True,
            "searchable": True, "unexpected_secret": "must-not-be-emitted",
        },
        "database": {
            "exists": True, "is_directory": False, "mode": "0o600",
            "uid": 10001, "gid": 10001, "size_bytes": 8192,
            "readable": True, "writable": True, "searchable": None,
        },
        "temporary_directory": "/tmp",
        "temporary_directory_state": {
            "exists": True, "is_directory": True, "mode": "0o1777",
            "uid": 0, "gid": 0, "readable": True, "writable": True,
            "searchable": True,
        },
        "sqlite_temp_store": "0",
        "sqlite_journal_mode": "wal",
        "api_secret": "must-not-be-emitted",
    }
    diagnostics = _circuit_breaker_diagnostics([{
        "name": "UNHANDLED_RUNTIME",
        "reason": "CYCLE_EXCEPTION",
        "ts": "2026-10-30T18:35:08.285234+00:00",
        "detail": {
            "sqlite_error_code": 14,
            "sqlite_error_name": "SQLITE_CANTOPEN",
            "sqlite_storage_diagnostics": storage_snapshot,
            "api_secret": "must-not-be-emitted",
        },
    }])

    assert diagnostics[0]["sqlite_storage_diagnostics"] == {
        "process_uid": 10001,
        "process_gid": 10001,
        "database_path": "/data/state/kraken_trader.sqlite3",
        "database_parent": {
            "exists": True, "is_directory": True, "mode": "0o700",
            "uid": 10001, "gid": 10001, "readable": True, "writable": True,
            "searchable": True,
        },
        "database": {
            "exists": True, "is_directory": False, "mode": "0o600",
            "uid": 10001, "gid": 10001, "size_bytes": 8192,
            "readable": True, "writable": True,
        },
        "temporary_directory": "/tmp",
        "temporary_directory_state": {
            "exists": True, "is_directory": True, "mode": "0o1777",
            "uid": 0, "gid": 0, "readable": True, "writable": True,
            "searchable": True,
        },
        "sqlite_temp_store": "0",
        "sqlite_journal_mode": "wal",
    }
    assert "must-not-be-emitted" not in str(diagnostics)
    assert "unexpected_secret" not in str(diagnostics)

    unrelated_error = _circuit_breaker_diagnostics([{
        "name": "UNHANDLED_RUNTIME",
        "reason": "CYCLE_EXCEPTION",
        "ts": "2026-10-30T18:35:08.285234+00:00",
        "detail": {
            "sqlite_error_code": 10,
            "sqlite_error_name": "SQLITE_IOERR",
            "sqlite_storage_diagnostics": storage_snapshot,
        },
    }])
    assert "sqlite_storage_diagnostics" not in unrelated_error[0]


def test_cantopen_breaker_includes_current_storage_snapshot_separately_from_history(tmp_path):
    database_path = str(tmp_path / "state" / "trader.sqlite3")
    db = Persistence(database_path)
    try:
        diagnostics = _circuit_breaker_diagnostics([{
            "name": "UNHANDLED_RUNTIME",
            "reason": "CYCLE_EXCEPTION",
            "ts": "2026-10-30T18:35:08.285234+00:00",
            "detail": {
                "sqlite_error_code": 14,
                "sqlite_error_name": "SQLITE_CANTOPEN",
                "failure_stage": "FINALIZE_TSM_RESEARCH_SUMMARY_READ",
            },
        }], persistence=db)

        item = diagnostics[0]
        assert "sqlite_storage_diagnostics" not in item
        current = item["sqlite_current_storage_diagnostics"]
        assert current["database_path"] == database_path
        assert current["database"]["exists"] is True
        assert current["database"]["is_directory"] is False
        assert current["database_parent"]["exists"] is True
        assert current["database_parent"]["is_directory"] is True
        assert "sqlite_current_storage_diagnostics_observed_at" in item
        assert item["sqlite_current_storage_diagnostics_scope"] == (
            "CURRENT_RUNTIME_STATE_NOT_ORIGINAL_FAILURE_STATE"
        )
    finally:
        db.close()


def test_current_storage_diagnostic_probe_failure_does_not_break_cycle():
    class BrokenStorageProbe:
        def storage_diagnostics(self):
            raise sqlite3.OperationalError("probe failed")

    diagnostics = _circuit_breaker_diagnostics([{
        "name": "UNHANDLED_RUNTIME",
        "reason": "CYCLE_EXCEPTION",
        "ts": "2026-10-30T18:35:08.285234+00:00",
        "detail": {
            "sqlite_error_code": 14,
            "sqlite_error_name": "SQLITE_CANTOPEN",
        },
    }], persistence=BrokenStorageProbe())

    assert diagnostics[0]["sqlite_current_storage_diagnostics_probe_error"] == {
        "exception_type": "OperationalError",
    }


def test_non_cantopen_breaker_does_not_probe_current_storage():
    class MustNotProbe:
        def storage_diagnostics(self):
            raise AssertionError("storage diagnostics should only run for CANTOPEN")

    diagnostics = _circuit_breaker_diagnostics([{
        "name": "UNHANDLED_RUNTIME",
        "reason": "CYCLE_EXCEPTION",
        "ts": "2026-10-30T18:35:08.285234+00:00",
        "detail": {
            "sqlite_error_code": 10,
            "sqlite_error_name": "SQLITE_IOERR",
        },
    }], persistence=MustNotProbe())

    assert "sqlite_current_storage_diagnostics" not in diagnostics[0]
    assert "sqlite_current_storage_diagnostics_probe_error" not in diagnostics[0]



def test_circuit_breaker_diagnostics_identify_wallet_mismatch_assets():
    diagnostics = _circuit_breaker_diagnostics([{
        "name": "WALLET_BALANCE_RECONCILIATION",
        "reason": "WALLET_BALANCE_MISMATCH",
        "ts": "2026-10-20T16:00:00Z",
        "detail": {
            "cycle_id": "cycle-456",
            "evidence_gaps": ["ZUSD:BALANCE_DIFFERS_FROM_LEDGER"],
            "asset_results": [
                {"asset": "ZUSD", "status": "BALANCE_DIFFERS_FROM_LEDGER", "difference": "1.25"},
                {"asset": "XXBT", "status": "BALANCE_STABLE", "difference": "0"},
            ],
        },
    }])

    assert diagnostics[0]["asset_result_count"] == 2
    assert diagnostics[0]["mismatched_assets"] == [{
        "asset": "ZUSD",
        "status": "BALANCE_DIFFERS_FROM_LEDGER",
        "difference": "1.25",
    }]


def test_transient_public_network_breaker_recovers_after_healthy_account_cycle():
    runtime, db, audit = make_runtime()
    db.trip_circuit_breaker("KRAKEN_API", "KRAKEN_PUBLIC_NETWORK_ERROR", {"cycle_id": "failed-cycle"})
    db.trip_circuit_breaker("ORDER_SNAPSHOT", "ORDER_RESPONSE_SHAPE_INVALID", {})

    recovered = _recover_transient_public_network_breaker(
        db, audit, cycle_id="recovery-cycle", system_status="online",
        instruments_count=625, initial_ticker_count=624, refreshed_ticker_count=624,
        credentials_present=True, api_permissions_ready=True,
        history_ready_for_entries=True, history_evidence_gaps=[],
        account_snapshot_available=True,
    )

    assert recovered is True
    assert [(row["name"], row["reason"]) for row in db.circuit_breakers()] == [
        ("ORDER_SNAPSHOT", "ORDER_RESPONSE_SHAPE_INVALID")
    ]
    audit.close()
    db.close()


def test_public_network_breaker_stays_active_without_complete_health_evidence():
    runtime, db, audit = make_runtime()
    db.trip_circuit_breaker("KRAKEN_API", "KRAKEN_PUBLIC_NETWORK_ERROR")

    recovered = _recover_transient_public_network_breaker(
        db, audit, cycle_id="incomplete-cycle", system_status="online",
        instruments_count=625, initial_ticker_count=624, refreshed_ticker_count=0,
        credentials_present=True, api_permissions_ready=True,
        history_ready_for_entries=True, history_evidence_gaps=[],
        account_snapshot_available=True,
    )

    assert recovered is False
    assert [(row["name"], row["reason"]) for row in db.circuit_breakers()] == [
        ("KRAKEN_API", "KRAKEN_PUBLIC_NETWORK_ERROR")
    ]
    audit.close()
    db.close()


def test_other_kraken_api_breaker_reasons_are_not_auto_resolved():
    runtime, db, audit = make_runtime()
    db.trip_circuit_breaker("KRAKEN_API", "KRAKEN_AUTHENTICATION_FAILURE")

    recovered = _recover_transient_public_network_breaker(
        db, audit, cycle_id="different-error-cycle", system_status="online",
        instruments_count=625, initial_ticker_count=624, refreshed_ticker_count=624,
        credentials_present=True, api_permissions_ready=True,
        history_ready_for_entries=True, history_evidence_gaps=[],
        account_snapshot_available=True,
    )

    assert recovered is False
    assert [(row["name"], row["reason"]) for row in db.circuit_breakers()] == [
        ("KRAKEN_API", "KRAKEN_AUTHENTICATION_FAILURE")
    ]
    audit.close()
    db.close()


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


def test_default_runtime_has_no_background_microstructure_worker():
    runtime, db, audit = make_runtime()

    assert runtime.settings.real_orders_enabled is False
    assert runtime.microstructure.enabled is False
    assert runtime.microstructure._thread is None
    assert runtime.microstructure.status()["connection_state"] == "DISABLED"

    runtime.close()
    audit.close()
    db.close()


def account_snapshot(positions):
    return {
        "captured_at": time.time(),
        "trade_balance": {"e": "1000", "mf": "500", "m": "100", "ml": "450"},
        "open_positions": {"open": positions},
        "open_orders": {"open": {}},
        "closed_orders": {"closed": {}},
        "balance": {"ZUSD": "1000"},
    }


def test_risk_snapshot_reflects_supplied_reconciliation_and_equity_baselines():
    runtime, db, audit = make_runtime()
    risk_state = {
        "daily_pnl_pct": D("-1.25"),
        "drawdown_pct": D("2.5"),
    }
    snapshot = runtime._account_risk_snapshot(
        account_snapshot({}),
        reconciliation_ok=True,
        equity_risk_state=risk_state,
    )
    assert snapshot.reconciliation_ok is True
    assert snapshot.daily_pnl_pct == D("-1.25")
    assert snapshot.drawdown_pct == D("2.5")
    audit.close()
    db.close()


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


def test_tax_report_refresh_invalidates_when_existing_event_content_changes(tmp_path):
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
        "payload": {"source": "test"},
    }
    assert db.record_tax_event(event)
    runtime.tax_reports = AustrianTaxReports(str(tmp_path))

    runtime._refresh_tax_report()
    report_path = tmp_path / str(year) / "income-tax-report.json"
    first_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert first_report["events"][0]["value_eur"] is None
    assert first_report["events"][0]["verified"] == 0

    # Explicit enrichment changes content, but keeps event ID and row count constant.
    enriched = {
        **event,
        "value_eur": "123.45",
        "verified": True,
        "manual_review": False,
        "payload": {"source": "test", "fx_rate_source": "verified-test-fixture"},
    }
    assert db.update_tax_event(enriched)
    runtime._refresh_tax_report()

    updated_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert updated_report["event_count"] == 1
    assert updated_report["events"][0]["value_eur"] == "123.45"
    assert updated_report["events"][0]["verified"] == 1
    assert updated_report["events"][0]["manual_review"] == 0
    assert updated_report["events"][0]["payload"]["fx_rate_source"] == "verified-test-fixture"
    audit.close()
    db.close()

def test_runtime_fetches_a_fresh_pair_scoped_ticker_for_entry_evaluation():
    from app.models import Instrument
    from decimal import Decimal

    runtime, db, audit = make_runtime()
    instrument = Instrument(
        "XXBTZUSD", "XBTUSD", "XBT/USD", "XBT", "USD", "online",
        Decimal("0.0001"), 8, 1, (2, 3), (2, 3),
    )

    class FakePublicClient:
        def __init__(self):
            self.calls = []

        def public(self, method, params=None):
            self.calls.append((method, params))
            assert method == "Ticker"
            assert params == {"pair": "XBTUSD"}
            return {
                "XBTUSD": {
                    "c": ["100", "100"],
                    "a": ["101", "101"],
                    "b": ["99", "99"],
                    "v": ["10", "20"],
                    "p": ["100", "100"],
                },
            }

    client = FakePublicClient()
    runtime.client = client
    ticker = runtime._fetch_fresh_ticker(instrument)

    assert ticker is not None
    assert ticker.pair_id == "XXBTZUSD"
    assert ticker.ask == Decimal("101")
    assert ticker.bid == Decimal("99")
    assert client.calls == [("Ticker", {"pair": "XBTUSD"})]
    audit.close()
    db.close()


def test_legacy_validate_shadow_option_is_ignored_and_simulation_stays_safe():
    settings = Settings.from_mapping({
        "mode": "LIVE",
        "live_enabled": True,
        "kill_switch": False,
        "validate_shadow_orders": True,
    })
    assert settings.real_orders_enabled is False
    assert settings.live_requested is False
    assert not hasattr(settings, "validate_shadow_orders")

def test_runtime_probes_history_and_persists_positions_without_local_exit_watcher(monkeypatch):
    import json

    import app.runtime as runtime_module
    from app.kraken import assess_api_key_info
    from app.models import Instrument, Ticker

    runtime, db, audit = make_runtime()
    runtime.settings = Settings.from_mapping({
        "kraken_api_key": "test-key",
        "kraken_api_secret": "test-secret",
        "tax_report_enabled": False,
    })
    key_assessment = assess_api_key_info({
        "apiKey": "never-log-this",
        "permissions": [
            "query-funds", "query-open-trades", "query-closed-trades", "query-ledger",
        ],
    }, now_epoch=200)
    instrument = Instrument(
        "XXBTZUSD", "XBTUSD", "XBT/USD", "XBT", "USD", "online",
        D("0.0001"), 8, 1, (2, 3), (2, 3),
    )

    class FakeClient:
        def __init__(self):
            self.permission_checks = 0
            self.snapshot_options = []
            self.trade_volume_calls = 0

        def public(self, method, params=None):
            if method == "SystemStatus":
                return {"status": "online"}
            if method == "AssetPairs":
                return {}
            if method == "Ticker":
                return {}
            if method == "OHLC":
                return {}
            raise AssertionError(f"unexpected public endpoint: {method}")

        def api_key_permission_assessment(self):
            self.permission_checks += 1
            return key_assessment

        def account_snapshot(self, *, include_closed_orders=True):
            self.snapshot_options.append(include_closed_orders)
            snapshot = account_snapshot({
                "POSITION-1": {
                    "pair": "XXBTZUSD", "type": "buy", "posstatus": "open",
                    "vol": "1", "vol_closed": "0", "cost": "100", "fee": "0",
                    "margin": "25", "value": "103.5", "net": "3.5", "time": "1700000000",
                },
            })
            snapshot["closed_orders"] = {"closed": {}} if include_closed_orders else None
            return snapshot

        def trade_volume(self, pairs):
            self.trade_volume_calls += 1
            return {}

    client = FakeClient()
    runtime.client = client
    ticker = Ticker(
        pair_id="XXBTZUSD",
        last=D("103.50"),
        bid=D("103.50"),
        ask=D("103.51"),
        volume_quote_24h=D("100000"),
    )
    history_calls = []

    def fake_history_sync(*, client, persistence, source, **kwargs):
        history_calls.append(source)
        return {
            "source": source, "pages": 1, "items_saved": 0, "expected_count": 0,
            "next_offset": 0, "generation_complete": False,
            "initial_backfill_complete": False, "cached_until_refresh": False,
            "last_error": None,
        }

    monkeypatch.setattr(runtime_module, "sync_history_source", fake_history_sync)
    monkeypatch.setattr(runtime_module, "usd_crypto_universe", lambda _raw: [instrument])
    monkeypatch.setattr(runtime_module, "parse_tickers", lambda *_args: {"XXBTZUSD": ticker})

    result = runtime.cycle()

    assert result["status"] == "OK", (result, audit.stream.getvalue())
    assert client.permission_checks == 1
    assert client.snapshot_options == [True]
    assert history_calls == ["TRADES", "LEDGERS", "CLOSED_ORDERS"]
    assert client.trade_volume_calls == 0
    assert db.circuit_breakers() == []
    logs = audit.stream.getvalue()
    breaker_statuses = [
        json.loads(line) for line in logs.splitlines()
        if line.strip() and '"event":"CIRCUIT_BREAKER_STATUS"' in line
    ]
    assert breaker_statuses
    assert breaker_statuses[-1]["payload"]["active"] is False
    assert breaker_statuses[-1]["payload"]["count"] == 0
    assert breaker_statuses[-1]["payload"]["breaker_blocks_new_entries"] is False
    assert breaker_statuses[-1]["payload"]["status_scope"] == "PERSISTED_CIRCUIT_BREAKERS_ONLY"
    assert "new_entries_blocked" not in breaker_statuses[-1]["payload"]
    assert "KRAKEN_API_KEY_METADATA_INCOMPLETE" in logs
    assert "KRAKEN_HISTORY_SYNC_SKIPPED" not in logs
    assert "NEW_RISK_BLOCKED_HISTORY_INCOMPLETE" in logs
    assert "ACCOUNT_SNAPSHOT_PERSISTED" in logs
    reconciled_events = [
        json.loads(line) for line in logs.splitlines()
        if line.strip() and '"event":"KRAKEN_HISTORY_RECONCILED"' in line
    ]
    assert reconciled_events
    assert reconciled_events[-1]["payload"]["history_ready_for_entries"] is False
    # The lightweight runtime persists the exchange position snapshot, but no
    # longer runs the removed local-only SHADOW exit recommendation watcher.
    position_rows = db.latest_exchange_position_snapshot()
    assert [row["position_txid"] for row in position_rows] == ["POSITION-1"]
    assert '"event":"POSITION_MANAGEMENT_DECISION"' not in logs
    assert "never-log-this" not in logs
    cycle_summaries = [
        json.loads(line) for line in logs.splitlines()
        if line.strip() and '"event":"CYCLE_SUMMARY"' in line
    ]
    assert cycle_summaries
    assert cycle_summaries[-1]["payload"]["internal_live_release"] is False
    assert cycle_summaries[-1]["payload"]["background_research_status"] == "DISABLED_BY_ARCHITECTURE"
    assert cycle_summaries[-1]["payload"]["microstructure_collector_enabled"] is False
    assert cycle_summaries[-1]["payload"]["microstructure_connection_state"] == "DISABLED"
    assert cycle_summaries[-1]["payload"]["microstructure_messages_this_process"] == 0
    assert cycle_summaries[-1]["payload"]["microstructure_history_retained"] is True
    audit.close()
    db.close()


def test_runtime_blocks_history_sync_but_keeps_position_monitoring_for_explicit_query_restriction(monkeypatch):
    import app.runtime as runtime_module
    from app.kraken import assess_api_key_info
    from app.models import Instrument

    runtime, db, audit = make_runtime()
    runtime.settings = Settings.from_mapping({
        "kraken_api_key": "test-key",
        "kraken_api_secret": "test-secret",
        "tax_report_enabled": False,
    })
    key_assessment = assess_api_key_info({
        "apiKey": "never-log-this",
        "permissions": [
            "query-funds", "query-open-trades", "query-closed-trades", "query-ledger",
        ],
        "validUntil": "0",
        "queryFrom": "100",
        "queryTo": "0",
    }, now_epoch=200)
    instrument = Instrument(
        "XXBTZUSD", "XBTUSD", "XBT/USD", "XBT", "USD", "online",
        D("0.0001"), 8, 1, (2, 3), (2, 3),
    )

    class FakeClient:
        def __init__(self):
            self.snapshot_options = []

        def public(self, method, params=None):
            if method in {"SystemStatus", "Ticker"}:
                return {"status": "online"} if method == "SystemStatus" else {}
            if method == "AssetPairs":
                return {}
            if method == "OHLC":
                return {}
            raise AssertionError(f"unexpected public endpoint: {method}")

        def api_key_permission_assessment(self):
            return key_assessment

        def account_snapshot(self, *, include_closed_orders=True):
            self.snapshot_options.append(include_closed_orders)
            return account_snapshot({})

        def trade_volume(self, pairs):
            raise AssertionError("TradeVolume must remain gated under an explicit restriction")

    client = FakeClient()
    runtime.client = client

    def unexpected_history_call(**kwargs):
        raise AssertionError("history sync must not run for an explicitly restricted query window")

    monkeypatch.setattr(runtime_module, "sync_history_source", unexpected_history_call)
    monkeypatch.setattr(runtime_module, "usd_crypto_universe", lambda _raw: [instrument])
    monkeypatch.setattr(runtime_module, "parse_tickers", lambda *_args: {})

    result = runtime.cycle()

    assert result["status"] == "OK", (result, audit.stream.getvalue())
    assert client.snapshot_options == [False]
    logs = audit.stream.getvalue()
    assert "API_KEY_PERMISSION_GATE_BLOCKED" in logs
    assert "KRAKEN_HISTORY_SYNC_SKIPPED" in logs
    assert "NEW_RISK_BLOCKED_HISTORY_INCOMPLETE" in logs
    assert "never-log-this" not in logs
    audit.close()
    db.close()


def test_runtime_retries_failed_api_key_preflight_without_persistent_breaker(monkeypatch):
    import app.runtime as runtime_module
    from app.kraken import KrakenAPIError
    from app.models import Instrument

    runtime, db, audit = make_runtime()
    runtime.settings = Settings.from_mapping({
        "kraken_api_key": "test-key",
        "kraken_api_secret": "test-secret",
        "tax_report_enabled": False,
    })
    instrument = Instrument(
        "XXBTZUSD", "XBTUSD", "XBT/USD", "XBT", "USD", "online",
        D("0.0001"), 8, 1, (2, 3), (2, 3),
    )

    class FakeClient:
        def __init__(self):
            self.permission_checks = 0

        def public(self, method, params=None):
            if method == "SystemStatus":
                return {"status": "online"}
            if method == "AssetPairs":
                return {}
            if method == "Ticker":
                return {}
            raise AssertionError(f"unexpected public endpoint: {method}")

        def api_key_permission_assessment(self):
            self.permission_checks += 1
            raise KrakenAPIError("KRAKEN_PRIVATE:Permission denied")

    client = FakeClient()
    runtime.client = client
    monkeypatch.setattr(runtime_module, "usd_crypto_universe", lambda _raw: [instrument])
    monkeypatch.setattr(runtime_module, "parse_tickers", lambda *_args: {})

    result = runtime.cycle()

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "API_KEY_PERMISSION_CHECK_FAILED"
    assert result["permission_error_code"] == "KRAKEN_PRIVATE:Permission denied"
    assert client.permission_checks == 1
    assert db.circuit_breakers() == []
    logs = audit.stream.getvalue()
    assert "API_KEY_PERMISSION_CHECK_FAILED" in logs
    assert "entries_blocked" in logs
    audit.close()
    db.close()


def test_clean_account_snapshot_resolves_only_its_own_stale_reconciliation_breaker():
    runtime, db, audit = make_runtime()
    db.trip_circuit_breaker(
        "ACCOUNT_SNAPSHOT", "POSITION_SNAPSHOT_SHAPE_INVALID",
        {"cycle_id": "old-cycle"},
    )
    db.trip_circuit_breaker(
        "WALLET_BALANCE_RECONCILIATION", "WALLET_BALANCE_MISMATCH",
        {"cycle_id": "other-old-cycle"},
    )

    recovered = _resolve_clean_reconciliation_breaker(
        db, audit,
        name="ACCOUNT_SNAPSHOT",
        cycle_id="new-clean-cycle",
        verified_by="CURRENT_ACCOUNT_SNAPSHOT",
        clean=True,
    )

    assert recovered is True
    assert [(row["name"], row["reason"]) for row in db.circuit_breakers()] == [
        ("WALLET_BALANCE_RECONCILIATION", "WALLET_BALANCE_MISMATCH")
    ]
    audit.close()
    db.close()


def test_unclean_account_snapshot_does_not_resolve_stale_reconciliation_breaker():
    runtime, db, audit = make_runtime()
    db.trip_circuit_breaker(
        "ACCOUNT_SNAPSHOT", "POSITION_SNAPSHOT_SHAPE_INVALID",
        {"cycle_id": "old-cycle"},
    )

    recovered = _resolve_clean_reconciliation_breaker(
        db, audit,
        name="ACCOUNT_SNAPSHOT",
        cycle_id="still-bad-cycle",
        verified_by="CURRENT_ACCOUNT_SNAPSHOT",
        clean=False,
    )

    assert recovered is False
    assert [(row["name"], row["reason"]) for row in db.circuit_breakers()] == [
        ("ACCOUNT_SNAPSHOT", "POSITION_SNAPSHOT_SHAPE_INVALID")
    ]
    audit.close()
    db.close()


def test_runtime_failure_context_identifies_sqlite_error_and_failing_location():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("SELECT * FROM definitely_missing_table")
    except sqlite3.OperationalError as exc:
        details = _runtime_failure_context(
            exc, failure_stage="FINALIZE_TSM_RESEARCH_SUMMARY_READ"
        )
    else:
        raise AssertionError("Expected SQLite OperationalError")
    finally:
        connection.close()

    assert details["failure_stage"] == "FINALIZE_TSM_RESEARCH_SUMMARY_READ"
    assert details["exception_type"] == "OperationalError"
    assert details["sqlite_error_message"] == "no such table: definitely_missing_table"
    assert details["sqlite_error_name"] == "SQLITE_ERROR"
    assert isinstance(details["sqlite_error_code"], int)
    assert details["failure_location"]["file"] == "test_runtime.py"
    assert details["failure_location"]["function"] == "test_runtime_failure_context_identifies_sqlite_error_and_failing_location"


def test_runtime_failure_context_adds_storage_state_for_sqlite_cantopen(tmp_path):
    db = Persistence(str(tmp_path / "trader.sqlite3"))
    missing_database = tmp_path / "missing-directory" / "missing.sqlite3"
    try:
        sqlite3.connect(str(missing_database))
    except sqlite3.OperationalError as exc:
        details = _runtime_failure_context(
            exc,
            failure_stage="FINALIZE_TSM_RESEARCH_SUMMARY_READ",
            persistence=db,
        )
    else:
        raise AssertionError("Expected SQLITE_CANTOPEN")

    diagnostics = details["sqlite_storage_diagnostics"]
    assert details["sqlite_error_name"] == "SQLITE_CANTOPEN"
    assert diagnostics["database_path"] == str(tmp_path / "trader.sqlite3")
    assert diagnostics["database"]["exists"] is True
    assert diagnostics["temporary_directory_state"]["exists"] is True
    assert "sqlite_storage_diagnostics" not in str(
        _runtime_failure_context(
            ValueError("must-not-be-logged"),
            failure_stage="MARKET_SCAN",
            persistence=db,
        )
    )
    db.close()


def test_runtime_failure_context_does_not_log_arbitrary_exception_messages():
    try:
        raise ValueError("private-api-secret-must-not-be-logged")
    except ValueError as exc:
        details = _runtime_failure_context(
            exc, failure_stage="MARKET_SCAN:XXBTZUSD"
        )

    assert details["failure_stage"] == "MARKET_SCAN:XXBTZUSD"
    assert details["exception_type"] == "ValueError"
    assert "sqlite_error_message" not in details
    assert "private-api-secret-must-not-be-logged" not in str(details)
    assert details["failure_location"]["file"] == "test_runtime.py"


def test_current_online_kraken_status_resolves_only_stale_maintenance_breaker():
    runtime, db, audit = make_runtime()
    db.trip_circuit_breaker(
        "KRAKEN_STATUS", "KRAKEN_NOT_ONLINE", {"status": "maintenance"}
    )

    recovered = _resolve_online_kraken_status_breaker(
        db, audit, cycle_id="recovered-cycle", system_status="online"
    )

    assert recovered is True
    assert all(row["name"] != "KRAKEN_STATUS" for row in db.circuit_breakers())
    recovered_events = db.connection.execute(
        "SELECT COUNT(*) AS count FROM events WHERE name='CIRCUIT_BREAKER_RECOVERED'"
    ).fetchone()
    assert recovered_events["count"] == 1
    audit.close()
    db.close()


def test_kraken_status_breaker_is_not_resolved_when_offline_or_reason_changed():
    runtime, db, audit = make_runtime()
    db.trip_circuit_breaker(
        "KRAKEN_STATUS", "KRAKEN_NOT_ONLINE", {"status": "maintenance"}
    )

    assert _resolve_online_kraken_status_breaker(
        db, audit, cycle_id="still-offline", system_status="maintenance"
    ) is False
    assert db.circuit_breakers()[0]["reason"] == "KRAKEN_NOT_ONLINE"

    db.trip_circuit_breaker(
        "KRAKEN_STATUS", "OPERATOR_REVIEW_REQUIRED", {"status": "online"}
    )
    assert _resolve_online_kraken_status_breaker(
        db, audit, cycle_id="changed-reason", system_status="online"
    ) is False
    assert db.circuit_breakers()[0]["reason"] == "OPERATOR_REVIEW_REQUIRED"
    audit.close()
    db.close()



def _historical_sqlite_cantopen_detail(**overrides):
    detail = {
        "exception_type": "OperationalError",
        "failure_stage": "FINALIZE_TSM_RESEARCH_SUMMARY_READ",
        "failure_location": {
            "file": "persistence.py",
            "function": "research_observations",
            "line": 624,
        },
        "sqlite_error_code": sqlite3.SQLITE_CANTOPEN,
        "sqlite_error_name": "SQLITE_CANTOPEN",
        "sqlite_error_message": "unable to open database file",
    }
    detail.update(overrides)
    return detail


def test_exact_sqlite_cantopen_research_read_breaker_recovers_after_read_and_storage_check(tmp_path):
    db = Persistence(str(tmp_path / "state" / "trader.sqlite3"))
    audit = AuditLogger(str(tmp_path / "audit.jsonl"), stream=io.StringIO())
    db.trip_circuit_breaker(
        "UNHANDLED_RUNTIME", "CYCLE_EXCEPTION", _historical_sqlite_cantopen_detail()
    )

    observations = db.research_observations(limit=5000)
    assert _recover_transient_sqlite_cantopen_research_read_breaker(
        db,
        audit,
        cycle_id="validated-cycle",
        research_observation_count=len(observations),
    ) is True

    assert db.circuit_breakers() == []
    recovered = db.connection.execute(
        "SELECT payload_json FROM events WHERE name='CIRCUIT_BREAKER_RECOVERED'"
    ).fetchone()
    assert recovered is not None
    assert "EXACT_RESEARCH_READ_AND_CURRENT_STORAGE_VALIDATION" in recovered["payload_json"]
    assert "validated-cycle" in audit.stream.getvalue()
    audit.close()
    db.close()


def test_sqlite_cantopen_recovery_does_not_clear_different_failure_signatures(tmp_path):
    db = Persistence(str(tmp_path / "state.sqlite3"))
    audit = AuditLogger(str(tmp_path / "audit.jsonl"), stream=io.StringIO())
    mismatches = [
        _historical_sqlite_cantopen_detail(failure_stage="FINALIZE_TSM_RESEARCH_SUMMARY_WRITE"),
        _historical_sqlite_cantopen_detail(
            sqlite_error_code=sqlite3.SQLITE_IOERR, sqlite_error_name="SQLITE_IOERR"
        ),
        _historical_sqlite_cantopen_detail(
            failure_location={
                "file": "persistence.py",
                "function": "other",
                "line": 624,
            }
        ),
        _historical_sqlite_cantopen_detail(exception_type="ValueError"),
    ]
    for index, mismatch in enumerate(mismatches):
        db.trip_circuit_breaker("UNHANDLED_RUNTIME", "CYCLE_EXCEPTION", mismatch)
        assert _recover_transient_sqlite_cantopen_research_read_breaker(
            db, audit, cycle_id=f"mismatch-{index}", research_observation_count=0
        ) is False
        assert db.circuit_breakers()[0]["detail"] == mismatch
    audit.close()
    db.close()


def test_sqlite_cantopen_recovery_defers_when_current_storage_probe_is_unhealthy(tmp_path, monkeypatch):
    db = Persistence(str(tmp_path / "state.sqlite3"))
    audit = AuditLogger(str(tmp_path / "audit.jsonl"), stream=io.StringIO())
    db.trip_circuit_breaker(
        "UNHANDLED_RUNTIME", "CYCLE_EXCEPTION", _historical_sqlite_cantopen_detail()
    )
    monkeypatch.setattr(db, "storage_diagnostics", lambda: {
        "database": {
            "exists": True,
            "is_directory": False,
            "readable": True,
            "writable": False,
        },
        "database_parent": {
            "exists": True,
            "is_directory": True,
            "readable": True,
            "writable": True,
            "searchable": True,
        },
        "temporary_directory_state": {
            "exists": True,
            "is_directory": True,
            "readable": True,
            "writable": True,
            "searchable": True,
        },
        "sqlite_journal_mode": "wal",
        "sqlite_temp_store": "0",
    })

    assert _recover_transient_sqlite_cantopen_research_read_breaker(
        db, audit, cycle_id="storage-unhealthy", research_observation_count=0
    ) is False
    assert db.circuit_breakers()[0]["active"] is True
    assert "CIRCUIT_BREAKER_RECOVERY_DEFERRED" in audit.stream.getvalue()
    audit.close()
    db.close()


def test_resolve_circuit_breaker_can_require_a_matching_persisted_detail_signature():
    db = Persistence(":memory:")
    db.trip_circuit_breaker(
        "UNHANDLED_RUNTIME", "CYCLE_EXCEPTION", {"exception_type": "ValueError"}
    )
    assert db.resolve_circuit_breaker(
        "UNHANDLED_RUNTIME",
        "CYCLE_EXCEPTION",
        expected_detail={"exception_type": "OperationalError"},
    ) is False
    assert db.circuit_breakers()[0]["active"] is True
    db.close()



def test_sqlite_cantopen_recovery_defers_when_storage_probe_raises(tmp_path, monkeypatch):
    db = Persistence(str(tmp_path / "state.sqlite3"))
    audit = AuditLogger(str(tmp_path / "audit.jsonl"), stream=io.StringIO())
    db.trip_circuit_breaker(
        "UNHANDLED_RUNTIME", "CYCLE_EXCEPTION", _historical_sqlite_cantopen_detail()
    )

    def fail_probe():
        raise OSError("filesystem probe unavailable")

    monkeypatch.setattr(db, "storage_diagnostics", fail_probe)
    assert _recover_transient_sqlite_cantopen_research_read_breaker(
        db, audit, cycle_id="storage-probe-error", research_observation_count=0
    ) is False
    assert db.circuit_breakers()[0]["active"] is True
    assert "OSError" in audit.stream.getvalue()
    assert "filesystem probe unavailable" not in audit.stream.getvalue()
    audit.close()
    db.close()



def test_exact_kraken_internal_error_breaker_recovers_after_complete_fresh_market_reads():
    _runtime, db, audit = make_runtime()
    db.trip_circuit_breaker(
        "KRAKEN_API", "KRAKEN_PUBLIC:EGeneral:Internal error",
        {"cycle_id": "old-public-internal-error"},
    )
    db.trip_circuit_breaker("ORDER_SNAPSHOT", "ORDER_RESPONSE_SHAPE_INVALID", {})

    recovered = _recover_transient_public_network_breaker(
        db, audit, cycle_id="recovery-cycle", system_status="online",
        instruments_count=615, initial_ticker_count=615, refreshed_ticker_count=12,
        expected_candidate_count=12, credentials_present=True,
        api_permissions_ready=True, history_ready_for_entries=True,
        history_evidence_gaps=[], account_snapshot_available=True,
    )

    assert recovered is True
    assert [(row["name"], row["reason"]) for row in db.circuit_breakers()] == [
        ("ORDER_SNAPSHOT", "ORDER_RESPONSE_SHAPE_INVALID")
    ]
    audit.close()
    db.close()


def test_exact_kraken_internal_error_breaker_stays_when_candidate_refresh_is_partial():
    _runtime, db, audit = make_runtime()
    db.trip_circuit_breaker(
        "KRAKEN_API", "KRAKEN_PUBLIC:EGeneral:Internal error", {}
    )

    recovered = _recover_transient_public_network_breaker(
        db, audit, cycle_id="partial-cycle", system_status="online",
        instruments_count=615, initial_ticker_count=615, refreshed_ticker_count=11,
        expected_candidate_count=12, credentials_present=True,
        api_permissions_ready=True, history_ready_for_entries=True,
        history_evidence_gaps=[], account_snapshot_available=True,
    )

    assert recovered is False
    assert [(row["name"], row["reason"]) for row in db.circuit_breakers()] == [
        ("KRAKEN_API", "KRAKEN_PUBLIC:EGeneral:Internal error")
    ]
    audit.close()
    db.close()


def test_market_data_empty_breaker_recovers_only_after_every_candidate_quote_refreshes():
    _runtime, db, audit = make_runtime()
    db.trip_circuit_breaker(
        "MARKET_DATA", "MARKET_TICKER_REFRESH_EMPTY", {"cycle_id": "old-empty-snapshot"}
    )
    db.trip_circuit_breaker("ORDER_SNAPSHOT", "ORDER_RESPONSE_SHAPE_INVALID", {})

    recovered = _recover_transient_market_data_breaker(
        db, audit, cycle_id="complete-market-cycle",
        initial_ticker_count=615, candidate_count=12,
        refreshed_candidate_count=12, invalid_candidate_count=0,
        account_snapshot_available=True,
    )

    assert recovered is True
    assert [(row["name"], row["reason"]) for row in db.circuit_breakers()] == [
        ("ORDER_SNAPSHOT", "ORDER_RESPONSE_SHAPE_INVALID")
    ]
    audit.close()
    db.close()


def test_market_data_empty_breaker_stays_when_any_candidate_refresh_is_invalid():
    _runtime, db, audit = make_runtime()
    db.trip_circuit_breaker("MARKET_DATA", "MARKET_TICKER_REFRESH_EMPTY", {})

    recovered = _recover_transient_market_data_breaker(
        db, audit, cycle_id="partial-market-cycle",
        initial_ticker_count=615, candidate_count=12,
        refreshed_candidate_count=11, invalid_candidate_count=1,
        account_snapshot_available=True,
    )

    assert recovered is False
    assert [(row["name"], row["reason"]) for row in db.circuit_breakers()] == [
        ("MARKET_DATA", "MARKET_TICKER_REFRESH_EMPTY")
    ]
    audit.close()
    db.close()



def test_futures_readonly_runtime_checks_permissions_then_persists_separate_snapshot(monkeypatch):
    runtime, db, audit = make_runtime()
    runtime.settings = Settings.from_mapping({
        "kraken_futures_api_key": "future-public-key",
        "kraken_futures_api_secret": "future-private-secret",
        "futures_readonly_enabled": True,
    })

    class FakeFuturesClient:
        def __init__(self):
            self.calls = []

        def get_api_key_info(self):
            self.calls.append("api_key_info")
            return {
                "apiKey": "future-public-key",
                "accountUid": "account-uid-not-logged",
                "permissions": {"general": "READ_ONLY", "transfer": "NO_ACCESS"},
                "allowedCidrBlocks": [],
            }

        def get_accounts(self):
            self.calls.append("accounts")
            return {
                "accounts": {
                    "cash": {
                        "balances": {"eur": "2.5", "usd": "10"},
                        "type": "cashAccount",
                    },
                    "flex": {
                        "currencies": {
                            "EUR": {
                                "quantity": "100",
                                "value": "108",
                                "collateral_value": "108",
                                "available": "108",
                                "haircut": "0",
                                "conversion_spread": "0",
                            },
                            "USD": {
                                "quantity": "25",
                                "value": "25",
                                "collateralValue": "25",
                                "available": "25",
                                "haircut": "0",
                                "conversionSpread": "0",
                            },
                        },
                        "balanceValue": "133",
                        "portfolioValue": "133",
                        "collateralValue": "133",
                        "availableMargin": "133",
                        "marginEquity": "133",
                        "type": "multiCollateralMarginAccount",
                    },
                    "fi_xbtusd": {
                        "balances": {"xbt": "0.02"},
                        "currency": "xbt",
                        "type": "marginAccount",
                    },
                }
            }

        def get_open_positions(self):
            self.calls.append("open_positions")
            return {"openPositions": []}

        def get_open_orders(self):
            self.calls.append("open_orders")
            return {"openOrders": []}

        def get_fills(self):
            self.calls.append("fills")
            return {"fills": []}

        def get_leverage_preferences(self):
            self.calls.append("leverage_preferences")
            return {"leveragePreferences": []}

        def get_instruments(self):
            self.calls.append("instruments")
            return {"instruments": [{"symbol": "PF_XBTUSD"}]}

        def get_tickers(self):
            self.calls.append("tickers")
            return {"tickers": [{"symbol": "PF_XBTUSD"}]}

    fake_client = FakeFuturesClient()

    def futures_client_factory(**kwargs):
        assert kwargs["api_key"] == "future-public-key"
        assert kwargs["api_secret"] == "future-private-secret"
        assert kwargs["environment"] == "production_read_only"
        return fake_client

    monkeypatch.setattr("app.runtime.FuturesAPIClient", futures_client_factory)

    result = runtime._run_futures_readonly_snapshot("cycle-futures-1")

    assert result["status"] == "COMPLETE"
    assert result["snapshot_id"]
    assert fake_client.calls == [
        "api_key_info", "accounts", "open_positions", "open_orders",
        "fills", "leverage_preferences", "instruments", "tickers",
    ]
    latest = db.latest_futures_readonly_snapshot()
    assert latest is not None
    assert latest["environment"] == "production_read_only"
    assert latest["general_permission"] == "READ_ONLY"
    assert latest["transfer_permission"] == "NO_ACCESS"
    assert latest["counts"]["open_positions"] == 0
    assert latest["counts"]["account_wallet_count"] == 3
    assert latest["counts"]["collateral_currency_count"] == 2
    assert latest["counts"]["funded_collateral_currency_count"] == 2
    assert latest["counts"]["cash_currency_count"] == 2
    assert latest["counts"]["single_collateral_account_count"] == 1
    assert latest["counts"]["single_collateral_currency_count"] == 1
    assert latest["counts"]["account_type_counts"] == {
        "cashAccount": 1,
        "multiCollateralMarginAccount": 1,
        "marginAccount": 1,
    }
    assert "account-uid-not-logged" not in str(latest["data"])
    assert "future-private-secret" not in str(latest)
    assert "future-public-key" not in str(latest)
    assert latest["data"]["accounts"]["accounts"]["flex"]["currencies"]["EUR"]["quantity"] == "100"
    assert '"collateral_currency_count":2' in audit.stream.getvalue()
    assert '"cash_currency_count":2' in audit.stream.getvalue()
    assert '"available_margin_usd":"133"' in audit.stream.getvalue()
    assert latest["counts"]["account_schema_status"] == "FLEX_SCHEMA_RECOGNIZED"
    assert latest["counts"]["flex_account_metrics"]["available_margin_usd"] == "133"
    assert "FUTURES_READONLY_DATA_SYNC" in audit.stream.getvalue()
    audit.close()
    db.close()


def test_futures_readonly_runtime_rejects_full_access_key_before_account_queries(monkeypatch):
    runtime, db, audit = make_runtime()
    runtime.settings = Settings.from_mapping({
        "kraken_futures_api_key": "future-public-key",
        "kraken_futures_api_secret": "future-private-secret",
        "futures_readonly_enabled": True,
    })

    class UnsafeFuturesClient:
        def __init__(self, **kwargs):
            pass

        def get_api_key_info(self):
            return {
                "apiKey": "future-public-key",
                "accountUid": "account-uid",
                "permissions": {"general": "FULL_ACCESS", "transfer": "FULL_ACCESS"},
                "allowedCidrBlocks": [],
            }

        def get_accounts(self):
            raise AssertionError("private account endpoints must not run for a full-access key")

    monkeypatch.setattr("app.runtime.FuturesAPIClient", UnsafeFuturesClient)
    result = runtime._run_futures_readonly_snapshot("cycle-futures-unsafe")

    assert result["status"] == "BLOCKED_UNSAFE_API_KEY_PERMISSIONS"
    assert db.latest_futures_readonly_snapshot() is None
    assert "no_private_account_queries_sent" in audit.stream.getvalue()
    audit.close()
    db.close()


def test_futures_readonly_runtime_stays_disabled_by_default(monkeypatch):
    runtime, db, audit = make_runtime()

    def unexpected_client(**kwargs):
        raise AssertionError("disabled Futures integration must not initialize its client")

    monkeypatch.setattr("app.runtime.FuturesAPIClient", unexpected_client)
    result = runtime._run_futures_readonly_snapshot("cycle-futures-disabled")

    assert result == {
        "status": "DISABLED",
        "snapshot_id": None,
        "endpoint_error_count": 0,
    }
    audit.close()
    db.close()




def test_futures_demo_runtime_reads_only_and_persists_separate_snapshot(monkeypatch):
    runtime, db, audit = make_runtime()
    runtime.settings = Settings.from_mapping({
        "kraken_futures_demo_api_key": "demo-public-key",
        "kraken_futures_demo_api_secret": "demo-private-secret",
        "futures_demo_enabled": True,
    })

    class FakeFuturesDemoClient:
        def __init__(self):
            self.calls = []

        def get_accounts(self):
            self.calls.append("accounts")
            return {"accounts": {"flex": {
                "currencies": {
                    "EUR": {
                        "quantity": "10",
                        "value": "11",
                        "collateral_value": "11",
                        "available": "11",
                    }
                },
                "balanceValue": "11",
                "portfolioValue": "11",
                "collateralValue": "11",
                "availableMargin": "11",
                "marginEquity": "11",
                "type": "multiCollateralMarginAccount",
            }}}

        def get_open_positions(self):
            self.calls.append("open_positions")
            return {"openPositions": []}

        def get_open_orders(self):
            self.calls.append("open_orders")
            return {"openOrders": []}

        def get_fills(self):
            self.calls.append("fills")
            return {"fills": []}

        def get_leverage_preferences(self):
            self.calls.append("leverage_preferences")
            return {"leveragePreferences": []}

        def get_instruments(self):
            self.calls.append("instruments")
            return {"instruments": [{"symbol": "PF_XBTUSD"}]}

        def get_tickers(self):
            self.calls.append("tickers")
            return {"tickers": [{"symbol": "PF_XBTUSD"}]}

    fake_client = FakeFuturesDemoClient()

    def futures_demo_client_factory(**kwargs):
        assert kwargs["api_key"] == "demo-public-key"
        assert kwargs["api_secret"] == "demo-private-secret"
        assert kwargs["environment"] == "demo"
        return fake_client

    monkeypatch.setattr("app.runtime.FuturesAPIClient", futures_demo_client_factory)
    result = runtime._run_futures_demo_snapshot("cycle-futures-demo-1")

    assert result["status"] == "COMPLETE"
    assert result["snapshot_id"]
    assert fake_client.calls == [
        "accounts", "open_positions", "open_orders", "fills",
        "leverage_preferences", "instruments", "tickers",
    ]
    latest_demo = db.latest_futures_demo_snapshot()
    assert latest_demo is not None
    assert latest_demo["environment"] == "demo_read_only"
    assert latest_demo["general_permission"] == "UNVERIFIED"
    assert latest_demo["transfer_permission"] == "UNVERIFIED"
    assert latest_demo["counts"]["open_positions"] == 0
    assert db.latest_futures_readonly_snapshot() is None
    assert "demo-private-secret" not in str(latest_demo)
    assert "demo-public-key" not in str(latest_demo)
    logs = audit.stream.getvalue()
    assert '"event":"FUTURES_DEMO_DATA_SYNC"' in logs
    assert '"environment":"DEMO_READ_ONLY"' in logs
    assert '"demo_key_permission_check_supported":false' in logs
    assert '"orders_submitted":false' in logs
    assert '"account_mutations_sent":false' in logs
    audit.close()
    db.close()


def test_futures_demo_runtime_stays_disabled_by_default(monkeypatch):
    runtime, db, audit = make_runtime()

    def unexpected_client(**kwargs):
        raise AssertionError("disabled Futures Demo integration must not initialize its client")

    monkeypatch.setattr("app.runtime.FuturesAPIClient", unexpected_client)
    result = runtime._run_futures_demo_snapshot("cycle-futures-demo-disabled")

    assert result == {
        "status": "DISABLED",
        "snapshot_id": None,
        "endpoint_error_count": 0,
    }
    audit.close()
    db.close()


def test_futures_demo_runtime_blocks_enabled_but_missing_credentials(monkeypatch):
    runtime, db, audit = make_runtime()
    runtime.settings = Settings.from_mapping({"futures_demo_enabled": True})

    def unexpected_client(**kwargs):
        raise AssertionError("missing Demo credentials must block before network access")

    monkeypatch.setattr("app.runtime.FuturesAPIClient", unexpected_client)
    result = runtime._run_futures_demo_snapshot("cycle-futures-demo-no-credentials")

    assert result["status"] == "BLOCKED_MISSING_CREDENTIALS"
    assert result["endpoint_error_count"] == 1
    assert "demo-private-secret" not in audit.stream.getvalue()
    audit.close()
    db.close()

def test_futures_account_summary_keeps_cash_and_flex_collateral_separate():
    summary = _futures_account_summary({
        "accounts": {
            "cash": {
                "type": "cashAccount",
                "balances": {"eur": "100"},
            },
            "flex": {
                "type": "multiCollateralMarginAccount",
                "currencies": {
                    "EUR": {
                        "quantity": "0",
                        "value": "0",
                        "collateral_value": "0",
                        "available": "0",
                    },
                },
                "balanceValue": "0",
                "availableMargin": "0",
            },
        },
    })

    assert summary["account_schema_status"] == "FLEX_SCHEMA_RECOGNIZED"
    assert summary["collateral_currency_count"] == 1
    assert summary["funded_collateral_currency_count"] == 0
    assert summary["cash_currency_count"] == 1
    assert summary["cash_balance_details"] == {"EUR": "100"}
    assert summary["flex_account_metrics"]["available_margin_usd"] == "0"


def test_futures_account_summary_reports_missing_flex_wallet_without_claiming_zero_collateral():
    summary = _futures_account_summary({
        "accounts": {
            "cash": {
                "type": "cashAccount",
                "balances": {"eur": "100"},
            },
        },
    })

    assert summary["account_schema_status"] == "FLEX_WALLET_MISSING"
    assert summary["multi_collateral_wallet_present"] is False
    assert summary["collateral_currency_count"] is None
    assert summary["funded_collateral_currency_count"] is None


def test_futures_demo_signal_evaluation_uses_spot_crypto_intersection_and_never_orders(monkeypatch):
    runtime, db, audit = make_runtime()
    runtime.settings = Settings.from_mapping({
        "kraken_futures_demo_api_key": "demo-public-key",
        "kraken_futures_demo_api_secret": "demo-private-secret",
        "futures_demo_enabled": True,
    })
    now = int(time.time())
    last_open = (now // 3600) * 3600 - 3600
    candles = []
    for index in range(80):
        close = Decimal("100") * (Decimal("1.003") ** index)
        candles.append({
            "time": (last_open - (79 - index) * 3600) * 1000,
            "open": str(close),
            "high": str(close * Decimal("1.001")),
            "low": str(close * Decimal("0.999")),
            "close": str(close),
            "volume": "100",
        })

    db.record_futures_readonly_snapshot({
        "snapshot_id": "demo-market-snapshot",
        "captured_at": "2026-11-22T14:00:00+00:00",
        "environment": "demo_read_only",
        "general_permission": "UNVERIFIED",
        "transfer_permission": "UNVERIFIED",
        "result_status": "COMPLETE",
        "counts": {},
        "data": {
            "instruments": {"instruments": [
                {"symbol": "PF_XBTUSD", "tradeable": True},
                {"symbol": "PF_ETHUSD", "tradeable": True},
                {"symbol": "PF_AAPLXUSD", "tradeable": True},
                {"symbol": "PI_XBTUSD", "tradeable": True},
                {"symbol": "PF_XRPUSD", "tradeable": False},
            ]},
            "tickers": {"tickers": [
                {"symbol": "PF_XBTUSD", "volumeQuote": 900000, "bid": 999, "ask": 1001, "suspended": False},
                {"symbol": "PF_ETHUSD", "volumeQuote": 800000, "bid": 999, "ask": 1001, "suspended": False},
                {"symbol": "PF_AAPLXUSD", "volumeQuote": 700000, "bid": 999, "ask": 1001, "suspended": False},
                {"symbol": "PF_XRPUSD", "volumeQuote": 600000, "bid": 999, "ask": 1001, "suspended": False},
            ]},
        },
    })

    class FakeChartClient:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "demo-public-key"
            assert kwargs["api_secret"] == "demo-private-secret"
            assert kwargs["environment"] == "demo"
            self.calls = []

        def get_ohlc_candles(self, symbol, **kwargs):
            self.calls.append((symbol, kwargs))
            return {"candles": candles, "more_candles": False}

    fake_client = FakeChartClient(
        api_key="demo-public-key",
        api_secret="demo-private-secret",
        environment="demo",
    )

    def factory(**kwargs):
        return fake_client

    monkeypatch.setattr("app.runtime.FuturesAPIClient", factory)
    spot_instruments = [
        Instrument("XBTUSD", "XBTUSD", "XBT/USD", "XBT", "USD", "online", D("0.0001"), 8, 1, (2,), (2,)),
        Instrument("ETHUSD", "ETHUSD", "ETH/USD", "ETH", "USD", "online", D("0.001"), 8, 1, (2,), (2,)),
    ]

    result = runtime._evaluate_futures_demo_signals(
        "cycle-futures-demo-signals",
        spot_instruments,
        snapshot_id="demo-market-snapshot",
    )

    assert result["status"] == "COMPLETE"
    assert result["candidate_count"] == 2
    assert result["directional_signal_count"] == 2
    assert result["orders_created"] is False
    assert {call[0] for call in fake_client.calls} == {"PF_XBTUSD", "PF_ETHUSD"}
    assert all(call[1] == {"tick_type": "trade", "resolution": "1h", "count": 90} for call in fake_client.calls)
    events = [
        json.loads(line) for line in audit.stream.getvalue().splitlines()
        if line.strip() and '"event":"FUTURES_DEMO_SIGNAL_EVALUATED"' in line
    ]
    assert len(events) == 2
    for event in events:
        assert event["payload"]["direction"] == "LONG"
        assert event["payload"]["execution_allowed"] is False
        assert event["payload"]["order_created"] is False
        assert "FUTURES_EDGE_NOT_VALIDATED" in event["payload"]["execution_blockers"]
        assert "FUTURES_EXCHANGE_INITIAL_MARGIN_TIER_NOT_RECONCILED" in event["payload"]["execution_blockers"]
    assert "demo-private-secret" not in audit.stream.getvalue()
    audit.close()
    db.close()
