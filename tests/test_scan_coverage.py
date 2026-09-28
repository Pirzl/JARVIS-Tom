"""An empty result is only worth something if you know what was looked for.

This is the argument from `usestrix/strix`'s `report/coverage.py`, whose
docstring says it better than it can be repeated: a list of findings cannot
answer the question Thomas will actually ask, which is "what did you check?"

Without an answer, a clean scan and a scan that crashed halfway are the same
document. Both show zero findings. One of them means the site was fine and the
other means the tool fell over, and nothing in the output says which. That is
not a cosmetic problem in a compliance context -- it is the difference between
evidence of a clean bill of health and no evidence at all.

So every scan produces a coverage record: which of the checks Deep Eye knows how
to run were actually run, which were skipped and why, and whether the run
finished or was cut short. A check that was not run is not a pass, and the
record says so in those words.

Three properties make the record worth trusting, and each is tested here:

  * it is derived from the same source as the checks themselves, so a check
    added to the scanner appears in coverage without a second list to forget
    to update
  * an unfinished run is marked as unfinished. `complete: false` is the single
    most important field in the file, because it is what stops a truncated
    scan being filed as a clean one
  * a gap is a gap. A risk class with no coverage appears in `gaps` rather than
    being quietly absent, because an absent category reads as "not a problem"

Strix is Apache-2.0. The shape of the record is ours.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.scan_coverage import (  # noqa: E402
    ALL_CHECKS,
    RISK_CLASSES,
    CoverageRecord,
    build_coverage,
    check_names_in,
)


class TestTheCheckListIsTheScannerOwn(unittest.TestCase):
    """One source of truth, or coverage drifts from what Deep Eye can do."""

    def test_it_found_the_vendored_checks(self):
        self.assertGreaterEqual(len(ALL_CHECKS), 20,
                                "the vendored scanner has 23; finding fewer "
                                "means the extraction broke")

    def test_the_known_checks_are_present(self):
        for name in ("sql_injection", "xss", "ssrf", "lfi", "rfi", "ssti",
                     "xxe", "csrf", "jwt_vulnerabilities", "security_headers"):
            self.assertIn(name, ALL_CHECKS)

    def test_there_are_no_duplicates(self):
        self.assertEqual(len(ALL_CHECKS), len(set(ALL_CHECKS)))

    def test_every_check_belongs_to_a_risk_class(self):
        """RISK_CLASSES maps a class name to the checks inside it, so the
        membership test has to look at the values. Checking the keys -- which
        is what the first version of this test did -- found every check
        'unmapped' and would have sent anyone fixing the module to the wrong
        dictionary entirely."""
        mapped = {c for checks in RISK_CLASSES.values() for c in checks}
        unmapped = [c for c in ALL_CHECKS if c not in mapped]
        self.assertEqual(unmapped, [],
                         f"checks with no risk class: {unmapped}")

    def test_no_check_appears_in_two_classes(self):
        """A check in two classes makes the class view lie about both."""
        seen, doubled = set(), []
        for checks in RISK_CLASSES.values():
            for c in checks:
                if c in seen:
                    doubled.append(c)
                seen.add(c)
        self.assertEqual(doubled, [])

    def test_every_risk_class_has_at_least_one_check(self):
        empty = [r for r, checks in RISK_CLASSES.items() if not checks]
        self.assertEqual(empty, [], f"risk classes with no checks: {empty}")


class TestACleanScanIsOnlyCleanIfItRan(unittest.TestCase):
    """The distinction the whole file exists for."""

    def test_a_partial_run_that_exited_cleanly_is_not_complete(self):
        """The dangerous case, and the reason this module exists.

        A scanner that exits 0 having run four of twenty-three checks looks
        exactly like a healthy scan from the outside: process finished, some
        output, no findings. Calling it complete hands Thomas a clean bill of
        health for a scan that never looked at authentication, SSRF or file
        inclusion.

        The check names are read out of the scanner's prose, so a check that
        ran quietly is indistinguishable from one that never ran. That
        uncertainty has to resolve towards "I do not know", never towards "you
        are clean" -- hence full coverage is required, not a non-empty list.
        """
        rec = build_coverage(ran=["sql_injection", "xss", "ssrf", "csrf"],
                             findings=[], finished=True)
        self.assertFalse(rec.complete,
                         "exiting cleanly is not the same as checking "
                         "everything")
        self.assertIn("4 of 23", rec.finish_reason)
        self.assertIn("does not mean the site is clean", rec.headline())

    def test_only_full_coverage_is_complete(self):
        for ran, expected in ((ALL_CHECKS, True),
                              (ALL_CHECKS[:-1], False),
                              (ALL_CHECKS[:1], False)):
            rec = build_coverage(ran=list(ran), findings=[])
            self.assertEqual(rec.complete, expected,
                             f"{len(ran)} of {len(ALL_CHECKS)} checks")

    def test_a_finished_scan_with_no_findings_is_complete(self):
        rec = build_coverage(ran=ALL_CHECKS, findings=[])
        self.assertTrue(rec.complete)
        self.assertEqual(rec.covered, len(ALL_CHECKS))

    def test_a_scan_that_was_cancelled_is_not_complete(self):
        rec = build_coverage(ran=["sql_injection", "xss"], findings=[],
                             finished=False)
        self.assertFalse(rec.complete,
                         "a truncated scan must never read as a clean one")

    def test_a_partial_run_lists_what_it_did_not_do(self):
        rec = build_coverage(ran=["sql_injection"], findings=[])
        self.assertIn("xss", rec.skipped)
        self.assertIn("ssrf", rec.skipped)
        self.assertNotIn("sql_injection", rec.skipped)

    def test_nothing_ran_means_nothing_is_covered(self):
        """And a run that covered nothing is not a complete run.

        The first version asserted the opposite, and was wrong: a scan that
        reached `wait()` with zero checks having run has produced no evidence
        about the site at all. Reporting it as complete would hand Thomas a
        clean bill of health for a scan that checked nothing -- the exact
        failure this module exists to prevent.
        """
        rec = build_coverage(ran=[], findings=[])
        self.assertEqual(rec.covered, 0)
        self.assertEqual(len(rec.skipped), len(ALL_CHECKS))
        self.assertFalse(rec.complete,
                         "a run that checked nothing is not a finished run")
        self.assertIn("does not mean the site is clean",
                      rec.headline())

    def test_an_unknown_check_name_is_rejected_not_ignored(self):
        """A typo in a check name would silently reduce coverage.

        Recording `sql-injecton` as ran while the real `sql_injection` shows
        as skipped is worse than an error: the record would say the wrong
        thing with total confidence.
        """
        with self.assertRaises(ValueError):
            build_coverage(ran=["sql-injecton"], findings=[])

    def test_an_unknown_name_cannot_be_counted_as_coverage(self):
        """Found by mutation: the raise above is not what makes this safe.

        Replacing it with a `continue` left the suite green, because nothing
        else observed the consequence. What matters is not that we raise, it
        is that an unrecognised name never becomes coverage -- so that is what
        this asserts, in a form that holds whether or not the guard raises.
        """
        try:
            rec = build_coverage(ran=["sql-injecton"], findings=[])
        except ValueError:
            return
        self.assertEqual(rec.covered, 0,
                         "an unknown name is not a check and cannot be covered")
        self.assertIn("sql_injection", rec.skipped)


class TestGapsAreNamedNotHidden(unittest.TestCase):
    """A category that is missing must be visible, not merely absent."""

    def test_a_risk_class_with_no_coverage_appears_in_gaps(self):
        rec = build_coverage(ran=["sql_injection", "xss"], findings=[])
        self.assertTrue(rec.gaps)
        gap_classes = {g["risk_class"] for g in rec.gaps}
        self.assertIn("authentication", gap_classes)

    def test_a_gap_says_which_checks_were_missing(self):
        rec = build_coverage(ran=["sql_injection"], findings=[])
        gap = next(g for g in rec.gaps if g["risk_class"] == "authentication")
        self.assertIn("jwt_vulnerabilities", gap["missing_checks"])

    def test_a_fully_covered_scan_has_no_gaps(self):
        rec = build_coverage(ran=ALL_CHECKS, findings=[])
        self.assertEqual(rec.gaps, [])

    def test_a_gap_is_not_a_finding(self):
        """Coverage describes what was looked for, never what was found.

        Mixing the two would let a reader scan the report, see a risk class
        listed under `gaps`, and conclude there is a problem there.
        """
        rec = build_coverage(ran=["sql_injection"], findings=[])
        self.assertEqual(getattr(rec, "findings", []), [])


class TestTheRecordSurvivesTheReport(unittest.TestCase):
    def test_it_serialises_to_json_ready_plain_types(self):
        rec = build_coverage(ran=ALL_CHECKS[:3], findings=[])
        data = rec.as_dict()
        self.assertIsInstance(data, dict)
        for value in data.values():
            self.assertIsInstance(
                value, (dict, list, str, int, float, bool, type(None)),
                f"not JSON-serialisable: {value!r}")

    def test_it_names_the_target_and_the_time(self):
        rec = build_coverage(ran=ALL_CHECKS, findings=[], target="example.com")
        self.assertEqual(rec.target, "example.com")
        self.assertTrue(rec.started_at)

    def test_a_junk_check_entry_is_skipped_not_fatal(self):
        """Names come from a subprocess's output in some code paths."""
        rec = build_coverage(ran=["sql_injection", None, 42, ""], findings=[])
        self.assertIn("sql_injection", rec.ran)
        self.assertIn("xss", rec.skipped)

    def test_duplicates_do_not_inflate_coverage(self):
        """The count and the list both, since the list is what gets reported.

        Asserting only `covered` left the dedupe in the list untested:
        dropping the `not in cleaned` guard changed the list and nothing
        failed.
        """
        rec = build_coverage(ran=["sql_injection", "sql_injection"], findings=[])
        self.assertEqual(rec.covered, 1)
        self.assertEqual(rec.ran.count("sql_injection"), 1,
                         "a repeated check in the report is a wrong report")


if __name__ == "__main__":
    unittest.main()
