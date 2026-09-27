"""The listening-window buttons must be visible AND readable.

Thomas' screenshot showed four blank rectangles under WAKE WORD. They existed,
were clickable and worked — but you could not read them, which makes them
useless. Two separate causes, both locked in here:

1. Stacked vertically, four more 26px rows overflowed the drawer, so they were
   clipped. They now sit in ONE row, verified by geometry rather than by
   isVisible() alone.
2. They carried no stylesheet, so the text rendered dark-on-dark. The checked
   state also needs its own visible style, otherwise "which one is active" is
   just as unreadable as the labels.

`isVisible()` is useless as a check until the window has been shown and the
event loop pumped — see jarvis-pyqt-ui-tips.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from PyQt6.QtWidgets import QApplication, QHBoxLayout, QWidget  # noqa: E402

_app = QApplication.instance() or QApplication([])

import main as M  # noqa: E402
import ui as U  # noqa: E402


class DrawerRenderTest(unittest.TestCase):
    def setUp(self):
        self.w = U.MainWindow.__new__(U.MainWindow)
        U.MainWindow.__init__(self.w, "face.png")
        self.sess = types.SimpleNamespace(
            _wake_enabled=True, _awake=False, _wake_detector=None,
            _wake_sleep_timeout=20.0, ui=self.w)
        self.w.wake_get_state = lambda: M.JarvisLive._wake_state(self.sess)
        self.w.wake_is_ready = lambda: True
        self.w.on_wake_toggle = None
        self.w.on_wake_set_timeout = lambda s: setattr(
            self.sess, "_wake_sleep_timeout", s)
        self.w.resize(1200, 800)
        self.w.show()
        _app.processEvents()
        self.w._toggle_drawer(True)
        _app.processEvents()

    def tearDown(self):
        self.w.close()

    def btns(self):
        return self.w._wake_timeout_btns


class TestLegibility(DrawerRenderTest):
    def test_every_button_has_a_label(self):
        for b, _ in self.btns():
            self.assertTrue(b.text().strip(), "a button has no text")

    def test_labels_are_the_documented_windows(self):
        self.assertEqual([b.text() for b, _ in self.btns()],
                         ["10s", "20s", "30s", "OFF"])

    def test_buttons_are_wide_enough_for_their_text(self):
        """A 7pt monospace label needs room; a button narrower than its text
        clips the text even though isVisible() is True."""
        for b, _ in self.btns():
            self.assertGreaterEqual(
                b.width(), b.sizeHint().width(),
                f"{b.text()!r} is {b.width()}px wide, needs "
                f"{b.sizeHint().width()}px")

    def test_buttons_stay_compact(self):
        """…and not wider than they need to be.

        Without a minimum width, four buttons in a 220px drawer were each
        given the full width, and stacked they became four near-full-width
        rows. The upper bound is what forces them to stay a compact row.
        """
        for b, _ in self.btns():
            self.assertLessEqual(
                b.width(), 70,
                f"{b.text()!r} stretched to {b.width()}px — the row is not "
                f"compact, so it will not fit next to its siblings")

    def test_each_button_declares_a_minimum_width(self):
        """The layout alone will not keep these readable.

        Left to itself, a QHBoxLayout divides the available width evenly, so
        the buttons came out at whatever fitted — 47px here by luck of the
        drawer's size, and clipped somewhere else. The explicit minimum is
        what guarantees the 7pt label is never cut, whatever the drawer width
        or the label text. Asserting the geometry alone would pass by
        coincidence, so assert the declared intent too.
        """
        for b, _ in self.btns():
            self.assertGreaterEqual(
                b.minimumWidth(), b.sizeHint().width(),
                f"{b.text()!r} declares a minimum narrower than its text needs")

    def test_the_row_does_not_exceed_the_drawer_width(self):
        """Row plus margins must stay inside the drawer; a row that overflows
        is what pushed the controls past the bottom edge."""
        d = self.w._quick_drawer
        left = min(b.geometry().x() for b, _ in self.btns())
        right = max(b.geometry().x() + b.geometry().width() for b, _ in self.btns())
        self.assertLessEqual(right, d.width(),
                             f"the row ends at x={right} but the drawer is "
                             f"{d.width()}px wide")
        self.assertGreaterEqual(left, 0)

    def test_the_active_one_is_visibly_distinct(self):
        """Not just checked — styled, so it reads at a glance."""
        self.sess._wake_sleep_timeout = 30.0
        self.w._toggle_drawer(False)
        self.w._toggle_drawer(True)
        _app.processEvents()
        checked = [b for b, _ in self.btns() if b.isChecked()]
        self.assertEqual([b.text() for b in checked], ["30s"])
        self.assertIn(":checked", checked[0].styleSheet())


class TestLayout(DrawerRenderTest):
    def test_they_sit_in_one_row_not_a_column(self):
        """Four stacked 26px rows overran the drawer and were clipped to blank
        boxes — visible in a screenshot as four empty rectangles."""
        ys = {b.geometry().y() for b, _ in self.btns()}
        self.assertEqual(len(ys), 1, f"buttons stacked vertically at y={ys}")

    def test_nothing_overflows_the_drawer(self):
        d = self.w._quick_drawer
        for b, _ in self.btns():
            g = b.geometry()
            self.assertLessEqual(
                g.y() + g.height(), d.height(),
                f"{b.text()!r} ends at y={g.y() + g.height()} but the drawer "
                f"is only {d.height()}px tall")

    def test_the_row_is_a_horizontal_layout(self):
        """The four buttons must be placed by one QHBoxLayout.

        `lay.addLayout(row)` does not reparent them: they stay children of the
        drawer, so `parentWidget()` cannot tell a row from a column. What
        distinguishes them is that a single horizontal layout owns all four —
        a column would have been four separate vertical placements.

        Uses `layout.itemAt(i)`, never `_items`: PyQt6 exposes no such
        attribute, and a test that reaches for one fails for the wrong reason.
        """
        drawer = self.w._quick_drawer
        names = {b.text() for b, _ in self.btns()}
        for lw in drawer.findChildren(QHBoxLayout):
            found = set()
            for i in range(lw.count()):
                w_ = lw.itemAt(i).widget()
                if w_ is not None and w_.text() in names:
                    found.add(w_.text())
            if found == names:
                return
        self.fail("no single horizontal layout places all four window buttons")


class TestInteraction(DrawerRenderTest):
    def test_clicking_changes_the_window_and_says_so(self):
        for b, _ in self.btns():
            if b.text() == "10s":
                b.click()
        _app.processEvents()
        self.assertEqual(self.sess._wake_sleep_timeout, 10)
        self.assertIn("10s", self.w._wake_btn.text())
        checked = [x.text() for x, _ in self.btns() if x.isChecked()]
        self.assertEqual(checked, ["10s"])

    def test_off_means_never_sleep(self):
        for b, seconds in self.btns():
            if b.text() == "OFF":
                b.click()
                self.assertEqual(seconds, 0)
        _app.processEvents()
        self.assertEqual(self.sess._wake_sleep_timeout, 0)
        self.assertIn("∞", self.w._wake_btn.text())


if __name__ == "__main__":
    unittest.main()
