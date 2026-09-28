"""Voice access to the web auditor, so a report can be requested by talking.

JARVIS's Deep Eye already has a voice action, and this follows the same shape
rather than inventing a second one: a session object that owns the run, a
method that speaks, and a binding that the action layer calls. Keeping the
three separate is what stops the UI from reaching into the scanner, which is
the coupling that made the original globe work hard to maintain.

The confirmation gate is not optional here and does not live in this file. A
scan sends real requests to a live host, and the only thing standing between a
misheard URL and somebody else's production system is Thomas saying yes. The
`pending` attribute exists so the caller can show the question before anything
is sent; `confirm()` is the only path that starts a run, and it is what the
action layer calls after the user agrees.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
TOOL_PATH = BASE_DIR / "tools" / "web_audit.py"


def _load_tool():
    """Import tools/web_audit.py without making tools a package.

    The file is a standalone CLI first -- it has to run when Hermes and JARVIS
    are both down -- so it keeps no `__init__.py` and is loaded by path. A
    normal import would couple the assistant's availability to this file being
    importable as a module, which is the dependency this design exists to
    avoid.

    The module must be registered in `sys.modules` before it is executed, and
    that is not a detail. `@dataclass` resolves the annotations of a class by
    looking up its own module in `sys.modules`, and a module built by
    `module_from_spec` and not registered is absent from that table -- so the
    very first dataclass raised `AttributeError: 'NoneType' object has no
    attribute '__dict__'`, and the audit was dead on arrival in exactly the
    environment it was written to survive.
    """
    if not TOOL_PATH.exists():
        return None
    name = "web_audit_tool"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, TOOL_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec_module, and removed on failure so a broken import
    # does not leave a half-initialised module behind for the next caller.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[name]
        raise
    return module


class WebAuditSession:
    """Owns one audit run and the words that come out of it.

    Three rules the class exists to keep:

    * nothing is sent until `confirm()` -- `scan()` only prepares
    * a run happens off the Qt thread, always, because it is network I/O with
      a twenty second timeout and freezing the interface on that is not
      acceptable
    * a failure to speak never loses the report; the text is returned too
    """

    MAX_TARGET_LEN = 300

    def __init__(self, speak: Optional[Callable[[str], None]] = None):
        self._speak = speak or (lambda text: None)
        self.lock = threading.Lock()
        self.pending: Optional[dict] = None
        self.result: Optional[dict] = None
        self.running = False

    # -- validation ----------------------------------------------------
    def _normalise(self, raw: str) -> str:
        target = (raw or "").strip().strip("\"'")
        if not target:
            raise ValueError("no target given")
        if "://" not in target:
            # A bare host:port is a local address the user is testing, and
            # upgrading it to https:// breaks the audit against a plain HTTP
            # dev server. Left as-is, with a scheme, rather than guessed at.
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d+$", target) or \
                    target.startswith("localhost"):
                target = "http://" + target
            else:
                target = "https://" + target
        if len(target) > self.MAX_TARGET_LEN:
            raise ValueError(
                f"that target is {len(target)} characters long; a web address "
                f"is never that long, so something other than a URL was heard")
        head = target.split("://", 1)[1].split("/")[0].lower()
        # A bare IP is legitimate to scan; a hostname with no dot is not, and
        # is almost always a misheard fragment.
        if "." not in head and not head.startswith("localhost"):
            raise ValueError(f"'{head}' is not a web address")
        return target.rstrip("/")

    # -- the two-step flow ---------------------------------------------
    def scan(self, raw_target: str) -> str:
        """Prepare an audit and return the question to ask. Sends nothing."""
        try:
            target = self._normalise(raw_target)
        except ValueError as exc:
            return str(exc)
        with self.lock:
            if self.running:
                return ("An audit is already running. Give it a moment before "
                        "starting another.")
            self.pending = {"target": target}
        return (f"You want me to audit {target}. That sends real requests to "
                f"a live server, so I need you to confirm you own it or have "
                f"permission. Say yes to go ahead.")

    def cancel(self) -> str:
        with self.lock:
            had = self.pending is not None
            self.pending = None
        return ("Cancelled, nothing was sent." if had
                else "There was nothing to cancel.")

    def confirm(self, timeout: int = 20, delay: float = 0.35,
                on_done: Optional[Callable[[dict], None]] = None) -> str:
        """Run the pending audit. Only this sends anything."""
        with self.lock:
            pending, self.pending = self.pending, None
            if pending is None:
                return ("Nothing is waiting for confirmation. Tell me which "
                        "site to audit first.")
            if self.running:
                return "An audit is already running."
            self.running = True
        target = pending["target"]
        result: dict = {}

        def work():
            try:
                tool = _load_tool()
                if tool is None:
                    result["error"] = (f"the audit tool is missing at "
                                       f"{TOOL_PATH}")
                    return
                report = tool.audit(target, timeout=timeout, delay=delay)
                result["report"] = report.as_dict()
                result["headline"] = report.headline()
                result["voice"] = report.voice_line()
            except Exception as exc:  # noqa: BLE001
                result["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                # The result has to be handed back before `running` clears.
                # Ordering matters: a caller that sees `running == False` and
                # then reads `result` must find the finished report, not the
                # state from a moment earlier.
                self.result = result
                with self.lock:
                    self.running = False
                if on_done is not None:
                    try:
                        on_done(result)
                    except Exception:  # noqa: BLE001
                        # A UI callback that raises must not leave the session
                        # looking busy forever.
                        pass

        thread = threading.Thread(target=work, daemon=True,
                                  name="web-audit")
        # Kept on the instance. The first version started the thread and
        # dropped the handle, which left `wait()` unable to join it and
        # `result` permanently empty -- so a caller that polled got nothing
        # and had no way to tell a finished run from one still in flight.
        self._thread = thread
        thread.start()
        return target

    def wait(self, timeout: float = 180) -> Optional[dict]:
        """Block until the running audit finishes. Test and CLI use only.

        Never called from the interface: the UI is driven by the `on_done`
        callback instead, because blocking the Qt thread on network I/O is
        the exact failure this class is arranged to avoid.
        """
        thread = getattr(self, "_thread", None)
        if thread is not None:
            thread.join(timeout)
        return self.result

    # -- speaking ------------------------------------------------------
    def speak_last(self) -> str:
        """The report as it should be heard."""
        if not self.result:
            return ("I have not run an audit yet. Tell me which site and I "
                    "will ask before sending anything.")
        if self.result.get("error"):
            return f"The audit failed: {self.result['error']}"
        if self.result.get("voice"):
            return self.result["voice"]
        return self.result.get("headline", "")

    def markdown(self) -> str:
        """The full report as Markdown, for a file or an email."""
        if not self.result or not self.result.get("report"):
            return ""
        tool = _load_tool()
        if tool is None:
            return ""
        report = tool.AuditReport(
            target=self.result["report"]["target"],
            started_at=self.result["report"]["started_at"],
            results=[tool.CheckResult(**r) for r in
                     self.result["report"]["results"]],
            unreachable=self.result["report"].get("unreachable", ""))
        return report.to_markdown()


def audit_now(target: str, timeout: int = 20, delay: float = 0.35) -> dict:
    """One-shot audit for callers that already have consent.

    For the skill and the CLI, where the user typed the command themselves and
    `--confirm` *is* the consent. Kept separate from the session so that
    nothing in the voice path can reach it by accident.
    """
    tool = _load_tool()
    if tool is None:
        raise RuntimeError(f"audit tool missing at {TOOL_PATH}")
    return tool.audit(target, timeout=timeout, delay=delay).as_dict()
