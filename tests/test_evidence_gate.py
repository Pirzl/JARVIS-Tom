"""A finding must be unable to promote itself.

Deep Eye's scanners assign severity by type: a SQL injection is `critical`
because the code says so, at vulnerability_scanner.py:456, and nothing ever
asks whether the response actually contained SQL. The LLM then reads that list
and writes a report from it, and a `critical` with no evidence behind it looks
identical to a real one. With 22 verifiers running, that is not a rare
mistake -- it is the normal state of the output, and it is the reason a
security report from this tool cannot be trusted at face value.

Three ideas from three projects converge on the same fix, and this test holds
them in place:

  * t3mp3st `src/evidence/gate.ts` -- a finding cannot be `verified` without
    evidence of a type that came from real tool output, and escalating to
    critical/high requires evidence of any kind. Severity is also degraded by
    provenance: nothing -> low, context only -> medium, tool output -> as
    asserted. The idea is free to take; the 47 lines of TypeScript are AGPL
    and are not copied.
  * t3mp3st `src/evidence/index.ts:175` -- `updateFinding` deletes any
    attempt to self-certify. Verification state belongs to the gate alone, not
    to the caller, not to the model, not to the API. This is what stops the
    obvious bypass, and it is why the gate has to be a separate module that
    findings pass through rather than a field something can set.
  * HexStrike's README lesson 5 -- scope control there is *persuasion*, a
    prompt asking the LLM to behave. Our guarantee that only Thomas's own
    sites get scanned has to be an assertion in Python. The test below
    requires the evidence types to be an explicit allowlist precisely so a
    model-supplied string can never widen it.

What is deliberately absent: no copying. Every rule here was written from the
described behaviour, not from the source, because AGPL-3.0 would oblige the
whole of JARVIS to be relicensed for forty lines of convenience.

The tests assert behaviour, not the presence of a keyword. A gate that could
be bypassed by constructing a finding with `verified=True` directly is exactly
the failure this file exists to prevent.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.evidence_gate import (  # noqa: E402
    EvidenceGate,
    EVIDENCE_TYPES,
    MINIMUM_EVIDENCE_FOR_VERIFIED,
    PROVENANCE_CEILING,
    redact,
    redact_text,
)


def finding(**kw):
    base = {
        "title": "Possible SQL injection in search",
        "severity": "critical",
        "verified": False,
        "evidence": [],
        "provenance": "tool",
    }
    base.update(kw)
    return base


REAL_EVIDENCE = {
    "type": "response",
    "detail": "HTTP 500 with 'You have an error in your SQL syntax' at /search?q=",
}


class TestTheGateRefusesToLetAFindingLie(unittest.TestCase):
    """The core of it: no evidence, no verification."""

    def test_it_blocks_verification_without_evidence(self):
        result = EvidenceGate().check(finding())
        self.assertFalse(result.verified)
        self.assertTrue(result.reasons,
                        "a blocked finding must say why, or nobody can fix it")

    def test_it_blocks_verification_with_prose_evidence_only(self):
        """The model's own explanation is not evidence.

        This is the bypass that matters: a finding whose "evidence" is a
        sentence the model wrote looks supported to any naive check, and
        inflates the report with inventions.
        """
        result = EvidenceGate().check(finding(evidence=[
            {"type": "model_reasoning", "detail": "the query looks injectable"},
        ]))
        self.assertFalse(result.verified)

    def test_it_blocks_verification_with_empty_evidence_detail(self):
        """An evidence block with no content is a placeholder, not evidence."""
        result = EvidenceGate().check(finding(evidence=[
            {"type": "response", "detail": ""},
        ]))
        self.assertFalse(result.verified)

    def test_it_allows_verification_with_real_tool_output(self):
        result = EvidenceGate().check(finding(evidence=[REAL_EVIDENCE]))
        self.assertTrue(result.verified, result.reasons)
        self.assertEqual(result.reasons, [])

    def test_the_evidence_types_are_a_closed_allowlist(self):
        """A model must not be able to widen the set by naming a new type."""
        self.assertIsInstance(EVIDENCE_TYPES, (set, frozenset))
        self.assertIn("response", EVIDENCE_TYPES)
        self.assertIn("output", EVIDENCE_TYPES)
        for bad in ("model_reasoning", "assumption", "guess", "inference"):
            self.assertNotIn(bad, EVIDENCE_TYPES)


class TestSeverityIsCappedByProvenance(unittest.TestCase):
    """A claim is only as strong as where it came from."""

    def test_a_claim_with_no_provenance_cannot_be_critical(self):
        result = EvidenceGate().check(finding(provenance="none"))
        self.assertLessEqual(result.severity_rank, PROVENANCE_CEILING["none"])

    def test_a_claim_from_context_only_is_capped_below_high(self):
        result = EvidenceGate().check(finding(provenance="context"))
        self.assertLessEqual(result.severity_rank, PROVENANCE_CEILING["context"])

    def test_tool_evidence_keeps_the_asserted_severity(self):
        result = EvidenceGate().check(
            finding(provenance="tool", evidence=[REAL_EVIDENCE]))
        self.assertEqual(result.severity, "critical")

    def test_a_downgrade_is_recorded_not_hidden(self):
        """Silently lowering a severity is how a report starts lying.

        The original claim has to stay visible next to the verdict, otherwise
        the report shows `low` and the reader has no idea the tool thought it
        was critical.
        """
        result = EvidenceGate().check(finding(provenance="none"))
        self.assertNotEqual(result.severity, "critical")
        self.assertTrue(result.asserted or result.reasons,
                        "the original claim or the reason for the change "
                        "must remain visible")


class TestVerificationIsNotSelfAssigned(unittest.TestCase):
    """The obvious bypass: build the finding with verified=True yourself."""

    def test_a_pre_verified_finding_is_stripped_by_the_gate(self):
        result = EvidenceGate().check(finding(verified=True, evidence=[]))
        self.assertFalse(result.verified,
                         "the gate must decide, not the caller")

    def test_updating_a_finding_cannot_keep_a_stale_verification(self):
        """Re-running the gate over a stored finding must not trust its flags.

        A finding saved as verified stays verified forever unless something
        actively re-checks it, so an update that only changes the description
        would carry the old stamp forward.
        """
        gate = EvidenceGate()
        stored = finding(verified=True, evidence=[REAL_EVIDENCE])
        self.assertTrue(gate.check(stored).verified)
        # Evidence disappears -- a rule was removed, a response changed.
        stored["evidence"] = []
        self.assertFalse(gate.check(stored).verified)

    def test_the_gate_ignores_severity_it_did_not_derive(self):
        result = EvidenceGate().check(
            finding(severity="critical", provenance="model"))
        self.assertNotEqual(result.severity, "critical")


class TestSecretsNeverReachTheReport(unittest.TestCase):
    """A report that leaks a token is worse than no report."""

    AWS = "AKIAIOSFODNN7EXAMPLE"
    JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
           ".eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r")

    def test_an_aws_key_is_stripped(self):
        out = redact({"detail": f"found key {self.AWS} in page source"})
        self.assertNotIn(self.AWS, out["detail"])

    def test_a_jwt_is_stripped(self):
        out = redact({"detail": f"cookie contained {self.JWT}"})
        self.assertNotIn(self.JWT, out["detail"])

    def test_a_secret_in_a_url_is_stripped(self):
        out = redact({"detail": "https://user:hunter2@example.com/x"})
        self.assertNotIn("hunter2", out["detail"])

    def test_redaction_says_it_happened(self):
        """A silently redacted report reads as if the secret was never there.

        The reader needs to know evidence was removed, or they will trust a
        finding whose payload is now empty.
        """
        out = redact({"detail": f"found key {self.AWS} here"})
        self.assertTrue(out.get("redacted") or "[REDACTED]" in out["detail"],
                        "redaction must be visible, not silent")

    def test_it_does_not_mutate_the_input(self):
        original = {"detail": f"found key {self.AWS}"}
        redact(original)
        self.assertIn(self.AWS, original["detail"],
                      "redaction must return a new structure")

    def test_text_redaction_leaves_ordinary_prose_alone(self):
        """A gate that eats everything makes every report unreadable."""
        clean = "SQL syntax error near 'AND 1=1' at GET /search"
        self.assertEqual(redact_text(clean), clean)

    def test_redaction_survives_a_cycle(self):
        """Deep Eye walks nested structures; a self-referential dict is cheap
        to build from parsed JSON and must not become infinite recursion."""
        node = {"detail": f"key {self.AWS}"}
        node["self"] = node
        out = redact(node)
        self.assertNotIn(self.AWS, str(out)[:2000])


class TestTheGateSurvivesNastyInput(unittest.TestCase):
    """Findings come from a network response and a language model."""

    def test_a_finding_with_no_fields_at_all(self):
        result = EvidenceGate().check({})
        self.assertFalse(result.verified)

    def test_evidence_of_a_wrong_shape(self):
        result = EvidenceGate().check(finding(evidence=["just a string", 42]))
        self.assertFalse(result.verified,
                         "malformed evidence must not verify anything")

    def test_evidence_of_none(self):
        self.assertFalse(EvidenceGate().check(finding(evidence=None)).verified)

    def test_an_unknown_severity_does_not_crash(self):
        result = EvidenceGate().check(finding(severity="apocalíptico"))
        self.assertIsInstance(result.severity, str)

    def test_a_non_dict_finding(self):
        for junk in ("a string", 42, None, []):
            result = EvidenceGate().check(junk)
            self.assertFalse(result.verified)


class TestTheMinimumIsADocumentedNumber(unittest.TestCase):
    """If the bar moves, the test moves with it -- and says so."""

    def test_at_least_one_piece_is_required(self):
        self.assertGreaterEqual(MINIMUM_EVIDENCE_FOR_VERIFIED, 1)

    def test_the_ceilings_are_ordered(self):
        self.assertLess(PROVENANCE_CEILING["none"],
                        PROVENANCE_CEILING["context"],
                        "a claim with no provenance must rank below one "
                        "with context")
        self.assertLessEqual(PROVENANCE_CEILING["context"],
                             PROVENANCE_CEILING["tool"])


if __name__ == "__main__":
    unittest.main()
