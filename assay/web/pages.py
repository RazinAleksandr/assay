"""Pages of the Assay server, for a person, not a QA engineer. Every page is rendered from the analyses CI uploaded.

A pull request answers "do we ship it?" in five seconds and shows the evidence in thirty: the screens, the data the
app saved against what the journey expected, and the code of the change it suspects. A branch answers "is the
product healthy, and what is not checked at all?". Engine detail (every check, sensor, step log) is for agents, in
the brief (JSON at /api/analyses/<id>/brief), not on these pages.
"""
from __future__ import annotations

import json
import re
import statistics
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote, urlencode

from ..explain import (FLAKY_NOTE, SECOND_RUN, a11y_phrase, check_label, cond_id, fill_failures, first_line, flaky_of, issue_phrase,
                       journey_phrase, known_of, markup_html, step_label, subject)
from ..explain import verdict as decide
from ..model import DIMENSIONS, SEV_RANK, SEVERITIES
from ..report import duration_s, scope_line
from ..rules import route_of
from .ui import (DIM_SHORT, STATUS_TEXT, ago, avatar, copy_line, donut, e, gate, icon, line_chart, local, page_head, purl, repo_name,
                 shell, verdict, when, zoom)
from .ui import copyable as _copyable

_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def analysis(store, aid: int) -> dict | None:
    """Analyses never change once uploaded: keep the last few parsed (per data folder; pages render in many threads)."""
    k = (str(store.root), aid)
    with _CACHE_LOCK:
        if k in _CACHE:
            _CACHE.move_to_end(k)
            return _CACHE[k]
    a = store.analysis(aid)
    if a is not None:
        with _CACHE_LOCK:
            _CACHE[k] = a
            while len(_CACHE) > 48:
                _CACHE.popitem(last=False)
    return a


def _dt(iso: str | None) -> datetime | None:
    """Uploads may carry any `created` string: a bad one hides that point, never the page."""
    return local(iso)


def _fmt(iso: str | None, f: str, missing: str = "—") -> str:
    d = _dt(iso)
    return d.strftime(f) if d else missing


def evidence(store, aid: int, case_id: str) -> dict:
    f = store.dir(aid) / "cases" / case_id / "evidence.json"
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def md(text) -> str:
    """A phrase of explain.py (plain text with `code` spans) as HTML."""
    return markup_html(str(text or ""))


def cap(s: str | None) -> str:
    s = (s or "").strip()
    return s[:1].upper() + s[1:] if s else s


def sentence(s: str | None) -> str:
    s = cap(s)
    return s if not s or s.endswith((".", "!", "?", "…")) else s + "."


def _s(n: int) -> str:
    return "" if n == 1 else "s"


# ---- context: which project, which branch / pull request, which analysis -------------------------------------------------

@dataclass
class Ctx:
    store: object
    p: object
    rows: list
    row: object | None
    a: dict | None
    branch: str | None
    pr: str | None
    explicit: bool = False
    public: str = ""
    q: dict = field(default_factory=dict)
    benches: list = field(default_factory=list)
    home: str = ""                # the branch a project opens on (its default branch, or the one it is analysed on)

    @property
    def key(self) -> str:
        return self.p["key"]

    def qs(self, latest: bool = False, **extra) -> str:
        d = {"pr": self.pr} if self.pr else ({"branch": self.branch} if self.branch else {})
        if self.explicit and self.row and not latest:
            d["analysis"] = self.row["id"]
        d.update({k: v for k, v in extra.items() if v is not None})
        return "?" + urlencode(d)

    def url(self, page: str = "", latest: bool = False, **extra) -> str:
        return f"{purl(self.key)}{('/' + quote(page, safe='/')) if page else ''}{self.qs(latest, **extra)}"   # dataset ids hold [ ]

    def history(self) -> list:
        """Analyses of the same branch or pull request, oldest first."""
        if self.pr:
            return [r for r in reversed(self.rows) if r["pr"] == self.pr]
        return [r for r in reversed(self.rows) if not r["pr"] and r["branch"] == self.branch]

    @property
    def ref(self):
        return self.store.row(self.row["reference_id"]) if self.row and self.row["reference_id"] else None

    @property
    def ref_a(self) -> dict | None:
        r = self.ref
        return analysis(self.store, r["id"]) if r else None

    @property
    def private(self) -> bool:
        return (self.p["visibility"] or "public") != "public"

    def brief(self, absolute: bool = False, **q) -> str:
        """The agent's brief of this analysis; a private project's link carries its view key (the page is authorised)."""
        params = {k: v for k, v in q.items() if v}
        if self.private:
            params["k"] = self.p["view_key"]
        return (f'{self.public if absolute else ""}/api/analyses/{self.row["id"]}/brief'
                + ("?" + urlencode(params) if params else ""))

    def shot(self, rel: str | None, aid: int | None = None) -> str | None:
        return f"/a/{aid or self.row['id']}/{rel}" if rel else None

    @property
    def view(self) -> str:
        """Who the page is for: "human" (the verdict and the evidence) or "agent" (every fact on one page, exportable)."""
        return "agent" if self.q.get("view") == "agent" else "human"

    def export(self, page: str, fmt: str, table: str | None = None) -> str:
        """The agent view of a page as data: `fmt` json (every table) or csv (one table)."""
        return self.url(page, view="agent", format=fmt, table=table)

    def analysis_json(self, aid: int | None = None) -> str:
        """The whole analysis as uploaded by CI (JSON); a private project's link carries its view key."""
        k = f'?k={self.p["view_key"]}' if self.private else ""
        return f'/api/analyses/{aid or self.row["id"]}{k}'


def home_branch(default: str | None, rows) -> str | None:
    """The branch a project opens on: its default branch once it has an analysis; until then the branch with the latest
    analysis (the server guesses `main` when CI did not say, and many repositories are on `master`)."""
    branches = [r["branch"] for r in rows if not r["pr"]]           # rows: newest first
    return default if default in branches or not branches else branches[0]


def resolve(store, key: str, q: dict, public: str = "") -> Ctx | None:
    with store.db() as c:
        p = c.execute("select * from projects where key=?", (key,)).fetchone()
        if not p:
            return None
        rows = c.execute("select * from analyses where project=? order by created desc, id desc", (key,)).fetchall()
    home = home_branch(p["default_branch"], rows)
    row, explicit = None, False
    if q.get("analysis"):
        try:
            row = store.row(int(q["analysis"]))
            explicit = bool(row and row["project"] == key)
            row = row if explicit else None
        except ValueError:
            row = None
    if row is None and q.get("pr"):
        row = store.latest(key, pr=q["pr"])
    if row is None and not q.get("pr"):
        row = store.latest(key, branch=q.get("branch") or home)
    pr = row["pr"] if row else q.get("pr")
    branch = row["branch"] if row else (q.get("branch") or home)
    with store.db() as c:
        benches = c.execute("select * from benchmarks where project=? order by created desc, id desc", (key,)).fetchall()
    return Ctx(store, p, rows, row, page_analysis(store, row["id"]) if row else None, branch, pr, explicit, public, q, benches, home)


def page_analysis(store, aid: int) -> dict | None:
    """The analysis a page shows: analyses from before structured failures get them built from their evidence. On a copy:
    the cached dict is shared by every thread rendering a page."""
    a = analysis(store, aid)
    if a is None or all(c.get("failure") or c.get("verdict") == "PASS" for c in a.get("cases", [])):
        return a
    return fill_failures({**a, "cases": [dict(c) for c in a.get("cases", [])]}, lambda cid: evidence(store, aid, cid))


# ---- shared project chrome --------------------------------------------------------------------------------------------

def base_crumbs(p) -> list:
    """GitHub projects sit under their owner's account, legacy projects under the project list."""
    k = p["key"]
    if k.startswith("gh/"):
        owner = k.split("/")[1]
        return [(owner, f"/gh/{owner}"), (p["title"], purl(k))]
    return [("Projects", "/projects"), (p["title"], purl(k))]


def snav(ctx: Ctx, active: str) -> str:
    """Project → Health, Issues, Journeys, Pull requests, Branches, Benchmarks. A pull request's page lives under Pull
    requests; Health and Journeys are the main branch's (or the branch being looked at); Issues are the analysis being
    looked at (a pull request's on its pages). Benchmarks is always there: its empty page says how to add one."""
    if active == "summary":
        active = "prs" if ctx.pr else "health"
    prs = len({r["pr"] for r in ctx.rows if r["pr"]})
    branches = len({r["branch"] for r in ctx.rows if not r["pr"]})
    a = ctx.a or {}
    n = len(a.get("dataset") or a.get("cases") or []) if a else None
    base = purl(ctx.key)
    bq = "" if ctx.pr or not ctx.branch or ctx.branch == ctx.home else "?" + urlencode({"branch": ctx.branch})
    iq = "?" + urlencode({"pr": ctx.pr}) if ctx.pr else bq
    items = [("health", base + bq, "overview", "Health", None),
             ("issues", f"{base}/issues{iq}", "issues", "Issues", len(a.get("issues") or []) if a else None),
             ("journeys", f"{base}/journeys{bq}", "journey", "Journeys", n),
             ("prs", f"{base}/pull-requests", "pr", "Pull requests", prs), ("branches", f"{base}/branches", "branch", "Branches", branches),
             ("bench", f"{base}/benchmarks", "flask", "Benchmarks", len(ctx.benches) or None)]

    def link(k, href, ic, label, n):
        return (f'<a class="it{" on" if active == k else ""}" href="{e(href)}">{icon(ic)}{label}'
                f'{f"<span class=cnt>{n}</span>" if n is not None else ""}</a>')
    return (f'<aside class=snav aria-label="Project"><div class=proj>{avatar(ctx.key, ctx.p["title"])}<div><b>{e(ctx.p["title"])}</b>'
            f'<span class=mono title="{e(repo_name(ctx.key))}">{e(ctx.key.split("/")[-1])}</span></div></div>'
            + "".join(link(*i) for i in items)
            + f'<a class="it foot{" on" if active == "setup" else ""}" href="{e(base)}/setup">{icon("setup")}Set up CI</a></aside>')


# Pages with an agent level: page → {is a pull request: kind of agent view} (assay/web/agent.py)
AGENT_PAGES = {"": {True: "pr", False: "health"}, "journeys": {True: "journeys", False: "journeys"},
               "pull-requests": {True: "prs", False: "prs"}}
AGENT_TITLES = {"pr": "Pull request #{pr}", "health": "Health", "journeys": "Journeys", "prs": "Pull requests"}


def view_toggle(ctx: Ctx, page: str) -> str:
    """Human: the verdict and its evidence. Agent: every fact on one page, exportable."""
    return ('<div class="seg vtoggle" role=group aria-label="Level of detail">'
            + "".join(f'<a class="{"on" if ctx.view == v else ""}" href="{e(ctx.url(page, view=v))}"'
                      f'{" aria-current=page" if ctx.view == v else ""}>{icon(ic)}{label}</a>'
                      for v, ic, label in (("human", "user", "Human"), ("agent", "terminal", "Agent"))) + '</div>')


def agent_data(ctx: Ctx, kind: str) -> dict:
    from . import agent
    if ctx.a is None and kind != "prs":
        return {"tables": {}, "facts": {"analysis": None}}
    return getattr(agent, f"{kind}_data")(ctx)


def agent_page(ctx: Ctx, page: str, kind: str) -> str:
    from . import agent
    title = AGENT_TITLES[kind].format(pr=ctx.pr, branch=ctx.branch or "")
    if ctx.a is None and kind != "prs":
        return no_analysis(ctx, title, page)
    body = project_head(ctx, title, page) + getattr(agent, f"{kind}_view")(ctx)
    return shell(f"{title} · {ctx.p['title']}", body, active="projects", snav=snav(ctx, "summary" if page == "" else
                                                                                  {"journeys": "journeys", "pull-requests": "prs"}[page]))


def scope_selector(ctx: Ctx, page: str) -> str:
    base = f"{purl(ctx.key)}{('/' + page) if page else ''}"
    branches = OrderedDict()
    prs = OrderedDict()
    for r in ctx.rows:            # newest first: first seen = latest
        if r["pr"]:
            prs.setdefault(r["pr"], r)
        else:
            branches.setdefault(r["branch"], r)
    cur = (f'{icon("pr")}<span>#{e(ctx.pr)}</span>' if ctx.pr else f'{icon("branch")}<span>{e(ctx.branch)}</span>')
    items = ['<li class=mh>Branches</li>']
    for b, r in sorted(branches.items(), key=lambda kv: (kv[0] != ctx.home, kv[0] or "")):
        on = not ctx.pr and b == ctx.branch
        items.append(f'<li><a{" aria-current=page" if on else ""} href="{e(base)}?{urlencode({"branch": b or ""})}">{icon("branch")}'
                     f'<span>{e(b or "(no branch)")}<span class=sub>{"Main branch · " if b == ctx.home else ""}{ago(r["created"])}</span></span>'
                     f'<span class=r>{gate(r["status"], label=False, kind="branch")}</span></a></li>')
    if prs:
        items.append('<li class=mh>Pull requests</li>')
        for n, r in prs.items():
            on = ctx.pr == n
            items.append(f'<li><a{" aria-current=page" if on else ""} href="{e(base)}?{urlencode({"pr": n})}">{icon("pr")}'
                         f'<span>#{e(n)} {e(r["pr_title"] or "")}<span class=sub>{e(r["branch"])} → {e(r["target"])}</span></span>'
                         f'<span class=r>{gate(r["status"], label=False)}</span></a></li>')
    return (f'<button type=button class=picker-btn popovertarget=scope-picker aria-expanded=false>{cur}'
            f'<span class=sr>: switch branch or pull request</span>{icon("down")}</button>'
            f'<nav id=scope-picker popover class="menu picker" aria-label="Branches and pull requests"><ul>{"".join(items)}</ul></nav>')


def project_head(ctx: Ctx, title: str, page: str) -> str:
    r = ctx.row if page != "pull-requests" else None
    # a branch page can switch branch; a pull request is reached from the list of pull requests
    extra = scope_selector(ctx, page) if not ctx.pr and page in ("", "journeys", "issues") else ""
    right = (f'<a class="btn sm" href="{e(r["ci_url"])}" target=_blank rel=noopener>{icon("external")}CI run</a>' if r and r["ci_url"] else "")
    if page in AGENT_PAGES:
        right += view_toggle(ctx, page)
    meta = []
    if r:
        if r["pr"]:
            meta.append(f'<span class=ink>{e(r["pr_title"] or "")}</span>')
            meta.append(f'<span class=mono>{e(r["branch"])}</span> into <span class=mono>{e(r["target"])}</span>')
        elif page == "journeys":
            meta.append(f'Analysed <span class=ink title="{e(when(r["created"]))}">{ago(r["created"])}</span>')
        latest = ctx.history()[-1] if ctx.history() else None
        if ctx.explicit and latest and latest["id"] != r["id"]:
            meta.append(f'<span class="chip warn">Older analysis #{r["id"]}</span> <a href="{e(ctx.url(page, latest=True))}">latest</a>')
    crumbs = [*base_crumbs(ctx.p)]
    if ctx.pr and page != "pull-requests":
        crumbs.append(("Pull requests", f"{purl(ctx.key)}/pull-requests"))
        if page:
            crumbs.append((f"Pull request #{ctx.pr}", ctx.url("")))
    elif page.startswith("journeys/"):
        crumbs.append(("Journeys", ctx.url("journeys")))
    crumbs.append((title, None))
    return page_head(crumbs, title, extra=extra, right=right, meta=meta)


def empty_state(title: str, text: str, action: str = "") -> str:
    return f'<div class=empty-state><h2>{e(title)}</h2><p>{text}</p>{action}</div>'


def no_analysis(ctx: Ctx, title: str, page: str) -> str:
    """Nothing to show here: why, and the one thing to do (open the analysis that exists, or set up CI)."""
    other = next((r for r in ctx.rows if not r["pr"] and r["branch"] != ctx.branch), None)
    if ctx.pr:
        head, text = f"No analysis of pull request #{ctx.pr}", "Assay analyses a pull request on every push to it, once its step is in CI."
    else:
        head, text = f"No analysis of {ctx.branch or 'this branch'} yet", "The Assay step in CI analyses a branch on every push to it."
    if other:
        text += f' The latest analysis is of <span class=mono>{e(other["branch"])}</span>, {ago(other["created"])}.'
        href = f'{purl(ctx.key)}{"/" + page if page else ""}?{urlencode({"branch": other["branch"] or ""})}'
        act = f'<a class="btn primary" href="{e(href)}">Open {e(other["branch"])}</a>'
    elif any(r["pr"] for r in ctx.rows):
        text += " Its pull requests are analysed already."
        act = f'<a class="btn primary" href="{e(purl(ctx.key))}/pull-requests">See pull requests</a>'
    else:
        act = f'<a class="btn primary" href="{e(purl(ctx.key))}/setup">Set up CI</a>'
    body = project_head(ctx, title, page) + '<div class=page>' + empty_state(head, text, act) + '</div>'
    return shell(f"{title} · {ctx.p['title']}", body, active="projects", snav=snav(ctx, page or "summary"))


# ---- projects ------------------------------------------------------------------------------------------------------------------

def _latest_by_project(projects, allrows):
    """From analyses sorted newest first: each project's latest analysis of its main branch (the branch it opens on) and
    the one before it, its latest analysis of any kind, its PR numbers."""
    rows: dict[str, list] = {}
    for r in allrows:
        rows.setdefault(r["project"], []).append(r)
    mains, prevs, lasts, prs = {}, {}, {}, {}
    for p in projects:
        k, rs = p["key"], rows.get(p["key"], [])
        if not rs:
            continue
        lasts[k] = rs[0]
        prs[k] = {r["pr"] for r in rs if r["pr"]}
        home = home_branch(p["default_branch"], rs)
        on = [r for r in rs if not r["pr"] and r["branch"] == home]
        if on:
            mains[k] = on[0]
            prevs[k] = on[1] if len(on) > 1 else None
    return mains, prevs, lasts, prs


def visible(store, viewer, owner: str | None = None) -> tuple[list, list]:
    """Projects this viewer may see (and their analyses, newest first); optionally of one account."""
    with store.db() as c:
        projects = [p for p in c.execute("select * from projects order by title").fetchall()
                    if (viewer is None or viewer.can_view(p)) and (owner is None or p["owner"] == owner)]
        keys = {p["key"] for p in projects}
        allrows = [r for r in c.execute("select * from analyses order by created desc, id desc").fetchall() if r["project"] in keys]
    return projects, allrows


def health_counts(a: dict | None) -> dict:
    """Kept, broken (known bugs and flaky failures apart), known bugs, flaky (failed, then passed on a second run), not
    checked, of the journeys an analysis ran."""
    cases = (a or {}).get("cases", [])
    known, flaky = known_of(a or {}), flaky_of(a or {})
    return {"total": len((a or {}).get("dataset") or cases), "ran": len(cases),
            "kept": sum(c["verdict"] == "PASS" for c in cases),
            "broken": sum(c["verdict"] == "FAIL" and c["case_id"] not in known | flaky for c in cases),
            "known": sum(c["verdict"] == "FAIL" and c["case_id"] in known for c in cases),
            "flaky": len(flaky - known),
            "unchecked": sum(c["verdict"] == "INCONCLUSIVE" for c in cases)}


def failure_words(c: dict, n: int = 240) -> str:
    """What went wrong with a journey, one sentence, and what its second run found (a failure is run once more before it
    blocks: "Reproduced on a second run.")."""
    out = sentence((c.get("failure") or {}).get("phrase") or first_line(c.get("summary"), n))
    again = SECOND_RUN.get((c.get("confirm") or {}).get("verdict")) if c.get("verdict") == "FAIL" else None
    if (c.get("confirm") or {}).get("verdict") == "PASS" and c.get("verdict") == "FAIL":
        again = cap(FLAKY_NOTE) + ": it blocks nothing"
    return f"{out} {again}." if again and out else out


def shown_verdict(c: dict | None) -> str | None:
    """A journey's result as the pages say it: a failure that passed on its second run is flaky, not broken."""
    if not c:
        return None
    return "FLAKY" if c["verdict"] == "FAIL" and (c.get("confirm") or {}).get("verdict") == "PASS" else c["verdict"]


def health_label(status: str | None, h: dict) -> str:
    if status == "FAILED" and h["broken"]:
        return f'{h["broken"]} broken'
    return STATUS_TEXT["branch"].get(status or "", "No analysis")


def overall_ratings(a: dict) -> list[tuple[str, str]]:
    """Each measured quality's A–E rating over the whole analysis; a quality nothing measured has none."""
    m, issues, ratings = _measured(a), a.get("issues", []), a.get("ratings") or {}
    out = []
    for dim, name in DIM_SHORT.items():
        sel = [i for i in issues if (i.get("dimension") or "functional") == dim]
        if dim != "functional" and (dim in m or sel):
            out.append((name, ratings.get(dim) or _letter(sel)))
    return out


def project_cards(store, projects, allrows) -> tuple[list, Counter]:
    """A row per project, like SonarQube's list: the gate of its main branch, its journeys (working, broken), its new
    issues against the analysis before, and when it was last analysed."""
    mains, prevs, lasts, pr_sets = _latest_by_project(projects, allrows)
    cards, states = [], Counter()
    for p in projects:
        k = p["key"]
        last, main = lasts.get(k), mains.get(k)
        a = analysis(store, main["id"]) if main else None
        prs = len(pr_sets.get(k, ()))
        st = main["status"] if main else "none"
        h = health_counts(a)
        states[st] += 1
        lock = ' <span class=tag>Private</span>' if (p["visibility"] or "public") == "private" else ""
        new = _new_issue_keys(a, analysis(store, prevs[k]["id"]) if prevs.get(k) else None) if a else None
        where = (f'on <span class=mono>{e(main["branch"])}</span>' if main else
                 f'only pull requests analysed so far' if last else "not analysed yet")
        if a:
            v = decide(a)                 # the problems its verdict counts, so a red stub never sits on "0 broken" alone
            probs = sum(p["kind"] == "issue" for p in v["problems"])
            blocking = any(p["blocking"] for p in v["problems"] if p["kind"] == "issue")
            stats = (f'<div><dt>Journeys work</dt><dd>{h["kept"]} of {h["total"]}</dd></div>'
                     f'<div><dt>Journeys broken</dt><dd class="{"bad" if h["broken"] else ""}">{h["broken"]}'
                     + (f' <span class=mute>+{h["known"]} known</span>' if h["known"] else "") + '</dd></div>'
                     f'<div><dt>New problems</dt><dd class="{("bad" if blocking else "warn") if probs else ""}">{probs}</dd></div>'
                     f'<div><dt>Issues</dt><dd>{len(a.get("issues") or [])}'
                     + (f' <span class=mute>{len(new)} new</span>' if new is not None else "") + '</dd></div>')
            what = f'<dl class="prow-what prow-m">{stats}</dl>'
        else:
            what = '<span class="prow-what mute">No analysis of its main branch yet</span>'
        rates = "".join(f'<span class=pr>{rating(letter)}{e(name)}</span>' for name, letter in overall_ratings(a)) if a else ""
        cards.append(
            f'<li class=prow data-item data-state="{e(st)}" data-name="{e(p["title"].lower())}" '
            f'data-last="{e((last or {"created": ""})["created"].replace("-", "").replace(":", "").replace("T", ""))}" '
            f'data-text="{e((p["title"] + " " + k).lower())}">'
            f'<span class=prow-state>{gate(st, kind="branch") if main else "<span class=mute>No analysis</span>"}</span>'
            f'<span class=prow-name><a class=prow-link href="{e(purl(k))}">{e(p["title"])}</a>{lock}'
            f'<span class=prow-sub>{e(repo_name(k, k))} · {where}</span></span>'
            f'{what}'
            f'<span class=prow-links><a href="{e(purl(k))}/pull-requests">{prs} pull request{_s(prs)}</a>'
            f'<a href="{e(purl(k))}/issues">Issues</a></span>'
            f'<span class=prow-when title="{e(when(last["created"]) if last else "")}">{ago(last["created"]) if last else ""}</span>'
            + (f'<p class=prow-rates><span class=sr>Ratings: </span>{rates}</p>' if rates else "") + '</li>')
    return cards, states


def _project_lists(store, projects, allrows, viewer, signed_in: bool) -> str:
    """Signed in: your projects, then public ones; a visitor: the public list. A line says when a filter hides all."""
    empty = '<p class=list-empty data-empty hidden>No project matches the filter.</p>'
    if not signed_in or viewer.admin:
        cards, _ = project_cards(store, projects, allrows)
        return f'<ul class=prows role=list data-filter-list>{"".join(cards)}</ul>{empty}'
    mine = [p for p in projects if viewer.mine(p)]
    others = [p for p in projects if not viewer.mine(p)]
    yours = (f'<ul class=prows role=list>{"".join(project_cards(store, mine, allrows)[0])}</ul>' if mine else
             '<p class=list-empty>None yet. A private project joins your account when you open it from its pull request\'s '
             'link while signed in; with GitHub connected, your own repositories\' projects are here too.</p>')
    public = (f'<section class=proj-sec data-group><h2>Public projects</h2><ul class=prows role=list>'
              f'{"".join(project_cards(store, others, allrows)[0])}</ul></section>' if others else "")
    return (f'<div data-filter-list><section class=proj-sec data-group><h2>Your projects</h2>{yours}</section>{public}</div>{empty}')


def projects_page(store, q: dict, viewer=None) -> str:
    projects, allrows = visible(store, viewer)
    query = (q.get("q") or "").strip()
    cards, states = project_cards(store, projects, allrows)
    btns = "".join(f'<button type=button data-facet="state" data-value="{k}" aria-pressed=false>{gate(k, kind="branch", quiet=True)}<span class=n>{states.get(k, 0)}</span></button>'
                   for k in ("PASSED", "FAILED", "INCONCLUSIVE"))
    fac = f'<div class=facet><h2>Main branch</h2>{btns}</div>'
    broken = states.get("FAILED", 0)
    head = page_head([("Projects", None)], "Projects",
                     meta=[f'{len(projects)} project{_s(len(projects))}',
                           f'<span class="chip fail">{broken} broken on main</span>' if broken else "all healthy on main",
                           f'{len(allrows)} analyses'])
    signed_in = bool(viewer and (viewer.user or viewer.admin))
    note = ('' if signed_in else '<p class=signin-note>These are public projects. <a href="/login?next=/projects">Sign in</a> '
            'to see your own, or <a href="/signup">create an account</a>.</p>')
    body = (head + '<div class=page data-filter-root>' + note + '<div class=withfacets>'
            f'<aside class=facets>{fac}</aside><div>'
            f'<search class=toolbar><form action="/projects"><label for=q-list class=sr>Filter projects by name</label>'
            f'<input id=q-list name=q class=input data-search placeholder="Filter by name" value="{e(query)}" autocomplete=off spellcheck=false></form>'
            f'<div class=r><span class=count><b data-shown>{len(cards)}</b> of {len(cards)} shown</span><label for=sort-list class=sr>Sort projects</label>'
            f'<select id=sort-list class=input data-sort><option value=name>Sort by name</option><option value=last>Last analysis</option></select></div>'
            f'<p class=sr role=status data-filter-status></p></search>'
            + (_project_lists(store, projects, allrows, viewer, signed_in) if cards else
               '<div class="panel empty">No projects to show. Private projects open from the link in their pull request. '
               '<a href="/setup">Connect a repository</a>: one CI step, and the first analysis creates the project.</div>')
            + '</div></div></div>')
    return shell("Projects", body, active="projects", q=query)


def account_page(store, owner: str, viewer) -> str | None:
    """An account (GitHub user or organisation): created by its first analysis, lists what this viewer may see."""
    with store.db() as c:
        exists = c.execute("select 1 from projects where owner=? limit 1", (owner,)).fetchone()
    if not exists:
        return None
    name = owner.split("/", 1)[1]
    projects, allrows = visible(store, viewer, owner)
    if not projects:                       # an account with only private projects looks like no account
        return None
    cards, states = project_cards(store, projects, allrows)
    broken = states.get("FAILED", 0)
    extra = f'<img class=ghav src="https://github.com/{e(name)}.png?size=64" alt="" width=32 height=32>'
    head = page_head([(name, None)], name, extra=extra,
                     right=f'<a class=btn href="https://github.com/{e(name)}" target=_blank rel=noopener>{icon("external")}GitHub</a>',
                     meta=[f'{len(projects)} project{_s(len(projects))} you can see',
                           f'<span class="chip fail">{broken} broken</span>' if broken else
                           ("all healthy" if states.get("PASSED") == len(projects) else "")])
    body = head + '<div class=page>' + (
        f'<ul class=prows role=list>{"".join(cards)}</ul>' if cards else
        '<div class="panel empty">The projects of this account are private. Open them from the link Assay posted in a pull request.</div>') + '</div>'
    return shell(name, body, active="projects")


# ---- history ---------------------------------------------------------------------------------------------------------------

def _history_map(ctx: Ctx, limit: int = 12) -> tuple[list, dict]:
    """The last `limit` analyses of this branch or pull request up to the current one, and per journey its results in
    them (oldest first) as (row, case)."""
    hist = ctx.history()
    if ctx.row:
        hist = [h for h in hist if h["created"] <= ctx.row["created"]]
    hist = hist[-limit:]
    per: dict[str, list] = {}
    for h in hist:
        ha = analysis(ctx.store, h["id"]) or {}
        for c in ha.get("cases", []):
            per.setdefault(c["case_id"], []).append((h, c))
    return hist, per


def flips(results: list) -> int:
    """How often a journey's result changed between PASS and FAIL (not-checked runs left out): ≥ 2 is flaky."""
    vs = [c["verdict"] for _, c in results if c["verdict"] in ("PASS", "FAIL")]
    return sum(a != b for a, b in zip(vs, vs[1:]))


def broken_since(results: list) -> str | None:
    """When the current failing streak started (the first FAIL after the last PASS)."""
    since = None
    for h, c in reversed(results):
        if c["verdict"] == "FAIL":
            since = h["created"]
        elif c["verdict"] == "PASS":
            break
    return since


# ---- evidence: screens, data, suspect code -----------------------------------------------------------------------------------

def _failed_rel(c: dict) -> str | None:
    """The screen where it broke: the failed step's screenshot, else the end screen."""
    bad = next((s for s in c.get("steps", []) if s.get("ok") is False and s.get("screenshot")), None)
    return f'cases/{c["case_id"]}/{bad["screenshot"]}' if bad else c.get("screenshot")


def _same_rel(c: dict, step_i) -> str | None:
    """In another analysis: the same step's screenshot if it has one, else the end screen."""
    st = next((s for s in c.get("steps", []) if s.get("i") == step_i and s.get("screenshot")), None)
    return f'cases/{c["case_id"]}/{st["screenshot"]}' if st else c.get("screenshot")


def _before_shot(ctx: Ctx, c: dict) -> tuple[str, str] | None:
    """How the same journey looked when it worked: on the reference (a pull request's target), else its last pass here."""
    step = next((s.get("i") for s in c.get("steps", []) if s.get("ok") is False), None)
    ra = ctx.ref_a
    if ra:
        rc = next((x for x in ra.get("cases", []) if x["case_id"] == c["case_id"]), None)
        if rc and (rel := _same_rel(rc, step)):
            return f"On {ctx.ref['branch'] or 'the target'}", ctx.shot(rel, ctx.ref["id"])
    hist, per = _history_map(ctx, 20)
    for h, hc in reversed(per.get(c["case_id"], [])):
        if h["id"] != ctx.row["id"] and hc["verdict"] == "PASS" and (rel := _same_rel(hc, step)):
            return f"When it last worked · {_fmt(h['created'], '%-d %b')}", ctx.shot(rel, h["id"])
    return None


def _shots(ctx: Ctx, c: dict) -> str:
    now = _failed_rel(c)
    before = _before_shot(ctx, c)
    figs = []
    if before:
        figs.append(f'<figure><figcaption>{e(before[0])}</figcaption>{zoom(before[1], subject(c) + ": " + before[0], before[0])}</figure>')
    if now:
        src = ctx.shot(now)
        cap = "In this change" if ctx.pr else "This analysis"
        figs.append(f'<figure class=now><figcaption>{cap}</figcaption>{zoom(src, subject(c) + ": " + cap.lower(), cap)}</figure>')
    return f'<div class="ec-shots n{len(figs)}">{"".join(figs)}</div>' if figs else ""


def _jv(v) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, default=str)


def _short(v, n=140) -> str:
    s = _jv(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def _pair(exp, act) -> tuple[str, str]:
    """Expected and saved values, the difference visible: a string cut short shows the part that was lost."""
    if isinstance(exp, str) and isinstance(act, str) and act and exp.startswith(act) and len(act) < len(exp):
        return (f'<span class=kept>{e(act)}</span><span class=lost>{e(exp[len(act):])}</span>',
                f'<span class=kept>{e(act)}</span><span class=cut>cut at {len(act)} of {len(exp)} characters</span>')
    if act is None:
        return e(_short(exp, 400)), '<span class=lost>nothing</span>'
    return e(_short(exp, 400)), f'<span class=bad>{e(_short(act, 400))}</span>'


def _key_of(rec: dict):
    for k in ("id", "uuid", "key", "slug", "name"):
        if isinstance(rec, dict) and k in rec:
            return f"{k}={rec[k]}"
    return None


def data_changes(before: dict, after: dict, only: set | None = None) -> str:
    """What the journey changed in the app's data, probe by probe (only the probes given, if any)."""
    out = []
    for name in sorted(set(before or {}) | set(after or {})):
        if only and name not in only:
            continue
        b, a = (before or {}).get(name), (after or {}).get(name)
        if b == a:
            continue
        lines = []
        if isinstance(b, list) and isinstance(a, list) and all(isinstance(x, dict) for x in b + a) and all(_key_of(x) for x in b + a):
            mb, ma = {_key_of(x): x for x in b}, {_key_of(x): x for x in a}
            for k in ma.keys() - mb.keys():
                lines.append(f'<div class=add>+ {e(k)} {e(_short({kk: vv for kk, vv in ma[k].items() if kk not in ("id",)}, 220))}</div>')
            for k in mb.keys() - ma.keys():
                lines.append(f'<div class=del>− {e(k)}</div>')
            for k in mb.keys() & ma.keys():
                if mb[k] != ma[k]:
                    fields = [f for f in set(mb[k]) | set(ma[k]) if mb[k].get(f) != ma[k].get(f)]
                    lines.append(f'<div class=chg>~ {e(k)}: ' + "; ".join(f'{e(f)} {e(_short(mb[k].get(f), 60))} → {e(_short(ma[k].get(f), 60))}' for f in sorted(fields)[:6]) + '</div>')
        else:
            lines.append(f'<div class=chg>{e(_short(b, 160))} → {e(_short(a, 160))}</div>')
        out.append(f'<div class=p>{e(name)}</div>{"".join(lines[:12])}')
    return f'<div class=diff>{"".join(out)}</div>' if out else '<div class=mute>No data changed.</div>'


def _data(ctx: Ctx, c: dict) -> str:
    """The data behind the failure: expected against saved, the unexpected changes, the errors, the inaccessible elements."""
    f = c.get("failure") or {}
    head = ""
    body = ""
    store = f.get("store") or "app"
    if f.get("fields"):
        rows = "".join(f'<tr><td class=mono>{e((f.get("path") + "." if f.get("path") else "") + k).replace(".", ".<wbr>")}</td>'
                       + "".join(f'<td>{x}</td>' for x in _pair(d.get("expected"), d.get("actual"))) + '</tr>'
                       for k, d in f["fields"].items())
        head, body = (f'What the {store} saved' + (" (a record this journey created)" if f.get("record_is_new") else ""),
                      f'<div class=scroll tabindex=0 role=region aria-label="Table, scrolls sideways"><table class=dt><thead><tr><th scope=col>Field</th><th scope=col>Expected</th><th scope=col>Saved</th></tr></thead><tbody>{rows}</tbody></table></div>')
    elif "expected" in f and "actual" in f and f.get("path"):
        exp, act = _pair(f.get("expected"), f.get("actual"))
        head, body = (f'What the {store} has',
                      f'<div class=scroll tabindex=0 role=region aria-label="Table, scrolls sideways"><table class=dt><thead><tr><th scope=col>Data</th><th scope=col>Expected</th><th scope=col>Found</th></tr></thead>'
                      f'<tbody><tr><td class=mono>{e(f["path"])}</td><td>{exp}</td><td>{act}</td></tr></tbody></table></div>')
    elif f.get("paths"):
        ev = evidence(ctx.store, ctx.row["id"], c["case_id"])
        probes = {p.split(".")[0].split("[")[0] for p in f["paths"]}
        head, body = (f'What else it changed in the {store}', data_changes(ev.get("state_before"), ev.get("state_after"), probes))
    elif f.get("violations"):
        items = "".join(f'<li>{e(cap(a11y_phrase(v.get("rule", ""), int(v.get("elements") or 0), v.get("help") or "")))}'
                        + (f'<div class="mono mute">{e(", ".join((v.get("targets") or [])[:3]))}</div>' if v.get("targets") else "") + '</li>'
                        for v in f["violations"][:6])
        head, body = "What a screen reader or keyboard user meets", f'<ul class=plain>{items}</ul>'
    elif f.get("errors"):
        head, body = "The error", f'<pre class=errbox>{e(chr(10).join(str(x) for x in f["errors"][:3]))}</pre>'
    elif (st := f.get("step")) and st.get("error"):
        head, body = "Where it stopped", f'<pre class=errbox>{e(st["error"])}</pre>'
    elif f.get("request"):
        head, body = "The request", f'<pre class=errbox>{e(f["request"])}</pre>'
    elif c.get("verdict") != "PASS" and not f and c.get("summary"):
        head, body = "What happened", f'<pre class=errbox>{e(c["summary"])}</pre>'
    return f'<div class=ec-sec><h4>{icon("data")}{e(head)}</h4>{body}</div>' if body else ""


def _hunk(text: str) -> str:
    out = []
    for line in (text or "").splitlines():
        cls = "add" if line.startswith("+") else "del" if line.startswith("-") else "at" if line.startswith("@@") else ""
        out.append(f'<span class="{cls}">{e(line) or " "}</span>')
    return "".join(out)                  # block spans: a newline between them would double every line


def _suspects(sus: list) -> str:
    if not sus:
        return ""

    def one(s: dict) -> str:
        return (f'<div class=sus><div class=sus-h>{icon("file")}<span class=mono>{e(s.get("file"))}:{e(s.get("lines"))}</span></div>'
                f'<p class=sus-why>{md(sentence(s.get("why")))}</p><pre class=hunk tabindex=0 role=region aria-label="Code change in {e(s.get("file"))}">'
                f'{_hunk(s.get("hunk") or "")}</pre></div>')
    more = sus[1:]
    return (f'<div class=ec-sec><h4>{icon("terminal")}Suspect code in this change</h4>{one(sus[0])}'
            + (f'<details class=more><summary>{len(more)} more suspect{_s(len(more))}</summary>{"".join(one(x) for x in more)}</details>'
               if more else "") + '</div>')


def evidence_card(ctx: Ctx, c: dict, *, chips: str = "", head: bool = True, lead: bool = True) -> str:
    """One broken journey's proof: what broke in words, the data, the suspect code, the screens. `head=False` on the
    journey's own page, where the title and the phrase are already said above."""
    f = c.get("failure") or {}
    top = (f'<header class=ec-h><h3><a class=ec-t href="{e(ctx.url("journeys/" + c["case_id"]))}">{e(subject(c))}</a></h3>{chips}</header>'
           if head else "")
    p = f'<p class=ec-p>{md(failure_words(c, 300))}</p>' if head and lead else ""
    inner = f'{_data(ctx, c)}{_suspects(c.get("suspects") or [])}'
    if not head:                                         # under the page's h2: its sections are the next level down
        inner = inner.replace("<h4>", "<h3>").replace("</h4>", "</h3>")
    return (f'<article class="ecard{"" if head else " bare"}">{top}'
            f'<div class=ec-body><div class=ec-main>{p}{inner}</div>{_shots(ctx, c)}</div></article>')


# ---- the verdict hero ---------------------------------------------------------------------------------------------------------

STATE_ICON = {"ship": "check", "no_ship": "x", "unsure": "alert"}


_LABEL_N = iter(range(1, 1 << 62))


def label(state: str, word: str, title: str, reason: str = "", fields: list[tuple[str, str]] | None = None,
          small: bool = False, side: str = "") -> str:
    """The verdict as a QC label: the word, what happened, the reason, and the record (fields). Colour = state, with a
    glyph and the word: never colour alone."""
    dl = ('<dl class=label-fields>' + "".join(f'<div><dt>{e(k)}</dt><dd>{v}</dd></div>' for k, v in fields) + '</dl>') if fields else side
    lid = f"verdict-{next(_LABEL_N)}"
    return (f'<section class="label {e(state)}{" small" if small else ""}" aria-labelledby={lid}><div class=label-main>'
            f'<h2 id={lid} class=label-word>{icon(STATE_ICON[state])}{e(word)}</h2>'
            + (f'<p class=label-title>{e(cap(title))}</p>' if title else "")
            + (f'<p class=label-reason>{reason}</p>' if reason else "") + f'</div>{dl}</section>')


def _took(a: dict) -> str | None:
    d = duration_s(a)
    return None if d is None else (f"{d / 60:.1f} min" if d >= 90 else f"{d:.0f} s")


def record(ctx: Ctx) -> list[tuple[str, str]]:
    """What a label certifies: which journeys, how long, which commit, against what, when."""
    a, r = ctx.a, ctx.row
    sc = a.get("scope") or {}
    n = len(a.get("cases", []))
    out = []
    if ctx.pr:
        out.append(("Pull request", f"#{e(ctx.pr)}"))
    out.append(("Checked", f'{sc["checked"]} of {sc["total"]} journeys' if sc.get("total") and sc.get("checked") != sc.get("total")
                else f"all {n} journey{_s(n)}"))
    if (took := _took(a)):
        out.append(("Run time", took))
    if r and r["sha"]:
        out.append(("Commit", f'<span class=mono>{e(r["sha"][:7])}</span>'))
    if ctx.pr and ctx.ref:
        out.append(("Compared with", f'{e(ctx.ref["branch"])} <span class=mono>{e((ctx.ref["sha"] or "")[:7])}</span>'))
    if r:
        out.append(("Analysed", f'<span title="{e(when(r["created"]))}">{ago(r["created"])}</span>'))
    return out


def hero(ctx: Ctx, v: dict, *, label: str, title: str, brief_q: dict | None = None) -> str:
    """The verdict for a person: the label, then what to do and why these journeys. Agent tooling is on the Agent level."""
    m = re.match(r"(\w+)([,:]?)(.*)", v["action_text"], re.S)
    word, sep, rest = (m[1], m[2], m[3]) if m else (v["action_text"], "", "")
    brief = f'<a href="{e(ctx.brief(**(brief_q or {})))}">the brief</a>'
    cls = v["action"]
    if v["state"] == "no_ship" and word.lower() == "nothing":     # broken: someone has to fix it, an agent can
        cls = "fix"
        act = (f'<b>{"Fix it before merging" if ctx.pr else "Fix what is broken"}.</b> Your coding agent can do it from '
               f'{brief}: in Claude Code, run <code>/assay:fix</code>.')
    elif word.lower() == "nothing":                       # nothing for a person to do
        act = "<b>Nothing to do.</b>" + (" It can merge." if ctx.pr and v["state"] == "ship" else "")
    else:
        act = f'<b>{e({"decide": "Decide", "look": "Take a look"}.get(word.lower(), cap(word)))}:</b> {md(cap(rest.strip()))}'

    why = ((ctx.a or {}).get("scope") or {}).get("why")
    return (globals()["label"](v["state"], label, title, md(v["headline"]), record(ctx))
            + f'<div class=label-after><p class="act {e(cls)}">{act}</p>'
            + (f'<p class=why>Journeys chosen for this change: {e(why)}.</p>' if why and ctx.pr else "") + '</div>')


# ---- under the verdict, SonarQube's structure: the gate's conditions, measures (new code | overall), issues ---------------

def _ikey(i: dict) -> str:
    """An issue's identity across analyses: its fingerprint, else its rule, place and quality."""
    return i.get("fingerprint") or f'{i.get("rule")}|{i.get("where")}|{i.get("dimension")}'


def _new_issue_keys(a: dict, base: dict | None) -> set | None:
    """The issues of `a` that are new: the engine's own marks when it compared with a reference (its `baseline`), else
    those missing from `base` (an earlier analysis the server holds). None: nothing to compare with, so nothing is new."""
    if a.get("baseline"):
        return {_ikey(i) for i in a.get("issues", []) if i.get("new", True)}
    if base is None:
        return None
    old = {_ikey(i) for i in base.get("issues", [])}
    return {_ikey(i) for i in a.get("issues", []) if _ikey(i) not in old}


def new_code(ctx: Ctx, v: dict | None = None) -> dict | None:
    """SonarQube's "new code": on a pull request, what it changed against its target; on a branch, what changed since
    the branch's previous analysis. None when there is nothing to compare with (then only the overall state is shown)."""
    a = ctx.a
    if a.get("baseline"):                        # the engine compared: its marks are the ones the gate counted
        v = v or decide(a)
        ch = a.get("changes") or {}
        return {"basis": f"Compared with {a['baseline']}.", "issues": _new_issue_keys(a, None),
                "broke": sorted(set(v["changes"]["broke"]) | set(ch.get("regressions") or [])), "fixed": list(ch.get("fixed") or [])}
    if ctx.pr:
        r = ctx.ref
        basis = f'Compared with {r["branch"]} {(r["sha"] or "")[:7]}.'.replace(" .", ".") if r else ""
    else:
        before = [h for h in ctx.history() if (h["created"], h["id"]) < (ctx.row["created"], ctx.row["id"])]
        r = before[-1] if before else None
        basis = f'Since the previous analysis of {ctx.branch}, {_fmt(r["created"], "%-d %b %Y")}.' if r else ""
    base = analysis(ctx.store, r["id"]) if r else None
    if base is None:
        return None
    bv = {c["case_id"]: c["verdict"] for c in base.get("cases", [])}
    cases = a.get("cases", [])
    return {"basis": basis, "issues": _new_issue_keys(a, base),
            "broke": [c["case_id"] for c in cases if c["verdict"] == "FAIL" and bv.get(c["case_id"]) == "PASS"],
            "fixed": [c["case_id"] for c in cases if c["verdict"] == "PASS" and bv.get(c["case_id"]) == "FAIL"]}


def _letter(issues: list) -> str:
    """SonarQube's rating: A no issue, B a minor one at worst, C major, D critical, E blocker (assay/quality.py)."""
    worst = min((SEV_RANK.get(i.get("severity"), 9) for i in issues if i.get("severity") != "info"), default=9)
    return "EDCB"[worst] if worst <= 3 else "A"


RATING_WORST = {"A": "no issues", "B": "minor issues at worst", "C": "a major issue at worst", "D": "a critical issue at worst",
                "E": "a blocker issue"}


def rating(letter: str | None) -> str:
    if not letter:
        return ""
    return (f'<span class="rate {e(letter)}" title="Rating {e(letter)}: {e(RATING_WORST.get(letter, ""))}">'
            f'<span class=sr>rating </span>{e(letter)}</span>')


GATE_COND = {"met": ("check", "Met"), "failed": ("x", "Failed"), "unsure": ("alert", "Not sure"), "warn": ("alert", "Warning"),
             "base": ("info", "Baseline")}


def _cond_value(c: dict) -> str:
    x = c.get("actual")
    if cond_id(c) == "weakened" and x in (0, "0"):
        return "none"
    return e("—" if x is None else x)


def _cond_need(c: dict) -> str:
    op, t = c.get("op") or "", c.get("threshold")
    t = "none" if cond_id(c) == "weakened" and t in (0, "0") else t
    return e(f"required {t}" if op == "=" else f"{op} {t}".strip())


def gate_section(ctx: Ctx) -> str:
    """The quality gate as SonarQube shows it: its status, then each condition met or failed, with value and threshold."""
    g = ctx.a.get("gate") or {}
    conds = g.get("conditions") or []
    first = bool(g.get("first_analysis"))
    rows, failed, base = [], 0, 0
    for c in conds:
        # a condition on issues with nothing to compare with (a branch's first analysis): what is there is the baseline
        st = "met" if c.get("ok") else ("warn" if first else "base" if c.get("baseline") else "unsure" if c.get("soft") else "failed")
        failed += st == "failed"
        base += st == "base"
        ic, word = GATE_COND[st]
        rows.append(f'<tr class={st}><th scope=row>{e(c.get("name") or cond_id(c))}</th><td class="r num">{_cond_value(c)}</td>'
                    f'<td class="r num">{_cond_need(c)}</td><td><span class="cres {st}">{icon(ic)}{word}</span></td></tr>')
    note = ((f'{failed} of {len(conds)} condition{_s(len(conds))} failed' if failed else
             f'{len(conds)} condition{_s(len(conds))}' + (", nothing blocks: the first analysis" if first else ""))
            + (f'; {base} only set the baseline: the first analysis of this branch' if base else ""))
    body = (f'<div class=scroll tabindex=0 role=region aria-label="Conditions of the quality gate, scrolls sideways"><table class="tbl conds">'
            '<thead><tr><th scope=col>Condition</th><th scope=col class=r>Value</th><th scope=col class=r>Required</th>'
            f'<th scope=col>Result</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>' if rows else
            '<p class="mute panel-b">This analysis recorded no gate conditions.</p>')
    return (f'<section class="panel qgate" aria-labelledby=gate-h><div class=panel-h><h2 id=gate-h>Quality gate</h2>'
            f'{gate(g.get("status"), kind="gate")}<span class="r mute">{e(note)}</span></div>{body}</section>')


def _cards(ctx: Ctx, nc: dict | None, new: bool) -> str:
    """One card per quality: its issues (new or all) with the rating; a quality nothing measured says so, never A."""
    a = ctx.a
    m = _measured(a)
    issues = a.get("issues", [])
    h = health_counts(a)
    if new:
        jv, js = f'<b class="num{" bad" if nc["broke"] else ""}">{len(nc["broke"])}</b> broke', f'{len(nc["fixed"])} fixed'
    else:
        jv, js = f'<b class="num{" bad" if h["broken"] else ""}">{h["broken"]}</b> broken', f'{h["kept"]} of {h["total"]} work'
    out = [f'<a class=mcard href="{e(ctx.url("journeys"))}"><span class=mc-h>{DIM_SHORT["functional"]}: journeys</span>'
           f'<span class=mc-v>{jv}</span><span class=mc-s>{js}</span></a>']
    ratings = (a.get("ratings_new") if new else a.get("ratings")) or {}
    for dim, name in DIM_SHORT.items():
        if dim == "functional":
            continue
        sel = [i for i in issues if (i.get("dimension") or "functional") == dim]
        allsel = sel
        if new:
            sel = [i for i in sel if _ikey(i) in nc["issues"]]
        if dim not in m and not allsel:
            out.append(f'<div class="mcard off"><span class=mc-h>{e(name)}</span><span class="mc-v mute">not measured</span>'
                       f'<span class=mc-s>nothing checked it</span></div>')
            continue
        n_new = sum(_ikey(i) in nc["issues"] for i in allsel) if nc else None
        sub = (f"of {len(allsel)} in all" if new else f"{n_new} new" if n_new is not None else "")
        letter = ratings.get(dim) or _letter(sel)
        out.append(f'<a class=mcard href="{e(ctx.url("issues", dimension=dim, new="new" if new else None))}">'
                   f'<span class=mc-h>{e(name)}{rating(letter)}</span>'
                   f'<span class=mc-v><b class=num>{len(sel)}</b> {"new " if new else ""}issue{_s(len(sel))}</span>'
                   f'<span class=mc-s>{e(sub)}</span></a>')
    return f'<div class=mgrid>{"".join(out)}</div>'


def measures_section(ctx: Ctx, nc: dict | None) -> str:
    """SonarQube's two tabs: New code (what this change, or the last analysis, introduced) and Overall (the product as it
    is). Without anything to compare with there is no new code to show: Overall only, and why."""
    overall = _cards(ctx, nc, False)
    if nc is None:
        why = ("its target branch has no analysis to compare with" if ctx.pr else
               f"this is the first analysis of {ctx.branch} here")
        return (f'<section class="panel measures" aria-labelledby=meas-h><div class=panel-h><h2 id=meas-h>Measures</h2>'
                f'<span class="r mute">Overall</span></div><p class=mbasis>No new code to show: {e(why)}.</p>{overall}</section>')
    first_new = bool(ctx.pr)                     # a pull request is about its change; a branch about its state
    tabs = [("new", "New code", _cards(ctx, nc, True), nc["basis"]),
            ("all", "Overall", overall, "Every journey and issue of this analysis.")]
    sel = "new" if first_new else "all"
    btns = "".join(f'<button type=button role=tab id=mt-{k} aria-controls=mp-{k} aria-selected={"true" if k == sel else "false"}'
                   f'{"" if k == sel else " tabindex=-1"}>{t}</button>' for k, t, _, _ in tabs)
    panes = "".join(f'<div class=tabpane role=tabpanel id=mp-{k} aria-labelledby=mt-{k}{"" if k == sel else " hidden"}>'
                    f'<p class=mbasis>{e(basis)}</p>{cards}</div>' for k, _, cards, basis in tabs)
    return (f'<section class="panel measures" aria-labelledby=meas-h><div class=panel-h><h2 id=meas-h>Measures</h2></div>'
            f'<div class="tabs mtabs" role=tablist aria-label="Measures of">{btns}</div>{panes}</section>')


def quality_band(ctx: Ctx, v: dict) -> str:
    """The gate and the measures side by side, under the verdict."""
    return f'<div class="grid qband">{gate_section(ctx)}{measures_section(ctx, new_code(ctx, v))}</div>'


def problem_items(ctx: Ctx, v: dict) -> list[dict]:
    """The problems (sensor issues) the verdict counts, each with its issue record: worst first, then most frequent."""
    out = []
    for p in v["problems"]:
        if p["kind"] != "issue":
            continue
        i = next((x for x in ctx.a.get("issues", []) if x.get("rule") == p.get("rule") and issue_phrase(x) == p["phrase"]), None) or {}
        out.append({"p": p, "i": i, "sev": i.get("severity") or "major", "blocking": p.get("blocking", True)})
    return sorted(out, key=lambda x: (SEV_RANK.get(x["sev"], 9), -(x["i"].get("count") or 0)))


def _seen(ctx: Ctx, i: dict) -> str:
    """Where an issue was seen and how often, in words: "8 times, in 8 journeys"."""
    _, journeys = _places(i)
    n = i.get("count") or 0
    parts = [f"{n} time{_s(n)}"] if n > 1 else []
    if journeys:
        parts.append(f"in {len(journeys)} journey{_s(len(journeys))}")
    elif i.get("sources"):
        parts.append("by the screen scan")
    return ", ".join(parts)


def problem_rows(ctx: Ctx, probs: list[dict], limit: int = 10) -> list[str]:
    """A problem as a line of the list of what is broken: severity, the phrase, where, how often, the issue behind it."""
    rows = []
    for x in probs[:limit]:
        i, sev = x["i"], x["sev"]
        where = i.get("where") or ""
        meta = [e(sev.capitalize()), e(DIM_SHORT.get(i.get("dimension") or "", i.get("dimension") or ""))]
        if where and not where.startswith("case:") and f"`{where}`" not in x["p"]["phrase"]:    # the phrase may name it already
            meta.append(f'on <span class=mono>{e(where)}</span>')
        if (seen := _seen(ctx, i)):
            meta.append(e(seen))
        href = ctx.url("issues", rule=i.get("rule"), screen=where if where and not where.startswith("case:") else None)
        rows.append(f'<li class=prob><span class="sev {e(sev)}" aria-hidden=true>{icon(sev if sev in SEV_RANK else "info")}</span>'
                    f'<div><a href="{e(href)}">{md(cap(x["p"]["phrase"]))}</a>'
                    + ('' if x["blocking"] else ' <span class="chip warn">does not block</span>')
                    + f'<p>{" · ".join(meta)} · <span class=mono>{e(i.get("rule") or "")}</span></p></div></li>')
    if len(probs) > limit:
        rows.append(f'<li class=more><a href="{e(ctx.url("issues"))}">{len(probs) - limit} more problem{_s(len(probs) - limit)} on the Issues page</a></li>')
    return rows


ISSUE_FACETS = (("dimension", "Quality"), ("severity", "Severity"), ("new", "New code"), ("rule", "Rule"), ("screen", "Screen"),
                ("journey", "Journey"))


def _places(i: dict) -> tuple[list, list]:
    """The screens (routes, requests) and the journeys an issue was seen on."""
    where, screens, journeys = i.get("where") or "", [], []
    if where.startswith("case:"):
        journeys.append(where[5:])
    elif where:
        screens.append(where)
    for s in i.get("sources") or []:
        if s.startswith("scan:"):
            screens.append(s.split(":", 2)[-1])
        elif s:
            journeys.append(s)
    return list(dict.fromkeys(screens)), list(dict.fromkeys(journeys))


def issues_page(ctx: Ctx) -> str:
    """SonarQube's Issues tab: every issue of the analysis, filters on the left kept in the address."""
    if not ctx.a:
        return no_analysis(ctx, "Issues", "issues")
    a = ctx.a
    nc = new_code(ctx)
    known = known_of(a)
    titles = {c["case_id"]: subject(c) for c in a.get("cases", [])}
    items = []
    for i in a.get("issues", []):
        scr, jr = _places(i)
        items.append({"i": i, "dimension": i.get("dimension") or "functional", "severity": i.get("severity") or "info",
                      "new": ("new" if _ikey(i) in nc["issues"] else "existing") if nc else None, "rule": i.get("rule") or "",
                      "screen": scr, "journey": jr})
    picked = {k: ctx.q[k] for k, _ in ISSUE_FACETS if ctx.q.get(k)}

    def hit(it, skip=None):
        return all(k == skip or (val in it[k] if isinstance(it[k], list) else it[k] == val) for k, val in picked.items())
    shown = sorted((it for it in items if hit(it)),
                   key=lambda it: (it["new"] != "new", SEV_RANK.get(it["severity"], 9), it["rule"], it["i"].get("where") or ""))

    def link(**change) -> str:                    # this page with one facet changed, the others kept
        q = {**picked, **change}
        return ctx.url("issues", **{k: q.get(k) for k, _ in ISSUE_FACETS})

    labels = {"dimension": DIM_SHORT, "severity": {s: s.capitalize() for s in SEVERITIES},
              "new": {"new": "New", "existing": "Existing"}}
    order = {"dimension": list(DIM_SHORT), "severity": list(SEVERITIES), "new": ["new", "existing"]}
    facets = []
    for k, title in ISSUE_FACETS:
        if k == "new" and not nc:
            continue
        cnt = Counter()
        for it in items:
            if hit(it, skip=k):
                for val in (it[k] if isinstance(it[k], list) else [it[k]]):
                    cnt[val] += 1
        vals = [x for x in (order.get(k) or sorted(cnt, key=lambda x: (-cnt[x], x))) if cnt[x] or picked.get(k) == x]
        if not vals:
            continue

        def one(x):
            on = picked.get(k) == x
            name = labels.get(k, {}).get(x) or (titles.get(x, x) if k == "journey" else x)
            inner = (f'<span class="lab{" mono" if k in ("rule", "screen") else ""}">'
                     f'{icon(x) + " " if k == "severity" else ""}{e(name)}</span><span class=n>{cnt[x]}</span>')
            return (f'<li><a href="{e(link(**{k: None if on else x}))}"{" aria-current=true" if on else ""}'
                    f'{f" title={chr(34)}{e(x)}{chr(34)}" if k == "journey" else ""}>{inner}</a></li>')
        head, rest = vals[:8], vals[8:]
        more = (f'<details><summary>{len(rest)} more</summary><ul role=list>{"".join(one(x) for x in rest)}</ul></details>' if rest else "")
        facets.append(f'<div class=facet><h2>{e(title)}</h2><ul role=list>{"".join(one(x) for x in head)}</ul>{more}</div>')
    if not nc:
        facets.append('<p class="facet-note mute">New code appears once there is an earlier analysis to compare with.</p>')

    rows = []
    for it in shown:
        i = it["i"]
        sev = it["severity"]
        runs = [j for j in it["journey"] if j in titles]
        seen = ""
        if runs:
            first = f'<a href="{e(ctx.url("journeys/" + runs[0]))}">{e(titles[runs[0]])}</a>'
            seen = f'seen in {first}' + (f' and {len(runs) - 1} more journey{_s(len(runs) - 1)}' if len(runs) > 1 else "")
        elif it["screen"]:
            seen = "found by the screen scan"
        where = i.get("where") or ""
        meta = [e(DIM_SHORT.get(it["dimension"], it["dimension"])), f'<span class=mono>{e(it["rule"])}</span>']
        if where and not where.startswith("case:") and f"`{where}`" not in issue_phrase(i):
            meta.append(f'on <span class=mono>{e(where)}</span>')
        if seen:
            meta.append(seen)
        if (i.get("count") or 0) > 1:
            meta.append(f'{i["count"]} occurrences')
        chips = ((' <span class="chip brand">new</span>' if it["new"] == "new" else "")
                 + (' <span class=chip>known bug</span>' if i.get("rule") == "CASE-FAIL" and set(it["journey"]) & known else ""))
        shot = i.get("evidence")
        thumb = (zoom(ctx.shot(shot), f'{i.get("title") or it["rule"]}: the screen', i.get("title") or it["rule"], "ithumb")
                 if shot and re.fullmatch(r"[\w./:-]+\.(png|jpe?g|webp)", shot) else "")
        rows.append(f'<li class=irow><span class="sev {e(sev)}">{icon(sev if sev in SEV_RANK else "info")}{e(sev.capitalize())}</span>'
                    f'<div class=ib><p class=ip>{md(sentence(issue_phrase({**i, "where": ""} if where.startswith("case:") else i)))}{chips}</p>'
                    f'<p class=im>{" · ".join(meta)}</p></div>{thumb}</li>')
    clear = (f' <a href="{e(ctx.url("issues"))}">Clear filters</a>' if picked else "")
    chosen = "".join(f'<span class=chip>{e(dict(ISSUE_FACETS)[k])}: {e(labels.get(k, {}).get(x) or titles.get(x, x))}'
                     f' <a href="{e(link(**{k: None}))}" aria-label="Remove the filter {e(dict(ISSUE_FACETS)[k])}">×</a></span>'
                     for k, x in picked.items())
    if shown:
        lst = f'<ol class=ilist role=list>{"".join(rows)}</ol>'
    elif items:
        lst = empty_state("No issue matches these filters", "Remove a filter to see more.",
                          f'<a class=btn href="{e(ctx.url("issues"))}">Clear filters</a>')
    else:
        lst = empty_state("No issues", "The journeys and the screen scan of this analysis found nothing to report.")
    body = (project_head(ctx, "Issues", "issues") + '<div class=page><div class=withfacets>'
            f'<aside class="facets ifacets" aria-label="Filters">{"".join(facets)}</aside><div>'
            f'<div class=toolbar><span class=count><b>{len(shown)}</b> of {len(items)} issue{_s(len(items))}</span>{chosen}{clear}</div>'
            f'{lst}</div></div></div>')
    return shell(f"Issues · {ctx.p['title']}", body, active="projects", snav=snav(ctx, "issues"))


# ---- the pull request page ---------------------------------------------------------------------------------------------------

def _speed(ctx: Ctx) -> tuple[str, str]:
    """Slower than the reference only beyond noise: the median journey ≥ 1.5× slower and ≥ 3 s more in total."""
    ra = ctx.ref_a
    if not ra:
        return "No reference to compare with", "mute"
    ref = {c["case_id"]: c for c in ra.get("cases", []) if c["verdict"] == "PASS"}
    pairs = [(c, ref[c["case_id"]]) for c in ctx.a.get("cases", []) if c["verdict"] == "PASS" and c["case_id"] in ref
             and (ref[c["case_id"]].get("duration_s") or 0) > 0]
    if not pairs:
        return "Nothing to compare", "mute"
    ratio = statistics.median((c.get("duration_s") or 0) / r["duration_s"] for c, r in pairs)
    extra = sum((c.get("duration_s") or 0) - r["duration_s"] for c, r in pairs)
    if ratio >= 1.5 and extra >= 3:
        c, r = max(pairs, key=lambda x: (x[0].get("duration_s") or 0) - x[1]["duration_s"])
        return f'Journeys got slower: {subject(c)} {r["duration_s"]:.1f} s → {c["duration_s"]:.1f} s', "bad"
    if ratio <= 1 / 1.5 and extra <= -3:
        return f"Journeys got faster: median ×{ratio:.1f}", "good"
    return "No change beyond noise", "mute"


def _ai(ctx: Ctx, pr: str | None) -> tuple[str, str, str] | None:
    """The latest benchmark of this pull request (or branch) in words: the candidate against the baseline, with its interval."""
    rows = [b for b in ctx.benches if (b["pr"] == pr if pr else (not b["pr"] and b["branch"] == ctx.branch))]
    if not rows:
        return None
    r = rows[0]
    b = ctx.store.bench(r["id"]) or {}
    for name, arm in (b.get("arms") or {}).items():
        d = arm.get("diff_vs_baseline")
        if not d or d.get("mean") is None:
            continue
        lo, hi = (d.get("ci95") or [None, None])[:2]
        half = f" (±{(hi - lo) * 50:.0f})" if lo is not None and hi is not None else ""
        if d.get("significant"):
            word, cls = ("better", "good") if d["mean"] > 0 else ("worse", "bad")
        else:
            word, cls = "no significant change", "mute"
        return f'{name} vs {b.get("baseline")}: {f"{d['mean'] * 100:+.0f}".replace("-", "−")} pp{half}, {word}', cls, f"{purl(ctx.key)}/benchmarks/{r['id']}"
    base = (b.get("arms") or {}).get(b.get("baseline")) or {}
    p1 = base.get("pass@1")
    return (f'{b.get("baseline")}: {p1 * 100:.0f}% of journeys pass' if p1 is not None else "benchmarked", "mute",
            f"{purl(ctx.key)}/benchmarks/{r['id']}")


def _quality(ctx: Ctx, v: dict) -> str:
    ch = v["changes"]
    nb = len(ch["broke"])
    j = (f'<b class={"bad" if nb else "mute"}>{nb} broke</b>, <b class={"good" if ch["fixed"] else "mute"}>{len(ch["fixed"])} fixed</b>, '
         f'{len(ch["new_journeys"])} new')
    speed, scls = _speed(ctx)
    tiles = [("journey", "Journeys", j, ""), ("clock", "Speed", f'<span class={scls}>{e(speed)}</span>', "")]
    ai = _ai(ctx, ctx.pr)
    if ai:
        tiles.append(("flask", "AI features", f'<a class={ai[1]} href="{e(ai[2])}">{e(ai[0])}</a>', ""))
    cells = "".join(f'<div class=qt><div class=qt-h>{icon(ic)}{e(t)}</div><div class=qt-v>{val}</div></div>' for ic, t, val, _ in tiles)
    return f'<section class=sec><h2 class=sh>Quality change{" vs " + e(ctx.ref["branch"]) if ctx.ref else ""}</h2><div class=qtiles>{cells}</div></section>'


def _background(ctx: Ctx, v: dict) -> str:
    rows = [(p["phrase"], p.get("note"), p.get("case_id")) for p in v["background"]]
    titles = {c["case_id"]: subject(c) for c in ctx.a.get("cases", [])}
    rows += [(f'{titles.get(x, x)}: a known bug, still failing', "tagged known-bug, never blocks", x) for x in v["changes"]["known_failing"]]
    rows += [(f'{titles.get(x, x)}: a known bug that now passes', "remove its known-bug tag so it guards against a comeback", x)
             for x in v["changes"]["known_fixed"]]
    if not rows:
        return ""
    items = "".join((f'<li><a href="{e(ctx.url("journeys/" + cid))}">{md(sentence(ph))}</a>' if cid in titles else f'<li>{md(sentence(ph))}')
                    + (f' <span class=mute>{e(note)}</span>' if note else "") + '</li>' for ph, note, cid in rows)   # linked or not, with its note
    return f'<details class="sec bg"><summary>Not blocking <span class=chip>{len(rows)}</span></summary><ul class=plain>{items}</ul></details>'


def pr_page(ctx: Ctx) -> str:
    a = ctx.a
    v = decide(a)
    cases = {c["case_id"]: c for c in a.get("cases", [])}
    reg = set((a.get("changes") or {}).get("regressions", []))
    jp = [p for p in v["problems"] if p["kind"] == "journey" and p["case_id"] in cases]
    cards = []
    for p in jp[:5]:
        c = cases[p["case_id"]]
        chips = ((' <span class="chip fail">broken by this change</span>' if p["case_id"] in reg else "")
                 + (' <span class="chip brand">critical</span>' if c.get("critical") else ""))
        cards.append(evidence_card(ctx, c, chips=chips, lead=len(jp) > 1))     # one: the label says it
    if len(jp) > 5:
        cards.append(f'<a class=more-link href="{e(ctx.url("journeys"))}">{len(jp) - 5} more broken journeys {icon("arrow")}</a>')
    probs = problem_items(ctx, v)
    if probs:
        cards.append(f'<ul class="jlist probs">{"".join(problem_rows(ctx, probs))}</ul>')
    weak = [p for p in v["problems"] if p["kind"] == "weakened"]
    if weak:
        items = "".join(f'<li>{md(sentence(p["phrase"]))}</li>' for p in weak)
        cards.append(f'<article class="ecard decide"><header class=ec-h><span class=ec-t>Checks weakened: decide</span></header>'
                     f'<div class=ec-main><ul class=plain>{items}</ul><p class=mute>The gate got easier to pass. Restore the checks, or approve '
                     f'the change with the label <code>assay-approved</code> on the pull request and run the job again.</p></div></article>')
    heading = "Evidence" if v["state"] == "no_ship" else "Problems found" if cards else ""
    ev = f'<section class=sec><h2 class=sh>{heading}</h2>{"".join(cards)}</section>' if cards else ""
    body = (project_head(ctx, f"Pull request #{ctx.pr}", "")
            + f'<div class=page>{hero(ctx, v, label=v["label"], title=v["title"])}{quality_band(ctx, v)}{ev}{_quality(ctx, v)}{_background(ctx, v)}</div>')
    return shell(f"PR #{ctx.pr} · {ctx.p['title']}", body, active="projects", snav=snav(ctx, "summary"))


# ---- the health page: a branch, usually main -----------------------------------------------------------------------------------

def _measured(a: dict) -> dict:
    """What measured each quality. Analyses from before the record: what their checks and findings show."""
    if a.get("measured"):                 # the upload check turns a missing record into {}
        return a["measured"]
    out: dict[str, list] = {"functional": ["journeys"]} if a.get("cases") else {}
    for c in a.get("cases", []):
        for it in c.get("items", []):
            if it.get("status") in ("pass", "fail"):
                out.setdefault(it.get("dimension") or "functional", ["journey checks"])
    for i in a.get("issues", []):
        out.setdefault(i.get("dimension") or "functional", ["sensors"])
    return out


BROWSERS = {"chromium": "Chromium (Chrome, Edge)", "firefox": "Firefox", "webkit": "WebKit (Safari)"}


def not_checked(ctx: Ctx) -> list[str]:
    """What this analysis did not check at all, said plainly: qualities nothing measured, browsers never used, screens no
    journey reached, accessibility results that need a person, journeys that did not run."""
    a = ctx.a
    out = []
    m = _measured(a)
    for d, label in DIM_SHORT.items():
        if d not in m:
            out.append(f"{label}: nothing measured it")
        elif d == "security" and m[d] == ["cookie flags and tokens in storage"]:
            out.append("Security: only cookie flags and tokens in browser storage")
    used = {c.get("browser") for c in a.get("cases", []) if c.get("browser")}
    if used:
        miss = [label for b, label in BROWSERS.items() if b not in used]
        if miss:
            out.append("Browsers no journey ran in: " + ", ".join(miss))
    scr = a.get("screens") or a.get("coverage") or {}
    if scr.get("uncovered"):
        u = scr["uncovered"]
        out.append(f"Screens no journey reached: `{'`, `'.join(u[:5])}`" + (f" and {len(u) - 5} more" if len(u) > 5 else ""))
    review = 0
    for c in a.get("cases", [])[:50]:
        a11y = ((evidence(ctx.store, ctx.row["id"], c["case_id"]).get("audit") or {}).get("a11y") or {})
        review += sum(int(x.get("nodes") or 0) for x in a11y.get("incomplete", []) or [])
    if review:
        out.append(f"{review} accessibility result{_s(review)} the audit could not decide (contrast over images, unresolved ARIA): a person should look")
    for c in [c for c in a.get("cases", []) if c["verdict"] == "INCONCLUSIVE"][:6]:
        out.append(f'Journey “{subject(c)}” could not be checked: '
                   + ((c.get("failure") or {}).get("phrase") or first_line(c.get("summary"), 200)).replace("could not be checked: ", ""))
    ran = {c["case_id"] for c in a.get("cases", [])}
    skipped = [d for d in a.get("dataset") or [] if d.get("id") not in ran]
    if skipped:
        out.append(f"{len(skipped)} journey{_s(len(skipped))} not run in this analysis")
    return out


def health_page(ctx: Ctx) -> str:
    a = ctx.a
    v = decide(a)
    h = health_counts(a)
    known = set(v["changes"]["known_failing"])
    cases = a.get("cases", [])
    hist, per10 = _history_map(ctx, 10)
    _, per20 = _history_map(ctx, 20)
    confirmed = flaky_of(a) - known                  # failed, then passed on a second run: not broken
    flaky = sorted({cid for cid, res in per10.items() if flips(res) >= 2} | confirmed)
    unchecked = h["unchecked"] + max(0, h["total"] - h["ran"])
    label = {"ship": "Healthy", "no_ship": "Broken", "unsure": "Not sure"}[v["state"]]
    probs = problem_items(ctx, v)
    blocking = any(p["blocking"] for p in v["problems"])
    title = (f'{h["broken"]} of {h["total"]} journeys broken' if v["state"] == "no_ship" and h["broken"] else
             v["title"] if v["state"] != "ship" or probs else
             f'all {h["kept"]} journeys checked work' if not h["known"] else
             f'{h["kept"]} of {h["total"]} journeys work, {h["known"]} known bug{_s(h["known"])}')
    # the numbers say what the verdict counted: journeys and problems apart, so a red label never sits on "0 broken" alone
    # (problems a broken journey already shows are the journey's, so the count appears only when there are others)
    nums = [("journeys", h["total"], ""), ("work", h["kept"], "good"), ("journeys broken", h["broken"], "bad" if h["broken"] else ""),
            *([("new problems", len(probs), ("bad" if blocking else "warn") if probs else "")] if probs or not h["broken"] else []),
            ("known bugs", h["known"], ""), ("flaky", len(flaky), "warn" if flaky else ""), ("no result", unchecked, "warn" if unchecked else "")]
    line = '<dl class=hline>' + "".join(f'<div><dt>{e(t)}</dt><dd class="{c} num">{n}</dd></div>' for t, n, c in nums) + '</dl>'

    broken = [c for c in cases if c["verdict"] == "FAIL" and c["case_id"] not in known | confirmed]
    rows = []
    for c in broken:
        since = broken_since(per20.get(c["case_id"], []))
        rows.append(f'<li><a href="{e(ctx.url("journeys/" + c["case_id"]))}">{e(subject(c))}</a>'
                    + (f' <span class="chip fail">since {e(_fmt(since, "%-d %b"))}</span>' if since else "")
                    + (' <span class="chip brand">critical</span>' if c.get("critical") else "")
                    + f'<p>{md(failure_words(c))}</p></li>')
    rows += problem_rows(ctx, probs)
    for c in [c for c in cases if c["case_id"] in known]:
        rows.append(f'<li class=known><a href="{e(ctx.url("journeys/" + c["case_id"]))}">{e(subject(c))}</a> <span class=chip>known bug</span>'
                    f'<p>{md(failure_words(c))}</p></li>')
    for cid in flaky:
        c = next((x for x in cases if x["case_id"] == cid), None)
        if c and (c["verdict"] == "PASS" or cid in confirmed):
            why = (f"{cap(FLAKY_NOTE)}: it blocks nothing." if cid in confirmed else
                   f"Passed and failed in turn over the last {len(hist)} analyses.")
            rows.append(f'<li class=known><a href="{e(ctx.url("journeys/" + cid))}">{e(subject(c))}</a> <span class="chip warn">flaky</span>'
                        f'<p>{e(why)}</p></li>')
    n_issues = len(a.get("issues") or [])
    all_link = (f'<a class=r href="{e(ctx.url("issues"))}">All {n_issues} issue{_s(n_issues)} {icon("arrow")}</a>' if n_issues else "")
    # only known bugs and flaky journeys listed: nothing is broken by this analysis, so the heading says what they are
    heading = "What is broken" if broken or probs else "Known bugs and flaky journeys"
    broken_sec = (f'<section class=panel><div class=panel-h><h2>{icon("x")}{heading}</h2>{all_link}</div>'
                  f'<ul class=jlist>{"".join(rows)}</ul></section>' if rows else
                  f'<section class=panel><div class=panel-h><h2>{icon("check")}What is broken</h2></div><div class=panel-b mute>Nothing: every journey that ran works.</div></section>')
    nc = not_checked(ctx)
    nc_sec = (f'<section class=panel><div class=panel-h><h2>{icon("alert")}Not checked</h2></div>'
              + (f'<ul class="plain nc">{"".join(f"<li>{md(x)}</li>" for x in nc)}</ul>' if nc else
                 '<div class=panel-b>Everything Assay can measure was measured.</div>') + '</section>')

    hist20 = ctx.history()
    if ctx.row:
        hist20 = [x for x in hist20 if x["created"] <= ctx.row["created"]]
    hist20 = hist20[-20:]
    chart = line_chart([("Journeys that work", [x["pass"] / x["cases"] if x["cases"] else None for x in hist20], "var(--chart-1)")],
                       [_fmt(x["created"], "%-d %b") for x in hist20], width=1100, height=190, ymax=1, pct=True,
                       links=[f"{purl(ctx.key)}?analysis={x['id']}" for x in hist20],
                       dot_colors=[{"PASSED": "var(--pass)", "FAILED": "var(--fail)"}.get(x["status"], "var(--warn)") for x in hist20])
    trend = (f'<section class=panel><div class=panel-h><h2>Journeys that work, last {len(hist20)} analyses of {e(ctx.branch)}</h2></div>'
             f'<div class=panel-b>{chart}</div></section>' if len(hist20) >= 4 else
             f'<section class=panel><div class=panel-h><h2>Trend</h2></div><p class="panel-b mute">The trend appears after four analyses of '
             f'{e(ctx.branch)}; there {"is" if len(hist20) == 1 else "are"} {len(hist20)} so far.</p></section>')
    benches = sorted([b for b in ctx.benches if not b["pr"] and b["branch"] == ctx.branch], key=lambda b: (b["created"], b["id"]))[-20:]
    if benches:
        vals = []
        for b in benches:
            arms = json.loads(b["arms"] or "{}")
            vals.append((arms.get(b["baseline"]) or {}).get("pass@1"))
        ai_chart = line_chart([("AI features: pass rate", vals, "var(--chart-3)")], [_fmt(b["created"], "%-d %b") for b in benches],
                              width=1100, height=170, ymax=1, pct=True, links=[f"{purl(ctx.key)}/benchmarks/{b['id']}" for b in benches])
        trend += (f'<section class=panel><div class=panel-h><h2>{icon("flask")}AI features, benchmark pass rate</h2>'
                  f'<a class=r href="{e(purl(ctx.key))}/benchmarks">Benchmarks {icon("arrow")}</a></div><div class=panel-b>{ai_chart}</div></section>')
    body = (project_head(ctx, "Health", "")
            + f'<div class=page>{hero(ctx, v, label=label, title=title)}{line}{quality_band(ctx, v)}'
            f'<div class="grid g2 hgrid">{broken_sec}{nc_sec}</div>{trend}</div>')
    return shell(ctx.p["title"], body, active="projects", snav=snav(ctx, "summary"))


def summary_page(ctx: Ctx) -> str:
    if not ctx.a:
        return no_analysis(ctx, "Pull request" if ctx.pr else "Health", "")
    return pr_page(ctx) if ctx.pr else health_page(ctx)


# ---- journeys: the dataset index, and one journey's evidence ---------------------------------------------------------------------

def _touches(jm: dict | None) -> str:
    if not jm:
        return ""
    routes = list(dict.fromkeys(f'{r.split(" ", 1)[0]} {route_of(r.split(" ", 1)[1])}' if " " in r else r for r in jm.get("routes") or []))
    writes = [r for r in routes if not r.startswith(("GET ", "HEAD ", "OPTIONS "))] or routes
    parts = writes[:2] + (jm.get("tables") or [])[:2]
    return " · ".join(parts)


def group_of(d: dict) -> str:
    """A journey's group: its `group:`, else the file it is written in (`cases/feed.yaml` → feed)."""
    if d.get("group"):
        return str(d["group"])
    src = str(d.get("source") or "")
    return re.sub(r"^(?:\.assay/)?cases/|\.ya?ml$", "", src) or "journeys"


def journeys_page(ctx: Ctx) -> str:
    """The dataset for a person: every journey by group, whether it works now and lately; details one click away."""
    if not ctx.a:
        return no_analysis(ctx, "Journeys", "journeys")
    a = ctx.a
    hist, per = _history_map(ctx, 12)
    cases = {c["case_id"]: c for c in a.get("cases", [])}
    ds = a.get("dataset") or [{"id": c["case_id"], "title": c.get("title"), "promise": c.get("promise"), "tags": c.get("tags"),
                               "critical": c.get("critical")} for c in a.get("cases", [])]
    known = known_of(a)
    order = {"FAIL": 0, "FLAKY": 1, "INCONCLUSIVE": 1, None: 2, "PASS": 3}
    groups: dict[str, list] = {}
    for d in ds:
        groups.setdefault(group_of(d), []).append(d)
    cnt, sections = Counter(), []
    for g in sorted(groups, key=lambda g: (min(order.get(shown_verdict(cases.get(d["id"])), 2) for d in groups[g]), g)):
        items, broken = [], 0
        for d in sorted(groups[g], key=lambda d: (order.get(shown_verdict(cases.get(d["id"])), 2), not d.get("critical"), d["id"])):
            cid, c = d["id"], cases.get(d["id"])
            vkey = shown_verdict(c) if c else "none"
            cnt[vkey] += 1
            broken += vkey == "FAIL" and cid not in known
            res = per.get(cid, [])
            name = d.get("promise") or d.get("title") or cid
            tags = [t for t in d.get("tags") or [] if t != g]
            chips = ((' <span class="chip brand">critical</span>' if d.get("critical") else "")
                     + (' <span class=chip>known bug</span>' if cid in known else "")
                     + (' <span class="chip warn">flaky</span>' if flips(res[-10:]) >= 2 else ""))
            why = failure_words(c, 200) if c and vkey != "PASS" else ""
            dots = ("".join(f'<i class="{x["verdict"]}" title="{e(when(h["created"]))}: {x["verdict"]}"></i>' for h, x in res[-12:])
                    if len(res) >= 3 else "")
            title = (f'<a class=jt href="{e(ctx.url("journeys/" + cid))}">{e(name)}</a>' if c else f'<span class="jt ink">{e(name)}</span>')
            items.append(f'<li data-item data-verdict="{vkey}" data-text="{e((name + " " + cid + " " + " ".join(tags) + " " + g).lower())}">'
                         f'<span class=jv>{verdict(vkey) if c else "<span class=vd>Not run</span>"}</span>'
                         f'<div class=jb>{title}{chips}'
                         + (f'<p class=jw>{md(sentence(why))}</p>' if why else "")
                         + (f'<div class=jtags>{"".join(f"<span>#{e(t)}</span>" for t in tags)}</div>' if tags else "")
                         + f'</div><span class=dots title="recent runs">{dots}</span></li>')
        head = (f'<h2>{e(g)}</h2><span class=mute>{len(groups[g])} journey{_s(len(groups[g]))}</span>'
                + (f'<span class="chip fail">{broken} broken</span>' if broken else ""))
        sections.append(f'<section class="panel jgroup" data-group><div class=panel-h>{head}</div><ul class=jrows>{"".join(items)}</ul></section>')
    seg = ('<div class=seg>' + "".join(f'<button type=button data-facet=verdict data-value="{v}" aria-pressed=false>{lab} <span class=n>{cnt.get(v, 0)}</span></button>'
                                       for v, lab in (("FAIL", "Broken"), ("FLAKY", "Flaky"), ("INCONCLUSIVE", "Not checked"), ("PASS", "Works"), ("none", "Not run"))
                                       if cnt.get(v)) + '</div>')
    body = (project_head(ctx, "Journeys", "journeys") + '<div class=page data-filter-root>'
            f'<search class=toolbar>{seg}<form><label for=q-journeys class=sr>Search journeys, tags and groups</label>'
            f'<input id=q-journeys name=q class=input data-search placeholder="Search journeys, tags, groups" autocomplete=off spellcheck=false></form>'
            f'<div class=r><span class=count><b data-shown>{len(ds)}</b> of {len(ds)} journeys in {len(groups)} group{_s(len(groups))}</span></div>'
            f'<p class=sr role=status data-filter-status></p></search>'
            f'<div data-filter-list>{"".join(sections)}</div></div>')
    return shell(f"Journeys · {ctx.p['title']}", body, active="projects", snav=snav(ctx, "journeys"))


def _check_phrase(it: dict, c: dict) -> str:
    f = c.get("failure") or {}
    chk = f.get("check") or {}
    if it["status"] == "fail" and it["kind"] == chk.get("kind") and f.get("phrase"):
        return sentence(f["phrase"])
    if it["status"] in ("fail", "undecided") and it.get("detail"):
        return sentence(f'{it["kind"].replace("_", " ")}: {it["detail"]}')
    return sentence(check_label(it["kind"], it.get("spec")))


def _step_words(s: dict, raw_steps: list) -> str:
    """A step as a person says it ("click button “Post”"); the recorded summary when the journey's YAML is unavailable."""
    i = s.get("i")
    raw = raw_steps[i] if isinstance(i, int) and 0 <= i < len(raw_steps) else None
    label = step_label(s, raw) if isinstance(raw, dict) else f'{s.get("action") or ""} {s.get("summary") or ""}'
    return label[:1].upper() + label[1:]


def journey_page(ctx: Ctx, case_id: str) -> str | None:
    if not ctx.a:
        return no_analysis(ctx, "Journey", "journeys")
    c = next((c for c in ctx.a["cases"] if c["case_id"] == case_id), None)
    if not c:
        return None
    aid = ctx.row["id"]
    base = f"/a/{aid}/cases/{case_id}/"
    vv = shown_verdict(c)
    hist, per = _history_map(ctx, 20)
    dots = "".join(f'<a href="{e(purl(ctx.key))}/journeys/{e(quote(case_id, safe=''))}?analysis={h["id"]}" title="#{h["id"]} {e(when(h["created"]))}: {x["verdict"]}"><i class="{x["verdict"]}"></i></a>'
                   for h, x in per.get(case_id, []))
    st = {"PASS": "ship", "FAIL": "no_ship"}.get(vv, "unsure")
    label_word = {"PASS": "Works", "FAIL": "Broken", "FLAKY": "Flaky", "INCONCLUSIVE": "Not checked"}.get(vv, vv)
    phrase = (failure_words(c, 300) if vv in ("FAIL", "FLAKY") else journey_phrase(c).split(": ", 1)[-1] if vv != "PASS" else
              f'All {sum(1 for i in c.get("items", []) if i["level"] == "must")} required checks held.')
    facts = " · ".join(x for x in ((f'as {c["account"]}' if c.get("account") else "visitor"), c.get("viewport"),
                                   f'{c.get("duration_s") or 0:.1f} s') if x)
    runs = len(per.get(case_id, []))
    top = globals()["label"](st, label_word, "", md(sentence(phrase)), [
        ("As", e(c.get("account") or "a visitor")), ("Screen", e(c.get("viewport") or "desktop")),
        ("Time", f'{c.get("duration_s") or 0:.1f} s'), (f"Last {runs} run{_s(runs)}", f'<span class=dots>{dots}</span>')] if runs >= 3 else [
        ("As", e(c.get("account") or "a visitor")), ("Screen", e(c.get("viewport") or "desktop")),
        ("Time", f'{c.get("duration_s") or 0:.1f} s')], small=True)
    ev = (f'<section class=sec><h2 class=sh>Evidence</h2>{evidence_card(ctx, c, head=False)}</section>' if vv != "PASS" else "")
    steps = []
    raw_steps = ((evidence(ctx.store, aid, case_id).get("case") or {}).get("steps") or [])
    for s in c.get("steps", []):
        shot = s.get("screenshot")
        img = zoom(base + shot, f"The screen after step {int(s.get('i') or 0) + 1}", f"Step {int(s.get('i') or 0) + 1}") if shot else ""
        err = f'<div class=err>{e(first_line(s["error"], 300))}</div>' if s.get("error") else ""
        steps.append(f'<li class="{"" if s.get("ok") is not False else "bad"}"><span class=n>{int(s.get("i") or 0) + 1}</span><div>'
                     f'<span class=sum>{md(_step_words(s, raw_steps))}</span>{err}</div>{img}</li>')
    checks = "".join(f'<li class={ {"pass": "ok", "fail": "bad"}.get(it["status"], "soft")}><span class=ic>{icon({"pass": "check", "fail": "x"}.get(it["status"], "alert"))}</span>'
                     f'<div>{md(_check_phrase(it, c))}{" <span class=mute>(should: never blocks)</span>" if it["level"] == "should" else ""}'
                     f'{" <span class=mute>· AI judge</span>" if it.get("by") == "judge" else ""}</div></li>'
                     for it in c.get("items", []) if it["status"] != "skipped")
    final = c.get("screenshot")
    body = (project_head(ctx, subject(c), f"journeys/{case_id}")
            + f'<div class=page>{top}{ev}<div class="grid jgrid">'
            f'<section class=panel><div class=panel-h><h2>{icon("journey")}What the journey did</h2><span class=r mute>a screenshot after each step</span></div>'
            f'<div class=panel-b><ol class=steps>{"".join(steps) or "<li class=mute>No steps recorded.</li>"}</ol></div></section>'
            f'<div><section class=panel><div class=panel-h><h2>{icon("check")}What it checks</h2></div><div class=panel-b><ul class=checks>{checks}</ul></div></section>'
            + (f'<section class=panel><div class=panel-h><h2>{icon("image")}End screen</h2></div><div class=panel-b>'
               f'{zoom(ctx.shot(final), subject(c) + ": the end screen", "End screen", "shotbig")}</div></section>' if final else "")
            + '</div></div></div>')
    return shell(f"{subject(c)} · {ctx.p['title']}", body, active="projects", snav=snav(ctx, "prs" if ctx.pr else "journeys"))


# ---- pull requests, branches -------------------------------------------------------------------------------------------------------

def prs_page(ctx: Ctx) -> str:
    """Every analysed pull request, newest first: its verdict and, in one line, why."""
    seen, rows = set(), []
    for r in ctx.rows:
        if not r["pr"] or r["pr"] in seen:
            continue
        seen.add(r["pr"])
        a = page_analysis(ctx.store, r["id"]) if len(seen) <= 50 else None
        v = decide(a) if a else None
        st = v["state"] if v else {"PASSED": "ship", "FAILED": "no_ship"}.get(r["status"], "unsure")
        why = md(sentence(v["headline"])) if v and st != "ship" else ""
        sc = (a or {}).get("scope") or {}
        checked = f'{sc["checked"]} of {sc["total"]} journeys' if sc.get("total") else f'{r["cases"]} journeys'
        rows.append(f'<a class="prrow {st}" href="{e(purl(ctx.key))}?{e(urlencode({"pr": r["pr"]}))}">'
                    f'<span class=prv>{gate(r["status"])}</span>'
                    f'<span class=prt><b>{e(r["pr_title"] or r["branch"])}</b> <span class=mute>#{e(r["pr"])}</span>'
                    + (f'<span class=prw>{why}</span>' if why else "")
                    + f'</span><span class="prm mute">{e(checked)}<br>{ago(r["created"])}</span></a>')
    body = (project_head(ctx, "Pull requests", "pull-requests") + '<div class=page>'
            + (f'<div class="panel prlist">{"".join(rows)}</div>' if rows else
               empty_state("No pull requests yet", "Assay posts a verdict on every pull request once its step is in CI.",
                           f'<a class=btn href="{e(purl(ctx.key))}/setup">Set up CI</a>'))
            + '</div>')
    return shell(f"Pull requests · {ctx.p['title']}", body, active="projects", snav=snav(ctx, "prs"))


MAIN_CHIP = ' <span class="chip brand">Main branch</span>'
FLAKY_CHIP = ' <span class="chip warn">flaky</span>'


def branches_page(ctx: Ctx) -> str:
    seen, rows = set(), []
    for r in ctx.rows:
        if r["pr"] or r["branch"] in seen:
            continue
        seen.add(r["branch"])
        main = r["branch"] == ctx.home
        rows.append(f'<tr><td>{icon("branch")} <a class=title href="{e(purl(ctx.key))}?{e(urlencode({"branch": r["branch"] or ""}))}">{e(r["branch"])}</a>'
                    f'{MAIN_CHIP if main else ""}</td><td>{gate(r["status"], kind="branch")}</td>'
                    f'<td class=r>{r["pass"]} of {r["cases"]}</td><td class=mono>{e((r["sha"] or "")[:7])}</td>'
                    f'<td class=mute title="{e(when(r["created"]))}">{ago(r["created"])}</td></tr>')
    body = (page_head([*base_crumbs(ctx.p), ("Branches", None)], "Branches",
                      meta=["On a branch every broken journey counts, not only new ones."])
            + f'<div class=page><div class=panel><div class=scroll tabindex=0 role=region aria-label="Table, scrolls sideways"><table class=tbl><thead><tr><th scope=col>Branch</th><th scope=col>Health</th><th scope=col class=r>Journeys kept</th>'
            f'<th scope=col>Commit</th><th scope=col>Analysed</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></div></div>')
    return shell(f"Branches · {ctx.p['title']}", body, active="projects", snav=snav(ctx, "branches"))


# ---- benchmarks (the separate workflow for products with AI) ---------------------------------------------------------

BENCH_HELP = ("A benchmark repeats every journey several times per arm: a model answers differently each time, so one run "
              "proves little. <b>Pass rate</b> is the share of journeys that pass on one attempt, with its 95% interval over "
              "journeys. <b>Δ</b> is the paired difference from the baseline arm; ✱ marks a difference beyond chance. "
              "<b>Flaky</b> journeys passed on some attempts and failed on others.")


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _cibar(r: dict, wide: bool = False) -> str:
    """The pass rate as a point on a 0–100% axis, its 95% interval as whiskers (aria-hidden: the label says it)."""
    lo, hi = (r.get("ci95") or [None, None])[:2]
    v = r.get("pass@1")
    if v is None:
        return ""
    band = (f'<span class=band style="left:{lo * 100:.1f}%;width:{max(0.0, (hi - lo)) * 100:.1f}%"></span>'
            if lo is not None and hi is not None else "")
    return (f'<span class="cibar{" wide" if wide else ""}" aria-hidden=true>{band}'
            f'<span class=pt style="left:{v * 100:.1f}%"></span></span>')


def _rate(r: dict) -> str:
    """The pass rate with its interval, said directly: "72% [64–79]"."""
    lo, hi = (r.get("ci95") or [None, None])[:2]
    rng = f' <span class="mute ci">[{lo * 100:.0f}–{hi * 100:.0f}]</span>' if lo is not None and hi is not None else ""
    return f'<span class=num>{_pct(r.get("pass@1"))}</span>{rng}'


def _sorted_arms(arms: dict, baseline: str | None) -> list:
    """The baseline first, then the other arms from the best pass rate down."""
    return sorted(arms.items(), key=lambda kv: (kv[0] != baseline, -(kv[1].get("pass@1") or 0), kv[0]))


def _delta(d: dict | None, base: str, words: bool = False) -> str:
    if not d:
        return '<span class=mute>baseline</span>'
    m, (lo, hi) = d.get("mean"), (d.get("ci95") or [None, None])[:2]
    if m is None:
        return "—"
    cls = ("good" if m > 0 else "bad") if d.get("significant") else "mute"
    rng = (f' <span class="mute ci">[{lo * 100:+.0f}, {hi * 100:+.0f}]</span>'.replace("-", "−") if lo is not None and hi is not None
           else "")
    say =((" better" if m > 0 else " worse") if d.get("significant") else ", no real difference") if words else ""
    return f'<span class="dl {cls}">{f"{m * 100:+.0f}".replace("-", "−")} pp{" ✱" if d.get("significant") else ""}{say}</span>{rng}'


def _bench_scope(r) -> str:
    return (f'{icon("pr")} PR #{e(r["pr"])}' if r["pr"] else f'{icon("branch")} {e(r["branch"] or "—")}') + \
           (f' · <span class=mono>{e((r["sha"] or "")[:7])}</span>' if r["sha"] else "")


def benchmarks_page(ctx: Ctx) -> str:
    head = page_head([*base_crumbs(ctx.p), ("Benchmarks", None)], "Benchmarks",
                     meta=[f'{len(ctx.benches)} benchmark{"s" if len(ctx.benches) != 1 else ""}', "The separate workflow for AI features."])
    if not ctx.benches:
        head = page_head([*base_crumbs(ctx.p), ("Benchmarks", None)], "Benchmarks", meta=["For features that answer with a model."])
        body = head + ('<div class=page>' + empty_state(
            "No benchmarks yet",
            "A model answers differently every time, so one green run proves little. A benchmark repeats the journeys of "
            "your AI features on two or more arms (models, prompts, builds), or one journey over a dataset of inputs, and "
            "says whether the difference is real. In the repository, in Claude Code, ask for one:",
            _copyable("/assay:bench", "command", cls="code")
            + f'<p class="mute fs-sm mt-2">Or add the <a href="{e(purl(ctx.key))}/setup#benchmark">benchmark workflow</a> by hand.</p>')
            + '</div>')
        return shell(f"Benchmarks · {ctx.p['title']}", body, active="projects", snav=snav(ctx, "bench"))
    rows = []
    for r in ctx.benches:
        arms = json.loads(r["arms"] or "{}")
        cells = "".join(f'<div class=barm><span class=an>{e(a)}</span><b class=ar>{_rate(v)}</b>{_cibar(v)}'
                        f'<span class=ad>{_delta({"mean": v.get("diff"), "significant": v.get("significant")} if v.get("diff") is not None else None, r["baseline"], words=True)}</span></div>'
                        for a, v in _sorted_arms(arms, r["baseline"]))
        flaky = sum(v.get("flaky", 0) for v in arms.values())
        rows.append(f'<tr><td class=nw><a class=title href="{e(purl(ctx.key))}/benchmarks/{r["id"]}">Benchmark #{r["id"]}</a>'
                    f'<div class="mute fs-xs">{e(when(r["created"]))}</div></td>'
                    f'<td class=nw>{_bench_scope(r)}<div class="mute fs-xs">{r["cases"]} journeys × {r["attempts"]} attempts</div></td>'
                    f'<td>{cells}</td><td class="r nw num">{flaky or "<span class=mute>0</span>"}</td></tr>')
    body = (head + f'<div class=page><div class=panel><div class="panel-b fs-sm">{BENCH_HELP}</div></div>'
            f'<div class=panel><div class=scroll tabindex=0 role=region aria-label="Table, scrolls sideways"><table class=tbl><thead><tr><th scope=col>Benchmark</th><th scope=col>Where</th><th scope=col>Arms: pass rate [95% interval], Δ vs baseline</th>'
            f'<th scope=col class=r>Flaky</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></div></div>')
    return shell(f"Benchmarks · {ctx.p['title']}", body, active="projects", snav=snav(ctx, "bench"))


def benchmark_page(ctx: Ctx, bid: str) -> str | None:
    r = next((b for b in ctx.benches if str(b["id"]) == str(bid)), None)
    if r is None:
        return None
    b = ctx.store.bench(r["id"]) or {}
    arms, meta = b.get("arms", {}), b.get("arms_meta", {})
    titles = b.get("case_titles", {})
    right = (f'<a class=btn href="{e(r["ci_url"])}" target=_blank rel=noopener>{icon("external")}CI run</a>' if r["ci_url"] else "")
    head = page_head([*base_crumbs(ctx.p), ("Benchmarks", f"{purl(ctx.key)}/benchmarks"), (f"Benchmark #{r['id']}", None)], f"Benchmark #{r['id']}",
                     right=right, meta=[_bench_scope(r), f'{r["cases"]} journeys × {r["attempts"]} attempts per arm',
                                        f'<span title="{e(when(r["created"]))}">{ago(r["created"])}</span>'])
    arm_rows = []
    for a, v in _sorted_arms(arms, b.get("baseline")):
        env = " ".join(f'{e(k)}={e(val)}' for k, val in (meta.get(a, {}).get("env") or {}).items())
        med = meta.get(a, {}).get("median_journey_s")
        lo, hi = (v.get("ci95") or [None, None])[:2]
        arm_rows.append(f'<tr><th scope=row><b class=ink>{e(a)}</b>{" <span class=chip>baseline</span>" if a == b.get("baseline") else ""}'
                        + (f'<div class="mono fs-xs mute">{env}</div>' if env else "") + '</th>'
                        f'<td class="r nw"><b class="num big">{_pct(v.get("pass@1"))}</b></td>'
                        f'<td class="minw-14">{_cibar(v, wide=True)}<div class="mute fs-xs num">{_pct(lo)}–{_pct(hi)}</div></td>'
                        f'<td class="r num">{_pct(v.get("pass^k"))}</td><td class=nw>{_delta(v.get("diff_vs_baseline"), b.get("baseline"), words=True)}</td>'
                        f'<td class="r num">{len(v.get("flaky", [])) or "<span class=mute>0</span>"}</td>'
                        f'<td class="r nw num">{"—" if med is None else f"{med:.1f} s"}</td></tr>')
    icons = {"PASS": "PASS", "FAIL": "FAIL", "INCONCLUSIVE": "INCONCLUSIVE"}
    arm_order = [a for a, _ in _sorted_arms(arms, b.get("baseline"))]
    case_rows = []
    flaky_any = {c for v in arms.values() for c in v.get("flaky", [])}
    per_case = b.get("per_case", {})

    def share(cid, a):
        vs = per_case[cid].get(a, [])
        return sum(x == "PASS" for x in vs) / len(vs) if vs else None
    differs = {cid for cid in per_case if len({share(cid, a) for a in arm_order if per_case[cid].get(a)}) > 1}
    order = sorted(per_case, key=lambda c: (c not in differs, c not in flaky_any, c))   # where the arms differ first
    for cid in order:
        row = per_case[cid]
        cells = []
        for a in arm_order:
            vs = row.get(a, [])
            npass = sum(x == "PASS" for x in vs)
            dots = "".join(f'<i class="{icons.get(x, "")}" title="attempt {k + 1}: {e(x)}"></i>' for k, x in enumerate(vs))
            cells.append(f'<td class=nw><span class=dots aria-hidden=true>{dots}</span> <span class="num {"mute" if npass == len(vs) else "bad"}">'
                         f'{npass}/{len(vs)}</span><span class=sr> attempts passed</span></td>')
        base, _, rowid = cid.partition("[")
        name = (f'<span class=ink>{e(rowid.rstrip("]"))}</span><div class="fs-xs mute">{e(titles.get(cid) or base)}</div>' if rowid else
                f'<span class=ink>{e(titles.get(cid) or cid)}</span><div class="mono fs-xs mute">{e(cid)}</div>')
        chips = ((' <span class="chip fail">differs</span>' if cid in differs else "") + (FLAKY_CHIP if cid in flaky_any else ""))
        case_rows.append(f'<tr{" class=differs" if cid in differs else ""}><th scope=row>{name.replace("</span>", "</span>" + chips, 1)}</th>'
                         + "".join(cells) + '</tr>')
    n_rows = len(per_case)
    rows_word = "dataset rows" if any("[" in c for c in per_case) else "journeys"
    where_h = (f'{len(differs)} of {n_rows} {rows_word} give different results' if differs else
               f'All {n_rows} {rows_word} give the same results on every arm' if len(arm_order) > 1 else f'{n_rows} {rows_word}')
    try:                                  # the answer first, in the words of the pull request comment
        from ..ci import bench_verdict
        mark, line = bench_verdict(b)
        st = {"✅": "ship", "❌": "no_ship"}.get(mark, "unsure")
        lab = {"ship": "Better", "no_ship": "Worse", "unsure": "No real difference" if len(arms) > 1 else "Measured"}[st]
        cand = next(((a, v["diff_vs_baseline"]) for a, v in arms.items() if a != b.get("baseline") and v.get("diff_vs_baseline")), None)
        reason = ""
        if cand:                          # "haiku against sonnet: −4 pp (±6)"; the label says whether that is beyond chance
            lo, hi = (cand[1].get("ci95") or [None, None])[:2]
            half = f" (±{(hi - lo) * 50:.0f})" if lo is not None and hi is not None else ""
            line = f'`{cand[0]}` against `{b.get("baseline")}`: {f"{cand[1]['mean'] * 100:+.0f}".replace("-", "−")} pp{half}'
            if lo is not None and hi is not None:
                rng = f"{lo * 100:+.0f} to {hi * 100:+.0f} pp".replace("-", "−")
                reason = (f"The 95% interval of the difference, {rng}, includes zero: no real difference at this sample size. "
                          "More attempts or journeys narrow it." if st == "unsure" else
                          f"The 95% interval of the difference, {rng}, excludes zero: the difference is beyond chance.")
        if differs and len(arm_order) > 1:            # where they differ, in one line: the rows a person would look at
            def said(cid):
                name = cid.partition("[")[2].rstrip("]") or titles.get(cid) or cid
                return name + " (" + ", ".join(f'{a} {sum(x == "PASS" for x in per_case[cid].get(a, []))}/{len(per_case[cid].get(a, []))}'
                                               for a in arm_order) + ")"
            shown_rows = sorted(differs)[:3]
            reason += (f' They differ on {len(differs)} of {n_rows}: ' + "; ".join(said(c) for c in shown_rows)
                       + (" and more below" if len(differs) > 3 else "") + ".")
        verdict_html = globals()["label"](st, lab, line.replace("AI features ", "").replace("AI features: ", "").replace("`", ""),
                                          e(reason.strip()), small=True)
    except (KeyError, TypeError, ValueError):
        verdict_html = ""
    # which arm, is the difference real (the label), the arms, then where they differ (those rows first)
    body = (head + f'<div class=page>{verdict_html}'
            f'<section class=panel aria-labelledby=arms-h><div class=panel-h><h2 id=arms-h>Arms</h2></div><div class=scroll tabindex=0 role=region aria-label="Arms, scrolls sideways"><table class=tbl><thead><tr>'
            '<th scope=col>Arm</th><th scope=col class=r>Pass rate</th><th scope=col>95% interval</th><th scope=col class=r>All attempts pass</th><th scope=col>Δ vs baseline</th><th scope=col class=r>Flaky</th>'
            f'<th scope=col class=r>Median journey</th></tr></thead><tbody>{"".join(arm_rows)}</tbody></table></div></section>'
            f'<section class=panel aria-labelledby=rows-h><div class=panel-h><h2 id=rows-h>Where the arms differ</h2><span class="mute fs-sm">{e(where_h)}; one mark per attempt</span></div>'
            '<div class=scroll tabindex=0 role=region aria-label="Results per journey, scrolls sideways"><table class="tbl bench-rows"><thead><tr><th scope=col>Journey</th>'
            + "".join(f'<th scope=col>{e(a)}</th>' for a in arm_order)
            + f'</tr></thead><tbody>{"".join(case_rows)}</tbody></table></div></section>'
            f'<details class="sec bg"><summary>How to read a benchmark</summary><p class="fs-sm mt-2">{BENCH_HELP}</p></details></div>')
    return shell(f"Benchmark #{r['id']} · {ctx.p['title']}", body, active="projects", snav=snav(ctx, "bench"))


ACTION = "RazinAleksandr/assay@v0"
REPO_URL = "https://github.com/RazinAleksandr/assay"     # the action, the Claude Code plugin and the scanner's source
PLUGIN_CMDS = """/plugin marketplace add RazinAleksandr/assay
/plugin install assay@assay
/assay:connect"""


def scanner_source() -> str:
    """The scanner this server was built with: the repository at the tag the action uses (`uvx --from <this>`)."""
    return f"git+{REPO_URL}@v0"


def workflow_yaml(public: str) -> str:
    """The GitHub Actions workflow that connects a repository. The action's default server is the public service."""
    from ..access import PUBLIC_SERVER
    with_host = "" if public.rstrip("/") == PUBLIC_SERVER else f"\n        with:\n          host-url: {public}"
    return f"""# .github/workflows/assay.yml
name: Assay
on:
  push:
    branches: [main]
  pull_request:
jobs:
  assay:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write   # the gate comment
      id-token: write        # sign-in: no token needed
    steps:
      - uses: actions/checkout@v4
      - run: npm ci          # install your app
      - uses: {ACTION}{with_host}"""


def bench_yaml() -> str:
    """The second workflow, for products with AI: repeat the journeys, compare arms (here: two models)."""
    return f"""# .github/workflows/assay-bench.yml
name: Assay benchmark
on:
  workflow_dispatch:
  schedule: [{{cron: "0 6 * * 1"}}]   # weekly
jobs:
  bench:
    runs-on: ubuntu-latest
    permissions: {{contents: read, id-token: write}}
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - uses: {ACTION}
        with:
          command: bench
          anthropic-api-key: ${{{{ secrets.ANTHROPIC_API_KEY }}}}
          args: >-
            --attempts 3
            --arm sonnet=env:ASSISTANT_MODEL=sonnet
            --arm haiku=env:ASSISTANT_MODEL=haiku"""


def copyable(text: str, label: str = "snippet") -> str:
    return _copyable(text, label)


def setup_page(store, key: str | None, public: str, ctx: Ctx | None = None, badge_tok: str | None = None) -> str:
    gl = """# .gitlab-ci.yml
include:
  - remote: """ + REPO_URL + """/raw/v0/ci/assay.gitlab-ci.yml
variables:
  ASSAY_HOST_URL: """ + public + """
# ASSAY_TOKEN: the server's admin token, as a masked CI/CD variable"""
    other = f"""docker run --rm -v "$PWD:/src" -w /src \\
  -e ASSAY_HOST_URL={public} -e ASSAY_TOKEN \\
  assay:0.3 ci"""
    owner = key.split("/")[1] if key and key.startswith("gh/") else None
    badge_src = f"{public}/api/projects/{key}/badge.svg" + (f"?token={badge_tok}" if badge_tok and ctx and ctx.p["visibility"] == "private" else "")
    badge = f"[![Assay]({badge_src})]({public}{purl(key)})" if key else ""
    steps = (f'<ol class=steps-setup>'
             f'<li><h2>Ask Claude Code to connect it</h2><p class="mute mb-2">In the repository, in Claude Code: install the plugin and run '
             f'<span class=mono>/assay:connect</span>. Claude studies the product and writes what it learned to '
             f'<span class=mono>.assay/product.md</span>, writes 5-10 journeys and proves them on the running app, adds a benchmark if '
             f'the product has AI features, adds the workflow below and opens the pull request. About 30 minutes, without you. '
             f'It runs on your own Claude.</p>'
             f'{copyable(PLUGIN_CMDS)}'
             f'<details class=byhand><summary>Or by hand</summary><p class="mute my-2">'
             f'<span class=mono>assay init</span> writes <span class=mono>.assay/</span> with a start command and one journey, and the workflow. '
             f'Then edit <span class=mono>assay.yaml</span> and add journeys (<a href="{REPO_URL}/blob/v0/docs/AUTHORING.md">format</a>).</p>'
             f'{copyable(f"uvx --from {scanner_source()} assay init")}</details></li>'
             f'<li><h2>The CI step it adds</h2><div class="panel mt-2"><div class=tabs role=tablist aria-label="CI system">'
             f'<button type=button role=tab id=t-gh aria-controls=p-gh aria-selected=true>GitHub Actions</button>'
             f'<button type=button role=tab id=t-gl aria-controls=p-gl aria-selected=false tabindex=-1>GitLab CI</button>'
             f'<button type=button role=tab id=t-other aria-controls=p-other aria-selected=false tabindex=-1>Other CI</button></div>'
             f'<div class="tabpane pad" role=tabpanel id=p-gh aria-labelledby=t-gh>{copyable(workflow_yaml(public), "workflow")}'
             f'<p class="mute mt-2">No sign-up and no secrets. <span class=mono>id-token: write</span> lets GitHub vouch for the job, '
             f'so it can only write to its own repository\'s project.</p>'
             f'<p class="mute mt-2">The app cannot start in CI (a GPU, a large backend)? Test where it runs: set '
             f'<span class=mono>base_url</span> to a staging or preview address and drop the start command '
             f'(<a href="{REPO_URL}/blob/v0/docs/AUTHORING.md#testing-a-deployed-app-staging-preview-special-hardware">deployed targets</a>).</p></div>'
             f'<div class="tabpane pad" role=tabpanel id=p-gl aria-labelledby=t-gl hidden>{copyable(gl, "GitLab job")}</div>'
             f'<div class="tabpane pad" role=tabpanel id=p-other aria-labelledby=t-other hidden>{copyable(other, "commands")}</div></div></li>'
             f'<li id=benchmark><h2>For AI features, add the benchmark</h2><p class="mute mb-2">A model answers differently every time, '
             f'so one green run proves little. A second workflow repeats every journey and compares arms: two models, two prompts, '
             f'two builds. Each arm reports its pass rate with a 95% interval and the journeys that flip. A journey with '
             f'<span class=mono>each:</span> runs over a dataset of inputs, one row per case. In Claude Code, '
             f'<span class=mono>/assay:bench</span> writes it for you.</p>{copyable(bench_yaml())}</li>'
             f'<li><h2>Open a pull request</h2><p class=mute>The first analysis creates '
             + (f'the account <a href="/gh/{e(owner)}">{e(owner)}</a> and this project.' if owner else 'your account (named after the GitHub owner) and the project.')
             + ' The pull request gets the gate and a link to the evidence. Private repositories stay private: '
             'their analyses open only from that link.</p>'
             + (f'<div class="row mt-3"><img src="{e(badge_src.replace(public, ""))}" alt="The Assay badge of this project">'
                f'<span class="mono fs-xs anywhere">{e(badge)}</span></div>' if key else "")
             + '</li></ol>'
             f'<p class="mute fs-sm">Free and open source (Apache-2.0). To keep the analyses on your own machine, run the whole '
             f'server yourself: <span class=mono>assay server</span> (<a href="{REPO_URL}#self-hosting">self-hosting</a>).</p>')
    head = (page_head([*base_crumbs(ctx.p), ("Set up CI", None)], "Set up CI") if ctx else
            page_head([("Connect a repository", None)], "Connect a repository", meta=["One CI step. Its first run creates the project, with no tokens to set up."]))
    body = head + f'<div class=page><div class=panel><div class="panel-b pad-lg">{steps}</div></div></div>'
    return shell("Set up CI", body, active="projects", snav=snav(ctx, "setup") if ctx else None)


CLAUDE_CODE = "https://claude.com/claude-code"


def landing_page(store, viewer, public: str) -> str:
    projects, allrows = visible(store, viewer)
    signed_in = bool(viewer and (viewer.user or viewer.admin))
    mine = [p for p in projects if viewer.admin or viewer.mine(p)] if signed_in else []
    cards, _ = project_cards(store, mine[:6], [r for r in allrows if r["project"] in {p["key"] for p in mine[:6]}])
    conds = [("Failed journeys", "1", "required 0", "failed"), ("New blocker issues", "0", "≤ 0", "met"),
             ("New critical issues", "0", "≤ 0", "met"), ("Inconclusive journeys", "0%", "≤ 20%", "met")]
    meas = [("Functional: journeys", "", '<b class="num bad">1</b> broke', "0 fixed"),
            ("Reliability", "A", "<b class=num>0</b> new issues", "of 2 in all"),
            ("Performance", "A", "<b class=num>0</b> new issues", "of 0 in all"),
            ("Accessibility", "B", "<b class=num>1</b> new issue", "of 6 in all"),
            ("UX & Mobile", "C", "<b class=num>1</b> new issue", "of 4 in all"),
            ("AI features", "A", "<b class=num>0</b> new issues", "of 1 in all")]
    overview = (
        '<div class=lp-ov><div class=panel-h><p class=ov-h>Quality gate</p>' + gate("FAILED", kind="gate")
        + '<span class="r mute">1 of 4 conditions failed</span></div>'
        '<table class="tbl conds"><thead><tr><th scope=col>Condition</th><th scope=col class=r>Value</th>'
        '<th scope=col class=r>Required</th><th scope=col>Result</th></tr></thead><tbody>'
        + "".join(f'<tr class={st}><th scope=row>{n}</th><td class="r num">{v}</td><td class="r num">{t}</td>'
                  f'<td><span class="cres {st}">{icon(GATE_COND[st][0])}{GATE_COND[st][1]}</span></td></tr>' for n, v, t, st in conds)
        + '</tbody></table><div class=panel-h><p class=ov-h>Measures</p><span class="r mute">New code: pull request #482</span></div>'
        '<div class=mgrid>' + "".join(f'<div class=mcard><span class=mc-h>{e(n)}{rating(r)}</span><span class=mc-v>{v}</span>'
                                      f'<span class=mc-s>{sub}</span></div>' for n, r, v, sub in meas)
        + '</div></div>')
    sample = label("no_ship", "Don't ship", "1 journey broke",
                   "Mila comments on Noah's post: <code>comments.body</code> was saved cut short in the database: 20 of 90 characters.",
                   [("Pull request", "#482"), ("Checked", "12 of 140 journeys"), ("Run time", "3.1 min"),
                    ("Compared with", "main <span class=mono>4f2c1a9</span>")])
    setup = [("Studies the product", "Reads the code, runs the app and writes what it learned to <code>.assay/product.md</code>: "
                                     "the people, the data, the risks."),
             ("Writes the journeys", "5 to 10 of them, as a few lines of YAML each, and proves every one on the running app."),
             ("Adds a benchmark", "If the product answers with a model: a dataset of realistic inputs and the arms to compare."),
             ("Opens the pull request", "With the CI workflow. Merge it, and every pull request after that gets a verdict.")]
    ways = [("Test the product now", "Ask Claude to run the journeys. It says what is broken, with the screen and the data "
                                     "the app saved."),
            ("The gate on every pull request", "Ship it, Don't ship or Not sure, with the evidence. "
                                               "<code>/assay:fix</code> fixes a failed gate from its brief."),
            ("Benchmark the AI", "<code>/assay:bench</code> compares models, prompts or builds over a dataset and says "
                                 "whether the difference is real."),
            ("The freedom test", "<code>/assay:explore</code>: an agent uses the product freely, and Assay shows it what "
                                 "changed after every action.")]
    e2e = [
        ("It checks what the app saved, not only what the screen shows.",
         "After each journey Assay reads the app's database and API. Posting a comment must add exactly one row with the "
         "full text and change nothing else. A test that only looks at the page passes when the text is saved cut short; "
         "Assay does not."),
        ("It runs the journeys this change can break.",
         "The diff is matched to the routes, tables and files each journey touched on main, plus a small rotating sample. "
         "On our demo app a pull request checks 6 of 18 journeys in 37 seconds. Main runs all of them."),
        ("It points at the line of the change.",
         "A failure names the field that came out wrong and the hunk that writes it: the handler of the route the journey "
         "called, the table it touched. Your coding agent gets this brief as JSON and fixes the change from it."),
        ("It is not the author grading its own work.",
         "The agent that wrote the code does not grade it. Only what the pull request breaks against main blocks it, a "
         "failure blocks only if it fails again on a second run, and a pull request that weakens a check waits for a person."),
        ("It tests where the app runs.",
         "When the product needs a GPU or a large backend, the journeys run against a staging or preview address instead "
         "of a copy started in CI."),
    ]
    explore = [
        ("It sees what the screen does not.",
         "After every click Assay tells the agent what changed in the app's saved data, the new errors and the requests "
         "that failed. A screen that says Saved is not taken at its word."),
        ("A bug counts once it reproduces.",
         "Each finding is written as a journey and run again. It is a bug when that journey fails twice."),
        ("Then it stays a test.",
         "The journey is kept as a regression journey, so the gate catches the bug if it ever comes back."),
    ]
    bench = [
        ("Many runs, not one green run.",
         "A model answers differently every time. Assay repeats each journey several times on every arm: two models, two "
         "prompts or two builds of your app."),
        ("One journey over a dataset.",
         "With <code>each:</code> a journey runs over rows of inputs (inline, CSV, JSON or YAML): every ticket, language "
         "or edge case is a row, and the result is per row."),
        ("Checked by what the app did.",
         "Every attempt is judged by the same checks as the gate: what the assistant saved, sent or refused, not a "
         "reading of its prose."),
        ("A difference you can trust.",
         "Each arm gets a pass rate with its 95% interval, and the difference to the baseline is called better, worse or "
         "no real difference. You see the rows where the arms differ first."),
    ]
    # a real benchmark: Relay, an AI support inbox, 11 dataset rows, 2 attempts per arm
    arms = [("sonnet", "baseline", {"pass@1": 1.0, "ci95": [1.0, 1.0]}),
            ("haiku", "−9 pp, no real difference", {"pass@1": 0.909, "ci95": [0.727, 1.0]})]
    arm_rows = "".join(f'<tr><th scope=row>{n}</th><td class="r nw">{_rate(v)}</td><td>{_cibar(v)}</td>'
                       f'<td>{d}</td></tr>' for n, d, v in arms)
    bench_ex = (label("unsure", "No real difference", "haiku against sonnet: −9 pp (±14)",
                      "The interval of the difference includes zero. They differ on 1 of 11 rows: refund-on-day-29 "
                      "(sonnet 2/2, haiku 0/2).", small=True)
                + '<div class="scroll mt-4" tabindex=0 role=region aria-label="Benchmark arms"><table class=tbl><thead><tr>'
                  '<th scope=col>Arm</th><th scope=col class=r>Pass rate [95%]</th><th scope=col><span class=sr>Interval</span></th>'
                  '<th scope=col>Against the baseline</th></tr></thead>'
                  f'<tbody>{arm_rows}</tbody></table></div>'
                + '<p class="mute fs-sm mt-2">Relay, an AI support inbox: one journey over 11 dataset rows, 2 attempts per arm.</p>')
    e2e_ex = (sample + '<div class="scroll mt-4" tabindex=0 role=region aria-label="What the database saved"><table class=dt><thead><tr>'
              '<th scope=col>Field</th><th scope=col>Expected</th><th scope=col>Saved</th></tr></thead><tbody><tr><td class=mono>comments.body</td>'
              '<td><span class=kept>QA kind words 1a2b</span><span class=lost>: the light in the second frame is lovely</span></td>'
              '<td><span class=kept>QA kind words 1a2b</span><span class=cut>cut at 20 of 90 characters</span></td></tr></tbody></table></div>'
              '<pre class="hunk mt-4" tabindex=0 role=region aria-label="Code change"><span class=at>@@ server/posts.ts:384 @@ POST /posts/:id/comments</span>'
              '<span>     const cid = id();</span><span class=add>+    const preview = d.body.slice(0, 20);</span>'
              '<span class=del>-        d.body,</span><span class=add>+        preview,</span></pre>'
              '<p class="mute fs-sm mt-2">What a pull request gets: the verdict, then its evidence, the saved row against the '
              'expected one and the hunk that wrote it.</p>')
    # what the agent of the freedom test sees after one action: the screen, and what the screen does not say
    explore_ex = (
        '<figure class=xp-ex><div class=xp-screen aria-hidden=true><div class=xp-bar><i></i><i></i><i></i><span>/posts/12</span></div>'
        '<div class=xp-body><p class=xp-post>Noah · The second frame</p><p class=xp-field>QA kind words: the light in the second frame is lovely</p>'
        '<p class=xp-row><span class=xp-btn>Post</span><span class=xp-toast>✓ Saved</span></p></div></div>'
        '<pre class=xp-obs tabindex=0 role=region aria-label="What Assay returns after the action">'
        '<span class=c>&gt; click button "Post"</span>\n'
        '<span class=k>screen</span>           "Saved"\n'
        '<span class=k>data</span>             <span class=chg>~ comments id=7: body\n'
        '                   "QA kind words: the light in the second frame is lovely"\n'
        '                 → "QA kind words: the li"</span>\n'
        '<span class=k>errors</span>           none\n'
        '<span class=k>failed requests</span>  none</pre>'
        '<figcaption>One action of the freedom test. The screen says Saved; the database kept 21 of the 54 characters. '
        'The agent writes that as a journey, it fails twice, and it stays in the suite.</figcaption></figure>')

    def points(items):
        return '<dl class=lp-points>' + "".join(f'<div><dt>{t}</dt><dd>{d}</dd></div>' for t, d in items) + '</dl>'
    body = (
        '<div class=lp>'
        '<section class=lp-hero><div class=lp-copy>'
        '<h1>Your product\'s health and progress, while agents ship</h1>'
        '<p class=lead>Assay tests your product end to end: it starts the app, walks through it the way people use it, checks what really got saved, '
        'and posts the answer on the pull request with the evidence. For AI features it benchmarks models, prompts and builds '
        'over many runs. Claude sets it all up in one prompt.</p>'
        f'<div class=lp-cta><a class="btn primary" href="/setup">Connect a repository</a>'
        f'<a class=btn href="#flow">How it works</a></div>'
        '<p class="mute fs-sm">Free and open source. It runs on your own Claude, and you can host all of it yourself.</p>'
        f'</div><figure class=lp-demo>{overview}<figcaption>What every project gets, in the format of SonarQube: the quality gate '
        'with its conditions, and each quality rated A to E, on new code and overall.</figcaption></figure></section>'

        '<section class=lp-dir id=flow aria-labelledby=flow-h><div class=lp-dir-head>'
        '<h2 id=flow-h>One prompt sets it up</h2>'
        f'<p class=lead>In your app\'s repository, in <a href="{CLAUDE_CODE}">Claude Code</a>, install the plugin and run '
        '<code>/assay:connect</code>. Claude does the rest and opens a pull request.</p></div>'
        '<ol class="howflow lp-flow">'
        + "".join(f'<li><b>{t}</b><span>{d}</span></li>' for t, d in setup)
        + f'</ol><div class=lp-code>{copyable(PLUGIN_CMDS, "plugin commands")}'
        '<p class="mute fs-sm mt-2">Not on Claude Code? <a href="/setup">Set up the CI step by hand</a>.</p></div>'
        '<h3 class=lp-h3>Then, four ways to use it</h3><dl class=lp-ways>'
        + "".join(f'<div><dt>{t}</dt><dd>{d}</dd></div>' for t, d in ways)
        + '</dl></section>'

        '<section class=lp-dir id=journeys aria-labelledby=journeys-h><div class=lp-dir-head>'
        '<h2 id=journeys-h>End-to-end journeys on every pull request</h2>'
        '<p class=lead>A journey is what a person does in your app: sign up, post, pay. You keep them in the repository as a '
        'few lines of YAML; Assay walks them in a real browser and decides whether the change can ship.</p></div>'
        f'<div class=lp-dir-body>{points(e2e)}<div class=lp-ex>{e2e_ex}</div></div></section>'

        '<section class=lp-dir id=explore aria-labelledby=explore-h><div class=lp-dir-head>'
        '<h2 id=explore-h>The agentic freedom test</h2>'
        '<p class=lead>Journeys check what someone thought of. <code>/assay:explore</code> gives an agent a charter and lets it '
        'use the product freely, the way a curious, skeptical person would, to find what nobody wrote a journey for.</p></div>'
        f'<div class=lp-dir-body>{points(explore)}<div class=lp-ex>{explore_ex}</div></div></section>'

        '<section class=lp-dir id=benchmarks aria-labelledby=benchmarks-h><div class=lp-dir-head>'
        '<h2 id=benchmarks-h>Benchmarks for your AI features</h2>'
        '<p class=lead>When a feature answers with a model, one passing run says little. <code>/assay:bench</code> compares '
        'models, prompts or builds on the same journeys and tells you which one to ship.</p></div>'
        f'<div class=lp-dir-body>{points(bench)}<div class=lp-ex>{bench_ex}</div></div></section>'

        '<section class=lp-sec id=open aria-labelledby=open-h><h2 id=open-h>Free, open source, yours to host</h2>'
        '<dl class=lp-ways>'
        '<div><dt>Your own Claude</dt><dd>The Claude Code plugin is the harness: setup, fixes, benchmarks and the freedom test '
        'run in your own Claude Code session.</dd></div>'
        '<div><dt>A free service</dt><dd>The hosted server is free. The first CI run creates the project, with no sign-up '
        'and no tokens.</dd></div>'
        '<div><dt>Or host it yourself</dt><dd><code>assay server</code> runs the whole thing on your machine: one process, '
        f'or one Docker container. <a href="{REPO_URL}#self-hosting">Self-hosting</a>.</dd></div>'
        f'<div><dt>Apache-2.0</dt><dd>The engine, the server, the CI action and the plugin are in <a href="{REPO_URL}">one '
        'repository</a>.</dd></div></dl></section>'
        + (f'<section class=lp-sec><div class=lp-sh><h2>Your projects</h2>'
           f'<a href="/projects">All projects</a></div><ul class=prows role=list>{"".join(cards)}</ul></section>' if cards else "")
        + '</div>')
    return shell("Your product's health while agents ship", body, active="home")


def not_found_page(message: str = "not found") -> str:
    text = message[:1].upper() + message[1:]
    text = "" if text.lower() == "not found" else sentence(text)
    body = (f'<div class=auth><div class=auth-box><h1>Page not found</h1>'
            + (f'<p class=auth-sub>{e(text)}</p>' if text else '<p class=auth-sub>Nothing lives at this address.</p>')
            + '<div class=auth-actions><a class="btn primary" href="/projects">Go to projects</a><a class=btn href="/">Home</a></div></div></div>')
    return shell("Page not found", body)


def docs_page() -> str:
    setup = [("/assay:connect", "One prompt in Claude Code, in your app's repository."),
             ("Study the product", "Claude reads the code, runs the app and writes .assay/product.md."),
             ("Write the journeys", "5 to 10, proven on the running app; a benchmark if the product has AI features."),
             ("Open the pull request", "With the CI workflow. Merged, it gates every pull request after it.")]
    ways = [("Test the product now", "Ask Claude to run the journeys: what is broken, with the screen and the saved data."),
            ("The gate on every pull request", "A verdict on each pull request. <code>/assay:fix</code> fixes a failed gate "
                                               "from its brief and runs the journeys again."),
            ("Benchmark the AI", "<code>/assay:bench</code> compares arms (models, prompts, builds) with repeated attempts, "
                                 "pass rates and 95% intervals."),
            ("The freedom test", "<code>/assay:explore</code>: an agent uses the product freely; after every action Assay shows "
                                 "it what changed in the saved data, new errors and failed requests. A bug it finds is "
                                 "confirmed by a journey that fails twice, then kept as a regression journey.")]
    flow = [("Pull request", "A push to main or a pull request starts the job."),
            ("Start the app", "The command from .assay/assay.yaml, on a fresh copy of its data, or a deployed address."),
            ("Walk the journeys", "The ones this change can break. On main, all of them."),
            ("Read the data", "The app's database, API, storage or canvas, before and after each journey."),
            ("Compare with main", "Only what this pull request breaks can block it, and only if it fails again on a second run."),
            ("Verdict", "A comment on the pull request, the evidence here, the brief for agents.")]
    verdicts = [("PASSED", "Nothing this change touches broke: the steps ran and the data changed exactly as expected."),
                ("FAILED", "The product broke. The page shows the screen, the data the app saved against what the journey "
                           "expected, and the code of the change it suspects."),
                ("INCONCLUSIVE", "The check did not complete: the app did not start, a login failed, or no model was there for an "
                                 "AI check. Never a product bug, and it never blocks a merge.")]
    concepts = (
        ("journey", "Journeys", "What a person does, a few lines of YAML each, stored in the repository: steps, or a goal an AI "
         "agent walks once and CI replays. Checks read the app's data: a like adds exactly one like and nothing else changes."),
        ("data", "Datasets", "<code>each:</code> runs one journey over many inputs, inline or from a CSV, JSON or YAML file. "
         "Each row is its own case, with its own result: what a benchmark of an AI feature runs on."),
        ("screens", "Deployed targets", "When the product needs a GPU or a large backend, Assay tests where it already runs: "
         "a staging stand or a pull request's preview, by its address."),
        ("issues", "Issues and the gate", "Besides journeys, every screen is scanned: console errors, failed requests, "
         "accessibility, layout, vitals. The gate's conditions, each quality's rating and every issue are on the project's "
         "pages, SonarQube-style."),
        ("terminal", "Human or Agent", "Every page has two levels. Human: the verdict and its evidence. Agent: every fact "
         "on one page (failures, suspect code, why each journey ran, history), exportable as JSON or CSV. Each analysis also "
         "has a brief for coding agents."),
        ("alert", "What was not checked", "Each branch lists what nothing measured: qualities without checks, browsers no "
         "journey ran in, screens no journey reached. Unmeasured is never shown as fine."),
    )
    body = (page_head([("How Assay works", None)], "How Assay works",
                      meta=["A quality gate for the running app: one prompt to set up, a verdict on each pull request, the "
                            "evidence on this site."])
            + '<div class=page>'
            '<section class=panel aria-labelledby=d-setup><div class=panel-h><h2 id=d-setup>Set up with one prompt</h2>'
            '<span class="r mute">runs on your own Claude</span></div><div class=panel-b><ol class=howflow>'
            + "".join(f'<li><b>{e(t)}</b><span>{e(d)}</span></li>' for t, d in setup)
            + '</ol></div></section>'
            '<section class=panel aria-labelledby=d-ways><div class=panel-h><h2 id=d-ways>Four ways to use it</h2></div>'
            '<dl class="lp-ways docs-ways">' + "".join(f'<div><dt>{t}</dt><dd>{d}</dd></div>' for t, d in ways) + '</dl></section>'
            '<section class=panel aria-labelledby=d-run><div class=panel-h><h2 id=d-run>What a CI run does</h2></div>'
            '<div class=panel-b><ol class=howflow>'
            + "".join(f'<li><b>{t}</b><span>{d}</span></li>' for t, d in flow)
            + '</ol></div></section>'
            '<section class=panel aria-labelledby=d-verdicts><div class=panel-h><h2 id=d-verdicts>Three verdicts</h2></div>'
            '<div class="grid g3">'
            + "".join(f'<div class=verdict-def><p>{gate(st)}</p><p class=mt-2>{d}</p></div>' for st, d in verdicts)
            + '</div></section>'
            '<section class=panel aria-labelledby=d-parts><div class=panel-h><h2 id=d-parts>The parts</h2></div>'
            '<div class="grid g3 parts">'
            + "".join(f'<div><h3>{icon(ic)} {t}</h3><p>{d}</p></div>' for ic, t, d in concepts)
            + '</div></section>'
            '<section class=panel aria-labelledby=d-open><div class=panel-h><h2 id=d-open>Free and open source</h2></div>'
            '<p class="panel-b lede">Apache-2.0. The engine, the server, the CI action and the Claude Code plugin are in '
            f'<a href="{REPO_URL}">one repository</a>. The hosted server is free; to keep everything on your own machine, run '
            f'<code>assay server</code> (<a href="{REPO_URL}#self-hosting">self-hosting</a>).</p></section>'
            '<p class="mt-6"><a class="btn primary" href="/setup">Connect a repository</a></p></div>')
    return shell("How Assay works", body, active="docs")
