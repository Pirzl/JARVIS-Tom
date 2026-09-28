"""A finding has to argue against itself before it is allowed to be serious.

Phase 1 gave findings a verdict: no evidence, no verification, and severity
capped by where the claim came from. That stopped the LLM inventing criticals,
but it left a quieter problem. An unverified `medium` is still in the report, and
the reader has no way to tell a real one from a scanner heuristic that fired on
a login form because it contained the word "id". A list of such findings is
noise, and noise is what makes people stop reading security reports.

So every finding now has to carry its own case against itself, which is the
idea taken from `usestrix/strix`'s `strix/skills/analysis/counterevidence.md`:
a `counterevidence` field that is mandatory and must be more than a shrug, a
`confidence` of high/medium/low with a rationale required for anything below
high, and `severity_change_conditions` naming what would move the severity.

The rule that makes it bite is the one from Strix's prompt: severity is scored
on the impact a proof of concept actually demonstrated. Reachability, missing
authentication and scanner labels do not by themselves justify a high rating.
That is not a style preference -- it is the most common way a scanner inflates
a finding, and it is why a report full of highs gets ignored.

Strix is Apache-2.0, so the field names and the rules are taken freely; the
implementation here is ours and does not import anything from it.

The tests are written as the failures they prevent. A finding that cannot
argue against itself must not reach `high`, and must not be allowed to claim
confidence without a reason.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.confidence_gate import (  # noqa: E402
    ConfidenceGate,
    CONFIDENCE_VALUES,
    EMPTY_COUNTEREVIDENCE,
    adjudicate_severity,
)

REAL_EVIDENCE = [{"type": "response",
                  "detail": "HTTP 500 body: You have an error in your SQL syntax"}]


def finding(**kw):
    base = {
        "title": "SQL Injection in search",
        "severity": "high",
        "verified": True,
        "provenance": "tool",
        "evidence": list(REAL_EVIDENCE),
    }
    base.update(kw)
    return base


class TestCounterevidenceIsMandatory(unittest.TestCase):
    """The whole point: a finding must look for the reason it is wrong."""

    def test_a_finding_with_no_counterevidence_cannot_be_high(self):
        result = ConfidenceGate().review(finding())
        self.assertNotEqual(result.severity, "high")

    def test_a_shrug_is_not_counterevidence(self):
        """'none', 'n/a', '-' and the empty string are all ways of not
        looking, and accepting them would make the field decorative."""
        # `impact_demonstrated=True` isolates this rule from the impact rule.
        # Without it the finding is capped at medium anyway, so the test would
        # pass even with the counterevidence cap deleted -- which is exactly
        # what happened when this was first written.
        for shrug in EMPTY_COUNTEREVIDENCE:
            result = ConfidenceGate().review(
                finding(counterevidence=shrug), impact_demonstrated=True)
            self.assertNotEqual(result.severity, "high",
                                f"'{shrug!r}' must not count as counterevidence")

    def test_the_shrug_rule_holds_when_impact_is_proven(self):
        """The two rules are independent, and both must hold on their own.

        Found by mutation: disabling the counterevidence cap changed nothing
        in the suite, because every other test was already capped at medium by
        the impact rule. A cap that is never the binding constraint is a cap
        nobody is testing.
        """
        result = ConfidenceGate().review(
            finding(counterevidence="none"), impact_demonstrated=True)
        self.assertEqual(result.severity, "medium")
        self.assertFalse(result.has_counterevidence)

    def test_real_counterevidence_keeps_the_severity(self):
        """Counterevidence alone is not enough -- impact has to be shown.

        The first version of this test asserted `high` with no
        `impact_demonstrated`, and the gate correctly refused. That is the
        design working: a scanner that argues against itself but proves
        nothing is still at most a medium.
        """
        result = ConfidenceGate().review(finding(
            counterevidence=(
                "The endpoint is a search box backed by the same indexed "
                "table the public site uses; the error may be reachable only "
                "for this account, and no write was demonstrated.")),
            impact_demonstrated=True)
        self.assertEqual(result.severity, "high")
    def test_real_counterevidence_without_demonstrated_impact_still_caps(self):
        result = ConfidenceGate().review(finding(
            counterevidence=("A WAF in front may be rewriting the response "
                             "rather than the database erroring")))
        self.assertNotEqual(result.severity, "high")

    def test_the_shrug_list_is_explicit(self):
        """An open-ended set of excuses cannot be enumerated in a test."""
        self.assertIn("none", EMPTY_COUNTEREVIDENCE)
        self.assertIn("", EMPTY_COUNTEREVIDENCE)
        self.assertIn("n/a", EMPTY_COUNTEREVIDENCE)


class TestConfidenceNeedsAReason(unittest.TestCase):
    def test_low_confidence_without_a_rationale_is_rejected(self):
        result = ConfidenceGate().review(finding(
            counterevidence="might be a false positive from a WAF banner",
            confidence="low"))
        self.assertTrue(result.needs_rationale,
                        "low confidence without a reason is just a shrug")

    def test_a_rationale_satisfies_it(self):
        result = ConfidenceGate().review(finding(
            counterevidence="might be a false positive from a WAF banner",
            confidence="low",
            confidence_rationale="no SQL syntax appears in the body"))
        self.assertFalse(result.needs_rationale)

    def test_high_confidence_needs_no_rationale(self):
        result = ConfidenceGate().review(finding(
            counterevidence="input is reflected verbatim, no encoding applied",
            confidence="high"))
        self.assertFalse(result.needs_rationale)

    def test_an_unknown_confidence_does_not_pass_silently(self):
        result = ConfidenceGate().review(finding(
            counterevidence="some doubt", confidence="very high"))
        self.assertTrue(result.needs_rationale)

    def test_an_unknown_confidence_cannot_carry_a_high_rating(self):
        """`very high` is not a stronger claim than `high`; it is an unusable
        one, and it must earn nothing.

        Isolation matters here too: the counterevidence is real and the impact
        is demonstrated, so the only thing that can lower this finding is the
        unrecognised confidence. Found by mutation -- with the impact rule
        doing the work, the confidence cap could be deleted and the suite
        stayed green.
        """
        result = adjudicate_severity(finding(
            counterevidence="the error body is the application's own, not a "
                            "WAF page, and it leaks the query text",
            confidence="very high"),
            impact_demonstrated=True)
        self.assertNotEqual(result.severity, "high",
                            "an unrecognised confidence must not be credited")

    def test_a_recognised_high_confidence_still_works(self):
        """The rule above must not become a blanket demotion."""
        result = adjudicate_severity(finding(
            counterevidence="the error body is the application's own, not a "
                            "WAF page, and it leaks the query text",
            confidence="high"),
            impact_demonstrated=True)
        self.assertEqual(result.severity, "high")

    def test_a_stated_confidence_actually_lowers_the_severity(self):
        """A finding that admits it is not sure may not keep a high rating.

        Third cap found untested by mutation: `medium` and `low` confidence
        each cap the severity, and neither cap had a test of its own -- the
        suite only ever checked that an unknown value was penalised.
        """
        for stated, allowed in (("medium", "high"), ("low", "medium")):
            result = adjudicate_severity(finding(
                counterevidence="the error body is the application's own and "
                                "leaks the query text",
                confidence=stated,
                confidence_rationale="one sample, no second endpoint tested"),
                impact_demonstrated=True)
            self.assertEqual(result.severity, allowed,
                             f"confidence {stated!r} should cap at {allowed!r}")

    def test_the_allowed_values_are_closed(self):
        self.assertEqual(set(CONFIDENCE_VALUES), {"high", "medium", "low"})


class TestSeverityIsScoredOnDemonstratedImpact(unittest.TestCase):
    """Strix's rule: score what the PoC showed, not what is theoretically true.

    These are the three excuses that inflate a finding, taken from how scanners
    actually behave rather than from any one tool.
    """

    def test_reachability_alone_does_not_make_it_high(self):
        result = adjudicate_severity(
            finding(counterevidence="no authentication is required, which is "
                                   "itself the concern"),
            impact_demonstrated=False)
        self.assertNotEqual(result.severity, "high")

    def test_missing_authentication_alone_does_not_make_it_high(self):
        result = adjudicate_severity(
            finding(counterevidence="the endpoint is unauthenticated"),
            impact_demonstrated=False)
        self.assertNotEqual(result.severity, "high")

    def test_a_scanner_label_alone_does_not_make_it_high(self):
        result = adjudicate_severity(
            finding(counterevidence="nuclei tagged this template critical"),
            impact_demonstrated=False)
        self.assertNotEqual(result.severity, "high")

    def test_demonstrated_impact_keeps_the_severity(self):
        result = adjudicate_severity(
            finding(counterevidence="no control bypassed; the error leaks the "
                                   "full query text"),
            impact_demonstrated=True)
        self.assertEqual(result.severity, "high")


class TestSeverityChangeConditionsAreRecorded(unittest.TestCase):
    def test_what_would_change_the_severity_is_surfaced(self):
        result = ConfidenceGate().review(finding(
            counterevidence="the error may come from a WAF, not the database",
            severity_change_conditions=(
                "confirmed if the same body appears with the WAF disabled")))
        self.assertIn("severity_change_conditions", result.fields)

    def test_a_missing_conditions_field_is_not_fatal(self):
        """It is a prompt for the scanner, not a gate. Refusing findings over
        a missing nicety would make the report worse, not better."""
        result = ConfidenceGate().review(
            finding(counterevidence="some real doubt here"))
        self.assertIn("severity", result.fields)


class TestTheReviewSurvivesJunk(unittest.TestCase):
    def test_a_bare_finding(self):
        result = ConfidenceGate().review({})
        self.assertIsInstance(result.severity, str)

    def test_none(self):
        self.assertIsInstance(ConfidenceGate().review(None).severity, str)

    def test_counterevidence_of_the_wrong_type(self):
        for junk in (42, [], {}, True):
            result = ConfidenceGate().review(finding(counterevidence=junk))
            self.assertIsInstance(result.severity, str)

    def test_the_input_is_not_mutated(self):
        original = finding(counterevidence="real doubt", severity="high")
        ConfidenceGate().review(original)
        self.assertEqual(original["severity"], "high",
                         "review must not edit the caller's finding")


if __name__ == "__main__":
    unittest.main()
