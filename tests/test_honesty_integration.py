"""The honesty block has to be on the real path, and has to be heard.

`core/evidence_ladder.py` can be flawless and change nothing, because it is
only consulted if `wait()` asks for it. The wiring test below is the same
shape as the coverage one, and for the same reason: the earlier integration
test proved the helper worked while the call site was absent, which is the
easiest way to ship a gate that gates nothing.

The voice test matters more than usual here, because the honesty block is
mostly made of sentences. A summary that computes the caveats correctly and
then speaks only the counts has kept the code and lost the point.
"""
from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.deep_eye import ScanResult  # noqa: E402
from core.evidence_ladder import build_honesty_block  # noqa: E402


def f(**kw):
    base = {"title": "X", "severity": "medium", "verified": False,
            "provenance": "none", "evidence": []}
    base.update(kw)
    return base


class TestWaitActuallyBuildsTheBlock(unittest.TestCase):
    """Read the source, not the behaviour: the call has to be there."""

    def setUp(self):
        self.tree = ast.parse((BASE_DIR / "core" / "deep_eye.py")
                              .read_text(encoding="utf-8"))
        self.wait = next(
            n for n in ast.walk(self.tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "wait")

    def test_the_honesty_block_is_built_inside_wait(self):
        calls = [c for c in ast.walk(self.wait)
                 if isinstance(c, ast.Call)
                 and getattr(c.func, "id", "") == "build_honesty_block"]
        self.assertTrue(calls,
                        "wait() must build the honesty block; without it the "
                        "ladder is a module nothing reads")

    def test_the_block_is_stored_on_the_result(self):
        src = ast.unparse(self.wait)
        # `ast.unparse` rewraps the multi-line constructor, so match the
        # keyword rather than the exact layout. A substring of the whole call
        # silently stopped matching the moment someone reformatted the
        # assignment, which is a test that breaks on style and says nothing
        # about the wiring.
        self.assertRegex(
            src, r"honesty\s*=\s*honesty",
            "the result must carry the block or the voice and the report "
            "cannot see it")

    def test_the_block_sees_the_gated_findings(self):
        """The block is built from post-gate findings, not the raw ones.

        Ordering is the whole point: a rung computed before the gates describe
        findings the gates then downgrade or clear, so the report would grade
        them as more established than the evidence allows.
        """
        src = ast.unparse(self.wait)
        judge = src.find("_judge_findings")
        block = src.find("build_honesty_block")
        self.assertNotEqual(judge, -1)
        self.assertNotEqual(block, -1)
        self.assertLess(judge, block,
                        "gate first, then grade; the reverse grades "
                        "findings the gate has already corrected")

    def test_the_coverage_is_built_before_the_block(self):
        src = ast.unparse(self.wait)
        self.assertLess(src.find("_write_coverage"),
                        src.find("build_honesty_block"),
                        "the block needs the coverage file to exist first")


class TestTheVoiceLeadsWithTheCaveats(unittest.TestCase):
    def _summary(self, findings=(), honesty=None, coverage=None):
        return ScanResult(target="example.com", returncode=0,
                          findings=list(findings), honesty=honesty,
                          coverage_path=coverage).summary()

    def test_the_headline_is_spoken_first(self):
        honesty = build_honesty_block([], {"complete": False, "covered": 4,
                                           "total_available": 23})
        text = self._summary(honesty=honesty)
        self.assertTrue(text.startswith("Scan of example.com finished with no "
                                        "findings."))
        self.assertIn("does not mean the site is clean", text)

    def test_findings_still_come_through_with_the_caveat(self):
        honesty = build_honesty_block([f(severity="high")],
                                      {"complete": False, "covered": 4,
                                       "total_available": 23})
        text = self._summary(findings=[f(severity="high")], honesty=honesty)
        self.assertIn("1 finding", text)
        self.assertIn("does not mean the site is clean", text)

    def test_it_does_not_speak_two_caveats_at_once(self):
        """A block and a coverage clause are the same warning in two forms.

        Speaking both makes the summary twice as long and gives the impression
        of two independent problems, which is worse than one clearly stated.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"complete": False, "covered": 4,
                                        "total_available": 23}),
                            encoding="utf-8")
            honesty = build_honesty_block([], {"complete": False, "covered": 4,
                                               "total_available": 23})
            text = ScanResult(target="example.com", returncode=0,
                              findings=[], honesty=honesty,
                              coverage_path=path).summary()
        self.assertEqual(text.lower().count("does not mean"), 1)

    def test_a_scan_with_no_still_works(self):
        """Older results, and the path before coverage existed."""
        text = self._summary()
        self.assertIn("no findings", text)


if __name__ == "__main__":
    unittest.main()
