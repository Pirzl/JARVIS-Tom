"""Tests for the configurable wake-sleep window.

The silence window used to be a hardcoded 120.0 in main.py, which meant the
one setting that decides whether you have to walk to the PC could not be
changed from anywhere. It is now read from config, and 0 means "stay awake".

These cover the getter's edge cases and, more importantly, the loop's: a window
of 0 must never trip `elapsed > 0`, which would put the assistant to sleep on
its very first tick.
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from memory import config_manager as cm  # noqa: E402


class TestGetter(unittest.TestCase):
    def setUp(self):
        self._orig = (cm.CONFIG_FILE.read_text(encoding="utf-8")
                      if cm.CONFIG_FILE.exists() else None)

    def tearDown(self):
        if self._orig is not None:
            cm.CONFIG_FILE.write_text(self._orig, encoding="utf-8")

    def test_round_trip(self):
        for v in (30, 120, 900, 3600):
            with self.subTest(v=v):
                cm.save_wake_sleep_timeout(v)
                self.assertEqual(cm.get_wake_sleep_timeout(), float(v))

    def test_zero_is_preserved_not_treated_as_missing(self):
        """0 means 'never sleep' and must survive the round trip."""
        cm.save_wake_sleep_timeout(0)
        self.assertEqual(cm.get_wake_sleep_timeout(), 0.0)

    def test_negative_is_clamped_to_zero(self):
        cm.save_wake_sleep_timeout(-5)
        self.assertEqual(cm.get_wake_sleep_timeout(), 0.0)

    def test_garbage_value_falls_back_instead_of_raising(self):
        """A bad edit to the JSON must never stop the app from booting."""
        data = json.loads(cm.CONFIG_FILE.read_text(encoding="utf-8"))
        cm.save_wake_sleep_timeout(120)
        text = cm.CONFIG_FILE.read_text(encoding="utf-8")
        cm.CONFIG_FILE.write_text(
            text.replace('"wake_sleep_timeout": 120.0',
                         '"wake_sleep_timeout": "basura"'), encoding="utf-8")
        self.assertEqual(cm.get_wake_sleep_timeout(), cm.DEFAULT_WAKE_SLEEP_TIMEOUT)
        del data

    def test_missing_key_uses_the_default(self):
        data = json.loads(cm.CONFIG_FILE.read_text(encoding="utf-8"))
        data.pop("wake_sleep_timeout", None)
        cm.CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
        self.assertEqual(cm.get_wake_sleep_timeout(), cm.DEFAULT_WAKE_SLEEP_TIMEOUT)


class TestSleepLoop(unittest.TestCase):
    """The loop, driven with a patched asyncio.sleep so it does not really wait."""

    class _Lock:
        def __enter__(self):
            return False
        def __exit__(self, *a):
            return False

    def _drive(self, timeout, last_user_speech, ticks=50):
        import main
        import types as pytypes

        slept = []
        s = pytypes.SimpleNamespace()
        s._wake_enabled = True
        s._awake = True
        s._wake_sleep_timeout = timeout
        s._speaking_lock = self._Lock()
        s._is_speaking = False
        s._last_user_speech = last_user_speech
        s.sleep = lambda reason="": slept.append(reason)

        async def go():
            real = asyncio.sleep

            async def fake(_d, *a, **k):
                await real(0)
            asyncio.sleep = fake
            try:
                t = asyncio.create_task(main.JarvisLive._run_sleep_watch(s))
                for _ in range(ticks):
                    await real(0)
                t.cancel()
                try:
                    await t
                except BaseException:
                    pass
            finally:
                asyncio.sleep = real

        asyncio.new_event_loop().run_until_complete(go())
        return slept

    def test_zero_never_sleeps(self):
        """The regression this setting exists to prevent: 0 must not fall
        through `elapsed > 0` and sleep the assistant immediately."""
        self.assertEqual(self._drive(0.0, 0.0), [])

    def test_zero_never_sleeps_even_after_long_silence(self):
        self.assertEqual(self._drive(0.0, -10_000.0), [])

    def test_expiry_sleeps_with_an_accurate_reason(self):
        slept = self._drive(0.01, -1000.0)
        self.assertTrue(slept, "the window never expired")
        self.assertIn("no speech for", slept[0])
        self.assertNotIn("2 minutes", slept[0],
                         "the reason still hardcodes the old 2-minute value")

    def test_disabled_wake_word_never_sleeps(self):
        import types as pytypes
        slept = []
        s = pytypes.SimpleNamespace()
        s._wake_enabled = False
        s._awake = True
        s._wake_sleep_timeout = 0.01
        s._speaking_lock = self._Lock()
        s._is_speaking = False
        s._last_user_speech = -1000.0
        s.sleep = lambda reason="": slept.append(reason)

        import main

        async def go():
            real = asyncio.sleep

            async def fake(_d, *a, **k):
                await real(0)
            asyncio.sleep = fake
            try:
                t = asyncio.create_task(main.JarvisLive._run_sleep_watch(s))
                for _ in range(30):
                    await real(0)
                t.cancel()
                try:
                    await t
                except BaseException:
                    pass
            finally:
                asyncio.sleep = real

        asyncio.new_event_loop().run_until_complete(go())
        self.assertEqual(slept, [], "auto-sleep fired with wake word off")


class TestWiring(unittest.TestCase):
    def test_main_reads_the_config_value(self):
        """main.py must not fall back to the module constant."""
        import main
        import inspect
        src = inspect.getsource(main.JarvisLive.__init__)
        self.assertIn("get_wake_sleep_timeout()", src)
        self.assertNotIn("= WAKE_SLEEP_TIMEOUT", src)

    def test_the_constant_still_documents_the_default(self):
        import main
        self.assertEqual(main.WAKE_SLEEP_TIMEOUT, 120.0)


if __name__ == "__main__":
    unittest.main()
