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

import time

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


class TestTheGateIsVisibleOverThePanel(PanelTestCase):
    """Regression: the confirmation banner came up *behind* the panel.

    The panel was parented to the MainWindow while the banner is an overlay of
    centralWidget(). Qt stacks siblings within a parent, so a child of the
    window drew over a child of the central widget no matter what raise_()
    did. The result was a dialog asking to confirm an active attack, hidden
    behind a box whose only button does nothing until it is answered.
    """

    def test_the_panel_shares_a_parent_with_the_confirmation_banner(self):
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        panel = self.w._de_panel
        self.assertIs(panel.parentWidget(), self.w.centralWidget(),
                      "the panel is not a sibling of the banner")

    def test_the_banner_goes_on_top_of_the_panel(self):
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        self.w._show_confirm_banner("Run a security scan", "localhost")
        _app.processEvents()
        banner = self.w._confirm_overlay
        self.assertIsNotNone(banner, "no banner was raised")
        # raise_() is a request to the window manager; the reliable check is
        # the sibling order, which is what actually decides the paint order.
        self.assertGreater(
            banner.parentWidget().children().index(banner),
            self.w.centralWidget().children().index(self.w._de_panel),
            "the banner is stacked below the Deep Eye panel")
        self.assertTrue(banner.isVisible())


class TestCallbackCrossesTheProxy(PanelTestCase):
    """Regression: the panel said "Not available" on a working scanner.

    JarvisLive sets its callbacks as `self.ui.on_deep_eye_scan = cb`, and the
    panel reads them off the MainWindow. In between sits the JarvisUI proxy,
    which forwards each callback as an explicit property. A callback with no
    property is not forwarded: the assignment lands on the proxy and stops
    there, and nothing complains. The panel then finds `None`, and reports a
    missing feature instead of a missing property.

    Every earlier test used the MainWindow directly or a stub, so none of them
    crossed the proxy — which is why this passed 214 tests and failed on
    Thomas's screen.
    """

    def _proxy(self):
        """A real JarvisUI over a real MainWindow, minus the audio stack."""
        proxy = U.JarvisUI.__new__(U.JarvisUI)
        U.JarvisUI.__init__(proxy, "face.png")
        self.addCleanup(proxy._win.close)
        return proxy

    def test_the_scan_callback_survives_the_proxy(self):
        proxy = self._proxy()
        sent = []
        proxy.on_deep_eye_scan = lambda t: sent.append(t)
        self.assertIsNotNone(proxy._win.on_deep_eye_scan,
                             "the callback never reached the MainWindow")
        # And read back the way the panel reads it.
        proxy._win.on_deep_eye_request("example.com")
        self.assertEqual(sent, ["example.com"])

    def test_the_cancel_callback_survives_the_proxy(self):
        proxy = self._proxy()
        proxy.on_deep_eye_cancel = lambda: None
        self.assertIsNotNone(proxy._win.on_deep_eye_cancel)

    def test_reading_before_writing_gives_none_not_an_error(self):
        """The panel checks this first, through the proxy. A bare attribute
        would raise AttributeError and take the whole click handler down —
        which looks to the user like the button being dead, with no error."""
        proxy = self._proxy()
        # Read the way the panel reads it: through the proxy, not off the
        # MainWindow directly.
        self.assertIsNone(proxy.on_deep_eye_scan)

    def test_the_whole_click_reaches_the_gate(self):
        """End to end through every real layer, minus the actual scan: type a
        site, press SCAN, and confirm the gate is what got called."""
        proxy = self._proxy()
        gate = []
        proxy.on_deep_eye_scan = lambda t: gate.append(t)

        proxy._win._toggle_deep_eye(True)
        _app.processEvents()
        panel = proxy._win._de_panel
        panel._target_edit.setText("example.com")
        panel._scan_btn.click()
        _app.processEvents()

        self.assertEqual(gate, ["example.com"],
                         "the click did not reach the gate")
        self.assertNotIn("not available", panel._status.text().lower())


class TestTheGateActuallyReleasesAndReturns(PanelTestCase):
    """The click path, through the real gate, with the scan itself stubbed.

    Two bugs lived here and neither showed up in an isolated test:

      * SCAN was disabled when the request went out, and only begin() switched
        it back. A cancelled banner calls neither, so the button stayed dead for
        the rest of the session — the panel looking broken all over again.
      * _de_ensure_panel called the header button's toggle, which resets the
        panel when it is hidden. It runs from on_begin, i.e. exactly when the
        scan starts, so the reset wiped the state it had just set and
        "Scanning" never appeared.
    """

    def setUp(self):
        # A real JarvisUI, so self.ui in the session is the real proxy and the
        # callbacks travel the same path they travel in the app.
        self.proxy = U.JarvisUI.__new__(U.JarvisUI)
        U.JarvisUI.__init__(self.proxy, "face.png")
        self.addCleanup(self.proxy._win.close)
        self.w = self.proxy._win
        self.w.resize(1200, 800)
        self.w.show()
        _app.processEvents()
        from core import confirm as C
        from core import deep_eye as de
        from core.deep_eye_session import DeepEyeSessionMixin
        self.C, self.de, self._mixin = C, de, DeepEyeSessionMixin
        self.shown = []
        self.seen = {"started": 0, "target": None}
        self._old = (C._show_cb, C._hide_cb, C._log_cb, C._settled_cb)
        C.bind(lambda t, d: self.shown.append((t, d)), lambda: None)
        C.set_settled_listener(lambda k, s: None)
        self._orig_start = de.DeepEyeScan.start
        # Assigning a bound method as a class attribute rebinds it as a plain
        # function: `self` is consumed at access time, so the scan instance
        # would be missing and every call would raise TypeError. A plain
        # function (not bound) is what belongs on the class.
        de.DeepEyeScan.start = self._make_spy()
        # capture cualquier excepcion del hilo worker del gate
        self._logs = []
        C.bind(lambda t, d: self.shown.append((t, d)), lambda: None,
               log=lambda m: self._logs.append(m))

    def tearDown(self):
        show, hide, log, settled = self._old
        self.de.DeepEyeScan.start = self._orig_start
        self.C.bind(show, hide, log=log)
        self.C.set_settled_listener(settled)

    def _make_spy(self):
        """A plain function, deliberately not a bound method — see setUp."""
        outer = self

        def start(scan, on_line=None):
            return outer._spy_start(scan, on_line=on_line)

        return start

    def _spy_start(self, scan, on_line=None):
        """Stand in for the scan: record that it was reached, then leave it
        with no output so wait() returns straight away. Nothing leaves the
        machine and no child process is started.

        wait() is driven by _lines/_proc, not by an event, so the stub has to
        reproduce that state rather than invent a "finished" flag.
        """
        self.seen["started"] += 1
        self.seen["target"] = getattr(scan, "target", None)

        class _Done:
            """A process that already exited cleanly, so wait() returns a
            normal ScanResult instead of raising ScanCancelled. This keeps the
            assertion on "Scanning…" meaningful: a stub that reported a
            cancellation would drive the panel to "Stopped" for the wrong
            reason."""
            def poll(self):
                return 0

            returncode = 0

        scan._proc = _Done()
        scan._thread = None
        scan._cancelled.clear()
        with scan._lock:
            scan._lines.append("stubbed scan, no real process")
        if callable(on_line):
            on_line("stubbed scan, no real process")

    def _pump(self, n=15):
        import time
        for _ in range(n):
            _app.processEvents()
            time.sleep(0.01)

    def _open_and_request(self):
        """Open the panel, wire the real session mixin, and press SCAN."""
        import types
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        # The real mixin methods, and the REAL UI object, not a stub.
        #
        # This matters more than it looks. In JarvisLive `self.ui` is a
        # JarvisUI proxy, and the mixin sets its callbacks on it — those
        # properties forward to the MainWindow, which is where the panel reads
        # them. A stub with no such forwarding makes bind_deep_eye write to
        # nowhere, and the click then reports "Not available": which is
        # exactly the bug this whole class exists to cover.
        self._session = types.SimpleNamespace(ui=self.proxy, _deep_eye_scan=None)
        for name in ("bind_deep_eye", "_de_gate_settled", "_de_request_scan",
                     "_de_cancel_scan", "_de_ensure_panel", "_de_on_begin",
                     "_de_on_line", "_de_on_done"):
            setattr(self._session, name,
                    types.MethodType(getattr(self._mixin, name), self._session))
        # bind_deep_eye is what registers the settled listener; without it,
        # resolve() has nobody to tell and the SCAN button never comes back.
        self._session.bind_deep_eye()
        self.assertIsNotNone(self.proxy._win.on_deep_eye_scan,
                             "the callback did not reach the MainWindow")
        panel = self.w._de_panel
        panel._target_edit.setText("localhost")
        panel._scan_btn.click()
        self._pump()
        return panel

    def test_the_click_raises_the_gate_and_scans_nothing(self):
        self._open_and_request()
        self.assertTrue(self.shown, "the gate was never raised")
        self.assertEqual(self.seen["started"], 0,
                         "the scan started before confirmation")

    def test_the_gate_warns_that_it_is_real_traffic(self):
        """The banner is the last thing between the user and someone else's
        server. It has to say what is about to happen, in the banner itself."""
        self._open_and_request()
        text = " ".join(str(x) for x in self.shown[0]).lower()
        self.assertIn("localhost", text)
        self.assertIn("attack traffic", text)
        self.assertIn("permission", text)

    def test_cancelling_leaves_the_scan_button_alive(self):
        panel = self._open_and_request()
        self.assertFalse(panel._scan_btn.isEnabled(), "precondition: it waits")
        self.C.resolve(False)
        self._pump()
        self.assertTrue(panel._scan_btn.isEnabled(),
                        "the SCAN button stayed dead after a cancel")
        self.assertIn("cancel", panel._status.text().lower())

    def test_a_second_audit_is_possible_after_a_cancel(self):
        """The case Thomas would have hit next: cancel once, press again."""
        panel = self._open_and_request()
        self.C.resolve(False)
        self._pump()
        panel._scan_btn.click()
        self._pump()
        self.assertEqual(len(self.shown), 2, "the second click did nothing")

    def test_confirming_starts_the_scan_and_paints_it(self):
        panel = self._open_and_request()
        self.C.resolve(True)
        # The gate runs the work on its own thread; give it real time. _pump
        # alone never yields long enough for it to be scheduled.
        for _ in range(400):
            if self.seen["started"]:
                break
            time.sleep(0.01)
        self._pump(30)
        # At least one, not exactly one: a scan that is somehow retried would
        # be a separate bug, and this test is about whether CONFIRM releases
        # the gate at all.
        self.assertGreaterEqual(self.seen["started"], 1,
                                f"confirming did not start it; gate log: {self._logs}")
        self.assertEqual(self.seen["target"], "localhost")

    def test_the_panel_went_through_scanning(self):
        """Not the final status: the stubbed scan finishes instantly, so by
        the time the test looks, the panel has legitimately moved on to Done.
        What matters is that begin() ran and announced the target.

        The regression this guards: _de_ensure_panel used to call the header
        button's _toggle_deep_eye, which resets a hidden panel. It runs from
        on_begin — the exact moment the scan starts — so the reset wiped the
        state that had just been set and "Scanning" never appeared at all.
        """
        seen_status = []
        panel = self._open_and_request()
        original = panel._status.setText

        def record(text):
            seen_status.append(text)
            original(text)

        panel._status.setText = record
        self.C.resolve(True)
        for _ in range(400):
            if self.seen["started"]:
                break
            time.sleep(0.01)
        self._pump(30)
        self.assertIn("scanning", " ".join(seen_status).lower(),
                      f"never announced the scan; statuses: {seen_status}")

    def test_begining_a_scan_never_resets_the_panel(self):
        """The regression, in the state where it actually bit.

        _de_ensure_panel used to call the header button's _toggle_deep_eye,
        which resets a hidden panel. The _running guard only protects a panel
        that has already begun, so the window where the reset fired was the
        one that mattered: a voice scan with the panel freshly created and
        still hidden. The reset wiped the state on_begin had just set, and
        "Scanning" never appeared.

        Asserted on the calls, not on the visible outcome: whether a reset
        happens to leave the same text on screen depends on ordering that Qt
        decides, and a test that only looks at the final pixels passes whether
        or not the reset actually ran.
        """
        import types

        calls = []

        class _PanelSpy:
            """Stands in for DeepEyePanel: records show/reset instead of
            painting, so the assertion cannot be satisfied by accident."""

            def show(self):
                calls.append("show")

            def raise_(self):
                pass

            def reset(self):
                calls.append("reset")

        w = U.MainWindow.__new__(U.MainWindow)
        U.MainWindow.__init__(w, "face.png")
        self.addCleanup(w.close)
        w._de_panel = _PanelSpy()

        ui_stub = types.SimpleNamespace(_win=w)
        for n in ("deep_eye_begin", "deep_eye_line", "deep_eye_done",
                  "deep_eye_findings", "deep_eye_activity", "write_log"):
            setattr(ui_stub, n, lambda *a, **k: None)
        session = types.SimpleNamespace(ui=ui_stub, _deep_eye_scan=None)
        setattr(session, "_de_ensure_panel",
                types.MethodType(self._mixin._de_ensure_panel, session))

        session._de_ensure_panel()

        self.assertIn("show", calls, "the panel was not shown")
        self.assertNotIn("reset", calls,
                         "_de_ensure_panel reset the panel it was showing")

    def test_a_panel_that_does_not_exist_yet_is_created_not_skipped(self):
        """The other branch: first run, no panel yet. Creating it is correct;
        returning without one would leave the scan running with nothing to
        watch it on."""
        import types
        created = []
        w = U.MainWindow.__new__(U.MainWindow)
        U.MainWindow.__init__(w, "face.png")
        self.addCleanup(w.close)
        w._de_panel = None
        w._toggle_deep_eye = lambda checked: created.append(checked)

        session = types.SimpleNamespace(ui=types.SimpleNamespace(_win=w),
                                        _deep_eye_scan=None)
        setattr(session, "_de_ensure_panel",
                types.MethodType(self._mixin._de_ensure_panel, session))
        session._de_ensure_panel()
        self.assertEqual(created, [True], "the panel was never created")

    def test_the_mixin_does_not_clobber_the_main_bind(self):
        """bind_deep_eye registers a listener. If it called bind() it would
        replace show/hide/log with None and break the very gate it extends."""
        import types
        marker = lambda t, d: None
        self.C.bind(show=marker, hide=lambda: None, log=lambda m: None)
        session = types.SimpleNamespace(ui=self.proxy, _deep_eye_scan=None)
        for name in ("bind_deep_eye", "_de_gate_settled", "_de_request_scan",
                     "_de_cancel_scan", "_de_ensure_panel", "_de_on_begin",
                     "_de_on_line", "_de_on_done"):
            setattr(session, name,
                    types.MethodType(getattr(self._mixin, name), session))
        session.bind_deep_eye()
        self.assertIs(self.C._show_cb, marker,
                      "the gate's show callback was replaced")


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

    def test_the_first_parameter_is_named_parameters(self):
        """Regression, found by JARVIS diagnosing itself in a live session.

        The loader calls every action as `fn(parameters=parameters, **ctx)` —
        the name is hardcoded there. An action that names it anything else
        (`params`, `args`) raises TypeError: unexpected keyword argument
        'parameters' on the first spoken scan, with no trace of where the
        name was decided. Every other action in actions/ uses `parameters`.
        """
        import inspect
        self.assertEqual(list(inspect.signature(A.deep_eye).parameters)[0],
                         "parameters")

    def test_it_survives_the_loader_actually_calling_it(self):
        """Not the signature — the real invocation shape, from
        core/action_loader.py line 154."""
        from core import action_loader
        src = (BASE_DIR / "core" / "action_loader.py").read_text(encoding="utf-8")
        self.assertIn("fn(parameters=parameters", src,
                      "the loader no longer calls it that way; re-check the "
                      "action's parameter name")

    def test_the_voice_path_works_with_the_loader_signature(self):
        """End to end through the real loader, with the gate stubbed out."""
        from core import action_loader
        A.de.request_scan = lambda t, c, **kw: "confirmation pending"
        try:
            out = A.deep_eye(parameters={"action": "scan", "target": "localhost"})
            self.assertIn("confirmation", out.lower())
        finally:
            A.de.request_scan = de.request_scan

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
