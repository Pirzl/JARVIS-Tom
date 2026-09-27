"""Regression tests for the 2026-09-27 security pass.

These lock in the three properties that were fixed, so a later refactor cannot
quietly reopen them:

  1. self_improvement must not auto-repair code without the user confirming
     on the HUD, and never by default.
  2. The action_name that drives a repair comes from telemetry, so it must be
     sanitised before it is ever used to build a path.
  3. No user-influenced value may reach a shell.

Run: python -m pytest tests/ -q
"""
from __future__ import annotations

import ast
import inspect
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from actions import self_improvement as si
from core import confirm


# ── 1. auto-repair is opt-in and gated ────────────────────────────────────────

class TestAutoRepairIsGated(unittest.TestCase):
    def test_auto_fix_defaults_to_false(self):
        """The model must not be able to trigger a self-repair by omission."""
        src = inspect.getsource(si.run_self_audit)
        self.assertIn('get("auto_fix", False)', src)
        self.assertNotIn('get("auto_fix", True)', src)

    def test_tool_schema_declares_false_default(self):
        prop = si.TOOL["parameters"]["properties"]["auto_fix"]
        self.assertIs(prop["default"], False)

    def test_repair_goes_through_confirm_gate(self):
        """Any path that can modify source must pass through core.confirm."""
        src = inspect.getsource(si.run_self_audit)
        self.assertIn("confirm.request", src)

    def test_headless_refuses_instead_of_repairing(self):
        """With no HUD bound, the gate must refuse rather than proceed."""
        confirm.bind(None, None, None)
        try:
            out = confirm.request("self-repair", "Rewrite your own code?",
                                  "detail", lambda: "SHOULD NOT RUN")
            self.assertIn("cannot confirm", out)
        finally:
            confirm.bind(None, None, None)

    def test_gate_does_not_run_the_callable_until_confirmed(self):
        ran = []
        confirm.bind(lambda t, d: None, lambda: None, lambda m: None)
        try:
            confirm.request("self-repair", "Rewrite your own code?",
                            "detail", lambda: ran.append(1))
            self.assertEqual(ran, [], "callable ran before the user confirmed")
            confirm.resolve(False)          # cancel
            self.assertEqual(ran, [], "callable ran on cancel")
        finally:
            confirm.resolve(False)
            confirm.bind(None, None, None)

    def test_repair_makes_a_backup_first(self):
        """A repair must leave a restorable copy behind."""
        src = inspect.getsource(si._autofix_run)
        self.assertIn("shutil.copy2", src)
        self.assertIn("backups", src)


# ── 2. telemetry-supplied names are never trusted as paths ────────────────────

class TestAutofixTargetSanitisation(unittest.TestCase):
    """The names here must be REACHABLE targets, or is_file() masks the test.

    A traversal like '../../core/gemini' never resolves to a real file, so the
    existence check alone already rejects it and the test would pass even with
    the sanitiser deleted. '../core/gemini' is the one that bites: it resolves
    to a genuine file in core/, which is exactly the file a repair must never
    be allowed to rewrite. These cases are therefore written to fail when the
    sanitiser is removed — verified by mutation.
    """

    def test_rejects_traversal_that_resolves_to_a_real_file(self):
        for reachable in (
            "../core/gemini",     # the model-selection ladder
            "../core/prompt",     # the system prompt
            "../main",            # the session loop
            "../ui",              # the entire HUD
            "../core/execution_logger",
        ):
            with self.subTest(reachable=reachable):
                got = si._autofix_target([{"action_name": reachable,
                                           "traceback": "NameError: x"}])
                self.assertIsNone(got)

    def test_rejects_traversal_and_separators(self):
        for hostile in (
            "../../core/gemini",
            "../../../Windows/System32/drivers/etc/hosts",
            "C:/Windows/System32",
            "os.path",
            "foo/bar",
            "..",
            "",
        ):
            with self.subTest(hostile=hostile):
                got = si._autofix_target([{"action_name": hostile,
                                           "traceback": "NameError: x"}])
                self.assertIsNone(got)

    def test_rejects_unknown_module(self):
        got = si._autofix_target([{"action_name": "no_existe_xyz",
                                   "traceback": "NameError: x"}])
        self.assertIsNone(got)

    def test_rejects_unfixable_error_signatures(self):
        """Only a short allowlist of crash types earns a code model."""
        for tb in ("ValueError: nope", "RuntimeError: nope", ""):
            with self.subTest(tb=tb):
                got = si._autofix_target([{"action_name": "self_improvement",
                                           "traceback": tb}])
                self.assertIsNone(got)

    def test_accepts_a_genuine_action(self):
        got = si._autofix_target([{"action_name": "self_improvement",
                                   "traceback": "TypeError: bad"}])
        self.assertEqual(got, "self_improvement")

    def test_handles_empty_and_malformed_input(self):
        for data in ([], None, [{}], [{"action_name": None, "traceback": None}]):
            with self.subTest(data=data):
                self.assertIsNone(si._autofix_target(data))


# ── 3. no user-influenced value reaches a shell ──────────────────────────────

def _shell_true_calls(path: Path) -> list[ast.Call]:
    """Every subprocess call in `path` that sets shell=True."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in {
                "run", "Popen", "call", "check_output", "check_call"}:
            continue
        owner = func.value
        if not (isinstance(owner, ast.Name) and owner.id == "subprocess"):
            continue
        for kw in node.keywords:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant) \
                    and kw.value.value is True:
                found.append(node)
    return found


class TestNoShellInjection(unittest.TestCase):
    """These files build commands from spoken requests or user-chosen paths."""

    def test_open_app_has_no_shell_true(self):
        """open_app passes a spoken app name; it must never reach cmd.exe."""
        calls = _shell_true_calls(BASE_DIR / "actions" / "open_app.py")
        self.assertEqual(calls, [],
                         "open_app.py reintroduced shell=True; spoken input "
                         "would be interpreted by the shell")

    def test_dev_agent_has_no_shell_true(self):
        """dev_agent passes a user-chosen project dir to VS Code."""
        calls = _shell_true_calls(BASE_DIR / "actions" / "dev_agent.py")
        self.assertEqual(calls, [],
                         "dev_agent.py reintroduced shell=True with a "
                         "user-supplied path")

    def test_open_app_uses_startfile_not_shell_string(self):
        src = (BASE_DIR / "actions" / "open_app.py").read_text(encoding="utf-8")
        self.assertNotIn('f"start {app_name}"', src)
        self.assertIn("os.startfile", src)


# ── the fixes must not have broken action discovery ───────────────────────────

class TestActionLoaderStillAcceptsTheTool(unittest.TestCase):
    def test_self_audit_is_discoverable(self):
        from core.action_loader import discover_actions
        actions = discover_actions(BASE_DIR / "actions", set(), logger=lambda _: None)
        self.assertIn("run_self_audit", actions.names())

    def test_schema_uses_the_loaders_caps_contract(self):
        """action_loader requires "OBJECT"/"BOOLEAN", not JSON Schema casing."""
        self.assertEqual(si.TOOL["parameters"]["type"], "OBJECT")
        self.assertEqual(
            si.TOOL["parameters"]["properties"]["auto_fix"]["type"], "BOOLEAN")


if __name__ == "__main__":
    unittest.main()
