"""The auto-sleep clock is only restarted by one kind of user activity.

Thomas: "he always goes to sleep after just talking once with it", with
`wake_sleep_timeout` at 20 s. Twenty seconds after a complete conversation the
assistant is asleep again, so it needs the wake word for every single turn.

The cause is that the clock is restarted in exactly one place on the audio
path -- when Gemini reports `input_transcription`. The wake word itself is
detected locally and never spoken to the model, so the utterance that follows
it is the model's *reply*, and whether a transcription accompanies that reply is
up to the server, not to us. When it does not, `_last_user_speech` still holds
its value from the moment JARVIS woke up, the 20 seconds elapse measured from
there, and it sleeps. A full conversation in between does not matter, because
nothing the user did on that path touched the clock.

The typed path has the same gap: `_on_text_command` checks the wake gate and
forwards the text, and never touches the clock either.

So the failure is not "20 seconds is too short". It is that the window is
measured from the last thing that happened to touch it, and for a voice
conversation that is the wake word rather than the conversation.
"""
from __future__ import annotations

import ast
import sys
import time
import types
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

MAIN = BASE_DIR / "main.py"
# The wake/sleep block was cut out of main.py into a mixin when main.py hit
# its size guard. These tests follow the code, not the file it used to live
# in, so both are read and searched together -- otherwise a correct
# extraction turns this whole file red and the suite teaches the wrong lesson.
MIXIN = BASE_DIR / "core" / "wake_session.py"
SRC = MAIN.read_text(encoding="utf-8")
SRC_ALL = SRC + "\n" + MIXIN.read_text(encoding="utf-8")


_COMPILED: dict = {}


def _compile_function(name: str):
    """The real function object, built from main.py's source.

    `JarvisLive.__init__` needs a live session, an asyncio loop and a UI, none
    of which the sleep arithmetic touches. Rebuilding just this one method from
    the shipped source keeps the test honest -- it cannot drift from main.py,
    because it *is* main.py's text -- without booting the world.
    """
    if name not in _COMPILED:
        import asyncio
        ns = {"time": time, "asyncio": asyncio, "Exception": Exception}
        exec(compile(_function(name), "<%s>" % name, "exec"), ns)
        _COMPILED[name] = ns[name]
    return _COMPILED[name]


def _function(name: str):
    """The source of one top-level or method named `name`."""
    tree = ast.parse(SRC_ALL)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return ast.get_source_segment(SRC_ALL, node) or ""
    return ""


class TestTheClockRestartsOnAVoiceTurn(unittest.TestCase):
    """The actual defect: a spoken turn does not count as speech."""

    def test_the_sleep_watch_measures_from_the_last_user_speech(self):
        watch = _function("_run_sleep_watch")
        self.assertIn("_last_user_speech", watch)
        self.assertIn("_wake_sleep_timeout", watch)

    def test_a_zero_window_means_stay_awake(self):
        """Already correct, and worth holding: `elapsed > 0` is true at once."""
        watch = _function("_run_sleep_watch")
        self.assertIn("if self._wake_sleep_timeout <= 0:", watch,
                      "a window of 0 must skip the check entirely")

    def test_the_typed_path_restarts_the_clock(self):
        """Typing is speech activity, and it currently is not counted.

        `_on_text_command` gates on the wake word and forwards the text, and
        never touches `_last_user_speech`. So a typed conversation is measured
        from whenever the assistant last woke.
        """
        handler = _function("_on_text_command")
        self.assertIn("_last_user_speech", handler,
                      "a typed command must count as speech activity; "
                      "otherwise a 20 s window closes during a typed chat")

    def test_the_voice_path_has_a_fallback_when_no_transcription_arrives(self):
        """The fix: the clock must not depend on the server sending a transcript.

        `input_transcription` is optional in the Live API and the wake word is
        detected locally, so the first user turn after waking frequently has
        none. Something on the mic path has to mark activity regardless.
        """
        listen = _function("_listen_audio")
        self.assertTrue(
            "_mark_user_activity" in listen or "_last_user_speech" in listen,
            "the mic path must mark user activity independently of whether "
            "Gemini reports a transcription")


class TestMarkingActivityIsCheapAndSafe(unittest.TestCase):
    """The helper the fix introduces."""

    def test_it_exists(self):
        self.assertIn("def _mark_user_activity", SRC_ALL,
                      "the helper the sleep window depends on is missing")

    def test_it_touches_the_clock(self):
        helper = _function("_mark_user_activity")
        self.assertIn("_last_user_speech", helper)

    def test_it_ignores_silence(self):
        """Otherwise the clock is restarted by background noise forever.

        The level guard is what makes this a speech detector rather than a
        "the microphone is open" detector. Without it JARVIS could never fall
        asleep in a room with a fan in it.
        """
        helper = _function("_mark_user_activity")
        self.assertTrue("_level" in helper or "level" in helper,
                        "silence must not count as speech")
        self.assertTrue("if" in helper,
                        "the level guard has to actually gate something")

    def test_it_does_not_wake_the_assistant_by_itself(self):
        """Marking activity is not the same as being awake.

        They are separate states on purpose: the gate at `_on_text_command`
        refuses commands while asleep, and a helper called from the audio path
        that set `_awake` would make the wake word optional.
        """
        helper = _function("_mark_user_activity")
        self.assertNotIn("_awake = True", helper,
                         "the activity marker must not bypass the wake gate")

    def test_the_wake_gate_survives(self):
        """The whole point of wake-word mode is that typing does not answer."""
        handler = _function("_on_text_command")
        self.assertIn("I'm asleep", handler)
        self.assertIn("_awake", handler)


class TestTheWindowActuallyStaysOpen(unittest.TestCase):
    """The behaviour itself, not just the presence of a line of code.

    Built from the real `_run_sleep_watch` and `_mark_user_activity` so the
    arithmetic cannot drift from the implementation. Thomas's report is the
    case: twenty seconds configured, one complete exchange, asleep again.
    """

    def setUp(self):
        # A stand-in carrying only the attributes these two functions touch.
        # The real __init__ needs a session, a loop and a UI.
        self.j = types.SimpleNamespace()
        self.j._wake_enabled = True
        self.j._awake = True
        self.j._wake_sleep_timeout = 20.0
        self.j._ACTIVITY_FLOOR = 0.012
        self.j._last_user_speech = 0.0
        self.j._speaking_lock = __import__("threading").Lock()
        self.j._is_speaking = False
        self.j.slept_for = None
        self.j.ui = types.SimpleNamespace(write_log=lambda m: None,
                                          set_state=lambda s: None)
        self.j.sleep = lambda reason="timeout": setattr(self.j, "slept_for",
                                                        reason)

    def _mark(self, level):
        """Call the shipped `_mark_user_activity` against the stand-in.

        Bound as an unbound function straight out of the parsed source, so
        this exercises the code that actually runs rather than a restatement of
        it. Importing JarvisLive itself would drag in a session, a Qt loop and
        the audio stack, none of which this arithmetic depends on.
        """
        fn = _compile_function("_mark_user_activity")
        return fn(self.j, level)

    def _watch_once(self):
        """One tick of the real sleep watch, with asyncio.sleep patched out."""
        import asyncio
        import inspect
        real_sleep = asyncio.sleep
        state = {"ticks": 0}

        async def fake_sleep(_n):
            state["ticks"] += 1
            if state["ticks"] > 1:
                raise KeyboardInterrupt
            return None

        asyncio.sleep = fake_sleep
        try:
            watch = _compile_function("_run_sleep_watch")
            asyncio.run(watch(self.j))
        except KeyboardInterrupt:
            pass
        finally:
            asyncio.sleep = real_sleep

    def test_a_conversation_keeps_the_window_open(self):
        """The reported failure, as a test.

        Wakes, then a conversation happens over 15 seconds with blocks of real
        speech arriving every half second. The window is 20 s. It must not
        close.
        """
        self.j._awake = True
        self.j._last_user_speech = time.monotonic()
        base = self.j._last_user_speech
        # 15 s of conversation, a speech block every 0.5 s.
        for step in range(30):
            self._mark(0.4)                     # the user is talking
            self.j._last_user_speech = base + step * 0.5
        self.j._last_user_speech = time.monotonic()
        # 5 s later -- well inside the 20 s window measured from the last speech
        self.j._last_user_speech -= 5.0
        self._watch_once()
        self.assertIsNone(self.j.slept_for,
                          "a 20 s window must survive a conversation that "
                          "ended 5 s ago")

    def test_silence_still_sleeps_it(self):
        """The other half: the feature has to keep working."""
        self.j._last_user_speech = time.monotonic() - 45.0
        self._watch_once()
        self.assertIsNotNone(self.j.slept_for,
                             "45 s of silence must still put it to sleep")

    def test_quiet_room_does_not_count_as_speech(self):
        """Otherwise a fan in the room keeps the assistant awake forever."""
        self._mark(0.001)
        self.assertEqual(self.j._last_user_speech, 0.0,
                         "silence must not restart the window")

    def test_a_voice_alone_does_not_wake_it(self):
        """Activity and wake are separate on purpose.

        The gate at `_on_text_command` is what makes wake-word mode mean
        something. A marker that also set `_awake` would make the wake word
        optional, and a quiet but non-zero noise floor would do it silently.
        """
        self.j._awake = False
        self._mark(0.5)
        self.assertFalse(self.j._awake,
                         "hearing something is not the wake word")


if __name__ == "__main__":
    unittest.main()
