from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

_ALLOWED = {
    "INTENT_CREATED": {"PRECHECK_PASSED", "REJECTED", "BLOCKED", "SHADOW_ONLY"},
    "PRECHECK_PASSED": {"VALIDATING", "SUBMITTING", "REJECTED", "BLOCKED"},
    "VALIDATING": {"VALIDATED", "REJECTED", "UNKNOWN_RECONCILING"},
    "VALIDATED": {"BLOCKED"},
    "SUBMITTING": {"ACKNOWLEDGED", "REJECTED", "UNKNOWN_RECONCILING"},
    "ACKNOWLEDGED": {"OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "UNKNOWN_RECONCILING"},
    "OPEN": {"PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "UNKNOWN_RECONCILING"},
    "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCELED", "UNKNOWN_RECONCILING"},
    "UNKNOWN_RECONCILING": {"ACKNOWLEDGED", "OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "EXPIRED"},
    "FILLED": set(), "CANCELED": set(), "REJECTED": set(), "EXPIRED": set(), "BLOCKED": set(), "SHADOW_ONLY": set(),
}

def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

class Persistence:
    def __init__(self, path: str = "/data/state/kraken_trader.sqlite3") -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self.lock:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,name TEXT NOT NULL,payload_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_ts_idx ON events(ts);
                CREATE TABLE IF NOT EXISTS bars(pair TEXT NOT NULL,interval_minutes INTEGER NOT NULL,ts INTEGER NOT NULL,open TEXT NOT NULL,high TEXT NOT NULL,low TEXT NOT NULL,close TEXT NOT NULL,volume TEXT NOT NULL,PRIMARY KEY(pair,interval_minutes,ts));
                CREATE TABLE IF NOT EXISTS intents(intent_id TEXT PRIMARY KEY,decision_id TEXT NOT NULL UNIQUE,client_order_id TEXT NOT NULL UNIQUE,pair TEXT NOT NULL,action TEXT NOT NULL,side TEXT NOT NULL,volume TEXT NOT NULL,leverage TEXT,state TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS intents_state_idx ON intents(state);
                CREATE TABLE IF NOT EXISTS fills(trade_id TEXT PRIMARY KEY,order_id TEXT,pair TEXT NOT NULL,side TEXT NOT NULL,volume TEXT NOT NULL,price TEXT NOT NULL,fee TEXT,fee_asset TEXT,ts TEXT NOT NULL,verified INTEGER NOT NULL,raw_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ledger_events(ledger_id TEXT PRIMARY KEY,asset TEXT,amount TEXT,fee TEXT,event_type TEXT,ts TEXT,verified INTEGER NOT NULL,raw_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tax_events(event_id TEXT PRIMARY KEY,ts TEXT NOT NULL,year INTEGER NOT NULL,event_type TEXT NOT NULL,asset TEXT,quote_asset TEXT,quantity TEXT,value_eur TEXT,fee_amount TEXT,fee_asset TEXT,source_ref TEXT,verified INTEGER NOT NULL,manual_review INTEGER NOT NULL,payload_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS tax_year_idx ON tax_events(year);
                CREATE TABLE IF NOT EXISTS exchange_orders(
                    order_id TEXT PRIMARY KEY,
                    client_order_id TEXT,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pair TEXT,
                    side TEXT,
                    order_type TEXT,
                    volume TEXT,
                    volume_executed TEXT,
                    cost TEXT,
                    fee TEXT,
                    opened_at TEXT,
                    closed_at TEXT,
                    observed_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS exchange_orders_client_id_idx ON exchange_orders(client_order_id);
                CREATE TABLE IF NOT EXISTS account_snapshots(
                    snapshot_id TEXT PRIMARY KEY,
                    captured_at TEXT NOT NULL,
                    position_count INTEGER NOT NULL,
                    positions_shape_complete INTEGER NOT NULL,
                    reconciliation_complete INTEGER NOT NULL,
                    balance_json TEXT NOT NULL,
                    trade_balance_json TEXT NOT NULL,
                    open_orders_json TEXT NOT NULL,
                    closed_orders_json TEXT NOT NULL,
                    open_positions_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS account_snapshots_captured_idx ON account_snapshots(captured_at);
                CREATE TABLE IF NOT EXISTS exchange_position_snapshots(
                    snapshot_id TEXT NOT NULL,
                    position_txid TEXT NOT NULL,
                    pair TEXT,
                    side TEXT,
                    position_status TEXT,
                    volume TEXT,
                    volume_closed TEXT,
                    cost TEXT,
                    fee TEXT,
                    margin TEXT,
                    value TEXT,
                    net TEXT,
                    opened_at TEXT,
                    rollover_time TEXT,
                    terms TEXT,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id,position_txid)
                );
                CREATE TABLE IF NOT EXISTS history_sync_state(
                    source TEXT PRIMARY KEY,
                    start_epoch INTEGER NOT NULL,
                    end_epoch INTEGER NOT NULL,
                    next_offset INTEGER NOT NULL,
                    next_cursor TEXT,
                    expected_count INTEGER,
                    initial_backfill_complete INTEGER NOT NULL,
                    generation_complete INTEGER NOT NULL,
                    last_error TEXT,
                    quality_gaps_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history_export_recovery(
                    request_key TEXT PRIMARY KEY,
                    report_id TEXT UNIQUE,
                    description TEXT NOT NULL UNIQUE,
                    start_epoch INTEGER NOT NULL,
                    end_epoch INTEGER NOT NULL,
                    trade_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at_epoch INTEGER NOT NULL,
                    next_poll_epoch INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    download_attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS history_export_recovery_status_idx
                    ON history_export_recovery(status,next_poll_epoch);
                CREATE TABLE IF NOT EXISTS circuit_breakers(name TEXT PRIMARY KEY,active INTEGER NOT NULL,reason TEXT NOT NULL,ts TEXT NOT NULL,detail_json TEXT NOT NULL);
            """)
            export_recovery_columns = {
                str(row["name"]) for row in self.connection.execute("PRAGMA table_info(history_export_recovery)")
            }
            if "download_attempt_count" not in export_recovery_columns:
                self.connection.execute(
                    "ALTER TABLE history_export_recovery ADD COLUMN download_attempt_count INTEGER NOT NULL DEFAULT 0"
                )
            history_columns = {
                str(row["name"]) for row in self.connection.execute("PRAGMA table_info(history_sync_state)")
            }
            if "next_cursor" not in history_columns:
                self.connection.execute("ALTER TABLE history_sync_state ADD COLUMN next_cursor TEXT")
            if "quality_gaps_json" not in history_columns:
                self.connection.execute(
                    "ALTER TABLE history_sync_state ADD COLUMN quality_gaps_json TEXT NOT NULL DEFAULT '[]'"
                )
            self.connection.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version','7') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )

    def record_event(self, name: str, payload: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.connection.execute("INSERT INTO events(ts,name,payload_json) VALUES(?,?,?)", (_now(), name, _json(payload or {})))

    def next_nonce(self) -> int:
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute("SELECT value FROM schema_meta WHERE key='last_nonce'").fetchone()
                previous = int(row["value"]) if row else 0
                nonce = max(time.time_ns() // 1000, previous + 1)
                self.connection.execute("INSERT INTO schema_meta(key,value) VALUES('last_nonce',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(nonce),))
                self.connection.execute("COMMIT")
                return nonce
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def record_bar(self, pair: str, interval: int, ts: int, open_price: str,
                   high: str, low: str, close: str, volume: str) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT INTO bars(pair,interval_minutes,ts,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(pair,interval_minutes,ts) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume",
                (pair, interval, ts, open_price, high, low, close, volume),
            )

    def get_closes(self, pair: str, interval: int = 60, limit: int = 1000) -> list[str]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT close FROM bars WHERE pair=? AND interval_minutes=? ORDER BY ts DESC LIMIT ?",
                (pair, interval, max(1, limit)),
            ).fetchall()
        return [str(row["close"]) for row in reversed(rows)]

    def create_intent(self, intent: dict[str, Any], *, initial_state: str = "INTENT_CREATED") -> bool:
        required = ("intent_id", "decision_id", "client_order_id", "pair", "action", "side", "volume")
        if any(not intent.get(key) for key in required):
            raise ValueError("INTENT_REQUIRED_FIELD_MISSING")
        if initial_state not in {"INTENT_CREATED", "SHADOW_ONLY"}:
            raise ValueError("INTENT_INITIAL_STATE_INVALID")
        now = _now()
        with self.lock:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO intents(intent_id,decision_id,client_order_id,pair,action,side,volume,leverage,state,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (intent["intent_id"], intent["decision_id"], intent["client_order_id"], intent["pair"],
                 intent["action"], intent["side"], str(intent["volume"]),
                 str(intent["leverage"]) if intent.get("leverage") is not None else None,
                 initial_state, _json(intent), now, now),
            )
            return cursor.rowcount == 1

    def transition_intent(self, intent_id: str, new_state: str, detail: dict[str, Any] | None = None) -> None:
        with self.lock:
            row = self.connection.execute("SELECT state,payload_json FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
            if row is None:
                raise KeyError("INTENT_NOT_FOUND")
            old_state = str(row["state"])
            if new_state not in _ALLOWED.get(old_state, set()):
                raise ValueError("INVALID_ORDER_STATE_TRANSITION:" + old_state + "->" + new_state)
            payload = json.loads(row["payload_json"])
            payload["last_transition_detail"] = detail or {}
            self.connection.execute("UPDATE intents SET state=?,payload_json=?,updated_at=? WHERE intent_id=?",
                                    (new_state, _json(payload), _now(), intent_id))
            self.connection.execute("INSERT INTO events(ts,name,payload_json) VALUES(?,?,?)",
                                    (_now(), "ORDER_STATE_TRANSITION", _json({"intent_id": intent_id, "old_state": old_state, "new_state": new_state, "detail": detail or {}})))

    def intent_state(self, intent_id: str) -> str | None:
        with self.lock:
            row = self.connection.execute("SELECT state FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
        return str(row["state"]) if row else None

    def unresolved_intents(self) -> list[dict[str, Any]]:
        states = ("INTENT_CREATED","PRECHECK_PASSED","VALIDATING","SUBMITTING","ACKNOWLEDGED","OPEN","PARTIALLY_FILLED","UNKNOWN_RECONCILING")
        with self.lock:
            rows = self.connection.execute("SELECT * FROM intents WHERE state IN (?,?,?,?,?,?,?,?) ORDER BY created_at", states).fetchall()
        return [dict(row) for row in rows]

    def record_exchange_order(
        self, order_id: str, order: dict[str, Any], *, source: str
    ) -> bool:
        """Persist an observed Kraken open/closed order without claiming reconciliation."""
        normalized_source = source.upper()
        if normalized_source not in {"OPEN", "CLOSED"}:
            raise ValueError("EXCHANGE_ORDER_SOURCE_INVALID")
        normalized_id = str(order_id).strip()
        if not normalized_id or not isinstance(order, dict):
            raise ValueError("EXCHANGE_ORDER_ID_OR_PAYLOAD_INVALID")
        description = order.get("descr")
        if not isinstance(description, dict):
            description = {}
        client_order_id = order.get("cl_ord_id") or description.get("cl_ord_id")
        status = str(order.get("status", "unknown")).strip().lower() or "unknown"
        observed_at = _now()
        with self.lock:
            self.connection.execute(
                "INSERT INTO exchange_orders(order_id,client_order_id,source,status,pair,side,"
                "order_type,volume,volume_executed,cost,fee,opened_at,closed_at,observed_at,raw_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(order_id) DO UPDATE SET "
                "client_order_id=excluded.client_order_id,source=excluded.source,status=excluded.status,"
                "pair=excluded.pair,side=excluded.side,order_type=excluded.order_type,"
                "volume=excluded.volume,volume_executed=excluded.volume_executed,cost=excluded.cost,"
                "fee=excluded.fee,opened_at=excluded.opened_at,closed_at=excluded.closed_at,"
                "observed_at=excluded.observed_at,raw_json=excluded.raw_json",
                (
                    normalized_id,
                    str(client_order_id) if client_order_id else None,
                    normalized_source,
                    status,
                    description.get("pair") or order.get("pair"),
                    description.get("type") or order.get("type"),
                    description.get("ordertype"),
                    str(order["vol"]) if order.get("vol") is not None else None,
                    str(order["vol_exec"]) if order.get("vol_exec") is not None else None,
                    str(order["cost"]) if order.get("cost") is not None else None,
                    str(order["fee"]) if order.get("fee") is not None else None,
                    str(order["opentm"]) if order.get("opentm") is not None else None,
                    str(order["closetm"]) if order.get("closetm") is not None else None,
                    observed_at,
                    _json(order),
                ),
            )
            self.connection.execute(
                "INSERT INTO events(ts,name,payload_json) VALUES(?,?,?)",
                (
                    observed_at,
                    "EXCHANGE_ORDER_OBSERVED",
                    _json({
                        "order_id": normalized_id,
                        "client_order_id": client_order_id,
                        "source": normalized_source,
                        "status": status,
                    }),
                ),
            )
        return True

    def exchange_orders(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM exchange_orders ORDER BY observed_at,order_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_fill(self, trade: dict[str, Any]) -> bool:
        trade_id = str(trade.get("trade_id", ""))
        if not trade_id:
            raise ValueError("FILL_TRADE_ID_REQUIRED")
        with self.lock:
            cur = self.connection.execute(
                "INSERT OR IGNORE INTO fills(trade_id,order_id,pair,side,volume,price,fee,fee_asset,ts,verified,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (trade_id, trade.get("order_id"), str(trade.get("pair","")), str(trade.get("side","")),
                 str(trade.get("volume","0")), str(trade.get("price","0")), trade.get("fee"),
                 trade.get("fee_asset"), str(trade.get("ts",_now())), int(bool(trade.get("verified",False))), _json(trade)))
            return cur.rowcount == 1

    def upsert_fill_from_exchange(self, trade: dict[str, Any]) -> bool:
        """Upsert authoritative Kraken trade fields while keeping duplicate imports idempotent."""
        trade_id = str(trade.get("trade_id", "")).strip()
        if not trade_id:
            raise ValueError("FILL_TRADE_ID_REQUIRED")
        raw_json = _json(trade)
        columns = (
            trade.get("order_id"), str(trade.get("pair", "")), str(trade.get("side", "")),
            str(trade.get("volume", "0")), str(trade.get("price", "0")), trade.get("fee"),
            trade.get("fee_asset"), str(trade.get("ts", _now())),
            int(bool(trade.get("verified", False))), raw_json,
        )
        with self.lock:
            existing = self.connection.execute(
                "SELECT order_id,pair,side,volume,price,fee,fee_asset,ts,verified,raw_json "
                "FROM fills WHERE trade_id=?", (trade_id,)
            ).fetchone()
            if existing is not None and tuple(existing) == columns:
                return False
            self.connection.execute(
                "INSERT INTO fills(trade_id,order_id,pair,side,volume,price,fee,fee_asset,ts,verified,raw_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_id) DO UPDATE SET "
                "order_id=excluded.order_id,pair=excluded.pair,side=excluded.side,"
                "volume=excluded.volume,price=excluded.price,fee=excluded.fee,fee_asset=excluded.fee_asset,"
                "ts=excluded.ts,verified=excluded.verified,raw_json=excluded.raw_json",
                (trade_id, *columns),
            )
            return True

    def fills(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM fills ORDER BY ts,trade_id"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["raw"] = json.loads(str(item.pop("raw_json")))
            item["verified"] = bool(item["verified"])
            result.append(item)
        return result

    def record_ledger(self, entry: dict[str, Any]) -> bool:
        ledger_id = str(entry.get("ledger_id", ""))
        if not ledger_id:
            raise ValueError("LEDGER_ID_REQUIRED")
        with self.lock:
            cur = self.connection.execute(
                "INSERT OR IGNORE INTO ledger_events(ledger_id,asset,amount,fee,event_type,ts,verified,raw_json) VALUES(?,?,?,?,?,?,?,?)",
                (ledger_id, entry.get("asset"), entry.get("amount"), entry.get("fee"), entry.get("event_type"),
                 entry.get("ts"), int(bool(entry.get("verified",False))), _json(entry)))
            return cur.rowcount == 1

    def upsert_ledger_from_exchange(self, entry: dict[str, Any]) -> bool:
        """Upsert authoritative Kraken ledger fields and preserve idempotent repeat syncs."""
        ledger_id = str(entry.get("ledger_id", "")).strip()
        if not ledger_id:
            raise ValueError("LEDGER_ID_REQUIRED")
        columns = (
            entry.get("asset"), entry.get("amount"), entry.get("fee"), entry.get("event_type"),
            entry.get("ts"), int(bool(entry.get("verified", False))), _json(entry),
        )
        with self.lock:
            existing = self.connection.execute(
                "SELECT asset,amount,fee,event_type,ts,verified,raw_json "
                "FROM ledger_events WHERE ledger_id=?", (ledger_id,)
            ).fetchone()
            if existing is not None and tuple(existing) == columns:
                return False
            self.connection.execute(
                "INSERT INTO ledger_events(ledger_id,asset,amount,fee,event_type,ts,verified,raw_json) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(ledger_id) DO UPDATE SET "
                "asset=excluded.asset,amount=excluded.amount,fee=excluded.fee,event_type=excluded.event_type,"
                "ts=excluded.ts,verified=excluded.verified,raw_json=excluded.raw_json",
                (ledger_id, *columns),
            )
            return True

    def ledger_events(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM ledger_events ORDER BY ts,ledger_id"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["raw"] = json.loads(str(item.pop("raw_json")))
            item["verified"] = bool(item["verified"])
            result.append(item)
        return result

    def record_tax_event(self, event: dict[str, Any]) -> bool:
        event_id = str(event.get("event_id", ""))
        if not event_id:
            raise ValueError("TAX_EVENT_ID_REQUIRED")
        with self.lock:
            cur = self.connection.execute(
                "INSERT OR IGNORE INTO tax_events(event_id,ts,year,event_type,asset,quote_asset,quantity,value_eur,fee_amount,fee_asset,source_ref,verified,manual_review,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, str(event.get("ts",_now())), int(event.get("year",0)),
                 str(event.get("event_type","UNCLASSIFIED")), event.get("asset"), event.get("quote_asset"),
                 event.get("quantity"), event.get("value_eur"), event.get("fee_amount"), event.get("fee_asset"),
                 event.get("source_ref"), int(bool(event.get("verified",False))), int(bool(event.get("manual_review",True))), _json(event)))
            return cur.rowcount == 1

    def upsert_imported_tax_event(self, event: dict[str, Any]) -> bool:
        """Refresh exchange-owned tax evidence without blindly erasing prior enrichment.

        If the economic facts change, prior EUR valuation and verification are invalidated.
        If the exchange facts are unchanged, established FX/review enrichment is preserved.
        """
        event_id = str(event.get("event_id", "")).strip()
        if not event_id:
            raise ValueError("TAX_EVENT_ID_REQUIRED")
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM tax_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                return self.record_tax_event(event)

            stored = dict(row)
            existing_event = json.loads(str(stored["payload_json"]))
            existing_payload = existing_event.get("payload", {})
            incoming_payload = event.get("payload", {})
            if not isinstance(existing_payload, dict):
                existing_payload = {}
            if not isinstance(incoming_payload, dict):
                incoming_payload = {}

            source_columns = (
                "ts", "year", "event_type", "asset", "quote_asset", "quantity",
                "fee_amount", "fee_asset", "source_ref",
            )
            source_changed = any(
                str(stored.get(column)) != str(event.get(column))
                for column in source_columns
            )
            economic_payload_keys = (
                "pair", "side", "price", "margin_amount", "product_type", "related_ledger_ids",
            )
            source_changed = source_changed or any(
                existing_payload.get(key) != incoming_payload.get(key)
                for key in economic_payload_keys
                if key in existing_payload or key in incoming_payload
            )
            merged = {**existing_event, **event}
            merged["payload"] = {**existing_payload, **incoming_payload}
            if source_changed:
                merged["value_eur"] = None
                merged["verified"] = False
                merged["manual_review"] = True
            else:
                merged["value_eur"] = stored["value_eur"]
                merged["verified"] = bool(stored["verified"])
                merged["manual_review"] = bool(stored["manual_review"])

            old_canonical = _json(existing_event)
            new_canonical = _json(merged)
            if old_canonical == new_canonical:
                return False
            self.connection.execute(
                "UPDATE tax_events SET ts=?,year=?,event_type=?,asset=?,quote_asset=?,quantity=?,"
                "value_eur=?,fee_amount=?,fee_asset=?,source_ref=?,verified=?,manual_review=?,payload_json=? "
                "WHERE event_id=?",
                (
                    str(merged.get("ts", _now())), int(merged.get("year", 0)),
                    str(merged.get("event_type", "UNCLASSIFIED")), merged.get("asset"),
                    merged.get("quote_asset"), merged.get("quantity"), merged.get("value_eur"),
                    merged.get("fee_amount"), merged.get("fee_asset"), merged.get("source_ref"),
                    int(bool(merged.get("verified", False))), int(bool(merged.get("manual_review", True))),
                    new_canonical, event_id,
                ),
            )
            return True

    def update_tax_event(self, event: dict[str, Any]) -> bool:
        """Update a previously imported tax event after authoritative enrichment.

        The import path remains insert-only/idempotent so routine history backfills
        cannot overwrite a verified FX valuation or manual-review decision. Enrichment
        is an explicit operation that updates an existing event only.
        """
        event_id = str(event.get("event_id", "")).strip()
        if not event_id:
            raise ValueError("TAX_EVENT_ID_REQUIRED")
        try:
            year = int(event.get("year", 0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("TAX_EVENT_YEAR_INVALID") from exc
        if year <= 0:
            raise ValueError("TAX_EVENT_YEAR_INVALID")
        timestamp = str(event.get("ts", "")).strip()
        if not timestamp:
            raise ValueError("TAX_EVENT_TIMESTAMP_REQUIRED")
        event_type = str(event.get("event_type", "")).strip()
        if not event_type:
            raise ValueError("TAX_EVENT_TYPE_REQUIRED")

        with self.lock:
            cur = self.connection.execute(
                "UPDATE tax_events SET ts=?,year=?,event_type=?,asset=?,quote_asset=?,"
                "quantity=?,value_eur=?,fee_amount=?,fee_asset=?,source_ref=?,verified=?,"
                "manual_review=?,payload_json=? WHERE event_id=?",
                (
                    timestamp, year, event_type, event.get("asset"), event.get("quote_asset"),
                    event.get("quantity"), event.get("value_eur"), event.get("fee_amount"),
                    event.get("fee_asset"), event.get("source_ref"),
                    int(bool(event.get("verified", False))),
                    int(bool(event.get("manual_review", True))), _json(event), event_id,
                ),
            )
            return cur.rowcount == 1

    def tax_events_for_year(self, year: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute("SELECT * FROM tax_events WHERE year=? ORDER BY ts,event_id", (year,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            serialized_event = json.loads(item.pop("payload_json"))
            # The row's typed columns are the canonical indexable fields. Expose
            # only the caller's audit payload here, not a duplicate nested copy
            # of the complete event envelope.
            event_payload = serialized_event.get("payload", {})
            item["payload"] = event_payload if isinstance(event_payload, dict) else {}
            result.append(item)
        return result

    def tax_event_years(self) -> list[int]:
        """Return all positive tax years with persisted source events."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT DISTINCT year FROM tax_events WHERE year > 0 ORDER BY year"
            ).fetchall()
        return [int(row["year"]) for row in rows]

    def record_account_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Persist raw account/position snapshots without asserting they reconcile."""
        required = ("captured_at", "balance", "trade_balance", "open_orders",
                    "closed_orders", "open_positions")
        if not isinstance(snapshot, dict) or any(key not in snapshot for key in required):
            raise ValueError("ACCOUNT_SNAPSHOT_REQUIRED_FIELD_MISSING")
        captured_at = str(snapshot["captured_at"]).strip()
        if not captured_at:
            raise ValueError("ACCOUNT_SNAPSHOT_TIMESTAMP_MISSING")
        for key in ("balance", "trade_balance", "open_orders", "open_positions"):
            if not isinstance(snapshot[key], dict):
                raise ValueError("ACCOUNT_SNAPSHOT_RESPONSE_SHAPE_INVALID:" + key)
        if snapshot["closed_orders"] is not None and not isinstance(snapshot["closed_orders"], dict):
            raise ValueError("ACCOUNT_SNAPSHOT_RESPONSE_SHAPE_INVALID:closed_orders")

        raw_positions = snapshot["open_positions"]
        wrapped = raw_positions.get("open")
        positions = wrapped if isinstance(wrapped, dict) else raw_positions
        positions_shape_complete = isinstance(positions, dict)
        normalized: list[tuple[str, dict[str, Any]]] = []
        required_position_fields = (
            "pair", "type", "posstatus", "vol", "vol_closed", "cost",
            "fee", "margin", "value", "net", "time",
        )
        for txid, row in positions.items():
            if not isinstance(row, dict) or not str(txid).strip():
                positions_shape_complete = False
                continue
            if any(field not in row for field in required_position_fields):
                positions_shape_complete = False
                continue
            side = str(row.get("type", "")).strip().lower()
            status = str(row.get("posstatus", "")).strip().lower()
            if (
                not str(row.get("pair", "")).strip()
                or side not in {"buy", "sell"}
                or status != "open"
            ):
                positions_shape_complete = False
                continue
            try:
                volume = Decimal(str(row["vol"]))
                closed = Decimal(str(row["vol_closed"]))
                cost = Decimal(str(row["cost"]))
                fee = Decimal(str(row["fee"]))
                margin = Decimal(str(row["margin"]))
                value = Decimal(str(row["value"]))
                net = Decimal(str(row["net"]))
                opened = Decimal(str(row["time"]))
                rollover_raw = row.get("rollovertm")
                rollover = Decimal(str(rollover_raw)) if rollover_raw not in (None, "") else None
            except (ArithmeticError, TypeError, ValueError):
                positions_shape_complete = False
                continue
            numeric = (volume, closed, cost, fee, margin, value, net, opened)
            if (
                any(not number.is_finite() for number in numeric)
                or (rollover is not None and (not rollover.is_finite() or rollover < 0))
                or volume <= 0 or closed < 0 or closed > volume
                or cost < 0 or fee < 0 or margin < 0 or value < 0 or opened < 0
            ):
                positions_shape_complete = False
                continue
            normalized.append((str(txid), row))

        snapshot_json = _json(snapshot)
        import hashlib
        snapshot_id = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        open_orders = snapshot["open_orders"].get("open", {})
        closed_orders = (
            snapshot["closed_orders"].get("closed", {})
            if isinstance(snapshot["closed_orders"], dict) else None
        )
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    "INSERT OR IGNORE INTO account_snapshots(snapshot_id,captured_at,position_count,"
                    "positions_shape_complete,reconciliation_complete,balance_json,trade_balance_json,"
                    "open_orders_json,closed_orders_json,open_positions_json,raw_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id, captured_at, len(normalized), int(positions_shape_complete), 0,
                        _json(snapshot["balance"]), _json(snapshot["trade_balance"]),
                        _json(open_orders), _json(closed_orders), _json(raw_positions), snapshot_json,
                    ),
                )
                for txid, row in normalized:
                    self.connection.execute(
                        "INSERT OR REPLACE INTO exchange_position_snapshots(snapshot_id,position_txid,pair,side,"
                        "position_status,volume,volume_closed,cost,fee,margin,value,net,opened_at,"
                        "rollover_time,terms,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            snapshot_id, txid, row.get("pair"), row.get("type"),
                            row.get("posstatus"), str(row["vol"]) if row.get("vol") is not None else None,
                            str(row["vol_closed"]) if row.get("vol_closed") is not None else None,
                            str(row["cost"]) if row.get("cost") is not None else None,
                            str(row["fee"]) if row.get("fee") is not None else None,
                            str(row["margin"]) if row.get("margin") is not None else None,
                            str(row["value"]) if row.get("value") is not None else None,
                            str(row["net"]) if row.get("net") is not None else None,
                            str(row["time"]) if row.get("time") is not None else None,
                            str(row["rollovertm"]) if row.get("rollovertm") is not None else None,
                            str(row.get("terms", "")), _json(row),
                        ),
                    )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
        return {
            "snapshot_id": snapshot_id,
            "captured_at": captured_at,
            "position_count": len(normalized),
            "positions_shape_complete": positions_shape_complete,
            "reconciliation_complete": False,
        }

    def latest_account_snapshot(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM account_snapshots ORDER BY captured_at DESC,snapshot_id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field in (
            "balance_json", "trade_balance_json", "open_orders_json",
            "closed_orders_json", "open_positions_json", "raw_json",
        ):
            parsed = json.loads(str(result.pop(field)))
            result[field.removesuffix("_json")] = parsed
        result["positions_shape_complete"] = bool(result["positions_shape_complete"])
        result["reconciliation_complete"] = bool(result["reconciliation_complete"])
        return result

    def latest_exchange_position_snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT p.* FROM exchange_position_snapshots p "
                "JOIN account_snapshots a ON a.snapshot_id=p.snapshot_id "
                "WHERE a.snapshot_id=(SELECT snapshot_id FROM account_snapshots "
                "ORDER BY captured_at DESC,snapshot_id DESC LIMIT 1) "
                "ORDER BY p.opened_at,p.position_txid"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["raw"] = json.loads(str(item.pop("raw_json")))
            result.append(item)
        return result

    def history_sync_state(self, source: str) -> dict[str, Any] | None:
        normalized = source.upper()
        with self.lock:
            row = self.connection.execute(
                "SELECT source,start_epoch,end_epoch,next_offset,next_cursor,expected_count,"
                "initial_backfill_complete,generation_complete,last_error,quality_gaps_json,updated_at "
                "FROM history_sync_state WHERE source=?",
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["initial_backfill_complete"] = bool(result["initial_backfill_complete"])
        result["generation_complete"] = bool(result["generation_complete"])
        try:
            quality_gaps = json.loads(str(result.get("quality_gaps_json", "[]")))
        except (TypeError, ValueError, json.JSONDecodeError):
            quality_gaps = ["HISTORY_QUALITY_GAP_STATE_INVALID"]
        if not isinstance(quality_gaps, list) or any(
            not isinstance(item, str) or not item.strip() for item in quality_gaps
        ):
            quality_gaps = ["HISTORY_QUALITY_GAP_STATE_INVALID"]
        result["quality_gaps"] = sorted(set(quality_gaps))
        return result

    def save_history_sync_state(self, state: dict[str, Any]) -> None:
        source = str(state["source"]).upper()
        if source not in {"TRADES", "LEDGERS", "CLOSED_ORDERS"}:
            raise ValueError("HISTORY_SOURCE_INVALID")
        start_epoch = int(state["start_epoch"])
        end_epoch = int(state["end_epoch"])
        next_offset = int(state["next_offset"])
        expected = state.get("expected_count")
        if start_epoch < 0 or end_epoch < start_epoch or next_offset < 0:
            raise ValueError("HISTORY_SYNC_STATE_INVALID")
        if expected is not None and int(expected) < 0:
            raise ValueError("HISTORY_SYNC_EXPECTED_COUNT_INVALID")
        raw_quality_gaps = state.get("quality_gaps", [])
        if not isinstance(raw_quality_gaps, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw_quality_gaps
        ):
            raise ValueError("HISTORY_SYNC_QUALITY_GAPS_INVALID")
        quality_gaps = sorted(set(raw_quality_gaps))
        with self.lock:
            self.connection.execute(
                "INSERT INTO history_sync_state(source,start_epoch,end_epoch,next_offset,next_cursor,expected_count,"
                "initial_backfill_complete,generation_complete,last_error,quality_gaps_json,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
                "start_epoch=excluded.start_epoch,end_epoch=excluded.end_epoch,"
                "next_offset=excluded.next_offset,next_cursor=excluded.next_cursor,expected_count=excluded.expected_count,"
                "initial_backfill_complete=excluded.initial_backfill_complete,"
                "generation_complete=excluded.generation_complete,last_error=excluded.last_error,"
                "quality_gaps_json=excluded.quality_gaps_json,updated_at=excluded.updated_at",
                (
                    source, start_epoch, end_epoch, next_offset,
                    str(state["next_cursor"]) if state.get("next_cursor") else None,
                    int(expected) if expected is not None else None,
                    int(bool(state.get("initial_backfill_complete", False))),
                    int(bool(state.get("generation_complete", False))),
                    state.get("last_error"), _json(quality_gaps), _now(),
                ),
            )

    def history_export_recovery_requests(self) -> list[dict[str, Any]]:
        """Return durable Kraken trade-export recovery jobs without exposing file contents."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT request_key,report_id,description,start_epoch,end_epoch,trade_ids_json,"
                "status,requested_at_epoch,next_poll_epoch,attempt_count,download_attempt_count,last_error,updated_at "
                "FROM history_export_recovery ORDER BY requested_at_epoch,request_key"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                trade_ids = json.loads(str(item.pop("trade_ids_json")))
            except (TypeError, ValueError, json.JSONDecodeError):
                trade_ids = []
                item["last_error"] = "EXPORT_RECOVERY_TRADE_IDS_STATE_INVALID"
            if not isinstance(trade_ids, list) or any(
                not isinstance(value, str) or not value.strip() for value in trade_ids
            ):
                trade_ids = []
                item["last_error"] = "EXPORT_RECOVERY_TRADE_IDS_STATE_INVALID"
            item["trade_ids"] = list(dict.fromkeys(value.strip() for value in trade_ids))
            result.append(item)
        return result

    def save_history_export_recovery_request(self, request: dict[str, Any]) -> None:
        """Persist export recovery progress before and after every asynchronous API step."""
        request_key = str(request.get("request_key", "")).strip()
        description = str(request.get("description", "")).strip()
        start_epoch = int(request.get("start_epoch", -1))
        end_epoch = int(request.get("end_epoch", -1))
        trade_ids = request.get("trade_ids")
        status = str(request.get("status", "")).strip().upper()
        if not request_key or not description or not status:
            raise ValueError("EXPORT_RECOVERY_REQUIRED_FIELD_MISSING")
        if start_epoch < 0 or end_epoch < start_epoch:
            raise ValueError("EXPORT_RECOVERY_TIME_WINDOW_INVALID")
        if not isinstance(trade_ids, list) or not trade_ids or any(
            not isinstance(value, str) or not value.strip() for value in trade_ids
        ):
            raise ValueError("EXPORT_RECOVERY_TRADE_IDS_INVALID")
        raw_report_id = request.get("report_id")
        report_id: str | None = None
        if raw_report_id is not None:
            candidate_report_id = str(raw_report_id).strip()
            if candidate_report_id:
                report_id = candidate_report_id
        requested_at_epoch = int(request.get("requested_at_epoch", time.time()))
        next_poll_epoch = int(request.get("next_poll_epoch", requested_at_epoch))
        attempt_count = int(request.get("attempt_count", 1))
        download_attempt_count = int(request.get("download_attempt_count", 0))
        if (
            requested_at_epoch < 0 or next_poll_epoch < 0 or attempt_count < 1
            or download_attempt_count < 0 or download_attempt_count > 100
        ):
            raise ValueError("EXPORT_RECOVERY_RETRY_METADATA_INVALID")
        with self.lock:
            self.connection.execute(
                "INSERT INTO history_export_recovery(request_key,report_id,description,start_epoch,end_epoch,"
                "trade_ids_json,status,requested_at_epoch,next_poll_epoch,attempt_count,download_attempt_count,last_error,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_key) DO UPDATE SET "
                "report_id=COALESCE(excluded.report_id,history_export_recovery.report_id),"
                "status=excluded.status,next_poll_epoch=excluded.next_poll_epoch,"
                "attempt_count=excluded.attempt_count,download_attempt_count=excluded.download_attempt_count,"
                "last_error=excluded.last_error,updated_at=excluded.updated_at",
                (
                    request_key, report_id, description, start_epoch, end_epoch,
                    _json(list(dict.fromkeys(value.strip() for value in trade_ids))),
                    status, requested_at_epoch, next_poll_epoch, attempt_count,
                    download_attempt_count, request.get("last_error"), _now(),
                ),
            )

    def trip_circuit_breaker(self, name: str, reason: str, detail: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT INTO circuit_breakers(name,active,reason,ts,detail_json) VALUES(?,1,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET active=1,reason=excluded.reason,ts=excluded.ts,detail_json=excluded.detail_json",
                (name, reason, _now(), _json(detail or {})))
            self.record_event("CIRCUIT_BREAKER_TRIPPED", {"name": name, "reason": reason, "detail": detail or {}})

    def circuit_breakers(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute("SELECT name,active,reason,ts,detail_json FROM circuit_breakers WHERE active=1").fetchall()
        return [{"name": r["name"], "active": bool(r["active"]), "reason": r["reason"],
                 "ts": r["ts"], "detail": json.loads(r["detail_json"])} for r in rows]

    def close(self) -> None:
        with self.lock:
            self.connection.close()
