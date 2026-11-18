"""The brief: what a coding agent needs to fix a failed gate, as JSON small enough for its context (≈4 000 tokens).

    assay brief [run folder | analysis URL] [--case ID] [--full]

By default: the verdict and why, then every failure ranked, each with the check that failed (expected against what the
app actually saved), the step, the code of the change it suspects, how to reproduce it and links to the screenshots.
A failure that ran a second time from a fresh app says what that run found (`second_run`); one that passed then is
listed under `flaky` and blocks nothing.
`--case ID --full` adds that journey's steps, every check, the data that changed, console errors and API calls.
The same function serves the CLI (a local run folder), the server (`/api/analyses/<id>/brief`) and the MCP tools.
"""
from __future__ import annotations

import json
import re
import shlex
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .explain import FLAKY_NOTE, SECOND_RUN, failure_of, fill_failures, flaky_of, journey_phrase, known_of, subject, trim, verdict
from .probes import diff, get_path
from .quality import failure_signature

BUDGET_TOKENS = 4000


def _load(root: Path) -> dict:
    f = root / "analysis.json"
    if not f.exists():
        raise FileNotFoundError(f"no analysis.json in {root}: pass a run folder (.assay/runs/<id>) or an analysis URL")
    return json.loads(f.read_text())


def _evidence(root: Path, case_id: str) -> dict:
    try:
        return json.loads((root / "cases" / case_id / "evidence.json").read_text())
    except Exception:
        return {}


def build(root: Path, *, case: str | None = None, full: bool = False, base: str | None = None, link_query: str = "",
          analysis_url: str | None = None, budget: int = BUDGET_TOKENS, diff_text: str | None = None) -> dict:
    """The brief of an analysis folder (analysis.json + cases/<id>/evidence.json). `base`: URL prefix of the folder's
    files on the server (None: local paths) and `link_query` what their links carry (a private project's key).
    `diff_text`: a unified diff to find suspects in when the analysis has none
    (a local run on a branch)."""
    root = Path(root)
    a = _load(root)
    fill_failures(a, lambda cid: _evidence(root, cid))
    v = verdict(a)
    # a dataset row's folder has [ ] in its name: encoded, or curl reads them as a range
    link = (lambda rel: f"{base.rstrip('/')}/{urllib.parse.quote(rel, safe='/:@+')}{link_query}") if base else \
        (lambda rel: str((root / rel).resolve()))
    project = (a.get("run") or {}).get("project") or ".assay"
    known, flaky = known_of(a), flaky_of(a)
    regress = set((a.get("changes") or {}).get("regressions", []))
    blocking_ids = [p["case_id"] for p in v["problems"] if p["kind"] == "journey"]
    by_id = {c["case_id"]: c for c in a.get("cases", [])}
    probes = {}                               # the profile's probes are not in the analysis; the evidence has the data
    if diff_text is not None and not any(by_id[i].get("suspects") for i in blocking_ids if i in by_id):
        from .code import suspects_for
        for cid in blocking_ids:
            if cid in by_id:
                by_id[cid]["suspects"] = suspects_for(by_id[cid], _evidence(root, cid), diff_text, a.get("journey_map"))

    def failure_entry(c: dict, deep: bool) -> dict:
        ev = _evidence(root, c["case_id"]) if (deep or not c.get("failure")) else {}
        f = c.get("failure") or failure_of(c, ev, probes) or {}
        bad = next((s for s in c.get("steps", []) if s.get("ok") is False), None)
        shot = (f"cases/{c['case_id']}/{bad['screenshot']}" if bad and bad.get("screenshot") else c.get("screenshot"))
        out = {"case_id": c["case_id"], "promise": subject(c), "new": c["case_id"] in regress or not a.get("baseline"),
               "owner": c.get("owner") or "product", "critical": bool(c.get("critical")), "phrase": f.get("phrase"),
               "failed_check": {k: x for k, x in f.items() if k not in ("phrase", "also", "step")},
               "step": f.get("step") or ({k: bad.get(k) for k in ("i", "action", "summary", "error")} if bad else None),
               "repro": f"assay run {project} -k {shlex.quote(c['case_id'])} --no-scan",   # a dataset row's [..] is a shell glob
               "suspects": (c.get("suspects") or [])[:3],
               "artifacts": {k: link(x) for k, x in (("screenshot", shot), ("evidence", f"cases/{c['case_id']}/evidence.json")) if x}}
        if f.get("also"):
            out["also_failed"] = f["also"]
        if (again := c.get("confirm") or {}).get("verdict") in SECOND_RUN:     # what the run from a fresh app found
            out["second_run"] = {"verdict": again["verdict"], "note": SECOND_RUN[again["verdict"]], "phrase": again.get("phrase")}
            out["artifacts"]["second_run"] = link(f"{again.get('run') or 'confirm'}/cases/{c['case_id']}/evidence.json")
        if deep:
            out.update(_full(c, ev, link))
        return out

    if case:
        c = by_id.get(case)
        if c is None:
            raise KeyError(f"no journey {case!r} in this analysis; journeys: {', '.join(sorted(by_id))[:400]}")
        return {"case": failure_entry(c, full) if c["verdict"] != "PASS" else
                {"case_id": case, "promise": subject(c), "verdict": "PASS", **(_full(c, _evidence(root, case), link) if full else {})},
                "analysis": analysis_url}

    issues = [p for p in v["problems"] if p["kind"] == "issue"]
    brief = {
        "verdict": v["state"], "title": v["title"], "headline": v["headline"], "action": v["action"],
        "action_text": v["action_text"],
        "analysis": {"url": analysis_url, "commit": ((a.get("ci") or {}).get("sha") or "")[:12] or None,
                     "branch": (a.get("ci") or {}).get("branch"), "compared_with": a.get("baseline")},
        "scope": {k: v["scope"].get(k) for k in ("mode", "checked", "total", "why") if v["scope"].get(k) is not None},
        "failures": [failure_entry(by_id[i], False) for i in blocking_ids if i in by_id],
        "issues": [{"rule": p.get("rule"), "phrase": p["phrase"]} for p in issues],
        "not_checked": [{"case_id": c["case_id"], "phrase": journey_phrase(c), "owner": c.get("owner")}
                        for c in a.get("cases", []) if c["verdict"] == "INCONCLUSIVE"],
        "weakened_checks": (a.get("weakened") or {}).get("items", []),
        "known_bugs": [{"case_id": k, "phrase": journey_phrase(by_id[k]) if k in by_id else None,
                        "signature": list(failure_signature(by_id[k])) if k in by_id else None} for k in sorted(known)],
        "also_failing": [{"case_id": c["case_id"], "phrase": journey_phrase(c), "note": "fails on the target branch too"}
                         for c in a.get("cases", []) if c["verdict"] == "FAIL" and c["case_id"] not in known | flaky
                         and c["case_id"] not in blocking_ids],
        "flaky": [{"case_id": c["case_id"], "phrase": journey_phrase(c), "note": f"{FLAKY_NOTE}: not blocking"}
                  for c in a.get("cases", []) if c["case_id"] in flaky],
        "more": f"assay brief {analysis_url or root} --case <case_id> --full",
    }
    if v["state"] == "ship" and not brief["failures"]:
        brief["more"] = None
    return shrink(brief, budget)


def _full(c: dict, ev: dict, link) -> dict:
    """Everything one journey recorded, for `--full`: steps, checks, data that changed, errors, API calls."""
    before, after = ev.get("state_before") or {}, ev.get("state_after") or {}
    changes = []
    for p in diff({k: before[k] for k in before if k in after}, {k: after[k] for k in after if k in before})[:40]:
        b, a_ = get_path(before, p), get_path(after, p)
        changes.append({"path": p, "before": trim(b if b is not None else None, 200), "after": trim(a_, 200)})
    s = ev.get("sensors") or {}
    a11y = (ev.get("audit") or {}).get("a11y") or {}
    return {
        "steps": [{"i": st.get("i"), "action": st.get("action"), "summary": st.get("summary"), "ok": st.get("ok"),
                   "error": (st.get("error") or "")[:400] or None, "ms": st.get("ms"),
                   "screenshot": link(f"cases/{c['case_id']}/{st['screenshot']}") if st.get("screenshot") else None}
                  for st in c.get("steps", [])],
        "checks": [{"level": i["level"], "kind": i["kind"], "status": i["status"], "spec": trim(i.get("spec"), 300),
                    "detail": (i.get("detail") or "")[:400]} for i in c.get("items", [])],
        "data_changes": changes,
        "probe_errors": {**(ev.get("probe_errors_before") or {}), **(ev.get("probe_errors_after") or {})} or None,
        "errors": [{"text": x.get("text", "")[:300], "step": x.get("step"), "stack": (x.get("stack") or "")[:500] or None}
                   for x in [*s.get("page_errors", []), *[c_ for c_ in s.get("console", []) if c_.get("type") == "error"]]][:10],
        "failed_requests": [*s.get("failed_requests", []), *s.get("bad_responses", [])][:10],
        "api": (ev.get("api") or [])[:40],
        "frontend_files": (ev.get("frontend_files") or [])[:60],
        "accessibility": {"violations": a11y.get("violations", []), "needs_review": a11y.get("incomplete", [])} if a11y else None,
        "final_url": ev.get("final_url"),
        "page_text": (ev.get("page_text") or "")[:1500] or None,
    }


def tokens(obj: Any) -> int:
    """Rough token count of JSON (≈ 4 characters a token)."""
    return len(json.dumps(obj, ensure_ascii=False, default=str)) // 4


def shrink(obj: Any, budget: int) -> Any:
    """Fit the brief in the budget by cutting long strings and long lists harder and harder; keys always survive."""
    if tokens(obj) <= budget:
        return obj
    for n_str, n_list in ((400, 12), (240, 8), (160, 5), (100, 3), (60, 2)):
        out = _cut(obj, n_str, n_list)
        if tokens(out) <= budget:
            return out
    return out


def _cut(v: Any, n_str: int, n_list: int) -> Any:
    if isinstance(v, str):
        return v if len(v) <= n_str or v.startswith(("http://", "https://", "/")) else v[: n_str - 1] + "…"
    if isinstance(v, list):
        keep = v if len(v) <= n_list else v[:n_list]
        out = [_cut(x, n_str, n_list) for x in keep]
        return out + [f"… {len(v) - n_list} more"] if len(v) > n_list else out
    if isinstance(v, dict):
        return {k: _cut(x, n_str, n_list) for k, x in v.items()}
    return v


# ---- from a URL -----------------------------------------------------------------------------------------------------------

def api_url(url: str, case: str | None = None, full: bool = False) -> str:
    """The brief API address for any Assay link: an analysis page, a /a/<id>/ link or the brief itself."""
    u = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(u.query))
    m = re.match(r"/api/analyses/(\d+)/brief", u.path) or re.match(r"/a/(\d+)", u.path)
    aid = m[1] if m else q.get("analysis")
    if not aid:
        raise ValueError(f"{url}: not an Assay analysis link (it needs ?analysis=<id>, /a/<id>/ or /api/analyses/<id>/brief)")
    params = {k: x for k, x in (("k", q.get("k")), ("case", case or q.get("case")), ("full", "1" if full or q.get("full") else None)) if x}
    return f"{u.scheme}://{u.netloc}/api/analyses/{aid}/brief" + ("?" + urllib.parse.urlencode(params) if params else "")


def fetch(url: str, case: str | None = None, full: bool = False) -> dict:
    req = urllib.request.Request(api_url(url, case, full), headers={"Accept": "application/json", "User-Agent": "assay-brief"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FileNotFoundError(f"{url}: no such analysis, or it is private (use the link from the pull request comment, "
                                    "it carries the project's key)") from None
        raise RuntimeError(f"{url}: HTTP {e.code}") from None


def latest_run(project: Path) -> Path:
    runs = sorted((p for p in (Path(project) / "runs").glob("*/analysis.json")), key=lambda p: p.stat().st_mtime)
    if not runs:
        raise FileNotFoundError(f"no analysed run under {project}/runs: run `assay run {project}` first, or pass an analysis URL")
    return runs[-1].parent
