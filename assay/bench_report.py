"""bench.html: is the candidate better than the baseline, by how much, and can we trust the difference?"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .theme import e, page


def _majority(vs: list[str]) -> str:
    vs = [v for v in vs if v != "INCONCLUSIVE"] or vs
    return Counter(vs).most_common(1)[0][0] if vs else "—"


def _ci_chart(res: dict, roles: dict) -> str:
    """pass@1 per arm with its 95% bootstrap interval, on one 0–100% scale."""
    arms = list(res["arms"].items())
    x0, w, row, top = 150, 420, 40, 30
    h = top + row * len(arms) + 24
    X = lambda v: x0 + w * v
    out = [f'<svg viewBox="0 0 620 {h}" width="100%" style="max-width:720px;display:block;font-family:var(--sans)" role="img" '
           f'aria-label="pass@1 per build with 95% confidence interval">']
    for t in (0, .25, .5, .75, 1):
        out.append(f'<line x1="{X(t):.1f}" x2="{X(t):.1f}" y1="{top - 10}" y2="{h - 20}" stroke="var(--rule)"/>'
                   f'<text x="{X(t):.1f}" y="{h - 5}" font-size="11" fill="var(--mute)" text-anchor="middle">{int(t * 100)}%</text>')
    for i, (name, r) in enumerate(arms):
        y = top + i * row + 10
        lo, hi = r["ci95"]
        p = r["pass@1"] or 0
        base = i == 0
        col = "var(--mute)" if base else "var(--brand)"
        out.append(f'<text x="0" y="{y + 4}" font-size="12.5" font-weight="600" fill="var(--ink)">{e(name)}</text>'
                   f'<text x="0" y="{y + 19}" font-size="11" fill="var(--mute)">{e(roles.get(name, ""))}</text>'
                   f'<line x1="{X(lo):.1f}" x2="{X(hi):.1f}" y1="{y}" y2="{y}" stroke="{col}" stroke-width="2"/>'
                   f'<line x1="{X(lo):.1f}" x2="{X(lo):.1f}" y1="{y - 6}" y2="{y + 6}" stroke="{col}" stroke-width="2"/>'
                   f'<line x1="{X(hi):.1f}" x2="{X(hi):.1f}" y1="{y - 6}" y2="{y + 6}" stroke="{col}" stroke-width="2"/>'
                   f'<circle cx="{X(p):.1f}" cy="{y}" r="5.5" fill="{col}"/>'
                   f'<text x="{X(hi) + 10:.1f}" y="{y + 4}" font-size="12" fill="var(--ink)">{p:.0%}</text>')
    out.append("</svg>")
    return "".join(out)


def _cells(vs: list[str]) -> str:
    col = {"PASS": "var(--pass)", "FAIL": "var(--fail)", "INCONCLUSIVE": "var(--inc)"}
    return "".join(f'<span title="{v}" style="display:inline-block;width:12px;height:12px;margin-right:3px;background:{col.get(v, "var(--rule)")}"></span>'
                   for v in vs)


def write_html(root: Path, res: dict) -> Path:
    arms = list(res["arms"])
    base = res["baseline"]
    meta = res.get("arms_meta", {})
    titles = res.get("case_titles", {})
    # which arm is the candidate: the last one that is not a copy of the baseline build
    cand = next((a for a in reversed(arms[1:]) if meta.get(a, {}).get("project") != meta.get(base, {}).get("project")), arms[-1])
    noise = [a for a in arms[1:] if a != cand]
    maj = {c: {a: _majority(per[a]) for a in arms} for c, per in res["per_case"].items()}
    fixed = [c for c in maj if maj[c][base] == "FAIL" and maj[c][cand] == "PASS"]
    regressed = [c for c in maj if maj[c][base] == "PASS" and maj[c][cand] == "FAIL"]
    rb, rc = res["arms"][base], res["arms"][cand]
    d = rc.get("diff_vs_baseline", {})
    flaky = sorted(set(sum((r["flaky"] for r in res["arms"].values()), [])))
    nf = res["arms"][noise[0]].get("diff_vs_baseline", {}) if noise else None
    runs = res["cases"] * res["attempts"] * len(arms)

    if fixed and not regressed:
        head = f"{cand} passes {len(fixed)} more journey{'s' if len(fixed) > 1 else ''} than {base}, with no regressions"
        status, label = "PASSED", "Candidate is better"
    elif regressed:
        head = f"{cand} breaks {len(regressed)} journey{'s' if len(regressed) > 1 else ''} that pass on {base}"
        status, label = "FAILED", "Candidate regresses"
    else:
        head = f"No difference between {cand} and {base}"
        status, label = "INCONCLUSIVE", "No measurable change"
    sentence = (f"pass@1 {rb['pass@1']:.0%} → {rc['pass@1']:.0%}, difference {d.get('mean', 0):+.0%} "
                f"(95% interval {d.get('ci95', [0, 0])[0]:+.0%} … {d.get('ci95', [0, 0])[1]:+.0%}). ")
    if nf is not None:
        sentence += ("The same build run twice gives identical results, so the gate adds no noise of its own. "
                     if abs(nf.get("mean", 0)) < 1e-9 else f"The same build run twice differs by {nf.get('mean', 0):+.0%}: that is the noise floor. ")
    sentence += f"{len(flaky)} flaky journeys in {runs} runs."

    out = [f'<header class=bar><div class=logo>assay<span>.</span><small>Benchmark</small></div>'
           f'<div class=meta>{e(res.get("app", ""))} · {e(res.get("generated", ""))} · {res["cases"]} journeys × '
           f'{res["attempts"]} attempts × {len(arms)} builds</div></header>',
           f'<section class="verdict {status}"><div class=label>{e(label)}</div><h1>{e(head)}</h1><p>{e(sentence)}</p></section>']
    figs = [(f'{rc["pass@1"]:.0%}', f"pass@1 of {cand} (baseline {rb['pass@1']:.0%})"),
            (str(len(fixed)), "journeys fixed: fail on baseline, pass on candidate"),
            (str(len(regressed)), "regressions: pass on baseline, fail on candidate"),
            (str(runs), f"journey runs, each from a clean copy of the data · {len(flaky)} flaky")]
    out.append('<div class=figs>' + "".join(f'<div class=fig><b>{v}</b><span>{e(l)}</span></div>' for v, l in figs) + '</div>')

    roles = {base: "baseline", cand: "candidate", **{a: "same build again" for a in noise}}
    out.append(f'<h2>Pass rate by build</h2>{_ci_chart(res, roles)}'
               '<p class=note style="margin-top:6px">Dot: pass@1, the share of journeys that pass, averaged over attempts. '
               'Whiskers: 95% bootstrap interval over journeys. A copy of the baseline shows the noise floor.</p>')

    rows = []
    for a in arms:
        r = res["arms"][a]
        dd = r.get("diff_vs_baseline")
        m = meta.get(a, {})
        delta = "—" if not dd else f"{dd['mean']:+.0%} <span class=mute>({dd['ci95'][0]:+.0%} … {dd['ci95'][1]:+.0%})</span>"
        rows.append(f'<tr><td><b style="color:var(--ink)">{e(a)}</b><div class="mute" style="font-size:12px">{e(m.get("title", ""))}</div></td>'
                    f'<td class=r>{r["pass@1"]:.0%}</td><td class="r mute">{r["ci95"][0]:.0%}–{r["ci95"][1]:.0%}</td>'
                    f'<td class=r>{r["pass^k"]:.0%}</td>'
                    f'<td class=r>{delta}</td>'
                    f'<td class=r>{len(r["flaky"])}</td><td class=r>{(str(round(m["median_journey_s"], 1)) + " s") if m.get("median_journey_s") else "—"}</td></tr>')
    out.append('<h2>Builds</h2><div class=scroll><table><thead><tr><th>Build</th><th class=r>pass@1</th><th class=r>95% interval</th>'
               '<th class=r>pass^k</th><th class=r>Δ vs baseline</th><th class=r>Flaky</th><th class=r>Median journey</th></tr></thead>'
               f'<tbody>{"".join(rows)}</tbody></table></div>'
               '<p class=note style="margin-top:6px">pass^k: share of journeys that pass in every attempt. '
               'Δ: paired difference on the same journeys and attempts.</p>')

    if fixed or regressed:
        ch = "".join(f'<tr><td><span class="st {"PASS" if c in fixed else "FAIL"}">{"fixed" if c in fixed else "regressed"}</span></td>'
                     f'<td><span style="color:var(--ink)">{e(titles.get(c, c))}</span><div class="mono mute">{e(c)}</div></td>'
                     f'<td>{_cells(res["per_case"][c][base])}</td><td>{_cells(res["per_case"][c][cand])}</td></tr>'
                     for c in fixed + regressed)
        consistent = all(set(res["per_case"][c][base]) == {"FAIL"} and set(res["per_case"][c][cand]) == {"PASS"} for c in fixed) \
            and all(set(res["per_case"][c][base]) == {"PASS"} and set(res["per_case"][c][cand]) == {"FAIL"} for c in regressed)
        out.append(f'<h2>What changed</h2><div class=scroll><table><thead><tr><th>Change</th><th>Journey</th><th>{e(base)}</th>'
                   f'<th>{e(cand)}</th></tr></thead><tbody>{ch}</tbody></table></div>'
                   + ('<p class=note style="margin-top:6px">Every change repeats in all attempts, so it is a property of the build, '
                      'not luck. With 18 journeys the interval for the overall pass rate stays wide; the per-journey evidence '
                      'above is what decides the merge.</p>' if consistent else ''))

    allrows = "".join(
        f'<tr><td><span style="color:var(--ink)">{e(titles.get(c, c))}</span><div class="mono mute">{e(c)}</div></td>'
        + "".join(f'<td>{_cells(per[a])}</td>' for a in arms) + "</tr>"
        for c, per in res["per_case"].items())
    out.append(f'<h2>Every journey, every attempt</h2><div class=scroll><table><thead><tr><th>Journey</th>'
               + "".join(f'<th>{e(a)}</th>' for a in arms) + f'</tr></thead><tbody>{allrows}</tbody></table></div>'
               '<p class=note style="margin-top:6px"><span style="color:var(--pass)">■</span> pass '
               '<span style="color:var(--fail)">■</span> fail <span style="color:var(--inc)">■</span> inconclusive (environment). '
               'An attempt that is inconclusive in any build is dropped for every build, so builds are compared on the same set.</p>')
    out.append(f'<footer class=foot><span>assay 0.1 · {e(root.name)}</span><span>Method: every attempt starts the build from its golden '
               f'data; bootstrap over journeys, 2000 resamples.</span></footer>')
    f = root / "bench.html"
    f.write_text(page(f"{res.get('app', '')} benchmark", "".join(out)))
    return f
