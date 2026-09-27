"""Local SQLite storage for authorized passive OSINT cases and evidence."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE_DIR / "memory" / "osint_cases.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OsintStore:
    def __init__(self, path: Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    authorization TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'passive',
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                );
                CREATE TABLE IF NOT EXISTS targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL REFERENCES cases(id),
                    entity_type TEXT NOT NULL,
                    value TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    in_scope INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(case_id, entity_type, normalized_value)
                );
                CREATE TABLE IF NOT EXISTS findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL REFERENCES cases(id),
                    entity_type TEXT NOT NULL,
                    value TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_url TEXT,
                    observed_at TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'INFO',
                    direct_observation INTEGER NOT NULL DEFAULT 1,
                    raw_data TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(case_id, entity_type, value, source)
                );
                CREATE INDEX IF NOT EXISTS findings_case_idx ON findings(case_id);
                """
            )

    def create_case(self, name: str, authorization: str) -> str:
        case_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO cases(id, name, authorization, created_at) VALUES (?, ?, ?, ?)",
                (case_id, name, authorization, _now()),
            )
        return case_id

    def add_target(self, case_id: str, entity_type: str, value: str, normalized_value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO targets
                   (case_id, entity_type, value, normalized_value) VALUES (?, ?, ?, ?)""",
                (case_id, entity_type, value, normalized_value),
            )

    def add_finding(
        self,
        case_id: str,
        entity_type: str,
        value: str,
        source: str,
        source_url: str = "",
        confidence: str = "MEDIUM",
        severity: str = "INFO",
        direct_observation: bool = True,
        raw_data: dict | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO findings
                   (case_id, entity_type, value, source, source_url, observed_at,
                    confidence, severity, direct_observation, raw_data)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    case_id,
                    entity_type,
                    value,
                    source,
                    source_url,
                    _now(),
                    confidence,
                    severity,
                    int(direct_observation),
                    json.dumps(raw_data or {}, ensure_ascii=False),
                ),
            )

    def report(self, case_id: str) -> dict:
        with self._connect() as connection:
            case = connection.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
            if case is None:
                raise ValueError(f"OSINT case not found: {case_id}")
            targets = connection.execute(
                "SELECT * FROM targets WHERE case_id = ? ORDER BY id", (case_id,)
            ).fetchall()
            findings = connection.execute(
                "SELECT * FROM findings WHERE case_id = ? ORDER BY id", (case_id,)
            ).fetchall()

        return {
            "case": dict(case),
            "targets": [dict(row) for row in targets],
            "findings": [
                {**dict(row), "raw_data": json.loads(row["raw_data"] or "{}")}
                for row in findings
            ],
        }

    def export_json(self, case_id: str, path: Path) -> Path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(self.report(case_id), indent=2, ensure_ascii=False), encoding="utf-8")
        return output