"""Bounded parallel task execution with observable state and cancellation."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

MAX_TASKS = 6
MAX_WORKERS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TaskRecord:
    task_id: str
    kind: str
    parameters: dict
    status: str = "queued"
    progress: str = "Queued"
    result: str = ""
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    _future: Future | None = field(default=None, repr=False, compare=False)

    def public(self) -> dict:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class TaskOrchestrator:
    def __init__(self, max_workers: int = MAX_WORKERS):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="jarvis-task")
        self._records: dict[str, TaskRecord] = {}
        self._lock = threading.RLock()

    def _update(self, record: TaskRecord, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(record, key, value)

    def _run_one(
        self,
        record: TaskRecord,
        worker: Callable,
        player=None,
    ) -> str:
        self._update(record, status="running", progress="Started", started_at=_now())
        if player and hasattr(player, "set_state"):
            player.set_state(f"WORKING:{record.kind} {record.task_id[:8]}")
        if player and hasattr(player, "write_log"):
            player.write_log(f"TASK {record.task_id[:8]}: {record.kind} started.")
        try:
            result = worker(record.parameters, player=player)
            result = str(result or "Done.")
            self._update(record, status="completed", progress="Completed", result=result, finished_at=_now())
            if player and hasattr(player, "write_log"):
                player.write_log(f"TASK {record.task_id[:8]}: {record.kind} completed.")
            return result
        except Exception as exc:
            self._update(record, status="failed", progress="Failed", error=str(exc), finished_at=_now())
            if player and hasattr(player, "write_log"):
                player.write_log(f"TASK {record.task_id[:8]}: {record.kind} failed: {exc}")
            return f"Task failed: {exc}"

    def start(self, tasks: list[dict], workers: dict[str, Callable], player=None) -> tuple[str, list[dict]]:
        if not tasks:
            raise ValueError("At least one task is required")
        if len(tasks) > MAX_TASKS:
            raise ValueError(f"A maximum of {MAX_TASKS} tasks may run at once")

        group_id = uuid.uuid4().hex
        records: list[TaskRecord] = []
        with self._lock:
            for item in tasks:
                kind = str(item.get("kind", "")).strip().lower()
                if kind not in workers:
                    raise ValueError(f"Unsupported orchestrated task: {kind or '<empty>'}")
                record = TaskRecord(
                    task_id=f"{group_id[:8]}-{uuid.uuid4().hex[:8]}",
                    kind=kind,
                    parameters=dict(item.get("parameters") or {}),
                )
                self._records[record.task_id] = record
                records.append(record)

            for record in records:
                record._future = self._executor.submit(self._run_one, record, workers[record.kind], player)

        if player and hasattr(player, "write_log"):
            player.write_log(f"TASK GROUP {group_id[:8]}: {len(records)} tasks started in parallel.")

        results: list[dict] = []
        for record in records:
            if record._future is not None:
                record._future.result()
            results.append(record.public())
        return group_id, results

    def status(self, task_id: str = "") -> list[dict]:
        with self._lock:
            records = list(self._records.values())
        if task_id:
            records = [record for record in records if record.task_id == task_id]
        return [record.public() for record in records]

    def cancel(self, task_id: str) -> str:
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return f"Task not found: {task_id}"
            if record.status != "queued" or record._future is None:
                return f"Task {task_id} is {record.status} and cannot be cancelled."
            if not record._future.cancel():
                return f"Task {task_id} could not be cancelled."
            record.status = "cancelled"
            record.progress = "Cancelled"
            record.finished_at = _now()
            return f"Task {task_id} cancelled."


ORCHESTRATOR = TaskOrchestrator()