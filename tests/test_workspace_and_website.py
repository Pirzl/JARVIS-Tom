import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen
from unittest.mock import patch

from core.workspace import WorkspaceError, safe_path
from actions import website_builder


class WorkspaceTests(unittest.TestCase):
    def test_safe_path_stays_inside_workspace(self):
        root = Path(tempfile.mkdtemp())
        self.assertEqual(safe_path(root, "site/index.html"), root / "site" / "index.html")

    def test_safe_path_rejects_parent_traversal(self):
        root = Path(tempfile.mkdtemp())
        with self.assertRaises(WorkspaceError):
            safe_path(root, "../outside.txt")


class WebsitePreviewTests(unittest.TestCase):
    def test_quality_audit_flags_basic_page(self):
        project = Path(tempfile.mkdtemp())
        (project / "index.html").write_text("<h1>Hello</h1>", encoding="utf-8")
        (project / "style.css").write_text("body { color: red; }", encoding="utf-8")
        issues = website_builder._quality_audit(project)
        self.assertIn("missing document title", issues)
        self.assertIn("responsive CSS media queries are missing", issues)
        self.assertIn("page needs at least four meaningful content sections", issues)

    def test_quality_audit_accepts_professional_structure(self):
        project = Path(tempfile.mkdtemp())
        (project / "index.html").write_text(
            """<!doctype html><html lang='en'><head>
            <title>Studio</title><meta name='description' content='A studio'>
            <meta name='viewport' content='width=device-width'><meta property='og:title' content='Studio'>
            </head><body><main><h1>Studio</h1>
            <section><h2>Work</h2></section><section><h2>Services</h2></section>
            <section><h2>Process</h2></section><section><h2>Contact</h2>
            <a href='#contact' aria-label='Contact studio'>Contact</a></section>
            </main></body></html>""",
            encoding="utf-8",
        )
        (project / "style.css").write_text(
            "@media (max-width: 700px) { body { overflow-x: hidden; } } :focus-visible { outline: 2px solid blue; }",
            encoding="utf-8",
        )
        self.assertEqual(website_builder._quality_audit(project), [])

    def test_preview_serves_project_files_and_stops(self):
        root = Path(tempfile.mkdtemp())
        (root / "index.html").write_text("<h1>Jarvis</h1>", encoding="utf-8")
        project = root / "site"
        project.mkdir()
        (project / "index.html").write_text("<h1>Jarvis</h1>", encoding="utf-8")

        original_open = website_builder.webbrowser.open
        website_builder.webbrowser.open = lambda url: True
        try:
            message = website_builder._start_preview(project, 0)
            url = message.split(" at ", 1)[1]
            with urlopen(url, timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("Jarvis", response.read().decode("utf-8"))
        finally:
            website_builder.webbrowser.open = original_open
            website_builder._stop_preview(project)

    def test_repair_loop_is_bounded(self):
        project = Path(tempfile.mkdtemp())
        for filename in ("index.html", "style.css", "script.js"):
            (project / filename).write_text("original", encoding="utf-8")

        failing_report = {
            "ok": False,
            "screenshots": [],
            "console_errors": ["desktop: broken"],
            "page_errors": [],
            "failed_requests": [],
            "http_errors": [],
        }
        with patch.object(
            website_builder,
            "_verify_preview",
            side_effect=[failing_report, failing_report, failing_report],
        ) as verify, patch.object(
            website_builder,
            "_repair_site",
            return_value={
                "index.html": "fixed",
                "style.css": "fixed",
                "script.js": "fixed",
            },
        ) as repair:
            report, attempts = website_builder._verify_and_repair(
                project, "http://127.0.0.1:1/", "test site", 2
            )

        self.assertFalse(report["ok"])
        self.assertEqual(attempts, 2)
        self.assertEqual(verify.call_count, 3)
        self.assertEqual(repair.call_count, 2)


if __name__ == "__main__":
    unittest.main()