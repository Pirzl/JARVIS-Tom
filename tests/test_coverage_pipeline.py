"""Coverage has to be on the real path, and honest about what it could not see.

`core/scan_coverage.py` can be perfect and the report still mislead, because
`core/scan_coverage.py` is only consulted if `wait()` asks. The voice summary is
the sharper version of the same risk: Thomas is told "scan finished with no
findings" in plain words, and that sentence is a claim about his website. If
the run died at check four of twenty-three, the sentence is false and he has no
way to know.

So these tests check the wiring and the wording, not the module.

The `_checks_that_ran` guess is the other thing worth testing, and the direction
of its error matters more than its accuracy. Reading check names out of the
scanner's own output is a heuristic. When it cannot tell whether a check ran,
the only safe answer is "not covered" -- a report that overstates what was
missed is a nuisance, and a report that overstates what was checked is a
security problem.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.deep_eye import (  # noqa: E402
    ScanResult,
    _checks_that_ran,
    _write_coverage,
)
from core.scan_coverage import ALL_CHECKS  # noqa: E402


class TestWhatRanIsReadNotAssumed(unittest.TestCase):
    def test_a_check_named_in_the_output_counts_as_ran(self):
        out = "[*] running sql_injection\n[*] running xss\n"
        ran = _checks_that_ran(out)
        self.assertIn("sql_injection", ran)
        self.assertIn("xss", ran)

    def test_a_check_never_mentioned_does_not(self):
        self.assertNotIn("ssrf", _checks_that_ran("[*] running sql_injection\n"))

    def test_silence_means_nothing_ran(self):
        """The dangerous direction: a crash produces no output at all."""
        self.assertEqual(_checks_that_ran(""), [])
        self.assertEqual(_checks_that_ran(None or ""), [])

    def test_it_only_ever_returns_real_checks(self):
        out = "sql_injection xss rm -rf / everything"
        for name in _checks_that_ran(out):
            self.assertIn(name, ALL_CHECKS)

    def test_the_search_is_case_insensitive(self):
        self.assertIn("sql_injection", _checks_that_ran("SQL_Injection done"))


class TestTheCoverageFileIsWritten(unittest.TestCase):
    def _write(self, stdout, finished, findings=()):
        """Write a coverage record into a throwaway tree, read it back.

        Everything happens inside the context manager. The first version read
        the file after the temporary directory had already been removed, so
        `path.exists()` was False and the test failed for a reason that had
        nothing to do with the code it was meant to check.
        """
        import core.deep_eye as de
        with tempfile.TemporaryDirectory() as tmp:
            original = de.VENDOR_DIR
            de.VENDOR_DIR = Path(tmp) / "vendor"
            try:
                path = _write_coverage("example.com", stdout, list(findings),
                                       finished)
                exists = bool(path) and path.exists()
                data = json.loads(path.read_text(encoding="utf-8")) if exists \
                    else None
            finally:
                de.VENDOR_DIR = original
        return exists, data

    def test_it_writes_a_readable_file(self):
        """One check out of twenty-three is a partial run, and says so.

        The first version of this test asserted `complete: True` after writing
        a single check's worth of output, which is the precise mistake the
        module exists to prevent -- a run that barely started, filed as a
        finished scan.
        """
        exists, data = self._write("[*] sql_injection\n", finished=True)
        self.assertTrue(exists, "the coverage file must exist on disk")
        self.assertEqual(data["target"], "example.com")
        self.assertFalse(data["complete"])
        self.assertEqual(data["covered"], 1)

    def test_a_full_run_is_reported_complete(self):
        from core.scan_coverage import ALL_CHECKS
        exists, data = self._write(" ".join(ALL_CHECKS), finished=True)
        self.assertTrue(exists)
        self.assertTrue(data["complete"],
                        "all 23 checks named and the process exited")
        self.assertEqual(data["gaps"], [])

    def test_a_failed_scan_still_writes_a_record(self):
        """This is the case that matters: the run that died must leave a trace
        of what it managed, or 'no findings' becomes meaningless."""
        _, data = self._write("[*] sql_injection\n", finished=False)
        self.assertIsNotNone(data)
        self.assertFalse(data["complete"])
        self.assertIn("sql_injection", data["ran"])
        self.assertTrue(data["gaps"])

    def test_the_note_tells_the_reader_what_complete_means(self):
        _, data = self._write("[*] sql_injection\n", finished=True)
        self.assertIn("not a check that passed", data["note"])


class TestTheVoiceSummaryCannotLie(unittest.TestCase):
    """The sentence Thomas actually hears."""

    def _summary(self, findings=(), coverage=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = None
            if coverage is not None:
                path = Path(tmp) / "coverage.json"
                path.write_text(json.dumps(coverage), encoding="utf-8")
            return ScanResult(target="example.com", returncode=0,
                              findings=list(findings),
                              coverage_path=path).summary()

    def test_a_full_scan_says_so_when_there_is_nothing(self):
        text = self._summary(coverage={"complete": True, "covered": 23,
                                       "total_available": 23})
        self.assertIn("ran all 23", text)
        self.assertNotIn("does not mean the site is clean", text)

    def test_a_partial_scan_says_the_result_does_not_mean_clean(self):
        text = self._summary(coverage={"complete": False, "covered": 4,
                                       "total_available": 23})
        self.assertIn("does not mean the site is clean", text)
        self.assertIn("4 of 23", text)

    def test_no_coverage_file_falls_back_to_the_old_sentence(self):
        """A scan from before this existed still has to say something usable."""
        text = self._summary()
        self.assertIn("no findings", text)
        self.assertNotIn("None", text)

    def test_a_corrupt_coverage_file_does_not_break_the_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coverage.json"
            path.write_text("{not json", encoding="utf-8")
            text = ScanResult(target="example.com", returncode=0,
                              coverage_path=path).summary()
        self.assertIn("no findings", text)

    def test_findings_and_coverage_are_both_reported(self):
        text = self._summary(
            findings=[{"severity": "medium"}, {"severity": "low"}],
            coverage={"complete": True, "covered": 23, "total_available": 23})
        self.assertIn("2 findings", text)
        self.assertIn("1 medium", text)
        self.assertIn("ran all 23", text)


if __name__ == "__main__":
    unittest.main()
