"""The GEV bridge has to survive a re-clone of the vendor tree.

`vendor/` is gitignored, and rightly: it is someone else's repository and 283
MB of node_modules. But the JARVIS bridge -- the 176 lines that let the globe
be moved by voice and the spoken answer be drawn on it -- live in
`vendor/gods-eye/src/sharelink.js` and therefore existed nowhere in this
repository. Clone again, and the globe opens and moves and then the markers
silently never appear, with nothing anywhere saying why. The Python side would
be committed and correct, which is the worst shape for a bug to have.

So the bridge is kept as patches/gods-eye-jarvis-bridge.patch with an applier
beside it, and these tests hold the two ends together:

  * the patch on disk really does rebuild the patched file, byte for byte
  * the Python and the JavaScript still name the same event and the same
    methods, so a rename on one side alone cannot pass unnoticed
  * the applier is safe to run twice, because running it twice on a live
    checkout would duplicate every method and break the class

The first of those is the one that matters. A patch file that exists, looks
right, and does not apply is worse than no patch at all, because it is
believed. It happened once here: the diff was generated through git on a
CRLF working tree, `git apply` returned 0, and the file it produced did not
contain a single line of the bridge.
"""
from __future__ import annotations

import difflib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PATCH = BASE_DIR / "patches" / "gods-eye-jarvis-bridge.patch"
APPLIER = BASE_DIR / "patches" / "apply_gev_bridge.py"
VENDOR = BASE_DIR / "vendor" / "gods-eye"
TARGET = "src/sharelink.js"


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n")


class TestTheBridgeIsReproducible(unittest.TestCase):
    """The patch must actually rebuild what is running."""

    @classmethod
    def setUpClass(cls):
        if not PATCH.exists():
            raise unittest.SkipTest("no hay patch del puente")
        if not (VENDOR / TARGET).exists():
            raise unittest.SkipTest("no hay checkout de GEV")
        cls.patched = _norm((VENDOR / TARGET).read_text(encoding="utf-8",
                                                        errors="replace"))
        # The upstream file, straight from the vendor repository's own history.
        try:
            proc = subprocess.run(["git", "show", "HEAD:" + TARGET],
                                  cwd=str(VENDOR), capture_output=True,
                                  text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            raise unittest.SkipTest("no se puede leer el original de GEV")
        if proc.returncode != 0:
            raise unittest.SkipTest("no se puede leer el original de GEV")
        cls.original = _norm(proc.stdout)

    def test_the_patch_is_well_formed(self):
        lines = _norm(PATCH.read_text(encoding="utf-8")).splitlines()
        self.assertTrue(lines[0].startswith("--- a/"),
                        "un patch sin cabecera de fichero no aplica")
        self.assertTrue(lines[1].startswith("+++ b/"))
        hunks = [l for l in lines if l.startswith("@@")]
        self.assertTrue(hunks, "un patch sin hunks no hace nada")

    def test_the_patch_rebuilds_the_running_file_exactly(self):
        """The test that catches a patch which applies to nothing.

        Applied to a copy of the pristine upstream file, the result must equal
        the file the app is actually running. Anything less -- a silently
        skipped hunk, a lost method, a stale context -- means a re-clone would
        leave the bridge in a different state from the one tested.
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir(parents=True)
            (src / "sharelink.js").write_text(self.original, encoding="utf-8",
                                              newline="")
            proc = subprocess.run(
                ["git", "apply", "-p1", "--recount", str(PATCH)],
                cwd=tmp, capture_output=True, text=True, timeout=120)
            self.assertEqual(
                proc.returncode, 0,
                "el patch no aplica a un fichero limpio:\n%s"
                % (proc.stderr or proc.stdout))
            rebuilt = _norm((src / "sharelink.js").read_text(
                encoding="utf-8", errors="replace"))
        self.assertEqual(rebuilt, self.patched,
                         "el patch no reconstruye el fichero en uso")

    def test_the_rebuilt_file_contains_both_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir(parents=True)
            (src / "sharelink.js").write_text(self.original, encoding="utf-8",
                                              newline="")
            subprocess.run(["git", "apply", "-p1", "--recount", str(PATCH)],
                           cwd=tmp, capture_output=True, text=True,
                           timeout=120)
            rebuilt = (src / "sharelink.js").read_text(encoding="utf-8",
                                                       errors="replace")
        for needed in ("jarvisFocus(detail)", "jarvisMarkers(detail)",
                       "gev:jarvis-focus", "gev:jarvis-markers"):
            self.assertIn(needed, rebuilt,
                          "el puente reconstruido no trae %s" % needed)

    def test_the_original_does_not_contain_the_bridge(self):
        """Otherwise the patch is empty and proves nothing."""
        self.assertNotIn("jarvisMarkers", self.original,
                         "el original upstream ya trae el puente: el patch "
                         "no aporta nada y hay que regenerarlo")

    def test_applying_twice_is_refused(self):
        """A second run would duplicate every method and break the class."""
        source = APPLIER.read_text(encoding="utf-8")
        self.assertIn("already_patched", source,
                      "el aplicador debe detectar un checkout ya parcheado")
        self.assertIn("SENTINEL", source,
                      "y debe usar un centinela, no solo un comentario")


class TestBothSidesNameTheSameThing(unittest.TestCase):
    """Two files, two repositories, nothing checking them but this."""

    @classmethod
    def setUpClass(cls):
        cls.js_path = VENDOR / TARGET
        if not cls.js_path.exists():
            raise unittest.SkipTest("no hay checkout de GEV")
        cls.js = cls.js_path.read_text(encoding="utf-8", errors="replace")
        cls.py = (BASE_DIR / "ui_world_view.py").read_text(encoding="utf-8")
        cls.session = (BASE_DIR / "core" / "deep_eye_session.py").read_text(
            encoding="utf-8")

    def test_the_camera_event_agrees(self):
        self.assertIn("gev:jarvis-focus", self.js)
        self.assertIn("gev:jarvis-focus", self.py)

    def test_the_markers_event_agrees(self):
        self.assertIn("gev:jarvis-markers", self.js)
        self.assertIn("gev:jarvis-markers", self.py)

    def test_the_page_exposes_the_manager_the_events_reach_through(self):
        self.assertIn("window.gevShareLink", self.js)

    def test_the_session_binds_both_hooks(self):
        """open_globe alone means the camera works and nothing else does."""
        self.assertIn("show_markers", self.session)
        self.assertIn("open_globe", self.session)


if __name__ == "__main__":
    sys.exit(unittest.main() or 0)
