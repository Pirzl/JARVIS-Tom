"""
Execution Logger — Persistent Telemetry Store for JARVIS Actions.
Logs tool calls, execution status, parameters, tracebacks, and execution time to SQLite.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "memory" / "execution_telemetry.sqlite3"

# ── Cached connection ──────────────────────────────────────────────────────────
# A single SQLite connection reused across the process lifetime.
# Protected by _DB_LOCK so concurrent tool calls never race on the handle.
# WAL mode + NORMAL sync: concurrent readers/writers, safe against crashes.
_DB_CONN: sqlite3.Connection | None = None
_DB_LOCK = threading.Lock()


def _get_connection() -> sqlite3.Connection:
    global _DB_CONN
    with _DB_LOCK:
        if _DB_CONN is not None:
            return _DB_CONN
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL allows concurrent reads while a write is in progress —
        # eliminates lock contention under rapid back-to-back tool calls.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS action_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_name TEXT NOT NULL,
                    parameters TEXT,
                    success INTEGER NOT NULL,
                    error_message TEXT,
                    traceback_str TEXT,
                    execution_time_ms REAL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_action_timestamp
                ON action_logs(timestamp, success)
            """)
        _DB_CONN = conn
        return _DB_CONN


def log_execution(
    action_name: str,
    parameters: Optional[Dict[str, Any]] = None,
    success: bool = True,
    error_message: Optional[str] = None,
    traceback_str: Optional[str] = None,
    execution_time_ms: float = 0.0,
) -> None:
    """Record an action execution event into telemetry DB."""
    try:
        params_json = json.dumps(parameters or {}, default=str)
    except Exception:
        params_json = str(parameters or {})

    try:
        conn = _get_connection()
        with _DB_LOCK:
            conn.execute(
                """
                INSERT INTO action_logs (
                    action_name, parameters, success, error_message, traceback_str, execution_time_ms, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_name,
                    params_json,
                    1 if success else 0,
                    error_message or "",
                    traceback_str or "",
                    execution_time_ms,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()
    except Exception as exc:
        print(f"[Telemetry Warning] Failed to log action execution: {exc}")


def get_recent_failures(hours: int = 24) -> List[Dict[str, Any]]:
    """Retrieve all failed action logs within the given past hours."""
    try:
        conn = _get_connection()
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        with _DB_LOCK:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, action_name, parameters, error_message, traceback_str, execution_time_ms, timestamp
                FROM action_logs
                WHERE success = 0 AND timestamp >= ?
                ORDER BY timestamp DESC
                """,
                (cutoff,),
            )
            rows = cursor.fetchall()

        results = []
        for r in rows:
            try:
                params = json.loads(r["parameters"]) if r["parameters"] else {}
            except Exception:
                params = r["parameters"]

            results.append({
                "id": r["id"],
                "action_name": r["action_name"],
                "parameters": params,
                "error_message": r["error_message"],
                "traceback": r["traceback_str"],
                "execution_time_ms": r["execution_time_ms"],
                "timestamp": r["timestamp"],
            })
        return results
    except Exception as exc:
        print(f"[Telemetry Error] Failed to retrieve recent failures: {exc}")
        return []


def get_action_stats(hours: int = 24) -> Dict[str, Any]:
    """Calculate execution statistics (total runs, total failures, success rate)."""
    try:
        conn = _get_connection()
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        with _DB_LOCK:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failures,
                    AVG(execution_time_ms) as avg_time
                FROM action_logs
                WHERE timestamp >= ?
                """,
                (cutoff,),
            )
            row = cursor.fetchone()

        total = row["total"] or 0
        failures = row["failures"] or 0
        success_rate = ((total - failures) / total * 100.0) if total > 0 else 100.0

        return {
            "hours": hours,
            "total_runs": total,
            "failed_runs": failures,
            "success_rate_pct": round(success_rate, 2),
            "avg_execution_time_ms": round(row["avg_time"] or 0.0, 2),
        }
    except Exception as exc:
        print(f"[Telemetry Error] Failed to fetch stats: {exc}")
        return {"hours": hours, "total_runs": 0, "failed_runs": 0, "success_rate_pct": 100.0, "avg_execution_time_ms": 0.0}
