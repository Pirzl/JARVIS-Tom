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
        # The panel's SCAN button. It routes through request_scan, the same
        # entry point the voice tool uses, so the gate is raised in exactly
        # one place and no caller can drift from it.
        self.ui.on_deep_eye_scan = self._de_request_scan  # (str) -> None
        # The gate tells us when it resolves. Without this the panel's SCAN
        # button stays disabled after a cancel: it is switched off when the
        # request goes out, and begin() — the only thing that switches it
        # back — is never called, because nothing started.
        from core import confirm as _confirm
        _confirm.set_settled_listener(self._de_gate_settled)
        try:
            from actions import deep_eye as _de_action
            _de_action.bind_session(
                on_begin=self._de_on_begin,
                on_line=self._de_on_line,
                on_done=self._de_on_done,
            )
        except Exception as e:
            print(f"[JARVIS] deep eye bind error: {e}")

    def _de_gate_settled(self, key: str, started: bool) -> None:
        """A confirmation resolved. `started` is True only if a scan is now
        running.

        Runs on the Qt thread, from resolve(). The panel's SCAN button is off
        while it waits, and a cancel or a timeout never reaches begin() — so
        without this it would stay dead for the rest of the session.
        """
        if key != "deep_eye_scan":
            return
        try:
            panel = getattr(self.ui._win, "_de_panel", None)
            if panel is not None:
                panel.gate_settled(started)
        except Exception:
            pass

    # ── lifecycle ──────────────────────────────────────────────────────────

    def _de_request_scan(self, target: str) -> None:
        """A scan was asked for from the panel.

        Goes through the same `request_scan` the voice tool uses, which is the
        one place that normalises the target, checks the install, and raises
        the confirmation gate. The panel has no privileged route of its own —
        if it had, the two paths could drift and one of them would end up
        scanning without asking.
        """
        from core import confirm as _confirm
        from core import deep_eye as _de
        try:
            _de.request_scan(target, _confirm.request,
                             on_begin=self._de_on_begin,
                             on_line=self._de_on_line,
                             on_done=self._de_on_done)
        except Exception as e:
            print(f"[JARVIS] deep eye request error: {e}")
            try:
                self.ui._win._de_panel_or_none()
                panel = self.ui._win._de_panel
                panel._status.setText(f"Could not start: {e}")
                panel._scan_btn.setEnabled(True)
            except Exception:
                pass

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
        is ready and a cancelled scan still leaves visible evidence of what was
        about to happen.

        show_only, not the header button's toggle: _toggle_deep_eye resets the
        panel when it is not visible, and this runs from _de_on_begin — that
        is, at the moment the scan starts. Resetting there would wipe the
        state the panel was just put into, so "Scanning" would never appear.
        """
        try:
            win = self.ui._win
            if getattr(win, "_de_panel", None) is None:
                win._toggle_deep_eye(True)      # creates it
            else:
                panel = win._de_panel
                panel.show()
                panel.raise_()
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
