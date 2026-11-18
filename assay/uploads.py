"""Uploads are untrusted: anyone's CI job can send one. Everything the pages render is checked here first.

`unpack` extracts a run folder within size limits. `check_analysis` makes analysis.json safe to render: numbers are
numbers, statuses and ratings come from fixed lists, ids are plain names (they become paths and URLs). Free text
(titles, messages, summaries) stays text; the pages escape it, and their CSP allows no inline script anyway.
"""
from __future__ import annotations

import re
import tarfile
from pathlib import Path

from .model import DIMENSIONS

MAX_UNPACKED = 1024 ** 3            # 1 GB of files in one analysis
MAX_MEMBERS = 20000
MAX_ANALYSIS_JSON = 32 * 1024 ** 2

VERDICTS = {"PASS", "FAIL", "INCONCLUSIVE"}
GATES = {"PASSED", "FAILED", "INCONCLUSIVE"}
RATINGS = set("ABCDE")
SEVERITIES = {"blocker", "critical", "major", "minor", "info"}
ID = re.compile(r"[A-Za-z0-9_][\w.:@+\[\]-]{0,199}")  # case ids, also a dataset row's (x[late]): never "." or "..", no slashes
REL_PATH = re.compile(r"(?:[\w.@+\[\]-]+/){0,6}[\w.@+\[\]-]+")    # files inside the analysis folder


def unpack(tar_path: Path, dest: Path, required: str = "analysis.json") -> None:
    """Extract a .tar.gz run folder: regular files and folders only, within the size and count limits."""
    total, n = 0, 0
    with tarfile.open(tar_path, "r:gz") as t:
        members = []
        for m in t:
            n += 1
            if n > MAX_MEMBERS:
                raise ValueError(f"more than {MAX_MEMBERS} files in the upload")
            if not (m.isfile() or m.isdir()):
                continue
            total += m.size
            if total > MAX_UNPACKED:
                raise ValueError(f"the upload unpacks to more than {MAX_UNPACKED // 1024 ** 2} MB")
            members.append(m)
        t.extractall(dest, members=members, filter="data")
    f = dest / required
    if not f.is_file() or f.stat().st_size > MAX_ANALYSIS_JSON:
        raise ValueError(f"the upload must be a folder with {required} (at most 32 MB)")


def _num(v, where: str, default=0):
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{where} must be a number")
    return v


def _text(v, where: str, limit: int = 2000):
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    if not isinstance(v, str):
        raise ValueError(f"{where} must be text")
    return v[:limit]


def _id(v, where: str) -> str:
    if not isinstance(v, str) or not ID.fullmatch(v):
        raise ValueError(f"{where} must be a plain name (letters, digits, . _ - : @ + [ ]), got {str(v)[:60]!r}")
    return v


def _path(v, where: str):
    if v is None or v == "":
        return None
    if not isinstance(v, str) or not REL_PATH.fullmatch(v) or ".." in v.split("/"):
        raise ValueError(f"{where} must be a file inside the run folder")
    return v


def _dict(v, where: str) -> dict:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ValueError(f"{where} must be an object")
    return v


def _list(v, where: str) -> list:
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError(f"{where} must be a list")
    return v


def _plain(v, where: str, depth: int = 6, text: int = 4000, items: int = 400):
    """Free-form data (a failure's expected and actual values, a journey as written): plain JSON only, bounded."""
    if v is None or isinstance(v, bool) or (isinstance(v, (int, float))):
        return v
    if isinstance(v, str):
        return v[:text]
    if depth <= 0:
        return None
    if isinstance(v, list):
        return [_plain(x, where, depth - 1, text, items) for x in v[:items]]
    if isinstance(v, dict):
        return {str(k)[:200]: _plain(x, where, depth - 1, text, items) for k, x in list(v.items())[:items]}
    raise ValueError(f"{where}: unsupported value")


def _ids(v, where: str) -> list[str]:
    return [_id(x, where) for x in _list(v, where)]


def check_analysis(a) -> dict:
    """Validate and normalise analysis.json in place; raises ValueError with what is wrong."""
    a = _dict(a, "analysis")
    s = a["summary"] = _dict(a.get("summary"), "summary")
    for k in ("cases", "PASS", "FAIL", "INCONCLUSIVE", "flaky", "issues", "new_issues", "pages_scanned", "routes_scanned", "pass_rate"):
        s[k] = _num(s.get(k), f"summary.{k}")
    s["by_severity"] = {k: _num(v, f"summary.by_severity.{k}") for k, v in _dict(s.get("by_severity"), "summary.by_severity").items()
                        if k in SEVERITIES}
    g = a["gate"] = _dict(a.get("gate"), "gate")
    if g.get("status") not in GATES:
        raise ValueError(f"gate.status must be one of {sorted(GATES)}")
    for c in _list(g.get("conditions"), "gate.conditions"):
        c = _dict(c, "gate.conditions[]")
        c["name"] = _text(c.get("name"), "condition name", 200) or ""
        c["op"] = _text(c.get("op"), "condition op", 10) or ""
        c["actual"], c["threshold"] = _text(c.get("actual"), "condition actual", 60), _text(c.get("threshold"), "condition threshold", 60)
        c["ok"], c["soft"] = bool(c.get("ok")), bool(c.get("soft"))
        c["id"] = _text(c.get("id"), "condition id", 60)
    a["gate"] = {"status": g["status"], "conditions": g["conditions"],
                 **({"first_analysis": True} if g.get("first_analysis") is True else {})}
    for k in ("ratings", "ratings_new"):
        a[k] = {d: (r if r in RATINGS else None) for d, r in _dict(a.get(k), k).items() if d in DIMENSIONS}
    a["measured"] = {d: [_text(x, "measured", 100) for x in _list(v, "measured")]
                     for d, v in _dict(a.get("measured"), "measured").items() if d in DIMENSIONS}
    a["dimensions"] = {d: {"issues": _num(_dict(v, f"dimensions.{d}").get("issues"), f"dimensions.{d}.issues"),
                           "new": _num(v.get("new"), f"dimensions.{d}.new")}
                       for d, v in _dict(a.get("dimensions"), "dimensions").items() if d in DIMENSIONS}
    a["baseline"] = _text(a.get("baseline"), "baseline", 200)
    a["app"] = _dict(a.get("app"), "app")
    a["app"]["name"] = _text(a["app"].get("name"), "app.name", 100) or "app"
    a["app"]["title"] = _text(a["app"].get("title"), "app.title", 200)
    for c in _list(a.get("cases"), "cases"):
        c = _dict(c, "cases[]")
        c["case_id"] = _id(c.get("case_id"), "case_id")
        if c.get("verdict") not in VERDICTS:
            raise ValueError(f"cases[{c['case_id']}].verdict must be one of {sorted(VERDICTS)}")
        if c.get("severity") is not None and c["severity"] not in SEVERITIES:
            raise ValueError(f"cases[{c['case_id']}].severity must be one of {sorted(SEVERITIES)}")
        for k in ("duration_s", "score"):
            c[k] = _num(c.get(k), f"cases[{c['case_id']}].{k}", None)
        for k in ("title", "cause", "owner", "summary", "dimension", "final_url", "viewport", "browser", "account", "error"):
            c[k] = _text(c.get(k), f"cases[{c['case_id']}].{k}", 4000)
        c["screenshot"] = _path(c.get("screenshot"), f"cases[{c['case_id']}].screenshot")
        c["tags"] = [_text(t, "tag", 60) for t in _list(c.get("tags"), "tags")]
        c["promise"] = _text(c.get("promise"), "promise", 300)
        c["critical"] = bool(c.get("critical"))
        if c.get("confirm") is not None:             # what the second run of a failure found (assay/analyze.py)
            cf = _dict(c["confirm"], f"cases[{c['case_id']}].confirm")
            if cf.get("verdict") not in VERDICTS:
                raise ValueError(f"cases[{c['case_id']}].confirm.verdict must be one of {sorted(VERDICTS)}")
            c["confirm"] = {"verdict": cf["verdict"], "run": _path(cf.get("run"), "confirm run"),
                            "phrase": _text(cf.get("phrase"), "confirm phrase", 2000)}
        c["failure"] = _plain(c["failure"], "failure") if isinstance(c.get("failure"), dict) else None
        if c["failure"] is not None:     # the pages print these as text
            c["failure"]["phrase"] = str(c["failure"].get("phrase") or "")[:2000]
        c["suspects"] = [{"file": str(x.get("file") or "")[:400], "lines": str(x.get("lines") or "")[:40],
                          "why": str(x.get("why") or "")[:1000], "hunk": str(x.get("hunk") or "")[:6000],
                          "score": _num(x.get("score"), "suspect score", 0)}
                         for x in (_dict(y, "suspect") for y in _list(c.get("suspects"), "suspects")[:5])]
        for st in _list(c.get("steps"), "steps"):
            st = _dict(st, "steps[]")
            st["ms"] = _num(st.get("ms"), "step ms", None)
            st["i"] = _num(st.get("i"), "step index", None)
            st["screenshot"] = _path(st.get("screenshot"), "step screenshot")
            for k in ("action", "summary", "error"):
                st[k] = _text(st.get(k), f"step {k}", 4000)
    for i in _list(a.get("issues"), "issues"):
        i = _dict(i, "issues[]")
        i["rule"] = _id(i.get("rule"), "issue rule")
        i["severity"] = i.get("severity") if i.get("severity") in SEVERITIES else "info"
        i["dimension"] = i.get("dimension") if i.get("dimension") in DIMENSIONS else "functional"
        i["count"] = _num(i.get("count"), "issue count", 1)
        i["new"] = bool(i.get("new"))
        i["evidence"] = _path(i.get("evidence"), "issue evidence") if isinstance(i.get("evidence"), str) or i.get("evidence") is None else None
        i["sources"] = [_text(x, "issue source", 200) for x in _list(i.get("sources"), "issue sources")]
        for k in ("title", "where", "message", "key", "fingerprint"):
            i[k] = _text(i.get(k), f"issue {k}", 4000)
    for p in _list(a.get("pages"), "pages"):
        p = _dict(p, "pages[]")
        for k in ("status", "load_s", "a11y"):
            p[k] = _num(p.get(k), f"page {k}", None)
        p["screenshot"] = _path(p.get("screenshot"), "page screenshot")
        for k in ("url", "route", "viewport", "browser", "error"):
            p[k] = _text(p.get(k), f"page {k}", 2000)
        p["vitals"] = {k: _num(v, f"vitals.{k}", None) if k != "longTasks" else [_num(x, "long task") for x in _list(v, "longTasks")]
                       for k, v in _dict(p.get("vitals"), "vitals").items()}
        p["timing"] = {k: _num(v, f"timing.{k}", None) for k, v in _dict(p.get("timing"), "timing").items()}
    a.pop("coverage", None)                     # a URL ratio read as product coverage: no longer shown
    scr = a.get("screens")
    if scr is not None:
        scr = a["screens"] = _dict(scr, "screens")
        scr["scanned"] = _num(scr.get("scanned"), "screens.scanned")
        for k in ("discovered", "covered", "uncovered"):
            scr[k] = [_text(x, "route", 300) for x in _list(scr.get(k), f"screens.{k}")]
    a["known_bugs"] = _ids(a.get("known_bugs"), "known_bugs") if "known_bugs" in a else None
    if a["known_bugs"] is None:
        del a["known_bugs"]
    a["reference_ids"] = _ids(a.get("reference_ids"), "reference_ids")
    sc = _dict(a.get("scope"), "scope")
    a["scope"] = {"mode": _text(sc.get("mode"), "scope.mode", 20), "checked": _num(sc.get("checked"), "scope.checked", None),
                  "total": _num(sc.get("total"), "scope.total", None), "why": _text(sc.get("why"), "scope.why", 600),
                  "reasons": {_id(k, "scope reason"): _text(v, "scope reason", 300) for k, v in _dict(sc.get("reasons"), "scope.reasons").items()},
                  "skipped": _ids(sc.get("skipped"), "scope.skipped")} \
        if sc else None
    ds = []
    for d in _list(a.get("dataset"), "dataset"):
        d = _dict(d, "dataset[]")
        ds.append({"id": _id(d.get("id"), "dataset id"), "title": _text(d.get("title"), "dataset title", 300),
                   "promise": _text(d.get("promise"), "dataset promise", 300), "critical": bool(d.get("critical")),
                   "ai": bool(d.get("ai")), "tags": [_text(t, "tag", 60) for t in _list(d.get("tags"), "tags")],
                   "source": _text(d.get("source"), "dataset source", 300),
                   **({"group": _text(d["group"], "dataset group", 120)} if d.get("group") else {}),
                   # steps and start state: the guard and the selection compare them with the pull request's
                   **({"journey": _plain(d["journey"], "dataset journey")} if isinstance(d.get("journey"), dict) else {}),
                   "must": _plain(d.get("must"), "dataset must"), "should": _plain(d.get("should"), "dataset should")})
    a["dataset"] = ds
    a["settings"] = _plain(a["settings"], "settings") if isinstance(a.get("settings"), dict) else None
    jm = {}
    for cid, e in _dict(a.get("journey_map"), "journey_map").items():
        e = _dict(e, "journey_map entry")
        jm[_id(cid, "journey_map id")] = {k: [_text(x, f"journey_map {k}", 400) for x in _list(e.get(k), f"journey_map {k}")[:500]]
                                          for k in ("routes", "probes", "tables", "files", "pages", "targets")}
    a["journey_map"] = jm
    w = a.get("weakened")
    a["weakened"] = {"items": [{"case_id": _text(x.get("case_id"), "weakened case", 200), "phrase": _text(x.get("phrase"), "weakened", 600)}
                               for x in (_dict(y, "weakened item") for y in _list(_dict(w, "weakened").get("items"), "weakened.items"))],
                     "approved": bool(w.get("approved")), "blocking": bool(w.get("blocking"))} if isinstance(w, dict) else None
    ch = a["changes"] = _dict(a.get("changes"), "changes")
    for k in ("regressions", "fixed"):
        ch[k] = [_id(x, f"changes.{k}") for x in _list(ch.get(k), f"changes.{k}")]
    return a


def check_meta(meta: dict) -> dict:
    """Upload metadata from the client: short text fields only."""
    out = {}
    for k, v in _dict(meta, "meta").items():
        if k == "reference_id":
            out[k] = v if isinstance(v, int) and not isinstance(v, bool) else None
        elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
            out[k] = str(v)[:300]
        elif v is None:
            out[k] = None
    return out


ARM = re.compile(r"[\w .:+'()-]{1,40}")


def _rate(v, where: str):
    v = _num(v, where, None)
    if v is not None and not 0 <= v <= 1:
        raise ValueError(f"{where} must be between 0 and 1")
    return v


def check_bench(b) -> dict:
    """Validate and normalise bench.json in place (see assay/bench.py)."""
    b = _dict(b, "benchmark")
    arms = _dict(b.get("arms"), "arms")
    if not arms or len(arms) > 8:
        raise ValueError("a benchmark has 1 to 8 arms")
    for name, r in arms.items():
        if not ARM.fullmatch(name):
            raise ValueError(f"arm name {name[:40]!r}: letters, digits, spaces and . : + ' ( ) - only, at most 40")
        r = _dict(r, f"arms.{name}")
        r["pass@1"], r["pass^k"] = _rate(r.get("pass@1"), "pass@1"), _rate(r.get("pass^k"), "pass^k")
        ci = _list(r.get("ci95"), "ci95")
        r["ci95"] = [_rate(x, "ci95") for x in ci[:2]] if len(ci) >= 2 else [None, None]
        r["flaky"] = [_id(c, "flaky case") for c in _list(r.get("flaky"), "flaky")]
        r["decided_cases"] = _num(r.get("decided_cases"), "decided_cases")
        d = r.get("diff_vs_baseline")
        if d is not None:
            d = r["diff_vs_baseline"] = _dict(d, "diff_vs_baseline")
            d["mean"] = _num(d.get("mean"), "diff mean", None)
            dci = _list(d.get("ci95"), "diff ci95")
            d["ci95"] = [_num(x, "diff ci95") for x in dci[:2]] if len(dci) >= 2 else [None, None]
            d["significant"] = bool(d.get("significant"))
    if b.get("baseline") not in arms:
        raise ValueError("baseline must name one of the arms")
    b["attempts"], b["cases"] = _num(b.get("attempts"), "attempts"), _num(b.get("cases"), "cases")
    per = {}
    for cid, row in _dict(b.get("per_case"), "per_case").items():
        _id(cid, "case id")
        per[cid] = {}
        for a, vs in _dict(row, "per_case row").items():
            if a not in arms:
                raise ValueError(f"per_case names an unknown arm {a[:40]!r}")
            vs = _list(vs, "verdicts")
            if any(v not in VERDICTS for v in vs):
                raise ValueError("verdicts must be PASS, FAIL or INCONCLUSIVE")
            per[cid][a] = vs[:100]
    b["per_case"] = per
    b["case_titles"] = {_id(k, "case id"): _text(v, "case title", 300) for k, v in _dict(b.get("case_titles"), "case_titles").items()}
    meta = {}
    for a, m in _dict(b.get("arms_meta"), "arms_meta").items():
        if a not in arms:
            continue
        m = _dict(m, "arms_meta")
        meta[a] = {"title": _text(m.get("title"), "arm title", 200), "base_url": _text(m.get("base_url"), "base_url", 300),
                   "median_journey_s": _num(m.get("median_journey_s"), "median_journey_s", None),
                   "median_answer_s": _num(m.get("median_answer_s"), "median_answer_s", None),
                   "env": {str(k)[:60]: _text(v, "env value", 200) for k, v in _dict(m.get("env"), "env").items()}}
    b["arms_meta"] = meta
    b["app"] = _text(b.get("app"), "app", 200)
    b["generated"] = _text(b.get("generated"), "generated", 40)
    return b
