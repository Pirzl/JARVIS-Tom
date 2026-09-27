"""Generate and locally preview small static websites."""

from __future__ import annotations

import json
import re
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path

from core import gemini
from core.workspace import WorkspaceError, resolve_workspace, safe_path

DEFAULT_PROJECTS = Path.home() / "Desktop" / "JarvisProjects"
_SERVERS: dict[str, ThreadingHTTPServer] = {}
_SERVER_LOCK = threading.Lock()


def _clean_json(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _parse_site_bundle(text: str) -> tuple[dict, dict[str, str]]:
    payload = json.loads(_clean_json(text))
    if not isinstance(payload, dict):
        raise ValueError("website generator returned a non-object response")
    brief = payload.get("design_brief")
    files = payload.get("files")
    expected = {"index.html", "style.css", "script.js", "favicon.svg", "site.webmanifest", "README.md"}
    if not isinstance(brief, dict) or not isinstance(files, dict):
        raise ValueError("website generator must return a design_brief and files object")
    if not expected.issubset(files) or not all(isinstance(value, str) for value in files.values()):
        raise ValueError("website generator returned an invalid file bundle")
    return brief, {name: files[name] for name in expected}


def _generate_site(description: str) -> tuple[dict, dict[str, str]]:
    prompt = f"""You are a senior art director and frontend engineer. Create a distinctive, production-quality static website, not a generic template.

User request:
{description}

First decide a coherent design system and then implement it. Return ONLY valid JSON:
{{
    "design_brief": {{
        "audience": "...",
        "visual_direction": "...",
        "palette": ["...", "..."],
        "typography": "...",
        "sections": ["..."],
        "signature_interaction": "...",
        "asset_strategy": "..."
    }},
    "files": {{
        "index.html": "complete source",
        "style.css": "complete source",
        "script.js": "complete source",
        "favicon.svg": "complete source",
        "site.webmanifest": "complete JSON",
        "README.md": "short project notes"
    }}
}}

Quality contract:
- Use a strong visual point of view with intentional typography, color, spacing, and composition.
- Include at least four meaningful sections and one distinctive interaction.
- Use semantic HTML, correct heading hierarchy, accessible names, visible focus styles, and keyboard-friendly controls.
- Include title, meta description, viewport, theme-color, Open Graph basics, and a language attribute.
- Use responsive CSS with an explicit mobile layout and no horizontal overflow.
- Use local inline/embedded assets or stable CSS artwork; never depend on remote images or packages.
- Do not use generic hero/card boilerplate, filler copy, "Lorem ipsum", or TODO placeholders.
- Keep the implementation vanilla HTML/CSS/JavaScript and do not include markdown fences.
"""
    response = gemini.call(prompt, tier=gemini.SMART, timeout_ms=60000)
    if response is None:
        raise RuntimeError("every Gemini model on the ladder failed")
    return _parse_site_bundle(response.text)


def _stop_preview(project_dir: Path) -> str:
    key = str(project_dir.resolve())
    with _SERVER_LOCK:
        server = _SERVERS.pop(key, None)
    if server is None:
        return "No preview is running for this project."
    server.shutdown()
    server.server_close()
    return f"Preview stopped for {project_dir}."


def _start_preview(project_dir: Path, port: int, *, open_browser: bool = True) -> str:
    key = str(project_dir.resolve())
    with _SERVER_LOCK:
        existing = _SERVERS.get(key)
        if existing is not None:
            actual_port = existing.server_address[1]
        else:
            handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(
                *args, directory=str(project_dir), **kwargs
            )
            try:
                server = ThreadingHTTPServer(("127.0.0.1", port), handler)
            except OSError as exc:
                return f"Could not start website preview on port {port}: {exc}"
            server.daemon_threads = True
            _SERVERS[key] = server
            actual_port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{actual_port}/"
    if open_browser:
        webbrowser.open(url)
    return f"Preview running at {url}"


def _extract_preview_url(message: str) -> str:
    match = re.search(r"https?://[^\s]+", message)
    if not match:
        raise RuntimeError(message)
    return match.group(0)


class _PageStructureParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.headings: list[int] = []
        self.images_without_alt = 0
        self.buttons_without_name = 0
        self.links_without_name = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.headings.append(int(tag[1]))
        if tag == "img" and "alt" not in attributes:
            self.images_without_alt += 1
        if tag == "button" and not (attributes.get("aria-label") or attributes.get("title")):
            self.buttons_without_name += 1
        if tag == "a" and not (attributes.get("aria-label") or attributes.get("title")):
            self.links_without_name += 1


def _quality_audit(project_dir: Path) -> list[str]:
    """Find structural quality problems that runtime checks cannot see."""
    try:
        html = safe_path(project_dir, "index.html").read_text(encoding="utf-8")
        css = safe_path(project_dir, "style.css").read_text(encoding="utf-8")
    except OSError as exc:
        return [f"Could not read site files for quality audit: {exc}"]

    parser = _PageStructureParser()
    parser.feed(html)
    issues = []
    lower = html.lower()
    required_patterns = {
        r"<title\b": "missing document title",
        r"<meta\b[^>]*\bname\s*=\s*['\"]description['\"]": "missing meta description",
        r"<meta\b[^>]*\bname\s*=\s*['\"]viewport['\"]": "missing responsive viewport metadata",
        r"<meta\b[^>]*\bproperty\s*=\s*['\"]og:title['\"]": "missing Open Graph title",
        r"<main\b": "missing semantic main landmark",
    }
    issues.extend(message for pattern, message in required_patterns.items() if not re.search(pattern, lower))
    if not parser.headings or parser.headings[0] != 1:
        issues.append("heading hierarchy should begin with one h1")
    if parser.headings.count(1) != 1:
        issues.append("page should contain exactly one h1")
    if parser.images_without_alt:
        issues.append(f"{parser.images_without_alt} image(s) missing alt text")
    if parser.buttons_without_name:
        issues.append(f"{parser.buttons_without_name} button(s) missing accessible names")
    if parser.links_without_name:
        issues.append(f"{parser.links_without_name} link(s) missing accessible names")
    if len(re.findall(r"<(?:section|article)\b", lower)) < 4:
        issues.append("page needs at least four meaningful content sections")
    if "@media" not in css:
        issues.append("responsive CSS media queries are missing")
    if "focus-visible" not in css and ":focus" not in css:
        issues.append("visible keyboard focus styling is missing")
    if re.search(r"lorem ipsum|todo|coming soon", lower):
        issues.append("placeholder or unfinished copy detected")
    return issues


def _verify_preview(project_dir: Path, url: str) -> dict:
    """Check desktop/mobile rendering, browser errors, and failed requests."""
    report = {
        "ok": True,
        "screenshots": [],
        "console_errors": [],
        "page_errors": [],
        "failed_requests": [],
        "http_errors": [],
        "quality_issues": _quality_audit(project_dir),
    }
    artifact_dir = project_dir / ".jarvis" / "verification"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        report["ok"] = False
        report["page_errors"].append(f"Playwright is unavailable: {exc}")
        return report

    viewports = {
        "desktop": {"width": 1440, "height": 900},
        "mobile": {"width": 390, "height": 844},
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            for name, viewport in viewports.items():
                page = browser.new_page(viewport=viewport)
                page.on(
                    "console",
                    lambda message, viewport_name=name: (
                        report["console_errors"].append(
                            f"{viewport_name}: {message.text}"
                        )
                        if message.type == "error"
                        else None
                    ),
                )
                page.on(
                    "pageerror",
                    lambda error, viewport_name=name: report["page_errors"].append(
                        f"{viewport_name}: {error}"
                    ),
                )
                page.on(
                    "requestfailed",
                    lambda request, viewport_name=name: report["failed_requests"].append(
                        f"{viewport_name}: {request.url} ({request.failure})"
                    ),
                )
                response = page.goto(url, wait_until="networkidle", timeout=15000)
                if response is not None and response.status >= 400:
                    report["http_errors"].append(
                        f"{viewport_name}: {response.status} {response.url}"
                    )
                if page.evaluate("document.documentElement.scrollWidth > window.innerWidth"):
                    report["quality_issues"].append(
                        f"{viewport_name}: horizontal overflow detected"
                    )
                screenshot = artifact_dir / f"{name}.png"
                page.screenshot(path=str(screenshot), full_page=True)
                report["screenshots"].append(str(screenshot))
                page.close()
            browser.close()
    except Exception as exc:
        report["page_errors"].append(f"Browser verification failed: {exc}")

    report["ok"] = not any(
        report[key]
        for key in ("console_errors", "page_errors", "failed_requests", "http_errors", "quality_issues")
    )
    return report


def _read_site_bundle(project_dir: Path) -> dict[str, str]:
    return {
        filename: safe_path(project_dir, filename).read_text(encoding="utf-8")
        for filename in ("index.html", "style.css", "script.js")
    }


def _repair_site(description: str, files: dict[str, str], report: dict) -> dict[str, str]:
    prompt = f"""You are repairing a static website after browser verification failed.

Original request:
{description}

Browser verification report:
{json.dumps(report, indent=2)[:6000]}

Current files:
--- index.html ---
{files['index.html'][:12000]}
--- style.css ---
{files['style.css'][:12000]}
--- script.js ---
{files['script.js'][:12000]}

Fix the reported runtime, console, network, responsive-layout, or quality problems.
Return ONLY valid JSON in this shape: {{"design_brief": {{}}, "files": {{
"index.html": "complete source", "style.css": "complete source", "script.js": "complete source",
"favicon.svg": "complete source", "site.webmanifest": "complete JSON", "README.md": "short notes"
}}}}. Do not include markdown fences.
"""
    response = gemini.call(prompt, tier=gemini.SMART, timeout_ms=60000)
    if response is None:
        raise RuntimeError("every Gemini model on the ladder failed")
    _, files = _parse_site_bundle(response.text)
    return files


def _verify_and_repair(
    project_dir: Path,
    url: str,
    description: str,
    max_attempts: int,
) -> tuple[dict, int]:
    report = {}
    for attempt in range(max_attempts + 1):
        report = _verify_preview(project_dir, url)
        if report["ok"] or attempt == max_attempts:
            return report, attempt
        try:
            repaired = _repair_site(description, _read_site_bundle(project_dir), report)
            for filename, content in repaired.items():
                safe_path(project_dir, filename).write_text(content, encoding="utf-8")
        except (RuntimeError, ValueError, OSError) as exc:
            report["page_errors"].append(f"Automatic repair failed: {exc}")
            return report, attempt
    return report, max_attempts


def _format_verification(report: dict, attempts: int) -> str:
    status = "passed" if report.get("ok") else "found issues"
    lines = [f"Browser verification {status} after {attempts} repair attempt(s)."]
    if report.get("screenshots"):
        lines.append("Screenshots: " + ", ".join(report["screenshots"]))
    for key, label in (
        ("console_errors", "Console errors"),
        ("page_errors", "Page errors"),
        ("failed_requests", "Failed requests"),
        ("http_errors", "HTTP errors"),
        ("quality_issues", "Quality issues"),
    ):
        if report.get(key):
            lines.append(f"{label}: " + " | ".join(report[key][:5]))
    return "\n".join(lines)


def website_builder(parameters: dict, player=None, speak=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "create")).strip().lower()
    description = str(params.get("description", "")).strip()
    project_name = str(params.get("project_name", "jarvis_site")).strip()
    workspace_arg = str(params.get("workspace_path", "")).strip()
    port = int(params.get("port", 0) or 0)
    max_repair_attempts = max(0, min(int(params.get("max_repair_attempts", 2) or 0), 2))

    try:
        workspace = resolve_workspace(workspace_arg, DEFAULT_PROJECTS, create=True)
        project_dir = safe_path(workspace, project_name)
        project_dir.mkdir(parents=True, exist_ok=True)
    except (WorkspaceError, ValueError) as exc:
        return f"Website workspace error: {exc}"

    if action == "stop":
        return _stop_preview(project_dir)
    if action not in {"create", "preview", "verify"}:
        return "Unknown website action. Use create, preview, verify, or stop."
    if action == "preview":
        return _start_preview(project_dir, port)
    if action == "verify":
        if not (project_dir / "index.html").exists():
            return f"No website exists at {project_dir}. Create it first."
        preview = _start_preview(project_dir, port, open_browser=False)
        if not preview.startswith("Preview running"):
            return preview
        report, attempts = _verify_and_repair(
            project_dir,
            _extract_preview_url(preview),
            description or "Verify and repair this website.",
            max_repair_attempts,
        )
        return _format_verification(report, attempts)
    if not description:
        return "Please describe the website you want me to create."

    try:
        brief, files = _generate_site(description)
        for filename, content in files.items():
            safe_path(project_dir, filename).write_text(content, encoding="utf-8")
        safe_path(project_dir, ".jarvis/design-brief.json").parent.mkdir(parents=True, exist_ok=True)
        safe_path(project_dir, ".jarvis/design-brief.json").write_text(
            json.dumps(brief, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return f"Website generation failed: {exc}"
    except OSError as exc:
        return f"Website files could not be saved: {exc}"

    preview = _start_preview(project_dir, port)
    if not preview.startswith("Preview running"):
        return f"Website created at {project_dir}, but preview failed: {preview}"
    report, attempts = _verify_and_repair(
        project_dir,
        _extract_preview_url(preview),
        description,
        max_repair_attempts,
    )
    message = (
        f"Website created at {project_dir}. {preview}\n"
        f"{_format_verification(report, attempts)}"
    )
    if speak:
        speak(message)
    return message


TOOL = {
    "name": "website_builder",
    "description": "Creates a responsive static website in a safe project workspace and opens a local preview.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "create, preview, verify, or stop (default: create)",
            },
            "description": {
                "type": "STRING",
                "description": "The website to create, including its purpose and visual direction",
            },
            "project_name": {
                "type": "STRING",
                "description": "Project folder name",
            },
            "workspace_path": {
                "type": "STRING",
                "description": "Existing parent folder for the project",
            },
            "port": {
                "type": "INTEGER",
                "description": "Local preview port, or 0 to choose an available port",
            },
            "max_repair_attempts": {
                "type": "INTEGER",
                "description": "Maximum automatic browser-guided repair attempts, from 0 to 2",
            },
        },
    },
    "handler": website_builder,
}