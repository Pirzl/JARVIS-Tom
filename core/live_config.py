"""Prompt & session configuration for the live session.

EXTRACTED 2026-09-27 from main.py.JarvisLive, with no behaviour change: both
methods moved verbatim. They are the cleanest cut in that class — 159 lines
that call no other method on the instance and touch only the config helpers,
so the extraction risk was low and worth taking first.

The methods build the LiveConnectConfig the session opens with: the system
prompt (identity, time, memory, abilities and limits), the voice, the turn
tuning, and the sliding-window compression that lets one conversation run for
hours. Everything about *what the model is told about itself* is derived here
from the live system rather than hardcoded, which is why it is worth keeping as
one readable unit.

Dependencies on main.py's module level are passed in by the imports below;
they are the same objects main.py already had, not copies.
"""

from __future__ import annotations

import json
import platform as _platform
from datetime import datetime

from google.genai import types

from memory.config_manager import (
    get_media_resolution,
    get_proactive_audio_enabled,
    get_thinking_enabled,
    get_turn_tuning,
    get_voice,
)
from memory.memory_manager import format_memory_for_prompt, load_memory


class LiveConfigMixin:
    """Mixin: prompt assembly and Live API tuning.

    Hosted by JarvisLive, which supplies ``_action_registry``,
    ``_plugin_registry``, ``_resume_handle``, ``_enhanced_live``,
    ``_tuned_live``, ``_asst_name`` and the ``_load_system_prompt`` /
    ``_render_prompt`` / ``_describe_tools`` / ``_describe_limits`` /
    ``API_CONFIG_PATH`` / ``TOOL_DECLARATIONS`` names the caller injects.
    They are read through ``_bound()`` rather than as module globals, because
    this module must not import main back.
    """

    def bind_live_config(self, **names) -> None:
        """Inject the main.py module-level names the prompt builder reads.

        Called once by JarvisLive.__init__. Assigning onto self (rather than
        importing from main) is what keeps this module free of a circular
        import: main imports the mixin, so the mixin cannot import main back.
        """
        for key, value in names.items():
            setattr(self, key, value)

    def _bound(self, name: str):
        """Fetch an injected name, so a missing binding fails loudly here.

        The methods below were moved verbatim and still read these as module
        globals. Resolving them through self here is the one adaptation the
        extraction needed, and doing it in one place keeps the moved bodies
        otherwise byte-identical to the originals.
        """
        try:
            return getattr(self, name)
        except AttributeError:
            raise AttributeError(
                f"{name} was never bound into the live config. "
                f"JarvisLive.__init__ must call bind_live_config({name}=...) "
                f"before the session starts."
            ) from None

    def _build_config(self) -> types.LiveConnectConfig:
        # Load customization from config
        try:
            _cfg = json.loads(open(self._bound('API_CONFIG_PATH'), encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "JARVIS").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "JARVIS"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = self._bound('_load_system_prompt')()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        # Address form is a property of the language being spoken, so it is
        # stated as a principle rather than a two-language lookup — the model
        # already knows the respectful register of whatever language it is in.
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else 'ADDRESS: Address the user with the ordinary respectful form '
                      'for a superior in the language you are currently speaking — '
                      '"sir" in English, its everyday equivalent in any other '
                      'language. Never an archaic or aristocratic form, and never '
                      'the form from a different language than the one you are '
                      'speaking in this sentence.')
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        # Everything the model is told about *itself* is derived here, not
        # written into prompt.txt: the name comes from config, the platform from
        # the host, the capability list from the registries that were just
        # discovered. Rename the assistant, add a plugin or move to another OS
        # and this follows without anyone editing a prompt.
        _all_decls = (self._bound('TOOL_DECLARATIONS')
                      + self._action_registry.get_tool_declarations()
                      + self._plugin_registry.get_tool_declarations())
        _names = {(d.get("name") if isinstance(d, dict) else getattr(d, "name", ""))
                  for d in _all_decls}
        sys_prompt = self._bound('_render_prompt')(sys_prompt, {
            "assistant_name": self._asst_name,
            "platform": f"{_platform.system()} {_platform.release()}".strip(),
            "capabilities": self._bound('_describe_tools')(_all_decls),
            "limits": self._bound('_describe_limits')(
                has_vision=("screen_process" in _names or "see_file" in _names),
                has_mic=True,
            ),
        })

        parts = [time_ctx, identity_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": _all_decls}],
            # Hand back the handle captured from the last session_resumption
            # update. `handle=None` is exactly the old behaviour (ask for
            # handles, start fresh), so the first connect of a run is unchanged.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            # Sliding-window compression: session never dies from a full context
            # window — JARVIS can stay in one conversation for hours
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )
        if self._enhanced_live:
            # Proactive audio: JARVIS stays silent when speech isn't addressed
            # to it (background chatter, talking to someone else in the room).
            # (Affective dialog was dropped: gemini-3.1-flash-live does not
            #  support it, and it never reliably detected tone in practice.
            #  To restore it on a 2.5 native-audio model, add back:
            #  cfg["enable_affective_dialog"] = True )
            if get_proactive_audio_enabled():
                cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)

        if self._tuned_live:
            cfg.update(self._tuning_config())

        return types.LiveConnectConfig(**cfg)

    def _tuning_config(self) -> dict:
        """The optional knobs, kept apart so one bad field can be dropped wholesale.

        Every one of these is a preview-API field. If a future model release
        stops accepting any of them the connection fails at setup, so the run
        loop turns `_tuned_live` off and reconnects on the plain config rather
        than leaving the user with an assistant that will not start.
        """
        out: dict = {}

        # How long the server waits through a pause before deciding your turn is
        # over. This — not the size of the prompt — is what most of the delay
        # before a reply actually is, and the default has to suit everybody, so
        # it is necessarily cautious.
        turn = get_turn_tuning()
        if turn.get("enabled", True):
            detect = types.AutomaticActivityDetection(
                silence_duration_ms=turn["silence_ms"],
                prefix_padding_ms=turn["prefix_ms"],
            )
            if turn["end_sensitivity"] == "high":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_HIGH
            elif turn["end_sensitivity"] == "low":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_LOW
            if turn["start_sensitivity"] == "high":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_HIGH
            elif turn["start_sensitivity"] == "low":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_LOW
            out["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=detect)

        # Screenshots and camera frames are tokenised at this resolution and then
        # stay in the session's context. 'medium' keeps on-screen text legible
        # for a fraction of a full-resolution frame.
        res = get_media_resolution()
        if res != "default":
            out["media_resolution"] = {
                "low":    types.MediaResolution.MEDIA_RESOLUTION_LOW,
                "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
                "high":   types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            }[res]

        # Thinking is left at the server default deliberately. Forcing the budget
        # to zero was measured on gemini-3.1-flash-live over interleaved trials
        # and did not make the first word arrive sooner — this model does not
        # appear to deliberate on the Live path, so pinning the field only adds a
        # way for a future release to behave differently. Set "thinking_enabled"
        # in config/api_keys.json to true to let it reason instead.
        if get_thinking_enabled():
            out["thinking_config"] = types.ThinkingConfig(thinking_budget=-1)

        return out
