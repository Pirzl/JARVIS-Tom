"""A finding has to argue against itself before it is allowed to be serious.

Phase 1 decided what was true: no evidence, no verification, severity capped by
provenance. That stopped the model inventing criticals, and it left a quieter
problem. An unverified `medium` is still in the report, and a reader cannot tell
it from a heuristic that fired because a login form contained the word "id". A
list of those is noise, and noise is how people learn to skip security
reports.

This is the second half of the same argument, and it comes from
`usestrix/strix` -- Apache-2.0, so the field names and the rules are taken
freely, though nothing is imported from it.

The idea is that a finding carries its own case against itself. Three fields,
all of them answers a scanner should be forced to give rather than a report
author's opinion:

  counterevidence
    The strongest argument *against* this finding, after actively looking for
    one. Mandatory, and "none" does not count. The way to tell this from
    decoration is the shrug list: a scanner that emits "n/a" has not looked,
    and accepting that would make the field theatre.

  confidence
    high, medium or low, with a rationale required for anything below high.
    Low confidence without a reason is a shrug in a different column.

  severity_change_conditions
    What specific evidence would raise or lower this. Its value is that it
    turns a finding into a question someone can answer, rather than a verdict
    someone has to trust.

And one rule that does most of the work, from Strix's system prompt: severity
is scored on the impact a proof of concept actually demonstrated. Reachability,
missing authentication and a scanner's own label do not by themselves justify a
high rating. That is the most common way a scanner inflates a finding, and the
reason a report full of highs gets ignored by the person who has to fix it.

The three excuses are named in `INFLATION_EXCUSES` rather than being detected
by pattern-matching, because the honest test is whether the PoC demonstrated
impact -- not whether a string contains the word "unauthenticated". A scanner
that finds a real unauthenticated SQL injection must not be talked out of
reporting it; what the rule suppresses is the *rating*, not the finding.

Nothing here discards a finding. A finding with no counterevidence still
appears in the report, with a lower severity and a visible reason why. Silently
dropping it would be the same class of dishonesty as inflating it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# The only confidence values. Anything else is treated as unearned, because a
# value we do not recognise cannot have been reasoned about.
CONFIDENCE_VALUES = ("high", "medium", "low")

# Ways of not looking. "none" is here on purpose: the field exists precisely to
# make the model argue with itself, and the answer it wants to give is "none".
EMPTY_COUNTEREVIDENCE = frozenset({
    "", "none", "n/a", "na", "-", "--", "unknown", "null", "nothing",
    "no", "not applicable", "no counterevidence", "no counter evidence",
    "n/a - checked", "sin evidencia", "ninguna", "nada",
})

_SEVERITY_ORDER = ["info", "low", "medium", "high", "critical"]
_SEVERITY_RANK = {name: i for i, name in enumerate(_SEVERITY_ORDER)}

# Confidence is allowed to lower a severity, never to raise it. A finding that
# says "I am not sure" must not be able to promote itself either.
CONFIDENCE_CEILING = {
    "high": _SEVERITY_RANK["critical"],
    "medium": _SEVERITY_RANK["high"],
    "low": _SEVERITY_RANK["medium"],
}

# Phrases that, on their own, argue a finding is severe without having
# demonstrated any impact. Recorded so the report can show the reader what was
# discounted and why -- a silent downgrade is a lie by omission.
INFLATION_EXCUSES = (
    "unauthenticated",
    "unauthenticated endpoint",
    "no authentication required",
    "reachable without auth",
    "publicly reachable",
    "tagged critical",
    "scanner reported",
    "critical template",
    "theoretically",
    "could lead to",
    "potentially leads to",
)


def severity_rank(severity: Any) -> int:
    return _SEVERITY_RANK.get(str(severity or "").strip().lower(), 0)


def _is_real_counterevidence(value: Any) -> bool:
    """Whether the finding actually argued against itself.

    A shrug is a short phrase in the shrug list, or any text too short to be an
    argument. Length is the test that generalises: a real argument is a
    sentence, and every way of saying "I did not look" is short.
    """
    if not isinstance(value, str):
        return False
    text = value.strip().lower().rstrip(".!")
    if text in EMPTY_COUNTEREVIDENCE:
        return False
    # Fewer than ~25 characters cannot contain an argument, whatever it says.
    return len(value.strip()) >= 25


@dataclass
class ReviewResult:
    """The reviewed finding, with the reasoning attached."""

    severity: str
    asserted: str = ""
    confidence: str = ""
    has_counterevidence: bool = False
    needs_rationale: bool = False
    reasons: list = field(default_factory=list)
    discounted: list = field(default_factory=list)
    fields: dict = field(default_factory=dict)

    @property
    def severity_rank(self) -> int:
        return severity_rank(self.severity)


def _confidence_of(finding: Mapping[str, Any]) -> str:
    value = str(finding.get("confidence", "") or "").strip().lower()
    return value if value in CONFIDENCE_VALUES else ""


def adjudicate_severity(finding: Any,
                        impact_demonstrated: bool = False) -> ReviewResult:
    """Score a finding on what was shown, not on what is theoretically true.

    `impact_demonstrated` is the honest question: did a proof of concept
    actually show the consequence, or did the scanner only show that the input
    reached something interesting? Only the first justifies a high rating.

    The three discounts Strix's prompt warns about -- reachability, missing
    authentication, a scanner's own label -- are applied together, and only
    when nothing was demonstrated. A scanner that proves an unauthenticated
    injection has demonstrated the impact and keeps the severity; what the rule
    removes is the rating that comes from the label alone.
    """
    if not isinstance(finding, Mapping):
        return ReviewResult(severity="info", asserted="",
                            reasons=["finding is not a mapping"])

    asserted = str(finding.get("severity", "") or "unknown")
    reasons: list[str] = []
    discounted: list[str] = []
    severity = asserted

    counter = finding.get("counterevidence")
    real_counter = _is_real_counterevidence(counter)
    if not real_counter:
        reasons.append("no counterevidence was offered")

    confidence = _confidence_of(finding)
    raw_confidence = str(finding.get("confidence", "") or "").strip().lower()
    rationale = finding.get("confidence_rationale")
    has_rationale = isinstance(rationale, str) and len(rationale.strip()) >= 15
    needs_rationale = bool(raw_confidence) and confidence != "high" \
        and not has_rationale
    if needs_rationale:
        if not confidence:
            # An unrecognised value is not a shrug, it is a claim we cannot
            # reason about. Treating it as "no confidence" would let
            # "very high" through as though it were unstated, which is how an
            # inflated value gets a free pass.
            reasons.append(
                f"confidence {raw_confidence!r} is not one of "
                f"{list(CONFIDENCE_VALUES)} and cannot be credited")
        else:
            reasons.append(
                f"confidence {confidence!r} stated without a rationale")
    # Rank the finding against each ceiling, lowest wins.
    rank = severity_rank(asserted)
    ceiling = _SEVERITY_RANK["critical"]
    if raw_confidence and not confidence:
        # An unrecognised value earns nothing, and earns it silently: "very
        # high" is not a stronger claim than "high", it is an unusable one.
        ceiling = min(ceiling, CONFIDENCE_CEILING["low"])
    if not real_counter:
        ceiling = min(ceiling, _SEVERITY_RANK["medium"])
        reasons.append(
            "severity capped at medium: a finding that cannot argue against "
            "itself is not evidence of a serious one")
    if confidence:
        ceiling = min(ceiling, CONFIDENCE_CEILING[confidence])
    if not impact_demonstrated and rank > _SEVERITY_RANK["medium"]:
        ceiling = min(ceiling, _SEVERITY_RANK["medium"])
        discounted.append("no impact was demonstrated by a proof of concept")
        reasons.append(
            "severity capped at medium: reachability, missing authentication "
            "and scanner labels do not by themselves justify a higher rating")

    if rank > ceiling:
        severity = _SEVERITY_ORDER[ceiling]

    return ReviewResult(
        severity=severity,
        asserted=asserted,
        confidence=confidence,
        has_counterevidence=real_counter,
        needs_rationale=needs_rationale,
        reasons=reasons,
        discounted=discounted,
    )


class ConfidenceGate:
    """Apply the self-argument rules to a finding. Stateless and reusable."""

    def review(self, finding: Any, impact_demonstrated: bool = False) -> ReviewResult:
        result = adjudicate_severity(finding, impact_demonstrated)
        out: dict = {}
        if isinstance(finding, Mapping):
            out = dict(finding)
        out.update({
            "severity": result.severity,
            "asserted_severity": result.asserted,
            "confidence": result.confidence,
            "has_counterevidence": result.has_counterevidence,
            "needs_rationale": result.needs_rationale,
            "review_reasons": list(result.reasons),
            "discounted": list(result.discounted),
        })
        if finding.get("severity_change_conditions") if isinstance(
                finding, Mapping) else None:
            out["severity_change_conditions"] = finding["severity_change_conditions"]
        result.fields = out
        return result

    def review_all(self, findings, impact_demonstrated=None) -> list:
        """Review a list, deciding impact per finding where it is stated.

        A batch must not have one finding's demonstrated impact applied to all
        of them, so `impact_demonstrated` may be a callable taking the finding.
        """
        out = []
        for f in findings or []:
            shown = bool(impact_demonstrated(f)) if callable(impact_demonstrated) \
                else bool(impact_demonstrated)
            out.append(self.review(f, shown))
        return out
