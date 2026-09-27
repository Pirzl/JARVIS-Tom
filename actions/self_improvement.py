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
from core import confirm
from core.execution_logger import get_action_stats, get_recent_failures

# Traceback signatures we are willing to hand a code model. A broad match here
# would let a wrong failure rewrite an unrelated file, so the list is short and
# explicit rather than "anything that looks like a crash".
_AUTO_FIXABLE = ("SyntaxError", "TypeError", "NameError")


def _autofix_target(failures: list[dict]) -> Optional[str]:
    """Return the action filename a repair could safely target, or None.

    The name comes from telemetry, so it is never trusted as a path: it is
    rejected unless it is a plain module name that resolves to a real file
    inside actions/. That closes the traversal a crafted action_name
    ('../../core/gemini') would otherwise open.
    """
    for f in failures or []:
        tb = str(f.get("traceback") or "")
        raw = str(f.get("action_name") or "").strip()
        if not any(sig in tb for sig in _AUTO_FIXABLE) or not raw:
            continue
        if not raw.replace("_", "").isalnum():      # no dots, slashes, colons
            continue
        if not (BASE_DIR / "actions" / f"{raw}.py").is_file():
            continue
        return raw
    return None


def _autofix_run(action_name: str) -> str:
    """Hand one action file to dev_agent, after the user confirmed on the HUD."""
    import shutil
    from datetime import datetime as _dt

    src = BASE_DIR / "actions" / f"{action_name}.py"
    backup_dir = BASE_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{action_name}.py.{_dt.now():%Y%m%d-%H%M%S}.bak"
    shutil.copy2(src, backup)

    from actions import dev_agent
    return dev_agent.dev_agent(
        parameters={
            "description": (
                f"Repair the single file {src}. It raises on this failure: "
                f"see {backup}. Change nothing else, add no new features, and "
                f"return the corrected full contents of that one file."
            ),
            "language": "python",
        }
    )


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
    # Off by default: the report is safe to generate unattended, rewriting the
    # app's own source is not. See the SAFETY note at the repair block below.
    auto_fix = bool(params.get("auto_fix", False))
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
    #
    # SAFETY (2026-09-27): this used to default auto_fix to True, which let the
    # model rewrite JARVIS's own source in actions/ with nobody watching. Two
    # changes, both deliberate:
    #
    #   1. auto_fix now defaults to False. The report always contains the fix
    #      plan; applying it is a separate, human-initiated step.
    #   2. Even when requested, the repair is parked behind core/confirm.py —
    #      the same gate that guards shutdown — so the token that approves it is
    #      issued by the HUD, never by the model. Without a bound interface
    #      confirm.request() refuses, which is the correct failure direction.
    #
    # The step is ALSO a no-op by design below: dev_agent only BUILDS projects,
    # it has no "repair this existing file" entry point, so the old code imported
    # it and then never called it. The gate is kept because the real wiring is a
    # one-line change once dev_agent grows a repair path.
    autofix_summary = ""
    if auto_fix and failures:
        pending = confirm.pending_title()
        if pending:
            autofix_summary = (
                f"\n- Self-repair NOT started: another confirmation is already "
                f"waiting on screen ({pending}).")
        else:
            target = _autofix_target(failures)
            if target is None:
                autofix_summary = ("\n- Self-repair NOT started: no auto-fixable "
                                   "failure found (needs a SyntaxError, TypeError "
                                   "or NameError traceback in an action).")
            else:
                autofix_summary = confirm.request(
                    key="self-repair",
                    title="Let JARVIS rewrite one of its own action files?",
                    detail=(f"Target: actions/{target}\n\n"
                            f"The audit found a repairable crash here. If you "
                            f"confirm, JARVIS will hand the file to dev_agent "
                            f"and let it rewrite the code. A copy is kept in "
                            f"backups/ so you can restore it."),
                    run=lambda f=target: _autofix_run(f),
                )
    elif not auto_fix:
        autofix_summary = ("\n- Self-repair skipped (auto_fix is off by default). "
                           "The fix plan is in the report; ask for it to be applied.")

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
    "description": "Run a self-diagnostic audit on JARVIS. Analyzes failed tasks, checks system health with doctor.py, writes a detailed audit note & copy-pasteable fix plan to Obsidian. Never modifies code unless the user explicitly asks AND confirms on the HUD.",
    "parameters": {
        # "OBJECT"/"BOOLEAN" in caps: that is core/action_loader.py's own
        # contract, not JSON Schema. Lowercase here makes the loader reject the
        # whole action at startup.
        "type": "OBJECT",
        "properties": {
            "auto_fix": {
                "type": "BOOLEAN",
                "default": False,
                "description": "Optional. Leave false unless the user explicitly asked you to repair a broken action. If true, JARVIS will ASK THE USER ON THE HUD to confirm before letting dev_agent rewrite one action file. Do not set this on your own initiative."
            }
        },
    },
    "handler": run_self_audit,
}
