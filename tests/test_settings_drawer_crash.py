"""Regression test for the crash when the settings drawer is opened.

Opening ⚙ used to kill the whole app: `MainWindow._wake_state` filtered the
live session's dict down to three keys, while the repaint indexed the new
`timeout_label` directly — so `KeyError` was raised inside a click handler on
the Qt thread and the window closed.

The rule these lock in: a repaint must never be able to take the app down, and
the state dict must carry every key the repaint reads on BOTH of its paths
(live session wired, and the startup fallback).

These build the real MainWindow. `show()` plus `processEvents()` is required
before asking about isVisible(): a widget that was never shown reports False
for itself and everything inside it.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import main as M  # noqa: E402
import ui as U  # noqa: E402

KEYS = ("ready", "enabled", "awake", "detector_running", "timeout", "timeout_label")


class DrawerTestCase(unittest.TestCase):
    def build(self, wake_get_state=None, enabled=True, timeout=20.0):
        w = U.MainWindow.__new__(U.MainWindow)
        U.MainWindow.__init__(w, "face.png")
        self.sess = types.SimpleNamespace(
            _wake_enabled=enabled, _awake=False, _wake_detector=None,
            _wake_sleep_timeout=timeout, ui=w)
        w.wake_get_state = (lambda: M.JarvisLive._wake_state(self.sess)
                            if wake_get_state == "live" else wake_get_state)
        w.wake_is_ready = lambda: True
        w.on_wake_toggle = None
        w.on_wake_set_timeout = None
        w.show()
        _app.processEvents()
        return w

    def open_drawer(self, w):
        """Click ⚙. This is the call that used to raise."""
        w._toggle_drawer(True)
        _app.processEvents()


class TestOpeningTheDrawer(DrawerTestCase):
    def test_opens_with_a_live_session(self):
        w = self.build("live")
        self.open_drawer(w)          # must not raise
        self.assertIn("WAKE WORD", w._wake_btn.text())

    def test_opens_before_the_session_is_wired(self):
        w = self.build(None)
        self.open_drawer(w)
        self.assertIn("WAKE WORD", w._wake_btn.text())

    def test_survives_an_empty_state(self):
        w = self.build(lambda: {})
        self.open_drawer(w)
        self.assertIn("WAKE WORD", w._wake_btn.text())

    def test_survives_a_raising_callback(self):
        def boom():
            raise RuntimeError("session not ready")
        w = self.build(boom)
        self.open_drawer(w)
        self.assertIn("WAKE WORD", w._wake_btn.text())

    def test_survives_a_non_dict_state(self):
        w = self.build(lambda: "nonsense")
        self.open_drawer(w)
        self.assertIn("WAKE WORD", w._wake_btn.text())

    def test_survives_a_state_missing_every_optional_key(self):
        """The second layer of defence, tested on its own.

        _wake_state is now correct on both paths, so feeding a partial dict
        through wake_get_state proves nothing: the repair happens upstream and
        the repaint never sees a broken value. To exercise the repaint's own
        normalisation, _wake_state itself is replaced with one that returns the
        partial dict — that is the only way to reach the guard.
        """
        w = self.build("live")
        partial = {"ready": True, "enabled": True, "awake": True}
        w._wake_state = lambda: partial
        w._refresh_wake_btns()          # must not raise
        self.assertIn("WAKE WORD", w._wake_btn.text())

    def test_survives_a_non_dict_reaching_the_repaint(self):
        w = self.build("live")
        w._wake_state = lambda: None
        w._refresh_wake_btns()          # must not raise
        self.assertIn("WAKE WORD", w._wake_btn.text())


class TestStateShape(DrawerTestCase):
    def test_live_path_carries_every_key(self):
        w = self.build("live")
        st = w._wake_state()
        for k in KEYS:
            self.assertIn(k, st, f"live path is missing {k}")

    def test_fallback_path_carries_every_key(self):
        """The startup fallback returned only three keys — that asymmetry is
        the bug, not an edge case worth tolerating."""
        w = self.build(None)
        st = w._wake_state()
        for k in KEYS:
            self.assertIn(k, st, f"fallback path is missing {k}")


class TestWindowRow(DrawerTestCase):
    def test_row_shows_and_marks_the_current_window(self):
        w = self.build("live", enabled=True, timeout=30.0)
        self.open_drawer(w)
        self.assertTrue(all(b.isVisible() for b, _ in w._wake_timeout_btns))
        checked = [b.text() for b, _ in w._wake_timeout_btns if b.isChecked()]
        self.assertEqual(checked, ["30s"])

    def test_row_hides_when_the_wake_word_is_off(self):
        w = self.build("live", enabled=False)
        self.open_drawer(w)
        self.assertFalse(any(b.isVisible() for b, _ in w._wake_timeout_btns))


if __name__ == "__main__":
    unittest.main()
