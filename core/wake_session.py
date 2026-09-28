"""Everything about staying awake, cut out of `main.py`.

`main.py` has a size guard, and a guard is a reason to extract rather than a
number to raise. The block moved here is contiguous and about one subject:
whether the assistant is awake, what ends that, and what the UI says about it.

It is a mixin because every method here reaches straight into the live
session -- the detector, the audio loop, the UI and the speaking flag all live
on the host. A mixin keeps `self` working, so the bodies moved verbatim: same
names, same calls, same behaviour, and no indirection added in exchange for
moving them. The only edits were the dedent and the docstring on this module.

One thing here is new, and it is the fix for JARVIS falling asleep mid
conversation. `_mark_user_activity` restarts the silence window from the level
of the microphone block being sent. It exists because the window used to be
restarted in one place only -- when Gemini reported an `input_transcription` --
which made sleeping depend on the server. The wake word is detected locally
and never sent to the model, so the utterance right after waking is the
model's reply, and it frequently arrives with no transcript at all. The window
was then measured from the moment of waking, so twenty seconds closed in the
middle of a conversation. Thomas reported exactly that: 20 s configured, and
asleep again after one exchange.

The level guard on that marker is what stops a fan in the room from holding
the assistant awake forever, and the marker deliberately does not set
`_awake`: hearing a voice is not the same as being addressed, and the wake
word is what does that.
"""
from __future__ import annotations

import asyncio
import time

from memory.config_manager import save_wake_word_enabled
from core.wake_word import (
    WakeWordDetector,
    install_and_download as wake_install,
    is_ready as wake_is_ready,
)


def _wake_state_label(seconds: float) -> str:
    """Human form of a listening window, for the log and the settings row."""
    t = float(seconds)
    if t <= 0:
        return "never"
    if t < 60:
        return f"{t:g} seconds"
    if t % 60 == 0:
        mins = int(t // 60)
        return f"{mins} minute{'s' if mins != 1 else ''}"
    return f"{t:g} seconds"

class WakeMixin:
    """Awake, asleep, and the silence window that decides between them."""

    def _wake_state(self) -> dict:
        # A loaded, running detector is definitively ready; otherwise fall back
        ready = bool(self._wake_detector and self._wake_detector.ready) or wake_is_ready()
        # "ready" and "listening" are different: the model can be present on disk
        # while the detector was never started, and then the setting is on but
        # the wake word can never fire. The UI shows that difference.
        running = bool(self._wake_detector and self._wake_detector.ready
                       and getattr(self._wake_detector, "_running", False))
        t = float(self._wake_sleep_timeout)
        if t <= 0:
            label = "∞"
        elif t < 60:
            label = f"{t:g}s"
        elif t % 60 == 0:
            label = f"{int(t // 60)}min"
        else:
            label = f"{t:g}s"
        return {"enabled": self._wake_enabled, "awake": self._awake,
                "ready": ready, "detector_running": running,
                "timeout": t, "timeout_label": label}

    def _set_wake_timeout_live(self, seconds: float) -> None:
        """Apply a new listening window to the running session.

        Called from the settings row so a change takes effect at once instead of
        needing a restart. The config write is done by the caller; this only
        updates the value the sleep watcher reads.
        """
        self._wake_sleep_timeout = max(0.0, float(seconds))
        self._last_user_speech = time.monotonic()   # restart the window now
        if self._awake:
            self.ui.write_log(
                f"SYS: Listening window set to "
                f"{'never sleep' if self._wake_sleep_timeout <= 0 else f'{self._wake_sleep_timeout:g}s'}"
                f" — sleeping {_wake_state_label(self._wake_sleep_timeout)} from now.")
        if not self._awake:
            self.ui.set_state("SLEEPING")

    def _ensure_wake_detector(self) -> bool:
        """Load the detector once (model loads on first start). Idempotent."""
        if self._wake_detector is None:
            self._wake_detector = WakeWordDetector(
                on_detect=self._on_wake_detected,
                logger=lambda m: print(f"[Wake] {m}"),
                notify=lambda m: self.ui.write_log(f"SYS: {m}"),
            )
        if not self._wake_detector.ready:
            return self._wake_detector.start()
        return True

    # PCM level, 0..1, above which a microphone block counts as the user
    # speaking rather than a quiet room. Deliberately low: this only has to
    # tell a person talking from silence, so that the auto-sleep window counts
    # a conversation instead of closing between sentences. It is not a speech
    # detector and does not try to be -- the echo guard already does the
    # harder job of telling the user from JARVIS's own voice.
    _ACTIVITY_FLOOR = 0.012

    def _mark_user_activity(self, level: float) -> None:
        """Note that the user was heard, so the silence window restarts.

        The auto-sleep clock used to be restarted in one place only: when
        Gemini reported an `input_transcription`. That made the window depend
        on the server, and it is the wrong place to depend on it. The wake word
        is detected locally and never spoken to the model, so the utterance
        that follows it is the model's *reply*; whether a transcription
        accompanies that reply is not ours to decide. When it does not, the
        clock still reads from the moment JARVIS woke up, twenty seconds
        elapse from there, and it falls asleep in the middle of a conversation
        it just had. Thomas reported exactly that: 20 s configured, and asleep
        again after one exchange.

        So the mic path marks activity itself, from the level of the block
        that is being sent to the model. Cheap: one float compare and an
        assignment, no allocation, on a path that already copies this buffer.

        The level guard is what keeps this a speech detector rather than an
        "is the microphone open" detector -- without it, a fan in the room
        would keep the assistant awake forever. The bar is low on purpose: it
        only has to tell a person talking from a quiet room, not to judge
        whether what was said was addressed to JARVIS.

        It deliberately does not set `_awake`. Waking is the wake word's job,
        and a helper that flipped that state from the audio path would make
        the gate decorative.
        """
        try:
            if level < self._ACTIVITY_FLOOR:
                return
            self._last_user_speech = time.monotonic()
        except Exception:                                 # noqa: BLE001
            # A sleep-window convenience must never break the audio path.
            pass

    def _on_wake_detected(self) -> None:
        """Called from the detector thread when 'Hey Jarvis' is heard."""
        self.wake(reason="wake word")

    def wake(self, reason: str = "wake word") -> None:
        if self._awake:
            return
        self._awake = True
        self._last_user_speech = time.monotonic()   # start the auto-sleep clock now
        if not self.ui.muted:
            self.ui.set_state("LISTENING")
        self.ui.write_log(f"SYS: Awake — {reason}.")

    def sleep(self, reason: str = "timeout") -> None:
        if not self._awake:
            return
        self._awake = False
        self.set_speaking(False)
        # Throw away the detector's audio state. While awake the mic does not
        # feed the detector, so its buffers still hold the tail of what the user
        # last said; the first chunks after sleeping would complete those
        # features and report a wake word for a phrase that is already over.
        if self._wake_detector is not None:
            try:
                self._wake_detector.reset()
            except Exception as e:
                print(f"[JARVIS] wake reset error: {e}")
        self.ui.set_state("SLEEPING")
        self.ui.write_log(f"SYS: Sleeping — {reason}. Say 'Hey Jarvis' to wake me.")

    async def _run_sleep_watch(self) -> None:
        """Auto-sleep after the configured silence window (wake-word mode only).

        A window of 0 means "stay awake", so the check is skipped entirely —
        without it, `elapsed > 0` is true on the very first tick and the
        assistant would fall asleep instantly.
        """
        while True:
            await asyncio.sleep(5)
            if not self._wake_enabled or not self._awake:
                continue
            if self._wake_sleep_timeout <= 0:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue
            if (time.monotonic() - self._last_user_speech) > self._wake_sleep_timeout:
                mins = round(self._wake_sleep_timeout / 60.0)
                when = f"{mins} minutes" if mins >= 1 else f"{self._wake_sleep_timeout:g} seconds"
                self.sleep(reason=f"no speech for {when}")

    # ── Wake word: UI callbacks (called from the Qt thread) ────────────────

    def _ui_wake_toggle(self, enable: bool) -> str:
        """Enable/disable wake word from the settings UI. Returns a status token:
        'enabled' | 'disabled' | 'need_download'."""
        if enable:
            if not wake_is_ready():
                return "need_download"
            self._wake_enabled = True
            save_wake_word_enabled(True)
            self._ensure_wake_detector()
            self.sleep(reason="wake word enabled")
            return "enabled"
        else:
            self._wake_enabled = False
            save_wake_word_enabled(False)
            self.wake(reason="wake word disabled")
            return "disabled"

    def _ui_wake_manual(self) -> None:
        """Manual sleep/wake button in the UI."""
        if not self._wake_enabled:
            return
        if self._awake:
            self.sleep(reason="you tapped sleep")
        else:
            self.wake(reason="you tapped wake")

    def _ui_wake_install(self) -> tuple[bool, str]:
        """Download openwakeword + the model (runs in a UI worker thread)."""
        # Triggered by the user pressing the button, so its progress is exactly
        # what they are waiting to see.
        return wake_install(logger=lambda m: print(f"[Wake] {m}"),
                            notify=lambda m: self.ui.write_log(f"SYS: {m}"))
