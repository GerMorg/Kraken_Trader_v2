from __future__ import annotations

from datetime import datetime, timezone
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
    def __init__(self, path: str = "/data/kraken_trader.sqlite3") -> None:
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
                CREATE TABLE IF NOT EXISTS history_sync_state(
                    source TEXT PRIMARY KEY,
                    start_epoch INTEGER NOT NULL,
                    end_epoch INTEGER NOT NULL,
                    next_offset INTEGER NOT NULL,
                    expected_count INTEGER,
                    initial_backfill_complete INTEGER NOT NULL,
                    generation_complete INTEGER NOT NULL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS circuit_breakers(name TEXT PRIMARY KEY,active INTEGER NOT NULL,reason TEXT NOT NULL,ts TEXT NOT NULL,detail_json TEXT NOT NULL);
            """)
            self.connection.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version','2') "
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

    def history_sync_state(self, source: str) -> dict[str, Any] | None:
        normalized = source.upper()
        with self.lock:
            row = self.connection.execute(
                "SELECT source,start_epoch,end_epoch,next_offset,expected_count,"
                "initial_backfill_complete,generation_complete,last_error,updated_at "
                "FROM history_sync_state WHERE source=?",
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["initial_backfill_complete"] = bool(result["initial_backfill_complete"])
        result["generation_complete"] = bool(result["generation_complete"])
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
        with self.lock:
            self.connection.execute(
                "INSERT INTO history_sync_state(source,start_epoch,end_epoch,next_offset,expected_count,"
                "initial_backfill_complete,generation_complete,last_error,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
                "start_epoch=excluded.start_epoch,end_epoch=excluded.end_epoch,"
                "next_offset=excluded.next_offset,expected_count=excluded.expected_count,"
                "initial_backfill_complete=excluded.initial_backfill_complete,"
                "generation_complete=excluded.generation_complete,last_error=excluded.last_error,"
                "updated_at=excluded.updated_at",
                (
                    source, start_epoch, end_epoch, next_offset,
                    int(expected) if expected is not None else None,
                    int(bool(state.get("initial_backfill_complete", False))),
                    int(bool(state.get("generation_complete", False))),
                    state.get("last_error"), _now(),
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
