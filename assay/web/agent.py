"""The agent level of the project pages: every fact a page rests on, on one page, for a coding agent or an engineer
debugging. No prose: tables, two small charts, ids in monospace, numbers right-aligned.

Each page has `<page>_data(ctx)` → {"tables": {name: [row, …]}, "facts": {…}}, JSON-serialisable (the server exports it:
`?view=agent&format=json`, or one table as `&format=csv&table=<name>`), and `<page>_view(ctx)` → the page's inner HTML,
built from that data, so the page and the export never disagree. Pages: pr, health, journeys, prs. Every table is always
present (maybe empty): the export's shape does not depend on the analysis.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import replace
from pathlib import PurePosixPath
from urllib.parse import urlencode

from ..explain import cond_id, first_line, flaky_of, known_of, trim
from ..explain import verdict as decide
from ..model import SEV_RANK
from ..quality import failure_signature
from ..report import duration_s
from ..rules import route_of
from .pages import (_failed_rel, _fmt, _history_map, _hunk, _measured, analysis, broken_since, evidence, flips,
                    health_counts, md)
from .ui import DIM_SHORT, copy_line, e, icon, line_chart, purl, when

HISTORY = 20          # analyses of a branch the history columns look back over
FLAKY_WINDOW = 10     # a journey whose result flipped twice in this many analyses is flaky
EVIDENCE_MAX = 80     # journeys whose evidence (console, network, accessibility, vitals) one page reads
PRS_MAX = 60          # pull requests the lists look at, latest first
ORDER = {"FAIL": 0, "INCONCLUSIVE": 1, None: 2, "PASS": 3}
STATUS_ORDER = {"new": 0, "broken": 1, "on_target": 2, "known_bug": 3, "not_checked": 4, "flaky": 5}


# ---- values ----------------------------------------------------------------------------------------------------------------

def _num(x, nd: int = 2):
    return None if x is None else round(float(x), nd)


def _pctl(xs, q: float):
    """Nearest-rank percentile of the values given (None ignored)."""
    xs = sorted(x for x in xs if x is not None)
    return round(xs[max(0, math.ceil(q * len(xs)) - 1)], 2) if xs else None


def _rate(verdicts) -> float | None:
    """Share of PASS among decided results (not-checked runs left out)."""
    d = [v for v in verdicts if v in ("PASS", "FAIL")]
    return round(sum(v == "PASS" for v in d) / len(d), 3) if d else None


def _sig(c: dict) -> str | None:
    """quality.failure_signature as one token string: `step3 1:state(comments)` = step 3 broke, must #1 (state) failed."""
    s = failure_signature(c)
    if not s:
        return None
    head, failed = (f"step{'?' if s[1] is None else s[1]}", s[2]) if s[0] == "step" else (s[0], s[1])
    return " ".join([head, *(f"{n}:{k}" + (f"({','.join(p)})" if p else "") for n, k, p in failed)])


def _group(d: dict) -> str | None:
    return d.get("group") or (PurePosixPath(d["source"]).stem if d.get("source") else None)


def _dataset(a: dict) -> list[dict]:
    """The journeys of the project; analyses from before the dataset record: the ones that ran."""
    return a.get("dataset") or [{"id": c["case_id"], "title": c.get("title"), "promise": c.get("promise"),
                                 "critical": c.get("critical"), "tags": c.get("tags") or []} for c in a.get("cases", [])]


def _routes(jm: dict) -> tuple[list, list]:
    """A journey's API routes as patterns (`POST /api/posts/:id/comments`), and the ones that write."""
    rs = list(dict.fromkeys(f'{r.split(" ", 1)[0]} {route_of(r.split(" ", 1)[1])}' if " " in r else r
                            for r in jm.get("routes") or []))
    return rs, [r for r in rs if not r.startswith(("GET ", "HEAD ", "OPTIONS "))]


def _map_counts(jm: dict | None) -> dict:
    """What a journey touches (journey_map), as counts; None when the analysis did not map it."""
    routes, writes = _routes(jm or {})
    n = {"routes": len(routes), "writes": len(writes), **{k: len(jm.get(k) or []) for k in ("pages", "files", "tables")}} if jm else {}
    return {k: n.get(k) for k in ("routes", "writes", "pages", "files", "tables")} | {"write_routes": writes}


def _new_issues(a: dict) -> int:
    return sum(1 for i in a.get("issues", []) if i.get("new", True) and i.get("rule") != "CASE-FAIL")


def _abs(ctx, path: str | None) -> str | None:
    return f"{ctx.public}{path}" if path and path.startswith("/") else path


def _file(ctx, rel: str | None) -> str | None:
    """A file of the analysis (a screenshot); a private project's link carries its view key, as the brief's do."""
    return f'{_abs(ctx, ctx.shot(rel))}{"?k=" + ctx.p["view_key"] if ctx.private else ""}' if rel else None


def _latest_prs(ctx) -> list:
    """The latest analysis of each pull request, newest first (as ctx.rows)."""
    latest: dict = {}
    for r in ctx.rows:
        latest.setdefault(r["pr"], r)
    return [r for pr, r in latest.items() if pr]


def _status(c: dict, known: set, broke: set, compared: bool) -> str:
    """Why a journey did not pass: new here, already failing on the target, a known bug, not checked; or simply broken
    (a branch, or nothing to compare with)."""
    if c["verdict"] == "INCONCLUSIVE":
        return "not_checked"
    if c["case_id"] in known:
        return "known_bug"
    if (c.get("confirm") or {}).get("verdict") == "PASS":       # failed, then passed on a second run: blocks nothing
        return "flaky"
    return ("new" if c["case_id"] in broke else "on_target") if compared else "broken"


def _expected_actual(f: dict) -> tuple:
    """What a failed check expected, what the app had, and the data paths that were wrong."""
    if f.get("fields"):
        base = f.get("path")
        paths = [f"{base}.{k}" if base else k for k in f["fields"]]
        if len(f["fields"]) == 1:
            d = next(iter(f["fields"].values()))
            return trim(d.get("expected")), trim(d.get("actual")), paths
        return ({k: trim(d.get("expected")) for k, d in f["fields"].items()},
                {k: trim(d.get("actual")) for k, d in f["fields"].items()}, paths)
    if f.get("violations"):
        return None, [v.get("rule") for v in f["violations"]], []
    paths = [x for x in (f.get("path") or f.get("var"),) if x] or list(f.get("paths") or [])
    if "expected" in f or "actual" in f:
        return trim(f.get("expected")), trim(f.get("actual")), paths
    if f.get("errors"):
        return None, trim([str(x) for x in f["errors"][:3]]), paths
    return None, trim(f.get("request")), paths


def _failure(ctx, c: dict, status: str) -> dict:
    f = c.get("failure") or {}
    chk = f.get("check") or {}
    st = f.get("step") or next((s for s in c.get("steps", []) if s.get("ok") is False), None) or {}
    exp, act, paths = _expected_actual(f)
    return {"journey": c["case_id"], "status": status, "critical": bool(c.get("critical")), "verdict": c["verdict"],
            "step": st.get("i"), "action": st.get("action"), "check": chk.get("kind") or f.get("kind"),
            "level": chk.get("level"), "paths": paths, "expected": exp, "actual": act,
            "detail": chk.get("detail") or first_line(c.get("error") or c.get("summary"), 300) or None,
            "phrase": f.get("phrase"), "also": f.get("also") or [], "signature": _sig(c), "cause": c.get("cause") or None,
            "screenshot": _file(ctx, _failed_rel(c)), "brief": ctx.brief(absolute=True, case=c["case_id"], full="1")}


def _sensors(ev: dict) -> dict:
    """What a journey's evidence recorded besides its checks: errors, requests, accessibility, vitals (None: not read)."""
    s, au = ev.get("sensors") or {}, ev.get("audit") or {}
    vit = au.get("vitals") or {}
    out = {"console_errors": sum(1 for x in s.get("console") or [] if not isinstance(x, dict) or x.get("type") == "error"),
           "page_errors": len(s.get("page_errors") or []), "failed_requests": len(s.get("failed_requests") or []),
           "bad_responses": len(s.get("bad_responses") or []), "requests": s.get("requests"),
           "a11y": len((au.get("a11y") or {}).get("violations") or []), "lcp_ms": vit.get("lcp"), "cls": _num(vit.get("cls"), 3)}
    return out if ev else dict.fromkeys(out)


def _issues(a: dict) -> list[dict]:
    """Sensor findings, new first then worst first. Failed journeys (CASE-FAIL) are in `failures` already."""
    rows = [{"severity": i.get("severity"), "rule": i.get("rule"), "dimension": i.get("dimension"), "where": i.get("where"),
             "new": bool(i.get("new", True)), "count": i.get("count"), "message": (i.get("message") or "")[:300] or None,
             "journeys": i.get("sources") or []} for i in a.get("issues", []) if i.get("rule") != "CASE-FAIL"]
    return sorted(rows, key=lambda r: (not r["new"], SEV_RANK.get(r["severity"], 9), r["rule"] or "", r["where"] or ""))


def _gate(a: dict) -> list[dict]:
    return [{"id": cond_id(c), "actual": c.get("actual"), "op": c.get("op"), "threshold": c.get("threshold"),
             "ok": bool(c.get("ok")), "soft": bool(c.get("soft"))} for c in (a.get("gate") or {}).get("conditions", [])]


def _links(ctx, page: str) -> dict:
    return {"page": _abs(ctx, ctx.url(page)), "agent": _abs(ctx, ctx.url(page, view="agent")),
            "json": _abs(ctx, ctx.export(page, "json")), "brief": ctx.brief(absolute=True) if ctx.row else None,
            "analysis": _abs(ctx, ctx.analysis_json()) if ctx.row else None}


def _cols(spec: str) -> list[tuple]:
    """'key[:label] kind[>href_key], …' → [(key, label, kind, href_key)], `_` in a label is a space. Kinds: j journey id
    (linked), v status code, b bool, n number, s seconds, d Δ seconds, p percent, w time, m mono, x text on one line,
    t text, l list, u link. `>href_key`: the cell links to the row's value of href_key."""
    out = []
    for item in spec.split(","):
        name, kind = item.split()
        key, _, label = name.partition(":")
        kind, _, href = kind.partition(">")
        out.append((key, label.replace("_", " ") or key, kind, href or None))
    return out


# ---- the pull request ---------------------------------------------------------------------------------------------------

def pr_data(ctx) -> dict:
    a, row = ctx.a, ctx.row
    if not a:
        return {"facts": {}, "tables": {k: [] for k in ("failures", "suspects", "selection", "journeys", "issues", "gate",
                                                        "weakened")}}
    v = decide(a)
    cases = a.get("cases", [])
    known, broke = known_of(a), set(v["changes"]["broke"]) | set((a.get("changes") or {}).get("regressions") or [])
    compared = bool(a.get("baseline") or row["reference_id"])
    bad = sorted((c for c in cases if c["verdict"] != "PASS"),
                 key=lambda c: (STATUS_ORDER[_status(c, known, broke, compared)], not c.get("critical"), c["case_id"]))
    failures = [_failure(ctx, c, _status(c, known, broke, compared)) for c in bad]
    suspects = [{"journey": c["case_id"], "rank": k, "file": s.get("file"), "lines": s.get("lines"), "score": s.get("score"),
                 "why": s.get("why"), "hunk": s.get("hunk")}
                for c in bad for k, s in enumerate(c.get("suspects") or [], 1)]

    sc = v["scope"]
    reasons, ran = sc.get("reasons") or {}, {c["case_id"]: c for c in cases}
    pos = {cid: i for i, cid in enumerate(ran)}
    selection = sorted(({"journey": d["id"], "ran": d["id"] in ran, "verdict": (ran.get(d["id"]) or {}).get("verdict"),
                         "reason": reasons.get(d["id"]) or (sc.get("why") if d["id"] in ran else None),
                         "group": _group(d), "critical": bool(d.get("critical"))} for d in _dataset(a)),
                       key=lambda r: (not r["ran"], pos.get(r["journey"], 0), r["journey"]))

    ref = ctx.ref
    ref_cases = {c["case_id"]: c for c in (ctx.ref_a or {}).get("cases", [])}
    per_ref = _history_map(replace(ctx, pr=None, branch=ref["branch"], row=ref), FLAKY_WINDOW)[1] if ref else {}
    journeys = []
    for i, c in enumerate(cases):
        d, rd = c.get("duration_s"), (ref_cases.get(c["case_id"]) or {}).get("duration_s")
        journeys.append({"journey": c["case_id"], "verdict": c["verdict"], "duration_s": _num(d), "ref_duration_s": _num(rd),
                         "delta_s": _num(d - rd) if d is not None and rd is not None else None,
                         "steps": len(c.get("steps", [])),
                         "failed_step": next((s.get("i") for s in c.get("steps", []) if s.get("ok") is False), None),
                         "checks": len(c.get("items", [])),
                         "checks_failed": sum(it.get("status") == "fail" for it in c.get("items", [])),
                         **_sensors(evidence(ctx.store, row["id"], c["case_id"]) if i < EVIDENCE_MAX else {}),
                         "flaky_on_target": flips(per_ref.get(c["case_id"], [])) >= 2 if ref else None,
                         "browser": c.get("browser"), "viewport": c.get("viewport")})

    weak = a.get("weakened") or {}
    gate = _gate(a)
    n = Counter(c["verdict"] for c in cases)
    facts = {"analysis": row["id"], "pr": ctx.pr, "title": row["pr_title"], "branch": row["branch"], "target": row["target"],
             "sha": row["sha"], "created": row["created"], "verdict": v["state"], "gate": (a.get("gate") or {}).get("status"),
             "gate_failed": [g["id"] for g in gate if not g["ok"]], "checked": sc.get("checked"), "total": sc.get("total"),
             "selection": sc.get("mode"), "selection_why": sc.get("why"), "duration_s": _num(duration_s(a)),
             "journeys_time_s": _num(sum(c.get("duration_s") or 0 for c in cases)),
             "reference": f'{ref["branch"]}@{(ref["sha"] or "")[:7]}' if ref else a.get("baseline"),
             "reference_id": row["reference_id"], "pass": n["PASS"], "fail": n["FAIL"], "inconclusive": n["INCONCLUSIVE"],
             "new_issues": _new_issues(a), "broke": sorted(broke), "fixed": v["changes"]["fixed"], "known_bugs": sorted(known),
             "weakened": len(weak.get("items") or []), "weakened_approved": bool(weak.get("approved")),
             "browsers": sorted({c["browser"] for c in cases if c.get("browser")}),
             "viewports": sorted({c["viewport"] for c in cases if c.get("viewport")}), "links": _links(ctx, "")}
    return {"facts": facts, "tables": {
        "failures": failures, "suspects": suspects, "selection": selection, "journeys": journeys, "issues": _issues(a),
        "gate": gate, "weakened": [{"journey": w.get("case_id"), "change": w.get("phrase")} for w in weak.get("items") or []]}}


FAILURE_COLS = _cols("journey j, status v, critical:crit b, step n, action m, check m, level m, paths l, signature l, "
                     "screenshot:png u, brief u")
SUSPECT_COLS = _cols("journey j, rank:# n, file m, lines m, score n")
SELECTION_COLS = _cols("journey j, ran b, verdict v, group m, critical:crit b, reason t")
PR_JOURNEY_COLS = _cols("journey j, verdict v, duration_s:time_s s, ref_duration_s:ref_s s, delta_s:Δ_s d, steps n, "
                        "failed_step:fail@ n, checks n, checks_failed:failed n, console_errors:console n, page_errors:pg_err n, "
                        "failed_requests:req_fail n, bad_responses:4xx/5xx n, requests:req n, a11y n, lcp_ms:lcp_ms n, cls n, "
                        "flaky_on_target:flaky b")
ISSUE_COLS = _cols("severity:sev v, new b, rule m, dimension:dim m, where m, count:n n, journeys l, message x")
GATE_COLS = _cols("id:condition m, actual n, op m, threshold:limit n, ok v, soft b")


def pr_view(ctx) -> str:
    if not ctx.a:
        return _empty("no analysis")
    d = pr_data(ctx)
    f, t = d["facts"], d["tables"]
    kv = _kv([("verdict", _tag(f["verdict"])), ("gate", _tag(f["gate"])), ("gate failed", _v(f["gate_failed"])),
              ("checked", f'{_v(f["checked"])}/{_v(f["total"])}'), ("selection", _v(f["selection"])),
              ("why", md(f["selection_why"]) if f["selection_why"] else _v(None), True),
              ("pass", _n(f["pass"], "good")), ("fail", _n(f["fail"], "bad")), ("unsure", _n(f["inconclusive"], "warn")),
              ("broke", _v(f["broke"])), ("fixed", _v(f["fixed"])), ("new issues", _n(f["new_issues"], "warn")),
              ("weakened", _n(f["weakened"], "bad") + (" approved" if f["weakened_approved"] else "")),
              ("run, s", _v(f["duration_s"])), ("journeys, s", _v(f["journeys_time_s"])),
              ("reference", f'{_v(f["reference"])} <span class=mute>#{_v(f["reference_id"])}</span>'),
              ("head", f'{e(f["branch"])}@{e((f["sha"] or "")[:7])}'), ("browsers", _v(f["browsers"])),
              ("viewports", _v(f["viewports"])), ("analysis", f'#{f["analysis"]}')])
    dl = _downloads(ctx, "", d, [("brief.json", ctx.brief()),
                                 ("analysis.json", ctx.analysis_json())], cli=True)
    hunks = "".join(f'<details><summary><span class=mono>{e(s["journey"])} #{s["rank"]} {e(s["file"])}:{e(s["lines"])}</span>'
                    f'{_plusminus(s["hunk"])}</summary><pre class=hunk>{_hunk(s["hunk"])}</pre></details>'
                    for s in t["suspects"] if s.get("hunk"))
    weak = _panel(ctx, "", "weakened", t["weakened"], _cols("journey j, change t")) if t["weakened"] else ""
    return ('<div class="page agent">' + kv + dl
            + _panel(ctx, "", "failures", t["failures"], FAILURE_COLS, sub=("expected", "actual", "detail")) + weak
            + _panel(ctx, "", "suspects", t["suspects"], SUSPECT_COLS, sub=("why",),
                     after=f'<div class=ag-hunks>{hunks}</div>' if hunks else "")
            + '<div class="grid ag-g ag-g2">' + _panel(ctx, "", "selection", t["selection"], SELECTION_COLS)
            + _panel(ctx, "", "gate", t["gate"], GATE_COLS) + '</div>'
            + _panel(ctx, "", "journeys", t["journeys"], PR_JOURNEY_COLS) + _panel(ctx, "", "issues", t["issues"], ISSUE_COLS)
            + '</div>')


# ---- the health of a branch -------------------------------------------------------------------------------------------------

def health_data(ctx) -> dict:
    a = ctx.a
    if not a:
        return {"facts": {}, "tables": {k: [] for k in ("problems", "issues", "matrix", "runs", "measured", "gate", "map", "uncovered")}}
    hist, per = _history_map(ctx, HISTORY)
    cases = a.get("cases", [])
    now = {c["case_id"]: c for c in cases}
    known = known_of(a)
    flaky = {cid for cid, res in per.items() if flips(res[-FLAKY_WINDOW:]) >= 2} | flaky_of(a)   # and failed-then-passed-again
    ds = _dataset(a)
    ids = [d["id"] for d in ds] + sorted(set(per) - {d["id"] for d in ds})

    runs = []
    for h in hist:
        ha = analysis(ctx.store, h["id"]) or {}
        cs = ha.get("cases", [])
        n = Counter(c["verdict"] for c in cs)
        runs.append({"analysis": h["id"], "created": h["created"], "sha": (h["sha"] or "")[:12] or None, "status": h["status"],
                     "ran": len(cs), "pass": n["PASS"], "fail": n["FAIL"], "inconclusive": n["INCONCLUSIVE"],
                     "pass_rate": round(n["PASS"] / len(cs), 3) if cs else None, "duration_s": _num(duration_s(ha)),
                     "journeys_time_s": _num(sum(c.get("duration_s") or 0 for c in cs))})

    matrix = []
    for cid in sorted(ids, key=lambda x: (ORDER.get((now.get(x) or {}).get("verdict")), x)):
        res = per.get(cid, [])
        cells = {h["id"]: c["verdict"] for h, c in res}
        matrix.append({"journey": cid, "now": (now.get(cid) or {}).get("verdict"), "pass_rate": _rate(c["verdict"] for _, c in res),
                       "runs": len(res), "fails": sum(c["verdict"] == "FAIL" for _, c in res), "flips": flips(res),
                       "p50_s": _pctl((c.get("duration_s") for _, c in res), .5),
                       "last_fail": next((h["created"] for h, c in reversed(res) if c["verdict"] == "FAIL"), None),
                       **{f"#{h['id']}": cells.get(h["id"]) for h in hist}})

    problems = []
    for c in sorted(cases, key=lambda c: (ORDER[c["verdict"]], not c.get("critical"), c["case_id"])):
        cid = c["case_id"]
        if c["verdict"] == "PASS" and cid not in flaky:
            continue
        res = per.get(cid, [])
        problems.append({"journey": cid, "status": "flaky" if c["verdict"] == "PASS" else _status(c, known, set(), False),
                         "critical": bool(c.get("critical")), "flaky": cid in flaky,
                         "fails": sum(x["verdict"] == "FAIL" for _, x in res), "runs": len(res),
                         "since": broken_since(res) if c["verdict"] == "FAIL" else None,
                         "last_pass": next((h["created"] for h, x in reversed(res) if x["verdict"] == "PASS"), None),
                         "phrase": (c.get("failure") or {}).get("phrase") or first_line(c.get("summary"), 240) or None,
                         "signature": _sig(c)})

    m = _measured(a)
    jmap = a.get("journey_map") or {}
    amap = [{"journey": cid, **_map_counts(jmap.get(cid)), "table_names": (jmap.get(cid) or {}).get("tables") or []}
            for cid in ids]
    scr = a.get("screens") or a.get("coverage") or {}
    h = health_counts(a)
    durs = [c.get("duration_s") for c in cases]
    gate = _gate(a)
    facts = {"analysis": ctx.row["id"], "branch": ctx.branch, "sha": ctx.row["sha"], "created": ctx.row["created"],
             "verdict": decide(a)["state"], "gate": (a.get("gate") or {}).get("status"),
             "gate_failed": [g["id"] for g in gate if not g["ok"]], "journeys": h["total"], "ran": h["ran"], "kept": h["kept"],
             "broken": h["broken"], "known": h["known"], "flaky": len(flaky & set(now)),
             "not_checked": h["unchecked"] + max(0, h["total"] - h["ran"]),
             "pass_rate": round(h["kept"] / h["ran"], 3) if h["ran"] else None, "duration_s": _num(duration_s(a)),
             "journeys_time_s": _num(sum(x or 0 for x in durs)), "p50_journey_s": _pctl(durs, .5),
             "p95_journey_s": _pctl(durs, .95), "history": len(hist),
             "browsers": sorted({c["browser"] for c in cases if c.get("browser")}),
             "viewports": sorted({c["viewport"] for c in cases if c.get("viewport")}),
             "uncovered_screens": len(scr.get("uncovered") or []), "links": _links(ctx, "")}
    return {"facts": facts, "tables": {
        "problems": problems, "matrix": matrix, "runs": runs,
        "measured": [{"dimension": dim, "measured": dim in m, "by": m.get(dim) or []} for dim in DIM_SHORT],
        "issues": _issues(a), "gate": gate, "map": amap, "uncovered": [{"screen": s} for s in scr.get("uncovered") or []]}}


PROBLEM_COLS = _cols("journey j, status v, critical:crit b, flaky b, fails n, runs n, since:failing_since w, last_pass:last_pass w, "
                     "signature l")
RUN_COLS = _cols("analysis:# n, created w, sha m, status:gate v, ran n, pass n, fail n, inconclusive:unsure n, pass_rate:pass_% p, "
                 "duration_s:run_s s, journeys_time_s:journeys_s s")
MAP_COLS = _cols("journey j, routes n, writes n, pages n, files n, tables n, write_routes:write_routes l, table_names:table_names l")


def health_view(ctx) -> str:
    if not ctx.a:
        return _empty("no analysis on this branch")
    d = health_data(ctx)
    f, t = d["facts"], d["tables"]
    kv = _kv([("verdict", _tag(f["verdict"])), ("gate", _tag(f["gate"])), ("gate failed", _v(f["gate_failed"])),
              ("journeys", _v(f["journeys"])), ("ran", _v(f["ran"])), ("kept", _n(f["kept"], "good")),
              ("broken", _n(f["broken"], "bad")), ("known", _v(f["known"])), ("flaky", _n(f["flaky"], "warn")),
              ("not checked", _n(f["not_checked"], "warn")), ("pass rate", _pct(f["pass_rate"])), ("run, s", _v(f["duration_s"])),
              ("journeys, s", _v(f["journeys_time_s"])), ("p50 / p95, s", f'{_v(f["p50_journey_s"])} / {_v(f["p95_journey_s"])}'),
              ("browsers", _v(f["browsers"])), ("viewports", _v(f["viewports"])),
              ("head", f'{e(f["branch"])}@{e((f["sha"] or "")[:7])}'), ("analysis", f'#{f["analysis"]}'),
              ("analyses in history", _v(f["history"]))])
    dl = _downloads(ctx, "", d, [("brief.json", ctx.brief()), ("analysis.json", ctx.analysis_json())], cli=True)
    runs = t["runs"]
    labels, links = [f'#{r["analysis"]}' for r in runs], [ctx.url("", analysis=r["analysis"]) for r in runs]
    secs = [("run, s", [r["duration_s"] for r in runs], "var(--chart-2)"), ("journeys, s", [r["journeys_time_s"] for r in runs], "var(--chart-3)")]
    charts = ('<div class="grid g2 ag-g">'
              + _chart("pass_rate", line_chart([("pass rate", [r["pass_rate"] for r in runs], "var(--chart-1)")], labels, width=560,
                                               height=150, ymax=1, pct=True, links=links))
              + _chart("duration_s", line_chart(secs, labels, width=560, height=150, links=links,
                                                ymax=_ymax(x for _, s, _ in secs for x in s)), legend=[(n, c) for n, _, c in secs])
              + '</div>')
    side = _panel(ctx, "", "uncovered", t["uncovered"], _cols("screen m")) if t["uncovered"] else ""
    return ('<div class="page agent">' + kv + dl + charts
            + _panel(ctx, "", "problems", t["problems"], PROBLEM_COLS, sub=("phrase",))
            + _panel(ctx, "", "matrix", t["matrix"], None, body=_matrix(ctx, t["matrix"], runs))
            + '<div class="grid g2 ag-g">' + _panel(ctx, "", "measured", t["measured"], _cols("dimension m, measured b, by l"))
            + '<div>' + _panel(ctx, "", "gate", t["gate"], GATE_COLS) + side + '</div></div>'
            + _panel(ctx, "", "issues", t["issues"], ISSUE_COLS) + _panel(ctx, "", "map", t["map"], MAP_COLS) + _panel(ctx, "", "runs", runs, RUN_COLS) + '</div>')


def _ymax(vals) -> float:
    """A top for line_chart whose five gridlines are whole numbers: it rounds the top up to its next step, so pass one
    less than 4, 8, 20, 40, 80, 100, 200…"""
    top = max([v for v in vals if v is not None] or [1])
    return next(t for t in sorted({n * 10 ** k for k in range(9) for n in (4, 8, 20)} | {10 ** k for k in range(2, 10)})
                if t >= top) - 1


def _matrix(ctx, rows: list, runs: list) -> str:
    """Journey × analysis: one coloured cell per result, linked to that journey in that analysis."""
    if not rows:
        return '<div class=ag-none>0 rows</div>'
    head = ("<th scope=col>journey</th><th scope=col class=r>pass %</th><th scope=col class=r>flips</th><th scope=col class=r>p50 s</th><th scope=col>last fail</th>"
            + "".join(f'<th scope=col class=mx-h title="{e(when(r["created"]))}"><span>#{r["analysis"]}</span></th>' for r in runs))
    body = []
    for r in rows:
        cid = r["journey"]
        cells = "".join(
            f'<td class=mx><a class="{e(v)}" href="{e(ctx.url("journeys/" + cid, analysis=x["analysis"]))}" '
            f'title="#{x["analysis"]} {e(when(x["created"]))}: {e(v)}"></a></td>' if (v := r.get(f'#{x["analysis"]}'))
            else '<td class=mx><i></i></td>' for x in runs)
        body.append(f'<tr>{_cell(ctx, "j", cid)}{_cell(ctx, "p", r["pass_rate"])}{_cell(ctx, "n", r["flips"])}'
                    f'{_cell(ctx, "s", r["p50_s"])}{_cell(ctx, "w", r["last_fail"])}{cells}</tr>')
    return f'<div class=scroll tabindex=0 role=region aria-label="Table, scrolls sideways"><table class="tbl ag ag-mx"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


# ---- the journeys (the dataset) ------------------------------------------------------------------------------------------

def journeys_data(ctx) -> dict:
    a = ctx.a
    if not a:
        return {"facts": {}, "tables": {"journeys": [], "groups": []}}
    hist, per = _history_map(ctx, HISTORY)
    now = {c["case_id"]: c for c in a.get("cases", [])}
    jmap: dict = {}
    for h in hist:                                   # a pull request maps only the journeys it ran: the latest map of each
        jmap.update((analysis(ctx.store, h["id"]) or {}).get("journey_map") or {})
    jmap.update(a.get("journey_map") or {})
    prs = _latest_prs(ctx)[:PRS_MAX]
    picked = Counter(c["case_id"] for r in prs for c in (analysis(ctx.store, r["id"]) or {}).get("cases", []))
    rows = []
    for d in _dataset(a):
        cid = d["id"]
        res = per.get(cid, [])
        durs = [c.get("duration_s") for _, c in res]
        counts = _map_counts(jmap.get(cid))
        rows.append({"journey": cid, "group": _group(d), "now": (now.get(cid) or {}).get("verdict"),
                     "pass_rate": _rate(c["verdict"] for _, c in res), "runs": len(res), "flips": flips(res),
                     "p50_s": _pctl(durs, .5), "p95_s": _pctl(durs, .95), "critical": bool(d.get("critical")),
                     "ai": bool(d.get("ai")), "must": len(d["must"]) if isinstance(d.get("must"), list) else None,
                     "should": len(d["should"]) if isinstance(d.get("should"), list) else None,
                     **{k: counts[k] for k in ("routes", "writes", "files", "tables")}, "prs_selected": picked.get(cid, 0),
                     "last_fail": next((h["created"] for h, c in reversed(res) if c["verdict"] == "FAIL"), None),
                     "tags": d.get("tags") or [], "title": d.get("promise") or d.get("title")})
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["group"] or "—", {"group": r["group"] or "—", "journeys": 0, "critical": 0, "ran": 0,
                                                   "pass": 0, "broken": 0, "unsure": 0, "time_s": 0.0})
        c = now.get(r["journey"])
        g["journeys"] += 1
        g["critical"] += r["critical"]
        if c:
            g["ran"] += 1
            g["pass" if c["verdict"] == "PASS" else "broken" if c["verdict"] == "FAIL" else "unsure"] += 1
            g["time_s"] += c.get("duration_s") or 0
    for g in groups.values():
        g["pass_rate"] = round(g["pass"] / g["ran"], 3) if g["ran"] else None
        g["time_s"] = _num(g["time_s"])
    n = Counter(r["now"] for r in rows)
    facts = {"analysis": ctx.row["id"], "scope": f"pr {ctx.pr}" if ctx.pr else f"branch {ctx.branch}", "journeys": len(rows),
             "groups": len(groups), "ran": sum(n[v] for v in ("PASS", "FAIL", "INCONCLUSIVE")), "pass": n["PASS"],
             "fail": n["FAIL"], "inconclusive": n["INCONCLUSIVE"], "not_run": n[None], "critical": sum(r["critical"] for r in rows),
             "ai": sum(r["ai"] for r in rows), "history": len(hist), "prs_considered": len(prs),
             "links": _links(ctx, "journeys")}
    return {"facts": facts, "tables": {"journeys": rows, "groups": list(groups.values())}}


JOURNEY_COLS = _cols("journey j, group m, now v, pass_rate:pass_% p, runs n, flips n, p50_s:p50_s s, p95_s:p95_s s, "
                     "critical:crit b, ai b, must n, should n, routes n, writes n, files n, tables n, prs_selected:PRs n, "
                     "last_fail:last_fail w, tags l, title x")
GROUP_COLS = _cols("group m, journeys n, critical:crit n, ran n, pass n, broken n, unsure n, pass_rate:pass_% p, time_s:time_s s")


def journeys_view(ctx) -> str:
    if not ctx.a:
        return _empty("no analysis")
    d = journeys_data(ctx)
    f, t = d["facts"], d["tables"]
    kv = _kv([("scope", e(f["scope"])), ("journeys", _v(f["journeys"])), ("groups", _v(f["groups"])), ("ran", _v(f["ran"])),
              ("pass", _n(f["pass"], "good")), ("fail", _n(f["fail"], "bad")), ("unsure", _n(f["inconclusive"], "warn")),
              ("not run", _v(f["not_run"])), ("critical", _v(f["critical"])), ("ai", _v(f["ai"])),
              ("analyses in history", _v(f["history"])), ("PRs counted", _v(f["prs_considered"])), ("analysis", f'#{f["analysis"]}')])
    return ('<div class="page agent">' + kv + _downloads(ctx, "journeys", d, [("analysis.json", ctx.analysis_json())])
            + _panel(ctx, "journeys", "groups", t["groups"], GROUP_COLS)
            + _panel(ctx, "journeys", "journeys", t["journeys"], JOURNEY_COLS) + '</div>')


# ---- the pull requests ------------------------------------------------------------------------------------------------------

def prs_data(ctx) -> dict:
    allp = _latest_prs(ctx)
    rows = []
    for r in allp[:PRS_MAX]:
        pa = analysis(ctx.store, r["id"]) or {}
        v = decide(pa) if pa.get("gate") else None
        by = {c["case_id"]: c for c in pa.get("cases", [])}
        order = [by[x] for x in (v["changes"]["broke"] if v else []) if x in by] + list(by.values())
        sus = next((c["suspects"][0] for c in order if c.get("suspects")), None)
        sc = pa.get("scope") or {}
        q = urlencode({"pr": r["pr"]})
        rows.append({"pr": r["pr"], "status": r["status"], "verdict": v["state"] if v else None, "title": r["pr_title"],
                     "branch": r["branch"], "target": r["target"], "checked": sc.get("checked", len(by)),
                     "total": sc.get("total", len(by)), "duration_s": _num(duration_s(pa)),
                     "broke": len(v["changes"]["broke"]) if v else r["regressed"],
                     "fixed": len(v["changes"]["fixed"]) if v else r["fixed"], "new_issues": _new_issues(pa),
                     "top_suspect": f'{sus.get("file")}:{sus.get("lines")}' if sus else None,
                     "weakened": len((pa.get("weakened") or {}).get("items") or []), "created": r["created"],
                     "analysis": r["id"], "sha": (r["sha"] or "")[:12] or None,
                     "page": _abs(ctx, f"{purl(ctx.key)}?{q}"), "agent": _abs(ctx, f"{purl(ctx.key)}?{q}&view=agent"),
                     "brief": replace(ctx, row=r).brief(absolute=True)})
    n = Counter(r["status"] for r in rows)
    facts = {"prs": len(allp), "shown": len(rows), "passed": n["PASSED"], "failed": n["FAILED"],
             "inconclusive": n["INCONCLUSIVE"], "broke": sum(r["broke"] or 0 for r in rows),
             "p50_duration_s": _pctl((r["duration_s"] for r in rows), .5),
             "links": {"page": _abs(ctx, f"{purl(ctx.key)}/pull-requests"), "json": _abs(ctx, ctx.export("pull-requests", "json"))}}
    return {"facts": facts, "tables": {"prs": rows}}


PR_COLS = _cols("pr:# m>page, status:gate v, title x>page, branch m, target:→ m, checked n, total:of n, duration_s:run_s s, broke n, fixed n, "
                "new_issues:new_iss n, top_suspect:top_suspect m, weakened:weak n, created:analysed w, brief u")


def prs_view(ctx) -> str:
    d = prs_data(ctx)
    f, t = d["facts"], d["tables"]
    if not t["prs"]:
        return _empty("no pull request analysed")
    kv = _kv([("pull requests", _v(f["prs"]) + (f' <span class=mute>latest {f["shown"]}</span>' if f["shown"] < f["prs"] else "")),
              ("passed", _n(f["passed"], "good")), ("failed", _n(f["failed"], "bad")), ("unsure", _n(f["inconclusive"], "warn")),
              ("journeys broke", _v(f["broke"])), ("p50 run, s", _v(f["p50_duration_s"]))])
    return ('<div class="page agent">' + kv + _downloads(ctx, "pull-requests", d)
            + _panel(ctx, "pull-requests", "prs", t["prs"], PR_COLS) + '</div>')


# ---- HTML -------------------------------------------------------------------------------------------------------------------

def _empty(what: str) -> str:
    return f'<div class="page agent"><div class="panel empty mono">{e(what)}</div></div>'


def _v(x) -> str:
    if x is None or x == []:
        return '<span class=mute>—</span>'
    return e(", ".join(map(str, x)) if isinstance(x, list) else f"{x:g}" if isinstance(x, float) else x)


def _n(x, cls: str) -> str:
    """A count coloured by what it means, grey when zero."""
    return f'<span class={cls if x else "faint"}>{_v(x)}</span>'


def _pct(x) -> str:
    return _v(None) if x is None else f"{x:.0%}"


def _tag(v) -> str:
    """A status code as the engine writes it (PASS, FAILED, no_ship, known_bug…), coloured by meaning; yes/no as marks."""
    if isinstance(v, bool):
        return f'<span class="ag-v {"PASS" if v else "FAIL"}">{"✓" if v else "✕"}<span class=sr>{"yes" if v else "no"}</span></span>'
    return f'<span class="ag-v {e(v)}">{e(v)}</span>' if v is not None else _v(None)


def _plusminus(hunk: str | None) -> str:
    lines = (hunk or "").splitlines()
    add = sum(x.startswith("+") and not x.startswith("+++") for x in lines)
    rem = sum(x.startswith("-") and not x.startswith("---") for x in lines)
    return f' <span class="good num">+{add}</span> <span class="bad num">−{rem}</span>'


def _kv(items) -> str:
    """The fact strip: (label, html[, wide])."""
    return ('<dl class=ag-kv>' + "".join(f'<div{" class=wide" if len(it) > 2 and it[2] else ""}><dt>{e(it[0])}</dt>'
                                          f'<dd>{it[1]}</dd></div>' for it in items) + '</dl>')


def _downloads(ctx, page: str, data: dict, own=(), cli: bool = False) -> str:
    links = "".join(f'<a class=ag-btn href="{e(h)}">{icon("data")}{e(t)}</a>' for t, h in [*own, ("view.json", ctx.export(page, "json"))])
    csv = "".join(f'<a href="{e(ctx.export(page, "csv", n))}">{e(n)}</a>' for n in data["tables"])
    cmd = copy_line(f"assay brief {ctx.brief(absolute=True)}", "the brief command") if cli and ctx.row else ""
    return f'<div class=ag-dl>{links}<span class=ag-csv><b>csv</b>{csv}</span>{cmd}</div>'


def _cell(ctx, kind: str, v, label: str = "", href: str | None = None) -> str:
    right = " r" if kind in "nsdp" else ""
    if v is None or v == "" or v == []:
        return f'<td class="mute{right}">—</td>'
    if kind == "j":                      # a journey's page exists in the analyses that ran it
        ran = any(c["case_id"] == v for c in (ctx.a or {}).get("cases", []))
        return f'<td class=mono><a href="{e(ctx.url("journeys/" + v))}">{e(v)}</a></td>' if ran else f'<td class=mono>{e(v)}</td>'
    if kind == "v":
        return f'<td>{_tag(v)}</td>'
    if kind == "b":
        return '<td class=c>✓</td>' if v else '<td class="c mute">·</td>'
    if kind == "s":
        return f'<td class="r num">{v:.1f}</td>'
    if kind == "d":
        return f'<td class="r num {"bad" if v >= 1 else "good" if v <= -1 else ""}">{v:+.1f}</td>'
    if kind == "p":
        return f'<td class="r num">{v:.0%}</td>'
    if kind == "n":
        return f'<td class="r num">{_v(v)}</td>'
    if kind == "w":
        return f'<td class="num nw" title="{e(when(v))}">{e(_fmt(v, "%-d %b %H:%M"))}</td>'
    if kind == "u":
        return f'<td><a href="{e(v)}">{e(label)}</a></td>'
    if kind == "t":
        return f'<td class=w>{md(v)}</td>'
    if kind == "l":
        return f'<td class="mono l">{_v(v)}</td>'
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    short = s if len(s) <= 48 else s[:47] + "…"
    title = f' title="{e(s[:800])}"' if short != s else ""
    return f'<td class="{"x" if kind == "x" else "mono"}"{title}>' + (f'<a href="{e(href)}">{e(short)}</a>' if href else e(short)) + '</td>'


def _sub(r: dict, keys, span: int) -> str:
    """Long values of a row (expected, actual, detail), in full on a line of their own under it."""
    def val(v) -> str:
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        return s if len(s) <= 400 else s[:399] + "…"          # in full in the export
    parts = [f'<span class=k>{e(k)}</span> ' + (md(val(r[k])) if isinstance(r[k], str) else e(val(r[k])))
             for k in keys if r.get(k) not in (None, "", [])]
    return f'<tr class=ag-sub><td colspan={span}><div>{"<br>".join(parts)}</div></td></tr>' if parts else ""


def _table(ctx, rows: list, cols: list, sub=()) -> str:
    if not rows:
        return '<div class=ag-none>0 rows</div>'
    head = "".join(f'<th scope=col{" class=r" if k in "nsdp" else ""}>{e(label)}</th>' for _, label, k, _ in cols)
    body = "".join(f'<tr{" class=has-sub" if sub else ""}>'
                   + "".join(_cell(ctx, k, r.get(key), label, r.get(h) if h else None) for key, label, k, h in cols)
                   + "</tr>" + _sub(r, sub, len(cols)) for r in rows)
    return f'<div class=scroll tabindex=0 role=region aria-label="Table, scrolls sideways"><table class="tbl ag"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _panel(ctx, page: str, name: str, rows: list, cols: list | None, *, body: str | None = None, after: str = "",
           sub=()) -> str:
    """One table of the page's data, named as in the export, with its CSV."""
    return (f'<section class="panel ag-p" id="t-{e(name)}"><div class=ag-ph><h2>{e(name)}</h2><span class=n>{len(rows)}</span>'
            f'<a class=x href="{e(ctx.export(page, "csv", name))}">csv</a></div>'
            f'{body if body is not None else _table(ctx, rows, cols, sub)}{after}</section>')


def _chart(name: str, svg: str, legend=()) -> str:
    keys = "".join(f'<span><i style="background:{c}"></i>{e(t)}</span>' for t, c in legend)
    return (f'<section class="panel ag-p"><div class=ag-ph><h2>{e(name)}</h2>{f"<span class=ag-key>{keys}</span>" if keys else ""}'
            f'</div><div class=ag-chart>{svg}</div></section>')
