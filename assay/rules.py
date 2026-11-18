"""Rules: turn raw sensor facts and case results into issues (SonarQube-style).

Every rule has an id, a quality dimension and a severity. Issues are deduplicated by fingerprint
(rule + where + normalised message), so the same broken component seen by ten cases is one issue
with ten sources, and a baseline run tells which issues are new.
"""
from __future__ import annotations

import json

import re
from urllib.parse import urlparse

from .model import FAIL, NOK, CaseResult, Issue

SESSION_COOKIE = re.compile(r"(sess|sid|token|auth|jwt|remember)", re.I)
A11Y_SEVERITY = {"critical": "critical", "serious": "major", "moderate": "minor", "minor": "info"}

RULES = {
    "CASE-FAIL": ("Journey failed", "functional"),
    "CASE-SHOULD": ("Journey quality below expectation", "functional"),
    "CASE-HEALED": ("Journey passed only after self-healing — case needs review", "functional"),
    "REL-001": ("Uncaught exception in the page", "reliability"),
    "REL-002": ("Console error", "reliability"),
    "REL-003": ("Server error (5xx) from first-party endpoint", "reliability"),
    "REL-004": ("Client error (4xx) from first-party endpoint", "reliability"),
    "REL-005": ("Request to first-party endpoint failed", "reliability"),
    "UX-001": ("Horizontal overflow: content wider than the screen", "ux"),
    "UX-002": ("Touch target smaller than 24×24 px (WCAG 2.5.8)", "ux"),
    "UX-003": ("Broken image", "ux"),
    "UX-004": ("Blank screen", "ux"),
    "PERF-001": ("Slow Largest Contentful Paint", "performance"),
    "PERF-002": ("Layout shift (CLS)", "performance"),
    "PERF-003": ("Main thread blocked (long task)", "performance"),
    "PERF-004": ("Heavy page (transfer size)", "performance"),
    "PERF-005": ("Slow API response", "performance"),
    "PERF-006": ("Slow interaction (INP)", "performance"),
    "SEC-001": ("Session cookie readable by JavaScript (no HttpOnly)", "security"),
    "SEC-002": ("Auth token stored in localStorage", "security"),
}


ROUTE_PATTERNS: list[str] = []   # from the profile (scan.route_patterns), e.g. "/u/:handle"


def set_route_patterns(patterns: list[str] | None) -> None:
    ROUTE_PATTERNS[:] = list(patterns or [])


def _match_pattern(path: str) -> str | None:
    segs = path.strip("/").split("/")
    for pat in ROUTE_PATTERNS:
        ps = pat.strip("/").split("/")
        if len(ps) == len(segs) and all(a.startswith(":") or a == b for a, b in zip(ps, segs)):
            return pat
    return None


def route_of(url: str) -> str:
    """URL → route pattern: the profile's patterns first ("/u/:handle"), then ids, numbers and hashes collapse to :id.
    A stable route keeps an issue's fingerprint stable between runs, so "new issue" means new."""
    if not url:
        return ""
    p = urlparse(url)
    path = p.path or "/"
    if (m := _match_pattern(path)) and not p.fragment.startswith("/"):
        return m
    if p.fragment.startswith("/"):
        path += "#" + p.fragment.split("?")[0]
    segs = []
    for s in path.split("/"):
        if re.fullmatch(r"\d+|[0-9a-f]{8,}(-[0-9a-f]{4,})*|[0-9A-Za-z_-]{20,}", s) or \
                (len(s) >= 6 and re.search(r"\d", s) and re.fullmatch(r"[\w-]+", s)):
            segs.append(":id")
        else:
            segs.append(s)
    return "/".join(segs) or "/"


def _issue(rule: str, where: str, message: str, severity: str, source: str, evidence: str = "",
           title: str | None = None) -> Issue:
    t, dim = RULES.get(rule, (title or rule, "functional"))
    return Issue(rule=rule, title=title or t, dimension=dim, severity=severity, where=where, message=message,
                 evidence=evidence, sources=[source])


def sensor_issues(ev: dict, source: str, evidence_path: str = "", page_mode: bool = False) -> list[Issue]:
    """Issues from one evidence record (a case or a scanned page)."""
    out: list[Issue] = []
    s = ev.get("sensors") or {}
    audit = ev.get("audit") or {}
    route = route_of(ev.get("final_url") or audit.get("url") or "")
    vw = (audit.get("viewport") or [9999])[0]
    shot = evidence_path

    for e in s.get("page_errors", []):
        out.append(_issue("REL-001", "", e["text"], "critical", source, shot))
    for c in s.get("console", []):
        if c["type"] == "error":
            out.append(_issue("REL-002", "", c["text"], "major", source, shot))
    for r in s.get("bad_responses", []):
        if not r.get("first_party") or r.get("offline"):
            continue
        where = f"{r['method']} {route_of(r['url'])}"
        if r["status"] >= 500:
            out.append(_issue("REL-003", where, f"HTTP {r['status']}", "critical", source, shot))
        elif r["status"] not in (401,):
            out.append(_issue("REL-004", where, f"HTTP {r['status']}", "minor", source, shot))
    for r in s.get("failed_requests", []):
        if r.get("first_party") and not r.get("offline"):
            out.append(_issue("REL-005", f"{r['method']} {route_of(r['url'])}", r.get("failure", ""), "major", source, shot))
    for r in s.get("slow_responses", []):
        out.append(_issue("PERF-005", f"{r['method']} {route_of(r['url'])}", f"{r['ms']} ms", "minor", source, shot))

    if audit and "error" not in audit:
        if audit.get("blank"):
            out.append(_issue("UX-004", route, "page rendered no visible content", "critical", source, shot))
        if audit.get("overflowX"):
            it = _issue("UX-001", route, "; ".join(audit.get("overflowEls", [])[:3]) or "page is wider than viewport",
                        "major", source, shot)
            it.key = ""
            out.append(it)
        if (vw < 600 or audit.get("coarsePointer")) and audit.get("smallTargets"):
            n = len(audit["smallTargets"])
            it = _issue("UX-002", route, f"{n}+ targets, e.g. " + "; ".join(audit["smallTargets"][:3]), "minor", source, shot)
            it.key = ""
            out.append(it)
        for src in audit.get("brokenImages", [])[:5]:
            out.append(_issue("UX-003", route, src, "minor", source, shot))
        vit = audit.get("vitals") or {}
        if page_mode and vit.get("lcp"):
            if vit["lcp"] > 4000:
                out.append(_issue("PERF-001", route, f"LCP {round(vit['lcp'])} ms (> 4000)", "major", source, shot))
            elif vit["lcp"] > 2500:
                out.append(_issue("PERF-001", route, f"LCP {round(vit['lcp'])} ms (> 2500)", "minor", source, shot))
        if vit.get("cls", 0) > 0.25:
            out.append(_issue("PERF-002", route, f"CLS {vit['cls']:.2f} (> 0.25)", "major", source, shot))
        elif vit.get("cls", 0) > 0.1:
            out.append(_issue("PERF-002", route, f"CLS {vit['cls']:.2f} (> 0.1)", "minor", source, shot))
        if vit.get("longTasks"):
            worst = max(vit["longTasks"])
            if worst > 500:
                out.append(_issue("PERF-003", route, f"longest task {worst} ms (> 500)", "major", source, shot))
            elif worst > 200:
                out.append(_issue("PERF-003", route, f"longest task {worst} ms (> 200)", "minor", source, shot))
        if vit.get("inp") and vit["inp"] > 500:
            out.append(_issue("PERF-006", route, f"INP {round(vit['inp'])} ms (> 500)", "major", source, shot))
        elif vit.get("inp") and vit["inp"] > 200:
            out.append(_issue("PERF-006", route, f"INP {round(vit['inp'])} ms (> 200)", "minor", source, shot))
        t = audit.get("timing") or {}
        if page_mode and t.get("transferKB", 0) > 5000:
            sev = "major" if t["transferKB"] > 15000 else "minor"
            it = _issue("PERF-004", route, f"{t['transferKB'] / 1024:.1f} MB transferred on load", sev, source, shot)
            it.key = ""
            out.append(it)
        for v in (audit.get("a11y") or {}).get("violations", []):
            sev = A11Y_SEVERITY.get(v.get("impact") or "minor", "info")
            out.append(Issue(rule=f"A11Y-{v['id']}", title=v.get("help", v["id"]), dimension="accessibility",
                             severity=sev, where=route,
                             message=f"{v['nodes']} element(s): " + ", ".join(v.get("targets", [])[:2]),
                             evidence=shot, sources=[source], key=""))
        for k in audit.get("storageTokens", []):
            out.append(_issue("SEC-002", "", f"localStorage[{k!r}] holds a JWT", "minor", source, shot))
    for c in ev.get("cookies", []):
        if SESSION_COOKIE.search(c["name"]) and not c.get("httpOnly"):
            out.append(_issue("SEC-001", "", f"cookie {c['name']!r} without HttpOnly", "major", source, shot))
    return out


def case_issues(res: CaseResult, evidence_path: str) -> list[Issue]:
    out: list[Issue] = []
    if res.verdict == FAIL:
        failed = [i for i in res.items if i.level == "must" and i.status == NOK]
        dim = res.dimension
        if dim == "functional" and failed and failed[0].dimension != "functional":
            dim = failed[0].dimension
        out.append(Issue(rule="CASE-FAIL", title=f"Journey failed: {res.title or res.case_id}", dimension=dim,
                         severity=res.severity, where=f"case:{res.case_id}", message=res.summary[:400],
                         evidence=evidence_path, sources=[res.case_id], key=""))   # same journey failing = same issue
    elif res.healed:
        out.append(_issue("CASE-HEALED", f"case:{res.case_id}", "cached steps no longer match the UI", "minor",
                          res.case_id, evidence_path))
    for i in res.items:
        if i.level == "should" and i.status == NOK:
            out.append(Issue(rule="CASE-SHOULD", title=f"{res.title or res.case_id}: {i.kind}", dimension=i.dimension,
                             severity="minor", where=f"case:{res.case_id}",
                             message=f"{i.kind}: {i.detail}"[:300], evidence=evidence_path, sources=[res.case_id],
                             key=json.dumps(i.spec, sort_keys=True, default=str)[:300]))   # the item, not the judge's wording
    return out


def merge(issues: list[Issue]) -> list[Issue]:
    by: dict[str, Issue] = {}
    for i in issues:
        fp = i.fingerprint
        if fp in by:
            cur = by[fp]
            cur.count += 1
            for s in i.sources:
                if s not in cur.sources:
                    cur.sources.append(s)
            if not cur.evidence and i.evidence:
                cur.evidence = i.evidence
            if i.key == "" and i.message and i.message not in cur.message and len(cur.message) < 600:
                cur.message += " | " + i.message   # same rule on the same screen, different offenders
        else:
            by[fp] = i
    from .model import SEV_RANK
    return sorted(by.values(), key=lambda i: (SEV_RANK[i.severity], i.dimension, -i.count, i.rule))
