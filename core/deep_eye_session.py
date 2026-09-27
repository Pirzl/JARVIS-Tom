"""
core/deep_eye_session.py — the live-session half of the Deep Eye feature.

WHY A MIXIN
    Third extraction of the same kind as core/live_config.py and
    core/background_loops.py: JarvisLive had grown past the size where adding
    to it is safe, and an existing size guard (test_main_is_smaller) failed the
    moment this feature's methods were added to main.py. Same mechanical
    pattern, same reason: these methods are a coherent unit with no reason to
    live in the middle of the audio loop.

WHAT MOVED HERE
    Only what needs the live session. The bridge (core/deep_eye.py) keeps
    validation, the subprocess and the consent gate — none of that touches Qt
    and all of it must work in a test with no window open.

BOUNDARIES
    Every UI call goes through a signal, because two of these run on the
    scanner's reader thread. Touching a widget from there is a crash, not a
    warning. The methods themselves never raise: a scan that fails to paint
    must not take the assistant down with it.
"""
from __future__ import annotations

from typing import Callable, Optional

# Worst-first, so the first hit is the most serious finding on the site. The
# labels are the information: the HUD is small and colour alone would not
# survive greyscale or colour-blindness.
SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")


def worst_severity(result) -> Optional[str]:
    """The most serious severity in a scan result, or None if there are none."""
    worst = None
    for f in getattr(result, "findings", []) or []:
        sev = str(f.get("severity", "")).lower()
        if sev not in SEVERITY_ORDER:
            continue
        if worst is None or SEVERITY_ORDER.index(sev) < SEVERITY_ORDER.index(worst):
            worst = sev
    return worst


class DeepEyeSessionMixin:
    """Deep Eye wiring for JarvisLive. Expects `self.ui` and `self._deep_eye_scan`."""

    # ── wiring ─────────────────────────────────────────────────────────────

    def bind_deep_eye(self) -> None:
        """Attach the panel's STOP button and the action module's callbacks.

        The action has no `self` — the loader calls it as
        `fn(parameters=..., **ctx)` — so it reaches the session through an
        explicit module-level binding rather than an instance. An earlier
        version of the action used `self._de_on_begin` and only failed when a
        voice scan was actually attempted.
        """
        self._deep_eye_scan = None
        self.ui.on_deep_eye_cancel = self._de_cancel_scan  # () -> None
        try:
            from actions import deep_eye as _de_action
            _de_action.bind_session(
                on_begin=self._de_on_begin,
                on_line=self._de_on_line,
                on_done=self._de_on_done,
            )
        except Exception as e:
            print(f"[JARVIS] deep eye bind error: {e}")

    # ── lifecycle ──────────────────────────────────────────────────────────

    def _de_cancel_scan(self) -> None:
        """STOP pressed.

        Killing the child is the whole job. A scan left running would keep
        sending attack traffic to somebody's server long after the user
        decided they had seen enough.
        """
        scan = getattr(self, "_deep_eye_scan", None)
        if scan is not None:
            try:
                scan.cancel()
            except Exception as e:
                print(f"[JARVIS] deep eye cancel error: {e}")
        try:
            self.ui.deep_eye_done(False, "Scan stopped.")
            self.ui.deep_eye_activity("")
        except Exception:
            pass

    def _de_ensure_panel(self) -> None:
        """Make sure the panel exists and is on screen.

        Called before the confirmation gate, so the destination for the output
        is ready and a cancelled scan still leaves visible evidence of what
        was about to happen.
        """
        try:
            self.ui._win._toggle_deep_eye(True)
        except Exception as e:
            print(f"[JARVIS] deep eye panel error: {e}")

    # ── callbacks (on_line runs on the scanner's reader thread) ────────────

    def _de_on_begin(self, target: str) -> None:
        """A scan is starting. Show it, and let the avatar say so.

        The avatar's line matters as much as the panel: a scan takes minutes,
        and if the only feedback lived in a panel the user never opened, the
        button would look broken and the honest response would be to press it
        again — starting a second scan against the same target.
        """
        try:
            self._de_ensure_panel()
            self.ui.deep_eye_begin(target)
            self.ui.deep_eye_activity(f"SCANNING {target}")
            self.ui.write_log(f"SYS: Deep Eye started on {target}.")
        except Exception as e:
            print(f"[JARVIS] deep eye begin error: {e}")

    def _de_on_line(self, line: str) -> None:
        """One line of scanner output. Called on the reader thread."""
        try:
            self.ui.deep_eye_line(line)
        except Exception:
            pass

    def _de_on_done(self, ok: bool, payload) -> None:
        """Scan finished. `payload` is a ScanResult, or a message on failure."""
        try:
            if isinstance(payload, str):
                self.ui.deep_eye_done(ok, payload)
                self.ui.deep_eye_activity("")
                self.ui.write_log(f"SYS: {payload}")
            else:
                summary = payload.summary()
                self.ui.deep_eye_findings(summary)
                self.ui.deep_eye_done(ok, summary)
                worst = worst_severity(payload)
                self.ui.deep_eye_activity(
                    "" if worst is None else f"{worst.upper()} FINDING")
                self.ui.write_log(f"SYS: {summary}")
        except Exception as e:
            print(f"[JARVIS] deep eye done error: {e}")
        finally:
            self._deep_eye_scan = None
