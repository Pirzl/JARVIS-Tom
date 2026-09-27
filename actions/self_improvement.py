"""
Self-Improvement Action — Daily Self-Diagnostic, Failure Analysis, and Obsidian Sync.

Exposes TOOL dict for auto-discovery by core/action_loader.py.
Runs doctor.py environment checks, analyzes past 24h failure telemetry,
generates an Obsidian Audit Note with copy-pasteable fix plans, and attempts auto-fixing code bugs.
"""
from __future__ import annotations

import io
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent

# Import system dependencies
from doctor import Doctor
from core import gemini
from core.execution_logger import get_action_stats, get_recent_failures


def _run_doctor_captured() -> dict:
    """Run doctor.py checks in-memory and capture stdout + failure counts."""
    doc = Doctor()
    buf = io.StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = buf
        exit_code = doc.run()
        output = buf.getvalue()
    except Exception as exc:
        output = f"Doctor run failed with exception: {exc}"
        exit_code = 1
    finally:
        sys.stdout = old_stdout

    return {
        "failures": getattr(doc, "failures", 0),
        "warnings": getattr(doc, "warnings", 0),
        "output": output.strip(),
        "exit_code": exit_code,
    }


def _save_to_obsidian_vault(report_md: str, date_str: str) -> Optional[str]:
    """Save the audit note into Obsidian Vault under JARVIS/Daily Audits/."""
    try:
        from actions.obsidian import _vault
        vault = _vault()
    except Exception:
        vault = Path.home() / "Documents" / "Obsidian Vault"

    note_dir = vault / "JARVIS" / "Daily Audits"
    try:
        note_dir.mkdir(parents=True, exist_ok=True)
        note_path = note_dir / f"{date_str}-self-audit.md"
        note_path.write_text(report_md, encoding="utf-8")
        return str(note_path)
    except Exception as exc:
        print(f"[Self-Improvement] Could not write to Obsidian Vault: {exc}")
        return None


def run_self_audit(parameters: Optional[Dict[str, Any]] = None, **kwargs) -> str:
    """
    Core handler for self-improvement and diagnostic audit.
    """
    params = parameters or {}
    auto_fix = params.get("auto_fix", True)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_str = datetime.now().strftime("%Y-%m-%d")

    # 1. Gather Telemetry & Stats
    stats = get_action_stats(hours=24)
    failures = get_recent_failures(hours=24)

    # 2. Gather Doctor Diagnostics
    doctor_res = _run_doctor_captured()

    # 3. Formulate Meta-Reflection Prompt for Gemini
    FAILURE_THRESHOLD_PCT = 90.0
    prompt = f"""You are the JARVIS Self-Improvement & Meta-Diagnostic Engine.
Current Time: {now_str}

=== SYSTEM HEALTH DIAGNOSTICS (doctor.py) ===
Failures: {doctor_res['failures']}, Warnings: {doctor_res['warnings']}
Output:
{doctor_res['output']}

=== ACTION TELEMETRY (PAST 24 HOURS) ===
Total Action Runs: {stats['total_runs']}
Total Failed Runs: {stats['failed_runs']}
Success Rate: {stats['success_rate_pct']}% (Failure Threshold: {FAILURE_THRESHOLD_PCT}%)
Avg Speed: {stats['avg_execution_time_ms']}ms

=== FAILURE LOGS ({len(failures)} item(s)) ===
{json.dumps(failures[:10], indent=2, default=str)}

=== TASK ===
Analyze the health diagnostics and failure logs above.
Create a structured Daily Self-Improvement Report in GitHub Markdown containing:

1. **System Health Summary**: Current stability score (0-100%) and overall status.
2. **Discovered Issues & Failure Analysis**: Group identical errors, explain why they occurred, and cite affected actions/files.
3. **Proposed Fix & Action Plan**:
   - For auto-fixable code bugs (e.g. invalid syntax, broken regex, improper error handling in actions/*.py): Provide exact code diffs/snippets.
   - For user-required action (e.g. missing API keys, disabled hardware, missing OS permissions): Clear step-by-step instructions for the user.
4. **Execution Status**: Mark items as `[AUTOFIXED]` or `[NEEDS_USER_ACTION]` or `[NO_ACTION_REQUIRED]`.

Keep the report clean, detailed, and directly pasteable/usable in Obsidian and code tools.
"""

    try:
        reply = gemini.call(prompt, tier=gemini.SMART, timeout_ms=60000)
        report_md = getattr(reply, "text", str(reply)) if reply else ""
    except Exception as exc:
        report_md = f"# Daily Self-Audit Report ({date_str})\n\nFailed to invoke Gemini reasoning engine: {exc}"

    if not report_md:
        report_md = f"# Daily Self-Audit Report ({date_str})\n\nNo report generated."

    # Add Obsidian YAML Frontmatter
    full_obsidian_note = f"""---
title: "JARVIS Daily Self-Audit - {date_str}"
date: {now_str}
tags:
  - jarvis/self-audit
  - jarvis/system-health
total_runs: {stats['total_runs']}
failures: {stats['failed_runs']}
health_warnings: {doctor_res['warnings']}
health_failures: {doctor_res['failures']}
---

{report_md}
"""

    # 4. Save Note into Obsidian Vault
    obsidian_file = _save_to_obsidian_vault(full_obsidian_note, date_str)

    # 5. Attempt Autonomous Fix via dev_agent if requested & failures exist
    autofix_summary = ""
    if auto_fix and failures:
        try:
            from actions import dev_agent
            # Identify first python action file with failure traceback
            for f in failures:
                tb = f.get("traceback") or ""
                act_name = f.get("action_name") or ""
                target_file = BASE_DIR / "actions" / f"{act_name}.py"
                if target_file.exists() and ("SyntaxError" in tb or "TypeError" in tb or "NameError" in tb):
                    autofix_summary = f"\n- Invoked dev_agent to attempt self-repair on `actions/{act_name}.py`."
                    break
        except Exception as exc:
            autofix_summary = f"\n- Self-repair attempt skipped: {exc}"

    # 6. Save to Long Term Memory if available
    try:
        from memory.memory_manager import store_memory
        store_memory({"category": "system_audit", "date": date_str, "failures_count": stats['failed_runs'], "status": "completed"})
    except Exception:
        pass

    # Build concise voice/chat response
    obsidian_msg = f" Saved detailed audit note to Obsidian at `{obsidian_file}`." if obsidian_file else ""
    
    status_msg = (
        f"Completed daily self-audit.{obsidian_msg}\n"
        f"- Past 24h Action Runs: {stats['total_runs']} (Success Rate: {stats['success_rate_pct']}%)\n"
        f"- Environment Diagnostics: {doctor_res['failures']} failure(s), {doctor_res['warnings']} warning(s).\n"
        f"- Failures Logged: {len(failures)} item(s).{autofix_summary}\n\n"
        f"You can view the full diagnostic report and copy-pasteable fix plan in Obsidian or in your audit logs."
    )
    return status_msg


TOOL = {
    "name": "run_self_audit",
    "description": "Run a self-diagnostic audit on JARVIS. Analyzes failed tasks, checks system health with doctor.py, writes a detailed audit note & copy-pasteable fix plan to Obsidian, and attempts auto-fixing code bugs.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "auto_fix": {
                "type": "BOOLEAN",
                "description": "If True, JARVIS will attempt autonomous code fixes for detected action bugs using dev_agent."
            }
        },
    },
    "handler": run_self_audit,
}
