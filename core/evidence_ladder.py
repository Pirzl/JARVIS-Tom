"""How each finding was established, stated as a rung on a ladder.

The two gates so far answer two yes/no questions. Is this real -- the evidence
gate. Is it serious -- the confidence gate. Neither says *how* it was
established, so a heuristic that matched a payload string against a response
and a scanner that watched a database hand back someone else's rows both
arrive as "verified, high" and are read the same way. The reader has no way to
spend attention on the one that matters, which is part of why reports with
forty findings get skimmed and the critical one gets missed.

The ladder grades establishment rather than severity:

  reported     a scanner asserted it; nothing was observed
  observed     a response was captured and it differs from the baseline
  confirmed    it was reproduced, or the consequence was shown rather than
               inferred
  disproved    it was checked and did not hold

`disproved` is the rung usually left out, and it is the one that makes the
others worth reading. Deleting what was checked and cleared is how a tool
acquires a reputation for finding real things. A scanner that remembers its
misses can be trusted about its hits; one that only reports successes cannot
be, and nothing in a list of successes tells you which kind you are looking
at.

Nothing here deletes or rewrites. The ladder is read-only over what the gates
already decided, because a tool that hides its misses cannot be audited, and
because the moment a report starts omitting things is the moment it stops
being evidence. Every rung is reported, and every rung below `confirmed` says
what evidence would raise it -- a claim the reader can check instead of trust.

The distinction that matters most is `observed` against `confirmed`. A
400-versus-200 response difference is observation: real, and reproducible, but
it shows that a request did something, not what it achieved. `confirmed` needs
repetition or a shown consequence. Phase 1's `verified` maps to `observed` and
no higher, on purpose: it means a response was seen, and it says nothing about
what that response proved.

Shapes after `usestrix/strix` (Apache-2.0) and `elder-plinius/t3mp3st`
(AGPL-3.0, ideas only). Implementation is ours.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

# Ascending. `disproved` sits at the end as a terminal state rather than a rung
# to climb: it is where a check ended, and a finding can leave it only by being
# re-checked.
RUNG_ORDER = ("reported", "observed", "confirmed", "disproved")

_SEVERITY_ORDER = ("info", "low", "medium", "high", "critical")
_SEVERITY_RANK = {name: i for i, name in enumerate(_SEVERITY_ORDER)}


def _severity_key(entry: dict) -> tuple:
    """Sort by severity, worst first, using the ranking not the alphabet.

    The first version sorted on the severity *string*, which orders
    alphabetically: `info`, `low`, `medium` came out in that order, so a report
    led with the least severe finding. The list a reader works down has to
    start with the thing most likely to be a real problem, and a sort bug
    silently inverts that.
    """
    rank = _SEVERITY_RANK.get(entry.get("severity", "").lower(), -1)
    # Unknown severities sort with the worst, not the best: an unreadable
    # severity is a reason to look, not a reason to bury.
    return (-rank if rank >= 0 else -len(_SEVERITY_ORDER),)

LADDER_VERSION = 1

# What a reader would have to see for the next rung up. Phrased as evidence
# rather than as effort, because "try harder" is not a testable claim.
_NEXT_STEP = {
    "reported": (
        "A captured response showing the behaviour differs from a baseline "
        "request to the same endpoint."
    ),
    "observed": (
        "The same request reproduced more than once, or the consequence shown "
        "directly rather than inferred from a status code."
    ),
    "confirmed": "",
    "disproved": (
        "The check failing in a way that cannot be explained by the input "
        "being rejected before reaching the vulnerable code."
    ),
}


def _is_true(finding: Mapping[str, Any], *names: str) -> bool:
    for name in names:
        value = finding.get(name)
        if isinstance(value, bool) and value:
            return True
        # A count of two or more is a reproduction even if no boolean was set.
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and value >= 2:
            return True
    return False


def _has_observed_evidence(finding: Mapping[str, Any]) -> bool:
    raw = finding.get("evidence")
    if not isinstance(raw, (list, tuple)):
        return False
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("type", "")).strip().lower()
        if kind in {"response", "request", "output", "log", "command", "file"}:
            for key, value in item.items():
                if key == "type":
                    continue
                if isinstance(value, str) and value.strip():
                    return True
    return False


def rung_of(finding: Any) -> str:
    """Which rung this finding has reached.

    `disproved` is checked first and wins outright. A finding that was checked
    and cleared is not sitting one rung below `confirmed` waiting for more
    evidence -- its state is settled, and reporting it as merely unconfirmed
    would invite someone to "look again" at a lead already tested.

    Unknown values never escape. A finding from a newer scanner, or one whose
    provenance was corrupted, lands on the lowest rung rather than carrying a
    value nothing can compare -- being pessimistic about establishment is the
    only safe direction.
    """
    if not isinstance(finding, Mapping):
        return "reported"

    if _is_true(finding, "disproved", "refuted", "ruled_out"):
        return "disproved"

    # `confirmed` needs two things that are easy to conflate: evidence that
    # something happened, and proof of what it achieved. The first version
    # wrote this as one boolean expression and had a precedence slip that let a
    # plain captured response satisfy the second half on its own -- so a
    # single 500 body was filed as `confirmed`, which is exactly the over-trust
    # the ladder exists to prevent. Written out as separate steps instead, with
    # each one named, so a future edit cannot quietly re-merge them.
    has_evidence = _has_observed_evidence(finding)
    consequence_shown = _is_true(
        finding, "impact_demonstrated", "poc_executed", "poc_built")
    reproduced = _is_true(
        finding, "reproduced", "reproductions", "attempts_succeeded")

    if has_evidence and (consequence_shown or reproduced):
        return "confirmed"

    if has_evidence or finding.get("verified") is True:
        return "observed"

    return "reported"


def what_would_raise(finding: Any) -> str:
    """The evidence that would move this finding up one rung.

    Empty for `confirmed`, which has nothing left to prove. For a disproved
    finding the text is about re-opening it rather than raising it, because
    that is the question a reader actually has about a closed lead.
    """
    rung = rung_of(finding)
    if rung == "confirmed":
        return ""
    if rung == "disproved":
        return _NEXT_STEP["disproved"]
    return _NEXT_STEP[rung]


def _entry(finding: Mapping[str, Any]) -> dict:
    """One finding, reduced to what the reader needs to judge it."""
    rung = rung_of(finding)
    entry = {
        "title": str(finding.get("title", "") or "untitled finding"),
        "severity": str(finding.get("severity", "") or "unknown"),
        "rung": rung,
        "what_would_confirm": what_would_raise(finding),
    }
    if finding.get("asserted_severity") and \
            str(finding["asserted_severity"]) != entry["severity"]:
        entry["asserted_severity"] = str(finding["asserted_severity"])
    if finding.get("confidence"):
        entry["confidence"] = str(finding["confidence"])
    return entry


def build_honesty_block(findings: Iterable[Any] = (),
                        coverage: Mapping[str, Any] | None = None) -> dict:
    """The block that goes at the top of a report, before any findings.

    It answers, in order: what did this scan cover, what was found that is not
    established, what was checked and cleared, and what reached confirmation.
    Findings that are not established come first on purpose. A report that
    leads with its criticals and buries the caveats teaches the reader to stop
    reading at the first page.

    The headline is the one sentence that must never overclaim. With partial
    coverage it says so in the same words the voice uses, because those are the
    words Thomas will act on.
    """
    entries = []
    disproved = []
    confirmed = []
    for finding in findings or ():
        entry = _entry(finding) if isinstance(finding, Mapping) else {
            "title": "unparseable finding", "severity": "unknown",
            "rung": "reported", "what_would_confirm": _NEXT_STEP["reported"],
        }
        rung = entry["rung"]
        if rung == "disproved":
            disproved.append(entry)
        elif rung == "confirmed":
            confirmed.append(entry)
        else:
            entries.append(entry)

    complete = bool(coverage.get("complete")) if isinstance(coverage, Mapping) \
        else None
    covered = coverage.get("covered") if isinstance(coverage, Mapping) else None
    total = coverage.get("total_available") if isinstance(coverage, Mapping) \
        else None

    if coverage is None:
        headline = (f"{len(entries)} finding(s) not yet established; "
                    f"scan coverage was not recorded, so a clean result "
                    f"cannot be claimed.")
    elif complete:
        headline = (f"All {total} checks ran. "
                    f"{len(entries)} finding(s) are not established, "
                    f"{len(confirmed)} confirmed, {len(disproved)} checked "
                    f"and cleared.")
    else:
        headline = (f"Only {covered} of {total} checks ran, so no finding "
                    f"does not mean the site is clean. "
                    f"{len(entries)} finding(s) are not established, "
                    f"{len(confirmed)} confirmed.")

    return {
        "ladder_version": LADDER_VERSION,
        "headline": headline,
        "complete": complete,
        "covered": covered,
        "total_available": total,
        # Sorted worst-first within each group: the thing most likely to be
        # wrong is the thing a reader should look at first.
        "unconfirmed": sorted(entries, key=_severity_key),
        "confirmed": confirmed,
        "disproved": disproved,
        "how_to_read_this": (
            "A finding is 'reported' when a scanner asserted it, 'observed' "
            "when a response was captured, and 'confirmed' only when it was "
            "reproduced or its consequence was shown. Anything below "
            "confirmed is a lead, not a result. 'disproved' means the check "
            "ran and cleared it; those are listed so the coverage of this "
            "scan can be judged."
        ),
    }
