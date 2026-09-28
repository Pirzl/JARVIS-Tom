"""The gate has to be on the real path, not just correct in isolation.

`core/evidence_gate.py` can be perfect and the report still lie, because
`_judge_findings` is the only thing that stands between the scanner's output
and everything downstream. If that call is removed, or moved after the report
is written, every unit test of the gate still passes and nothing catches it.
So these tests take findings shaped like the ones the vendored scanner actually
emits -- `vulnerability_scanner.py` produces `title`, `severity`, `url`,
`payload` and `response`, and no `evidence` key at all -- and check what comes
out the other side.

That last detail is the point. The scanner has no evidence field, so a gate
that only reads `finding["evidence"]` would mark every real finding unverified
and silently destroy the tool's usefulness. Equally, a gate that treated the
scanner's own `severity: critical` as proof would be the bug this whole change
exists to fix. The behaviour has to be in between, and only running it against
the real shape shows which of the two it actually is.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.deep_eye import _judge_findings, _infer_provenance  # noqa: E402


# Shaped after the vendored scanner's real output: severity asserted, no
# evidence key, payload and response present.
FROM_SCANNER = [
    {
        "title": "SQL Injection in search parameter",
        "severity": "critical",
        "url": "https://example.com/search",
        "payload": "' OR 1=1--",
        "response": "HTTP 500: You have an error in your SQL syntax",
    },
    {
        "title": "Missing security header",
        "severity": "medium",
        "url": "https://example.com/",
    },
    {
        "title": "Possible IDOR on /api/user/1",
        "severity": "high",
        "url": "https://example.com/api/user/1",
        "line": 42,
    },
]


class TestProvenanceIsInferredNotAssumed(unittest.TestCase):
    def test_a_scanner_finding_with_payload_and_response_is_context(self):
        self.assertEqual(_infer_provenance(FROM_SCANNER[0]), "context")

    def test_a_finding_with_attached_evidence_is_tool(self):
        f = dict(FROM_SCANNER[0])
        f["evidence"] = [{"type": "response", "detail": "raw body"}]
        self.assertEqual(_infer_provenance(f), "tool")

    def test_a_bare_assertion_is_none(self):
        self.assertEqual(_infer_provenance(FROM_SCANNER[1]), "none")

    def test_a_url_with_a_location_is_context(self):
        self.assertEqual(_infer_provenance(FROM_SCANNER[2]), "context")


class TestTheRealScannerOutputIsJudged(unittest.TestCase):
    """The whole point: the scanner's `critical` does not survive on trust."""

    def setUp(self):
        self.judged = _judge_findings(FROM_SCANNER)

    def test_it_keeps_the_findings(self):
        self.assertEqual(len(self.judged), 3,
                         "judging must not lose findings; a dropped finding "
                         "is a hole nobody is told about")

    def test_the_critical_is_capped_when_it_is_only_inferred(self):
        """Scanner said critical; the evidence is a payload and a response,
        which is inference, so the report is not allowed to call it critical."""
        first = self.judged[0]
        self.assertLessEqual(
            first["severity"].lower(), "medium",
            "a finding inferred from context must not stay critical")
        self.assertEqual(first["asserted_severity"], "critical",
                         "the original claim must remain visible")

    def test_the_downgrade_is_explained(self):
        first = self.judged[0]
        self.assertTrue(first["verify_reasons"],
                        "a downgrade with no reason is a silent lie")

    def test_a_bare_assertion_falls_to_low(self):
        second = self.judged[1]
        self.assertEqual(second["severity"].lower(), "low")
        self.assertFalse(second["verified"])

    def test_nothing_is_verified_without_attached_evidence(self):
        """The scanner emits no evidence blocks, so nothing is confirmed.

        This is correct and slightly uncomfortable: the honest answer today is
        that Deep Eye suspects rather than confirms. Wiring the raw response
        through as evidence would make the report say `verified`, which is a
        claim the scanner has not earned.
        """
        for f in self.judged:
            self.assertFalse(f["verified"],
                             f"unearned verification on: {f['title']}")

    def test_evidence_text_becomes_real_evidence(self):
        """A scanner that reports its own evidence block should be believed."""
        f = _judge_findings([{
            "title": "Reflected XSS",
            "severity": "high",
            "provenance": "tool",
            "evidence_text": "HTTP 200 body contains <script>alert(1)</script>",
        }])[0]
        self.assertTrue(f["verified"], f.get("verify_reasons"))

    def test_an_explicit_tool_provenance_keeps_its_severity(self):
        f = _judge_findings([{
            "title": "Reflected XSS",
            "severity": "high",
            "provenance": "tool",
            "evidence": [{"type": "response",
                          "detail": "body echoed the payload verbatim"}],
        }])[0]
        self.assertEqual(f["severity"].lower(), "high")
        self.assertTrue(f["verified"])


class TestJunkDoesNotBreakTheScan(unittest.TestCase):
    """Findings arrive from a subprocess's stdout, parsed as JSON."""

    def test_a_non_dict_is_dropped_not_crashed(self):
        self.assertEqual(_judge_findings(["string", 42, None]), [])

    def test_an_empty_list(self):
        self.assertEqual(_judge_findings([]), [])
        self.assertEqual(_judge_findings(None), [])

    def test_the_input_is_not_mutated(self):
        """The scanner's own list is shared with the report writer."""
        original = [dict(FROM_SCANNER[0])]
        _judge_findings(original)
        self.assertNotIn("verified", original[0],
                         "judging must not edit the caller's findings")

    def test_a_secret_in_the_response_is_stripped(self):
        f = _judge_findings([{
            "title": "Exposed credential",
            "severity": "high",
            "provenance": "tool",
            "evidence": [{"type": "response",
                          "detail": "key AKIAIOSFODNN7EXAMPLE in page source"}],
        }])[0]
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", str(f))


    def test_the_gate_is_called_on_the_path_findings_take(self):
        """The wiring itself, not the helper.

        `_judge_findings` can be perfect and the pipeline can still be wrong.
        The earlier version of this file tested the helper directly and passed
        happily with the call removed from `wait()` -- the report was reading
        ungated findings and nothing went red. This asserts the call site, so
        deleting it is a failing test rather than a silent regression.

        Found by trying it: this assertion was written after that test passed
        against a pipeline with the gate removed.
        """
        src = (BASE_DIR / "core" / "deep_eye.py").read_text(encoding="utf-8")
        wait_src = _source_of(src, "wait")
        self.assertIn("_judge_findings", wait_src,
                      "wait() must gate findings before building the result")
        # And it must be after parsing, or it would be judging nothing.
        self.assertLess(wait_src.index("_parse_json_findings"),
                        wait_src.index("_judge_findings"))


def _source_of(src: str, name: str) -> str:
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


if __name__ == "__main__":
    unittest.main()
