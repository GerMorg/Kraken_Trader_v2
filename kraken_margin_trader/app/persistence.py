from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
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
    SCHEMA_VERSION = 13

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
                CREATE TABLE IF NOT EXISTS circuit_breakers(name TEXT PRIMARY KEY,active INTEGER NOT NULL,reason TEXT NOT NULL,ts TEXT NOT NULL,detail_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS risk_equity_state(
                    state_key TEXT PRIMARY KEY,
                    trading_day TEXT NOT NULL,
                    day_start_equity TEXT NOT NULL,
                    day_start_epoch REAL NOT NULL,
                    high_water_equity TEXT NOT NULL,
                    last_equity TEXT NOT NULL,
                    last_snapshot_epoch REAL NOT NULL,
                    baseline_source TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_observations(
                    observation_id TEXT PRIMARY KEY,
                    pair TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL,
                    bar_ts INTEGER NOT NULL,
                    decision_ts INTEGER NOT NULL,
                    strategy_version TEXT NOT NULL,
                    fast_threshold_bps TEXT NOT NULL,
                    slow_threshold_bps TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    signal_reason TEXT NOT NULL,
                    momentum_fast_bps TEXT NOT NULL,
                    momentum_slow_bps TEXT NOT NULL,
                    signal_score TEXT NOT NULL,
                    trend_efficiency_24h TEXT,
                    trend_efficiency_72h TEXT,
                    stop_distance_pct TEXT NOT NULL,
                    entry_mid_price TEXT NOT NULL,
                    entry_spread_bps TEXT NOT NULL,
                    taker_fee_bps TEXT,
                    horizon_seconds INTEGER NOT NULL,
                    target_epoch INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    outcome_bar_ts INTEGER,
                    exit_close_price TEXT,
                    exit_spread_bps TEXT,
                    gross_return_bps TEXT,
                    spot_cost_estimate_bps TEXT,
                    spot_net_return_bps TEXT,
                    margin_cost_break_even_bps TEXT,
                    cost_quality TEXT,
                    target_open_reference_ts INTEGER,
                    target_open_price TEXT,
                    target_open_gross_return_bps TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(pair,interval_minutes,bar_ts,strategy_version,fast_threshold_bps,slow_threshold_bps,horizon_seconds)
                );
                CREATE INDEX IF NOT EXISTS research_pending_idx
                    ON research_observations(status,target_epoch);
                CREATE INDEX IF NOT EXISTS research_pair_target_idx
                    ON research_observations(pair,interval_minutes,target_epoch);
                CREATE INDEX IF NOT EXISTS research_decision_order_idx
                    ON research_observations(decision_ts DESC,observation_id DESC);
                CREATE TABLE IF NOT EXISTS strategy_candidate_observations(
                    candidate_id TEXT PRIMARY KEY,
                    pair TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL,
                    bar_ts INTEGER NOT NULL,
                    decision_ts INTEGER NOT NULL,
                    candidate_name TEXT NOT NULL,
                    candidate_version TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    signal_reason TEXT NOT NULL,
                    score TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    entry_mid_price TEXT NOT NULL,
                    entry_spread_bps TEXT NOT NULL,
                    taker_fee_bps TEXT,
                    horizon_seconds INTEGER NOT NULL,
                    target_epoch INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    outcome_bar_ts INTEGER,
                    exit_close_price TEXT,
                    exit_spread_bps TEXT,
                    gross_return_bps TEXT,
                    spot_cost_estimate_bps TEXT,
                    spot_net_return_bps TEXT,
                    cost_quality TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(pair,interval_minutes,bar_ts,candidate_name,candidate_version,horizon_seconds)
                );
                CREATE INDEX IF NOT EXISTS strategy_candidates_pending_idx
                    ON strategy_candidate_observations(status,target_epoch);
                CREATE INDEX IF NOT EXISTS strategy_candidates_pair_bar_idx
                    ON strategy_candidate_observations(pair,interval_minutes,bar_ts);
                CREATE INDEX IF NOT EXISTS strategy_candidate_decision_order_idx
                    ON strategy_candidate_observations(decision_ts DESC,candidate_id DESC);
                CREATE TABLE IF NOT EXISTS multi_asset_research_runs(
                    research_key TEXT PRIMARY KEY,
                    as_of_bar_ts INTEGER NOT NULL,
                    research_version TEXT NOT NULL,
                    symbols_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS multi_asset_research_asof_idx
                    ON multi_asset_research_runs(as_of_bar_ts DESC,research_key DESC);
                CREATE TABLE IF NOT EXISTS market_depth_snapshots(
                    pair TEXT NOT NULL,
                    snapshot_ts INTEGER NOT NULL,
                    research_bar_ts INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    bids_json TEXT NOT NULL,
                    asks_json TEXT NOT NULL,
                    taker_fee_bps TEXT,
                    maker_fee_bps TEXT,
                    spread_bps TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(pair,snapshot_ts)
                );
                CREATE INDEX IF NOT EXISTS market_depth_pair_ts_idx
                    ON market_depth_snapshots(pair,snapshot_ts DESC);
                CREATE TABLE IF NOT EXISTS microstructure_bars(
                    pair TEXT NOT NULL,
                    minute_ts INTEGER NOT NULL,
                    quality_status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(pair,minute_ts)
                );
                CREATE INDEX IF NOT EXISTS microstructure_pair_minute_idx
                    ON microstructure_bars(pair,minute_ts DESC);
            """)
            research_columns = {
                str(row["name"]) for row in self.connection.execute("PRAGMA table_info(research_observations)")
            }
            if "trend_efficiency_24h" not in research_columns:
                self.connection.execute(
                    "ALTER TABLE research_observations ADD COLUMN trend_efficiency_24h "
                    "TEXT"
                )
            if "trend_efficiency_72h" not in research_columns:
                self.connection.execute(
                    "ALTER TABLE research_observations ADD COLUMN trend_efficiency_72h "
                    "TEXT"
                )
            if "target_open_reference_ts" not in research_columns:
                self.connection.execute(
                    "ALTER TABLE research_observations ADD COLUMN target_open_reference_ts INTEGER"
                )
            if "target_open_price" not in research_columns:
                self.connection.execute(
                    "ALTER TABLE research_observations ADD COLUMN target_open_price TEXT"
                )
            if "target_open_gross_return_bps" not in research_columns:
                self.connection.execute(
                    "ALTER TABLE research_observations ADD COLUMN target_open_gross_return_bps TEXT"
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
                "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(self.SCHEMA_VERSION),),
            )


    def update_risk_equity_state(
        self,
        *,
        equity: Decimal,
        captured_epoch: Decimal,
        trading_day: str,
        external_flow_usd: Decimal = Decimal(0),
        external_flow_valued: bool = True,
    ) -> dict[str, Any]:
        """Persist a flow-adjusted daily baseline and a durable equity high-water mark.

        The first trustworthy snapshot starts measurement. It does not fabricate
        pre-installation daily PnL or historical drawdown. Unvalued external transfers
        explicitly rebase the measurement window instead of creating false PnL.
        """
        values = (equity, captured_epoch, external_flow_usd)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
            raise ValueError("RISK_EQUITY_STATE_NON_FINITE")
        if equity <= 0 or captured_epoch < 0 or not trading_day.strip():
            raise ValueError("RISK_EQUITY_STATE_OUT_OF_RANGE")

        now = _now()
        with self.lock:
            row = self.connection.execute(
                "SELECT trading_day,day_start_equity,day_start_epoch,high_water_equity,"
                "last_snapshot_epoch,baseline_source "
                "FROM risk_equity_state WHERE state_key='ACCOUNT_EQUITY'"
            ).fetchone()
            if row is None:
                day_start = equity
                day_start_epoch = captured_epoch
                high_water = equity
                baseline_source = "INITIAL_VERIFIED_ACCOUNT_EQUITY"
            else:
                previous_day = str(row["trading_day"])
                day_start = Decimal(str(row["day_start_equity"]))
                day_start_epoch = Decimal(str(row["day_start_epoch"]))
                high_water = Decimal(str(row["high_water_equity"]))
                previous_epoch = Decimal(str(row["last_snapshot_epoch"]))
                if captured_epoch <= previous_epoch:
                    raise ValueError("RISK_EQUITY_STATE_TIMESTAMP_NOT_MONOTONIC")

                if not external_flow_valued:
                    # A non-USD deposit/withdrawal cannot be assigned a trustworthy
                    # USD value from a wallet ledger alone. Begin a transparent new
                    # measurement window rather than calling the flow trading PnL.
                    day_start = equity
                    day_start_epoch = captured_epoch
                    high_water = equity
                    baseline_source = "REBASED_AFTER_UNVALUED_EXTERNAL_FLOW"
                else:
                    if external_flow_usd != 0:
                        day_start += external_flow_usd
                        high_water += external_flow_usd
                        if day_start <= 0 or high_water <= 0:
                            day_start = equity
                            day_start_epoch = captured_epoch
                            high_water = equity
                            baseline_source = "REBASED_AFTER_NON_POSITIVE_FLOW_ADJUSTMENT"
                        else:
                            baseline_source = "FLOW_ADJUSTED_PERSISTED_BASELINE"
                    else:
                        baseline_source = "PERSISTED_EQUITY_BASELINE"

                    if previous_day != trading_day:
                        # At day rollover use the first available equity snapshot of
                        # the new local day; don't reuse yesterday's PnL as today's.
                        day_start = equity
                        day_start_epoch = captured_epoch
                        baseline_source = "FIRST_VERIFIED_SNAPSHOT_OF_LOCAL_DAY"

                high_water = max(high_water, equity)

            daily_pnl = (
                (equity - day_start) / day_start * Decimal(100)
                if day_start > 0 else None
            )
            drawdown = (
                (high_water - equity) / high_water * Decimal(100)
                if high_water > 0 else None
            )
            self.connection.execute(
                "INSERT INTO risk_equity_state("
                "state_key,trading_day,day_start_equity,day_start_epoch,high_water_equity,"
                "last_equity,last_snapshot_epoch,baseline_source,updated_at"
                ") VALUES('ACCOUNT_EQUITY',?,?,?,?,?,?,?,?) "
                "ON CONFLICT(state_key) DO UPDATE SET "
                "trading_day=excluded.trading_day,day_start_equity=excluded.day_start_equity,"
                "day_start_epoch=excluded.day_start_epoch,high_water_equity=excluded.high_water_equity,"
                "last_equity=excluded.last_equity,last_snapshot_epoch=excluded.last_snapshot_epoch,"
                "baseline_source=excluded.baseline_source,updated_at=excluded.updated_at",
                (
                    trading_day, str(day_start), float(day_start_epoch), str(high_water),
                    str(equity), float(captured_epoch), baseline_source, now,
                ),
            )

        return {
            "status": "BASELINE_UPDATED",
            "trading_day": trading_day,
            "baseline_source": baseline_source,
            "day_start_equity": str(day_start),
            "high_water_equity": str(high_water),
            "current_equity": str(equity),
            "daily_pnl_pct": daily_pnl,
            "drawdown_pct": drawdown,
            "measurement_window_start_epoch": str(day_start_epoch),
            "historical_pnl_before_baseline_known": False,
            "external_flow_adjusted": bool(external_flow_valued),
        }

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

    def record_research_observation(self, observation: dict[str, Any]) -> bool:
        """Persist one immutable strategy observation per closed bar and strategy configuration."""
        required = (
            "observation_id", "pair", "interval_minutes", "bar_ts", "decision_ts",
            "strategy_version", "fast_threshold_bps", "slow_threshold_bps", "direction",
            "signal_reason", "momentum_fast_bps", "momentum_slow_bps", "signal_score",
            "stop_distance_pct", "entry_mid_price", "entry_spread_bps", "horizon_seconds",
            "target_epoch",
        )
        if any(key not in observation for key in required):
            raise ValueError("RESEARCH_OBSERVATION_REQUIRED_FIELD_MISSING")
        pair = str(observation["pair"]).strip()
        direction = str(observation["direction"]).upper()
        if not pair or direction not in {"LONG", "SHORT", "NONE"}:
            raise ValueError("RESEARCH_OBSERVATION_IDENTITY_INVALID")
        interval = int(observation["interval_minutes"])
        bar_ts = int(observation["bar_ts"])
        decision_ts = int(observation["decision_ts"])
        horizon_seconds = int(observation["horizon_seconds"])
        target_epoch = int(observation["target_epoch"])
        if (
            interval <= 0 or bar_ts < 0 or decision_ts < 0 or horizon_seconds <= 0
            or target_epoch <= decision_ts or target_epoch - decision_ts != horizon_seconds
        ):
            raise ValueError("RESEARCH_OBSERVATION_TIME_RANGE_INVALID")
        for field in (
            "fast_threshold_bps", "slow_threshold_bps", "momentum_fast_bps",
            "momentum_slow_bps", "signal_score", "trend_efficiency_24h",
            "trend_efficiency_72h", "stop_distance_pct", "entry_mid_price",
            "entry_spread_bps",
        ):
            raw_value = observation.get(field, "0") if field.startswith("trend_efficiency_") else observation[field]
            try:
                number = Decimal(str(raw_value))
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ValueError("RESEARCH_OBSERVATION_NUMERIC_FIELD_INVALID:" + field) from exc
            if not number.is_finite():
                raise ValueError("RESEARCH_OBSERVATION_NUMERIC_FIELD_NON_FINITE:" + field)
            if field.startswith("trend_efficiency_") and not Decimal(0) <= number <= Decimal(1):
                raise ValueError("RESEARCH_OBSERVATION_TREND_EFFICIENCY_OUT_OF_RANGE:" + field)
        if Decimal(str(observation["entry_mid_price"])) <= 0 or Decimal(str(observation["entry_spread_bps"])) < 0:
            raise ValueError("RESEARCH_OBSERVATION_ENTRY_QUOTE_INVALID")
        fee_value = observation.get("taker_fee_bps")
        if fee_value is not None:
            try:
                fee = Decimal(str(fee_value))
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ValueError("RESEARCH_OBSERVATION_TAKER_FEE_INVALID") from exc
            if not fee.is_finite() or fee < 0:
                raise ValueError("RESEARCH_OBSERVATION_TAKER_FEE_INVALID")
        now = _now()
        status = "PENDING" if direction in {"LONG", "SHORT"} else "NO_DIRECTION"
        key = (
            pair, interval, bar_ts, str(observation["strategy_version"]),
            str(observation["fast_threshold_bps"]), str(observation["slow_threshold_bps"]),
            horizon_seconds,
        )
        values = (
            str(observation["observation_id"]), pair, interval, bar_ts, decision_ts,
            str(observation["strategy_version"]), str(observation["fast_threshold_bps"]),
            str(observation["slow_threshold_bps"]), direction, str(observation["signal_reason"]),
            str(observation["momentum_fast_bps"]), str(observation["momentum_slow_bps"]),
            str(observation["signal_score"]),
            str(observation.get("trend_efficiency_24h", "0")),
            str(observation.get("trend_efficiency_72h", "0")),
            str(observation["stop_distance_pct"]), str(observation["entry_mid_price"]),
            str(observation["entry_spread_bps"]),
            str(fee_value) if fee_value is not None else None, horizon_seconds, target_epoch,
            status, now, now,
        )
        with self.lock:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO research_observations("
                "observation_id,pair,interval_minutes,bar_ts,decision_ts,strategy_version,"
                "fast_threshold_bps,slow_threshold_bps,direction,signal_reason,momentum_fast_bps,"
                "momentum_slow_bps,signal_score,trend_efficiency_24h,trend_efficiency_72h,"
                "stop_distance_pct,entry_mid_price,entry_spread_bps,taker_fee_bps,horizon_seconds,"
                "target_epoch,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            if cursor.rowcount == 0 and fee_value is not None:
                self.connection.execute(
                    "UPDATE research_observations SET taker_fee_bps=COALESCE(taker_fee_bps,?),updated_at=? "
                    "WHERE pair=? AND interval_minutes=? AND bar_ts=? AND strategy_version=? "
                    "AND fast_threshold_bps=? AND slow_threshold_bps=? AND horizon_seconds=? "
                    "AND status IN ('PENDING','NO_DIRECTION')",
                    (str(fee_value), now, *key),
                )
            return cursor.rowcount == 1

    def pending_research_observations(self, pair: str, interval_minutes: int = 60, limit: int = 500) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM research_observations WHERE status='PENDING' AND pair=? "
                "AND interval_minutes=? ORDER BY target_epoch,decision_ts LIMIT ?",
                (pair, interval_minutes, max(1, min(int(limit), 5000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def research_bar_covering_epoch(self, pair: str, interval_minutes: int, target_epoch: int) -> dict[str, Any] | None:
        """Return the stored closed candle whose interval contains target_epoch."""
        duration = int(interval_minutes) * 60
        with self.lock:
            row = self.connection.execute(
                "SELECT pair,interval_minutes,ts,open,high,low,close,volume FROM bars "
                "WHERE pair=? AND interval_minutes=? AND ts<=? AND ts+? >= ? "
                "ORDER BY ts DESC LIMIT 1",
                (pair, interval_minutes, int(target_epoch), duration, int(target_epoch)),
            ).fetchone()
        return dict(row) if row is not None else None

    def research_bar_open_at_target_epoch(
        self, pair: str, interval_minutes: int, target_epoch: int
    ) -> dict[str, Any] | None:
        """Return a stored candle whose open is at or before, and strictly covers, target_epoch."""
        duration = int(interval_minutes) * 60
        with self.lock:
            row = self.connection.execute(
                "SELECT pair,interval_minutes,ts,open,high,low,close,volume FROM bars "
                "WHERE pair=? AND interval_minutes=? AND ts<=? AND ts+? > ? "
                "ORDER BY ts DESC LIMIT 1",
                (pair, interval_minutes, int(target_epoch), duration, int(target_epoch)),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_bar_timestamp(self, pair: str, interval_minutes: int = 60) -> int | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT MAX(ts) AS latest_ts FROM bars WHERE pair=? AND interval_minutes=?",
                (pair, interval_minutes),
            ).fetchone()
        return int(row["latest_ts"]) if row is not None and row["latest_ts"] is not None else None

    def research_observations_missing_target_open(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Return settled directional labels whose separate target-open label is not yet recorded."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM research_observations "
                "WHERE status IN ('SETTLED_COST_ESTIMATE','SETTLED_GROSS_ONLY') "
                "AND direction IN ('LONG','SHORT') AND target_open_gross_return_bps IS NULL "
                "ORDER BY target_epoch,decision_ts LIMIT ?",
                (max(1, min(int(limit), 20000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_research_target_open_outcome(
        self,
        observation_id: str,
        *,
        reference_ts: int,
        reference_price: Decimal,
        gross_return_bps: Decimal,
    ) -> bool:
        """Add an audit-only earlier-at-or-before-target price label without changing the primary outcome."""
        if reference_ts < 0 or not reference_price.is_finite() or reference_price <= 0:
            raise ValueError("RESEARCH_TARGET_OPEN_REFERENCE_INVALID")
        if not gross_return_bps.is_finite():
            raise ValueError("RESEARCH_TARGET_OPEN_RETURN_INVALID")
        now = _now()
        with self.lock:
            cursor = self.connection.execute(
                "UPDATE research_observations SET target_open_reference_ts=?,target_open_price=?,"
                "target_open_gross_return_bps=?,updated_at=? "
                "WHERE observation_id=? AND target_open_gross_return_bps IS NULL "
                "AND status IN ('SETTLED_COST_ESTIMATE','SETTLED_GROSS_ONLY') "
                "AND direction IN ('LONG','SHORT')",
                (
                    int(reference_ts), str(reference_price), str(gross_return_bps),
                    now, str(observation_id),
                ),
            )
            return cursor.rowcount == 1

    def settle_research_observation(self, observation_id: str, outcome: dict[str, Any]) -> bool:
        required = (
            "status", "outcome_bar_ts", "exit_close_price", "exit_spread_bps",
            "gross_return_bps", "spot_cost_estimate_bps", "spot_net_return_bps",
            "margin_cost_break_even_bps", "cost_quality",
            "target_open_reference_ts", "target_open_price", "target_open_gross_return_bps",
        )
        if any(key not in outcome for key in required):
            raise ValueError("RESEARCH_OUTCOME_REQUIRED_FIELD_MISSING")
        if str(outcome["status"]) not in {"SETTLED_COST_ESTIMATE", "SETTLED_GROSS_ONLY", "INCOMPLETE_MISSING_TARGET_BAR"}:
            raise ValueError("RESEARCH_OUTCOME_STATUS_INVALID")
        reference_ts = outcome["target_open_reference_ts"]
        reference_price = outcome["target_open_price"]
        target_open_return = outcome["target_open_gross_return_bps"]
        if reference_ts is not None and int(reference_ts) < 0:
            raise ValueError("RESEARCH_TARGET_OPEN_REFERENCE_INVALID")
        for field, value in (
            ("target_open_price", reference_price),
            ("target_open_gross_return_bps", target_open_return),
        ):
            if value is not None:
                try:
                    number = Decimal(str(value))
                except (ArithmeticError, TypeError, ValueError) as exc:
                    raise ValueError("RESEARCH_TARGET_OPEN_NUMERIC_INVALID:" + field) from exc
                if not number.is_finite() or (field == "target_open_price" and number <= 0):
                    raise ValueError("RESEARCH_TARGET_OPEN_NUMERIC_INVALID:" + field)
        now = _now()
        with self.lock:
            cursor = self.connection.execute(
                "UPDATE research_observations SET status=?,outcome_bar_ts=?,exit_close_price=?,"
                "exit_spread_bps=?,gross_return_bps=?,spot_cost_estimate_bps=?,spot_net_return_bps=?,"
                "margin_cost_break_even_bps=?,cost_quality=?,target_open_reference_ts=?,"
                "target_open_price=?,target_open_gross_return_bps=?,updated_at=? "
                "WHERE observation_id=? AND status='PENDING'",
                (
                    str(outcome["status"]),
                    int(outcome["outcome_bar_ts"]) if outcome["outcome_bar_ts"] is not None else None,
                    str(outcome["exit_close_price"]) if outcome["exit_close_price"] is not None else None,
                    str(outcome["exit_spread_bps"]) if outcome["exit_spread_bps"] is not None else None,
                    str(outcome["gross_return_bps"]) if outcome["gross_return_bps"] is not None else None,
                    str(outcome["spot_cost_estimate_bps"]) if outcome["spot_cost_estimate_bps"] is not None else None,
                    str(outcome["spot_net_return_bps"]) if outcome["spot_net_return_bps"] is not None else None,
                    str(outcome["margin_cost_break_even_bps"]) if outcome["margin_cost_break_even_bps"] is not None else None,
                    str(outcome["cost_quality"]),
                    int(reference_ts) if reference_ts is not None else None,
                    str(reference_price) if reference_price is not None else None,
                    str(target_open_return) if target_open_return is not None else None,
                    now, str(observation_id),
                ),
            )
            return cursor.rowcount == 1

    def research_observations(self, limit: int = 5000) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM research_observations ORDER BY decision_ts DESC,observation_id DESC LIMIT ?",
                (max(1, min(int(limit), 20000)),),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def record_strategy_candidate_observation(self, observation: dict[str, Any]) -> bool:
        """Persist an immutable research-only strategy candidate observation."""
        required = (
            "candidate_id", "pair", "interval_minutes", "bar_ts", "decision_ts",
            "candidate_name", "candidate_version", "direction", "signal_reason",
            "score", "features", "entry_mid_price", "entry_spread_bps",
            "horizon_seconds", "target_epoch",
        )
        if any(key not in observation for key in required):
            raise ValueError("STRATEGY_CANDIDATE_REQUIRED_FIELD_MISSING")
        candidate_id = str(observation["candidate_id"]).strip()
        pair = str(observation["pair"]).strip()
        candidate_name = str(observation["candidate_name"]).strip()
        candidate_version = str(observation["candidate_version"]).strip()
        direction = str(observation["direction"]).upper()
        if (
            not candidate_id or not pair or not candidate_name or not candidate_version
            or direction not in {"LONG", "SHORT", "NONE"}
        ):
            raise ValueError("STRATEGY_CANDIDATE_IDENTITY_INVALID")
        try:
            interval = int(observation["interval_minutes"])
            bar_ts = int(observation["bar_ts"])
            decision_ts = int(observation["decision_ts"])
            horizon_seconds = int(observation["horizon_seconds"])
            target_epoch = int(observation["target_epoch"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("STRATEGY_CANDIDATE_TIME_INVALID") from exc
        if (
            interval <= 0 or bar_ts < 0 or decision_ts <= 0 or horizon_seconds <= 0
            or target_epoch - decision_ts != horizon_seconds
        ):
            raise ValueError("STRATEGY_CANDIDATE_TIME_INVALID")
        numeric_values: dict[str, Decimal] = {}
        for field in ("score", "entry_mid_price", "entry_spread_bps"):
            try:
                value = Decimal(str(observation[field]))
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ValueError("STRATEGY_CANDIDATE_NUMERIC_FIELD_INVALID:" + field) from exc
            if not value.is_finite():
                raise ValueError("STRATEGY_CANDIDATE_NUMERIC_FIELD_NON_FINITE:" + field)
            numeric_values[field] = value
        if numeric_values["entry_mid_price"] <= 0 or numeric_values["entry_spread_bps"] < 0:
            raise ValueError("STRATEGY_CANDIDATE_ENTRY_QUOTE_INVALID")
        fee_value = observation.get("taker_fee_bps")
        if fee_value is not None:
            try:
                fee = Decimal(str(fee_value))
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ValueError("STRATEGY_CANDIDATE_TAKER_FEE_INVALID") from exc
            if not fee.is_finite() or fee < 0:
                raise ValueError("STRATEGY_CANDIDATE_TAKER_FEE_INVALID")
        features = observation["features"]
        if not isinstance(features, dict):
            raise ValueError("STRATEGY_CANDIDATE_FEATURES_NOT_MAPPING")

        now = _now()
        status = "PENDING" if direction in {"LONG", "SHORT"} else "NO_DIRECTION"
        with self.lock:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO strategy_candidate_observations("
                "candidate_id,pair,interval_minutes,bar_ts,decision_ts,candidate_name,"
                "candidate_version,direction,signal_reason,score,features_json,entry_mid_price,"
                "entry_spread_bps,taker_fee_bps,horizon_seconds,target_epoch,status,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate_id, pair, interval, bar_ts, decision_ts, candidate_name,
                    candidate_version, direction, str(observation["signal_reason"]),
                    str(numeric_values["score"]), _json(features),
                    str(numeric_values["entry_mid_price"]), str(numeric_values["entry_spread_bps"]),
                    str(fee_value) if fee_value is not None else None, horizon_seconds,
                    target_epoch, status, now, now,
                ),
            )
            return cursor.rowcount == 1

    def strategy_candidate_bar_is_complete(
        self,
        pair: str,
        bar_ts: int,
        *,
        interval_minutes: int = 60,
        expected_candidate_count: int = 3,
    ) -> bool:
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(DISTINCT candidate_name || ':' || candidate_version) AS candidate_count "
                "FROM strategy_candidate_observations WHERE pair=? AND interval_minutes=? AND bar_ts=?",
                (str(pair), int(interval_minutes), int(bar_ts)),
            ).fetchone()
        return row is not None and int(row["candidate_count"]) >= expected_candidate_count

    def pending_strategy_candidate_observations(
        self, pair: str, interval_minutes: int = 60, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM strategy_candidate_observations WHERE status='PENDING' "
                "AND pair=? AND interval_minutes=? ORDER BY target_epoch,decision_ts LIMIT ?",
                (pair, interval_minutes, max(1, min(int(limit), 5000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def settle_strategy_candidate_observation(
        self, candidate_id: str, outcome: dict[str, Any]
    ) -> bool:
        required = (
            "status", "outcome_bar_ts", "exit_close_price", "exit_spread_bps",
            "gross_return_bps", "spot_cost_estimate_bps", "spot_net_return_bps",
            "cost_quality",
        )
        if any(key not in outcome for key in required):
            raise ValueError("STRATEGY_CANDIDATE_OUTCOME_REQUIRED_FIELD_MISSING")
        status = str(outcome["status"])
        if status not in {
            "SETTLED_COST_ESTIMATE", "SETTLED_GROSS_ONLY", "INCOMPLETE_MISSING_TARGET_BAR"
        }:
            raise ValueError("STRATEGY_CANDIDATE_OUTCOME_STATUS_INVALID")
        for field in (
            "exit_close_price", "exit_spread_bps", "gross_return_bps",
            "spot_cost_estimate_bps", "spot_net_return_bps",
        ):
            value = outcome[field]
            if value is None:
                continue
            try:
                parsed = Decimal(str(value))
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ValueError("STRATEGY_CANDIDATE_OUTCOME_NUMERIC_INVALID:" + field) from exc
            if not parsed.is_finite():
                raise ValueError("STRATEGY_CANDIDATE_OUTCOME_NUMERIC_INVALID:" + field)
            if field == "exit_close_price" and parsed <= 0:
                raise ValueError("STRATEGY_CANDIDATE_OUTCOME_NUMERIC_INVALID:" + field)
            if field == "exit_spread_bps" and parsed < 0:
                raise ValueError("STRATEGY_CANDIDATE_OUTCOME_NUMERIC_INVALID:" + field)
        now = _now()
        with self.lock:
            cursor = self.connection.execute(
                "UPDATE strategy_candidate_observations SET status=?,outcome_bar_ts=?,"
                "exit_close_price=?,exit_spread_bps=?,gross_return_bps=?,"
                "spot_cost_estimate_bps=?,spot_net_return_bps=?,cost_quality=?,updated_at=? "
                "WHERE candidate_id=? AND status='PENDING'",
                (
                    status,
                    int(outcome["outcome_bar_ts"]) if outcome["outcome_bar_ts"] is not None else None,
                    str(outcome["exit_close_price"]) if outcome["exit_close_price"] is not None else None,
                    str(outcome["exit_spread_bps"]) if outcome["exit_spread_bps"] is not None else None,
                    str(outcome["gross_return_bps"]) if outcome["gross_return_bps"] is not None else None,
                    str(outcome["spot_cost_estimate_bps"]) if outcome["spot_cost_estimate_bps"] is not None else None,
                    str(outcome["spot_net_return_bps"]) if outcome["spot_net_return_bps"] is not None else None,
                    str(outcome["cost_quality"]), now, str(candidate_id),
                ),
            )
            return cursor.rowcount == 1

    def strategy_candidate_observations(self, limit: int = 100000) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM strategy_candidate_observations "
                "ORDER BY decision_ts DESC,candidate_id DESC LIMIT ?",
                (max(1, min(int(limit), 100000)),),
            ).fetchall()
        result = [dict(row) for row in reversed(rows)]
        for row in result:
            try:
                row["features"] = json.loads(str(row.pop("features_json")))
            except (json.JSONDecodeError, TypeError, ValueError):
                row["features"] = {}
        return result

    def record_multi_asset_research_run(self, report: dict[str, Any]) -> bool:
        """Persist one immutable multi-asset research report per common closed-bar snapshot."""
        required = ("research_key", "as_of_bar_ts", "research_version", "symbols")
        if any(key not in report for key in required):
            raise ValueError("MULTI_ASSET_RESEARCH_REQUIRED_FIELD_MISSING")
        research_key = str(report["research_key"]).strip()
        research_version = str(report["research_version"]).strip()
        symbols = report["symbols"]
        if (
            not research_key or not research_version
            or not isinstance(symbols, list) or len(symbols) < 2
            or any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols)
        ):
            raise ValueError("MULTI_ASSET_RESEARCH_IDENTITY_INVALID")
        try:
            as_of_bar_ts = int(report["as_of_bar_ts"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("MULTI_ASSET_RESEARCH_TIMESTAMP_INVALID") from exc
        if as_of_bar_ts <= 0:
            raise ValueError("MULTI_ASSET_RESEARCH_TIMESTAMP_INVALID")
        now = _now()
        with self.lock:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO multi_asset_research_runs("
                "research_key,as_of_bar_ts,research_version,symbols_json,payload_json,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (
                    research_key,
                    as_of_bar_ts,
                    research_version,
                    _json(symbols),
                    _json(report),
                    now,
                ),
            )
            return cursor.rowcount == 1

    def record_market_depth_snapshot(self, snapshot: dict[str, Any]) -> bool:
        """Persist one immutable public L2 snapshot; never overwrite market evidence."""
        required = ("pair", "snapshot_ts", "research_bar_ts", "source", "bids", "asks")
        if not isinstance(snapshot, dict) or any(key not in snapshot for key in required):
            raise ValueError("MARKET_DEPTH_REQUIRED_FIELD_MISSING")
        pair = str(snapshot["pair"]).strip()
        source = str(snapshot["source"]).strip()
        try:
            snapshot_ts = int(snapshot["snapshot_ts"])
            research_bar_ts = int(snapshot["research_bar_ts"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("MARKET_DEPTH_TIMESTAMP_INVALID") from exc
        pair_parts = pair.rsplit("/", 1)
        fiat_base_assets = {"USD", "EUR", "GBP", "CAD", "AUD", "CHF", "JPY"}
        if (
            len(pair_parts) != 2 or not pair_parts[0]
            or pair_parts[0].upper() in fiat_base_assets
            or pair_parts[1].upper() != "USD"
            or snapshot_ts <= 0 or research_bar_ts <= 0
            or source != "KRAKEN_PUBLIC_DEPTH"
        ):
            raise ValueError("MARKET_DEPTH_IDENTITY_INVALID")

        def normalize_levels(raw: Any, *, bids: bool) -> list[list[str]]:
            if not isinstance(raw, list) or not raw or len(raw) > 10:
                raise ValueError("MARKET_DEPTH_LEVELS_INVALID")
            normalized: list[list[str]] = []
            previous: Decimal | None = None
            for row in raw:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    raise ValueError("MARKET_DEPTH_LEVEL_INVALID")
                try:
                    price = Decimal(str(row[0]))
                    quantity = Decimal(str(row[1]))
                except (ArithmeticError, TypeError, ValueError) as exc:
                    raise ValueError("MARKET_DEPTH_LEVEL_INVALID") from exc
                if (
                    not price.is_finite() or not quantity.is_finite()
                    or price <= 0 or quantity <= 0
                ):
                    raise ValueError("MARKET_DEPTH_LEVEL_INVALID")
                if previous is not None and (
                    (bids and price >= previous) or (not bids and price <= previous)
                ):
                    raise ValueError("MARKET_DEPTH_LEVEL_ORDER_INVALID")
                normalized.append([str(price), str(quantity)])
                previous = price
            return normalized

        bids = normalize_levels(snapshot["bids"], bids=True)
        asks = normalize_levels(snapshot["asks"], bids=False)
        if Decimal(bids[0][0]) >= Decimal(asks[0][0]):
            raise ValueError("MARKET_DEPTH_BOOK_CROSSED_OR_LOCKED")

        optional_costs: dict[str, str | None] = {}
        for field in ("taker_fee_bps", "maker_fee_bps", "spread_bps"):
            value = snapshot.get(field)
            if value is None:
                optional_costs[field] = None
                continue
            try:
                parsed = Decimal(str(value))
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ValueError("MARKET_DEPTH_COST_INVALID:" + field) from exc
            if not parsed.is_finite() or parsed < 0:
                raise ValueError("MARKET_DEPTH_COST_INVALID:" + field)
            optional_costs[field] = str(parsed)

        with self.lock:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO market_depth_snapshots("
                "pair,snapshot_ts,research_bar_ts,source,bids_json,asks_json,"
                "taker_fee_bps,maker_fee_bps,spread_bps,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    pair, snapshot_ts, research_bar_ts, source, _json(bids), _json(asks),
                    optional_costs["taker_fee_bps"], optional_costs["maker_fee_bps"],
                    optional_costs["spread_bps"], _now(),
                ),
            )
            return cursor.rowcount == 1

    def market_depth_snapshot_count(self, pair: str | None = None) -> int:
        """Count immutable public depth snapshots, optionally for one USD pair."""
        with self.lock:
            if pair is None:
                row = self.connection.execute(
                    "SELECT COUNT(*) AS n FROM market_depth_snapshots"
                ).fetchone()
            else:
                row = self.connection.execute(
                    "SELECT COUNT(*) AS n FROM market_depth_snapshots WHERE pair=?",
                    (str(pair).strip(),),
                ).fetchone()
        return int(row["n"]) if row is not None else 0

    def market_depth_snapshots(
        self, pair: str, *, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """Return stored depth observations in chronological order for cost calibration."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM market_depth_snapshots WHERE pair=? "
                "ORDER BY snapshot_ts DESC LIMIT ?",
                (str(pair).strip(), max(1, min(int(limit), 10000))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in reversed(rows):
            item = dict(row)
            item["bids"] = json.loads(str(item.pop("bids_json")))
            item["asks"] = json.loads(str(item.pop("asks_json")))
            result.append(item)
        return result

    def record_microstructure_bar(self, bar: dict[str, Any]) -> bool:
        """Persist one immutable, quality-labelled Kraken WS v2 minute aggregate."""
        required = ("pair", "minute_ts", "source", "quality_status")
        if not isinstance(bar, dict) or any(key not in bar for key in required):
            raise ValueError("MICROSTRUCTURE_REQUIRED_FIELD_MISSING")
        pair = str(bar["pair"]).strip().upper()
        source = str(bar["source"]).strip()
        quality_status = str(bar["quality_status"]).strip().upper()
        pair_parts = pair.rsplit("/", 1)
        fiat_base_assets = {"USD", "EUR", "GBP", "CAD", "AUD", "CHF", "JPY"}
        try:
            minute_ts = int(bar["minute_ts"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("MICROSTRUCTURE_TIMESTAMP_INVALID") from exc
        if (
            len(pair_parts) != 2 or not pair_parts[0]
            or pair_parts[0] in fiat_base_assets or pair_parts[1] != "USD"
            or minute_ts <= 0 or minute_ts % 60 != 0
            or source != "KRAKEN_SPOT_WS_V2"
            or quality_status not in {"VALID", "PARTIAL"}
        ):
            raise ValueError("MICROSTRUCTURE_IDENTITY_INVALID")

        numeric_fields = (
            "buy_notional_usd", "sell_notional_usd", "signed_trade_notional_usd",
            "trade_imbalance", "ofi_l1_base", "ofi_l1_imbalance", "spread_bps",
            "depth_imbalance", "microprice_deviation_bps", "bid_depth_usd",
            "ask_depth_usd", "trade_open_usd", "trade_high_usd",
            "trade_low_usd", "trade_close_usd", "trade_base_volume", "midprice_usd",
        )
        for field in numeric_fields:
            value = bar.get(field)
            if value is None:
                continue
            try:
                parsed = Decimal(str(value))
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ValueError("MICROSTRUCTURE_VALUE_INVALID:" + field) from exc
            if not parsed.is_finite():
                raise ValueError("MICROSTRUCTURE_VALUE_INVALID:" + field)
        for field in (
            "trade_count", "book_update_count", "checksum_checks",
            "checksum_failures", "trade_id_gaps", "connection_gap_count",
            "book_sample_count",
        ):
            try:
                value = int(bar.get(field, 0))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("MICROSTRUCTURE_COUNT_INVALID:" + field) from exc
            if value < 0:
                raise ValueError("MICROSTRUCTURE_COUNT_INVALID:" + field)
        for field in ("first_trade_ts", "last_trade_ts", "first_book_event_ts", "last_book_event_ts"):
            value = bar.get(field)
            if value is not None:
                try:
                    parsed_timestamp = int(value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError("MICROSTRUCTURE_TIMESTAMP_INVALID:" + field) from exc
                if parsed_timestamp <= 0:
                    raise ValueError("MICROSTRUCTURE_TIMESTAMP_INVALID:" + field)
        for field, descending in (("top10_bids", True), ("top10_asks", False)):
            levels = bar.get(field, [])
            if not isinstance(levels, list) or len(levels) > 10:
                raise ValueError("MICROSTRUCTURE_BOOK_LEVELS_INVALID:" + field)
            previous_price: Decimal | None = None
            for level in levels:
                if not isinstance(level, dict):
                    raise ValueError("MICROSTRUCTURE_BOOK_LEVELS_INVALID:" + field)
                try:
                    price = Decimal(str(level["price"]))
                    quantity = Decimal(str(level["qty"]))
                except (KeyError, ArithmeticError, TypeError, ValueError) as exc:
                    raise ValueError("MICROSTRUCTURE_BOOK_LEVELS_INVALID:" + field) from exc
                if not price.is_finite() or not quantity.is_finite() or price <= 0 or quantity <= 0:
                    raise ValueError("MICROSTRUCTURE_BOOK_LEVELS_INVALID:" + field)
                if previous_price is not None and (
                    (descending and price >= previous_price)
                    or (not descending and price <= previous_price)
                ):
                    raise ValueError("MICROSTRUCTURE_BOOK_LEVELS_INVALID:" + field)
                previous_price = price
        now = _now()
        with self.lock:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO microstructure_bars("
                "pair,minute_ts,quality_status,payload_json,created_at"
                ") VALUES(?,?,?,?,?)",
                (pair, minute_ts, quality_status, _json(bar), now),
            )
            return cursor.rowcount == 1

    def microstructure_quality_summary(
        self, *, now_epoch: int | None = None, recent_window_minutes: int = 1440
    ) -> dict[str, Any]:
        """Aggregate lifetime counts in SQL and inspect JSON quality only in the recent window."""
        now = int(time.time()) if now_epoch is None else int(now_epoch)
        if recent_window_minutes < 1:
            raise ValueError("MICROSTRUCTURE_QUALITY_WINDOW_INVALID")
        recent_start = now - recent_window_minutes * 60
        with self.lock:
            aggregate_rows = self.connection.execute(
                "SELECT pair,COUNT(*) AS total_count,"
                "SUM(CASE WHEN quality_status='VALID' THEN 1 ELSE 0 END) AS valid_count,"
                "SUM(CASE WHEN quality_status='PARTIAL' THEN 1 ELSE 0 END) AS partial_count,"
                "MIN(minute_ts) AS first_minute_ts,MAX(minute_ts) AS latest_minute_ts "
                "FROM microstructure_bars GROUP BY pair"
            ).fetchall()
            latest_rows = self.connection.execute(
                "SELECT b.pair,b.minute_ts,b.quality_status,b.payload_json "
                "FROM microstructure_bars AS b JOIN ("
                "SELECT pair,MAX(minute_ts) AS latest_ts FROM microstructure_bars GROUP BY pair"
                ") AS latest ON latest.pair=b.pair AND latest.latest_ts=b.minute_ts"
            ).fetchall()
            recent_rows = self.connection.execute(
                "SELECT pair,minute_ts,quality_status,payload_json FROM microstructure_bars "
                "WHERE minute_ts>=? ORDER BY pair,minute_ts",
                (recent_start,),
            ).fetchall()

        by_pair: dict[str, dict[str, Any]] = {}
        valid_total = partial_total = 0
        for row in aggregate_rows:
            pair = str(row["pair"])
            by_pair[pair] = {
                "total_count": int(row["total_count"]),
                "valid_count": int(row["valid_count"] or 0),
                "partial_count": int(row["partial_count"] or 0),
                "recent_valid_count": 0,
                "recent_partial_count": 0,
                "recent_usable_valid_count": 0,
                "recent_legacy_or_unqualified_valid_count": 0,
                "first_minute_ts": int(row["first_minute_ts"]),
                "latest_minute_ts": int(row["latest_minute_ts"]),
                "latest_quality_status": None,
                "latest_book_event_ts": None,
                "latest_book_age_seconds": None,
            }
            valid_total += int(row["valid_count"] or 0)
            partial_total += int(row["partial_count"] or 0)

        for row in latest_rows:
            pair = str(row["pair"])
            summary = by_pair[pair]
            summary["latest_quality_status"] = str(row["quality_status"]).upper()
            try:
                decoded = json.loads(str(row["payload_json"]))
                payload = decoded if isinstance(decoded, dict) else {}
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = {}
            try:
                event_ts = int(
                    payload.get("last_book_event_ts")
                    or payload.get("book_event_ts")
                    or 0
                )
            except (TypeError, ValueError, OverflowError):
                event_ts = 0
            if event_ts > 0:
                summary["latest_book_event_ts"] = event_ts
                summary["latest_book_age_seconds"] = now - event_ts

        recent_valid = recent_partial = 0
        recent_usable = recent_legacy = 0
        for row in recent_rows:
            pair = str(row["pair"])
            status = str(row["quality_status"]).upper()
            summary = by_pair[pair]
            try:
                decoded = json.loads(str(row["payload_json"]))
                payload = decoded if isinstance(decoded, dict) else {}
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = {}
            if status == "VALID":
                recent_valid += 1
                summary["recent_valid_count"] += 1
                try:
                    first_ts = int(payload.get("first_book_event_ts", 0))
                    last_ts = int(payload.get("last_book_event_ts", 0))
                    usable = (
                        int(payload.get("book_sample_count", 0)) >= 2
                        and first_ts > 0
                        and last_ts - first_ts >= 10
                        and int(payload.get("checksum_checks", 0)) > 0
                        and int(payload.get("checksum_failures", 0)) == 0
                        and int(payload.get("trade_id_gaps", 0)) == 0
                        and int(payload.get("connection_gap_count", 0)) == 0
                        and not payload.get("quality_flags")
                    )
                except (TypeError, ValueError, OverflowError):
                    usable = False
                if usable:
                    recent_usable += 1
                    summary["recent_usable_valid_count"] += 1
                else:
                    recent_legacy += 1
                    summary["recent_legacy_or_unqualified_valid_count"] += 1
            elif status == "PARTIAL":
                recent_partial += 1
                summary["recent_partial_count"] += 1

        for summary in by_pair.values():
            summary["valid_pct"] = (
                round(summary["valid_count"] / summary["total_count"] * 100.0, 2)
                if summary["total_count"] else None
            )
        return {
            "total_bar_count": sum(item["total_count"] for item in by_pair.values()),
            "status_counts": {"VALID": valid_total, "PARTIAL": partial_total},
            "recent_window_minutes": recent_window_minutes,
            "recent_status_counts": {"VALID": recent_valid, "PARTIAL": recent_partial},
            "recent_usable_valid_bar_count": recent_usable,
            "recent_legacy_or_unqualified_valid_bar_count": recent_legacy,
            # Compatibility aliases are explicitly scoped to the recent window.
            "usable_valid_bar_count": recent_usable,
            "legacy_or_unqualified_valid_bar_count": recent_legacy,
            "usable_quality_count_scope": "RECENT_WINDOW_ONLY",
            "pair_count": len(by_pair),
            "by_pair": by_pair,
            "quality_scope": "VALID_REQUIRES_V0_1_61_VERIFIED_MULTI_SAMPLE_COVERAGE",
        }

    def microstructure_bar_count(self, pair: str | None = None) -> int:
        """Count persisted one-minute WS v2 feature aggregates."""
        with self.lock:
            if pair is None:
                row = self.connection.execute(
                    "SELECT COUNT(*) AS n FROM microstructure_bars"
                ).fetchone()
            else:
                row = self.connection.execute(
                    "SELECT COUNT(*) AS n FROM microstructure_bars WHERE pair=?",
                    (str(pair).strip().upper(),),
                ).fetchone()
        return int(row["n"]) if row is not None else 0

    def microstructure_pairs(self, *, limit: int = 1000) -> list[str]:
        """Return only markets that actually have persisted microstructure bars."""
        if limit < 1:
            raise ValueError("MICROSTRUCTURE_PAIR_LIMIT_INVALID")
        with self.lock:
            rows = self.connection.execute(
                "SELECT DISTINCT pair FROM microstructure_bars ORDER BY pair LIMIT ?",
                (min(int(limit), 10000),),
            ).fetchall()
        return [str(row["pair"]) for row in rows]

    def microstructure_bars(
        self, pair: str, *, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """Return immutable minute aggregates in ascending timestamp order."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT payload_json FROM microstructure_bars WHERE pair=? "
                "ORDER BY minute_ts DESC LIMIT ?",
                (str(pair).strip().upper(), max(1, min(int(limit), 10000))),
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in reversed(rows)]

    def multi_asset_research_bar_exists(self, as_of_bar_ts: int) -> bool:
        """Check whether a multi-asset snapshot already exists for this common H1 bar."""
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM multi_asset_research_runs WHERE as_of_bar_ts=? LIMIT 1",
                (int(as_of_bar_ts),),
            ).fetchone()
        return row is not None

    def multi_asset_research_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the newest immutable multi-asset research reports for review."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT payload_json FROM multi_asset_research_runs "
                "ORDER BY as_of_bar_ts DESC,research_key DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        reports: list[dict[str, Any]] = []
        for row in rows:
            try:
                report = json.loads(str(row["payload_json"]))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(report, dict):
                reports.append(report)
        return reports

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

    def intent_for_decision(self, decision_id: str) -> dict[str, Any] | None:
        """Return the durable state for a deterministic decision/idempotency key."""
        with self.lock:
            row = self.connection.execute(
                "SELECT intent_id,decision_id,client_order_id,pair,action,side,volume,"
                "leverage,state,created_at,updated_at FROM intents WHERE decision_id=?",
                (str(decision_id),),
            ).fetchone()
        return dict(row) if row is not None else None

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

    def resolve_circuit_breaker(
        self,
        name: str,
        expected_reason: str,
        detail: dict[str, Any] | None = None,
        *,
        expected_detail: dict[str, Any] | None = None,
    ) -> bool:
        """Resolve a matching active breaker and retain its recovery audit event.

        When supplied, expected_detail is a partial signature of the stored
        failure. Nested dictionaries are compared by their supplied keys, preventing
        a stale recovery attempt from clearing a newer incident with the same
        breaker name and reason.
        """
        recovery_detail = detail or {}
        with self.lock:
            row = self.connection.execute(
                "SELECT active, reason, detail_json FROM circuit_breakers WHERE name = ?",
                (name,),
            ).fetchone()
            if (
                row is None
                or not bool(row["active"])
                or str(row["reason"]) != expected_reason
            ):
                return False
            if expected_detail is not None:
                try:
                    stored_detail = json.loads(row["detail_json"])
                except (TypeError, ValueError):
                    return False
                if not isinstance(stored_detail, dict):
                    return False
                for key, expected_value in expected_detail.items():
                    actual_value = stored_detail.get(key)
                    if isinstance(expected_value, dict):
                        if not isinstance(actual_value, dict) or any(
                            actual_value.get(nested_key) != nested_value
                            for nested_key, nested_value in expected_value.items()
                        ):
                            return False
                    elif actual_value != expected_value:
                        return False
            updated = self.connection.execute(
                "UPDATE circuit_breakers SET active = 0 "
                "WHERE name = ? AND active = 1 AND reason = ?",
                (name, expected_reason),
            )
            if updated.rowcount != 1:
                return False
            self.record_event(
                "CIRCUIT_BREAKER_RECOVERED",
                {
                    "name": name,
                    "previous_reason": expected_reason,
                    "detail": recovery_detail,
                },
            )
            return True

    def storage_diagnostics(self) -> dict[str, Any]:
        """Return bounded filesystem and SQLite settings for database-open failures."""
        if self.path == ":memory:":
            return {"database_path": ":memory:", "storage_mode": "MEMORY"}

        def path_state(path: Path) -> dict[str, Any]:
            try:
                info = path.stat()
            except OSError as exc:
                return {"exists": False, "stat_errno": exc.errno}
            is_directory = stat.S_ISDIR(info.st_mode)
            return {
                "exists": True,
                "is_directory": is_directory,
                "mode": oct(stat.S_IMODE(info.st_mode)),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "size_bytes": info.st_size,
                "readable": os.access(path, os.R_OK),
                "writable": os.access(path, os.W_OK),
                "searchable": os.access(path, os.X_OK) if is_directory else None,
            }

        database_path = Path(self.path)
        try:
            temporary_path = Path(tempfile.gettempdir())
            temporary_state = path_state(temporary_path)
        except (OSError, RuntimeError):
            temporary_path = None
            temporary_state = {"exists": False, "reason": "TEMP_DIRECTORY_UNAVAILABLE"}

        details: dict[str, Any] = {
            "process_uid": os.getuid(),
            "process_gid": os.getgid(),
            "database_path": str(database_path),
            "database": path_state(database_path),
            "database_parent": path_state(database_path.parent),
            "database_wal": path_state(Path(f"{self.path}-wal")),
            "database_shm": path_state(Path(f"{self.path}-shm")),
            "database_journal": path_state(Path(f"{self.path}-journal")),
            "temporary_directory": str(temporary_path) if temporary_path is not None else None,
            "temporary_directory_state": temporary_state,
        }
        with self.lock:
            for key, statement in (
                ("sqlite_temp_store", "PRAGMA temp_store"),
                ("sqlite_journal_mode", "PRAGMA journal_mode"),
            ):
                try:
                    row = self.connection.execute(statement).fetchone()
                    details[key] = str(row[0]) if row is not None else None
                except sqlite3.Error as exc:
                    details[f"{key}_probe_error"] = {
                        "exception_type": type(exc).__name__,
                        "sqlite_error_code": getattr(exc, "sqlite_errorcode", None),
                        "sqlite_error_name": getattr(exc, "sqlite_errorname", None),
                    }
        return details

    def close(self) -> None:
        with self.lock:
            self.connection.close()
