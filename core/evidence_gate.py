"""Whether a finding is allowed to say it is real.

Deep Eye's twenty-two scanners assign severity by type and never look at the
response: a SQL injection is `critical` because `vulnerability_scanner.py:456`
says so. Nothing in the pipeline asks whether the HTTP response actually
contained SQL, so a scanner that guessed wrong and one that found a real
injection produce the same `critical` line in the report. The LLM then writes
prose from that list, and the invented findings inherit the invented severity.

This module is the missing step, and it is deliberately separate from the
scanners so that it can be the only thing allowed to set the verdict. Findings
arrive from a network response and from a language model, both of which can be
wrong, and both of which currently get to grade their own homework.

The rules, and where they come from:

  * A finding cannot be `verified` without at least one piece of evidence
    whose type is in a fixed set and whose content is non-empty. The model's
    own explanation of why something looks wrong is not evidence; if it were,
    an eloquent model would verify its own inventions.
  * Critical and high need evidence of any kind, not just real tool output --
    an unverified critical is still worth reading, it just is not a confirmed
    hole.
  * Severity is capped by where the claim came from. Something asserted with
    nothing behind it is a guess, and a guess is at most `low`. Something
    reasoned from surrounding context is at most `medium`. Only real tool
    output keeps the severity it asserted.
  * Any downgrade is recorded. A report that silently shows `low` where the
    scanner said `critical` is lying by omission, and the reader has no way
    to know to look.
  * Secrets are redacted on the way out. A security report containing a live
    API key is a worse outcome than no report, because it gets forwarded.

Provenance levels, and the reasoning behind the ceilings:

  none     the model simply asserted it, with nothing to check it against
  context  it was inferred from surrounding evidence in the page
  tool     a scanner actually sent a payload and read a response

The idea of capping severity by provenance, and of a gate that strips
self-assigned verification, comes from `elder-plinius/t3mp3st`'s
`src/evidence/gate.ts` and `src/evidence/index.ts`. That project is AGPL-3.0.
None of its code is copied here: copyright protects expression, not ideas, and
a gate that copied forty lines of TypeScript would oblige the whole of JARVIS
to be relicensed. This was written from the described behaviour. If that
distinction ever stops being comfortable, the honest move is to write it from
a written specification without the original in view -- which is how it was
written in the first place.

t3mp3st's README also demonstrates the failure this guards against in a
different form: its scope control is a prompt asking the model to behave. Our
promise that only Thomas's own sites get scanned is enforced in
`normalize_target` and confirmed per scan; it is not a sentence in a prompt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# The closed set of evidence that means something outside the model was
# actually observed. `model_reasoning` is absent on purpose: it is precisely
# the type a model would reach for to justify its own finding.
EVIDENCE_TYPES = frozenset({
    "response",     # what the target actually sent back
    "request",      # what was actually sent
    "output",       # stdout of a tool
    "command",      # the command that produced the output
    "log",          # scanner log
    "file",         # a file read off disk
})

# Ranking used to compare severities and ceilings. Ordered, not numeric, so
# that an unknown severity can be placed without pretending to know it.
_SEVERITY_ORDER = ["info", "low", "medium", "high", "critical"]
_SEVERITY_RANK = {name: i for i, name in enumerate(_SEVERITY_ORDER)}

# How high a claim may be ranked depending on where it came from.
PROVENANCE_CEILING = {
    "none": _SEVERITY_RANK["low"],
    "context": _SEVERITY_RANK["medium"],
    "tool": _SEVERITY_RANK["critical"],
}

# Escalation needs evidence of any recognised kind; confirmation needs this
# much of it. One is the floor: a confirmed finding is one thing observed, not
# a pattern inferred from many.
MINIMUM_EVIDENCE_FOR_VERIFIED = 1

# Severities where "no evidence at all" is itself disqualifying.
_NEEDS_EVIDENCE_TO_ASSERT = {"critical", "high"}

_SECRET_PATTERNS = (
    # AWS access key id
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # JWT
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    # GitHub / Slack / Stripe style tokens: a word boundary plus length is
    # what keeps these from eating ordinary prose.
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"),
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    # Private key blocks
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"),
)

# userinfo in a URL: scheme://user:password@host
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)"
                              r"(?P<user>[^:/@\s]+):(?P<pw>[^@/\s]+)@")

REDACTION = "[REDACTED]"


def severity_rank(severity: Any) -> int:
    """Where a severity sits, with unknown values treated as the bottom.

    An unrecognised severity must not be allowed to claim a high rank by
    being unparseable, and it must not crash a report either.
    """
    return _SEVERITY_RANK.get(str(severity or "").strip().lower(), 0)


def _evidence_items(finding: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The usable evidence blocks, ignoring anything of the wrong shape.

    Findings arrive from parsed JSON, so `evidence` can be a string, a number,
    a list of strings, or absent. All of those mean the same thing here: no
    evidence was provided.
    """
    raw = finding.get("evidence") if isinstance(finding, Mapping) else None
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for item in raw:
        if isinstance(item, Mapping):
            out.append(item)
    return out


def _has_real_evidence(items: Iterable[Mapping[str, Any]]) -> bool:
    for item in items:
        etype = str(item.get("type", "")).strip().lower()
        if etype not in EVIDENCE_TYPES:
            continue
        detail = item.get("detail")
        if isinstance(detail, str) and detail.strip():
            return True
        # A block may carry the payload in another field; accept any
        # non-empty string content as the substance of the evidence.
        for key, value in item.items():
            if key == "type":
                continue
            if isinstance(value, str) and value.strip():
                return True
    return False


def _has_any_evidence(items: Iterable[Mapping[str, Any]]) -> bool:
    """Any recognised evidence, even from a source we do not fully trust."""
    for item in items:
        if str(item.get("type", "")).strip().lower() in EVIDENCE_TYPES:
            if any(isinstance(v, str) and v.strip()
                   for k, v in item.items() if k != "type"):
                return True
    return False


def _capped_severity(finding: Mapping[str, Any]) -> tuple[str, str]:
    """The severity this finding is allowed, and the reason it was lowered.

    Returns the original too, so the caller can show "asserted X, allowed Y"
    rather than pretending the scanner never said `critical`.
    """
    asserted = str(finding.get("severity", "") or "unknown")
    provenance = str(finding.get("provenance", "none") or "none").strip().lower()
    ceiling = PROVENANCE_CEILING.get(provenance)
    if ceiling is None:
        # An unrecognised provenance is treated as the weakest one, not
        # trusted into the strongest bucket.
        ceiling = PROVENANCE_CEILING["none"]
    if severity_rank(asserted) <= ceiling:
        return asserted, ""
    allowed = _SEVERITY_ORDER[ceiling]
    return allowed, (f"severity {asserted!r} reduced to {allowed!r}: "
                     f"provenance is {provenance!r}")


@dataclass
class GateResult:
    """The verdict, and everything needed to explain it in the report."""

    verified: bool
    severity: str
    asserted: str = ""
    provenance: str = "none"
    reasons: list = field(default_factory=list)
    evidence_count: int = 0
    redacted: bool = False

    @property
    def severity_rank(self) -> int:
        return severity_rank(self.severity)

    def as_finding_fields(self) -> dict:
        """The fields to write back onto a finding, dropping self-assignment.

        `verified` is always present and always derived. A caller cannot pass
        `verified=True` in and have it survive, which is the whole point.
        """
        return {
            "verified": self.verified,
            "severity": self.severity,
            "asserted_severity": self.asserted,
            "provenance": self.provenance,
            "verify_reasons": list(self.reasons),
            "redacted": self.redacted,
        }


class EvidenceGate:
    """The only thing allowed to decide whether a finding is real.

    One instance is stateless and safe to share; it holds no verdict of its
    own, so a second call cannot inherit the first call's answer. That matters
    because re-checking a stored finding has to re-derive the verdict from the
    evidence that still exists, not remember that it was verified once.
    """

    def check(self, finding: Any) -> GateResult:
        if not isinstance(finding, Mapping):
            return GateResult(
                verified=False, severity="info", asserted="",
                provenance="none",
                reasons=["finding is not a mapping"])

        raw_evidence = finding.get("evidence")
        items = _evidence_items(finding)
        real = _has_real_evidence(items)
        any_evidence = _has_any_evidence(items)

        reasons: list[str] = []
        if not items:
            reasons.append("no evidence blocks were provided")
        else:
            unrecognised = [
                str(i.get("type", "?"))
                for i in items
                if str(i.get("type", "")).strip().lower() not in EVIDENCE_TYPES
            ]
            if unrecognised and not any_evidence:
                reasons.append(
                    "evidence is not observed tool output: "
                    + ", ".join(sorted(set(unrecognised))))
            elif not real:
                reasons.append("evidence blocks are empty")

        severity, downgrade = _capped_severity(finding)
        if downgrade:
            reasons.append(downgrade)

        asserted = str(finding.get("severity", "") or "")
        if asserted.strip().lower() in _NEEDS_EVIDENCE_TO_ASSERT and not any_evidence:
            reasons.append(
                f"{asserted} severity asserted with no evidence at all")

        verified = real and len(items) >= MINIMUM_EVIDENCE_FOR_VERIFIED
        if not verified and not reasons:
            # Defensive: never silently return an unverified finding with no
            # explanation, whatever combination produced it.
            reasons.append("evidence did not meet the threshold for verification")

        redacted = bool(
            raw_evidence and _contains_secret(_safe_str(raw_evidence)))

        return GateResult(
            verified=verified,
            severity=severity,
            asserted=asserted,
            provenance=str(finding.get("provenance", "none") or "none"),
            reasons=reasons,
            evidence_count=len(items),
            redacted=redacted,
        )

    def apply(self, finding: Any) -> dict:
        """Check a finding and return a redacted copy with the verdict on it.

        The input is never mutated: findings are shared between the scanner,
        the report writer and the voice summary, and a gate that edited them
        in place would leave a half-updated object behind if it raised.
        """
        result = self.check(finding)
        if isinstance(finding, Mapping):
            out = dict(finding)
        else:
            out = {"value": finding}
        out.update(result.as_finding_fields())
        if "evidence" in out:
            out["evidence"] = redact(out["evidence"])
        if "description" in out and isinstance(out["description"], str):
            out["description"] = redact_text(out["description"])
        return out

    def apply_all(self, findings: Iterable[Any]) -> list:
        return [self.apply(f) for f in findings]


def _safe_str(value: Any) -> str:
    try:
        return str(value)
    except Exception:                                    # noqa: BLE001
        return ""


def _contains_secret(text: str) -> bool:
    return bool(_URL_CREDENTIALS.search(text)) or any(
        p.search(text) for p in _SECRET_PATTERNS)


def redact_text(text: str) -> str:
    """Remove credentials from a string, saying that we did.

    Returns a new string; the caller keeps its own copy. A silent redaction
    reads as though the secret was never there, and a reader would then trust
    a finding whose evidence is now empty.
    """
    if not isinstance(text, str) or not text:
        return text
    out = _URL_CREDENTIALS.sub(
        lambda m: f"{m.group('scheme')}{m.group('user')}:{REDACTION}@", text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTION, out)
    return out


def redact(value: Any, _seen: set | None = None) -> Any:
    """Walk a structure and remove credentials from every string in it.

    Recursive because evidence arrives as parsed JSON of unknown shape, and
    cycle-aware because a self-referential dict is cheap to produce from
    attacker-influenced data and would otherwise recurse until the stack ran
    out. Keys are left alone: a field named `token` is not itself a secret.
    """
    if _seen is None:
        _seen = set()
    if isinstance(value, (str, bytes)):
        return redact_text(value.decode("utf-8", "replace")
                           if isinstance(value, bytes) else value)
    if isinstance(value, Mapping):
        if id(value) in _seen:
            return REDACTION
        _seen.add(id(value))
        out = {k: redact(v, _seen) for k, v in value.items()}
        _seen.discard(id(value))
        return out
    if isinstance(value, (list, tuple)):
        if id(value) in _seen:
            return REDACTION
        _seen.add(id(value))
        kind = type(value)
        out = kind(redact(v, _seen) for v in value)
        _seen.discard(id(value))
        return out
    return value
