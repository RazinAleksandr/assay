"""Check catalogue. Every must/should item is one check.

Code decides facts, the judge decides meaning. A check has two halves:
  observe(spec, page)        — optional, runs live at the end of the case, stores a raw fact
  decide(spec, ev, obs, ctx) — runs offline on the evidence, returns (status, detail)
so grading can be repeated without re-running the app.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from .driver import locate
from .model import NOK, OK, UNDECIDED
from .probes import MISSING, diff, get_path, path_matches

IMPACT_RANK = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}

# check name -> default quality dimension of a failure
DIMENSION = {
    "no_errors": "reliability", "no_failed_requests": "reliability", "a11y": "accessibility",
    "perf": "performance", "step_time": "performance", "no_layout_issues": "ux", "judge": "functional",
    "answer": "ai", "tools_called": "ai",
}


def check_name(item: dict) -> str:
    keys = [k for k in item if k not in ("rubric", "dimension", "note", "votes")]
    if len(keys) != 1:
        raise ValueError(f"check item must have exactly one check key: {item!r}")
    return keys[0]


# ---- live observers ---------------------------------------------------------

def _obs_visible(spec, page):
    try:
        loc = locate(page, spec)
        loc.wait_for(state="visible", timeout=3000)
        return True
    except Exception:
        return False


def _obs_not_visible(spec, page):
    try:
        locate(page, spec).wait_for(state="hidden", timeout=3000)
        return True
    except Exception:
        return False


def _full(page, target):
    """Like driver.locate but returns every match (no .first)."""
    if isinstance(target, str):
        if target.startswith(("css=", "text=", "xpath=", "role=", "#", ".", "[", "//")):
            return page.locator(target)
        return page.get_by_text(target)
    if "role" in target:
        kw = {"name": target["name"]} if "name" in target else {}
        return page.get_by_role(target["role"], **kw)
    if "css" in target:
        return page.locator(target["css"])
    if "text" in target:
        return page.get_by_text(target["text"], exact=bool(target.get("exact")))
    if "testid" in target:
        return page.get_by_test_id(target["testid"])
    if "label" in target:
        return page.get_by_label(target["label"])
    raise ValueError(f"count target not supported: {target!r}")


def _obs_invariant(spec, page):
    expr = spec if isinstance(spec, str) else spec["js"]
    body = expr if expr.strip().startswith(("async", "(", "function")) else f"async () => ({expr})"
    try:
        return {"value": page.evaluate(body)}
    except Exception as e:
        return {"error": str(e).split("\n")[0][:300]}


def _obs_url(spec, page):
    return page.url


OBSERVERS: dict[str, Callable] = {
    "visible": _obs_visible, "not_visible": _obs_not_visible, "invariant": _obs_invariant,
    "count": lambda spec, page: _full(page, {k: v for k, v in spec.items() if k not in ("eq", "gte", "lte", "gt", "lt")}).count(),
}


# ---- matchers for state ------------------------------------------------------

def match(value: Any, expected: Any, before: Any = MISSING) -> tuple[bool, str]:
    """Match a probed value against an expectation (literal or operator dict)."""
    if not isinstance(expected, dict) or not any(k in MATCH_OPS for k in expected):
        ok = value == expected
        return ok, "" if ok else f"expected {_short(expected)}, got {_short(value)}"
    for op, arg in expected.items():
        if op not in MATCH_OPS:
            return False, f"unknown matcher {op!r}"
        ok, why = MATCH_OPS[op](value, arg, before)
        if not ok:
            return False, why
    return True, ""


def _short(v: Any, n: int = 160) -> str:
    if v is MISSING:
        return "<missing>"
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


def _num(v):
    return len(v) if isinstance(v, (list, dict, str)) else v


def _subset(item: Any, pattern: Any) -> bool:
    if isinstance(pattern, dict) and isinstance(item, dict):
        for k, v in pattern.items():
            if k not in item:
                return False
            if isinstance(v, dict) and any(op in MATCH_OPS for op in v):
                if not match(item[k], v)[0]:
                    return False
            elif not _subset(item[k], v):
                return False
        return True
    if isinstance(pattern, str) and isinstance(item, str) and pattern.startswith("~"):
        return pattern[1:].lower() in item.lower()
    return item == pattern


def _field_ok(item: dict, k: str, want: Any) -> bool:
    if k not in item:
        return False
    if isinstance(want, dict) and any(op in MATCH_OPS for op in want):
        return match(item[k], want)[0]
    return _subset(item[k], want)


def closest_record(rows: list, want: dict, before: Any = MISSING) -> tuple[dict | None, bool]:
    """The record of a list nearest to the expected one: most matching fields, records the journey created first.
    Returns (record or None, whether it is new since the snapshot before the journey)."""
    known = {json.dumps(x, sort_keys=True, default=str) for x in before} if isinstance(before, list) else set()
    best, best_key = None, (-1, False)
    for x in rows:
        if isinstance(x, dict):
            key = (sum(_field_ok(x, k, w) for k, w in want.items()), json.dumps(x, sort_keys=True, default=str) not in known)
            if key > best_key:
                best, best_key = x, key
    if best is None or best_key[0] == 0:
        # no field matches: the record this journey created is still the one it got wrong ("saved cut short")
        new = [x for x in rows if isinstance(x, dict) and json.dumps(x, sort_keys=True, default=str) not in known] if known or before == [] else []
        return (new[-1], True) if new else (None, False)
    return best, best_key[1] and bool(known)


def field_ok(item: dict, k: str, want: Any) -> bool:
    return _field_ok(item, k, want)


def _closest(rows: list, want: dict, before: Any) -> str:
    """Why a list of records does not contain the expected one: the nearest record (new ones first) and its differing
    fields, e.g. `body = "QA kind words 8f40", expected "QA kind words 8f40: the light…"`."""
    best, is_new = closest_record(rows, want, before)
    if best is None:
        return f"no record matches {_short(want)}"
    ident = "" if is_new else next((f"{k}={best[k]}" for k in ("id", "uuid", "key", "slug", "name") if k in best), "")
    diffs = "; ".join(f"{k} = {_short(best.get(k, MISSING), 90)}, expected {_short(w, 90)}"
                      for k, w in want.items() if not _field_ok(best, k, w))
    return f"{'the new record' if is_new else 'closest record'}{' ' + ident if ident else ''}: {diffs}"


def _op_contains(v, arg, _b):
    if isinstance(v, str):
        ok = str(arg).lower() in v.lower()
    elif isinstance(v, list):
        ok = any(_subset(x, arg) for x in v)
        if not ok and isinstance(arg, dict):
            return False, _closest(v, arg, _b)
    elif isinstance(v, dict):
        ok = _subset(v, arg)
    else:
        ok = False
    return ok, "" if ok else f"{_short(v)} does not contain {_short(arg)}"


def _op_not_contains(v, arg, b):
    ok, _ = _op_contains(v, arg, b)
    return (not ok), "" if not ok else f"{_short(v)} contains {_short(arg)}"


def _op_cmp(fn, sym):
    def op(v, arg, _b):
        try:
            ok = fn(_num(v), arg)
        except TypeError:
            ok = False
        return ok, "" if ok else f"{_short(v)} is not {sym} {arg}"
    return op


def _op_delta(v, arg, before):
    if before is MISSING:
        return False, "no before-value to compute delta"
    try:
        d = _num(v) - _num(before)
    except TypeError:
        return False, f"cannot compute delta of {_short(v)}"
    ok = match(d, arg)[0] if isinstance(arg, dict) else d == arg
    return ok, "" if ok else f"changed by {d}, expected {arg}"


def _op_exists(v, arg, _b):
    ok = (v is not MISSING and v is not None) == bool(arg)
    return ok, "" if ok else ("missing" if arg else f"exists: {_short(v)}")


def _op_regex(v, arg, _b):
    ok = isinstance(v, str) and re.search(arg, v) is not None
    return ok, "" if ok else f"{_short(v)} does not match /{arg}/"


def _op_every(v, arg, _b):
    if not isinstance(v, list):
        return False, f"{_short(v)} is not a list"
    bad = [x for x in v if not _subset(x, arg)]
    return not bad, "" if not bad else f"{len(bad)} item(s) violate {_short(arg)}: {_short(bad[:2])}"


def _op_includes_all(v, arg, _b):
    if not isinstance(v, list):
        return False, f"{_short(v)} is not a list"
    missing = [a for a in (arg if isinstance(arg, list) else [arg]) if not any(_subset(x, a) for x in v)]
    return not missing, "" if not missing else f"missing {_short(missing)}"


def _op_any_of(v, arg, _b):
    ok = v in arg
    return ok, "" if ok else f"{_short(v)} not in {_short(arg)}"


def _op_unchanged(v, arg, before):
    if before is MISSING:
        return False, "no before-value"
    ok = (v == before) == bool(arg)
    return ok, "" if ok else ("changed" if arg else "did not change")


MATCH_OPS: dict[str, Callable] = {
    "eq": lambda v, a, b: (v == a, "" if v == a else f"expected {_short(a)}, got {_short(v)}"),
    "ne": lambda v, a, b: (v != a, "" if v != a else f"should not be {_short(a)}"),
    "gt": _op_cmp(lambda x, y: x > y, ">"), "gte": _op_cmp(lambda x, y: x >= y, ">="),
    "lt": _op_cmp(lambda x, y: x < y, "<"), "lte": _op_cmp(lambda x, y: x <= y, "<="),
    "len": lambda v, a, b: (match(_num(v), a)[0] if isinstance(a, dict) else _op_cmp(lambda x, y: x == y, "of length")(v, a, b)[0],
                            f"length {_num(v) if isinstance(v, (list, str, dict)) else _short(v)} does not satisfy {_short(a)}"),
    "contains": _op_contains, "includes": _op_contains, "not_contains": _op_not_contains,
    "excludes": _op_not_contains, "delta": _op_delta, "exists": _op_exists, "regex": _op_regex,
    "every": _op_every, "any_of": _op_any_of, "unchanged": _op_unchanged, "includes_all": _op_includes_all,
}


# ---- deciders ----------------------------------------------------------------

def _act_phase(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("phase") in ("act", "check")]


def d_no_errors(spec, ev, obs, ctx):
    s = ev.get("sensors") or {}
    allow = [re.compile(p) for p in (spec.get("allow", []) if isinstance(spec, dict) else [])]
    include_console = spec.get("console", True) if isinstance(spec, dict) else True
    errs = [e["text"] for e in _act_phase(s.get("page_errors", []))]
    if include_console:
        errs += [c["text"] for c in _act_phase(s.get("console", [])) if c["type"] == "error"]
    errs = [e for e in errs if not any(p.search(e) for p in allow)]
    if errs:
        return NOK, f"{len(errs)} error(s): " + " | ".join(dict.fromkeys(e[:140] for e in errs))
    return OK, ""


def d_no_failed_requests(spec, ev, obs, ctx):
    s = ev.get("sensors") or {}
    allow = set(spec.get("allow_status", [])) if isinstance(spec, dict) else set()
    bad = [f"{r['method']} {r['url'].split('?')[0]} → {r['status']}"
           for r in _act_phase(s.get("bad_responses", [])) if r.get("first_party") and r["status"] not in allow]
    bad += [f"{r['method']} {r['url'].split('?')[0]} ✗ {r['failure']}"
            for r in _act_phase(s.get("failed_requests", [])) if r.get("first_party")]
    return (NOK, "; ".join(dict.fromkeys(bad))[:600]) if bad else (OK, "")


def d_visible(spec, ev, obs, ctx):
    if obs is None:
        return UNDECIDED, "not observed (page unavailable)"
    return (OK, "") if obs else (NOK, f"not visible: {_short(spec)}")


def d_not_visible(spec, ev, obs, ctx):
    if obs is None:
        return UNDECIDED, "not observed"
    return (OK, "") if obs else (NOK, f"still visible: {_short(spec)}")


def d_count(spec, ev, obs, ctx):
    if not isinstance(obs, int):
        return UNDECIDED, f"count not observed: {obs}"
    exp = {k: v for k, v in spec.items() if k in ("eq", "gte", "lte", "gt", "lt")}
    ok, why = match(obs, exp)
    return (OK, f"{obs}") if ok else (NOK, why)


def d_text(spec, ev, obs, ctx):
    text = ev.get("page_text") or ""
    if isinstance(spec, str):
        spec = {"contains": spec}
    for op in ("contains", "not_contains", "regex"):
        if op in spec:
            vals = spec[op] if isinstance(spec[op], list) else [spec[op]]
            for v in vals:
                ok, why = match(text, {op: v})
                if not ok:
                    return NOK, f"page text: {op} {v!r} failed"
    return OK, ""


def d_url(spec, ev, obs, ctx):
    url = ev.get("final_url") or ""
    ok = re.search(spec, url) is not None
    return (OK, "") if ok else (NOK, f"url {url} does not match /{spec}/")


def state_value(ev, path):
    probe = path.split(".", 1)[0].split("[", 1)[0]
    after, before = ev.get("state_after") or {}, ev.get("state_before") or {}
    if probe not in after:
        err = (ev.get("probe_errors_after") or {}).get(probe) or (ev.get("probe_errors_before") or {}).get(probe)
        return None, None, f"probe {probe!r} unavailable" + (f": {err}" if err else "")
    return get_path(after, path), (get_path(before, path) if probe in before else MISSING), None


def d_state(spec, ev, obs, ctx):
    fails, undecided = [], []
    for path, expected in spec.items():
        v, b, err = state_value(ev, path)
        if err:
            undecided.append(err)
            continue
        ok, why = match(v, expected, b)
        if not ok:
            fails.append(f"{path}: {why}")
    if fails:
        return NOK, "; ".join(fails)
    if undecided:
        return UNDECIDED, "; ".join(undecided)
    return OK, ""


def _probe_of(path: str) -> str:
    return path.split(".", 1)[0].split("[", 1)[0]


def _changes(ev, ctx, probes: set[str] | None = None) -> tuple[list[str] | None, str]:
    """Changed paths between the snapshots before and after the journey, minus volatile ones; or (None, why) when a
    probe the check needs was not read both times. Fail-closed: a probe that failed must never drop out of the diff and
    let a side effect pass. `probes`: the probes the check is about (None: every probe, as `allowed_changes` needs)."""
    before, after = ev.get("state_before"), ev.get("state_after")
    if before is None or after is None:
        return None, "no state snapshots"
    errs = {**(ev.get("probe_errors_before") or {}), **(ev.get("probe_errors_after") or {})}
    known = set(before) | set(after) | set(errs)
    need = known if probes is None else set(probes)
    missing = sorted(p for p in need if p not in before or p not in after or p in errs)
    if missing:
        why = "; ".join(re.sub(rf"^probe {re.escape(p)}: ", "", errs[p][:160]) if p in errs else
                        ("read only before or after the journey" if p in known else "no such probe") for p in missing)
        return None, f"probe {', '.join(missing)} unreadable: {why}"
    paths = diff({k: before[k] for k in need}, {k: after[k] for k in need})
    volatile = ctx.get("volatile", [])
    return [p for p in paths if not path_matches(p, volatile)], ""


def d_changed(spec, ev, obs, ctx):
    paths = spec if isinstance(spec, list) else [spec]
    ch, why = _changes(ev, ctx, {_probe_of(p) for p in paths})
    if ch is None:
        return UNDECIDED, why
    missing = [p for p in paths if not any(path_matches(c, [p]) for c in ch)]
    return (NOK, "did not change: " + ", ".join(missing)) if missing else (OK, "")


def d_unchanged(spec, ev, obs, ctx):
    paths = spec if isinstance(spec, list) else [spec]
    ch, why = _changes(ev, ctx, {_probe_of(p) for p in paths})
    if ch is None:
        return UNDECIDED, why
    bad = [c for c in ch if path_matches(c, paths)]
    return (NOK, "changed: " + ", ".join(bad[:8])) if bad else (OK, "")


def d_allowed_changes(spec, ev, obs, ctx):
    ch, why = _changes(ev, ctx)
    if ch is None:
        return UNDECIDED, why
    allowed = spec if isinstance(spec, list) else [spec]
    bad = [c for c in ch if not path_matches(c, allowed)]
    if bad:
        return NOK, "unexpected changes: " + ", ".join(bad[:8]) + (f" (+{len(bad) - 8} more)" if len(bad) > 8 else "")
    return OK, f"{len(ch)} change(s), all allowed"


def d_invariant(spec, ev, obs, ctx):
    if not isinstance(obs, dict):
        return UNDECIDED, "not observed"
    if "error" in obs:
        return UNDECIDED, f"invariant threw: {obs['error']}"
    v = obs.get("value")
    if isinstance(spec, dict) and "equals" in spec:
        ok = v == spec["equals"]
    else:
        ok = v is True or (isinstance(v, (int, float)) and v != 0) or (isinstance(v, list) and not v)
    if ok:
        return OK, ""
    return NOK, f"invariant violated: {_short(v, 300)}"


def d_a11y(spec, ev, obs, ctx):
    a = (ev.get("audit") or {}).get("a11y")
    if not a:
        return UNDECIDED, "no accessibility audit"
    if a.get("error"):
        return UNDECIDED, a["error"]
    # violations at or above this impact fail the check (default: serious → serious + critical)
    worst = (spec.get("max_impact", "serious") if isinstance(spec, dict) else "serious")
    limit = IMPACT_RANK[worst]
    bad = [v for v in a.get("violations", []) if IMPACT_RANK.get(v.get("impact") or "minor", 3) <= limit]
    if bad:
        return NOK, "; ".join(f"{v['id']} ({v['impact']}, {v['nodes']} el.)" for v in bad[:6])
    return OK, ""


def d_perf(spec, ev, obs, ctx):
    audit = ev.get("audit") or {}
    vit = audit.get("vitals") or {}
    if any(k in spec for k in ("lcp_ms", "cls", "inp_ms", "long_task_ms")) and not vit:
        return UNDECIDED, audit.get("error") or "no web vitals recorded"
    if "lcp_ms" in spec and vit.get("lcp") is None:
        return UNDECIDED, "no Largest Contentful Paint recorded"
    fails = []
    if "lcp_ms" in spec and vit.get("lcp") is not None and vit["lcp"] > spec["lcp_ms"]:
        fails.append(f"LCP {round(vit['lcp'])}ms > {spec['lcp_ms']}ms")
    if "cls" in spec and vit.get("cls", 0) > spec["cls"]:
        fails.append(f"CLS {vit['cls']:.3f} > {spec['cls']}")
    if "inp_ms" in spec and vit.get("inp") is not None and vit["inp"] > spec["inp_ms"]:
        fails.append(f"INP {round(vit['inp'])}ms > {spec['inp_ms']}ms")
    if "long_task_ms" in spec and vit.get("longTasks"):
        worst = max(vit["longTasks"])
        if worst > spec["long_task_ms"]:
            fails.append(f"long task {worst}ms > {spec['long_task_ms']}ms")
    if "step_ms" in spec:
        slow = [f"step {s['i']} {s['action']} {s['ms']}ms" for s in ev.get("steps", [])
                if s.get("ms", 0) > spec["step_ms"] and s["action"] not in ("wait", "goto", "repeat", "ask")]
        fails += slow[:3]
    return (NOK, "; ".join(fails)) if fails else (OK, "")


def d_no_layout_issues(spec, ev, obs, ctx):
    a = ev.get("audit") or {}
    if "error" in a and "overflowX" not in a:
        return UNDECIDED, a["error"]
    fails = []
    if a.get("blank"):
        fails.append("blank screen")
    if a.get("overflowX"):
        fails.append("horizontal overflow: " + ", ".join(a.get("overflowEls", [])[:2]))
    if a.get("brokenImages"):
        fails.append(f"{len(a['brokenImages'])} broken image(s)")
    return (NOK, "; ".join(fails)) if fails else (OK, "")


def d_answer(spec, ev, obs, ctx):
    answers = ev.get("answers") or []
    if not answers:
        return UNDECIDED, "no chat answer recorded"
    text = answers[-1].get("answer", "")
    if isinstance(spec, str):
        spec = {"contains": spec}
    if spec.get("min_len") and len(text) < spec["min_len"]:
        return NOK, f"answer too short ({len(text)} chars)"
    for op in ("contains", "not_contains", "regex"):
        if op in spec:
            vals = spec[op] if isinstance(spec[op], list) else [spec[op]]
            for v in vals:
                ok, _ = match(text, {op: v})
                if not ok:
                    return NOK, f"answer: {op} {v!r} failed"
    return OK, ""


def d_tools_called(spec, ev, obs, ctx):
    trace = ev.get("trace")
    if not trace:
        return UNDECIDED, "no trace (configure a trace adapter)"
    called = {t.get("name") for t in trace.get("tools", [])}
    missing = [t for t in spec if t not in called]
    return (NOK, f"not called: {missing}; called: {sorted(called)}") if missing else (OK, "")


def d_vars(spec, ev, obs, ctx):
    """Check values saved by `http` steps (`save:`) — API-level assertions inside a journey."""
    vars_ = ev.get("vars") or {}
    fails, undecided = [], []
    for name, expected in spec.items():
        if name not in vars_:
            undecided.append(f"var {name!r} not saved")
            continue
        ok, why = match(vars_[name], expected)
        if not ok:
            fails.append(f"{name}: {why}")
    if fails:
        return NOK, "; ".join(fails)
    return (UNDECIDED, "; ".join(undecided)) if undecided else (OK, "")


_NUM = re.compile(r"-?\d[\d,\u00a0\u202f ]*(?:\.\d+)?")


def d_numbers(spec, ev, obs, ctx):
    """Every expected number appears in the last answer within its tolerance (AI answers, reports)."""
    answers = ev.get("answers") or []
    text = answers[-1].get("answer", "") if answers else (ev.get("page_text") or "")
    if not text:
        return UNDECIDED, "no answer text"
    found = []
    for m in _NUM.finditer(text):
        try:
            found.append(float(re.sub(r"[,\u00a0\u202f ]", "", m.group(0))))
        except ValueError:
            pass
    missing = [e["value"] for e in spec if not any(abs(f - e["value"]) <= e.get("tolerance", 0) for f in found)]
    return (NOK, f"numbers not found: {missing}") if missing else (OK, f"{len(spec)} number(s) found")


def d_tools_any(spec, ev, obs, ctx):
    trace = ev.get("trace")
    if not trace:
        return UNDECIDED, "no trace (configure a trace adapter)"
    called = {t.get("name") for t in trace.get("tools", [])}
    return (OK, "") if called & set(spec) else (NOK, f"none of {spec} called; called: {sorted(called)}")


def d_step_time(spec, ev, obs, ctx):
    return d_perf({"step_ms": spec if isinstance(spec, (int, float)) else spec["max_ms"]}, ev, obs, ctx)


DECIDERS: dict[str, Callable] = {
    "no_errors": d_no_errors, "no_failed_requests": d_no_failed_requests, "visible": d_visible,
    "not_visible": d_not_visible, "count": d_count, "text": d_text, "url": d_url, "state": d_state,
    "changed": d_changed, "unchanged": d_unchanged, "allowed_changes": d_allowed_changes,
    "invariant": d_invariant, "a11y": d_a11y, "perf": d_perf, "no_layout_issues": d_no_layout_issues,
    "answer": d_answer, "tools_called": d_tools_called, "tools_any": d_tools_any, "step_time": d_step_time,
    "vars": d_vars, "numbers": d_numbers,
    # "judge" is decided in grade.py (needs the judge backend)
}

KNOWN_CHECKS = set(DECIDERS) | {"judge"}


def register_check(name: str, decide, observe=None, dimension: str = "functional") -> None:
    """Plugin API: add a product-specific check (decide(spec, ev, obs, ctx) -> (status, detail))."""
    DECIDERS[name] = decide
    DIMENSION[name] = dimension
    if observe:
        OBSERVERS[name] = observe
