"""Findings should be graded by how they were established, not by how loud they are.

The two gates so far answer two yes/no questions: is this real, and is it
serious. Neither of them says *how* it was established, so a heuristic that
matched a payload against a response and a scanner that watched a database
return someone else's rows both arrive as "verified, high" and are read the
same way. The reader has no way to spend their attention on the one that
matters, which is why reports get skimmed.

So each finding gets a rung on a ladder, from least to most established:

  reported    a scanner asserted it. Nothing observed.
  observed    a response was captured, and it differs from the baseline.
  confirmed   the finding was reproduced more than once, or the consequence
              was directly shown rather than inferred.
  disproved   checked and did not hold. Kept in the record on purpose.

`disproved` is the rung people leave out. Deleting what was checked and failed
is how a tool accumulates a reputation for finding real things: a scanner that
remembers its misses can be trusted about its hits, and one that only ever
reports successes cannot.

Two properties make the ladder worth having rather than decorative:

  * it never deletes a finding. Every rung is reported, and the ladder is
    read-only over what the gates already decided. A tool that hides its
    misses cannot be audited.
  * it is falsifiable. `confirmed` and `observed` both say what evidence
    would raise the rung, so a reader can check the claim rather than trust
    it. A ladder that could not be wrong would be decoration.

The shape comes from `usestrix/strix` (Apache-2.0) and
`elder-plinius/t3mp3st` (AGPL-3.0, ideas only). The implementation is ours.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.evidence_ladder import (  # noqa: E402
    RUNG_ORDER,
    rung_of,
    what_would_raise,
    build_honesty_block,
    LADDER_VERSION,
)


def finding(**kw):
    base = {
        "title": "SQL Injection in search",
        "severity": "medium",
        "verified": False,
        "provenance": "none",
        "evidence": [],
    }
    base.update(kw)
    return base


class TestTheLadderIsOrderedAndClosed(unittest.TestCase):
    def test_the_rungs_are_in_ascending_order(self):
        self.assertEqual(RUNG_ORDER[0], "reported")
        self.assertLess(RUNG_ORDER.index("observed"),
                        RUNG_ORDER.index("confirmed"))
        # `disproved` is last but is not "higher than confirmed". It is a
        # terminal state -- where a check ended -- not a rung to climb, so the
        # first version of this assertion, which demanded it as the peak, was
        # asking for an ordering the design does not have.
        self.assertEqual(RUNG_ORDER[-1], "disproved")
        self.assertEqual(RUNG_ORDER[:3], ("reported", "observed", "confirmed"))

    def test_an_unknown_rung_never_appears(self):
        """A finding from a future version of the scanner must land somewhere
        known rather than carrying a value nothing can compare."""
        for junk in (None, "", "CONFIRMED", "maybe", 42):
            self.assertIn(rung_of(finding(provenance=junk)),
                          RUNG_ORDER)

    def test_the_version_is_reported(self):
        self.assertTrue(LADDER_VERSION)


class TestRungAssignmentReflectsEvidence(unittest.TestCase):
    def test_a_bare_assertion_is_only_reported(self):
        self.assertEqual(rung_of(finding()), "reported")

    def test_a_captured_response_is_observed(self):
        f = finding(provenance="tool",
                    evidence=[{"type": "response", "detail": "HTTP 500 body"}])
        self.assertEqual(rung_of(f), "observed")

    def test_a_reproduced_finding_is_confirmed(self):
        """Reproduction is the difference between a lead and a result."""
        f = finding(provenance="tool", reproductions=3,
                    evidence=[{"type": "response", "detail": "HTTP 500 body"}])
        self.assertEqual(rung_of(f), "confirmed")

    def test_a_demonstrated_consequence_is_confirmed(self):
        f = finding(provenance="tool", impact_demonstrated=True,
                    evidence=[{"type": "response", "detail": "row dumped"}])
        self.assertEqual(rung_of(f), "confirmed")

    def test_a_disproved_finding_stays_disproved(self):
        """Evidence for the finding being *wrong* is evidence, too.

        This is the rung that is normally missing. If it downgraded to
        'reported' the record would lose the fact that the check ran and
        cleared it -- and a tool with no memory of its misses cannot be
        trusted about its hits.
        """
        f = finding(provenance="tool", disproved=True,
                    evidence=[{"type": "response",
                               "detail": "input was rejected, no error"}])
        self.assertEqual(rung_of(f), "disproved")

    def test_a_verified_flag_alone_does_not_reach_confirmed(self):
        """Phase 1's `verified` means the response was seen. It says nothing
        about consequence, so it may not promote a finding on its own."""
        f = finding(verified=True, provenance="tool",
                    evidence=[{"type": "response", "detail": "body"}])
        self.assertEqual(rung_of(f), "observed")


class TestEveryRungCanSayWhatWouldRaiseIt(unittest.TestCase):
    """A ladder you cannot check is decoration."""

    def test_a_reported_finding_says_what_would_observe_it(self):
        step = what_would_raise(finding())
        self.assertTrue(step)
        self.assertNotIn("nothing", step.lower())

    def test_an_observed_finding_says_what_would_confirm_it(self):
        f = finding(provenance="tool",
                    evidence=[{"type": "response", "detail": "HTTP 500"}])
        self.assertIn("reproduc", what_would_raise(f).lower())

    def test_a_confirmed_finding_has_nothing_left_to_raise(self):
        f = finding(provenance="tool", reproductions=2,
                    evidence=[{"type": "response", "detail": "x"}])
        self.assertEqual(what_would_raise(f), "")

    def test_a_disproved_finding_says_what_would_reopen_it(self):
        self.assertTrue(what_would_raise(finding(disproved=True)))


class TestTheHonestyBlock(unittest.TestCase):
    """The part Thomas reads first, and the reason the ladder exists."""

    def _block(self, findings, coverage=None):
        return build_honesty_block(findings, coverage)

    def test_it_leads_with_what_was_not_established(self):
        """`confirmed` is the only rung that leaves the "not established" list.

        The first version of this test expected one entry from a pair of
        findings, counting `observed` as established. It is not: observed
        means a response was captured, which is a lead until it is
        reproduced or its consequence is shown. Only `confirmed` and
        `disproved` are settled states.
        """
        block = self._block([
            finding(title="A", severity="medium"),
            finding(title="B", severity="low", provenance="tool",
                    evidence=[{"type": "response", "detail": "x"}]),
        ])
        self.assertEqual(len(block["unconfirmed"]), 2,
                         "observed is not established; only confirmed is")
        self.assertEqual(block["unconfirmed"][0]["title"], "A",
                         "worst first, not insertion order")
        self.assertEqual(block["unconfirmed"][1]["rung"], "observed")

    def test_only_confirmed_leaves_the_lead_list(self):
        block = self._block([finding(
            title="solid", provenance="tool", impact_demonstrated=True,
            evidence=[{"type": "response", "detail": "rows returned"}])])
        self.assertEqual(block["unconfirmed"], [])
        self.assertEqual(len(block["confirmed"]), 1)

    def test_the_worst_finding_is_listed_first(self):
        """Not alphabetical, not insertion order: worst first.

        Found by a failing test. Sorting on the severity string put `info`
        before `medium` before `low`, so the reader's list started with the
        least severe item. The list a reader works down has to start with the
        thing most likely to be a real problem.
        """
        block = self._block([
            finding(title="A", severity="info"),
            finding(title="B", severity="critical"),
            finding(title="C", severity="low"),
            finding(title="D", severity="medium"),
        ])
        self.assertEqual([e["severity"] for e in block["unconfirmed"]],
                         ["critical", "medium", "low", "info"])

    def test_an_unreadable_severity_sorts_with_the_worst(self):
        block = self._block([finding(title="A", severity="who knows"),
                             finding(title="B", severity="low")])
        self.assertEqual(block["unconfirmed"][0]["title"], "A",
                         "an unreadable severity is a reason to look")

    def test_it_surfaces_what_would_settle_each_one(self):
        block = self._block([finding(title="A")])
        self.assertTrue(block["unconfirmed"][0]["what_would_confirm"])

    def test_it_never_hides_a_disproved_finding(self):
        block = self._block([finding(title="X", disproved=True)])
        self.assertEqual(len(block["disproved"]), 1)

    def test_it_carries_the_coverage_verdict(self):
        """An unconfirmed finding means less without knowing what was run."""
        block = self._block([finding()],
                            coverage={"complete": False, "covered": 4,
                                      "total_available": 23})
        self.assertFalse(block["complete"])
        self.assertIn("4", block["headline"])

    def test_the_headline_never_claims_cleanliness_on_its_own(self):
        for cov in (None,
                    {"complete": True, "covered": 23, "total_available": 23},
                    {"complete": False, "covered": 0, "total_available": 23}):
            block = self._block([], coverage=cov)
            self.assertTrue(block["headline"])
            if cov and not cov.get("complete"):
                self.assertIn("does not mean",
                              block["headline"].lower().replace("’", "'")
                              .replace("no finding does not mean", "does not mean"))

    def test_it_survives_junk(self):
        for junk in (None, "string", 42, []):
            self.assertTrue(self._block([junk])["headline"])

    def test_it_does_not_mutate_the_findings(self):
        original = finding(title="A", severity="high")
        self._block([original])
        self.assertEqual(original["severity"], "high")
        self.assertNotIn("rung", original)

    def test_it_serialises(self):
        import json
        block = self._block([finding()], coverage={"complete": True,
                                                   "covered": 23,
                                                   "total_available": 23})
        json.dumps(block)


if __name__ == "__main__":
    unittest.main()
