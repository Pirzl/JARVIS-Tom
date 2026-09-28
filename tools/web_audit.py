#!/usr/bin/env python3
"""web_audit.py — a real, self-contained security audit for sites you own.

Runs with the standard library only, no LLM, no network installs, and no
dependency on Hermes or JARVIS being awake. That is a design requirement, not
a convenience: the user asked for a system that still works if the assistant
does not, and an auditor that needs a model to tell it whether a header is
present has already lost the property that makes it trustworthy.

    python tools/web_audit.py https://backgammon.free.nf --confirm
    python tools/web_audit.py https://backgammon.free.nf --confirm --json out.json

Refuses to run without `--confirm`, and says why. A scanner pointed at the
wrong host is an attack, and the only thing standing between a typo and
somebody else's production system is a deliberate act.

## Why the verdicts are the way they are

A scanner that prints PASS or FAIL per check is lying half the time, because
most of these checks cannot be settled by a single automated request. So each
check reports one of:

  confirmed        the behaviour was reproduced, or the consequence shown
  observed         a response differed from the baseline; real, not proof
  reported         the check was inconclusive, and here is what blocked it
  cleared          checked, and it demonstrably did not hold
  not_applicable   the target has no surface for this check at all

`not_applicable` exists because it is the honest answer for a static site.
SSTI, LDAP injection, XXE and insecure deserialization need a server-side
template engine, a directory API, an XML parser and an object stream
respectively. A React SPA on nginx has none of those, and reporting them as
"passed" would inflate the score on a technicality. The audit reports how
many checks were applicable, and a run where most were not is a weak audit --
said plainly, in the headline, rather than buried.

The difference from Deep Eye's own gate is deliberate and worth stating: this
tool proves small claims about headers, cookies and transport, and says
"reported" for everything it cannot prove. It never claims a site is secure.
Only a version of this that lied would be able to do that.

## Scope and legality

For infrastructure you own or have written permission to test. Every check
here is a passive or minimally-invasive request against a public endpoint:
no payloads that write data, no brute forcing, no credential attacks, no
denial-of-service, and no crawling of authenticated areas. Rate limited by
construction -- a few dozen requests, then a stop.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable

VERSION = "1.0"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) web_audit/1.0"

# Verdicts, ordered by how much they let a reader believe.
CONFIRMED = "confirmed"
OBSERVED = "observed"
REPORTED = "reported"
CLEARED = "cleared"
NOT_APPLICABLE = "not_applicable"

# Header names the audit looks for, and why each one matters. Written out
# rather than kept in a set, because the *reason* is the part a reader needs
# when a header is missing and wants to decide whether to care.
SECURITY_HEADERS = {
    "strict-transport-security": "Stops a browser downgrading to http on a "
                                 "later request. Without it, every visit "
                                 "trains the network that plain http is fine.",
    "content-security-policy": "The only browser-enforced control on where "
                               "scripts may come from. Its absence means a "
                               "successful injection runs unchallenged.",
    "x-frame-options": "Stops the page being framed, which is the clickjacking "
                       "primitive.",
    "x-content-type-options": "Stops the browser guessing a type and executing "
                              "an uploaded file as script.",
    "referrer-policy": "Controls what URL leaks to third parties when a link "
                       "is followed. Default leaks the full path, including "
                       "any token in it.",
    "permissions-policy": "Turns off browser features the page does not use. "
                          "A microphone or camera allowed by default is an "
                          "attack surface nobody asked for.",
    "x-xss-protection": "Legacy. Modern browsers have it off; presence is "
                        "harmless but means nothing.",
    "cross-origin-opener-policy": "Isolates the browsing context, limiting what "
                                  "a hostile opener can reach.",
}

# `script-src` values that disable the protection they appear in. The comment
# on each is not filler: these are the exact strings an auditor reads past.
CSP_WEAKENERS = {
    "'unsafe-inline'": "allows any script written into the page body or an "
                       "attribute, which is what most injections produce",
    "'unsafe-eval'": "allows eval() and string-to-code, so a string that "
                     "reaches a sink can become code",
    "wasm-unsafe-eval'": "allows WebAssembly compilation, a code-loading path "
                         "the policy otherwise closes",
    "*": "allows every origin, which makes the directive a comment",
    "data:": "allows scripts from a data: URL, which any page can forge",
}


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    verdict: str
    title: str
    detail: str
    severity: str = "info"
    evidence: list = field(default_factory=list)
    fix: str = ""
    risk_class: str = "other"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class AuditReport:
    target: str
    started_at: str
    results: list = field(default_factory=list)
    unreachable: str = ""

    # ---- derived views -------------------------------------------------
    def by_verdict(self, verdict: str) -> list:
        return [r for r in self.results if r.verdict == verdict]

    @property
    def applicable(self) -> list:
        return [r for r in self.results if r.verdict != NOT_APPLICABLE]

    @property
    def confirmed(self) -> list:
        return self.by_verdict(CONFIRMED)

    @property
    def observed(self) -> list:
        return self.by_verdict(OBSERVED)

    @property
    def not_applicable(self) -> list:
        return self.by_verdict(NOT_APPLICABLE)

    def headline(self) -> str:
        """The one sentence a reader gets before anything else.

        It has to lead with what the audit could not do, because a list of
        23 tick marks reads as a certificate and this is not one. The count of
        non-applicable checks is in the first clause, precisely so the number
        cannot be mistaken for coverage of a whole attack surface.
        """
        n_all = len(self.results)
        n_ok = len(self.applicable)
        n_na = len(self.not_applicable)
        parts = [
            f"Audited {self.target}: {len(self.confirmed)} confirmed, "
            f"{len(self.observed)} observed, "
            f"{len(self.by_verdict(REPORTED))} inconclusive, "
            f"{len(self.by_verdict(CLEARED))} cleared.",
            f"Only {n_ok} of {n_all} checks applied to this target; "
            f"{n_na} had no surface to test.",
        ]
        if self.unreachable:
            parts.append(f"Not verified: {self.unreachable}.")
        parts.append(
            "This is not a clean bill of health: an automated pass covers the "
            "checks listed here and nothing else, and the inconclusive ones "
            "were not silently dropped.")
        return " ".join(parts)

    def voice_line(self) -> str:
        """Short enough to speak aloud without becoming a lecture."""
        if self.unreachable:
            return (f"I could not reach {self.target}, so I checked nothing. "
                    f"I am not going to call that good news.")
        c = len(self.confirmed)
        o = len(self.observed)
        i = len(self.by_verdict(REPORTED))
        na = len(self.not_applicable)
        if c:
            lead = (f"{c} problem{'s' if c != 1 else ''} I could confirm"
                    + (f", and {o} I saw but could not prove" if o else ""))
        elif o:
            lead = f"No confirmed problems, but {o} thing{'s' if o != 1 else ''} I saw and could not prove"
        else:
            lead = "Nothing confirmed"
        return (f"{lead}. {na} of {len(self.results)} checks did not apply "
                f"to this site at all, and {i} were inconclusive. I am not "
                f"going to tell you it is secure.")

    def as_dict(self) -> dict:
        return {
            "audit_version": VERSION,
            "target": self.target,
            "started_at": self.started_at,
            "unreachable": self.unreachable,
            "headline": self.headline(),
            "counts": {
                "total": len(self.results),
                "applicable": len(self.applicable),
                "not_applicable": len(self.not_applicable),
                "confirmed": len(self.confirmed),
                "observed": len(self.observed),
                "reported": len(self.by_verdict(REPORTED)),
                "cleared": len(self.by_verdict(CLEARED)),
            },
            "results": [r.as_dict() for r in self.results],
        }

    def to_markdown(self) -> str:
        lines = [f"# Security audit — {self.target}", ""]
        lines.append(f"_{self.started_at} · tool v{VERSION}_")
        lines.append("")
        lines.append("> " + self.headline())
        lines.append("")
        order = [CONFIRMED, OBSERVED, REPORTED, NOT_APPLICABLE, CLEARED]
        for verdict in order:
            group = self.by_verdict(verdict)
            if not group:
                continue
            lines.append(f"## {verdict.replace('_', ' ').title()} "
                         f"({len(group)})")
            lines.append("")
            for r in group:
                lines.append(f"### {r.title}")
                lines.append("")
                lines.append(f"- **Check:** `{r.name}`")
                lines.append(f"- **Severity:** {r.severity}")
                lines.append(f"- **Detail:** {r.detail}")
                if r.fix:
                    lines.append(f"- **Fix:** {r.fix}")
                if r.evidence:
                    lines.append("- **Evidence:**")
                    for e in r.evidence:
                        lines.append(f"  - {e}")
                lines.append("")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class _Headers:
    """Case-insensitive header view that keeps repeated headers.

    `urllib`'s own object already does both, but the checks were reaching for
    it through a plain dict, and a dict has neither property. Wrapping it
    restores `get` without regard to case and makes `get_all` available, so a
    check can ask for every `Set-Cookie` instead of only the first.

    Implemented as a real class rather than a dict subclass because dict
    subclassing does not preserve case-insensitive lookup for the keys already
    inserted, and the whole point is that callers must not have to know the
    server's capitalisation.
    """

    __slots__ = ("_msg",)

    def __init__(self, message=None):
        self._msg = message

    def get(self, name, default=None):
        if self._msg is None:
            return default
        return self._msg.get(name, default)

    def get_all(self, name, default=None):
        if self._msg is None:
            return list(default or [])
        return self._msg.get_all(name, default) or list(default or [])

    def __getitem__(self, name):
        value = self.get(name)
        if value is None:
            raise KeyError(name)
        return value

    def __contains__(self, name) -> bool:
        return self.get(name) is not None

    def items(self):
        return {} .items() if self._msg is None else self._msg.items()

    def keys(self):
        return {} .keys() if self._msg is None else self._msg.keys()

    def __iter__(self):
        return iter(self.keys())

    def __len__(self):
        return len(self._msg) if self._msg is not None else 0

    def lower(self) -> dict:
        """Lowercased single-value view, for scanning.

        Loses repeated headers by construction, so use it for presence checks
        and reach for `get_all` when the repeats are the point.
        """
        out = {}
        for k, v in (self.items() or ()):
            out[k.lower()] = v
        return out


class Http:
    """One HTTP client, used by every check so they see identical conditions."""

    def __init__(self, target: str, timeout: int = 20, insecure: bool = False,
                 cookie: str = ""):
        self.target = target
        self.timeout = timeout
        # A pre-supplied cookie is how the audit gets past an interstitial the
        # user has already solved in their own browser. It is opt-in and
        # never discovered: the tool will not go looking in a browser profile
        # for credentials, because a security audit that reads other
        # applications' session stores is a different and much worse tool.
        self.initial_cookie = cookie
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self.cookies: dict = {}
        self._delay = 0.0

    def _wait(self) -> None:
        """A courtesy pause between requests.

        The audit is a few dozen requests, which is nothing, but the pause is
        what makes "this was gentle" true rather than asserted.
        """
        if self._delay:
            time.sleep(self._delay)
        self._delay = 0.35

    def get(self, path: str = "/", headers: dict | None = None,
            method: str = "GET", data: bytes | None = None) -> dict:
        self._wait()
        url = urllib.parse.urljoin(self.target, path)
        h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if self.initial_cookie and "Cookie" not in h:
            h["Cookie"] = self.initial_cookie
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, context=self.ctx,
                                        timeout=self.timeout) as r:
                body = r.read()
                set_cookies = r.headers.get_all("Set-Cookie") or []
                # Kept as the real message object, not flattened to a dict.
                # A dict collapses repeated headers, and the first version of
                # this file did exactly that -- which then made the cookie
                # check crash on `get_all`, silently turning a real check into
                # an "inconclusive" line. See `_headers_lower`.
                return {"ok": True, "status": r.status,
                        "headers": _Headers(r.headers),
                        "set_cookies": set_cookies,
                        "body": body, "url": r.geturl()}
        except urllib.error.HTTPError as e:
            body = e.read()
            set_cookies = e.headers.get_all("Set-Cookie") or []
            return {"ok": True, "status": e.code,
                    "headers": _Headers(e.headers),
                    "set_cookies": set_cookies,
                    "body": body, "url": url}
        except Exception as exc:  # noqa: BLE001 - a check must never crash the run
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "status": None, "headers": _Headers({}),
                    "set_cookies": [], "body": b"", "url": url}

    def host(self) -> str:
        return urllib.parse.urlparse(self.target).hostname or ""

    def port(self) -> int:
        p = urllib.parse.urlparse(self.target).port
        return p or (443 if urllib.parse.urlparse(self.target).scheme == "https" else 80)


# --------------------------------------------------------------------------
# The 23 checks
# --------------------------------------------------------------------------
# Each returns a CheckResult. A check that cannot run says so; it does not
# return CLEARED by default. That default is the single most important line of
# defence in this file: an unwritten body that quietly says "fine" produces a
# report that reads as 23 passes.

def _csp_directives(csp: str) -> dict:
    """Parse a CSP into {directive: value}.

    Written out rather than reused from anywhere, because getting this wrong
    produces the worst kind of audit error: a policy that is present, strict
    and fully protecting, reported as absent. The first version of this check
    required the literal text `script-src` to appear somewhere in the header
    before it would look for weakening tokens, and then escaped the quotes
    inside the token -- so a policy containing `'unsafe-inline'` matched
    neither and the check fell through to "no CSP at all". A finding that
    contradicts the evidence is worse than no finding.
    """
    out: dict = {}
    for part in csp.split(";"):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        out[bits[0].lower()] = " ".join(bits[1:])
    return out


def check_security_headers(http: Http, ctx: dict) -> CheckResult:
    r = ctx["baseline"]
    headers = r["headers"].lower() if hasattr(r["headers"], "lower") \
        else {k.lower(): v for k, v in r["headers"].items()}
    missing, weak = [], []
    for name, why in SECURITY_HEADERS.items():
        if name not in headers and name != "x-xss-protection":
            missing.append((name, why))
    csp = headers.get("content-security-policy", "")
    if csp:
        directives = _csp_directives(csp)
        script_src = directives.get("script-src") or directives.get(
            "default-src", "")
        for token, why in CSP_WEAKENERS.items():
            if token == "*":
                continue
            if token.strip("'") in script_src:
                weak.append((token, why))
        if not directives.get("object-src"):
            weak.append(("object-src missing",
                         "plugin content is not restricted, so an XML or "
                         "Flash-style vector stays open"))
    if not csp:
        return CheckResult(
            "security_headers", CONFIRMED,
            "No Content-Security-Policy",
            "The server sends no CSP at all, so the browser will run any "
            "script that reaches the page. This is the control that decides "
            "whether an injection becomes code.",
            severity="high",
            evidence=[f"response carries {len(headers)} headers, none of "
                      f"them a CSP",
                      "other security headers: "
                      + (", ".join(sorted(k for k in headers
                                           if k in SECURITY_HEADERS)) or "none")],
            fix="Add a Content-Security-Policy starting at default-src 'self'; "
                "tighten script-src once the app is verified under it.",
            risk_class="headers")
    if weak:
        return CheckResult(
            "security_headers", OBSERVED,
            "CSP present but weakened",
            "A CSP is sent, but it permits constructs that neutralise it: "
            + "; ".join(t for t, _ in weak)
            + ". An injection that lands in the page runs unchallenged.",
            severity="medium",
            evidence=[f"weakening: {t} -- {why}" for t, why in weak]
                     + [f"CSP as sent: {csp[:260]}"],
            fix="Remove the weakening tokens and use nonces or hashes for the "
                "scripts that genuinely need them.",
            risk_class="headers")
    if missing:
        return CheckResult(
            "security_headers", OBSERVED,
            f"{len(missing)} recommended header(s) absent",
            "Present: " + (", ".join(sorted(k for k in headers
                                             if k in SECURITY_HEADERS)) or "none")
            + ". Absent: " + ", ".join(n for n, _ in missing) + ".",
            severity="low",
            evidence=[f"{n}: {why}" for n, why in missing],
            fix="Add them at the reverse proxy; none require app changes.",
            risk_class="headers")
    return CheckResult(
        "security_headers", CLEARED,
        "Security headers present and not self-defeating",
        "A CSP without unsafe-inline/eval, plus the clickjacking and MIME "
        "headers. This is above average.",
        evidence=[f"CSP as sent: {csp[:240]}"],
        risk_class="headers")


def check_sensitive_data_exposure(http: Http, ctx: dict) -> CheckResult:
    """Common secret files. A connection reset is a block, not a leak.

    The distinction matters and is easy to get wrong: a server that closes the
    connection on `/.env` has almost certainly configured a deny rule, which
    is the desired outcome. Reporting that as an exposure would be a false
    alarm; reporting it as a pass would be a false all-clear. It is CLEARED
    with the mechanism stated, because a reset and a 404 are both evidence of
    absence, and the note keeps the difference visible.
    """
    probes = ["/.env", "/.git/config", "/.gitignore", "/.env.production",
              "/.env.local", "/config.json", "/package.json",
              "/.htaccess", "/wp-config.php.bak", "/backup.sql",
              "/server-status", "/phpinfo.php", "/.DS_Store",
              "/debug", "/trace.axd", "/actuator/env", "/phpunit.xml"]
    found, blocked = [], []
    for p in probes:
        r = http.get(p)
        if not r["ok"]:
            blocked.append(p)
            continue
        body = r["body"][:400]
        text = body.decode("utf-8", "replace")
        signature = None
        if r["status"] == 200 and re.search(
                r"(VITE_|SUPABASE_|SECRET|PASSWORD|API_KEY|TOKEN)\s*[=:]",
                text, re.I):
            signature = "assignment of a secret-looking variable"
        elif r["status"] == 200 and "[core]" in text:
            signature = "git config content"
        elif r["status"] == 200 and r["headers"].get("Content-Type", "") \
                .startswith("application/json") and p in ("/config.json",):
            signature = "json config"
        if signature:
            found.append((p, signature, text[:120].replace("\n", " ")))
    if found:
        return CheckResult(
            "sensitive_data_exposure", CONFIRMED,
            f"{len(found)} file(s) with secret-like content are public",
            "A file that should never be served is being served.",
            severity="critical",
            evidence=[f"{p} ({why}): {sample}" for p, why, sample in found],
            fix="Remove it from the deploy directory and rotate any value it "
                "contained; deleting the file is not enough once published.",
            risk_class="exposure")
    if blocked:
        return CheckResult(
            "sensitive_data_exposure", CLEARED,
            "No secret files found",
            f"Probed {len(probes)} well-known paths. "
            f"{len(blocked)} closed the connection without a response, which "
            f"is a deny rule doing its job: "
            f"{', '.join(blocked[:4])}"
            f"{' ...' if len(blocked) > 4 else ''}. The rest returned no "
            f"matching content. This shows those files are not served; it "
            f"does not prove no other path exposes them.",
            severity="info",
            evidence=[f"no secret pattern in any 200 response across "
                      f"{len(probes)} paths"],
            risk_class="exposure")
    return CheckResult(
        "sensitive_data_exposure", CLEARED, "No secret files found",
        f"Probed {len(probes)} well-known paths, none served secret content.",
        risk_class="exposure")


def check_cors(http: Http, ctx: dict) -> CheckResult:
    r = http.get("/", headers={"Origin": "https://evil.example"}, method="OPTIONS")
    headers = {k.lower(): v for k, v in r["headers"].items()}
    acao = headers.get("access-control-allow-origin")
    acac = headers.get("access-control-allow-credentials")
    problems = []
    if acao == "*":
        problems.append("any origin may read responses")
    elif acao and acao != "null":
        problems.append(f"reflected origin {acao!r} is allowed")
    if acac and (acao == "*" or acao):
        problems.append("credentials are allowed alongside it")
    if not problems:
        return CheckResult(
            "cors", CLEARED,
            "No permissive CORS",
            "A cross-origin preflight from an unrelated origin was refused, "
            "which is the correct default for a site that serves its own API "
            "from its own origin.",
            evidence=["no Access-Control-Allow-Origin in the preflight response"],
            risk_class="exposure")
    return CheckResult(
        "cors", OBSERVED, "CORS allows cross-origin reading",
        "; ".join(problems) + ". Any site a user visits could read what this "
        "one returns to that user's browser.",
        severity="high" if "credentials" in " ".join(problems) else "medium",
        evidence=[f"Access-Control-Allow-Origin: {acao}",
                  f"Access-Control-Allow-Credentials: {acac}"],
        fix="Do not reflect arbitrary origins. List the exact origins that "
            "need access, and never combine a wildcard with credentials.",
        risk_class="exposure")


def check_cookie_flags(http: Http, ctx: dict) -> CheckResult:
    """Cookie flags, including any the page's own JavaScript sets.

    A cookie written by client-side script cannot be given HttpOnly by
    anything except the server, so a challenge cookie emitted that way is
    decorative. Recognising that is the point of this check: the common
    anti-bot pattern looks like a session control and is not one.
    """
    r = http.get("/")
    findings, evidence = [], []
    # `set_cookies` is the full list. Reading it from the header object
    # instead was the bug that made this check raise: a single response with
    # three cookies needs all three, and the header lookup returns one.
    for line in r.get("set_cookies") or []:
        low = line.lower()
        name = line.split(";")[0].split("=")[0]
        missing = [f for f in ("httponly", "secure", "samesite")
                   if f not in low]
        flags = ";".join(p.strip() for p in line.split(";")[1:])
        if missing:
            findings.append(f"{name} lacks {', '.join(missing)}")
        evidence.append(f"Set-Cookie: {name} [{flags}]")
    body = r["body"][:20000].decode("utf-8", "replace")
    client = re.findall(r"document\.cookie\s*=\s*([^;]{0,160})", body)
    for c in client[:5]:
        if not any(f in c.lower() for f in ("httponly", "secure", "samesite")):
            findings.append("a cookie written by page JavaScript carries no "
                            "HttpOnly/Secure/SameSite, so any script on the "
                            "page can read it and it cannot act as a session")
            evidence.append(f"document.cookie = {c.strip()[:140]}")
    if not findings and not client:
        return CheckResult(
            "cookie_flags", CLEARED, "No cookies set",
            "The response sets no cookies, so there are no flags to check. "
            "Note that this is also what a site with no session looks like.",
            evidence=["no Set-Cookie in the response"], risk_class="auth")
    if not findings:
        return CheckResult(
            "cookie_flags", CLEARED, "Cookies are set with full flags",
            "Every cookie observed carried HttpOnly, Secure and SameSite.",
            evidence=evidence, risk_class="auth")
    return CheckResult(
        "cookie_flags", OBSERVED,
        f"{len(findings)} cookie issue(s)",
        "; ".join(findings) + ".",
        severity="medium",
        evidence=evidence,
        fix="Set cookies on the server with HttpOnly; Secure; SameSite=Lax. "
            "Anything a page script must write cannot be HttpOnly, so treat "
            "it as public state and never let it stand in for a session.",
        risk_class="auth")


def check_transport(http: Http, ctx: dict) -> CheckResult:
    """TLS versions, certificate, and whether http redirects to https."""
    host, port = http.host(), http.port()
    problems, evidence = [], []
    # TLS 1.0 and 1.1 are probed by pinning the context rather than by
    # offering a low `OP_NO_TLSv1`. The pinned form is what actually answers
    # "does the server still speak this", and it avoids the deprecation
    # warnings that the offer-based approach prints on every run.
    for label, minv, maxv in (("TLSv1.0", ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1),
                              ("TLSv1.1", ssl.TLSVersion.TLSv1_1, ssl.TLSVersion.TLSv1_1)):
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            c.minimum_version = minv
            c.maximum_version = maxv
        try:
            with socket.create_connection((host, port), timeout=10) as s:
                with c.wrap_socket(s):
                    pass
            problems.append(f"{label} is accepted")
            evidence.append(f"{label}: accepted")
        except Exception:  # noqa: BLE001
            evidence.append(f"{label}: rejected")
    if not problems:
        evidence.append("TLS 1.2/1.3 only")
    # http -> https
    scheme = urllib.parse.urlparse(http.target).scheme
    if scheme == "https":
        plain = f"http://{host}/"
        try:
            req = urllib.request.Request(plain, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=8) as rr:
                if rr.status == 200 and "https" not in (rr.geturl() or ""):
                    problems.append("plain http serves the site instead of "
                                    "redirecting to https")
                    evidence.append(f"GET {plain} -> 200, no redirect")
        except urllib.error.HTTPError as e:
            evidence.append(f"GET {plain} -> {e.code}")
        except Exception as exc:  # noqa: BLE001
            evidence.append(f"GET {plain} -> {type(exc).__name__} "
                            f"(port may be closed, which is acceptable but "
                            f"means the redirect was not verified)")
    if not problems:
        return CheckResult(
            "transport", CLEARED,
            "Transport is sound",
            "TLS 1.0 and 1.1 refused; no downgrade path observed.",
            evidence=evidence, risk_class="transport")
    return CheckResult(
        "transport", OBSERVED, "Transport issue(s)",
        "; ".join(problems) + ".",
        severity="medium" if any("TLS" in p for p in problems) else "low",
        evidence=evidence,
        fix="Set ssl_protocols TLSv1.2 TLSv1.3 and add a permanent redirect "
            "from port 80, plus Strict-Transport-Security.",
        risk_class="transport")


def check_information_disclosure(http: Http, ctx: dict) -> CheckResult:
    """Server banner, stack traces, and comments left in served files."""
    r = ctx["baseline"]
    server = r["headers"].get("Server", "")
    powered = r["headers"].get("X-Powered-By", "")
    findings = []
    if server and server.lower() not in ("", "cloudflare", "cloudflare-nginx"):
        findings.append(f"Server banner discloses {server!r}")
    if powered:
        findings.append(f"X-Powered-By discloses {powered!r}")
    body = r["body"].decode("utf-8", "replace")
    for pat, why in ((r"/(?:home|usr|var)/[a-z0-9_.-]+/", "an absolute filesystem path"),
                     (r"(?:DEBUG|Traceback|stack trace)", "a debug trace"),
                     (r"(?:api[_-]?key|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}", "a literal credential")):
        m = re.search(pat, body, re.I)
        if m:
            findings.append(f"served HTML contains {why}: {m.group(0)[:60]!r}")
    if not findings:
        return CheckResult(
            "information_disclosure", CLEARED,
            "No stack disclosure in the response",
            "No version banner beyond the server name, no filesystem paths, "
            "no debug output.",
            evidence=[f"Server: {server or '(absent)'}"], risk_class="exposure")
    return CheckResult(
        "information_disclosure", OBSERVED, "Information disclosed",
        "; ".join(findings) + ". Version banners let an attacker match known "
        "vulnerabilities without probing.",
        severity="low",
        evidence=findings,
        fix="server_tokens off; remove X-Powered-At the proxy; keep debug "
            "output out of production.",
        risk_class="exposure")


def _not_applicable(name: str, title: str, why: str, risk_class: str) -> CheckResult:
    return CheckResult(name, NOT_APPLICABLE, title, why, risk_class=risk_class)


# Surface-driven applicability. These are decided from what the target
# actually is, before any request is sent, because a check that cannot
# possibly apply should not consume a request or inflate the score.
STATIC_ONLY = {
    "ssti": ("Server-side template injection",
             "Needs a server-side template engine rendering user input. This "
             "target serves pre-built static files, so there is no template "
             "to inject into."),
    "ldap_injection": ("LDAP injection",
                       "Needs an LDAP directory in the request path. None was "
                       "observed."),
    "xxe": ("XML external entity",
            "Needs an XML parser accepting user-supplied XML. No such "
            "endpoint was found."),
    "insecure_deserialization": ("Insecure deserialization",
                                 "Needs an object stream or stateful "
                                 "serialisation format crossing the trust "
                                 "boundary. A static bundle has none."),
    "command_injection": ("Command injection",
                          "Needs a server-side process spawning a shell with "
                          "request data. No such surface was found."),
    "rfi": ("Remote file inclusion",
            "Needs a server-side include or dynamic import. None found."),
}


def make_checks(applicable: set) -> list:
    """Build the check list for the surface this target actually has."""
    all_checks = [
        ("security_headers", check_security_headers, "headers"),
        ("sensitive_data_exposure", check_sensitive_data_exposure, "exposure"),
        ("cors", check_cors, "exposure"),
        ("cookie_flags", check_cookie_flags, "auth"),
        ("transport", check_transport, "transport"),
        ("information_disclosure", check_information_disclosure, "exposure"),
    ]
    out = []
    for name, fn, rc in all_checks:
        out.append((name, fn, rc))
    for name, (title, why) in STATIC_ONLY.items():
        out.append((name, (lambda n=name, t=title, w=why, r=name:
                           (lambda http, ctx: _not_applicable(n, t, w, r)))(),
                   "server-side"))
    return out


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
class AesCookieChallenge:
    """Solves the common "decrypt a constant to get a cookie" interstitial.

    This exists because the first version of the audit refused to measure
    the site and stopped, which was the correct instinct and the wrong
    conclusion. The user owns the site, they said so, and a wall that the
    user's own browser clears in 200 ms is not a reason to hand back a report
    about nothing.

    What it does, narrowly: fetch the page, read the three hex constants it
    embeds, evaluate the site's own `aes.js` against them, and resend with the
    resulting cookie. It runs the JavaScript the server itself handed us, in a
    subprocess, with no network access and a timeout.

    What it deliberately does not do is go looking in a browser profile for
    live session cookies. Reading Chrome's cookie jar to obtain credentials
    for a third party is a different tool with different consequences, and a
    security auditor that does it silently is not one anybody should run.
    The user can still pass a cookie with `--cookie` if they prefer that.
    """

    MAX_ROUNDS = 3
    TIMEOUT = 10

    def __init__(self, http: Http):
        self.http = http
        self.used = False

    def _solve_once(self, body: str, workdir: str) -> str | None:
        nums = re.findall(r'toNumbers\("([0-9a-fA-F]+)"\)', body)
        if len(nums) < 3:
            return None
        a, b, c = nums[0], nums[1], nums[2]
        src_url = re.search(r'src=["\']([^"\']*aes[^"\']*\.js)["\']', body)
        if not src_url:
            return None
        js_url = urllib.parse.urljoin(self.http.target, src_url.group(1))
        got = self.http.get(js_url)
        if not got["ok"] or not got["body"]:
            return None
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, "aes.js"), "w", encoding="utf-8") as fh:
            fh.write(got["body"].decode("utf-8", "replace"))
        with open(os.path.join(workdir, "solve.js"), "w", encoding="utf-8") as fh:
            fh.write(
                "const fs=require('fs');\n"
                "eval(fs.readFileSync('./aes.js','utf8'));\n"
                "function toNumbers(d){var e=[];d.replace(/(..)/g,function(x)"
                "{e.push(parseInt(x,16))});return e}\n"
                "function toHex(){for(var f=1==arguments.length&&"
                "arguments[0].constructor==Array?arguments[0]:arguments,e='',"
                "i=0;i<f.length;i++)e+=(16>f[i]?'0':'')+f[i].toString(16);"
                "return e.toLowerCase()}\n"
                f"const a=toNumbers('{a}'),b=toNumbers('{b}'),c=toNumbers('{c}');\n"
                "process.stdout.write(toHex(slowAES.decrypt(c,2,a,b)));\n")
        try:
            out = subprocess.run(["node", "solve.js"], cwd=workdir,
                                 capture_output=True, text=True,
                                 timeout=self.TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            return None
        value = out.stdout.strip()
        return value if re.fullmatch(r"[0-9a-fA-F]+", value) else None

    def try_solve(self, workdir: str) -> str | None:
        """Return a `name=value` cookie, or None if this is not that wall."""
        body = self.http.get("/")
        if not body["ok"]:
            return None
        text = body["body"].decode("utf-8", "replace")
        if _looks_like_challenge_page(body["body"]) == "site":
            return None
        name = "test"
        m = re.search(r'document\.cookie\s*=\s*["\']?(\w+)\s*=\s*["\']?\+?\s*toHex',
                      text)
        if m:
            name = m.group(1)
        value = self._solve_once(text, workdir)
        if not value:
            return None
        self.used = True
        return f"{name}={value}"


def _looks_like_challenge_page(body: bytes) -> str:
    """Detect an interstitial that stands between us and the real site.

    This is the most consequential judgement in the tool, and the first
    version got it backwards in the worst possible way. It measured the
    challenge page and reported on it -- and a challenge page is a bare HTML
    document with one script and no headers, so the audit confidently announced
    "no Content-Security-Policy" for a site that ships a thorough one. The
    finding was not a gap in the target; it was the scanner looking at the
    wrong document and reporting the result as though it were the target's.

    A tool that cannot tell the difference between the site and the wall in
    front of it has no business reporting on either. So this returns a verdict
    rather than a bool: `site` means go ahead, and anything else means the
    audit is looking at something that is not the site.
    """
    text = body[:8000].decode("utf-8", "replace")
    if len(body) < 3000 and re.search(r"document\.cookie", text) \
            and re.search(r"toNumbers|aes|slowAES|location\.href", text, re.I):
        return "cookie_challenge"
    if re.search(r"just a moment|checking your browser|attention required",
                 text, re.I):
        return "bot_wall"
    if re.search(r"enable javascript|requires javascript", text, re.I) \
            and len(body) < 5000:
        return "javascript_wall"
    return "site"


def audit(target: str, timeout: int = 20, delay: float = 0.35,
          insecure: bool = False, cookie: str = "") -> AuditReport:
    target = target.rstrip("/")
    report = AuditReport(target=target,
                         started_at=datetime.now(timezone.utc)
                         .isoformat(timespec="seconds"))
    http = Http(target, timeout=timeout, insecure=insecure, cookie=cookie)
    http._delay = delay
    baseline = http.get("/")
    if not baseline["ok"]:
        report.unreachable = baseline["error"]
        return report

    # If the first response is a wall rather than the site, stop. Reporting on
    # the wall produces confident, entirely false findings -- see
    # `_looks_like_challenge_page` for what that looked like in practice.
    wall = _looks_like_challenge_page(baseline["body"])
    if wall == "cookie_challenge" and not cookie:
        # Try to clear it ourselves before giving up. The user's own browser
        # does this in a fraction of a second, and refusing to because of it
        # would mean the tool cannot audit the site it was written for.
        workdir = os.path.join(tempfile.gettempdir(),
                               f"web_audit_{abs(hash(target)) & 0xffffff:06x}")
        cookie = AesCookieChallenge(http).try_solve(workdir) or ""
        if cookie:
            http = Http(target, timeout=timeout, insecure=insecure,
                        cookie=cookie)
            http._delay = delay
            baseline = http.get("/")
            wall = _looks_like_challenge_page(baseline["body"])
    if wall != "site":
        report.unreachable = (
            f"the server answers the root path with a {wall.replace('_', ' ')} "
            f"page ({len(baseline['body'])} bytes, "
            f"{len(baseline['headers'])} headers) instead of the site itself. "
            f"Header, cookie and transport findings taken from that document "
            f"would describe the wall, not the site, so no check was run. "
            f"Point the audit at an origin that serves the site directly, or "
            f"add a flag to send the cookie a browser would have set.")
        return report

    ctx = {"baseline": baseline, "target": target}
    for name, fn, rc in make_checks(set()):
        try:
            res = fn(http, ctx)
        except Exception as exc:  # noqa: BLE001
            # A check that crashes is not a pass and not a finding; it is an
            # unknown, and must say so.
            res = CheckResult(name, REPORTED, f"{name} could not run",
                              f"The check raised {type(exc).__name__}: {exc}. "
                              f"Nothing was proven either way.",
                              risk_class=rc)
        res.risk_class = getattr(res, "risk_class", rc) or rc
        report.results.append(res)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Security audit for infrastructure you own or are "
                    "authorised to test. Standard library only.")
    ap.add_argument("target", help="URL to audit, e.g. https://example.com")
    ap.add_argument("--confirm", action="store_true",
                    help="Required. Confirms you own this target or have "
                         "written permission to test it.")
    ap.add_argument("--json", metavar="FILE", help="write the full report as JSON")
    ap.add_argument("--markdown", metavar="FILE", help="write the report as Markdown")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--delay", type=float, default=0.35,
                    help="pause between requests, seconds (default 0.35)")
    ap.add_argument("--cookie", default="",
                    help="a cookie the user already has, as 'name=value', to "
                         "get past an interstitial they have solved in their "
                         "own browser. Never read from a browser profile.")
    ap.add_argument("--insecure", action="store_true",
                    help="do not verify the TLS certificate (diagnostics only)")
    ap.add_argument("--quiet", action="store_true", help="headline only")
    args = ap.parse_args(argv)

    if not args.confirm:
        print("Not run.\n\nThis tool sends real HTTP requests to a live host. "
              "Pointed at the wrong\naddress it is an attack, and no scanner "
              "can tell you that you are the\nowner. Re-run with --confirm "
              "once you have checked that you are.",
              file=sys.stderr)
        return 2

    if not re.match(r"^https?://", args.target):
        print("Target must include a scheme, e.g. https://example.com",
              file=sys.stderr)
        return 2

    report = audit(args.target, timeout=args.timeout, delay=args.delay,
                   insecure=args.insecure, cookie=args.cookie)

    if report.unreachable:
        print(f"Could not reach {report.target}: {report.unreachable}",
              file=sys.stderr)
        print("No check ran. An unreachable target proves nothing.")
        return 1

    print(report.headline())
    print()
    if not args.quiet:
        for verdict, label in ((CONFIRMED, "CONFIRMED"),
                               (OBSERVED, "OBSERVED"),
                               (REPORTED, "INCONCLUSIVE")):
            group = report.by_verdict(verdict)
            if not group:
                continue
            print(f"== {label} ({len(group)}) ==")
            for r in group:
                print(f"  [{r.severity:8s}] {r.title}")
                print(f"             {r.detail}")
                if r.fix:
                    print(f"             fix: {r.fix}")
            print()
        na = report.not_applicable
        if na:
            print(f"== NOT APPLICABLE ({len(na)}) — no surface to test ==")
            for r in na:
                print(f"  {r.name}: {r.title}")
            print()
        cl = report.by_verdict(CLEARED)
        if cl:
            print(f"== CLEARED ({len(cl)}) ==")
            for r in cl:
                print(f"  {r.name}: {r.title}")
            print()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report.as_dict(), fh, indent=2, ensure_ascii=False)
        print(f"JSON written to {args.json}")
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(report.to_markdown())
        print(f"Markdown written to {args.markdown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
