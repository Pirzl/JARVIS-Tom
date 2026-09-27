"""Run approved Jarvis workers concurrently with visible progress."""

from __future__ import annotations

import json

from core.task_orchestrator import ORCHESTRATOR


def _workers() -> dict:
    from actions.code_helper import code_helper
    from actions.dev_agent import dev_agent
    from actions.osint_scan import osint_scan
    from actions.website_builder import website_builder

    return {
        "code": code_helper,
        "project": dev_agent,
        "osint": osint_scan,
        "website": website_builder,
    }


def _parse_tasks(value) -> list[dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"tasks must be valid JSON: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("tasks must be an array of {kind, parameters} objects")
    return value


def task_orchestrator(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    operation = str(params.get("operation", "start")).strip().lower()
    if operation == "status":
        task_id = str(params.get("task_id", "")).strip()
        return json.dumps(ORCHESTRATOR.status(task_id), ensure_ascii=False)
    if operation == "cancel":
        task_id = str(params.get("task_id", "")).strip()
        return ORCHESTRATOR.cancel(task_id) if task_id else "Please provide a task_id to cancel."
    if operation != "start":
        return "Unknown operation. Use start, status, or cancel."

    try:
        tasks = _parse_tasks(params.get("tasks", []))
        group_id, records = ORCHESTRATOR.start(tasks, _workers(), player=player)
    except (ValueError, TypeError) as exc:
        return f"Could not start task group: {exc}"

    completed = sum(record["status"] == "completed" for record in records)
    failed = sum(record["status"] == "failed" for record in records)
    summary = {
        "group_id": group_id,
        "completed": completed,
        "failed": failed,
        "tasks": records,
    }
    message = json.dumps(summary, ensure_ascii=False)
    if speak:
        speak(f"Task group {group_id[:8]} finished: {completed} completed, {failed} failed.")
    return message


TOOL = {
    "name": "task_orchestrator",
    "description": "Runs up to six approved coding, website, project, or passive OSINT tasks in parallel with visible progress; also reports or cancels queued tasks.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {"type": "STRING", "description": "start, status, or cancel (default: start)"},
            "tasks": {
                "type": "ARRAY",
                "description": "Tasks to run concurrently. Each item has kind=code, project, website, or osint and a parameters object.",
                "items": {"type": "OBJECT"},
            },
            "task_id": {"type": "STRING", "description": "Task ID for status or cancellation"},
        },
    },
    "handler": task_orchestrator,
}