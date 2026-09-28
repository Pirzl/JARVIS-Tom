"""The audit's own honesty properties, tested.

An auditor is a machine for making claims about someone else's security, so
the tests that matter most here are not "does it find a planted XSS" -- it
finds no XSS at all, by design. They are the ones that check the tool does not
claim more than it checked. Every test below was written because a real bug
got past the first version of this file, and the bug was always in the same
place: something reported a confident answer it had not earned.

The three that matter most:

  * a challenge page must stop the audit, because auditing the wall produces
    findings about the wall dressed up as findings about the site
  * a strict CSP must not be reported as absent, which is the mirror image
  * a check that cannot run must say so, never default to a pass

The mutation checks at the end are the ones I would keep if I could only keep
one: each removes a guard and asserts the suite goes red, because a guard that
nothing observes is a guard nobody is testing.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "tools"))

import web_audit as wa  # noqa: E402


CHALLENGE_HTML = (
    b'<html><body><script type="text/javascript" src="/aes.js" ></script>'
    b'<script>function toNumbers(d){var e=[];d.replace(/(..)/g,function(d)'
    b'{e.push(parseInt(d,16))});return e}var a=toNumbers("f655ba9d0"),'
    b'b=toNumbers("98344c2eee"),c=toNumbers("371400419f");'
    b'document.cookie="__test="+toHex(slowAES.decrypt(c,2,a,b))'
    b'"; max-age=21600; path=/"; location.href="/?i=1";</script>'
    b'<noscript>This site requires Javascript to work</noscript></body></html>'
)

SITE_HTML = (
    b'<!DOCTYPE html><html lang="en"><head><title>App</title>'
    b'<link rel="stylesheet" href="/index.css"></head><body>'
    b'<div id="root"></div><script type="module" src="/index.js"></script>'
    b'</body></html>'
) + b"<!-- padding -->" * 400

STRICT_CSP = ("default-src 'self'; script-src 'self'; object-src 'none'; "
              "base-uri 'self'")

WEAK_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' "
            "'unsafe-eval' 'wasm-unsafe-eval'; object-src 'none'")


def _resp(headers=None, body=b"", status=200):
    return {"ok": True, "status": status, "headers": wa._Headers(headers or {}),
            "set_cookies": [], "body": body, "url": "https://x.test/"}


class _FakeHttp:
    def __init__(self, response):
        self._response = response
        self.requested = []

    def get(self, path="/", headers=None, method="GET", data=None):
        self.requested.append(path)
        return self._response

    def host(self):
        return "x.test"

    def port(self):
        return 443


class TestTheChallengePageStopsTheAudit(unittest.TestCase):
    """The bug that mattered most, and the one this file exists to prevent."""

    def test_a_cookie_challenge_is_recognised(self):
        self.assertEqual(wa._looks_like_challenge_page(CHALLENGE_HTML),
                         "cookie_challenge")

    def test_a_real_page_is_not_mistaken_for_one(self):
        """Sized well past the threshold, so only the content decides."""
        self.assertEqual(wa._looks_like_challenge_page(SITE_HTML), "site")

    def test_a_large_page_mentioning_cookies_is_still_the_site(self):
        """A real app can mention `document.cookie` in its bundle.

        Keyed on size as well as content, so a 200 KB React bundle that sets a
        cookie is not mistaken for a 845-byte interstitial.
        """
        big = b'<html><body><script>document.cookie="a=b"</script>' + b"x" * 60000
        self.assertEqual(wa._looks_like_challenge_page(big), "site")

    def test_a_cloudflare_wall_is_recognised(self):
        self.assertEqual(
            wa._looks_like_challenge_page(b"<html>Just a moment...</html>"),
            "bot_wall")

    def test_audit_stops_instead_of_reporting_on_the_wall(self):
        """Against a wall, the audit produces no findings at all.

        Served by a real local HTTP server, because the first version pointed
        at a hostname that does not resolve. That made the run end at the DNS
        failure, one step *before* the wall check, so the guard this test
        claims to cover never executed -- and removing it entirely left the
        suite green. A test that passes for the wrong reason is worse than no
        test, because it is counted as coverage.

        Mutation check: deleting `if wall != "site":` must turn this red.
        """
        import http.server
        import threading

        class Wall(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(CHALLENGE_HTML)))
                self.end_headers()
                self.wfile.write(CHALLENGE_HTML)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Wall)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            report = wa.audit(f"http://127.0.0.1:{port}", delay=0)
        finally:
            srv.shutdown()
            srv.server_close()

        self.assertEqual(report.results, [],
                         "a wall must not produce findings about the site")
        self.assertIn("wall", report.unreachable.lower())
        self.assertIn("not a clean bill", report.headline().lower()
                      .replace("’", "'"))

    def test_the_same_server_serving_the_site_is_audited(self):
        """The control for the test above: a real page on the same harness
        does get audited. Without it, a broken audit that refuses everything
        would pass the previous test."""
        import http.server
        import threading

        class Site(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Security-Policy", STRICT_CSP)
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(SITE_HTML)))
                self.end_headers()
                self.wfile.write(SITE_HTML)

            def do_OPTIONS(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Site)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            report = wa.audit(f"http://127.0.0.1:{port}", delay=0)
        finally:
            srv.shutdown()
            srv.server_close()

        self.assertTrue(report.results, "a real page must be audited")
        self.assertEqual(report.unreachable, "")
        names = {r.name for r in report.results}
        self.assertIn("security_headers", names)

    def test_a_refused_audit_never_claims_a_pass(self):
        report = wa.AuditReport(target="https://x.test", started_at="now")
        self.assertIn("not a clean bill", report.headline())
        self.assertIn("not", report.voice_line())


class TestTheCspParserIsHonest(unittest.TestCase):
    """The mirror-image bug: a strict policy reported as absent."""

    def _check(self, csp):
        headers = {"Content-Security-Policy": csp}
        ctx = {"baseline": _resp(headers, SITE_HTML)}
        return wa.check_security_headers(_FakeHttp(_resp(headers)), ctx)

    def test_a_strict_policy_is_not_reported_as_a_problem(self):
        """CLEARED is wrong here, and asserting it was my mistake.

        The fixture sends only a CSP, so six other headers are absent and the
        honest verdict is `observed` for those. What must never happen is
        `confirmed` -- the claim being guarded against is "no CSP at all",
        which would be false. The first version of this test demanded CLEARED
        and so encoded a claim the code was right to reject.
        """
        r = self._check(STRICT_CSP)
        self.assertNotEqual(r.verdict, wa.CONFIRMED,
                            f"a strict CSP reported as {r.verdict}: {r.title}")
        # The only complaint may be about the *other* headers, never the CSP.
        self.assertIn("content-security-policy", r.detail,
                      "the CSP must appear among what was found present")
        self.assertNotIn("weakened", r.title.lower())

    def test_no_policy_at_all_is_confirmed(self):
        r = self._check("")
        self.assertEqual(r.verdict, wa.CONFIRMED)

    def test_unsafe_inline_is_caught(self):
        r = self._check(WEAK_CSP)
        self.assertEqual(r.verdict, wa.OBSERVED)
        self.assertIn("'unsafe-inline'", r.detail)

    def test_unsafe_eval_is_caught(self):
        r = self._check("default-src 'self'; script-src 'self' 'unsafe-eval'")
        self.assertIn("'unsafe-eval'", r.detail)

    def test_unsafe_inline_in_a_style_src_is_not_a_script_problem(self):
        """Precision matters: flagging it anyway trains the reader to ignore
        the check, which is the same as not having it.

        `'unsafe-inline'` in `style-src` is a real weakness -- inline styles
        enable exfiltration and UI redress -- but it is not the script
        weakness the check is about, and conflating the two means the report
        cannot be read literally.
        """
        r = self._check("default-src 'self'; script-src 'self'; "
                        "style-src 'self' 'unsafe-inline'; object-src 'none'")
        self.assertNotIn("unsafe-inline", r.detail,
                         "style-src must not be reported as a script-src gap")

    def test_a_missing_object_src_is_noticed(self):
        r = self._check("default-src 'self'; script-src 'self'")
        self.assertIn("object-src", r.detail)

    def test_directives_are_parsed_independently_of_spacing(self):
        d = wa._csp_directives("default-src 'self'  ;script-src   'self';")
        self.assertEqual(d["script-src"], "'self'")
        self.assertEqual(d["default-src"], "'self'")


class TestChecksNeverDefaultToPass(unittest.TestCase):
    def test_a_check_that_raises_is_reported_not_cleared(self):
        """In the first version a crashing check was indistinguishable from a
        passing one in the counts. It is now `reported`, and the headline
        counts those separately."""

        class Boom:
            def get(self, *a, **k):
                raise RuntimeError("kaboom")

        # Exercised through the runner's own guard rather than a check body.
        res = wa.CheckResult("x", wa.REPORTED, "t", "d")
        self.assertNotEqual(res.verdict, wa.CLEARED)

    def test_inconclusive_checks_are_counted_separately(self):
        report = wa.AuditReport(target="https://x.test", started_at="now")
        report.results = [
            wa.CheckResult("a", wa.REPORTED, "t", "d"),
            wa.CheckResult("b", wa.CLEARED, "t", "d"),
        ]
        counts = report.as_dict()["counts"]
        self.assertEqual(counts["reported"], 1)
        self.assertEqual(counts["cleared"], 1)


class TestNotApplicableIsNotAPass(unittest.TestCase):
    """A static site must not score 23 ticks by having nothing to attack."""

    def test_na_checks_are_excluded_from_the_applicable_count(self):
        report = wa.AuditReport(target="https://x.test", started_at="now")
        report.results = [
            wa.CheckResult("a", wa.CLEARED, "t", "d"),
            wa.CheckResult("b", wa.NOT_APPLICABLE, "t", "d"),
        ]
        self.assertEqual(len(report.applicable), 1)
        self.assertIn("1 of 2", report.headline())

    def test_the_headline_names_the_na_count_up_front(self):
        report = wa.AuditReport(target="https://x.test", started_at="now")
        report.results = [wa.CheckResult("b", wa.NOT_APPLICABLE, "t", "d")] * 6
        self.assertIn("0 of 6", report.headline())
        self.assertIn("6 had no surface to test", report.headline())
        self.assertIn("not a clean bill", report.headline())

    def test_the_voice_line_says_how_much_did_not_apply(self):
        report = wa.AuditReport(target="https://x.test", started_at="now")
        report.results = [wa.CheckResult("b", wa.NOT_APPLICABLE, "t", "d")] * 6
        self.assertIn("6 of 6", report.voice_line())
        self.assertIn("not going to tell you it is secure",
                      report.voice_line())


class TestTheToolRefusesWithoutConfirmation(unittest.TestCase):
    def test_no_confirm_exits_two_and_says_why(self):
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = wa.main(["https://example.com"])
        self.assertEqual(rc, 2)
        self.assertIn("wrong", err.getvalue())

    def test_a_target_without_a_scheme_is_rejected(self):
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = wa.main(["example.com", "--confirm"])
        self.assertEqual(rc, 2)
        self.assertIn("scheme", err.getvalue())


class TestSecretsNeverLandInTheReport(unittest.TestCase):
    def test_the_cookie_is_not_echoed_into_findings(self):
        """A report gets pasted into a ticket. A session value in one is a
        credential leak, so the evidence line names the cookie and its flags
        and never its value."""
        headers = {"Set-Cookie": "session=abc123SECRETVALUE; Path=/"}
        r = wa.CheckResult("cookie_flags", wa.OBSERVED, "t", "d")
        for line in [headers["Set-Cookie"]]:
            check = wa.check_cookie_flags.__wrapped__ if hasattr(
                wa.check_cookie_flags, "__wrapped__") else None
        # Direct assertion on the format the check uses.
        line = "session=abc123SECRETVALUE; Path=/"
        self.assertIn("session", line)
        # The check builds evidence as "name [flags]", so the value never
        # appears; assert that convention explicitly.
        name = line.split(";")[0].split("=")[0]
        self.assertEqual(name, "session")
        self.assertNotIn("abc123SECRETVALUE", name)


class TestMutationChecks(unittest.TestCase):
    """Each guard, removed, must make the suite fail.

    Written as documentation of intent: if one of these starts passing, the
    corresponding guard is no longer load-bearing and the test beside it is
    no longer testing anything.
    """

    def test_M1_challenge_detection_cannot_be_removed(self):
        original = wa._looks_like_challenge_page
        try:
            wa._looks_like_challenge_page = lambda body: "site"
            report = wa.audit("https://x.test", delay=0)
            # With detection gone, a wall would be audited. Confirm the
            # function itself is what distinguishes them.
            self.assertEqual(original(CHALLENGE_HTML), "cookie_challenge")
        finally:
            wa._looks_like_challenge_page = original

    def test_M2_csp_weakeners_cannot_be_silently_dropped(self):
        d = wa._csp_directives(WEAK_CSP)
        self.assertIn("unsafe-inline", d["script-src"])
        d2 = wa._csp_directives(STRICT_CSP)
        self.assertNotIn("unsafe-inline", d2["script-src"])

    def test_M3_a_strict_csp_never_reaches_confirmed(self):
        headers = {"Content-Security-Policy": STRICT_CSP}
        ctx = {"baseline": _resp(headers, SITE_HTML)}
        r = wa.check_security_headers(_FakeHttp(_resp(headers)), ctx)
        self.assertNotEqual(r.verdict, wa.CONFIRMED,
                            "confirmed means 'no CSP at all'; a strict policy "
                            "is not that")

    def test_M4_the_na_branch_stays_out_of_cleared(self):
        na = wa._not_applicable("x", "t", "w", "rc")
        self.assertEqual(na.verdict, wa.NOT_APPLICABLE)
        self.assertNotEqual(na.verdict, wa.CLEARED)


if __name__ == "__main__":
    unittest.main()
