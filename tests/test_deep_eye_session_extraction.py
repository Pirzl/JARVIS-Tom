"""The Deep Eye session mixin, and that the extraction preserved behaviour.

Third of the same mechanical extraction as core/live_config.py and
core/background_loops.py. It happened for a concrete reason rather than a
stylistic one: adding Deep Eye's session methods to main.py pushed it to 2,189
lines and failed the existing size guard
(`test_background_loops_extraction.py::test_main_is_smaller`).

Shape is asserted the same way as the other two: the methods live in the mixin,
JarvisLive inherits them, and main.py does not still carry its own copy — a
duplicate definition would silently win over the mixin and the tests below
would pass against the wrong code.
"""
from __future__ import annotations

import ast
import sys
import types
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import deep_eye as de  # noqa: E402
from core.deep_eye_session import (  # noqa: E402
    SEVERITY_ORDER, DeepEyeSessionMixin, worst_severity,
)

MIXIN_METHODS = (
    "bind_deep_eye",
    "_de_request_scan",
    "_de_cancel_scan",
    "_de_ensure_panel",
    "_de_on_begin",
    "_de_on_line",
    "_de_on_done",
)


def _class_methods(path: Path, class_name: str) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {n.name for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return set()


class TestShape(unittest.TestCase):
    def test_the_methods_are_in_the_mixin(self):
        have = _class_methods(BASE_DIR / "core" / "deep_eye_session.py",
                              "DeepEyeSessionMixin")
        for m in MIXIN_METHODS:
            self.assertIn(m, have, f"{m} is not in DeepEyeSessionMixin")

    def test_main_no_longer_carries_them(self):
        """A copy left behind in JarvisLive would shadow the mixin, and every
        behaviour test here would then be testing the wrong code."""
        have = _class_methods(BASE_DIR / "main.py", "JarvisLive")
        for m in MIXIN_METHODS:
            self.assertNotIn(m, have, f"{m} is still defined in JarvisLive")

    def test_jarvislive_inherits_the_mixin(self):
        import main as M
        self.assertIn(DeepEyeSessionMixin, M.JarvisLive.__mro__)

    def test_main_stayed_under_the_size_guard(self):
        lines = len((BASE_DIR / "main.py").read_text(encoding="utf-8").splitlines())
        self.assertLess(lines, 2100, f"main.py is {lines} lines")

    def test_it_is_used_not_merely_imported(self):
        """An import that nothing uses is how these extractions rot."""
        src = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn("self.bind_deep_eye()", src)


class TestWorstSeverity(unittest.TestCase):
    def test_reports_the_most_serious(self):
        res = de.ScanResult(target="x", returncode=0, findings=[
            {"severity": "low"}, {"severity": "critical"}, {"severity": "medium"}])
        self.assertEqual(worst_severity(res), "critical")

    def test_none_when_clean(self):
        self.assertIsNone(worst_severity(de.ScanResult(target="x", returncode=0)))

    def test_ignores_unknown_severities(self):
        res = de.ScanResult(target="x", returncode=0, findings=[
            {"severity": "banana"}, {"severity": "high"}])
        self.assertEqual(worst_severity(res), "high")

    def test_the_order_is_worst_first(self):
        """If someone appends to this tuple, 'worst' silently becomes 'best'."""
        self.assertEqual(SEVERITY_ORDER[0], "critical")
        self.assertLess(SEVERITY_ORDER.index("critical"), SEVERITY_ORDER.index("low"))


def _session():
    """A JarvisLive-shaped stub with a recording UI.

    The UI records calls *and* accepts attribute assignment: bind_deep_eye
    stores its two callbacks on it, and a stub that only records would fail
    with AttributeError before reaching the behaviour under test.
    """
    calls = []

    class UI:
        def __setattr__(self, name, value):
            object.__setattr__(self, name, value)

        def __getattr__(self, name):
            def rec(*a, **k):
                calls.append((name, a, k))
            return rec

    s = types.SimpleNamespace(ui=UI(), _deep_eye_scan=None)
    for m in MIXIN_METHODS:
        setattr(s, m, types.MethodType(getattr(DeepEyeSessionMixin, m), s))
    return s, calls, UI


class TestSessionBehaviour(unittest.TestCase):
    def test_stop_kills_the_child(self):
        s, calls, _ = _session()
        killed = []
        s._deep_eye_scan = types.SimpleNamespace(cancel=lambda: killed.append(1))
        s._de_cancel_scan()
        self.assertEqual(killed, [1], "STOP did not kill the scanner")

    def test_stop_with_no_scan_is_harmless(self):
        s, _, _ = _session()
        s._deep_eye_scan = None
        s._de_cancel_scan()          # must not raise

    def test_bind_initialises_the_scan_handle(self):
        """`_de_on_done` clears the handle in a `finally`, so a missing
        initialisation would only bite on the first STOP of a session — the
        one moment a stale handle from a previous run could still be killed.
        """
        s, calls, _ = _session()
        s._deep_eye_scan = object()
        s.bind_deep_eye()
        self.assertIsNone(s._deep_eye_scan,
                         "bind_deep_eye must start with no scan in flight")

    def test_bind_registers_the_stop_handler(self):
        s, _, _ = _session()
        s.bind_deep_eye()
        # A no-op UI records nothing, so check the attribute exists at all.
        self.assertTrue(hasattr(s.ui, "on_deep_eye_cancel"))

    def test_bind_registers_the_scan_request_handler(self):
        """The panel's SCAN button reaches the gate through this. Missing, the
        button would report "Not available" and the panel would be decoration."""
        s, _, _ = _session()
        s.bind_deep_eye()
        self.assertTrue(hasattr(s.ui, "on_deep_eye_scan"))

    def test_the_scan_request_goes_through_the_shared_gate(self):
        """The panel must use request_scan, the same entry point as the voice
        tool. A second route into confirm.py is a second chance to scan without
        asking, which is the one thing this feature must never do."""
        import inspect
        from core import confirm as _confirm
        src = inspect.getsource(DeepEyeSessionMixin._de_request_scan)
        self.assertIn("request_scan", src)
        self.assertIn("confirm", src)
        # ...and it must not resolve() the gate itself.
        self.assertNotIn("_confirm.resolve", src)

    def test_a_raising_cancel_does_not_break_stop(self):
        s, calls, _ = _session()

        class Boom:
            def cancel(self):
                raise RuntimeError("already gone")

        s._deep_eye_scan = Boom()
        s._de_cancel_scan()          # must not raise
        names = [c[0] for c in calls]
        self.assertIn("deep_eye_done", names,
                      "the panel was not told the scan stopped")

    def test_begin_shows_the_target_on_the_avatar(self):
        """The panel alone is not enough: a scan runs for minutes, and if the
        user never opened the panel the button would look broken — so the
        obvious response would be to press it again."""
        s, calls, _ = _session()
        s._de_on_begin("example.com")
        activity = dict((c[0], c[1]) for c in calls)["deep_eye_activity"]
        self.assertIn("example.com", str(activity[0]))
        self.assertIn("SCANNING", str(activity[0]))

    def test_done_clears_the_activity_and_reports_the_worst(self):
        s, calls, _ = _session()
        res = de.ScanResult(target="x", returncode=0,
                            findings=[{"severity": "low"}, {"severity": "high"}])
        s._de_on_done(True, res)
        acts = [c[1][0] for c in calls if c[0] == "deep_eye_activity"]
        self.assertIn("HIGH FINDING", acts)

    def test_done_with_no_findings_clears_the_avatar(self):
        s, calls, _ = _session()
        s._de_on_done(True, de.ScanResult(target="x", returncode=0, findings=[]))
        acts = [c[1][0] for c in calls if c[0] == "deep_eye_activity"]
        self.assertIn("", acts)

    def test_done_accepts_a_failure_message(self):
        s, calls, _ = _session()
        s._de_on_done(False, "Deep Eye did not finish.")
        self.assertIn("deep_eye_done", [c[0] for c in calls])

    def test_done_releases_the_scan_handle(self):
        """Leaving it set would make the next STOP target a dead process."""
        s, _, _ = _session()
        s._deep_eye_scan = types.SimpleNamespace(cancel=lambda: None)
        s._de_on_done(True, de.ScanResult(target="x", returncode=0))
        self.assertIsNone(s._deep_eye_scan)

    def test_a_line_that_cannot_be_painted_does_not_raise(self):
        s, _, _ = _session()
        s.ui = types.SimpleNamespace(
            deep_eye_line=lambda l: (_ for _ in ()).throw(RuntimeError("gone")))
        s._de_on_line("x")           # must not raise

    def test_ensure_panel_failure_does_not_stop_a_scan(self):
        s, _, _ = _session()
        s.ui = types.SimpleNamespace(_win=types.SimpleNamespace(
            _toggle_deep_eye=lambda _v: (_ for _ in ()).throw(RuntimeError("x"))))
        s._de_ensure_panel()         # must not raise


if __name__ == "__main__":
    unittest.main()
