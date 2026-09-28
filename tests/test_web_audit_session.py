"""The voice path must not be able to send anything without a yes.

A scan is real HTTP traffic to a live host. Thomas talks to a voice assistant
that mishears, so the gate is the only thing between a mistaken word and
somebody else's production system. These tests are mostly about that gate,
and about the failure modes that make a session look finished when it is not
-- which is how a confirmation prompt gets skipped.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
for p in (str(BASE_DIR), str(BASE_DIR / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.web_audit_session import WebAuditSession, _load_tool  # noqa: E402


class TestNothingIsSentWithoutConfirmation(unittest.TestCase):
    def test_scan_only_asks(self):
        s = WebAuditSession()
        answer = s.scan("example.com")
        self.assertIn("confirm", answer.lower())
        self.assertIsNotNone(s.pending)

    def test_scan_makes_no_request(self):
        """The real proof. A local server counts hits; scan() must add none."""
        import http.server
        import threading

        hits = []

        class Counting(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                body = b"<html><body>x" + b"y" * 4000 + b"</body></html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Counting)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            s = WebAuditSession()
            s.scan(f"127.0.0.1:{port}")
            self.assertEqual(hits, [], "scan() sent a request before consent")
            s.cancel()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_confirm_with_nothing_pending_sends_nothing(self):
        s = WebAuditSession()
        answer = s.confirm()
        self.assertIn("nothing is waiting", answer.lower())

    def test_cancel_clears_the_pending_target(self):
        s = WebAuditSession()
        s.scan("example.com")
        self.assertIsNotNone(s.pending)
        answer = s.cancel()
        self.assertIsNone(s.pending)
        self.assertIn("cancelled", answer.lower())

    def test_cancelling_twice_is_harmless(self):
        s = WebAuditSession()
        s.scan("example.com")
        s.cancel()
        s.cancel()
        self.assertIsNone(s.pending)


class TestTargetsAreCheckedBeforeAnythingElse(unittest.TestCase):
    """A misheard word must not become a request."""

    def test_a_bare_word_is_rejected(self):
        s = WebAuditSession()
        answer = s.scan("banana")
        self.assertIsNone(s.pending,
                          "a rejected target must never become pending")
        self.assertIn("not a web address", answer)

    def test_a_scheme_is_added_when_missing(self):
        s = WebAuditSession()
        s.scan("example.com")
        self.assertEqual(s.pending["target"], "https://example.com")

    def test_http_is_preserved_not_upgraded(self):
        s = WebAuditSession()
        s.scan("http://example.com")
        self.assertEqual(s.pending["target"], "http://example.com")

    def test_an_absurdly_long_target_is_rejected(self):
        s = WebAuditSession()
        answer = s.scan("https://" + "a" * 500 + ".com")
        self.assertIn("characters long", answer)
        self.assertIsNone(s.pending)

    def test_quotes_and_spaces_are_stripped(self):
        s = WebAuditSession()
        s.scan(' "https://example.com" ')
        self.assertEqual(s.pending["target"], "https://example.com")

    def test_localhost_is_allowed(self):
        s = WebAuditSession()
        s.scan("localhost:8080")
        self.assertEqual(s.pending["target"], "http://localhost:8080",
                         "a local dev server is plain http; upgrading it "
                         "breaks the audit")


class TestTheResultArrivesUsably(unittest.TestCase):
    """The bug this file was written after: `result` never got filled.

    The first version started the audit thread and dropped both the handle and
    the result, so a caller that waited got nothing back and could not tell a
    finished run from one still going. Everything here fails against that
    version.
    """

    def _run_against_local(self, handler_cls, **kwargs):
        import http.server
        import threading
        srv = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            s = WebAuditSession()
            s.scan(f"127.0.0.1:{port}")
            s.confirm(delay=0, **kwargs)
            s.wait(30)
            return s
        finally:
            srv.shutdown()
            srv.server_close()

    def test_a_finished_run_leaves_a_result(self):
        import http.server

        class Site(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html><body>" + b"z" * 4000 + b"</body></html>"
                self.send_response(200)
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        s = self._run_against_local(Site)
        self.assertIsNotNone(s.result, "the result was never stored")
        self.assertIn("report", s.result)
        self.assertTrue(s.result["headline"])
        self.assertFalse(s.running)

    def test_speak_last_returns_the_voice_line(self):
        s = self._run_against_local(_site_handler())
        spoken = s.speak_last()
        self.assertTrue(spoken)
        self.assertIn("not going to tell you it is secure", spoken)

    def test_a_callback_that_raises_does_not_wedge_the_session(self):
        import http.server
        srv = http.server.HTTPServer(("127.0.0.1", 0), _site_handler())
        port = srv.server_address[1]
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            s = WebAuditSession()
            s.scan(f"127.0.0.1:{port}")

            def boom(_):
                raise RuntimeError("ui exploded")

            s.confirm(delay=0, on_done=boom)
            s.wait(30)
            self.assertFalse(s.running, "a raising callback left it busy")
        finally:
            srv.shutdown()
            srv.server_close()


def _site_handler():
    import http.server

    class Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<html><body>" + b"z" * 4000 + b"</body></html>"
            self.send_response(200)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; "
                             "object-src 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    return Site


class TestTheToolLoadsWithoutHermes(unittest.TestCase):
    """The requirement the user stated: it must work if the assistant is not.

    Checked by loading the module on its own, with nothing from the assistant
    framework in scope.
    """

    def test_the_tool_imports_standalone(self):
        tool = _load_tool()
        self.assertIsNotNone(tool, "tools/web_audit.py failed to load")
        self.assertTrue(hasattr(tool, "audit"))
        self.assertTrue(hasattr(tool, "AuditReport"))

    def test_it_uses_only_the_standard_library(self):
        """No pip install, no venv, no vendor. The whole point of having a
        separate CLI is that it survives a broken environment."""
        import re
        src = (BASE_DIR / "tools" / "web_audit.py").read_text(encoding="utf-8")
        imports = re.findall(r"^\s*(?:import|from)\s+([\w.]+)", src, re.M)
        allowed = {"argparse", "json", "os", "re", "socket", "ssl", "subprocess",
                   "sys", "tempfile", "time", "urllib", "dataclasses",
                   "datetime", "typing", "warnings", "__future__"}
        third_party = {m.split(".")[0] for m in imports} - allowed
        self.assertFalse(third_party,
                         f"the standalone tool must stay dependency-free, "
                         f"found: {third_party}")


class TestMutationChecks(unittest.TestCase):
    def test_M1_confirm_is_the_only_path_that_runs(self):
        s = WebAuditSession()
        s.scan("example.com")
        s.pending = None
        answer = s.confirm()
        self.assertIn("nothing is waiting", answer.lower())

    def test_M2_the_normaliser_still_rejects_nonsense(self):
        s = WebAuditSession()
        s.scan("banana")
        self.assertIsNone(s.pending)

    def test_M3_result_is_assigned_before_running_clears(self):
        import inspect
        src = inspect.getsource(WebAuditSession.confirm)
        assign = src.find("self.result = result")
        clear = src.find("self.running = False")
        self.assertNotEqual(assign, -1)
        self.assertNotEqual(clear, -1)
        self.assertLess(assign, clear,
                        "a caller that sees running=False must find the "
                        "finished report")


if __name__ == "__main__":
    unittest.main()
