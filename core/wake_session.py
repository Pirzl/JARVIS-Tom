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
middle of a conversation.

The level guard on that marker is what stops a fan in the room from holding
the assistant awake forever, and the marker deliberately does not set
`_awake`: hearing a voice is not the same as being addressed, and the wake
word is what does that.
"""

from **future** import annotations

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

```
def _wake_state(self) -> dict:
    # A loaded, running detector is definitively ready; otherwise fall back
    ready = bool(self._wake_detector and self._wake_detector.ready) or wake_is_ready()

    # "ready" and "listening" are different: the model can be present on disk
    # while the detector was never started, and then the setting is on but
    # the wake word can never fire. The UI shows that difference.
    running = bool(
        self._wake_detector
        and self._wake_detector.ready
        and getattr(self._wake_detector, "_running", False)
    )

    t = float(self._wake_sleep_timeout)

    if t <= 0:
        label = "∞"
    elif t < 60:
        label = f"{t:g}s"
    elif t % 60 == 0:
        label = f"{int(t // 60)}min"
    else:
        label = f"{t:g}s"

    return {
        "enabled": self._wake_enabled,
        "awake": self._awake,
        "ready": ready,
        "detector_running": running,
        "timeout": t,
        "timeout_label": label,
    }

def _set_wake_timeout_live(self, seconds: float) -> None:
    """Apply a new listening window to the running session.

    Called from the settings row so a change takes effect at once instead
    of needing a restart. The config write is done by the caller; this only
    updates the value the sleep watcher reads.
    """
    self._wake_sleep_timeout = max(0.0, float(seconds))

    # Restart the clock when the setting changes.
    self._last_user_speech = time.monotonic()

    if self._awake:
        self.ui.write_log(
            f"SYS: Listening window set to "
            f"{'never sleep' if self._wake_sleep_timeout <= 0 else f'{self._wake_sleep_timeout:g}s'}"
            f" — sleeping {_wake_state_label(self._wake_sleep_timeout)} from now."
        )

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
# speaking rather than a quiet room.
_ACTIVITY_FLOOR = 0.012

def _mark_user_activity(self, level: float) -> None:
    """Note that the user was heard, so the silence window restarts.

    The level guard keeps this from becoming an "is the microphone open"
    detector. It deliberately does not set `_awake`; waking is the wake
    word's job.
    """
    try:
        if level < self._ACTIVITY_FLOOR:
            return

        self._last_user_speech = time.monotonic()

    except Exception:  # noqa: BLE001
        # A sleep-window convenience must never break the audio path.
        pass

def _on_wake_detected(self) -> None:
    """Called from the detector thread when 'Hey Jarvis' is heard."""
    self.wake(reason="wake word")

def wake(self, reason: str = "wake word") -> None:
    if self._awake:
        return

    self._awake = True
    self._last_user_speech = time.monotonic()

    if not self.ui.muted:
        self.ui.set_state("LISTENING")

    self.ui.write_log(f"SYS: Awake — {reason}.")

def sleep(self, reason: str = "timeout", automatic: bool = False) -> None:
    """Put JARVIS to sleep.

    `automatic=True` is used only by the silence timeout watcher.

    IMPORTANT:
    Automatic sleep is checked again here, at the final point where sleep
    actually happens. This prevents a race where the user disables wake
    mode or selects "never" between the watcher's checks and this call.
    """
    if not self._awake:
        return

    # FINAL SAFETY GATE:
    #
    # If automatic sleeping is no longer enabled, NEVER put JARVIS to
    # sleep. This protects against a race with the settings UI.
    if automatic:
        if not self._wake_enabled:
            return

        if float(self._wake_sleep_timeout) <= 0:
            return

    self._awake = False
    self.set_speaking(False)

    # Throw away the detector's audio state. While awake the mic does not
    # feed the detector, so its buffers still hold the tail of what the
    # user last said; the first chunks after sleeping would complete those
    # features and report a wake word for a phrase that is already over.
    if self._wake_detector is not None:
        try:
            self._wake_detector.reset()
        except Exception as e:
            print(f"[JARVIS] wake reset error: {e}")

    self.ui.set_state("SLEEPING")
    self.ui.write_log(
        f"SYS: Sleeping — {reason}. Say 'Hey Jarvis' to wake me."
    )

async def _run_sleep_watch(self) -> None:
    """Auto-sleep after the configured silence window (wake-word mode only).

    A window of 0 means "stay awake", so the check is skipped entirely.
    Wake-word mode being disabled also means automatic sleep is completely
    disabled.
    """
    while True:
        await asyncio.sleep(5)

        # Wake-word mode OFF = automatic sleeping OFF.
        if not self._wake_enabled:
            continue

        if not self._awake:
            continue

        # 0 means NEVER sleep.
        timeout = float(self._wake_sleep_timeout)
        if timeout <= 0:
            continue

        with self._speaking_lock:
            speaking = self._is_speaking

        if speaking:
            continue

        if (time.monotonic() - self._last_user_speech) > timeout:
            mins = round(timeout / 60.0)
            when = (
                f"{mins} minutes"
                if mins >= 1
                else f"{timeout:g} seconds"
            )

            # `automatic=True` is critical. sleep() performs the same
            # setting checks again immediately before actually sleeping.
            self.sleep(
                reason=f"no speech for {when}",
                automatic=True,
            )

# ── Wake word: UI callbacks (called from the Qt thread) ────────────────

def _ui_wake_toggle(self, enable: bool) -> str:
    """Enable/disable wake word from the settings UI.

    Returns:
        'enabled' | 'disabled' | 'need_download'
    """
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

        # Wake immediately when wake-word mode is disabled.
        # The sleep watcher will also stop automatically because
        # `_wake_enabled` is now False.
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
    return wake_install(
        logger=lambda m: print(f"[Wake] {m}"),
        notify=lambda m: self.ui.write_log(f"SYS: {m}"),
    )
```
