"""The phantom wake: 'Awake — wake word' for a phrase that already ended.

Thomas' transcript, verbatim:

    SYS: Sleeping — no speech for 10 seconds. Say 'Hey Jarvis' to wake me.
    SYS: Awake — wake word.
    You: That's cool. Now you cannot hear me anymore.

He never said the wake word. The cause: while AWAKE the mic does not feed the
detector at all (that is the privacy gate), so openwakeword's mel/embedding
buffers keep the tail of his last sentence. The first chunks fed after sleeping
complete those stale features and the model reports a detection for audio that
was over before the assistant closed its eyes.

Feeding that exact phrase through the model scores 0.000 — the model is fine.
The bug is stale state, not a false positive in the classifier.

The fix is `WakeWordDetector.reset()` on sleep. These tests drive the real
detector and the real `sleep()`.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.wake_word import WakeWordDetector  # noqa: E402


class FakeModel:
    """Stands in for openwakeword's Model: counts resets, holds 'stale' state."""

    def __init__(self):
        self.resets = 0
        self.stale_audio = True

    def reset(self):
        self.resets += 1
        self.stale_audio = False

    def predict(self, frame):
        # A model with stale buffers "detects"; a reset one does not.
        return {"hey_jarvis": 0.9} if self.stale_audio else {"hey_jarvis": 0.0}


def detector_with_fake_model():
    d = WakeWordDetector(on_detect=lambda: None, logger=lambda _m: None)
    m = FakeModel()
    d._model = m
    d._running = True
    d._ready = True
    return d, m


class TestResetOnSleep(unittest.TestCase):
    def test_reset_clears_the_queue(self):
        d, _ = detector_with_fake_model()
        for _ in range(5):
            d._queue.put(b"audio")
        d.reset()
        self.assertTrue(d._queue.empty(), "audio kept feeding the model after sleeping")

    def test_reset_clears_the_model_buffers(self):
        d, m = detector_with_fake_model()
        d.reset()
        self.assertEqual(m.resets, 1)
        self.assertFalse(m.stale_audio,
                         "the model still holds the features of what the user "
                         "said while awake")

    def test_reset_survives_a_missing_model(self):
        """Sleep must never raise, even if the detector failed to load."""
        d = WakeWordDetector(on_detect=lambda: None, logger=lambda _m: None)
        d.reset()   # must not raise
        d._model = None
        d.reset()


class TestSleepResetsTheDetector(unittest.TestCase):
    """`JarvisLive.sleep()` is the only caller that matters."""

    def build(self):
        det, model = detector_with_fake_model()
        logs = []
        self.model = model
        s = types.SimpleNamespace(
            _awake=True,
            _wake_detector=det,
            set_speaking=lambda _v: None,
            ui=types.SimpleNamespace(
                set_state=lambda _s: None,
                write_log=logs.append),
        )
        self.logs = logs
        import main as M
        return s, M

    def test_sleeping_resets_the_detector(self):
        s, M = self.build()
        M.JarvisLive.sleep(s, reason="no speech for 10 seconds")
        self.assertEqual(self.model.resets, 1,
                         "sleeping must discard the detector's audio state")

    def test_sleeping_logs_the_reason(self):
        s, M = self.build()
        M.JarvisLive.sleep(s, reason="no speech for 10 seconds")
        self.assertTrue(any("Sleeping" in m for m in self.logs))

    def test_a_sleep_while_already_asleep_is_a_no_op(self):
        """No second reset, no duplicate log line: the early return must come
        before the reset, not after it."""
        s, M = self.build()
        M.JarvisLive.sleep(s)
        before = self.model.resets
        M.JarvisLive.sleep(s)
        self.assertEqual(self.model.resets, before)

    def test_sleeping_without_a_detector_does_not_raise(self):
        import main as M
        s = types.SimpleNamespace(
            _awake=True, _wake_detector=None, set_speaking=lambda _v: None,
            ui=types.SimpleNamespace(set_state=lambda _s: None,
                                     write_log=lambda _m: None))
        M.JarvisLive.sleep(s)   # must not raise

    def test_a_detector_that_raises_does_not_break_sleep(self):
        """Sleep is what makes the app quiet; it must not fail because of an
        optional feature."""
        import main as M

        class Exploding:
            def reset(self):
                raise RuntimeError("model gone")
        s = types.SimpleNamespace(
            _awake=True, _wake_detector=Exploding(), set_speaking=lambda _v: None,
            ui=types.SimpleNamespace(set_state=lambda _x: None,
                                     write_log=lambda _m: None))
        M.JarvisLive.sleep(s)   # must not raise


class TestTheRealModelIsNotFooled(unittest.TestCase):
    """Guard against 'fixing' this by raising the threshold.

    The phrase Thomas actually said scores 0.000, so a threshold change would
    be treating a symptom the model does not have. Recorded here so a future
    change that raises DEFAULT_THRESHOLD has to reckon with it.
    """

    def test_the_false_phrase_scores_zero(self):
        wav = BASE_DIR / "scratch" / "false_positive.wav"
        if not wav.exists():
            self.skipTest("audio fixture not generated on this machine")
        import numpy as np
        from openwakeword.model import Model
        with __import__("wave").open(str(wav), "rb") as w:
            samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        m = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        best = 0.0
        for i in range(0, len(samples) - 1024, 1024):
            r = m.predict(samples[i:i + 1024])
            for k, v in r.items():
                if "jarvis" in k.lower():
                    best = max(best, float(v))
        self.assertLess(best, 0.1,
                        f"'That's cool' scored {best} — if this rose, the "
                        f"problem was the classifier, not stale state")


if __name__ == "__main__":
    unittest.main()
