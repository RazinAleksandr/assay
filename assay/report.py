"""Reports: report.html (dashboard), report.md (PR/MR comment), junit.xml (CI test tab)."""
from __future__ import annotations

import html
import json
from pathlib import Path
from xml.sax.saxutils import escape as xesc

from .model import DIMENSIONS

RATING_COLOR = {"A": "#1f9d55", "B": "#7cb342", "C": "#f2b705", "D": "#f07c1f", "E": "#d93025"}
SEV_ICON = {"blocker": "⛔", "critical": "🔴", "major": "🟠", "minor": "🟡", "info": "⚪"}
VERDICT_ICON = {"PASS": "✅", "FAIL": "❌", "INCONCLUSIVE": "⚠️"}
GATE_ICON = {"PASSED": "✅", "FAILED": "❌", "INCONCLUSIVE": "⚠️"}


def write_all(run_dir: Path, a: dict) -> None:
    (run_dir / "report.md").write_text(markdown(a, run_dir))
    (run_dir / "junit.xml").write_text(junit(a))
    (run_dir / "report.html").write_text(dashboard(a))


# ---- markdown: the verdict a person reads in five seconds ------------------------------------------------------------------

def duration_s(a: dict) -> float | None:
    from datetime import datetime
    run = a.get("run") or {}
    try:
        return (datetime.fromisoformat(run["finished"]) - datetime.fromisoformat(run["started"])).total_seconds()
    except Exception:
        return None


def _minutes(sec: float | None) -> str:
    if sec is None:
        return ""
    return f"{sec:.0f} s" if sec < 90 else f"{sec / 60:.0f} min"


def scope_line(a: dict, v: dict | None = None) -> str:
    """Honest volume: how many journeys were checked of how many, why, how long, compared with what."""
    from .explain import verdict
    v = v or verdict(a)
    sc = v["scope"]
    n, total = sc.get("checked", 0), sc.get("total", 0)
    if sc.get("mode") == "auto" and total and n < total:
        what = f"Checked {n} of {total} journeys ({sc.get('why') or 'touched by this change'})"
    elif sc.get("mode") == "filter" and total and n < total:
        what = f"Checked {n} of {total} journeys ({sc.get('why')})"
    else:
        what = f"Checked {'all ' if n > 1 else ''}{n} journey{'s' if n != 1 else ''}"
    parts = [what]
    if (d := _minutes(duration_s(a))):
        parts.append(d)
    if a.get("baseline"):
        parts.append(f"compared with `{a['baseline']}`")
    return " · ".join(parts)


def verdict_markdown(a: dict, *, url: str | None = None, brief_url: str | None = None, heading: str = "###") -> str:
    """Four lines before the link: verdict and title, the headline, what the person must do, the honest scope.
    Everything else (more problems, known bugs, how an agent gets the brief) folds away under the link."""
    from .explain import verdict
    v = verdict(a)
    links = " · ".join(x for x in ((f"[Evidence →]({url})" if url else ""), (f"[Brief for agents]({brief_url})" if brief_url else "")) if x)
    lines = [f"{heading} {v['icon']} {v['label']}: {v['title']}", v["headline"],
             f"**From you:** {v['action_text']}" + (f" {links}" if links else ""),
             f"<sub>{scope_line(a, v)}</sub>"]
    more = []
    blocking = [p for p in v["problems"]][1:]
    if blocking:
        more.append(f"**{len(blocking)} more:** " + "; ".join(p["phrase"] for p in blocking[:6])
                    + (f"; and {len(blocking) - 6} more" if len(blocking) > 6 else ""))
    titles = {c["case_id"]: (c.get("promise") or c.get("title") or c["case_id"]) for c in a.get("cases", [])}
    ch = v["changes"]
    if ch["fixed"]:
        more.append("**Fixed:** " + "; ".join(titles.get(x, x) for x in ch["fixed"][:6]))
    if ch["known_fixed"]:
        more.append("**Known bugs that now pass** (remove their `known-bug` tag so they guard against a comeback): "
                    + "; ".join(f"{titles.get(x, x)} (`{x}`)" for x in ch["known_fixed"][:6]))
    if ch["known_failing"]:
        more.append(f"**Known bugs, still failing** (tagged `known-bug`, never block): "
                    + "; ".join(f"`{x}`" for x in ch["known_failing"][:8]))
    bg = [p for p in v["background"]]
    if bg:
        more.append("**Not blocking:** " + "; ".join(p["phrase"] + (f" ({p['note']})" if p.get("note") else "") for p in bg[:6]))
    if brief_url:
        more.append("**For your agent:** in Claude Code run `/assay:fix " + brief_url + "`; elsewhere "
                    f"`assay brief {brief_url}` prints the failures, the suspect code and how to reproduce them as JSON.")
    if more:
        lines += ["", "<details><summary>Details</summary>", "", *[f"- {m}" for m in more], "", "</details>"]
    return "\n".join(lines) + "\n"


def markdown(a: dict, run_dir: Path | None = None) -> str:
    """report.md of a local run: the same verdict as the pull request comment, then every journey that is not a pass."""
    from .explain import FLAKY_NOTE, flaky_of, journey_phrase
    out = [verdict_markdown(a, heading="##")]
    bad, flaky = [c for c in a["cases"] if c["verdict"] != "PASS"], flaky_of(a)
    if bad:
        out += ["", "### Journeys that need attention", ""]
        out += [f"- 🔁 `{c['case_id']}` {journey_phrase(c)} ({FLAKY_NOTE}: not blocking)" if c["case_id"] in flaky else
                f"- {VERDICT_ICON[c['verdict']]} `{c['case_id']}` {journey_phrase(c)}" for c in bad]
    where = run_dir if run_dir else f"{a['run'].get('project', '.assay')}/runs/{a['run'].get('id', '')}"
    out += ["", f"For a coding agent: `assay brief {where}` (JSON: failures, expected against saved values, how to reproduce).", ""]
    return "\n".join(out)


def _md(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


# ---- junit --------------------------------------------------------------------------

def junit(a: dict) -> str:
    """A flaky journey (failed, then passed on a second run) is a `flakyFailure`, as Maven Surefire writes a test that
    passed on a rerun: test tabs show it as passed and flaky."""
    from .explain import flaky_of
    cases, flaky = a["cases"], flaky_of(a)
    out = [f'<?xml version="1.0" encoding="UTF-8"?>',
           f'<testsuite name="assay.{xesc(a["app"]["name"])}" tests="{len(cases)}" '
           f'failures="{sum(c["verdict"] == "FAIL" and c["case_id"] not in flaky for c in cases)}" '
           f'skipped="{sum(c["verdict"] == "INCONCLUSIVE" for c in cases)}">']
    for c in cases:
        out.append(f'  <testcase classname="{xesc(a["app"]["name"])}" name="{xesc(c["case_id"])}" time="{c["duration_s"]}">')
        if c["case_id"] in flaky:
            out.append(f'    <flakyFailure message="{xesc(c["summary"][:300], {chr(34): "&quot;"})}"/>')
        elif c["verdict"] == "FAIL":
            out.append(f'    <failure message="{xesc(c["summary"][:300], {chr(34): "&quot;"})}"/>')
        elif c["verdict"] == "INCONCLUSIVE":
            out.append(f'    <skipped message="{xesc(c["summary"][:300], {chr(34): "&quot;"})}"/>')
        out.append("  </testcase>")
    out.append("</testsuite>")
    return "\n".join(out) + "\n"


# ---- html report ----------------------------------------------------------------------

DASH_CSS = """
.vh{border-left:4px solid var(--mute);padding:4px 0 4px 18px;margin-bottom:8px}
.vh.ship{border-color:var(--pass)}.vh.no_ship{border-color:var(--fail)}.vh.unsure{border-color:var(--inc)}
.vh .label{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.ship .label{color:var(--pass)}.no_ship .label{color:var(--fail)}.unsure .label{color:var(--inc)}
.vh h1{margin-top:2px}.vh .head{font-size:15.5px;color:var(--ink);margin:10px 0 0}
.vh .act{margin:8px 0 0}.vh .scope{font-size:12.5px;color:var(--mute);margin:10px 0 0}
code{font-family:var(--mono);font-size:.88em;background:var(--fill);padding:0 4px;border-radius:3px;overflow-wrap:anywhere}
.ev{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:22px;padding:16px 0;border-bottom:1px solid var(--rule)}
.ev:last-child{border-bottom:0}.ev h3 a{color:var(--ink);text-decoration:none}
.ev .ph{margin:6px 0 0;color:var(--ink)}.ev img{width:100%;border:1px solid var(--rule);display:block}
.ev h4{font-size:11.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--mute);margin:14px 0 6px}
.kept{background:var(--pass-bg)}.lost{color:var(--fail);text-decoration:line-through;background:var(--fail-bg)}
.cut{font-size:11.5px;font-weight:600;color:var(--fail);margin-left:6px;white-space:nowrap}
pre.hunk{margin:0;background:#0f1420;color:#c9d2df;padding:8px 0;overflow:auto;font:12px/1.55 var(--mono)}
pre.hunk span{display:block;padding:0 12px;white-space:pre}pre.hunk .add{background:rgba(46,160,67,.22)}pre.hunk .del{background:rgba(248,81,73,.2)}
pre.err{margin:0;background:var(--fill);padding:8px 10px;font:12px/1.5 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
ul.plain{list-style:none;margin:0;padding:0}ul.plain li{padding:7px 0;border-bottom:1px solid var(--rule);overflow-wrap:anywhere}
ul.plain li:last-child{border-bottom:0}
@media (max-width:760px){.ev{grid-template-columns:minmax(0,1fr)}}
"""


def dashboard(a: dict, header: str | None = None, context: str = "", foot: str | None = None) -> str:
    """The run report, for a person: the verdict, the evidence of what broke (screen, data, suspect code), every journey,
    other findings in words. The Assay server may pass its own navigation `header`, a `context` block and `foot`;
    the standalone file uses the defaults. Paths are relative to the run folder."""
    from .explain import issue_phrase, markup_html as md, subject, verdict
    from .theme import e, page
    v = verdict(a)
    s = a["summary"]
    title = a["app"].get("title") or a["app"]["name"]
    run = a.get("run", {})
    cases = {c["case_id"]: c for c in a.get("cases", [])}
    dur = _minutes(duration_s(a))
    j = a.get("judge", {})
    out = [header or (f'<header class=bar><div class=logo>assay<span>.</span><small>Run report</small></div>'
           f'<div class=meta>{e(title)} · run {e(run.get("id", ""))} · {e(a.get("generated", ""))} · {e(a["app"].get("base_url", ""))}</div></header>'),
           context, f"<style>{DASH_CSS}</style>"]
    import re
    m = re.match(r"(\w+)([,:]?)(.*)", v["action_text"], re.S)
    act_word, act_sep, act_rest = (m[1], m[2], m[3]) if m else (v["action_text"], "", "")
    out.append(f'<section class="vh {v["state"]}"><div class=label>{e(v["icon"])} {e(v["label"])}</div><h1>{e(v["title"][:1].upper() + v["title"][1:])}</h1>'
               f'<p class=head>{md(v["headline"])}</p><p class=act><b>From you: {e(act_word)}</b>{e(act_sep)}{md(act_rest)}</p>'
               f'<p class=scope>{md(scope_line(a, v))}</p></section>')

    def pair(exp, act):
        if isinstance(exp, str) and isinstance(act, str) and act and exp.startswith(act) and len(act) < len(exp):
            return (f'<span class=kept>{e(act)}</span><span class=lost>{e(exp[len(act):])}</span>',
                    f'<span class=kept>{e(act)}</span><span class=cut>cut at {len(act)} of {len(exp)}</span>')
        sh = lambda x: e(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str)[:400])
        return sh(exp), sh(act)

    def evidence(c):
        f = c.get("failure") or {}
        bad = next((st for st in c.get("steps", []) if st.get("ok") is False and st.get("screenshot")), None)
        shot = f'cases/{c["case_id"]}/{bad["screenshot"]}' if bad else c.get("screenshot")
        data = ""
        if f.get("fields"):
            rows = "".join(f'<tr><td class=mono>{e((f.get("path") + "." if f.get("path") else "") + k)}</td>'
                           + "".join(f"<td>{x}</td>" for x in pair(d.get("expected"), d.get("actual"))) + "</tr>"
                           for k, d in f["fields"].items())
            data = (f'<h4>What the {e(f.get("store") or "app")} saved</h4><div class=scroll><table><thead><tr><th>Field</th><th>Expected</th>'
                    f'<th>Saved</th></tr></thead><tbody>{rows}</tbody></table></div>')
        elif f.get("errors") or (f.get("step") or {}).get("error"):
            data = f'<h4>The error</h4><pre class=err>{e(chr(10).join(map(str, f.get("errors") or [f["step"]["error"]]))[:1200])}</pre>'
        sus = (c.get("suspects") or [])[:1]
        code_html = "".join(
            f'<h4>Suspect code in this change</h4><div class=mono>{e(x.get("file"))}:{e(x.get("lines"))}</div>'
            f'<p class=note>{md(x.get("why"))}</p><pre class=hunk>'
            + "".join(f'<span class="{"add" if ln.startswith("+") else "del" if ln.startswith("-") else ""}">{e(ln) or " "}</span>'
                        for ln in (x.get("hunk") or "").splitlines()) + '</pre>' for x in sus)
        img = f'<a href="{e(shot)}"><img loading=lazy src="{e(shot)}" alt="the screen where it broke"></a>' if shot else ""
        return (f'<article class=ev><div><h3>{e(subject(c))}</h3><p class=ph>{md(f.get("phrase") or c.get("summary") or "")}</p>'
                f'{data}{code_html}</div><div>{img}</div></article>')

    jp = [p for p in v["problems"] if p["kind"] == "journey" and p["case_id"] in cases]
    others = [p for p in v["problems"] if p["kind"] != "journey"]
    if jp or others:
        lines = "".join("<li>" + md(p["phrase"]) + "</li>" for p in others)
        out.append(f'<h2>{"What broke" if v["state"] == "no_ship" else "Problems found"}</h2>'
                   + "".join(evidence(cases[p["case_id"]]) for p in jp) + (f"<ul class=plain>{lines}</ul>" if others else ""))
    if v["background"]:
        out.append('<h2>Not blocking</h2><ul class=plain>' + "".join(
            f'<li>{md(p["phrase"])}{" <span class=mute>(" + e(p["note"]) + ")</span>" if p.get("note") else ""}</li>' for p in v["background"]) + '</ul>')

    rows = []
    for c in a.get("cases", []):
        shot = c.get("screenshot")
        word = {"PASS": "Works", "FAIL": "Broken", "INCONCLUSIVE": "Not checked"}.get(c["verdict"], c["verdict"])
        st = c["verdict"]
        if c["case_id"] in v["changes"]["flaky"]:         # failed, then passed on a second run: amber, not red
            word, st = "Flaky", "INCONCLUSIVE"
        rows.append(f'<tr><td><span class="st {st}">{e(word)}</span></td>'
                    f'<td><b style="font-weight:500;color:var(--ink)">{e(subject(c))}</b><div class="mono mute">{e(c["case_id"])}</div></td>'
                    f'<td class=hide-sm>{e(c.get("viewport"))}</td><td class="r hide-sm">{(c.get("duration_s") or 0):.1f} s</td>'
                    f'<td class=hide-sm>{f"<img class=thumb loading=lazy src={chr(34)}{e(shot)}{chr(34)} alt={chr(34)}{chr(34)}>" if shot else ""}</td></tr>')
    if rows:
        out.append(f'<h2>All journeys ({len(rows)})</h2><div class=scroll><table><thead><tr><th>Now</th><th>Journey</th>'
                   f'<th class=hide-sm>Device</th><th class="r hide-sm">Time</th><th class=hide-sm>End screen</th></tr></thead>'
                   f'<tbody>{"".join(rows)}</tbody></table></div>')

    told = {p.get("rule") for p in others}
    found = list(dict.fromkeys(issue_phrase(i) for i in a.get("issues", []) if i.get("rule") != "CASE-FAIL" and i.get("rule") not in told))
    if found:
        out.append(f'<h2>Other findings</h2><p class=note>Found by the always-on sensors (console, network, accessibility, layout, speed) '
                   f'while the journeys ran and the screens were scanned. They block a pull request only when new.</p>'
                   f'<ul class=plain>{"".join(f"<li>{md(x)}</li>" for x in found[:40])}</ul>'
                   + (f'<p class=note>and {len(found) - 40} more</p>' if len(found) > 40 else ""))

    if a.get("pages"):
        figs = "".join(
            f'<figure><img loading=lazy src="{e(p.get("screenshot"))}" alt=""><figcaption><span style="color:var(--ink)">{e(p["route"])}</span>'
            f' · {e(p["viewport"])}</figcaption></figure>' for p in a["pages"] if p.get("screenshot"))
        out.append(f'<h2>Scanned screens ({s.get("pages_scanned", len(a["pages"]))})</h2>'
                   f'<p class=note>The crawler follows links; it does not open menus, dialogs or editor modes. Journeys check those.</p>'
                   f'<div class=gallery>{figs}</div>')

    out.append(f'<footer class=foot><span>{foot or ("assay · evidence in runs/" + e(run.get("id", "")))} · re-grade with <span class=mono>assay grade</span>'
               f' · for agents: <span class=mono>assay brief</span></span>'
               f'<span>{("took " + dur + " · ") if dur else ""}AI judge {e(j.get("backend") or "off")}{("/" + e(j.get("model"))) if j.get("model") else ""}: '
               f'{j.get("calls", 0)} calls, ${j.get("cost_usd", 0) or 0:.2f}</span></footer>')
    return page(f"{title} run report", "".join(out))


def _first(msg: str, n: int = 220) -> str:
    """Merged issues carry one message per occurrence; show the first, trimmed."""
    m = (msg or "").split(" | ")[0]
    return m if len(m) <= n else m[: n - 1] + "…"
