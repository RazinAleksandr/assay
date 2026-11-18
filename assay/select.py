"""Which journeys a pull request runs: the ones its change can break, plus a safety net. The main branch runs all.

    select: {mode: auto, sample: 0.1, budget_min: 10}      # assay.yaml; `mode: all` turns it off
    groups: {feed: {paths: ["server/feed.ts", "web/src/Feed*"]}}   # optional: who owns files no journey is mapped to

In this order:
  1. no journey map in the reference analysis → every journey;
  2. always: `critical: true` journeys, journeys this change adds or edits, journeys that failed on the reference;
  3. from the reference's journey map: frontend files that ran in a journey, API routes whose handlers the change
     touches, tables whose data a journey changed, texts its steps aim at;
  4. a changed source file none of that links to a journey: the journeys of the group whose `paths` own it, else the
     ones that reach it through code importing it, two levels up (a helper → the route file → the app): those that ran
     an importer, and those that call a route an importer declares;
  5. still unlinked, a change to something everything depends on (lock files, build and CI config, assay.yaml, auth
     and middleware, global styles) or a change that cannot be read → every journey (unknown impact is not "no impact");
  6. a rotating sample of the rest (10 % by default), so a gap in the map shows up within a few pull requests;
  7. `budget_min` is a hard cap on the estimated time (the reference's durations, 5 s for a journey it did not run).
     Over it, the sample goes first, then the rest of "every journey" (in a seeded shuffle, so different pull requests
     drop different journeys), journeys not mapped yet, the ones linked by group or import, the ones the change
     touches directly. Critical, new or edited and failing journeys always run; the summary says how many were dropped.
The verdict and the brief say how many of how many ran and why each one did.
"""
from __future__ import annotations

import fnmatch
import json
import random
import re
from collections.abc import Callable
from pathlib import Path

from .code import Hunk, Repo, changed_files, enclosing_routes, is_source, parse_diff, repo_root, route_matches
from .model import group_of

GLOBAL = ["package.json", "*lock*", "*.lock", "pnpm-workspace.yaml", "requirements*.txt", "pyproject.toml", "Pipfile*",
          "go.mod", "go.sum", "Gemfile*", "Cargo.*", "Dockerfile*", "docker-compose*", ".env*", "*.config.*", "*.conf",
          "tsconfig*.json", "vite.config.*", "next.config.*", "webpack.*", ".github/*", ".gitlab-ci.yml",
          ".assay/assay.yaml", ".assay/*.py", ".assay/*.mjs", ".assay/*.js", "*/middleware*", "*middleware.*", "*/auth/*",
          "*auth.*", "*session*", "*/layout.*", "_app.*", "_document.*", "global*.css", "globals.css", "index.css",
          "reset.css", "theme.*", "tailwind.config.*"]

# why a journey runs, from what a time budget drops first to what it never drops
SAMPLE, REST, UNMAPPED, LINKED, TOUCHED, KEEP = range(6)


def _matches(path: str, globs: list[str]) -> bool:
    """A glob on the path, on the file name, or on any tail of the path (`web/src/Feed*`, `*.lock`, `theme.*`)."""
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(name, g) or fnmatch.fnmatch(path, "*/" + g) for g in globs)


def _def(entry: dict, like: dict | None = None) -> str:
    """A journey as written, for "edited in this change"; only the fields the reference recorded (older analyses
    have no steps), so an upgrade does not make every journey look edited."""
    keys = [k for k in ("must", "should", "tags", "critical", "promise", "journey") if like is None or k in like]
    return json.dumps({k: entry.get(k) for k in keys}, sort_keys=True, default=str)


def _callers(jmap: dict) -> Callable[[list[tuple[str, str]]], dict[str, str]]:
    """declared routes → {journey: the first of them it calls}; each distinct call is matched once, not per journey."""
    calls: dict[tuple[str, str], list[str]] = {}
    for cid, e in jmap.items():
        for r in e.get("routes") or []:
            if " " in r:
                calls.setdefault(tuple(r.split(" ", 1)), []).append(cid)

    def find(decls: list[tuple[str, str]]) -> dict[str, str]:
        out: dict[str, str] = {}
        for d in decls:
            for c, cids in calls.items():
                if route_matches(d, c):
                    for cid in cids:
                        out.setdefault(cid, f"{d[0] + ' ' if d[0] else ''}{d[1]}")
        return out
    return find


def select(dataset: list[dict], reference: dict | None, diff_text: str | None, cfg: dict | None = None,
           root: Path | None = None, seed: str = "", groups: dict | None = None) -> dict:
    """{"ids": [...] or None for all, "reasons": {id: why}, "why": summary, "skipped": ids over the time budget}.
    `dataset`: Case.entry() of every journey; `reference`: the target branch's analysis (its journey map, dataset,
    results); `groups`: assay.yaml `groups`, {name: {paths: [globs]}}."""
    cfg = cfg or {}
    ids = [d["id"] for d in dataset]

    def everything(why: str) -> dict:
        return {"ids": None, "reasons": {}, "why": why, "skipped": []}
    if cfg.get("mode", "auto") == "all":
        return everything("every journey (select: all)")
    jmap = (reference or {}).get("journey_map")
    if not jmap:
        return everything("every journey: the target branch has no journey map yet")
    budget = float(cfg.get("budget_min") or 0)
    hunks = parse_diff(diff_text)
    files = changed_files(hunks, diff_text)
    globs = GLOBAL + list(cfg.get("global") or [])
    g = next((f for f in files if _matches(f, globs)), None)
    whole = ("the change could not be read" if diff_text is None else "the change has no files" if not files else
             f"`{g}` affects the whole app" if g else None)       # why every journey must run, if one must
    if whole and not budget:
        return everything("every journey: " + whole)               # with a budget, rank them to keep the likeliest

    root = root or repo_root()
    reasons: dict[str, str] = {}
    tier: dict[str, int] = {}

    def pick(cid: str, why: str, t: int) -> None:
        if cid in ids and t > tier.get(cid, -1):                   # the strongest reason is the one shown
            reasons[cid], tier[cid] = why, t
    ref_ds = {d.get("id"): d for d in (reference or {}).get("dataset") or []}
    for d in dataset:
        if d.get("critical"):
            pick(d["id"], "critical", KEEP)
        if ref_ds and d["id"] not in ref_ds:
            pick(d["id"], "new in this change", KEEP)
        elif ref_ds and _def(ref_ds[d["id"]], ref_ds[d["id"]]) != _def(d, ref_ds[d["id"]]):
            pick(d["id"], "edited in this change", KEEP)
    for c in (reference or {}).get("cases", []):
        if c.get("verdict") == "FAIL":
            pick(c["case_id"], "failed on the target branch", KEEP)

    for cid in ids:                        # no map entry (new, or not checked on the target): nothing tells us it is safe
        e = jmap.get(cid)
        if cid not in reasons and (not e or not any(e.get(k) for k in ("routes", "files", "tables", "pages"))):
            pick(cid, "not mapped yet", UNMAPPED)
    ran: dict[str, list[str]] = {}
    for cid, e in jmap.items():
        for f in e.get("files") or []:
            ran.setdefault(f, []).append(cid)
    callers = _callers(jmap)
    by_file: dict[str, list[Hunk]] = {}
    for h in hunks:
        by_file.setdefault(h.file, []).append(h)
    unmapped = []
    for f in files:
        if f.startswith(".assay/") or not is_source(f) or _matches(f, globs):
            continue                       # journeys' own files are compared above; docs and tests do not run in the app
        for cid in ran.get(f, []):
            pick(cid, f"`{f}` ran in it", TOUCHED)
        if f in ran:
            continue                       # coverage is per file: every journey that ran it is picked
        file_hunks = by_file.get(f) or []
        if not file_hunks:                 # binary, mode-only or an empty file: no text to link
            unmapped.append((f, None))
            continue
        for h in file_hunks:               # each hunk must link to a journey, else its impact is unknown
            hit = False
            for cid, route in callers(enclosing_routes(h, root)).items():
                pick(cid, f"calls `{route}`, changed in `{f}`", TOUCHED)
                hit = True
            for cid, e in jmap.items():
                tabs = [t for t in e.get("tables", []) if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", h.code)]
                if tabs:
                    pick(cid, f"changes data in `{tabs[0]}`, which `{f}` touches", TOUCHED)
                    hit = True
                txt = [t for t in e.get("targets", []) if len(t) >= 4 and
                       re.search(rf"[\"'`>]\s*{re.escape(t)}\s*[\"'`<]", h.code)]
                if txt:
                    pick(cid, f"aims at “{txt[0]}”, changed in `{f}`", TOUCHED)
                    hit = True
            if not hit and h.code.strip():
                unmapped.append((f, h.start))

    repo = Repo(root)
    linked = set()
    for f in dict.fromkeys(f for f, _ in unmapped):       # nothing links it: who owns it, who imports it
        owners = {name for name, spec in (groups if isinstance(groups, dict) else {}).items()
                  if isinstance(spec, dict) and _matches(f, [str(x) for x in spec.get("paths") or []])}
        for d in dataset:
            if (name := d.get("group") or group_of(d.get("source") or "")) in owners:
                pick(d["id"], f"group `{name}` owns `{f}`", LINKED)
                linked.add(f)
        if f in linked:
            continue
        seen, level = {f}, [(f, "")]
        for _ in range(2):                 # the importers, then theirs: a helper → the route file → the app
            nxt = []
            for target, via in level:
                for imp in repo.importers(target):
                    if imp in seen:
                        continue
                    seen.add(imp)
                    chain = f"{via} and `{imp}`" if via else f"`{imp}`"
                    nxt.append((imp, chain))
                    for cid in ran.get(imp, []):
                        pick(cid, f"uses `{f}` through {chain}, which ran in it", LINKED)
                        linked.add(f)
                    for cid, route in callers(repo.routes(imp)).items():
                        pick(cid, f"uses `{f}` through {chain} (`{route}`)", LINKED)
                        linked.add(f)
            level = nxt
    still = [(f, line) for f, line in unmapped if f not in linked]
    if still and not whole:
        f, line = still[0]
        whole = (f"no journey is known to use `{f}`" + (f" (line {line})" if line else "")
                 + (f" and {len(still) - 1} more place(s)" if len(still) > 1 else ""))
        if not budget:
            return everything("every journey: " + whole)

    rest = [i for i in ids if i not in reasons]
    if whole:
        for cid in rest:
            pick(cid, "every journey: " + whole, REST)
    else:
        share = float(cfg.get("sample", 0.1))
        n = min(len(rest), max(1 if rest and share > 0 else 0, round(len(rest) * share)))
        for cid in random.Random(seed or "assay").sample(rest, n):
            pick(cid, "safety sample", SAMPLE)
    skipped = []
    if budget:
        dur = {c["case_id"]: float(c.get("duration_s") or 0) for c in (reference or {}).get("cases", [])}
        total = sum(dur.get(i, 5.0) for i in reasons) / 60
        shuffled = {cid: i for i, cid in enumerate(random.Random(f"{seed}/budget").sample(ids, len(ids)))}
        for cid in sorted((i for i in reasons if tier[i] < KEEP), key=lambda i: (tier[i], shuffled[i])):
            if total <= budget:
                break
            total -= dur.get(cid, 5.0) / 60
            if tier[cid] > SAMPLE:         # a smaller safety sample is not a journey that should have run
                skipped.append(cid)
            del reasons[cid]
    if whole and not skipped:
        return everything("every journey: " + whole)
    picked = [i for i in ids if i in reasons]
    kinds = {"critical": "critical", "new in this change": "new or edited", "edited in this change": "new or edited",
             "not mapped yet": "not mapped yet",
             "failed on the target branch": "failing on the target", "safety sample": "sampled"}
    rest_kind = f"more since {whole}"
    count: dict[str, int] = {}
    for i in picked:
        k = rest_kind if tier[i] == REST else kinds.get(reasons[i], "touched by this change")
        count[k] = count.get(k, 0) + 1
    order = ["touched by this change", "critical", "new or edited", "not mapped yet", "failing on the target", rest_kind,
             "sampled"]
    why = ", ".join([f"{count[k]} {k}" for k in order if count.get(k)]
                    + ([f"{len(skipped)} over the {budget:g}-min budget"] if skipped else []))
    return {"ids": picked, "reasons": {i: reasons[i] for i in picked}, "why": why,
            "skipped": [i for i in ids if i in set(skipped)]}
