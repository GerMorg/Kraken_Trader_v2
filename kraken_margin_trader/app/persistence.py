from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

_ALLOWED = {
    "INTENT_CREATED": {"PRECHECK_PASSED", "REJECTED", "BLOCKED"},
    "PRECHECK_PASSED": {"VALIDATING", "SUBMITTING", "REJECTED", "BLOCKED"},
    "VALIDATING": {"VALIDATED", "REJECTED", "UNKNOWN_RECONCILING"},
    "VALIDATED": {"BLOCKED"},
    "SUBMITTING": {"ACKNOWLEDGED", "REJECTED", "UNKNOWN_RECONCILING"},
    "ACKNOWLEDGED": {"OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "UNKNOWN_RECONCILING"},
    "OPEN": {"PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "UNKNOWN_RECONCILING"},
    "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCELED", "UNKNOWN_RECONCILING"},
    "UNKNOWN_RECONCILING": {"ACKNOWLEDGED", "OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "EXPIRED"},
    "FILLED": set(), "CANCELED": set(), "REJECTED": set(), "EXPIRED": set(), "BLOCKED": set(),
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
                CREATE TABLE IF NOT EXISTS circuit_breakers(name TEXT PRIMARY KEY,active INTEGER NOT NULL,reason TEXT NOT NULL,ts TEXT NOT NULL,detail_json TEXT NOT NULL);
            """)
            self.connection.execute("INSERT OR IGNORE INTO schema_meta(key,value) VALUES('schema_version','1')")

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

    def create_intent(self, intent: dict[str, Any]) -> bool:
        required = ("intent_id", "decision_id", "client_order_id", "pair", "action", "side", "volume")
        if any(not intent.get(key) for key in required):
            raise ValueError("INTENT_REQUIRED_FIELD_MISSING")
        now = _now()
        with self.lock:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO intents(intent_id,decision_id,client_order_id,pair,action,side,volume,leverage,state,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (intent["intent_id"], intent["decision_id"], intent["client_order_id"], intent["pair"],
                 intent["action"], intent["side"], str(intent["volume"]),
                 str(intent["leverage"]) if intent.get("leverage") is not None else None,
                 "INTENT_CREATED", _json(intent), now, now),
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
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

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
