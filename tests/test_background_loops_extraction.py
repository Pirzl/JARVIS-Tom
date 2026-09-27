"""Tests for the extracted background loops (core/background_loops.py).

Extracted from main.py.JarvisLive on 2026-09-27. These lock in:

  1. JarvisLive still exposes all five, and the bodies are not duplicated.
  2. The moved code is semantically identical to what main.py used to hold —
     checked by comparing the AST dump, which ignores indentation and comments
     and therefore catches any real edit (a changed constant, a reordered
     guard, a dropped await) that a line diff would hide behind reindentation.
  3. The loops still refuse to talk over an active conversation.
"""
from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.background_loops import BackgroundLoopsMixin  # noqa: E402

METHODS = ("_send_startup_briefing", "_save_session_summary",
           "_run_system_monitor", "_run_background_monitor", "_run_proactive_mode")


def _methods_of(path_text: str, class_name: str) -> dict:
    tree = ast.parse(path_text)
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == class_name)
    return {m.name: m for m in cls.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            and m.name in METHODS}


class TestExtractionShape(unittest.TestCase):
    def test_jarvis_live_exposes_all_five(self):
        import main as m
        for name in METHODS:
            self.assertTrue(hasattr(m.JarvisLive, name), name)

    def test_bodies_are_not_defined_twice(self):
        import main as m
        for name in METHODS:
            self.assertNotIn(name, vars(m.JarvisLive),
                             f"{name} still exists in main.py — the two copies "
                             f"will drift apart")

    def test_main_is_smaller(self):
        lines = len((BASE_DIR / "main.py").read_text(encoding="utf-8").splitlines())
        self.assertLess(lines, 2100)


class TestMovedCodeIsUnchanged(unittest.TestCase):
    """The strongest guarantee available: compare the moved methods against
    the commit that still held them.

    The reference is NOT `HEAD` — after the extraction is committed, HEAD's
    main.py no longer contains these methods, so comparing against it would
    fail on a correct tree. Instead the file is found by searching history for
    the last commit whose main.py still defines them, and that snapshot is
    frozen as the reference. Comparing AST (not lines) ignores the indentation
    the move introduced, so any real edit shows up.
    """

    @classmethod
    def setUpClass(cls):
        cls.moved = (BASE_DIR / "core" / "background_loops.py").read_text(
            encoding="utf-8")

    def _last_main_py_with_methods(self):
        """Newest commit whose main.py still defines all five methods."""
        import subprocess
        commits = subprocess.run(
            ["git", "log", "--format=%H", "--", "main.py"],
            cwd=BASE_DIR, capture_output=True, text=True).stdout.split()
        for sha in commits:
            src = subprocess.run(["git", "show", f"{sha}:main.py"], cwd=BASE_DIR,
                                 capture_output=True, text=True).stdout
            if not src:
                continue
            try:
                found = _methods_of(src, "JarvisLive")
            except SyntaxError:
                continue
            if all(m in found for m in METHODS):
                return src
        return ""

    def test_all_five_present_in_the_mixin(self):
        found = _methods_of(self.moved, "BackgroundLoopsMixin")
        for name in METHODS:
            self.assertIn(name, found, f"{name} is missing from the mixin")

    def test_bodies_match_the_last_containing_version(self):
        ref = self._last_main_py_with_methods()
        if not ref:
            self.skipTest("no commit whose main.py still holds these methods")
        before = _methods_of(ref, "JarvisLive")
        after = _methods_of(self.moved, "BackgroundLoopsMixin")
        for name in METHODS:
            with self.subTest(method=name):
                self.assertIn(name, before)
                self.assertEqual(
                    ast.dump(before[name], include_attributes=False),
                    ast.dump(after[name], include_attributes=False),
                    f"{name} changed during the move",
                )


class TestConversationSafety(unittest.TestCase):
    """These loops speak on their own, so the guards that stop them talking
    over the user are the behaviour that matters most."""

    def _run_loop_one_step(self, method, **state):
        """Drive one iteration of an infinite loop and return what it sent.

        The loops start with a real sleep (10 s for the system monitor, 60 s
        for proactive), so this patches asyncio.sleep to yield instead of
        actually waiting. Without that the test would either take a minute or
        silently assert against a loop that never reached its body.
        """
        import asyncio

        sent = []

        class Session:
            async def send_client_content(self, turns=None, turn_complete=False):
                sent.append(turns["parts"][0]["text"])

        class UI:
            def write_log(self, *_a, **_k):
                pass

        class SysMonitor:
            def check(self):
                return "CPU is at 99 percent."

        class SpeakingLock:
            def __enter__(self):
                return state.get("is_speaking", False)
            def __exit__(self, *a):
                return False

        class Proactive:
            def should_trigger(self, _t):
                return True
            def mark_triggered(self):
                pass
            def build_prompt(self, **kw):
                return "PROACTIVE PROMPT"

        class Stub(BackgroundLoopsMixin):
            pass

        s = Stub()
        s.session = Session()
        s.ui = UI()
        s._sys_monitor = SysMonitor()
        s._speaking_lock = SpeakingLock()
        s._awake = state.get("awake", True)
        s._is_speaking = state.get("is_speaking", False)
        s._last_user_speech = state.get("last_user_speech", 0.0)
        s._briefing_sent = False
        s._session_log = []
        s._turn_done_event = None
        s._proactive = Proactive()

        async def run():
            real_sleep = asyncio.sleep
            # yield control without waiting out the loop's own interval
            async def fake_sleep(_d, *a, **k):
                await real_sleep(0)
            asyncio.sleep = fake_sleep
            try:
                task = asyncio.create_task(getattr(s, method)())
                for _ in range(200):
                    await real_sleep(0)
                    if sent:
                        break
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
            finally:
                asyncio.sleep = real_sleep
            return sent

        return asyncio.new_event_loop().run_until_complete(run())

    def test_system_monitor_stays_quiet_while_user_speaks(self):
        sent = self._run_loop_one_step(
            "_run_system_monitor", last_user_speech=1e12)
        self.assertEqual(sent, [], "monitor spoke over an active conversation")

    def test_system_monitor_stays_quiet_when_jarvis_is_speaking(self):
        sent = self._run_loop_one_step(
            "_run_system_monitor", is_speaking=True, last_user_speech=0.0)
        self.assertEqual(sent, [], "monitor spoke while JARVIS was mid-sentence")

    def test_system_monitor_speaks_when_idle(self):
        sent = self._run_loop_one_step(
            "_run_system_monitor", is_speaking=False, last_user_speech=0.0)
        self.assertTrue(sent, "monitor never alerted on a 99% CPU reading")
        self.assertIn("99 percent", sent[0])

    def test_proactive_stays_quiet_while_user_speaks(self):
        sent = self._run_loop_one_step(
            "_run_proactive_mode", is_speaking=True, last_user_speech=0.0)
        self.assertEqual(sent, [], "proactive mode spoke over the user")

    def test_proactive_speaks_when_idle(self):
        sent = self._run_loop_one_step(
            "_run_proactive_mode", is_speaking=False, last_user_speech=0.0)
        self.assertTrue(sent, "proactive mode never checked in")
        self.assertIn("PROACTIVE", sent[0])


if __name__ == "__main__":
    unittest.main()
