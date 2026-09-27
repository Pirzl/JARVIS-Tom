"""Tests for the extracted prompt/config builder (core/live_config.py).

Extracted from main.py.JarvisLive on 2026-09-27 with no behaviour change. These
lock in the three things that could silently break the extraction:

  1. JarvisLive still exposes _build_config / _tuning_config, and the methods
     now resolve through the mixin rather than being defined twice.
  2. The config it builds is still a valid LiveConnectConfig carrying the
     system prompt, the tool declarations, the voice and the sliding window.
  3. A missing bind fails loudly instead of producing a half-built prompt.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.live_config import LiveConfigMixin  # noqa: E402


def _make_stub(**overrides):
    """A JarvisLive-shaped object exposing only what _build_config reads."""
    import main as m

    class Stub(LiveConfigMixin):
        _asst_name = "JARVIS"
        _resume_handle = None
        _enhanced_live = False
        _tuned_live = True

    s = Stub()
    s._action_registry = m.discover_actions(
        BASE_DIR / "actions", set(), logger=lambda _: None)
    s._plugin_registry = m.discover_plugins(
        plugins_dir=BASE_DIR / "plugins",
        core_tool_names=s._action_registry.names(),
        logger=lambda *a: None, notify=lambda *a: None)
    s.bind_live_config(
        API_CONFIG_PATH=m.API_CONFIG_PATH,
        TOOL_DECLARATIONS=m.TOOL_DECLARATIONS,
        _load_system_prompt=m._load_system_prompt,
        _render_prompt=m._render_prompt,
        _describe_tools=m._describe_tools,
        _describe_limits=m._describe_limits,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class TestExtractionShape(unittest.TestCase):
    def test_jarvis_live_still_exposes_both_methods(self):
        import main as m
        self.assertTrue(issubclass(m.JarvisLive, LiveConfigMixin))
        self.assertTrue(hasattr(m.JarvisLive, "_build_config"))
        self.assertTrue(hasattr(m.JarvisLive, "_tuning_config"))

    def test_methods_are_not_defined_twice(self):
        """The bodies live in the mixin only; a copy left behind would drift."""
        import main as m
        self.assertNotIn("_build_config", vars(m.JarvisLive))
        self.assertNotIn("_tuning_config", vars(m.JarvisLive))

    def test_main_no_longer_holds_the_prompt_bodies(self):
        """The extraction must actually have removed lines from main.py."""
        src = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "JarvisLive")
        self.assertLess(cls.end_lineno, 2400)


class TestBuiltConfig(unittest.TestCase):
    """setUpClass builds one config and reuses it, so a broken binding would
    surface as a setUpClass ERROR and skip every assertion here. Each test that
    checks a field therefore also asserts the build actually succeeded, so a
    future regression is reported as a failure rather than passing silently.
    """

    @classmethod
    def setUpClass(cls):
        cls.stub = _make_stub()
        cls.cfg = cls.stub._build_config()
        cls.dumped = cls.cfg.model_dump(exclude_none=False)

    def _assert_built(self):
        """Guard: prove the shared config was really produced."""
        self.assertIsNotNone(self.dumped, "config was never built")
        self.assertIn("system_instruction", self.dumped)
        self.assertIn("tools", self.dumped)

    def test_it_is_a_live_connect_config(self):
        from google.genai import types
        self._assert_built()
        self.assertIsInstance(self.cfg, types.LiveConnectConfig)

    def test_audio_out_only(self):
        self._assert_built()
        mods = self.dumped["response_modalities"]
        self.assertEqual([m.value for m in mods], ["AUDIO"])

    def test_system_prompt_keeps_every_section(self):
        self._assert_built()
        si = self.dumped["system_instruction"]
        for marker in ("[CURRENT DATE & TIME]", "[IDENTITY]"):
            self.assertIn(marker, si)
        # the memory block is only present when a memory exists, so check the
        # builder rather than the output
        from memory.memory_manager import format_memory_for_prompt, load_memory
        if load_memory():
            self.assertIn("[WHAT YOU KNOW ABOUT THIS PERSON",
                          format_memory_for_prompt(load_memory()))
        # prompt.txt's own header must survive the {token} rendering
        self.assertIn("CORE PROTOCOL", si)
        self.assertNotIn("{capabilities}", si)
        self.assertNotIn("{limits}", si)

    def test_tool_declarations_are_attached(self):
        self._assert_built()
        decls = self.dumped["tools"][0]["function_declarations"]
        self.assertGreater(len(decls), 20)
        names = {d.get("name") if isinstance(d, dict) else getattr(d, "name", "")
                 for d in decls}
        self.assertIn("run_self_audit", names)

    def test_sliding_window_compression_is_on(self):
        self._assert_built()
        # Asserted as a FACT, not by dumping the model: the SDK accepts unknown
        # keyword fields silently, so a mutation that renames the field still
        # builds fine and "not in the dump" would never fire.
        comp = self.dumped.get("context_window_compression")
        self.assertIsNotNone(comp, "sliding-window compression was dropped")
        self.assertTrue(
            getattr(comp, "sliding_window", None) is not None
            or (isinstance(comp, dict) and comp.get("sliding_window")),
            "compression is present but is no longer a sliding window",
        )

    def test_session_resumption_carries_the_handle(self):
        self._assert_built()
        res = self.dumped.get("session_resumption")
        self.assertIsNotNone(res, "session resumption was dropped")
        self.assertIn("handle", res)

    def test_voice_is_set(self):
        self._assert_built()
        voice = (self.dumped["speech_config"]["voice_config"]
                 ["prebuilt_voice_config"]["voice_name"])
        self.assertTrue(voice, "the session would open with no voice")

    def test_tuning_config_returns_a_plain_dict(self):
        tuning = self.stub._tuning_config()
        self.assertIsInstance(tuning, dict)


class TestBindingIsRequired(unittest.TestCase):
    def test_missing_binding_fails_loudly(self):
        """A half-bound session must raise, not build a broken prompt."""
        class Bare(LiveConfigMixin):
            _asst_name = "JARVIS"
            _resume_handle = None
            _enhanced_live = False
            _tuned_live = True

        with self.assertRaises(AttributeError) as ctx:
            Bare()._build_config()
        self.assertIn("never bound", str(ctx.exception))

    def test_bound_helper_returns_the_injected_object(self):
        s = _make_stub()
        self.assertIs(s._bound("TOOL_DECLARATIONS"), s.TOOL_DECLARATIONS)


if __name__ == "__main__":
    unittest.main()
