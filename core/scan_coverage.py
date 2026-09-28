"""What did this scan actually look at?

Deep Eye can report zero findings today, and the report cannot tell you whether
that means the site was clean or the scanner fell over halfway. Both produce an
empty list. Nothing in the output says which, and in a compliance conversation
that is the difference between evidence and nothing at all.

This module answers the question the findings cannot: of the checks Deep Eye
knows how to run, which ones ran, which did not, and did the scan finish. The
idea is from `usestrix/strix`'s `report/coverage.py` -- Apache-2.0, and this is
our own implementation of it.

`complete: false` is the field that matters most. A scan cut short by a
timeout, a cancel or a crash is not a clean scan, and the record has to say so
in a way nobody can misread.

The check list is parsed out of the vendored scanner rather than written here,
for one reason: a second hand-maintained list is a second thing to forget. Deep
Eye is a launcher for someone else's scanner, so the honest universe of "what
could have been checked" is whatever that scanner defines. If a check is
renamed or added upstream, coverage follows it; if it is copied into this file
instead, coverage starts lying the day the vendor changes.

That means a rename upstream turns every run into an error, which is the
behaviour wanted: an unknown check name is refused rather than recorded, because
recording a typo as "ran" while the real check shows as "skipped" produces a
record that is confidently wrong.

Risk classes group the checks so a reader can ask the question that actually
matters -- was authentication looked at at all -- without reading a list of
twenty-three names. A class with nothing covered is a gap, and gaps are named
in the output rather than left to be inferred from an absence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

VENDOR_SCANNER = (Path(__file__).resolve().parent.parent
                  / "vendor" / "deep-eye" / "core" / "vulnerability_scanner.py")

# Checks are discovered rather than declared. `_check_` is the scanner's own
# naming for its verifiers, and reading them keeps coverage honest when the
# vendor adds one.
_CHECK_DEF = re.compile(r"^\s*def (_check_\w+)\s*\(", re.MULTILINE)


def _discover_checks() -> tuple[str, ...]:
    """Every check the vendored scanner defines, as bare names.

    Returns an empty tuple when the vendor tree is absent, which is a normal
    state: `vendor/` is gitignored, and a checkout without it cannot scan
    anything either. Coverage then reports nothing covered, which is the
    truthful answer rather than a guess at a default list.
    """
    try:
        source = VENDOR_SCANNER.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    found = {m.group(1)[len("_check_"):] for m in _CHECK_DEF.finditer(source)}
    return tuple(sorted(found))


ALL_CHECKS = _discover_checks()

# Grouping is a judgement about what a reader wants to know, not something the
# scanner exposes, so it is written here. Every check must land in exactly one
# class: a check with no class would silently disappear from the class view,
# which is the failure this file exists to prevent.
RISK_CLASSES: dict[str, tuple[str, ...]] = {
    "injection": (
        "sql_injection", "command_injection", "ldap_injection",
        "xml_injection", "ssti", "xxe", "crlf_injection",
        "host_header_injection", "insecure_deserialization",
    ),
    "cross_site": ("xss", "csrf", "open_redirect", "cors"),
    "path_and_file": ("lfi", "rfi", "path_traversal"),
    "server_side_request": ("ssrf",),
    "authentication": (
        "jwt_vulnerabilities", "authentication_bypass",
        "broken_authentication",
    ),
    "data_exposure": (
        "information_disclosure", "sensitive_data_exposure",
    ),
    "transport_and_headers": ("security_headers",),
}


@dataclass
class CoverageRecord:
    """What was looked for, what was skipped, and whether the run finished."""

    target: str = ""
    ran: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    complete: bool = True
    started_at: str = ""
    covered: int = 0
    total_available: int = 0
    finish_reason: str = ""

    @property
    def fraction(self) -> float:
        if not self.total_available:
            return 0.0
        return self.covered / self.total_available

    def headline(self) -> str:
        """One line, for the voice and for the top of the report.

        The wording matters more than it looks: "no findings" and "no findings
        from 23 of 23 checks" are different claims, and only the second is
        evidence. An incomplete run says so in the same words the voice uses,
        because that is the sentence Thomas will hear and the one he will act
        on.
        """
        if not self.total_available:
            return f"No checks available to run against {self.target}."
        if not self.complete:
            return (f"Scan of {self.target} did not cover everything: "
                    f"{self.covered} of {self.total_available} checks ran"
                    f"{' before it stopped' if self.finish_reason else ''}, "
                    f"so no finding does not mean the site is clean.")
        if self.gaps:
            return (f"Scan of {self.target} covered {self.covered} of "
                    f"{self.total_available} checks. "
                    f"{len(self.gaps)} area(s) had no coverage at all.")
        return (f"Scan of {self.target} ran all {self.total_available} checks. "
                f"Zero findings is a meaningful result.")

    def as_dict(self) -> dict:
        """Plain types only, ready for `json.dump`.

        The report writer serialises this straight to disk, and a dataclass with
        a set or a Path in it would fail there rather than here.
        """
        return {
            "target": self.target,
            "started_at": self.started_at,
            "complete": self.complete,
            "finish_reason": self.finish_reason,
            "covered": self.covered,
            "total_available": self.total_available,
            "coverage_fraction": round(self.fraction, 3),
            "ran": list(self.ran),
            "skipped": list(self.skipped),
            "gaps": [dict(g) for g in self.gaps],
            "note": (
                "A check that did not run is not a check that passed. "
                "'complete: false' means this scan was cut short and its "
                "absence of findings is not evidence of anything."
            ),
        }


def check_names_in(source: str) -> tuple[str, ...]:
    """The check names defined in a scanner's source.

    Exposed so the extraction can be tested against a sample rather than only
    against the vendor file, which is not present in every checkout.
    """
    return tuple(sorted({m.group(1)[len("_check_"):]
                         for m in _CHECK_DEF.finditer(source)}))


def build_coverage(ran: Iterable[str],
                   findings: Sequence[Any] = (),
                   *,
                   target: str = "",
                   finished: bool = True,
                   finish_reason: str = "",
                   available: Sequence[str] | None = None) -> CoverageRecord:
    """Record what a run covered.

    `ran` is what the scan says it did, not what we hope it did. Unknown names
    raise rather than being dropped, because a name that is not a check means
    either a typo here or a rename upstream, and in both cases a record that
    quietly omits it is worse than an error. Empty and non-string entries are
    tolerated: some code paths feed names parsed from a subprocess's output.
    """
    universe = tuple(available) if available is not None else ALL_CHECKS
    known = set(universe)

    cleaned: list[str] = []
    for name in ran or ():
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if name not in known:
            raise ValueError(
                f"unknown check {name!r}; the scanner defines "
                f"{len(known)} checks and this is not one of them. A renamed "
                f"check upstream means coverage cannot be reported honestly.")
        if name not in cleaned:
            cleaned.append(name)

    ran_set = set(cleaned)
    skipped = [c for c in universe if c not in ran_set]

    gaps = []
    for risk_class, checks in RISK_CLASSES.items():
        missing = [c for c in checks
                   if c in known and c not in ran_set]
        if missing and len(missing) == len([c for c in checks if c in known]):
            gaps.append({
                "risk_class": risk_class,
                "missing_checks": missing,
                "why_it_matters": _CLASS_WHY.get(risk_class, ""),
            })

    # Two separate things have to be true before a run may be called complete,
    # and conflating them is how a broken scan gets filed as a clean one.
    #
    # `finished` is the process's exit. That it exited says nothing about what
    # it managed to check, and neither does a non-empty `ran` list: which
    # checks ran is read out of the scanner's own prose, so a check that ran
    # quietly is indistinguishable from one that never ran at all. Requiring
    # full coverage is the only honest reading -- and it errs towards saying
    # "I am not sure" rather than towards "you are clean".
    #
    # A run that reaches this point having checked nothing has produced no
    # evidence about the site at all, which is the other case worth naming
    # separately, because it is what a crashed scanner looks like.
    complete = bool(finished) and len(ran_set) == len(universe)
    if not finished:
        reason = finish_reason or "scan ended early"
    elif not cleaned:
        reason = "no checks were executed"
    elif not complete:
        reason = (f"only {len(ran_set)} of {len(universe)} checks could be "
                  f"confirmed as run")
    else:
        reason = ""

    return CoverageRecord(
        target=target,
        ran=cleaned,
        skipped=skipped,
        gaps=gaps,
        complete=complete,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        covered=len(ran_set),
        total_available=len(universe),
        finish_reason=reason,
    )


# Why an uncovered area is worth flagging rather than leaving silent. One
# sentence each, aimed at someone deciding whether to trust the scan.
_CLASS_WHY = {
    "injection": "code execution and data access through unvalidated input",
    "cross_site": "a visitor's browser acting on someone else's behalf",
    "path_and_file": "reading or writing files the site should not expose",
    "server_side_request": "the server being used to reach places it should not",
    "authentication": "who is allowed in, and whether tokens can be forged",
    "data_exposure": "secrets or personal data leaving where they belong",
    "transport_and_headers": "the browser-level protections that limit an attack",
}
