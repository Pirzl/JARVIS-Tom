"""Tests for the wake-word listening-window setting in the HUD.

The window (how long the assistant stays awake after 'Hey Jarvis') was a
hardcoded constant. It is now a setting: four buttons in the settings drawer,
applied live, with the label on the wake button telling you both the window
and whether the detector is genuinely listening.

These drive the real MainWindow methods against a Qt app instance.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from PyQt6.QtWidgets import QApplication, QPushButton  # noqa: E402

from memory import config_manager as cm  # noqa: E402

_app = QApplication.instance() or QApplication([])

import main as M  # noqa: E402
import ui as U  # noqa: E402

WINDOWS = (("10s", 10), ("20s", 20), ("30s", 30), ("∞", 0))


class _Session:
    """The attributes _wake_state and the live setter read."""
    _wake_enabled = True
    _awake = True
    _wake_detector = None
    _wake_sleep_timeout = 20.0


class _Harness:
    """A MainWindow with only the attributes the wake row touches."""

    def __init__(self):
        self.win = types.SimpleNamespace()
        w = self.win
        self.sess = _Session()
        w._wake_btn = QPushButton()
        w._wake_sleep_btn = QPushButton()
        w._wake_timeout_btns = []
        self.applied = []
        w.on_wake_toggle = None
        w.write_log = lambda _m: None
        w._wake_state = lambda: M.JarvisLive._wake_state(self.sess)
        # The window reads this to repaint after a click; in the app both names
        # point at the same callable, wired in JarvisLive.__init__.
        w.wake_get_state = w._wake_state
        w.on_wake_set_timeout = self._apply
        w._set_wake_timeout = types.MethodType(U.MainWindow._set_wake_timeout, w)
        w._refresh_wake_btns = types.MethodType(U.MainWindow._refresh_wake_btns, w)
        w._refresh_wake_timeout_btns = types.MethodType(
            U.MainWindow._refresh_wake_timeout_btns, w)
        # Wire the buttons exactly as _build_quick_drawer does, so a click
        # travels the real path instead of a test-only shortcut.
        for label, seconds in WINDOWS:
            b = QPushButton(label)
            b.setCheckable(True)
            b.clicked.connect(
                lambda _c=False, s=seconds: w._set_wake_timeout(s))
            w._wake_timeout_btns.append((b, seconds))

    def _apply(self, seconds):
        self.applied.append(seconds)
        self.sess._wake_sleep_timeout = seconds

    def click(self, label):
        for b, _ in self.win._wake_timeout_btns:
            if b.text() == label:
                b.click()
                return
        raise AssertionError(f"no button labelled {label}")


class TestWindowButtons(unittest.TestCase):
    def setUp(self):
        self._orig = (cm.CONFIG_FILE.read_text(encoding="utf-8")
                      if cm.CONFIG_FILE.exists() else None)
        self.h = _Harness()

    def tearDown(self):
        if self._orig is not None:
            cm.CONFIG_FILE.write_text(self._orig, encoding="utf-8")

    def test_each_button_saves_and_applies_its_value(self):
        for label, seconds in WINDOWS:
            with self.subTest(button=label):
                self.h.click(label)
                self.assertEqual(cm.get_wake_sleep_timeout(), float(seconds))
                self.assertEqual(self.h.applied[-1], seconds)

    def test_the_label_reflects_the_current_window(self):
        self.h.win._refresh_wake_btns()
        for label, seconds in WINDOWS:
            with self.subTest(window=label):
                self.h.sess._wake_sleep_timeout = float(seconds)
                self.h.win._refresh_wake_btns()
                expected = "∞" if seconds == 0 else f"{seconds}s"
                self.assertIn(expected, self.h.win._wake_btn.text())

    def test_the_active_button_is_checked(self):
        self.h.sess._wake_sleep_timeout = 30.0
        self.h.win._refresh_wake_btns()
        checked = [b.text() for b, _ in self.h.win._wake_timeout_btns if b.isChecked()]
        self.assertEqual(checked, ["30s"])

    def test_clicking_repaints_immediately(self):
        """A click that saves but does not repaint leaves the old button marked
        for as long as the drawer stays open — the user cannot tell the change
        took, and the label keeps lying about the window."""
        self.h.sess._wake_sleep_timeout = 20.0
        self.h.win._refresh_wake_btns()
        self.assertEqual(
            [b.text() for b, _ in self.h.win._wake_timeout_btns if b.isChecked()],
            ["20s"])
        self.h.click("10s")
        self.assertEqual(
            [b.text() for b, _ in self.h.win._wake_timeout_btns if b.isChecked()],
            ["10s"],
            "the clicked window is not marked as active")
        self.assertIn("10s", self.h.win._wake_btn.text(),
                      "the wake button label still shows the old window")

    def test_row_is_hidden_when_the_wake_word_is_off(self):
        """A visible choice that does nothing is worse than no choice."""
        self.h.sess._wake_enabled = False
        self.h.win._refresh_wake_btns()
        self.assertEqual(self.h.win._wake_btn.text(), "🎙  WAKE WORD: OFF")
        self.assertFalse(any(b.isVisible() for b, _ in self.h.win._wake_timeout_btns))

    def test_a_stopped_detector_is_called_out(self):
        """'enabled' and 'listening' are different; confusing them is exactly
        how you end up believing a wake word is on when it never fires."""
        self.h.sess._wake_enabled = True
        self.h.sess._wake_sleep_timeout = 20.0
        self.h.sess._wake_detector = None
        self.h.win._refresh_wake_btns()
        self.assertIn("NO ESCUCHA", self.h.win._wake_btn.text())


class TestWakeState(unittest.TestCase):
    def test_labels(self):
        s = _Session()
        s._wake_sleep_timeout = 0.0
        self.assertEqual(M.JarvisLive._wake_state(s)["timeout_label"], "∞")
        s._wake_sleep_timeout = 20.0
        self.assertEqual(M.JarvisLive._wake_state(s)["timeout_label"], "20s")
        s._wake_sleep_timeout = 120.0
        self.assertEqual(M.JarvisLive._wake_state(s)["timeout_label"], "2min")

    def test_state_carries_the_new_keys(self):
        st = M.JarvisLive._wake_state(_Session())
        for key in ("detector_running", "timeout", "timeout_label"):
            self.assertIn(key, st)

    def test_live_setter_updates_the_running_window(self):
        s = _Session()
        s._awake = False
        ui = types.SimpleNamespace(
            write_log=lambda _m: None, set_state=lambda _x: None)
        s.ui = ui
        M.JarvisLive._set_wake_timeout_live(s, 30)
        self.assertEqual(s._wake_sleep_timeout, 30.0)
        M.JarvisLive._set_wake_timeout_live(s, -5)
        self.assertEqual(s._wake_sleep_timeout, 0.0)


if __name__ == "__main__":
    unittest.main()
