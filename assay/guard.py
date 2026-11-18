"""Weakened checks: a change that makes the gate easier to pass instead of making the product right.

Agents optimise for the gate they are given; the cheapest way to green is to delete the check that fails. So every
analysis compares its journeys (as written in .assay/, not expanded) and the gate settings with the reference's and
lists what got weaker: a journey removed, a `must` check removed or moved to `should`, a new `known-bug` tag, errors or
data changes newly allowed, a speed limit relaxed, new `ignore`/`volatile` entries, a looser gate. On a pull request
this blocks until a person approves it (label `assay-approved`), unless `gate.block_on_weakened: false`.
"""
from __future__ import annotations

import json
from collections import Counter

from .checks import check_name
from .quality import KNOWN_BUG

APPROVE_LABEL = "assay-approved"


def _kinds(items: list) -> Counter:
    out = Counter()
    for it in items or []:
        try:
            out[check_name(it)] += 1
        except ValueError:
            pass
    return out


def _spec(items: list, kind: str) -> list:
    out = []
    for it in items or []:
        try:
            if check_name(it) == kind:
                out.append(it[kind])
        except ValueError:
            pass
    return out


def _num_limits(specs: list) -> dict[str, float]:
    out: dict[str, float] = {}
    for spec in specs:
        items = {"value": spec} if isinstance(spec, (int, float)) else (spec if isinstance(spec, dict) else {})
        for k, v in items.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = max(out.get(k, v), v)
    return out


def _entries(v) -> set[str]:
    """`ignore` is a dict of lists, `volatile` a list: the set of their entries."""
    if isinstance(v, dict):
        return {f"{k}:{json.dumps(x, sort_keys=True)}" for k, xs in v.items() for x in (xs if isinstance(xs, list) else [xs])}
    return {json.dumps(x, sort_keys=True) for x in v or []}


def journey_changes(ref: dict, cur: dict) -> list[str]:
    """How one journey's checks got weaker or changed (phrases), comparing the entries of two datasets. A `must` check
    that is not byte-identical, or different steps, can hide a weaker gate (`state: {posts: {len: 1}}` → `{}`, a click
    replaced by an API call), and nobody can tell stricter from looser in general: a person decides."""
    cid, out = cur["id"], []
    dump = lambda x: json.dumps(x, sort_keys=True, default=str)
    rm, cm = _kinds(ref.get("must")), _kinds(cur.get("must"))
    ref_must = [dump(x) for x in ref.get("must") or []]
    cur_must = {dump(x) for x in cur.get("must") or []}
    now_should = {dump(x) for x in cur.get("should") or []}
    moved = Counter()
    for x in ref_must:
        if x not in cur_must and x in now_should:
            moved[next(iter(json.loads(x)), "?")] += 1
    for kind, n in rm.items():
        lost = n - cm.get(kind, 0)
        if moved.get(kind):
            out.append(f"moved {moved[kind]} `{kind}` check{'s' if moved[kind] > 1 else ''} of `{cid}` from must to should")
        if lost - moved.get(kind, 0) > 0:
            k = lost - moved.get(kind, 0)
            out.append(f"removed {k} `{kind}` check{'s' if k > 1 else ''} from `{cid}`")
    edited = Counter(next(iter(json.loads(x)), "?") for x in ref_must if x not in cur_must and x not in now_should)
    for kind, n in edited.items():
        extra = n - max(0, rm.get(kind, 0) - cm.get(kind, 0))          # beyond the ones counted as removed
        if extra > 0 and kind not in ("no_errors", "allowed_changes", "perf", "step_time"):
            out.append(f"changed {extra} `{kind}` check{'s' if extra > 1 else ''} of `{cid}`")
    if dump((ref.get("journey") or {})) != dump((cur.get("journey") or {})) and ref.get("journey") is not None:
        out.append(f"changed the steps or start state of `{cid}`")
    if KNOWN_BUG in (cur.get("tags") or []) and KNOWN_BUG not in (ref.get("tags") or []):
        out.append(f"tagged `{cid}` as a known bug (its failure stops blocking)")
    for a, b in zip(_spec(ref.get("must"), "no_errors"), _spec(cur.get("must"), "no_errors")):
        ra = set(a.get("allow", [])) if isinstance(a, dict) else set()
        rb = set(b.get("allow", [])) if isinstance(b, dict) else set()
        if rb - ra or (isinstance(b, dict) and b.get("console") is False and not (isinstance(a, dict) and a.get("console") is False)):
            out.append(f"allowed more errors in `{cid}`")
    ra = {p for s in _spec(ref.get("must"), "allowed_changes") for p in (s if isinstance(s, list) else [s])}
    rb = {p for s in _spec(cur.get("must"), "allowed_changes") for p in (s if isinstance(s, list) else [s])}
    if ra and rb - ra:
        out.append(f"allowed `{cid}` to change more data ({', '.join(f'`{p}`' for p in sorted(rb - ra)[:3])})")
    for kind in ("perf", "step_time"):
        a, b = _num_limits(_spec(ref.get("must"), kind)), _num_limits(_spec(cur.get("must"), kind))
        if any(b.get(k, 0) > v for k, v in a.items()):
            out.append(f"relaxed a speed limit of `{cid}`")
    return out


def settings_changes(ref: dict | None, cur: dict | None) -> list[str]:
    """Gate and noise settings of assay.yaml that got looser, compared as the gate applies them (defaults included)."""
    from .config import DEFAULT_GATE
    from .quality import CONFIRM_MAX, RATING_ORDER
    ref, cur, out = ref or {}, cur or {}, []
    for key, what in (("ignore", "ignored findings"), ("volatile", "volatile data paths")):
        if _entries(cur.get(key)) - _entries(ref.get(key)):
            out.append(f"added {what} in assay.yaml")
    ga, gb = {**DEFAULT_GATE, **(ref.get("gate") or {})}, {**DEFAULT_GATE, **(cur.get("gate") or {})}
    if ga.get("fail_on_case_fail", True) and gb.get("fail_on_case_fail", True) is False:
        out.append("made failing journeys not block (gate.fail_on_case_fail: false)")
    if float(gb.get("max_inconclusive", 0.2)) > float(ga.get("max_inconclusive", 0.2)):
        out.append("allowed more journeys to go unchecked (gate.max_inconclusive)")
    if int(gb.get("min_cases", 1)) < int(ga.get("min_cases", 1)):
        out.append("lowered the number of journeys that must run (gate.min_cases)")
    for sev, lim in (ga.get("max_new") or {}).items():
        if (gb.get("max_new") or {}).get(sev, float("inf")) > lim:
            out.append(f"allowed more new {sev} problems (gate.max_new)")
    for dim, worst in (ga.get("min_rating") or {}).items():
        now = (gb.get("min_rating") or {}).get(dim)
        if now is None or (now in RATING_ORDER and worst in RATING_ORDER and RATING_ORDER.index(now) > RATING_ORDER.index(worst)):
            out.append(f"relaxed the {dim} rating the gate requires (gate.min_rating)")
    if ga.get("block_on_weakened", True) and gb.get("block_on_weakened", True) is False:
        out.append("stopped blocking on weakened checks")
    if gb.get("confirm", True) and (not ga.get("confirm", True)
                                    or int(gb.get("confirm_max", CONFIRM_MAX)) > int(ga.get("confirm_max", CONFIRM_MAX))):
        out.append("let more failures pass on a second run (gate.confirm)")      # a flaky failure blocks nothing
    return out


def weakened(reference: dict | None, dataset: list[dict], settings: dict | None) -> list[dict]:
    """Everything that got weaker since the reference analysis: [{case_id?, phrase}]."""
    if not reference or not reference.get("dataset"):
        return []
    ref = {d["id"]: d for d in reference["dataset"]}
    cur = {d["id"]: d for d in dataset}
    out = [{"case_id": cid, "phrase": f"removed journey `{cid}`" + (f" ({ref[cid].get('promise') or ref[cid].get('title')})"
                                                                     if ref[cid].get("promise") or ref[cid].get("title") else "")}
           for cid in ref if cid not in cur]
    for cid, d in cur.items():
        if cid in ref:
            out += [{"case_id": cid, "phrase": p} for p in journey_changes(ref[cid], d)]
    out += [{"case_id": None, "phrase": p} for p in settings_changes(reference.get("settings"), settings)]
    return out
