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


def test_validate_shadow_order_calls_central_authority_with_validate_only(monkeypatch):
    from app.models import Action, Instrument
    from decimal import Decimal
    import app.runtime as runtime_module

    runtime, db, audit = make_runtime()
    runtime.settings = Settings.from_mapping({
        "mode": "SHADOW", "validate_shadow_orders": True,
        "kill_switch": True, "live_enabled": False,
    })
    instrument = Instrument(
        "XXBTZUSD", "XBTUSD", "XBT/USD", "XBT", "USD", "online",
        Decimal("0.0001"), 8, 1, (2, 3), (2, 3),
    )
    calls = []

    class FakeAuthority:
        def __init__(self, client, persistence, settings, audit_logger):
            assert client is runtime.client
            assert persistence is db
            assert settings.validate_shadow_orders is True
            assert audit_logger is audit

        def execute(self, order, selected_instrument, *, prechecks_passed, validate_only):
            calls.append((order, selected_instrument, prechecks_passed, validate_only))
            return {"state": "VALIDATED"}

    monkeypatch.setattr(runtime_module, "OrderAuthority", FakeAuthority)
    intent = {
        "intent_id": "shadow-validation-id",
        "decision_id": "cycle:XXBTZUSD:LONG",
        "client_order_id": "shadow-client-id",
        "pair": "XXBTZUSD",
        "action": Action.OPEN_MARGIN_LONG.value,
        "side": "LONG",
        "volume": "0.01",
        "leverage": 2,
        "product_type": "SPOT_MARGIN",
        "decision_evidence": {"net_edge_is_uncalibrated_proxy": True},
    }

    result = runtime._validate_shadow_order(intent, instrument)

    assert result == {"state": "VALIDATED"}
    assert len(calls) == 1
    order, selected, prechecks_passed, validate_only = calls[0]
    assert order.action == Action.OPEN_MARGIN_LONG
    assert order.volume == Decimal("0.01")
    assert selected is instrument
    assert prechecks_passed is True
    assert validate_only is True
    audit.close()
    db.close()



def test_runtime_probes_history_when_key_metadata_is_missing_and_keeps_position_monitoring(monkeypatch):
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
    assert '"action":"REDUCE_LONG"' in logs
    assert '"position_txid":"POSITION-1"' in logs
    assert "never-log-this" not in logs
    cycle_summaries = [
        json.loads(line) for line in logs.splitlines()
        if line.strip() and '"event":"CYCLE_SUMMARY"' in line
    ]
    assert cycle_summaries
    assert cycle_summaries[-1]["payload"]["internal_live_release"] is False
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
