"""Portfolio: one page over many apps — for each, healthy or not, and how many of its journeys work."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .explain import known_of, verdict
from .theme import CSS, e as _e


def latest_analysis(project: Path, run_id: str | None = None) -> Path | None:
    runs = project / "runs"
    if run_id and (runs / run_id / "analysis.json").exists():
        return runs / run_id / "analysis.json"
    cands = [p for p in runs.glob("*/analysis.json") if not p.parent.name.startswith(("dev", "doctor", "bench"))]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def build(projects: list[Path], out: Path, run_id: str | None = None) -> dict:
    rows = []
    for proj in projects:
        f = latest_analysis(proj, run_id)
        if not f:
            continue
        a = json.loads(f.read_text())
        a["_report"] = os.path.relpath(f.parent / "report.html", out.parent)
        rows.append(a)
    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<title>Assay portfolio</title><style>{CSS}"
             ".wrap{max-width:1120px;margin:0 auto}header{border-bottom:2px solid var(--ink);padding-bottom:10px;margin-bottom:18px}"
             ".kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:0;border-block:1px solid var(--rule)}"
             ".kpi{padding:12px 14px 12px 0}.kpi b{display:block;font-size:24px;font-weight:600;color:var(--ink)}.kpi span{font-size:12px;color:var(--mute)}"
             ".card{border-bottom:1px solid var(--rule);padding:14px 0}.sub{font-size:12px;color:var(--mute)}"
             ".gate{font-size:11.5px;font-weight:600;letter-spacing:.04em}.gate.PASSED{color:var(--pass)}.gate.FAILED{color:var(--fail)}.gate.INCONCLUSIVE{color:var(--inc)}"
             ".app{display:grid;grid-template-columns:1.3fr 2fr 1fr;gap:16px;align-items:center}"
             "@media(max-width:760px){.app{grid-template-columns:1fr}}</style></head><body><div class=wrap>"
             "<header><div><h1>Portfolio: are the running apps healthy</h1><div class=sub>latest analysis per app</div></div></header>"]
    tot = {"cases": 0, "PASS": 0, "broken": 0, "INCONCLUSIVE": 0, "pages": 0}
    for a in rows:
        s = a["summary"]
        known = known_of(a)
        a["_broken"] = sum(c["verdict"] == "FAIL" and c["case_id"] not in known for c in a.get("cases", []))
        for k in ("cases", "PASS", "INCONCLUSIVE"):
            tot[k] += s[k]
        tot["broken"] += a["_broken"]
        tot["pages"] += s["pages_scanned"]
    parts.append(f"<div class=kpis><div class='card kpi'><b>{len(rows)}</b><span>apps</span></div>"
                 f"<div class='card kpi'><b>{tot['PASS']}/{tot['cases']}</b><span>journeys work</span></div>"
                 f"<div class='card kpi'><b>{tot['broken']}</b><span>broken</span></div>"
                 f"<div class='card kpi'><b>{tot['INCONCLUSIVE']}</b><span>could not be checked</span></div></div><h2>Apps</h2>")
    word = {"ship": "Healthy", "no_ship": "Broken", "unsure": "Not sure"}
    gcls = {"ship": "PASSED", "no_ship": "FAILED", "unsure": "INCONCLUSIVE"}
    for a in rows:
        s, v = a["summary"], verdict(a)
        parts.append(f"<div class='card app' style='margin-bottom:12px'><div><b style='font-size:16px'>{_e(a['app'].get('title') or a['app']['name'])}</b>"
                     f"<div class=sub><a href='{_e(a['_report'])}'>report</a> · run {_e(a['run'].get('id', ''))}</div>"
                     f"<div class='gate {gcls[v['state']]}' style='font-size:13px;padding:4px 0;margin-top:8px;display:inline-flex'>"
                     f"{v['icon']} {word[v['state']]}</div></div>"
                     f"<div style='color:var(--ink)'>{_e(v['headline'])}</div>"
                     f"<div class=sub><b style='color:var(--ink)'>{s['PASS']} of {s['cases']}</b> journeys work"
                     + (f" · {a['_broken']} broken" if a["_broken"] else "")
                     + f"<br>{s['pages_scanned']} screens scanned</div></div>")
    parts.append("</div></body></html>")
    out.write_text("".join(parts))
    return {"apps": len(rows), **tot}
