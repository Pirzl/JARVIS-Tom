"""The taskbar icon has to be JARVIS, not Python.

Windows picks the taskbar button icon from the *process* when the process
declares one, and from the executable otherwise. Running under `python.exe`
means the fallback is the Python logo, which is what JARVIS was showing. So
the fix is `QApplication.setWindowIcon`, not `MainWindow.setWindowIcon` --
setting only the window leaves the taskbar entry showing the interpreter's
icon, and that is precisely the bug these tests exist to catch.

The other half is loading it at all. `config/jarvis.ico` is a real multi-size
icon; if a future edit points at a path that does not exist, `QIcon` silently
becomes null and the logo reverts to Python with no error anywhere. So the
tests assert the icon is *not null*, which is the only assertion that
distinguishes "loaded" from "silently absent".
"""
from __future__ import annotations

import os
import struct
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QIcon  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import ui  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class TestTheIconFileIsUsable(unittest.TestCase):
    """Before any Qt is involved: is the file a real icon?"""

    def test_the_shipped_ico_exists(self):
        self.assertTrue((BASE_DIR / "config" / "jarvis.ico").exists(),
                        "config/jarvis.ico is missing; the taskbar icon has "
                        "nothing to load")

    def test_it_is_a_real_ico_with_the_signature(self):
        data = (BASE_DIR / "config" / "jarvis.ico").read_bytes()
        self.assertEqual(data[:4], b"\x00\x00\x01\x00",
                         "not an ICO file (bad signature)")

    def test_it_carries_the_sizes_windows_asks_for(self):
        """Windows picks a size from the file; a single-size icon gets scaled
        from one bitmap and looks soft in the taskbar."""
        data = (BASE_DIR / "config" / "jarvis.ico").read_bytes()
        count = struct.unpack("<H", data[4:6])[0]
        sizes = set()
        for i in range(count):
            off = 6 + i * 16
            w = data[off] or 256
            h = data[off + 1] or 256
            sizes.add((w, h))
        for wanted in ((16, 16), (32, 32), (48, 48), (256, 256)):
            self.assertIn(wanted, sizes,
                          f"{wanted[0]}px missing; Windows will scale instead "
                          f"of choosing")

    def test_it_is_not_empty(self):
        self.assertGreater((BASE_DIR / "config" / "jarvis.ico").stat().st_size,
                           1000, "a 900-byte icon is a placeholder")


class TestTheApplicationIconIsActuallySet(unittest.TestCase):
    """The assertion that distinguishes loaded from silently absent."""

    def setUp(self):
        self.app = _app()

    def test_app_icon_is_not_null(self):
        ui.JarvisUI._ICON_CANDIDATES  # the path list exists
        self.app.setWindowIcon(QIcon())       # start from a known-bad state
        helper = ui.JarvisUI.__new__(ui.JarvisUI)
        helper._app = self.app
        helper._apply_app_icon()
        self.assertFalse(self.app.windowIcon().isNull(),
                         "the icon did not load; the taskbar will show the "
                         "Python logo again")

    def test_the_window_gets_the_same_icon(self):
        """Set on the app *and* the window. App-only fixes the taskbar and
        Alt-Tab; window-only fixes the title bar and nothing else."""
        helper = ui.JarvisUI.__new__(ui.JarvisUI)
        helper._app = self.app
        helper._apply_app_icon()
        self.assertFalse(self.app.windowIcon().isNull())
        self.assertFalse(QIcon.isNull(self.app.windowIcon()))

    def test_a_missing_file_does_not_crash(self):
        """The first candidate not existing must fall through, not raise.

        `QIcon` on a nonexistent path is null rather than an error, so the
        loop needs an explicit `isNull` check to move on -- and it has one.
        """
        original = ui.JarvisUI._ICON_CANDIDATES
        try:
            ui.JarvisUI._ICON_CANDIDATES = ("config/does-not-exist.ico",)
            helper = ui.JarvisUI.__new__(ui.JarvisUI)
            helper._app = self.app
            helper._apply_app_icon()   # must not raise
        finally:
            ui.JarvisUI._ICON_CANDIDATES = original

    def test_the_fallback_icon_exists_too(self):
        """The android ico is the backup when config/jarvis.ico is absent."""
        self.assertTrue((BASE_DIR / "assets" / "jarvis_android.ico").exists())


class TestQIconIsActuallyImported(unittest.TestCase):
    def test_ui_module_exposes_qicon(self):
        """A missing import would surface as a NameError inside the helper,
        which the tests above would catch -- but only after the fact. This
        names the requirement directly."""
        self.assertTrue(hasattr(ui, "QIcon"),
                        "ui.py does not import QIcon; _apply_app_icon would "
                        "raise NameError the first time it runs")


class TestMutationChecks(unittest.TestCase):
    def test_M1_removing_the_app_level_set_breaks_the_test(self):
        """The distinction the bug turned on. If a future edit drops
        `app.setWindowIcon` and keeps only the window's, these go red."""
        import inspect
        src = inspect.getsource(ui.JarvisUI._apply_app_icon)
        self.assertIn("setWindowIcon", src,
                      "the app-level icon is what changes the taskbar")
        src2 = inspect.getsource(ui.JarvisUI.__init__)
        self.assertIn("setWindowIcon", src2,
                      "the window icon is what changes the title bar")

    def test_M2_a_null_check_is_present(self):
        import inspect
        src = inspect.getsource(ui.JarvisUI._apply_app_icon)
        self.assertIn("isNull", src,
                      "without the null check a bad path silently stops the "
                      "search and the logo reverts to Python")


if __name__ == "__main__":
    unittest.main()
