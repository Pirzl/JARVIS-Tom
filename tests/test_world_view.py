"""The globe panel must be constructible, not merely importable.

A panel that only fails when a user clicks the button is the worst outcome:
the feature looks present and does nothing. So this builds the real widget and
asks it things after showing it, because an unshown widget reports itself and
its children as hidden and that reading is fiction.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication

# Must happen before the QApplication exists and before anything pulls in
# QtWebEngine, or the render process dies on the first page load.
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

import ui_world_view as wv


def _app():
    # sys.argv, not []. An empty argument list makes the QtWebEngine render
    # process die on the first page load -- silently, with no traceback, so the
    # test run just stops early and looks like a pass. Found by bisection: the
    # same script survived with sys.argv and died with []. The application
    # itself already does the right thing in JarvisUI.
    a = QApplication.instance() or QApplication(sys.argv)
    return a


class TestGlobePanelBuilds(unittest.TestCase):
    def setUp(self):
        self.app = _app()

    def test_the_panel_constructs(self):
        p = wv.GlobePanel()
        self.assertIsNotNone(p)
        self.assertGreater(p.width(), 0)

    def test_it_reports_a_readable_idle_state(self):
        p = wv.GlobePanel()
        p.show()
        self.app.processEvents()
        # Status is a word, not just a colour: the test asserts the word.
        self.assertIn("idle", p._status.text())
        self.assertTrue(p._placeholder.text())

    def test_closing_hides_and_does_not_stop_the_server(self):
        p = wv.GlobePanel()
        p.show()
        self.app.processEvents()
        p.close()
        self.app.processEvents()
        self.assertFalse(p.isVisible(),
                         "CLOSE must hide the panel")
        self.assertIsNone(p._process,
                          "CLOSE must not kill a server that was never started")

    def test_stop_without_a_server_is_harmless(self):
        p = wv.GlobePanel()
        p.stop_server()          # must not raise
        self.assertIn("stopped", p._status.text() + " " + _status_all(p))

    def test_it_refuses_to_pretend_when_nothing_is_installed(self):
        # Simulates a fresh clone with no vendor checkout: the button must
        # report that, not open a panel that can only fail later.
        p = wv.GlobePanel()
        original = wv.available
        wv.available = lambda: False
        try:
            ok = p.start_server()
        finally:
            wv.available = original
        self.assertFalse(ok)
        self.assertIn("not installed", p._status.text())

    def test_missing_webengine_is_reported_not_raised(self):
        p = wv.GlobePanel()
        import builtins
        real_import = builtins.__import__

        def fake(name, *a, **k):
            if name == "PyQt6.QtWebEngineWidgets":
                raise ImportError("simulated: PyQt6-WebEngine not installed")
            return real_import(name, *a, **k)

        builtins.__import__ = fake
        try:
            ok = p.ensure_view()
        finally:
            builtins.__import__ = real_import
        self.assertFalse(ok, "a missing package must be reported, not raised")
        self.assertIn("WebEngine", p._status.text())


class TestStartCrossesThreadsCorrectly(unittest.TestCase):
    """The failure this guards took the longest to find, and it was silent.

    A cold start blocks for about twenty seconds waiting for the port. Doing that
    on the UI thread freezes the HUD -- the face, the clock and the voice all
    stop -- and the feature still "works", so a test that only checks the panel
    came up would pass while the assistant is unusable.

    The subtler half: the worker has to hand control back across a thread
    boundary. QTimer.singleShot(0, ...) called *from* the worker was tried first
    and the callback silently never ran, leaving a panel with a working server,
    no view, and no error to explain why. A signal is the mechanism that
    actually crosses.
    """

    def setUp(self):
        self.app = _app()

    def test_starting_does_not_block_the_caller(self):
        """The whole point: the caller must not wait for the port.

        start_server is replaced with a slow stub, because the real one launches
        a Node process. A test suite must not spawn servers -- and with the real
        one this test killed the interpreter, which is a second way of learning
        the same lesson.
        """
        import time as _t
        p = wv.GlobePanel()
        p.start_server = lambda: (_t.sleep(2.0), False)[1]
        t0 = _t.perf_counter()
        p.start_server_async()
        returned = _t.perf_counter() - t0
        self.assertLess(returned, 0.5,
                        "start_server_async must return immediately, not wait "
                        "for the server")
        self.assertTrue(p._starting)
        p.stop_server()

    def test_a_second_click_does_not_launch_a_second_server(self):
        import time as _t
        p = wv.GlobePanel()
        calls = []
        p.start_server = lambda: (calls.append(1), _t.sleep(0.4), False)[-1]
        p.start_server_async()
        p.start_server_async()          # second click while starting
        _t.sleep(0.9)
        self.assertEqual(len(calls), 1,
                         "a second click must not launch a second server")

    def test_the_worker_hands_back_through_a_signal_not_a_timer(self):
        """The regression that produced a server with no view.

        Asserts the mechanism, not the outcome: the signal exists, is connected,
        and the worker's success path emits it. Removing the emit leaves a panel
        that starts a server and never shows anything.
        """
        import inspect
        src = inspect.getsource(wv.GlobePanel._start_worker)
        self.assertIn("_started_ok.emit()", src,
                      "the worker must signal the Qt thread, not call a timer")
        self.assertNotIn("self.ensure_view()", src,
                         "the worker must not touch widgets directly")

    def test_the_panel_creates_its_view_on_the_signal(self):
        """What the signal must cause -- asserted without starting WebEngine.

        The obvious version of this test emits the signal and checks the view
        exists. That test kills the interpreter: constructing a real
        QWebEngineView from inside a pytest process is what brought the render
        process down every time, and a dead interpreter looks like a truncated
        run rather than a failure.

        So the connection is asserted by substituting ensure_view and checking
        it is the thing that gets called. That is the wiring under test; the
        WebEngine construction itself is covered by driving the real button in
        scratch/, where a crash is visible.
        """
        p = wv.GlobePanel()
        called = []
        p.ensure_view = lambda: called.append(1)
        p._started_ok.emit()
        self.app.processEvents()
        self.assertEqual(called, [1],
                         "the success signal must make the panel build its view")

    def test_the_signal_is_connected_to_the_slot(self):
        """A declared-but-unconnected signal is the quietest failure in GUI
        code: nothing raises, the feature does nothing, the suite is green."""
        p = wv.GlobePanel()
        try:
            receivers = p.receivers(p._started_ok)
        except (TypeError, RuntimeError):
            receivers = p._started_ok.receivers(p._started_ok)
        self.assertTrue(receivers,
                        "_started_ok must be connected to a slot")


def _status_all(panel):
    return panel._status.text()


if __name__ == "__main__":
    unittest.main()
