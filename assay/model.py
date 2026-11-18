"""Core data model: cases, verdicts, issues.

Vocabulary:
  case      — a user journey: start state + actions (steps or a goal) + must/should items
  item      — one check inside must/should; decided by code or by a judge
  evidence  — everything a run recorded about a case; grading reads only evidence
  verdict   — PASS / FAIL / INCONCLUSIVE (the environment broke, not the product)
  issue     — a finding with rule, dimension and severity (SonarQube-style), from a failed
              case or from an always-on sensor rule (errors, a11y, perf, layout, ...)
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"
VERDICTS = (PASS, FAIL, INCONCLUSIVE)

# Item outcome
OK, NOK, UNDECIDED, SKIPPED = "pass", "fail", "undecided", "skipped"

SEVERITIES = ("blocker", "critical", "major", "minor", "info")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}  # lower = worse

# Quality dimensions (the "software qualities" of a running app)
DIMENSIONS = {
    "functional": "Functional",
    "reliability": "Reliability",
    "performance": "Performance",
    "accessibility": "Accessibility",
    "ux": "UX & Mobile",
    "security": "Security",
    "ai": "AI quality",
}

# Who fixes a non-PASS (shown in the report so nobody chases the wrong owner)
OWNER_PRODUCT, OWNER_CASE, OWNER_ENV, OWNER_JUDGE = "product", "case", "environment", "judge"


@dataclass
class Case:
    id: str
    title: str = ""
    severity: str = "major"          # issue severity when the case FAILs
    dimension: str = "functional"
    tags: list[str] = field(default_factory=list)
    as_account: str | None = None
    viewport: str | None = None
    setup: list[dict] = field(default_factory=list)
    expect_before: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    goal: str | None = None
    must: list[dict] = field(default_factory=list)
    should: list[dict] = field(default_factory=list)
    promise: str | None = None       # what the product promises, in a person's words; shown instead of the title
    critical: bool = False           # always runs, whatever the selection for a pull request
    ai: bool = False                 # exercises an AI feature: what `assay bench` repeats by default
    source: str = ""                 # file the case came from
    group: str = ""                  # `group:` as written, else the file it came from (cases/social/feed.yaml → social/feed)
    raw: dict = field(default_factory=dict)

    @staticmethod
    def from_dict(d: dict, source: str = "", group: str = "") -> "Case":
        """`group`: the default group, the file's path inside its suite without the extension (load_cases passes it);
        without one it comes from `source` (group_of). `row`: the dataset row a case of `each:` was made from."""
        known = {"id", "title", "severity", "dimension", "tags", "as", "viewport", "setup",
                 "expect_before", "steps", "goal", "must", "should", "promise", "critical", "ai"}
        unknown = set(d) - known - {"notes", "source", "group", "row"}
        if unknown:
            raise ValueError(f"case {d.get('id')!r}: unknown fields {sorted(unknown)}")
        if not d.get("id"):
            raise ValueError(f"case without id in {source}")
        if not d.get("steps") and not d.get("goal"):
            raise ValueError(f"case {d['id']!r}: needs `steps` or `goal`")
        sev = d.get("severity", "major")
        if sev not in SEVERITIES:
            raise ValueError(f"case {d['id']!r}: severity must be one of {SEVERITIES}")
        if d.get("group") is not None and (not isinstance(d["group"], str) or not d["group"].strip()):
            raise ValueError(f"case {d['id']!r}: `group` must be a name")
        return Case(
            id=d["id"], title=d.get("title", ""), severity=sev,
            dimension=d.get("dimension", "functional"), tags=list(d.get("tags", [])),
            as_account=d.get("as"), viewport=d.get("viewport"), setup=list(d.get("setup", [])),
            expect_before=dict(d.get("expect_before", {})), steps=list(d.get("steps", [])),
            goal=d.get("goal"), must=list(d.get("must", [])), should=list(d.get("should", [])),
            promise=d.get("promise"), critical=bool(d.get("critical")), ai=bool(d.get("ai")), source=source,
            group=(d.get("group") or group or group_of(source)).strip(), raw=d,
        )

    def entry(self) -> dict:
        """The journey as the dataset lists it: identity and its promise, with the checks as written (not expanded),
        so a later analysis can tell whether a change weakened them."""
        return {"id": self.id, "title": self.title, "promise": self.promise, "critical": self.critical, "ai": self.ai,
                "tags": self.tags, "group": self.group, "source": self.source, "must": self.must, "should": self.should,
                "journey": {k: v for k, v in (("as", self.as_account), ("viewport", self.viewport), ("setup", self.setup),
                                              ("expect_before", self.expect_before), ("steps", self.steps), ("goal", self.goal)) if v}}


def group_of(source: str) -> str:
    """The group of a journey that names none, from its file: cases/feed.yaml → feed, cases/social/feed.yaml →
    social/feed (the suite folder and the extension dropped). Also for dataset entries recorded before groups."""
    parts = (source or "").split("/")
    return "/".join(parts[1:] if len(parts) > 1 else parts).rsplit(".", 1)[0]


@dataclass
class ItemResult:
    kind: str                 # check name, e.g. "state", "judge", "no_errors"
    spec: Any                 # the item as written in the case
    level: str                # must | should
    status: str               # pass | fail | undecided | skipped
    detail: str = ""
    by: str = "code"          # code | judge
    dimension: str = "functional"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CaseResult:
    case_id: str
    title: str
    verdict: str
    cause: str = ""           # machine cause: setup_failed, precondition_mismatch, step_failed, ...
    owner: str = ""           # who fixes: product / case / environment / judge
    summary: str = ""
    items: list[ItemResult] = field(default_factory=list)
    score: float = 0.0        # passed items / decided items (must+should)
    duration_s: float = 0.0
    severity: str = "major"
    dimension: str = "functional"
    healed: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["items"] = [i.to_dict() for i in self.items]
        return d


@dataclass
class Issue:
    rule: str                 # e.g. REL-001, A11Y-color-contrast, CASE-FAIL
    title: str
    dimension: str
    severity: str
    where: str                # route pattern or case id
    message: str = ""
    evidence: str = ""        # relative path to screenshot / evidence
    count: int = 1
    new: bool = True
    sources: list[str] = field(default_factory=list)  # case ids / scan pages that hit it
    key: str | None = None    # identity override; None = derived from the message

    @property
    def fingerprint(self) -> str:
        ident = self.key if self.key is not None else _normalise_msg(self.message)
        key = json.dumps([self.rule, self.where, ident], ensure_ascii=False)
        return hashlib.sha1(key.encode()).hexdigest()[:12]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fingerprint"] = self.fingerprint
        return d


def _normalise_msg(msg: str) -> str:
    """Strip volatile parts (numbers, hashes, ports) so the same issue keeps its fingerprint."""
    import re
    msg = re.sub(r"https?://[^/\s]+", "", msg or "")
    msg = re.sub(r"[0-9a-f]{8,}", "#", msg)
    msg = re.sub(r"\d+", "N", msg)
    return msg[:200]


def now_iso() -> str:
    """Local time with its UTC offset: runs are made on CI runners (UTC) and read on servers and laptops elsewhere."""
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")
