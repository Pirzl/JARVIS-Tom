"""
core/deep_eye.py — bridge to the Deep Eye security scanner.

WHAT THIS IS
    A launcher, not an integration. Deep Eye is 33,000+ lines in 236 files
    across 142 modules; none of it is copied into this project. It runs as a
    separate process with its own interpreter, and this module starts it,
    streams its output, and stops it. That is the whole job.

WHY A SEPARATE PROCESS, NOT AN IMPORT
    The two projects cannot share a virtualenv:

        deep-eye   google-genai>=0.8.0
        JARVIS     google-genai>=2.8.0,<3

    Installing deep-eye alongside Gemini Live would resolve one of those and
    break the other. Beyond that, a 33k-line synchronous scanner inside the
    process that owns the live audio stream is one unhandled exception away
    from taking the assistant down. A subprocess can be killed, and its
    failure is just a return code.

WHAT THIS MODULE WILL NOT DO
    It will not start a scan on its own authority. `scan()` refuses until a
    human has approved this specific target through core/confirm.py. The
    check lives HERE rather than in the caller, so no other code path — a
    plugin, a future tool, a voice command routed differently — can reach a
    scan without passing the gate. See `scan()` for the reasoning.

Deep Eye is MIT-licensed and requires you to have authorisation for any
target. The scanner has no consent flag of its own, which is exactly why the
gate is in this file.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import urlparse

# ── Layout ────────────────────────────────────────────────────────────────────
# vendor/deep-eye is deliberately outside the import path: nothing in JARVIS
# may `import deep_eye`, or the dependency conflict above comes back.
VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "deep-eye"
DEEP_VENV = VENDOR_DIR / ".deep-venv"
# Deep Eye's installer puts the interpreter one level up from the checkout.
DEEP_PYTHON = DEEP_VENV / "Scripts" / "python.exe"
DEEP_MAIN = VENDOR_DIR / "deep_eye.py"

# A scan of a real site takes minutes. Generous, but bounded: an assistant that
# scans forever is indistinguishable from a hung one.
DEFAULT_TIMEOUT = 900.0

# ── Target validation ─────────────────────────────────────────────────────────
# Reused from the OSINT action so a target means the same thing everywhere in
# JARVIS. It rejects anything that is not a bare hostname, so no path
# traversal, scheme, port or credentials can smuggle through to argv.
from actions.osint_scan import _normalize_target  # noqa: E402  (shared contract)


class DeepEyeUnavailable(RuntimeError):
    """Deep Eye is not installed, or its interpreter is missing."""


class InvalidTarget(ValueError):
    """The target is not a plain host JARVIS is willing to scan."""


class ScanCancelled(RuntimeError):
    """The user stopped the scan, or it outlived its timeout."""


def available() -> bool:
    """True when a scan could actually be started.

    Checks the three things that must all hold: the checkout, the interpreter
    deep-eye's own installer creates, and the entrypoint. Deliberately does not
    shell out — `--version` loads 30+ modules and takes seconds, far too slow
    to call while drawing a button.
    """
    return (DEEP_MAIN.is_file()
            and DEEP_PYTHON.is_file()
            and VENDOR_DIR.is_dir())


def _require_available() -> None:
    if not available():
        raise DeepEyeUnavailable(
            "Deep Eye is not installed. Expected:\n"
            f"  entrypoint: {DEEP_MAIN}\n"
            f"  python:     {DEEP_PYTHON}\n"
            "\nRun scripts/install.ps1 inside vendor/deep-eye, then restart JARVIS."
        )


def probe(timeout: float = 60.0) -> dict:
    """Actually run the scanner's --version and report what happened.

    `available()` only checks that three files exist, which is true partway
    through an install: the checkout and the interpreter are there before pip
    has fetched a single dependency. Asking the tool itself is the only honest
    answer, and it is slow (30+ imports), so it is NOT called while drawing a
    button — only when the user explicitly asks, or right before a scan.

    Never raises. Returns {"ok": bool, "version": str, "error": str|None}.
    """
    out = {"ok": False, "version": "", "error": None}
    if not available():
        out["error"] = "not installed"
        return out
    try:
        p = subprocess.run(
            [str(DEEP_PYTHON), str(DEEP_MAIN), "--version"],
            cwd=str(VENDOR_DIR), capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
            # A child console flashing on screen during a voice answer is
            # jarring; CREATE_NO_WINDOW is the documented way on Windows.
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                           if sys.platform == "win32" else 0),
        )
    except subprocess.TimeoutExpired:
        out["error"] = f"start-up timed out after {timeout:g}s"
        return out
    except Exception as e:
        out["error"] = str(e)
        return out
    text = (p.stdout or "") + (p.stderr or "")
    out["version"] = (p.stdout or "").strip()[:120]
    # --version may exit non-zero on some versions; trust a version string over
    # the code, but require *some* output so an import crash is not "ok".
    if out["version"] or "version" in text.lower():
        out["ok"] = True
    else:
        out["error"] = text.strip().splitlines()[-1] if text.strip() else (
            f"exited {p.returncode} with no output")
    return out


def normalize_target(raw: str) -> str:
    """Validate a scan target and return its bare hostname.

    Only the host survives. A URL with a path, a port, credentials or a scheme
    is reduced to its hostname here, so the value handed to the subprocess is
    always a plain domain and can never be mistaken for a flag or a path.

    Raises InvalidTarget rather than ValueError so callers can explain the
    refusal to the user instead of surfacing a stack trace.
    """
    value = (raw or "").strip()
    if not value:
        raise InvalidTarget("No target given.")

    # Strip anything that is not a hostname BEFORE handing it to the shared
    # normaliser. That helper classifies by shape, and a URL carrying
    # credentials ("http://user:pw@host/") matches its email pattern — so
    # passing it through unsaid produced the confusing refusal
    # "... is a email" for something that is plainly a web address. Reducing
    # to the host first keeps the decision honest and the message readable.
    candidate = value
    if "://" in candidate:
        candidate = urlparse(candidate).hostname or ""
    elif "/" in candidate:
        candidate = candidate.split("/", 1)[0]
    candidate = candidate.rsplit(":", 1)[0] if candidate.count(":") == 1 else candidate
    candidate = candidate.strip()

    # `localhost` is a legitimate target — and the only safe one available for
    # testing, since it is the one host a person can attack without permission
    # worries. The shared normaliser requires a dot, so allow it through
    # explicitly rather than loosening the rule for everything.
    if candidate.lower() in ("localhost", "localhost.localdomain"):
        return "localhost"

    try:
        kind, clean = _normalize_target(candidate)
    except ValueError as e:
        raise InvalidTarget(str(e)) from e
    # Only hosts are scannable. Refuse a bare IP or an email: a host name is
    # what a person can meaningfully claim to own, and it keeps the scope
    # obvious to whoever reads the confirmation banner.
    if kind != "domain":
        # "a ip" is what you get from naive interpolation, and it is spoken
        # aloud to the user, so the article has to agree with the noun.
        article = "an" if kind[0] in "aeiou" else "a"
        raise InvalidTarget(
            f"Deep Eye only scans web hosts, and {clean!r} is {article} {kind}. "
            f"Give a domain name, e.g. example.com."
        )
    return clean


@dataclass
class ScanResult:
    """Outcome of one finished scan."""
    target: str
    returncode: int
    output: str = ""
    findings: list = field(default_factory=list)
    report_path: Optional[Path] = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def summary(self) -> str:
        """One sentence for the voice: counts, not payloads.

        A finding's raw evidence is a SQL injection string or a token; reading
        that aloud is useless and leaks attack material into the audio stream.
        Severity counts are what a person actually needs to hear.
        """
        if not self.findings:
            return f"Scan of {self.target} finished with no findings."
        by_sev: dict = {}
        for f in self.findings:
            sev = str(f.get("severity", "unknown")).lower()
            by_sev[sev] = by_sev.get(sev, 0) + 1
        order = [s for s in ("critical", "high", "medium", "low", "info")
                 if s in by_sev]
        parts = [f"{by_sev[s]} {s}" for s in order]
        return (f"Scan of {self.target} finished: "
                f"{len(self.findings)} finding{'s' if len(self.findings) != 1 else ''}"
                f" ({', '.join(parts)}).")


def _gemini_key() -> str:
    """JARVIS's Gemini key, or "".

    Read through core.gemini.api_key() rather than parsing the JSON here, so
    there is one place that knows where the key lives and how it is cached. A
    missing or unreadable key is not an error — Deep Eye's rule-based checks
    work without one — so this never raises.
    """
    try:
        from core.gemini import api_key
        return str(api_key() or "")
    except Exception:
        return ""


def _parse_json_findings(stdout: str) -> tuple[list, Optional[Path]]:
    """Pull findings out of deep-eye's output.

    Best effort by design: the tool prints a progress narrative, not a
    guaranteed machine-readable blob, and a scan that found real problems
    must not be reported as failed just because the summary could not be
    parsed. Anything unparseable yields an empty list, and the caller still
    sees the raw text.
    """
    # A JSON report path is more reliable than parsing a mixed stdout stream.
    m = re.search(r"([\w./\\:-]+\.(?:json|sarif))", stdout)
    if m:
        candidate = Path(m.group(1))
        if not candidate.is_absolute():
            candidate = VENDOR_DIR / candidate
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                data = None
            if isinstance(data, dict):
                for key in ("findings", "vulnerabilities", "results"):
                    if isinstance(data.get(key), list):
                        return data[key], candidate
            elif isinstance(data, list):
                return data, candidate

    # Fall back to the last JSON object in the stream.
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if isinstance(data, dict):
            for key in ("findings", "vulnerabilities", "results"):
                if isinstance(data.get(key), list):
                    return data[key], None
    return [], None


class DeepEyeScan:
    """One running scan. Use as a context manager.

    Streams progress line by line so the UI can show work happening instead of
    freezing for five minutes, and always terminates the child — including on
    timeout, cancel, or an exception in the caller.
    """

    def __init__(self, target: str, timeout: float = DEFAULT_TIMEOUT,
                 extra_args: Optional[list[str]] = None,
                 formats: str = "json",
                 logger: Optional[Callable[[str], None]] = None):
        self.target = normalize_target(target)
        self.timeout = float(timeout)
        self.extra_args = list(extra_args or [])
        self.formats = formats
        self._logger = logger or (lambda _m: None)
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._cancelled = threading.Event()
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self.result: Optional[ScanResult] = None

    def command(self) -> list[str]:
        """The exact argv. Exposed for tests and for showing the user.

        `creationflags` is applied at launch, not here, so this list stays
        printable. No shell is involved at any point.

        The scheme is added back here even though the target is validated as a
        bare host: deep-eye rejects anything without http:// or https:// and
        exits 1 with a one-line message, so passing the hostname alone looked
        like a broken scan rather than a formatting mistake. Validation stays
        strict about the host; only the tool's own transport requirement is
        satisfied here.
        """
        return [str(DEEP_PYTHON), str(DEEP_MAIN),
                "-u", f"http://{self.target}",
                "--formats", self.formats,
                *self.extra_args]

    def _pump(self, on_line: Optional[Callable[[str], None]]) -> None:
        """Drain stdout to the end, forwarding each line. Runs on a worker."""
        assert self._proc is not None
        proc = self._proc
        try:
            # bufsize=1 + text gives line buffering, so progress is live.
            for raw in proc.stdout:                     # type: ignore[union-attr]
                line = raw.rstrip("\n")
                with self._lock:
                    self._lines.append(line)
                if on_line:
                    try:
                        on_line(line)
                    except Exception:
                        # A UI that fails to render one line must not kill the
                        # scan; the text is still collected either way.
                        pass
        except Exception:
            pass
        finally:
            try:
                proc.wait(timeout=self.timeout)
            except Exception:
                pass

    def start(self, on_line: Optional[Callable[[str], None]] = None) -> "DeepEyeScan":
        _require_available()
        if self._proc is not None:
            return self
        env = dict(os.environ)
        # Deep Eye is synchronous and prints progress; unbuffered keeps the
        # stream honest so the UI is not a lie about how far along it is.
        env["PYTHONUNBUFFERED"] = "1"
        # Force UTF-8 on the child's pipes. Windows otherwise hands the child a
        # cp1252 stdout, and Deep Eye's very first act is to print a banner of
        # box-drawing characters — which cp1252 cannot encode. The child died
        # with a UnicodeEncodeError before a single check ran, while the
        # installed-and-working scanner looked broken from the outside.
        #
        # This is why the same command worked from the shell and not from
        # here: there stdout was already UTF-8.
        env["PYTHONIOENCODING"] = "utf-8"
        # ...and to the Rich console, which is what actually writes the banner.
        env.setdefault("PYTHONUTF8", "1")
        env.pop("PYTHONHOME", None)
        # Hand over JARVIS's Gemini key to the scanner, but only through the
        # child's environment — never on disk and never in argv, where it would
        # be visible in any process listing. The AI provider is what generates
        # payloads and filters false positives, which is most of the difference
        # between a useful report and a wall of noise.
        key = _gemini_key()
        if key:
            env["GEMINI_API_KEY"] = key
            env.setdefault("GOOGLE_API_KEY", key)
        else:
            # Deep Eye's config has gemini enabled. Without a key it initialises
            # the provider and then fails on the first request, so the scan
            # degrades to the rule-based checks — worth saying out loud rather
            # than letting it look like the AI silently worked.
            self._logger("Deep Eye: no Gemini key found; AI checks will be skipped.")
        env["DEEP_EYE_NO_KEY_WARNING"] = "1" if not key else ""
        flags = 0
        if sys.platform == "win32":
            # Hide the console window that a child process would otherwise
            # flash on screen. CREATE_NO_WINDOW is the documented way; passing
            # it on POSIX would raise, hence the guard.
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            self.command(),
            cwd=str(VENDOR_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=flags,
        )
        self._thread = threading.Thread(target=self._pump, args=(on_line,),
                                        daemon=True, name="DeepEyeScan")
        self._thread.start()
        return self

    def cancel(self) -> None:
        """Ask the scan to stop, then make sure it did."""
        self._cancelled.set()
        self._terminate()

    def _terminate(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                proc.kill()          # no graceful HTTP abort on Windows
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            pass

    def wait(self) -> ScanResult:
        """Block until the scan finishes. Raises on cancel or timeout."""
        if self._proc is None:
            self.start()
        assert self._proc is not None
        if self._thread:
            self._thread.join(timeout=self.timeout + 5)
        code = self._proc.poll()
        if code is None:
            self._terminate()
            code = self._proc.poll() or -1
        with self._lock:
            output = "\n".join(self._lines)
        findings, report = _parse_json_findings(output)
        self.result = ScanResult(target=self.target, returncode=code,
                                 output=output, findings=findings,
                                 report_path=report)
        if self._cancelled.is_set():
            raise ScanCancelled(f"Scan of {self.target} was cancelled.")
        if code == -1 or (code != 0 and not output.strip()):
            raise ScanCancelled(
                f"Deep Eye did not finish on {self.target} "
                f"(exit {code}). It may have timed out after {self.timeout:g}s.")
        return self.result

    def stream(self, on_line: Optional[Callable[[str], None]] = None) -> Iterator[str]:
        """Start and yield progress lines as they arrive."""
        self.start(on_line)
        seen = 0
        while True:
            with self._lock:
                have = len(self._lines)
                chunk = self._lines[seen:]
                seen = have
            for line in chunk:
                yield line
            if self._proc is not None and self._proc.poll() is not None and have == seen:
                break
            if self._thread is None or not self._thread.is_alive():
                break
            threading.Event().wait(0.15)

    def __enter__(self) -> "DeepEyeScan":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Always kill the child, whatever happened in the body. An orphaned
        # scanner would keep hitting someone else's server after the assistant
        # stopped caring.
        self.cancel()
        return False


def request_scan(target: str, confirm, speak=None, on_line=None,
                 on_begin=None, on_done=None,
                 timeout: float = DEFAULT_TIMEOUT) -> str:
    """Ask for consent, and only then scan.

    `confirm` is core/confirm.request — the interface-issued gate. It returns a
    sentence for the model to speak and does the real work later, off the Qt
    thread, once the human presses CONFIRM.

    The validation and the gate happen before anything is executed, and there
    is no branch that skips them. That ordering is the point: an unavailable
    scanner or a malformed target must never reach the confirmation banner, and
    a banner must never appear for something that will not run.
    """
    try:
        host = normalize_target(target)
    except InvalidTarget as e:
        return f"I can't scan that: {e}"
    try:
        _require_available()
    except DeepEyeUnavailable as e:
        return str(e)

    def _run() -> str:
        if callable(on_begin):
            try:
                on_begin(host)
            except Exception:
                pass
        scan = DeepEyeScan(host, timeout=timeout)
        # The line callback runs on the reader thread. Anything it touches
        # that belongs to Qt must marshal itself; the UI's signals do that.
        scan.start(on_line=on_line)
        try:
            result = scan.wait()
        except ScanCancelled as e:
            if callable(on_done):
                try:
                    on_done(False, str(e))
                except Exception:
                    pass
            return str(e)
        except Exception as e:
            msg = f"The scan of {host} failed: {e}"
            if callable(on_done):
                try:
                    on_done(False, msg)
                except Exception:
                    pass
            return msg
        if callable(on_done):
            try:
                on_done(result.ok, result)
            except Exception:
                pass
        return result.summary()

    return confirm(
        "deep_eye_scan",
        "Run a security scan",
        f"Deep Eye will actively test this host:\n\n{host}\n\n"
        "50+ vulnerability checks, ~2-5 minutes. This sends real attack "
        "traffic to that server.\n\n"
        "Only continue if the site is yours or you have written permission "
        "to test it.",
        _run,
    )
