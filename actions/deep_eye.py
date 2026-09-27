"""
deep_eye — voice control for the Deep Eye security scanner.

Lets Thomas say "scan example.com" and get a real, active vulnerability scan
without touching the keyboard. Two things are non-negotiable here, and both
live in core/deep_eye.py rather than in this file, so that no other caller can
reach a scan without passing the same checks:

  1. A scan NEVER starts on the model's say-so. `request_scan` routes through
     core/confirm.py, whose token is issued by the HUD and cannot be written by
     the model — the "confirmed=yes" convention it replaced was forgeable by
     the model itself, which is why it is gone.
  2. Only a bare domain is scannable, normalised before it reaches argv.

The description below is what Gemini reads to decide when to route here. It
states the authorisation requirement plainly, because the model should not
sound like it is about to attack somebody's website when the user's intent is
to audit their own.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import deep_eye as de  # noqa: E402
from core import confirm as _confirm  # noqa: E402
from core.deep_eye import available  # noqa: E402  (re-exported for _status)

# The panel and the avatar line, reached without importing ui.py.
#
# The action loader calls handlers as `fn(parameters=..., **ctx)` — there is no
# `self`, because an action is a module, not a method. An earlier version of
# this file called `self._ensure_deep_eye_panel()` and only failed when the
# voice path was actually exercised. So the session is reached through a
# module-level function that JarvisLive sets at startup, and which degrades to
# a no-op if nobody has. That keeps the action importable and callable in a
# test, which is where the gate is verified.
_on_begin = None      # (target: str) -> None
_on_line = None       # (line: str) -> None
_on_done = None       # (ok: bool, payload) -> None


def bind_session(on_begin=None, on_line=None, on_done=None) -> None:
    """Wire the live session in. Called once by JarvisLive.__init__."""
    global _on_begin, _on_line, _on_done
    _on_begin, _on_line, _on_done = on_begin, on_line, on_done


def _open_panel() -> None:
    """Show the panel, whatever the session is."""
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return
        for w in app.topLevelWidgets():
            if hasattr(w, "_toggle_deep_eye"):
                w._toggle_deep_eye(True)
                return
    except Exception:
        pass


def deep_eye(parameters: dict, speak=None) -> str:
    """Entry point for the `deep_eye` tool.

    `speak` is injected by the action loader and used only for the spoken
    summary; the confirmation banner is raised by core/confirm, which is
    already bound to the HUD at startup.
    """
    action = str(parameters.get("action", "scan")).strip().lower()
    target = str(parameters.get("target", "")).strip()

    if action == "status":
        return _status()

    if action == "scan":
        if not target:
            return "Which site should I scan? Give me a domain, like example.com."
        # Show the panel BEFORE the gate. If the user then cancels, an empty
        # panel is visible and honest: it shows what was about to happen, and
        # that it did not happen.
        _open_panel()
        # request_scan validates, checks the install, and raises the gate. It
        # returns the sentence for the model to say; the work happens later,
        # off the Qt thread, only if Thomas presses CONFIRM.
        return de.request_scan(target, _confirm.request,
                               on_begin=_on_begin, on_line=_on_line,
                               on_done=_on_done)

    return (f"Unknown action {action!r}. I can do 'scan' (audit a site you own) "
            "or 'status' (check whether the scanner is installed).")


def _status() -> str:
    """A plain-language answer to 'can you actually do this?'.

    Worth having as a voice command: an assistant that claims a capability it
    cannot deliver is worse than one that admits an install step is missing.
    """
    if not available():
        return ("Deep Eye is not installed yet. Run scripts/install.ps1 inside "
                "vendor/deep-eye, then restart me.")
    probe = de.probe()
    if not probe.get("ok"):
        return ("Deep Eye is present but not runnable yet: "
                f"{probe.get('error') or 'its dependencies are not installed'}. "
                "Finish scripts/install.ps1 in vendor/deep-eye, then restart me.")
    return ("Deep Eye is installed and ready. Ask me to scan a domain you own "
            "and I will ask you to confirm first.")


TOOL = {
    "name": "deep_eye",
    "description": (
        "Actively audits a website for security vulnerabilities (SQL injection, "
        "XSS, SSRF, exposed secrets and 50+ other checks) using the locally "
        "installed Deep Eye scanner, and reports the findings. Use this ONLY "
        "for a site the user owns or has explicit written permission to test. "
        "Always explains what it is about to do and asks for confirmation on "
        "the HUD before any scan begins; the model cannot authorise itself. "
        "Never use it to scan a third party."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "scan | status",
            },
            "target": {
                "type": "STRING",
                "description": "Domain name to audit, e.g. example.com. A bare "
                               "host only — no scheme, path, port or IP address.",
            },
        },
        "required": ["action"],
    },
    "handler": deep_eye,
}
