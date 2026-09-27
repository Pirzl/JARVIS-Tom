"""The whole Deep Eye feature, assembled.

Every other test builds one piece: the bridge alone, the panel alone, the mixin
alone. This one builds the real MainWindow the way main.py does and checks
what only exists once everything is together — because a signal that was
declared but never connected, a method added to the wrong class, or a mixin
that was inherited but never bound, all pass every isolated test and then fail
the first time a scan actually runs.

The regression this file exists for: the `deep_eye_*` methods were first added
to MainWindow instead of the JarvisUI proxy. JarvisLive calls them as
`self.ui.deep_eye_line(...)`, so the proxy is where they have to be — and the
mistake was invisible until the whole assembly was checked at once.
"""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import main as M  # noqa: E402
import ui as U  # noqa: E402
import actions.deep_eye as A  # noqa: E402
from core.deep_eye_session import DeepEyeSessionMixin  # noqa: E402

PROXY_METHODS = ("deep_eye_begin", "deep_eye_line", "deep_eye_done",
                 "deep_eye_findings", "deep_eye_activity")
SIGNALS = ("_de_begin_sig", "_de_line_sig", "_de_done_sig",
           "_de_findings_sig", "_de_activity_sig")


class TestAssembly(unittest.TestCase):
    def setUp(self):
        self.w = U.MainWindow.__new__(U.MainWindow)
        U.MainWindow.__init__(self.w, "face.png")
        self.w.resize(1200, 800)
        self.w.show()
        _app.processEvents()

    def tearDown(self):
        self.w.close()


class TestTheProxyCarriesTheApi(TestAssembly):
    """JarvisLive calls self.ui.<method>; JarvisUI is what self.ui is."""

    def test_every_method_is_on_the_proxy(self):
        for m in PROXY_METHODS:
            self.assertTrue(hasattr(U.JarvisUI, m),
                            f"JarvisUI.{m} is missing — main.py would raise "
                            f"AttributeError in the middle of a scan")

    def test_they_emit_rather_than_touch_widgets(self):
        """They are called from the scanner's reader thread. Painting from
        there is a crash, not a warning."""
        for m in PROXY_METHODS:
            src = inspect.getsource(getattr(U.JarvisUI, m))
            self.assertIn("_win._de_", src,
                          f"{m} does not go through a signal")


class TestSignalsAreWired(TestAssembly):
    def test_the_signals_exist(self):
        for s in SIGNALS:
            self.assertTrue(hasattr(U.MainWindow, s), f"{s} was never declared")

    def test_emitting_a_line_reaches_the_panel(self):
        """A declared-but-unconnected signal is the quiet failure: the scan
        runs, nothing appears, and the button looks broken.

        Asserted on the text, not on blockCount(): a QTextEdit starts with one
        empty block, and the first appended line replaces it rather than adding
        to it — so the count does not move even though the content changed.
        """
        self.w._toggle_deep_eye(True)
        _app.processEvents()
        panel = self.w._de_panel
        self.w._de_line_sig.emit("line via the real signal")
        _app.processEvents()
        self.assertIn("line via the real signal", panel._log.toPlainText(),
                      "the line signal never reached the panel")

    def test_emitting_activity_reaches_the_avatar(self):
        self.w._de_activity_sig.emit("SCANNING example.com")
        _app.processEvents()
        self.assertEqual(self.w.hud.activity, "SCANNING example.com")

    def test_emitting_with_no_panel_does_not_raise(self):
        """A voice scan with the button never pressed must not blow up: the
        panel is created on click, so these can arrive first."""
        for s, arg in ((self.w._de_line_sig, "x"),
                       (self.w._de_activity_sig, "y")):
            s.emit(arg)             # must not raise
        _app.processEvents()


class TestMixinIsBound(TestAssembly):
    def test_jit(self):
        self.assertIn(DeepEyeSessionMixin, M.JarvisLive.__mro__)

    def test_main_calls_bind(self):
        src = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn("self.bind_deep_eye()", src,
                      "the mixin is inherited but never bound — the action "
                      "would have no callbacks and STOP would do nothing")

    def test_the_action_is_bound_by_the_mixin_not_by_hand(self):
        src = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("_de_action.bind_session(", src,
                         "the binding moved into the mixin; a second copy in "
                         "main.py would shadow it")


class TestVoiceReachesTheGate(TestAssembly):
    def setUp(self):
        super().setUp()
        self.orig = A.de.request_scan
        self.seen = {}

    def tearDown(self):
        A.de.request_scan = self.orig
        A.bind_session()
        super().tearDown()

    def test_the_voice_path_raises_the_gate_and_stops(self):
        A.de.request_scan = lambda t, c, **kw: (
            self.seen.update(target=t), "I need your confirmation.")[1]
        out = A.deep_eye({"action": "scan", "target": "localhost"})
        self.assertIn("confirmation", out.lower())
        self.assertEqual(self.seen.get("target"), "localhost")

    def test_the_handler_signature_is_loader_shaped(self):
        params = list(inspect.signature(A.deep_eye).parameters)
        self.assertNotIn("self", params)
        self.assertEqual(params, ["params", "speak"])

    def test_nothing_starts_a_scan_without_the_gate(self):
        """Belt and braces: across this whole file, no test may reach
        DeepEyeScan.start(). A scan is an active attack on a server."""
        src = (BASE_DIR / "tests" / "test_deep_eye_ui.py").read_text(encoding="utf-8")
        self.assertNotIn(".start()", src)
        src2 = (BASE_DIR / "tests" / "test_deep_eye_session_extraction.py").read_text(
            encoding="utf-8")
        self.assertNotIn(".start()", src2)


if __name__ == "__main__":
    unittest.main()
