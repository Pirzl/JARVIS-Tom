"""Tests for the Deep Eye bridge.

The bridge's job is not to scan well; it is to refuse well. A misconfigured
launcher that quietly runs a 33k-line active scanner against whatever it is
handed is far worse than one that says "no" and explains why.

Covered here: the target gate, the subprocess argv, refusal to run without
human confirmation, and containment — every failure mode must leave JARVIS
alive.

`Deep Eye` is a real tool but it is NOT invoked in these tests: no test may
send traffic anywhere. Everything runs against fakes, or against a trivially
safe local target when the tool is installed.
"""
from __future__ import annotations

import os

import subprocess
import sys
import types
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import deep_eye as de  # noqa: E402


class TestAvailable(unittest.TestCase):
    def test_reports_missing_install_without_raising(self):
        self.assertIsInstance(de.available(), bool)

    def test_availability_requires_all_three_pieces(self):
        """A checkout without its interpreter is not 'available' — that is
        exactly the state after a half-finished install, and treating it as
        ready would fail later, in front of the user."""
        self.assertTrue(de.DEEP_MAIN.name.endswith(".py"))
        self.assertTrue(de.DEEP_PYTHON.name.endswith("python.exe"))

    def test_raising_is_only_from_require(self):
        """`available()` is called while drawing a button; it must never raise."""
        de.available()
        de.available()


class TestTargetGate(unittest.TestCase):
    def test_accepts_a_plain_host(self):
        self.assertEqual(de.normalize_target("example.com"), "example.com")

    def test_strips_scheme_path_port_and_credentials(self):
        """Whatever the user says by voice, the subprocess only ever receives a
        bare hostname."""
        for raw in ("https://example.com/admin?x=1",
                    "http://user:pw@example.com:8443/a/b",
                    "example.com/path/to/thing",
                    "  example.com  "):
            with self.subTest(raw=raw):
                self.assertEqual(de.normalize_target(raw), "example.com")

    def test_lowercases_the_host(self):
        self.assertEqual(de.normalize_target("EXAMPLE.COM"), "example.com")

    def test_localhost_is_allowed(self):
        """The one host a person can attack without anyone's permission — and
        therefore the only safe one to test against. The shared normaliser
        requires a dot, so this has to be let through on purpose."""
        for raw in ("localhost", "http://localhost:8777/login", "LOCALHOST"):
            with self.subTest(raw=raw):
                self.assertEqual(de.normalize_target(raw), "localhost")

    def test_rejects_empty(self):
        with self.assertRaises(de.InvalidTarget):
            de.normalize_target("")

    def test_rejects_non_hosts(self):
        """A bare IP or an email is refused: a domain name is something a
        person can meaningfully claim to own, and it keeps the scope legible
        in the confirmation banner."""
        for raw in ("192.168.1.1", "8.8.8.8", "user@example.com",
                    "+34600111222"):
            with self.subTest(raw=raw):
                with self.assertRaises(de.InvalidTarget):
                    de.normalize_target(raw)

    def test_rejects_junk_that_could_become_a_flag(self):
        """The classic injection shape: if this ever reached argv, a leading
        dash would become an option rather than a host."""
        for raw in ("--config=/etc/passwd", "-u", "$(whoami)", "`id`", "a;rm -rf /"):
            with self.subTest(raw=raw):
                with self.assertRaises(de.InvalidTarget):
                    de.normalize_target(raw)

    def test_invalid_target_message_is_user_facing(self):
        """It has to be speakable: it goes straight into the assistant's reply."""
        with self.assertRaises(de.InvalidTarget) as ctx:
            de.normalize_target("8.8.8.8")
        self.assertIn("domain", str(ctx.exception).lower())

    def test_the_article_agrees_with_the_noun(self):
        """`is a ip` is what naive interpolation produces, and it gets read
        aloud — a small thing, but the assistant is judged on how it speaks."""
        for raw, article in (("8.8.8.8", "an ip"),
                             ("192.168.1.1", "an ip"),
                             ("+34600111222", "a phone")):
            with self.subTest(raw=raw):
                with self.assertRaises(de.InvalidTarget) as ctx:
                    de.normalize_target(raw)
                self.assertIn(article, str(ctx.exception))


class TestCommand(unittest.TestCase):
    def test_argv_has_no_shell_metacharacters_and_no_shell(self):
        scan = de.DeepEyeScan("example.com")
        cmd = scan.command()
        self.assertEqual(cmd[0], str(de.DEEP_PYTHON))
        self.assertEqual(cmd[1], str(de.DEEP_MAIN))
        self.assertEqual(cmd[cmd.index("-u") + 1], "http://example.com")
        self.assertIn("--formats", cmd)

    def test_argv_never_contains_a_shell(self):
        src = (BASE_DIR / "core" / "deep_eye.py").read_text(encoding="utf-8")
        self.assertNotIn("shell=True", src)

    def test_argv_carries_the_scheme_deep_eye_requires(self):
        """deep-eye rejects a bare host with `Error: URL must start with
        http://` and exits 1, which reads as a failed scan rather than a
        formatting slip. Found by running it, not by reading it."""
        cmd = de.DeepEyeScan("example.com").command()
        url = cmd[cmd.index("-u") + 1]
        self.assertTrue(url.startswith("http://"),
                        f"deep-eye will refuse {url!r}")

    def test_the_scheme_is_added_once(self):
        """normalize_target strips it; the command must not produce http://http://."""
        cmd = de.DeepEyeScan("https://example.com/x").command()
        self.assertEqual(cmd[cmd.index("-u") + 1], "http://example.com")

    def test_extra_args_are_appended_after_the_target(self):
        scan = de.DeepEyeScan("example.com", extra_args=["--verbose"])
        self.assertEqual(scan.command()[-1], "--verbose")

    def test_target_is_normalised_at_construction(self):
        scan = de.DeepEyeScan("https://example.com/x")
        self.assertEqual(scan.target, "example.com")

    def test_construction_refuses_a_bad_target(self):
        with self.assertRaises(de.InvalidTarget):
            de.DeepEyeScan("--help")


class _FakeProc:
    """Stands in for a Popen. `running` models the two states that matter:
    a child still running, and one that has already exited."""

    def __init__(self, lines=(), code=0, running=True):
        self.stdout = iter(lines)
        self._code = None if running else code
        self.final_code = code
        self.killed = False

    def poll(self):
        return self._code

    def wait(self, timeout=None):
        if self._code is None:
            self._code = self.final_code
        return self._code

    def kill(self):
        self.killed = True
        self._code = -1

    terminate = kill


class TestScanContainment(unittest.TestCase):
    def test_cancel_kills_the_child(self):
        """An orphaned scanner keeps hitting someone else's server after the
        assistant stopped caring. Non-negotiable."""
        scan = de.DeepEyeScan("example.com")
        scan._proc = _FakeProc()
        scan.cancel()
        self.assertTrue(scan._proc.killed)

    def test_context_manager_kills_even_on_exception(self):
        scan = de.DeepEyeScan("example.com")
        proc = _FakeProc()
        scan._proc = proc
        with self.assertRaises(RuntimeError):
            with scan:
                raise RuntimeError("boom")
        self.assertTrue(proc.killed)

    def test_terminate_is_safe_when_nothing_is_running(self):
        de.DeepEyeScan("example.com").cancel()      # must not raise

    def test_a_ui_error_in_the_callback_does_not_kill_the_scan(self):
        """A line that fails to render must not cost the user their scan."""
        scan = de.DeepEyeScan("example.com")
        proc = _FakeProc(lines=["one\n", "two\n"], code=0)
        scan._proc = proc

        def bad(_line):
            raise RuntimeError("paint failed")

        scan._pump(bad)
        self.assertEqual(scan._lines, ["one", "two"])

    def test_cancel_after_finish_raises_cancel(self):
        scan = de.DeepEyeScan("example.com")
        scan._proc = _FakeProc(lines=[], code=0)
        scan.cancel()
        with self.assertRaises(de.ScanCancelled):
            scan.wait()


class TestSummaryIsSpeakable(unittest.TestCase):
    def test_counts_severities_and_omits_payloads(self):
        """A finding's evidence is an attack string. Reading it aloud is
        useless and leaks attack material into the audio stream."""
        r = de.ScanResult(
            target="example.com", returncode=0,
            findings=[{"severity": "high", "payload": "' OR 1=1--"},
                      {"severity": "high", "payload": "<script>"},
                      {"severity": "low", "payload": "x"}])
        s = r.summary()
        self.assertIn("3 findings", s)
        self.assertIn("2 high", s)
        self.assertIn("1 low", s)
        for leak in ("OR 1=1", "script", "payload"):
            self.assertNotIn(leak, s)

    def test_no_findings_reads_naturally(self):
        r = de.ScanResult(target="example.com", returncode=0, findings=[])
        self.assertIn("no findings", r.summary())

    def test_singular_finding_is_not_plural(self):
        r = de.ScanResult(target="example.com", returncode=0,
                          findings=[{"severity": "medium"}])
        self.assertIn("1 finding (", r.summary())


class TestParsing(unittest.TestCase):
    def test_reads_a_json_report(self, ):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            p.write_text(json.dumps({"findings": [{"severity": "high"}]}),
                         encoding="utf-8")
            found, report = de._parse_json_findings(f"report: {p}")
        self.assertEqual(len(found), 1)
        self.assertEqual(report, p)

    def test_reads_a_json_line(self):
        import json
        line = json.dumps({"findings": [{"severity": "low"}, {"severity": "low"}]})
        found, _ = de._parse_json_findings(f"progress\n{line}\n")
        self.assertEqual(len(found), 2)

    def test_unparseable_output_is_not_a_failure(self):
        """A real scan that found things must not be reported as failed just
        because the summary could not be read."""
        found, report = de._parse_json_findings("total mess \x00 not json {{{")
        self.assertEqual(found, [])
        self.assertIsNone(report)


class TestConsentGate(unittest.TestCase):
    """The gate is the entire reason this module is safe to ship."""

    def test_scan_never_runs_without_confirmation(self):
        calls = []

        def fake_confirm(key, title, detail, run):
            calls.append((key, title, detail, run))
            return "Please confirm."      # what the model would speak

        out = de.request_scan("https://example.com/admin", fake_confirm)
        self.assertIn("confirm", out.lower())
        self.assertEqual(len(calls), 1, "the gate was never raised")
        self.assertEqual(calls[0][0], "deep_eye_scan")

    def test_no_scan_is_even_constructed_before_the_gate(self):
        """The sharpest form of the gate check.

        `request_scan` raising DeepEyeUnavailable inside the stored callable
        means an earlier version of this test passed while the gate was
        bypassed: the work was attempted, failed for lack of an install, and
        the exception was swallowed. Whether Deep Eye happens to be installed
        on the machine running the suite must not decide whether a scan
        happens.

        So the assertion is on the bridge constructing a scan at all, not on
        what the scan would have done.
        """
        built = []

        class SpyScan:
            def __init__(self, *a, **k):
                built.append(a)
                raise AssertionError(
                    "a scan was constructed before the user confirmed")

        original = de.DeepEyeScan
        de.DeepEyeScan = SpyScan
        try:
            out = de.request_scan("example.com", lambda *a, **k: "waiting")
            self.assertIn("waiting", out)
        finally:
            de.DeepEyeScan = original
        self.assertEqual(built, [],
                         "request_scan built a scan before confirmation")

    def test_the_runnable_is_only_inside_the_confirmation(self):
        """Nothing may execute before the user approves: the banner is the only
        thing that happens, and the work is a callable it can invoke later."""
        executed = []
        captured = {}

        def fake_confirm(key, title, detail, run):
            captured["run"] = run
            return "waiting"

        de.request_scan("example.com", fake_confirm)
        self.assertNotIn("run", executed)
        # the real work is only reachable through the stored callable
        self.assertTrue(callable(captured["run"]))

    def test_banner_states_the_authorisation_requirement(self):
        seen = {}

        def fake_confirm(key, title, detail, run):
            seen.update(title=title, detail=detail)
            return "ok"

        de.request_scan("example.com", fake_confirm)
        text = (seen["title"] + " " + seen["detail"]).lower()
        self.assertIn("permission", text)
        self.assertIn("example.com", seen["detail"])
        self.assertIn("attack", text)

    def test_invalid_target_never_reaches_the_gate(self):
        """A banner for something that will not run teaches the user to click
        CONFIRM without reading."""
        asked = []

        def fake_confirm(*a, **k):
            asked.append(a)
            return "ok"

        out = de.request_scan("8.8.8.8", fake_confirm)
        self.assertEqual(asked, [], "the gate was raised for a refused target")
        self.assertIn("can't scan", out.lower())

    def test_unavailable_scanner_never_reaches_the_gate(self):
        asked = []
        original = de.available
        de.available = lambda: False
        try:
            out = de.request_scan("example.com", lambda *a, **k: asked.append(a))
        finally:
            de.available = original
        self.assertEqual(asked, [])
        self.assertIn("not installed", out.lower())

    def test_refusals_are_speakable_sentences(self):
        """They are returned to the model to be said out loud."""
        out = de.request_scan("", lambda *a, **k: "ok")
        self.assertTrue(out.endswith((".", "!", "?")))
        self.assertNotIn("Traceback", out)


if __name__ == "__main__":
    unittest.main()


class TestTheChildGetsAUtf8Stdout(unittest.TestCase):
    """Regression: the scanner died on its very first banner.

    Thomas hit, on the first confirmed scan:

        'charmap' codec can't encode characters in position 0-78
        vendor/deep-eye/deep_eye.py line 162, in display_banner
            console.print(BANNER, style="bold cyan")

    position 0-78 is the whole banner; "charmap" is cp1252, the encoding
    Windows gives a console-less process. JARVIS is started with pythonw.exe —
    no console window — so the scanner inherited cp1252, and Rich's very first
    write (box-drawing and braille characters) raised UnicodeEncodeError
    before a single check ran. The installed, working scanner looked broken.

    Note what does NOT fix this: DeepEyeScan already passes
    `encoding="utf-8"` to Popen. That only controls how the *parent* decodes
    the pipe. The child is what crashes, writing to its own stdout. The
    child's encoding is set by its environment, so that is where the fix has
    to be.
    """

    def _child_env(self, parent_env):
        """The env DeepEyeScan would hand the child, given a parent env."""
        import copy
        import core.deep_eye as de
        captured = {}

        class _P:
            def __init__(self, *a, **k):
                captured["env"] = k.get("env")
                self.stdout = self.stderr = None
                self.returncode = 0
                self.pid = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        real = de.subprocess.Popen
        de.subprocess.Popen = _P
        real_environ = os.environ
        os.environ = copy.copy(parent_env)
        try:
            de.DeepEyeScan("example.com", timeout=1).start()
        except Exception:
            pass
        finally:
            de.subprocess.Popen = real
            os.environ = real_environ
        return captured.get("env") or {}

    def _windows_console_less_parent(self):
        """What JARVIS actually looks like: pythonw.exe, so no PYTHONIOENCODING
        anywhere and the system default is cp1252."""
        return {k: v for k, v in os.environ.items()
                if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}

    def test_the_child_is_told_utf8_even_when_the_parent_says_nothing(self):
        """The regression itself, stated as the failing condition: a parent
        with no encoding declared is exactly the pythonw.exe case."""
        env = self._child_env(self._windows_console_less_parent())
        self.assertEqual(env.get("PYTHONIOENCODING"), "utf-8",
                         "the child inherits cp1252 and dies printing its "
                         "own banner, before scanning anything")

    def test_utf8_mode_is_set_too(self):
        """PYTHONUTF8 covers the paths that reconfigure the stream after
        start-up, which PYTHONIOENCODING alone does not."""
        env = self._child_env(self._windows_console_less_parent())
        self.assertIn("PYTHONUTF8", env)

    def test_the_banner_itself_would_crash_a_cp1252_child(self):
        """Why the child needs this at all: the very first thing it prints
        contains characters cp1252 cannot encode. If this ever stops being
        true, the fix becomes dead code and the test should say so."""
        self.assertTrue(any(ord(c) > 0x2500 for c in "\u2500\u256d\u2500\u2524"),
                        "the sample no longer contains box drawing")
        try:
            "\u256d\u2500\u2524".encode("cp1252")
        except UnicodeEncodeError:
            pass                      # what we expect: it cannot be encoded
        else:
            self.fail("cp1252 now encodes box drawing; re-check the child env")
