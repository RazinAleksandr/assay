"""What a result means, in words a person reads in five seconds. Built from the data the checks recorded, never made up.

Every phrase is a template filled with recorded values ("`comments.body` was saved cut short in the database: 20 of 90
characters"). A check without a template falls back to its own detail. Nothing here calls a model: if a phrase lied
once, nobody would trust the gate again. Phrases use one bit of markup, `code`, which the PR comment shows as is and
the pages render as <code>.

  failure_of(result, evidence, probes) → the structured failure of one journey, with its phrase
  issue_phrase(issue)                   → a sensor finding (console error, accessibility, …) in words
  verdict(analysis)                     → ship / no_ship / unsure, the title, the headline and what the person must do
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import checks as C
from .model import FAIL, INCONCLUSIVE, NOK, PASS, SEV_RANK
from .probes import MISSING

STATE_ICON = {"ship": "✅", "no_ship": "❌", "unsure": "⚠️"}
STATE_LABEL = {"ship": "Ship it", "no_ship": "Don't ship", "unsure": "Not sure"}
GATE_STATE = {"PASSED": "ship", "FAILED": "no_ship", "INCONCLUSIVE": "unsure"}

NOT_CHECKED = {      # why a journey could not be checked; none of these is the product's fault
    "app_unreachable": "the app did not start or did not answer",
    "login_failed": "the test account could not log in",
    "setup_failed": "the start state could not be prepared",
    "probe_failed": "the app's data could not be read",
    "precondition_mismatch": "the data at the start was not what the journey expects",
    "browser_crashed": "the browser or the runner crashed",
    "agent_unavailable": "the goal agent could not run",
    "case_invalid": "the journey itself is invalid",
}
UNDECIDED_WHY = {
    "judge": "the AI judge did not decide (no API key, or it could not answer)",
    "a11y": "the accessibility audit did not complete",
    "allowed_changes": "the app's data could not be read before and after",
    "changed": "the app's data could not be read before and after",
    "unchanged": "the app's data could not be read before and after",
    "state": "the app's data could not be read",
    "tools_called": "no trace of the assistant's tool calls",
}
VERB = {"click": "click", "dblclick": "double-click", "tap": "tap", "hover": "hover over", "fill": "fill in",
        "type": "type into", "press": "press", "select": "choose from", "check": "tick", "uncheck": "untick",
        "upload": "upload to", "wait_for": "see", "goto": "open", "drag": "drag", "draw": "draw on", "scroll": "scroll",
        "http": "call", "login": "log in as", "ask": "ask", "expect": "see", "download": "download from",
        "reload": "reload the page", "back": "go back", "new_tab": "open a tab", "switch_tab": "switch to"}
A11Y_PHRASE = {      # axe rule → what a person loses, with {n} elements
    "button-name": "button{s} without a name for screen readers",
    "link-name": "link{s} without a name for screen readers",
    "label": "form field{s} without a label",
    "select-name": "dropdown{s} without a label",
    "input-button-name": "input button{s} without a name",
    "image-alt": "image{s} without alt text",
    "role-img-alt": "image{s} without alt text",
    "svg-img-alt": "image{s} without alt text",
    "color-contrast": "text element{s} with too little contrast to read",
    "aria-required-children": "menu{s} or list{s} whose items screen readers cannot follow",
    "aria-required-parent": "item{s} missing the menu or list around {them}",
    "aria-allowed-attr": "element{s} with unsupported ARIA attributes",
    "aria-valid-attr-value": "element{s} with invalid ARIA values",
    "label-content-name-mismatch": "control{s} whose spoken name differs from the visible text",
    "meta-viewport": "page{s} with zoom disabled",
    "document-title": "page{s} without a title",
    "html-has-lang": "page{s} without a language",
    "duplicate-id-aria": "element{s} with an id that is not unique",
    "nested-interactive": "control{s} nested inside another control",
    "scrollable-region-focusable": "scrollable area{s} the keyboard cannot reach",
}
STORE = {"sqlite": "database", "sql": "database", "postgres": "database", "http": "API", "js": "browser",
         "cmd": "app state", "files": "files"}
SECOND_RUN = {FAIL: "Reproduced on a second run",       # a failure that blocked ran once more from a fresh app
              INCONCLUSIVE: "A second run could not check it"}
FLAKY_NOTE = "failed once, passed on a second run"


# ---- small text helpers ----------------------------------------------------------------------------------------------------

def _s(n: int) -> str:
    return "" if n == 1 else "s"


def _val(v: Any, n: int = 90) -> str:
    if v is MISSING or v is None:
        return "nothing"
    s = json.dumps(v, ensure_ascii=False, default=str) if not isinstance(v, str) else f'"{v}"'
    return s if len(s) <= n else s[: n - 2] + "…" + s[-1]


def trim(v: Any, n: int = 300) -> Any:
    """A value small enough for a brief: long strings cut, long lists sampled."""
    if v is MISSING:
        return None
    if isinstance(v, str):
        return v if len(v) <= n else v[: n - 1] + "…"
    if isinstance(v, list):
        out = [trim(x, n) for x in v[:5]]
        return out + [f"… {len(v) - 5} more"] if len(v) > 5 else out
    if isinstance(v, dict):
        return {k: trim(x, n) for k, x in list(v.items())[:20]}
    return v


def first_line(err: str | None, n: int = 160) -> str:
    """Playwright errors carry a call log; the first line says what happened, in words where we know the pattern."""
    line = re.split(r"\n|Call log:", err or "")[0].strip()
    line = re.sub(r"^(Error|TimeoutError|step \d+ \(\w+\)):\s*", "", line)
    line = re.sub(r"^\w+\.\w+:\s*", "", line)                      # Locator.click: …
    if m := re.search(r"Timeout (\d+)ms exceeded", line):
        return f"nothing matched within {int(m[1]) / 1000:g} s"
    if "strict mode violation" in line:
        return "more than one element matched"
    return line if len(line) <= n else line[: n - 1] + "…"


def target_text(t: Any) -> str:
    """A step target as a person names it: button “Post”, the “Email” field, “Save draft”."""
    if isinstance(t, str):
        return f"“{t}”" if not t.startswith(("css=", "#", ".", "[", "//", "xpath=")) else f"`{t}`"
    if not isinstance(t, dict):
        return ""
    if "role" in t:
        return f"{t['role']} “{t['name']}”" if t.get("name") else f"a {t['role']}"
    if "label" in t:
        return f"the “{t['label']}” field"
    if "placeholder" in t:
        return f"the “{t['placeholder']}” field"
    if "text" in t:
        return f"“{t['text']}”"
    if "testid" in t:
        return f"`[data-testid={t['testid']}]`"
    if "css" in t:
        return f"`{t['css']}`"
    if "url" in t or "path" in t:
        return f"`{t.get('url') or t.get('path')}`"
    return ""


def code(s: str) -> str:
    return f"`{s}`"


# ---- one journey ----------------------------------------------------------------------------------------------------------

def subject(r: dict) -> str:
    return r.get("promise") or r.get("title") or r["case_id"]


def store_of(probe: str, probes: dict | None) -> str:
    spec = (probes or {}).get(probe) or {}
    return next((STORE[k] for k in spec if k in STORE), "app state") if isinstance(spec, dict) else "app state"


def tables_of(probe: str, probes: dict | None) -> list[str]:
    """Tables a SQL probe reads: the data address of a failed state check."""
    sql = ((probes or {}).get(probe) or {}).get("sql") or ""
    return list(dict.fromkeys(re.findall(r"\b(?:from|join)\s+[\"`]?([A-Za-z_][\w]*)", sql, re.I)))


def _state_fact(spec: dict, ev: dict, probes: dict | None) -> dict | None:
    for path, expected in (spec or {}).items():
        v, b, err = C.state_value(ev, path)
        if err:
            continue
        ok, why = C.match(v, expected, b)
        if ok:
            continue
        probe = C._probe_of(path)
        where = store_of(probe, probes)
        out = {"probe": probe, "path": path, "store": where, "expected": trim(expected), "actual": trim(v),
               "address": {"probe": probe, "store": where, "tables": tables_of(probe, probes), "fields": []}}
        if (probes or {}).get(probe, {}).get("http"):
            h = probes[probe]["http"]
            out["address"]["api"] = h if isinstance(h, str) else f"{h.get('method', 'GET')} {h.get('path')}"
        ops = expected if isinstance(expected, dict) and any(k in C.MATCH_OPS for k in expected) else {}
        ops = {op: arg for op, arg in ops.items() if op not in C.MATCH_OPS or not C.MATCH_OPS[op](v, arg, b)[0]}   # the ones that failed
        for op, arg in ops.items():
            if op in ("contains", "includes") and isinstance(v, list) and isinstance(arg, dict):
                rec, is_new = C.closest_record(v, arg, b)
                if rec is None:
                    out["phrase"] = f"no matching record was saved in {code(path)} in the {where}"
                    out["address"]["fields"] = list(arg)
                    return out
                fields = {k: {"expected": trim(w), "actual": trim(rec.get(k, MISSING))}
                          for k, w in arg.items() if not C.field_ok(rec, k, w)}
                out.update(record=trim(rec), record_is_new=is_new, fields=fields)
                out["address"]["fields"] = list(fields)
                k = next(iter(fields), None)
                if k is None:
                    break
                e, a = arg[k], rec.get(k)
                if isinstance(e, str) and isinstance(a, str) and a and e.startswith(a) and len(a) < len(e):
                    out["phrase"] = (f"{code(f'{path}.{k}')} was saved cut short in the {where}: "
                                     f"{len(a)} of {len(e)} characters")
                else:
                    out["phrase"] = f"the {where} has {code(f'{path}.{k}')} = {_val(a)}, expected {_val(e)}"
                return out
            if op == "delta":
                try:
                    d = C._num(v) - C._num(b)
                except TypeError:
                    break
                if isinstance(arg, int):
                    noun = "record" if isinstance(v, list) else "change"
                    out["phrase"] = (f"{code(path)} got {d} new {noun}{_s(d)} in the {where}, expected {arg}"
                                     if isinstance(v, list) and d >= 0 else f"{code(path)} changed by {d:+}, expected {arg:+}")
                    return out
        if not ops:
            out["phrase"] = f"the {where} has {code(path)} = {_val(v)}, expected {_val(expected)}"
        else:
            out["phrase"] = f"{code(path)} in the {where}: {why}"
        return out
    return None


def _changes_fact(kind: str, detail: str, spec: Any, probes: dict | None) -> dict:
    paths = re.split(r",\s*", re.sub(r"^[^:]*:\s*", "", detail or "").split(" (+")[0])
    paths = [p for p in paths if p]
    probe = C._probe_of(paths[0]) if paths else ""
    where = store_of(probe, probes)
    shown = ", ".join(code(p) for p in paths[:3]) + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")
    phrase = {"allowed_changes": f"it also changed {shown} in the {where}, which this journey must not touch",
              "unchanged": f"{shown} changed in the {where}, but must stay the same",
              "changed": f"{shown} did not change in the {where}"}[kind]
    return {"probe": probe, "store": where, "paths": paths, "phrase": phrase,
            "address": {"probe": probe, "store": where, "tables": tables_of(probe, probes),
                        "fields": [p.split(".")[-1] for p in paths if "." in p]}}


def _errors_fact(spec: Any, ev: dict, steps: dict) -> dict:
    s = ev.get("sensors") or {}
    allow = [re.compile(p) for p in (spec.get("allow", []) if isinstance(spec, dict) else [])]
    rows = [{**e, "kind": "an uncaught error"} for e in s.get("page_errors", []) if e.get("phase") in ("act", "check")]
    if not isinstance(spec, dict) or spec.get("console", True):
        rows += [{**c, "kind": "a console error"} for c in s.get("console", [])
                 if c.get("type") == "error" and c.get("phase") in ("act", "check")]
    rows = [r for r in rows if not any(p.search(r.get("text", "")) for p in allow)]
    if not rows:
        return {"phrase": "the page reported an error"}
    r = rows[0]
    at = steps.get(r.get("step"))
    where = f" at step {r['step'] + 1} ({at})" if at else ""
    more = len({x.get("text") for x in rows}) - 1
    return {"errors": [trim(x.get("text"), 300) for x in rows[:5]], "step": r.get("step"),
            "stack": trim(r.get("stack") or r.get("url"), 600),
            "phrase": f"{r['kind']}{where}: {first_line(r.get('text'), 140)}" + (f" (and {more} more)" if more > 0 else "")}


def a11y_phrase(rule: str, n: int, help_text: str = "") -> str:
    """Selectors stay out of the sentence (a person cannot read `#radix-:r8:`); the brief lists them for the agent."""
    tpl = A11Y_PHRASE.get(rule)
    what = tpl.format(s=_s(n), them="it" if n == 1 else "them") if tpl else f"element{_s(n)} failing “{help_text or rule}”"
    return f"{n} {what}"


def _a11y_fact(spec: Any, ev: dict) -> dict:
    a = (ev.get("audit") or {}).get("a11y") or {}
    worst = spec.get("max_impact", "serious") if isinstance(spec, dict) else "serious"
    bad = [v for v in a.get("violations", []) if C.IMPACT_RANK.get(v.get("impact") or "minor", 3) <= C.IMPACT_RANK[worst]]
    bad.sort(key=lambda v: (C.IMPACT_RANK.get(v.get("impact") or "minor", 3), -int(v.get("nodes") or 0)))
    if not bad:
        return {"phrase": "accessibility problems on the screen"}
    v = bad[0]
    phrase = a11y_phrase(v["id"], int(v.get("nodes") or 0), v.get("help", ""))
    if len(bad) > 1:
        phrase += f", and {len(bad) - 1} more accessibility problem{_s(len(bad) - 1)}"
    return {"violations": [{"rule": x["id"], "impact": x.get("impact"), "elements": x.get("nodes"),
                            "targets": (x.get("targets") or [])[:8], "html": (x.get("html") or [])[:3], "help": x.get("help")}
                           for x in bad], "phrase": phrase}


def _check_fact(item: dict, ev: dict, probes: dict | None, steps: dict) -> dict:
    kind, spec, detail = item["kind"], item.get("spec"), item.get("detail") or ""
    fact: dict | None = None
    if kind == "state":
        fact = _state_fact(spec if isinstance(spec, dict) else {}, ev, probes)
    elif kind in ("allowed_changes", "unchanged", "changed"):
        fact = _changes_fact(kind, detail, spec, probes)
    elif kind == "no_errors":
        fact = _errors_fact(spec, ev, steps)
    elif kind == "a11y":
        fact = _a11y_fact(spec, ev)
    elif kind == "no_failed_requests":
        first = detail.split("; ")[0]
        m = re.match(r"(\w+) (\S+) (→|✗) (.+)", first)
        fact = {"request": first, "phrase": (f"{code(m[1] + ' ' + m[2])} answered {m[4]}" if m and m[3] == "→" else
                                              f"{code(m[1] + ' ' + m[2])} failed: {m[4]}" if m else f"a request failed: {first}")}
    elif kind == "visible":
        fact = {"phrase": f"{target_text(spec)} is not on the screen"}
    elif kind == "not_visible":
        fact = {"phrase": f"{target_text(spec)} is still on the screen"}
    elif kind == "text":
        m = re.search(r"(contains|not_contains|regex) (.+) failed", detail)
        fact = {"phrase": ("the page does not show " if m and m[1] != "not_contains" else "the page shows ") + (m[2] if m else detail)}
    elif kind == "url":
        fact = {"phrase": f"the journey ended on {code(ev.get('final_url') or '?')}, expected a URL matching {code(str(spec))}"}
    elif kind == "count":
        tgt = target_text({k: v for k, v in spec.items() if k not in ("eq", "gte", "lte", "gt", "lt")}) if isinstance(spec, dict) else ""
        fact = {"phrase": f"found {detail.split(' ')[0] if detail else '?'} × {tgt}: {detail}"}
    elif kind == "judge":
        fact = {"phrase": f"the AI judge: {first_line(detail, 200)}", "by": "judge"}
    elif kind in ("answer", "numbers"):
        fact = {"phrase": f"the assistant's answer: {detail}"}
    elif kind in ("tools_called", "tools_any"):
        fact = {"phrase": f"the assistant {detail}"}
    elif kind in ("perf", "step_time"):
        fact = {"phrase": f"too slow: {detail}"}
    elif kind == "vars" and isinstance(spec, dict):
        got = ev.get("vars") or {}
        bad = next(((k, w) for k, w in spec.items() if k in got and not C.match(got[k], w)[0]), None)
        if bad:
            k, w = bad
            v = got[k]
            shown = f"{len(v)} item{_s(len(v))}" if isinstance(v, list) else _val(v)
            fact = {"var": k, "expected": trim(w), "actual": trim(v),
                    "phrase": f"the API returned {code(k)} with {shown}, expected "
                              + (expect_words(w["len"]) if isinstance(w, dict) and set(w) == {"len"} and isinstance(w["len"], dict)
                                 else expect_words(w))}
        else:
            fact = {"phrase": f"the API returned {detail}"}
    elif kind == "invariant":
        fact = {"phrase": f"the app's invariant broke: {detail.replace('invariant violated: ', '')}"}
    fact = fact or {"phrase": f"{kind}: {detail}" if detail else f"the {kind} check failed"}
    fact.setdefault("phrase", f"{kind}: {detail}")
    return fact


def step_label(st: dict, raw: dict | None) -> str:
    """What a step does, as a person says it: click button “Post”, fill in the “Email” field, open `/settings`."""
    act = st.get("action") or "?"
    arg = raw.get(act) if isinstance(raw, dict) else None
    if act == "goto" and arg is not None:
        tgt = code(str(arg if not isinstance(arg, dict) else arg.get("url") or arg))
    elif act == "press" and arg is not None:
        tgt = code(str(arg if not isinstance(arg, dict) else arg.get("key")))
    elif act in ("reload", "back", "wait", "offline", "screenshot", "clear_storage"):
        tgt = " "
    else:
        tgt = target_text(arg) if arg is not None else ""
    return f"{VERB.get(act, act)} {tgt or (st.get('summary') or '')}".strip()


def _step_fact(st: dict, raw: dict | None) -> dict:
    reason = first_line(st.get("error"))
    act = st.get("action") or "?"
    return {"step": {"i": st.get("i"), "action": act, "target": trim(raw.get(act) if isinstance(raw, dict) else None, 200),
                     "error": first_line(st.get("error"), 400), "screenshot": st.get("screenshot")},
            "phrase": f"could not {step_label(st, raw)}" + (f": {reason}" if reason else "") + f" (step {int(st.get('i') or 0) + 1})"}


def failure_of(r: dict, ev: dict | None = None, probes: dict | None = None) -> dict | None:
    """The structured failure of one journey and its phrase; None for a pass.
    `r`: the journey's entry of analysis.json; `ev`: its evidence.json (state, sensors, audit), when available."""
    v = r.get("verdict")
    if v == PASS:
        return None
    ev = ev or {}
    raw_steps = ((ev.get("case") or {}).get("steps") or [])

    def raw(st):
        i = st.get("i")
        return raw_steps[i] if isinstance(i, int) and 0 <= i < len(raw_steps) else None
    steps = {st.get("i"): step_label(st, raw(st)) for st in r.get("steps", [])}
    musts = [i for i in r.get("items", []) if i.get("level") == "must"]
    if v == INCONCLUSIVE:
        cause = str(r.get("cause") or "")
        if cause.startswith("undecided:"):
            k = cause.split(":", 1)[1]
            item = next((i for i in musts if i.get("status") == "undecided"), {})
            detail = item.get("detail") or ""
            if (m := re.match(r"probe (.+?) unreadable: (.+)", detail)):      # a state probe the journey needs
                why, detail = f"the app's data {', '.join(code(x) for x in m[1].split(', '))} could not be read", m[2]
            else:
                why = UNDECIDED_WHY.get(k, f"the {k} check could not decide")
        else:
            why, detail = NOT_CHECKED.get(cause, cause or "the run did not complete"), first_line(r.get("error") or r.get("summary"), 200)
        return {"kind": "not_checked", "cause": cause, "owner": r.get("owner") or "environment", "detail": detail,
                "phrase": f"could not be checked: {why}" + (f" ({detail})" if detail and detail not in why else "")
                          + ". Not a product bug"}
    failed = [(n, i) for n, i in enumerate(musts) if i.get("status") == NOK]
    bad_step = next((s for s in r.get("steps", []) if s.get("ok") is False), None)
    use_step = r.get("cause") in ("step_failed", "goal_not_reached") and not (failed and failed[0][1].get("kind") != "no_errors")
    if use_step and bad_step:
        out = {"kind": "step", **_step_fact(bad_step, raw(bad_step))}
    elif r.get("cause") == "goal_not_reached":
        out = {"kind": "goal", "phrase": f"the agent could not complete the goal: {first_line(r.get('error'), 200)}"}
    elif failed:
        n, item = failed[0]
        out = {"kind": item["kind"], "check": {"level": "must", "index": n, "kind": item["kind"], "spec": trim(item.get("spec"), 400),
                                               "detail": trim(item.get("detail"), 600)},
               **_check_fact(item, ev, probes, steps)}
        if bad_step:
            out["step"] = _step_fact(bad_step, raw(bad_step))["step"]
    else:
        out = {"kind": "other", "phrase": first_line(r.get("summary"), 200) or "the journey failed"}
    out["also"] = [i["kind"] for _, i in failed[1:]]
    return out


def fill_failures(a: dict, evidence) -> dict:
    """Analyses from before structured failures: build them from each journey's evidence (`evidence(case_id) → dict`)."""
    for c in a.get("cases", []):
        if c.get("verdict") != PASS and not c.get("failure"):
            c["failure"] = failure_of(c, evidence(c["case_id"]))
    return a


def check_label(kind: str, spec: Any) -> str:
    """What a check promises, for a check that held: "no errors in the page", "changes only `comments`"."""
    def paths(x):
        return ", ".join(code(str(p)) for p in (x if isinstance(x, list) else [x])) or "nothing"
    if kind == "no_errors":
        return "no errors in the page" + (" (some allowed)" if isinstance(spec, dict) and spec.get("allow") else "")
    if kind == "no_failed_requests":
        return "no failed requests to the app"
    if kind == "allowed_changes":
        return f"changes only {paths(spec)} in the app's data"
    if kind == "unchanged":
        return f"leaves {paths(spec)} as they were"
    if kind == "changed":
        return f"changes {paths(spec)}"
    if kind == "state" and isinstance(spec, dict):
        return "the app's data: " + "; ".join(f"{code(p)} {expect_words(v)}" for p, v in list(spec.items())[:3])
    if kind == "visible":
        return f"{target_text(spec)} is on the screen"
    if kind == "not_visible":
        return f"{target_text(spec)} is not on the screen"
    if kind == "text":
        v = spec if isinstance(spec, str) else (spec or {}).get("contains") or (spec or {}).get("regex")
        return f"the page shows “{v}”" if isinstance(v, str) else "the page text is as expected"
    if kind == "url":
        return f"ends on a URL matching {code(str(spec))}"
    if kind == "a11y":
        worst = spec.get("max_impact", "serious") if isinstance(spec, dict) else "serious"
        return f"no {worst} or worse accessibility problems"
    if kind in ("perf", "step_time"):
        return "fast enough: " + (", ".join(f"{k} ≤ {v}" for k, v in spec.items()) if isinstance(spec, dict) else f"steps ≤ {spec} ms")
    if kind == "judge":
        crit = spec if isinstance(spec, str) else (spec or {}).get("criterion") or ""
        return f"the AI judge agrees: “{crit}”"
    if kind == "vars" and isinstance(spec, dict):
        return "the API returns " + "; ".join(f"{code(k)} {expect_words(v)}" for k, v in list(spec.items())[:3])
    return f"{kind.replace('_', ' ')}: {code(json.dumps(spec, ensure_ascii=False, default=str)[:120])}"


def expect_words(expected: Any) -> str:
    """A matcher in words: {len: {lte: 1}} → "has at most 1 item", {delta: 1} → "grows by 1", "x" → "= "x""."""
    words = {"eq": "= {}", "ne": "≠ {}", "gt": "> {}", "gte": "at least {}", "lt": "< {}", "lte": "at most {}",
             "contains": "contains {}", "includes": "contains {}", "not_contains": "does not contain {}",
             "excludes": "does not contain {}", "regex": "matches {}", "any_of": "is one of {}",
             "includes_all": "contains all of {}", "every": "every item matches {}"}
    if not isinstance(expected, dict) or not any(k in C.MATCH_OPS for k in expected):
        return f"= {_val(expected, 60)}"
    out = []
    for op, arg in expected.items():
        if op == "len":
            out.append("with " + (expect_words(arg).replace("= ", "") if isinstance(arg, dict) else str(arg)) + " item(s)")
        elif op == "delta":
            out.append(f"grows by {arg}" if isinstance(arg, int) and arg >= 0 else f"changes by {arg}")
        elif op == "exists":
            out.append("is present" if arg else "is absent")
        elif op == "unchanged":
            out.append("stays the same" if arg else "changes")
        elif op in words:
            out.append(words[op].format(_val(arg, 60)))
    return " and ".join(out)


def journey_phrase(r: dict) -> str:
    """The journey and what went wrong; a failure that ran a second time says what that run found."""
    f = r.get("failure") or {}
    out = f"{subject(r)}: {f['phrase']}" if f.get("phrase") else f"{subject(r)}: {first_line(r.get('summary'), 200)}"
    again = SECOND_RUN.get((r.get("confirm") or {}).get("verdict")) if r.get("verdict") == FAIL else None
    return f"{out.rstrip('.')}. {again}" if again else out


# ---- sensor findings -------------------------------------------------------------------------------------------------------

def issue_phrase(i: dict) -> str:
    rule, where, msg = i.get("rule", ""), i.get("where") or "", first_line(i.get("message"), 140)
    on = f" on {code(where)}" if where and not where.startswith(("GET ", "POST ", "PUT ", "PATCH ", "DELETE ")) else ""
    if rule.startswith("A11Y-"):
        n = max([int(x) for x in re.findall(r"(\d+) element", i.get("message") or "")] or [1])
        return a11y_phrase(rule[5:], n, i.get("title", "")) + on
    if rule == "REL-001":
        return f"an uncaught error in the page{on}: {msg}"
    if rule == "REL-002":
        return f"a console error{on}: {msg}"
    if rule in ("REL-003", "REL-004"):
        return f"{code(where)} answered {msg}"
    if rule == "REL-005":
        return f"{code(where)} failed: {msg}"
    if rule == "UX-004":
        return f"a blank screen{on}"
    if rule == "CASE-FAIL":
        return i.get("title") or "a journey failed"
    return f"{i.get('title') or rule}{on}" + (f": {msg}" if msg else "")


# ---- the verdict ----------------------------------------------------------------------------------------------------------

def cond_id(c: dict) -> str:
    """A gate condition's id; analyses from before ids existed name it only."""
    if c.get("id"):
        return c["id"]
    name = str(c.get("name") or "")
    if name == "New failed journeys":
        return "new_failed_journeys"
    if name == "Failed journeys":
        return "failed_journeys"
    if name.startswith("New ") and name.endswith(" issues"):
        return "new_issues:" + name[4:-7]
    return name.lower()


def _cond(g: dict, cid: str) -> dict | None:
    return next((c for c in g.get("conditions", []) if cond_id(c) == cid), None)


def known_of(a: dict) -> set[str]:
    """Journeys failing the expected way; analyses from before the field: every failing `known-bug` journey."""
    if "known_bugs" in a:
        return set(a["known_bugs"] or [])
    return {c["case_id"] for c in a.get("cases", []) if "known-bug" in (c.get("tags") or []) and c["verdict"] == FAIL}


def flaky_of(a: dict) -> set[str]:
    """Journeys that failed and passed on a second run from a fresh app: they block nothing (assay/quality.py `flaky`)."""
    from .quality import flaky
    return flaky(a.get("cases", []))


def verdict(a: dict) -> dict:
    """The answer to "do we ship it?" for a person: state, a title, the headline (the most important problem in one
    sentence), what the person must do, and the problems ranked. Pure function of analysis.json."""
    g, cases = a["gate"], a.get("cases", [])
    st = GATE_STATE.get(g["status"], "unsure")
    first = bool(g.get("first_analysis"))
    ch = a.get("changes") or {}
    known, flaky = known_of(a), flaky_of(a)
    pr_mode = _cond(g, "new_failed_journeys") is not None
    failing = [c for c in cases if c["verdict"] == FAIL and c["case_id"] not in known | flaky]
    regress = set(ch.get("regressions", []))
    counts_journeys = pr_mode or _cond(g, "failed_journeys") is not None or not g.get("conditions")
    broke = ([c for c in failing if c["case_id"] in regress] if pr_mode else failing) if counts_journeys else []
    already = [c for c in failing if c not in broke] if pr_mode else []
    failed = lambda base: {cond_id(c).split(":", 1)[1] for c in g.get("conditions", [])
                           if cond_id(c).startswith("new_issues:") and not c["ok"] and bool(c.get("baseline")) == base}
    sevs, base_sevs = failed(False), failed(True)        # base: no earlier analysis, so these are what the product had
    shown = {c["case_id"] for c in broke}
    told = {"A11Y-" + v.get("rule", "") for c in broke for v in (c.get("failure") or {}).get("violations") or []} | {
        "A11Y-" + m for c in broke for i in c.get("items", []) if i.get("kind") == "a11y" and i.get("status") == NOK
        for m in re.findall(r"([\w-]+) \((?:critical|serious|moderate|minor)", i.get("detail") or "")}
    issues = [i for i in a.get("issues", []) if i.get("rule") != "CASE-FAIL" and i.get("new", True) and i.get("severity") in sevs | base_sevs
              and not (i.get("sources") and set(i["sources"]) <= shown | known | flaky)    # a broken journey says it; a
              and i.get("rule") not in told]                                               # known bug's symptom is known
    dims = {cond_id(c).split(":", 1)[1] for c in g.get("conditions", [])
            if cond_id(c).startswith("rating:") and not c["ok"] and not c.get("soft")}      # a quality got worse
    issues += [i for i in a.get("issues", []) if i.get("dimension") in dims and i.get("new", True)
               and i.get("rule") != "CASE-FAIL" and i not in issues]
    issues.sort(key=lambda i: SEV_RANK.get(i.get("severity"), 9))
    weak = a.get("weakened") or {}
    weakened = [w for w in weak.get("items", [])] if not weak.get("approved") else []
    approved = [w for w in weak.get("items", [])] if weak.get("approved") else []
    not_checked = [c for c in cases if c["verdict"] == INCONCLUSIVE]
    crit = {d.get("id") for d in a.get("dataset") or [] if d.get("critical")}
    broke.sort(key=lambda c: (c["case_id"] not in crit and not c.get("critical"), SEV_RANK.get(c.get("severity"), 9)))

    problems = ([{"kind": "journey", "case_id": c["case_id"], "phrase": journey_phrase(c), "blocking": True} for c in broke]
                + [{"kind": "issue", "rule": i["rule"], "phrase": issue_phrase(i), "blocking": i.get("severity") not in base_sevs
                    or i.get("severity") in sevs} for i in issues]
                + [{"kind": "weakened", "case_id": w.get("case_id"), "phrase": w["phrase"], "blocking": True} for w in weakened])
    if first:                            # nothing blocks: what exists already, as warnings
        problems = [{**p, "blocking": False} for p in problems]
    existing = first or any(not p["blocking"] for p in problems)     # what the product already had: listed, not blocking
    blocking_issues = [i for i in issues if i.get("severity") in sevs]
    background = ([{"kind": "journey", "case_id": c["case_id"], "phrase": journey_phrase(c), "blocking": False,
                    "note": "fails on the target too"} for c in already]
                  + [{"kind": "not_checked", "case_id": c["case_id"], "phrase": journey_phrase(c), "blocking": False}
                     for c in not_checked]
                  + [{"kind": "flaky", "case_id": c["case_id"], "phrase": f"{subject(c)}: {FLAKY_NOTE}", "blocking": False}
                     for c in cases if c["case_id"] in flaky]                 # named, not described: it blocks nothing
                  + [{"kind": "weakened", "case_id": w.get("case_id"), "phrase": w["phrase"], "blocking": False,
                      "note": "approved with `assay-approved`"} for w in approved])

    n = len(cases)
    scope = dict(a.get("scope") or {})
    scope.setdefault("checked", n)
    scope.setdefault("total", n)
    if st == "no_ship":
        parts = ([f"{len(broke)} journey{_s(len(broke))} {'broke' if pr_mode else 'fail' + ('s' if len(broke) == 1 else '')}"]
                 if broke else []) \
            + ([f"{len(blocking_issues)} new problem{_s(len(blocking_issues))}"] if blocking_issues else []) \
            + (["checks weakened"] if weakened else [])
        title = ", ".join(parts) or "a gate condition failed"
        top = problems[0]["phrase"] if problems else "a gate condition failed"
    elif st == "unsure":
        if n == 0:
            title, top = "no journeys ran", "Nothing was executed, so nothing was checked. Not a product bug."
        elif not_checked:
            title = f"{len(not_checked)} of {n} journey{_s(n)} could not be checked"
            top = journey_phrase(not_checked[0])
        else:
            dims = [c["name"].replace(" rating (new)", "") for c in g.get("conditions", []) if c.get("actual") == "not measured"]
            title = f"{', '.join(dims) or 'a gate condition'} was not measured"
            top = f"The gate asks for {', '.join(dims)}, but no check measured it. Not a product bug."
    else:
        fixed = ch.get("fixed", [])
        title = ("nothing blocks yet (first analysis)" if existing and problems else
                 "nothing blocks" if failing and not counts_journeys else
                 f"nothing broke, {len(fixed)} journey{_s(len(fixed))} fixed" if fixed else "nothing broke")
        if existing and problems:
            top = f"First analysis: {len(problems)} problem{_s(len(problems))} already exist{'s' if len(problems) == 1 else ''}. " \
                  f"{problems[0]['phrase']}"
        else:                             # never "all pass" when something did not pass
            titles = {c["case_id"]: subject(c) for c in cases}
            uncounted = [] if counts_journeys else failing       # gate.fail_on_case_fail: false
            notes = ([f"fixed: {'; '.join(titles.get(x, x) for x in fixed[:3])}"] if fixed else []) \
                + ([f"{len(failing)} journey{_s(len(failing))} fail{'s' if len(failing) == 1 else ''}, but this gate does not "
                    "count failing journeys (gate.fail_on_case_fail: false)"] if uncounted else []) \
                + ([f"{len(already)} journey{_s(len(already))} already fail{'s' if len(already) == 1 else ''} on the target branch"]
                   if already else []) \
                + ([f"{len(not_checked)} could not be checked (not a product bug)"] if not_checked else []) \
                + ([f"{len(known)} known bug{_s(len(known))} still fail{'s' if len(known) == 1 else ''}"] if known else []) \
                + ([f"{len(flaky)} journey{_s(len(flaky))} failed once and passed on a second run: not blocking"] if flaky else [])
            top = ("Nothing broke; " + "; ".join(notes)) if notes else (
                (f"All {n} journeys checked pass." if n > 1 else "The journey checked passes.") if n else
                "No journeys ran; the screen scan found nothing that blocks.")

    if weakened:
        action = "decide"
        act_text = (f"decide: this change weakens checks ({weakened[0]['phrase']}"
                    + (f", and {len(weakened) - 1} more" if len(weakened) > 1 else "")
                    + "). Approve with the label `assay-approved`, or restore them.")
    elif st == "unsure":
        action, act_text = "look", "look: the check did not complete. This is not a product bug."
    elif existing and problems and st != "no_ship":
        action, act_text = "look", "look: these problems already exist; nothing blocks this change."
    elif st == "no_ship":
        action, act_text = "none", "nothing, your agent can fix it from the brief."
    else:
        action, act_text = "none", "nothing."

    titles = {c["case_id"]: subject(c) for c in cases}
    case_ids = {c["case_id"] for c in cases}
    ds_ids = [d.get("id") for d in a.get("dataset") or []]
    ref_ids = set(a.get("reference_ids") or [])
    return {
        "state": st, "icon": STATE_ICON[st], "label": STATE_LABEL[st], "title": title, "headline": _sentence(top),
        "action": action, "action_text": act_text, "problems": problems, "background": background,
        "scope": scope,
        "changes": {"broke": [c["case_id"] for c in broke], "fixed": list(ch.get("fixed", [])),
                    "new_journeys": [x for x in ds_ids if ref_ids and x not in ref_ids and x in case_ids],
                    "known_failing": sorted(known), "flaky": sorted(flaky),
                    "titles": {k: titles[k] for k in titles if k in regress | set(ch.get("fixed", []))},
                    "known_fixed": [c["case_id"] for c in cases if "known-bug" in (c.get("tags") or []) and c["verdict"] == PASS]},
    }


def _sentence(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return s
    s = s[0].upper() + s[1:]
    return s if s.endswith((".", "!", "?", "…")) else s + "."


def markup_html(text: str) -> str:
    """Escape a phrase for HTML and render its `code` spans."""
    import html
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(text or ""))
