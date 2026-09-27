"""The Deep Eye panel and the header button that opens it.

These build the real MainWindow and the real panel. Three things are being
locked in, each of which was a real bug while the panel was being written:

  * `QTextEdit.LineWrap` does not exist in PyQt6 — the enum is
    `QTextEdit.LineWrapMode` — and getting it wrong raised AttributeError in
    the constructor, so the panel could never be opened.
  * The header button must sit LEFT of the clock. Placed after it, it reads as
    a clock control rather than a scanner.
  * The action has no `self`: the loader calls `fn(parameters=..., **ctx)`, so
    a handler written as a method only fails when a voice scan is actually
    attempted.

And the property that matters most: **nothing here starts a scan.** Opening the
panel, or asking by voice, raises the confirmation gate and stops. A scan of a
third-party site is an active attack, and it must never begin from a click.
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
import ui_deep_eye as D  # noqa: E402
import actions.deep_eye as A  # noqa: E402
from core import deep_eye as de  # noqa: E402
# worst_severity moved to core/deep_eye_session.py with the rest of the
# session-side Deep Eye code, the same way JarvisLive does. Imported from
# there, not from main.
from core.deep_eye_session import worst_severity  # noqa: E402


class PanelTestCase(unittest.TestCase):
    def setUp(self):
        self.w = U.MainWindow.__new__(U.MainWindow)
        U.MainWindow.__init__(self.w, "face.png")
        self.w.resize(1200, 800)
        self.w.show()
        _app.processEvents()

    def tearDown(self):
        self.w.close()


class TestHeaderButton(PanelTestCase):
    def test_the_button_exists_next_to_the_clock(self):
        btn = getattr(self.w, "_deep_eye_btn", None)
        self.assertIsNotNone(btn, "the Deep Eye button was never built")

    def test_it_sits_left_of_the_clock(self):
        """Placed after the clock it reads as part of the clock."""
        btn = self.w._deep_eye_btn
        clk = self.w._clock_lbl
        self.assertLess(btn.geometry().x(), clk.geometry().x(),
                        "the scanner button is to the RIGHT of the clock")

    def test_it_is_small_enough_not_to_crowd_the_header(self):
        self.assertLessEqual(self.w._deep_eye_btn.width(), 32)
        self.assertLessEqual(self.w._deep_eye_btn.height(), 32)

    def test_it_says_what_it_does_on_hover(self):
        tip = self.w._deep_eye_btn.toolTip().lower()
        self.assertIn("scanner", tip)
        self.assertIn("confirmation", tip)


class TestPanel(PanelTestCase):
    def test_opening_the_panel_works(self):
        """The LineWrap regression: this raised AttributeError outright."""
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        self.assertIsNotNone(getattr(self.w, "_de_panel", None))
        self.assertTrue(self.w._de_panel.isVisible())

    def test_closing_does_not_stop_a_running_scan(self):
        """Two different intents, two controls. Hiding a panel the user is
        watching must not kill a scan they still want."""
        self.w._toggle_deep_eye(True)
        panel = self.w._de_panel
        stopped = []
        panel.on_stop = lambda: stopped.append(1)
        panel.close_panel()
        self.assertEqual(stopped, [], "closing the panel stopped the scan")

    def test_stop_calls_the_handler(self):
        self.w._toggle_deep_eye(True)
        panel = self.w._de_panel
        stopped = []
        panel.on_stop = lambda: stopped.append(1)
        panel._on_stop()
        self.assertEqual(stopped, [1])

    def test_a_raising_stop_handler_does_not_propagate(self):
        self.w._toggle_deep_eye(True)
        panel = self.w._de_panel

        def boom():
            raise RuntimeError("no live session")

        panel.on_stop = boom
        panel._on_stop()          # must not raise

    def test_output_is_cleaned_for_display(self):
        """ANSI colour, box-drawing and emoji all arrive from the scanner; at
        8pt Qt renders emoji as tofu and box characters as noise."""
        raw = "\x1b[1;32m\u2500\u2500 Scanning \U0001F40D target \u2500\u2500"
        out = D.clean_line(raw)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\U0001F40D", out)
        self.assertNotIn("\u2500", out)
        self.assertIn("Scanning", out)
        self.assertIn("target", out)

    def test_blank_lines_are_dropped(self):
        self.assertEqual(D.clean_line("   "), "")
        self.assertEqual(D.clean_line("\u2500\u2500\u2500"), "")

    def test_a_very_long_scan_does_not_grow_without_bound(self):
        """A scan emits several hundred lines. Without a cap the document grows
        for the life of the window, and each append gets slower — the panel
        would progressively freeze mid-scan."""
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        panel = self.w._de_panel
        for i in range(900):
            panel.append_line(f"line {i}")
        self.assertLessEqual(panel._log.document().blockCount(), 501,
                             "the log is unbounded")
        # ...and it is still showing the newest output, not the oldest.
        self.assertIn("line 899", panel._log.toPlainText())

    def test_severity_is_written_out_not_only_coloured(self):
        """Colour alone does not survive greyscale or colour-blindness, and
        the HUD is small."""
        self.w._toggle_deep_eye(True)
        panel = self.w._de_panel
        res = de.ScanResult(target="x", returncode=0, findings=[
            {"severity": "high"}, {"severity": "medium"}])
        panel.show_findings(res)
        text = panel._verdict.text()
        for word in ("HIGH", "MEDIUM"):
            self.assertIn(word, text)

    def test_no_findings_reads_as_a_sentence(self):
        self.w._toggle_deep_eye(True)
        panel = self.w._de_panel
        panel.show_findings(de.ScanResult(target="x", returncode=0, findings=[]))
        self.assertIn("No findings", panel._verdict.text())


class TestIdleState(PanelTestCase):
    """The reported bug: the popup opened and showed nothing at all.

    An empty log box above a blank verdict is indistinguishable from a broken
    feature — Thomas could not tell whether Deep Eye had failed to start or
    whether it was simply waiting. So the resting state says what it is and
    offers the one action available, and the log stays hidden until it has
    something in it.
    """

    def test_opening_shows_instructions_not_a_blank_box(self):
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        panel = self.w._de_panel
        self.assertTrue(panel._idle.isVisible(), "the idle help is not shown")
        self.assertIn("permission", panel._idle.text().lower())
        self.assertIn("confirm", panel._idle.text().lower())

    def test_the_empty_log_is_hidden_when_idle(self):
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        self.assertFalse(self.w._de_panel._log.isVisible(),
                         "an empty log box is what looked broken")

    def test_the_two_states_are_mutually_exclusive(self):
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        panel = self.w._de_panel
        panel.begin("example.com")
        self.assertFalse(panel._idle.isVisible())
        self.assertTrue(panel._log.isVisible())

    def test_there_is_a_way_to_type_a_target(self):
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        panel = self.w._de_panel
        self.assertTrue(panel._target_edit.isEnabled())
        self.assertTrue(panel._scan_btn.isEnabled())

    def test_the_scan_button_does_not_scan(self):
        """It asks. A click must never start an attack on a third party."""
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        panel = self.w._de_panel
        asked = []
        panel.on_request = lambda t: asked.append(t)
        panel._target_edit.setText("example.com")
        panel._scan_btn.click()
        self.assertEqual(asked, ["example.com"])

    def test_an_empty_target_asks_instead_of_doing_nothing(self):
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        panel = self.w._de_panel
        asked = []
        panel.on_request = lambda t: asked.append(t)
        panel._target_edit.setText("   ")
        panel._scan_btn.click()
        self.assertEqual(asked, [])
        self.assertIn("site", panel._status.text().lower())

    def test_a_missing_owner_says_so_instead_of_hanging(self):
        """A dead SCAN button with no explanation reads as a crash."""
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        panel = self.w._de_panel
        panel.on_request = None
        panel._target_edit.setText("example.com")
        panel._start_clicked()
        self.assertIn("not available", panel._status.text().lower())
        self.assertTrue(panel._scan_btn.isEnabled())

    def test_an_raising_owner_restores_the_button(self):
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        panel = self.w._de_panel

        def boom(t):
            raise RuntimeError("gate unavailable")

        panel.on_request = boom
        panel._target_edit.setText("example.com")
        panel._start_clicked()
        self.assertTrue(panel._scan_btn.isEnabled())
        self.assertIn("could not start", panel._status.text().lower())

    def test_pressing_enter_asks_too(self):
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        panel = self.w._de_panel
        asked = []
        panel.on_request = lambda t: asked.append(t)
        panel._target_edit.setText("example.com")
        panel._target_edit.returnPressed.emit()
        self.assertEqual(asked, ["example.com"])


class TestReopening(PanelTestCase):
    def test_reopening_after_a_scan_does_not_show_the_old_results(self):
        """Otherwise the previous run's output sits under a fresh status
        line, and reads as the current state of the site."""
        self.w._toggle_deep_eye(True)
        panel = self.w._de_panel
        panel.begin("example.com")
        panel.append_line("SQLi: 2 candidates  <-- HIGH")
        panel.end(True, "1 findings\nHIGH: 1")
        panel.close_panel()
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        self.assertEqual(panel._log.toPlainText(), "")
        self.assertTrue(panel._idle.isVisible())

    def test_reopening_during_a_scan_keeps_the_output(self):
        """The user closed it to see the face and came back — wiping the log
        mid-scan would look like the scan died."""
        self.w._toggle_deep_eye(True)
        panel = self.w._de_panel
        panel.begin("example.com")
        panel.append_line("still scanning")
        panel.close_panel()
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        self.assertIn("still scanning", panel._log.toPlainText())
        self.assertTrue(panel._running)

    def test_a_second_scan_can_be_asked_for_after_the_first(self):
        self.w._toggle_deep_eye(True)
        panel = self.w._de_panel
        panel.begin("example.com")
        panel.end(True, "done")
        self.assertTrue(panel._scan_btn.isEnabled(),
                        "the target row stayed dead after a finished scan")


class TestWorstSeverity(unittest.TestCase):
    def test_reports_the_most_serious(self):
        res = de.ScanResult(target="x", returncode=0, findings=[
            {"severity": "low"}, {"severity": "critical"}, {"severity": "medium"}])
        self.assertEqual(worst_severity(res), "critical")

    def test_none_when_clean(self):
        res = de.ScanResult(target="x", returncode=0, findings=[])
        self.assertIsNone(worst_severity(res))

    def test_ignores_severities_it_does_not_know(self):
        res = de.ScanResult(target="x", returncode=0, findings=[
            {"severity": "banana"}, {"severity": "high"}])
        self.assertEqual(worst_severity(res), "high")


class TestVoiceAction(PanelTestCase):
    """The gate, from the side a user actually reaches it."""

    def setUp(self):
        super().setUp()
        self.orig = A.de.request_scan
        self.seen = {}

    def tearDown(self):
        A.de.request_scan = self.orig
        A.bind_session()
        super().tearDown()

    def spy(self, target, confirm, **kw):
        self.seen["target"] = target
        self.seen["cbs"] = sorted(k for k, v in kw.items() if callable(v))
        return "I need your confirmation on the HUD before scanning."

    def test_voice_raises_the_gate_and_does_not_scan(self):
        A.de.request_scan = self.spy
        out = A.deep_eye({"action": "scan", "target": "example.com"})
        self.assertIn("confirmation", out.lower())
        self.assertEqual(self.seen["target"], "example.com")

    def test_the_session_callbacks_are_passed_through(self):
        A.de.request_scan = self.spy
        A.bind_session(on_begin=lambda t: None, on_line=lambda l: None,
                       on_done=lambda ok, p: None)
        A.deep_eye({"action": "scan", "target": "example.com"})
        self.assertEqual(self.seen["cbs"], ["on_begin", "on_done", "on_line"])

    def test_the_handler_takes_no_self(self):
        """It is called as fn(parameters=..., **ctx). A method signature would
        only fail when a voice scan was really attempted."""
        import inspect
        params = list(inspect.signature(A.deep_eye).parameters)
        self.assertNotIn("self", params)
        self.assertEqual(params[0], "params")

    def test_a_missing_target_asks_instead_of_guessing(self):
        out = A.deep_eye({"action": "scan", "target": ""})
        self.assertIn("domain", out.lower())

    def test_status_is_honest_when_not_installed(self):
        original = de.available
        de.available = lambda: False
        try:
            out = A.deep_eye({"action": "status"})
            self.assertIn("not installed", out.lower())
        finally:
            de.available = original

    def test_unknown_action_lists_what_it_can_do(self):
        out = A.deep_eye({"action": "launch"})
        self.assertIn("scan", out)
        self.assertIn("status", out)


if __name__ == "__main__":
    unittest.main()
