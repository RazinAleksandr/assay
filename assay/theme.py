"""Shared look of every Assay report: a quiet engineering document, not a dashboard toy.
One typeface family, one brand colour, hairline tables, status shown by text + a thin rule."""
from __future__ import annotations

import base64
import html
import shutil
import subprocess
import tempfile
from pathlib import Path

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700'
         '&family=IBM+Plex+Mono:wght@400;500&display=swap">')

CSS = """
:root{--bg:#f4f6f8;--paper:#ffffff;--ink:#14191e;--text:#2b3137;--mute:#69727b;--rule:#d8dee3;--fill:#f1f4f6;
  --brand:#1f3d63;--pass:#2f6f55;--fail:#a9443a;--inc:#9a6c12;--pass-bg:#e7f1ec;--fail-bg:#f6e7e5;--inc-bg:#f5eedd;
  --rA:#2e7a4e;--rB:#6b9a3b;--rC:#c39b22;--rD:#cb7429;--rE:#b2413a;
  --sans:"IBM Plex Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Arial,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e1216;--paper:#151b21;--ink:#e9eef2;--text:#c9d1d8;--mute:#8d98a2;
  --rule:#27313a;--fill:#1b232a;--brand:#8fb0d8;--pass:#5dbb8e;--fail:#e07a70;--inc:#d7aa4a;--pass-bg:#16291f;--fail-bg:#2e1a18;--inc-bg:#2b2414;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0e1216;--paper:#151b21;--ink:#e9eef2;--text:#c9d1d8;--mute:#8d98a2;
  --rule:#27313a;--fill:#1b232a;--brand:#8fb0d8;--pass:#5dbb8e;--fail:#e07a70;--inc:#d7aa4a;--pass-bg:#16291f;--fail-bg:#2e1a18;--inc-bg:#2b2414;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 var(--sans);
  padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:20px 48px}
.doc{max-width:1120px;margin:0 auto;background:var(--paper);border:1px solid var(--rule);padding:clamp(18px,4vw,44px)}
h1,h2,h3{color:var(--ink);margin:0;text-wrap:balance}
h1{font-size:clamp(22px,3vw,28px);font-weight:600;line-height:1.2}
h2{font-size:16px;font-weight:600;margin:34px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--rule)}
h3{font-size:14.5px;font-weight:600}
p{margin:0 0 8px}
a{color:var(--brand)}
.mono{font-family:var(--mono);font-size:12.5px}
.mute{color:var(--mute)}
.bar{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;border-bottom:2px solid var(--ink);padding-bottom:10px;margin-bottom:22px}
.logo{font:700 18px/1 var(--sans);color:var(--ink)}.logo span{color:var(--brand)}
.logo small{font-weight:500;font-size:13px;color:var(--mute);margin-left:10px}
.bar .meta{font-size:12px;color:var(--mute)}
.verdict{border-left:4px solid var(--mute);padding:4px 0 4px 16px;margin-bottom:22px}
.verdict.PASSED{border-color:var(--pass)}.verdict.FAILED{border-color:var(--fail)}.verdict.INCONCLUSIVE{border-color:var(--inc)}
.verdict .label{font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase}
.PASSED .label{color:var(--pass)}.FAILED .label{color:var(--fail)}.INCONCLUSIVE .label{color:var(--inc)}
.verdict p{margin:6px 0 0;font-size:15px}
.figs{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.fig{padding:12px 14px 12px 0;min-width:0}.fig+.fig{padding-left:14px;border-left:1px solid var(--rule)}
.fig b{display:block;font-size:24px;font-weight:600;line-height:1.15;color:var(--ink);font-variant-numeric:tabular-nums}
.fig span{display:block;font-size:12px;color:var(--mute);margin-top:3px}
.cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:28px}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:8px 10px 8px 0;border-bottom:1px solid var(--rule);vertical-align:top}
thead th{font-size:11.5px;font-weight:600;color:var(--mute);border-bottom:1px solid var(--ink);white-space:nowrap}
td.r,th.r{text-align:right}td.c,th.c{text-align:center}
.sq{display:inline-block;width:22px;height:22px;line-height:22px;text-align:center;color:#fff;font-weight:600;font-size:12px;border-radius:2px}
.sq.A{background:var(--rA)}.sq.B{background:var(--rB)}.sq.C{background:var(--rC)}.sq.D{background:var(--rD)}.sq.E{background:var(--rE)}
.st{font-weight:600;font-size:12px;letter-spacing:.03em;white-space:nowrap}
.st.PASS,.ok{color:var(--pass)}.st.FAIL,.nok{color:var(--fail)}.st.INCONCLUSIVE,.und{color:var(--inc)}
.sev{white-space:nowrap;font-size:12.5px}
.sev:before{content:"";display:inline-block;width:3px;height:12px;margin-right:7px;vertical-align:-1px;background:var(--mute)}
.sev.blocker:before,.sev.critical:before{background:var(--fail)}.sev.major:before{background:var(--rD)}.sev.minor:before{background:var(--rC)}
.thumb{width:132px;height:80px;max-width:100%;object-fit:cover;object-position:top;border:1px solid var(--rule);display:block}
.case{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:22px;padding:16px 0;border-bottom:1px solid var(--rule)}
.case:last-child{border-bottom:0}
.case img{width:100%;border:1px solid var(--rule);display:block}
.why{background:var(--fail-bg);padding:8px 12px;margin:10px 0;font-size:13.5px;color:var(--ink)}
.why.INCONCLUSIVE{background:var(--inc-bg)}
ol.steps,ul.checks{margin:6px 0 0;padding-left:20px;font-size:13px}
ol.steps li,ul.checks li{margin:2px 0}
.tabs{display:flex;gap:16px;flex-wrap:wrap;margin:-4px 0 6px;font-size:13px}
.tabs button{all:unset;cursor:pointer;color:var(--mute);padding:4px 0;border-bottom:2px solid transparent}
.tabs button.on{color:var(--ink);border-bottom-color:var(--brand)}
.tabs button:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:14px}
.gallery figure{margin:0;min-width:0}.gallery img{width:100%;height:110px;object-fit:cover;object-position:top;border:1px solid var(--rule);display:block}
.gallery figcaption{font-size:11.5px;color:var(--mute);margin-top:4px;word-break:break-word}
details summary{cursor:pointer;color:var(--brand);font-size:13px}
.foot{margin-top:34px;padding-top:12px;border-top:1px solid var(--rule);font-size:11.5px;color:var(--mute);display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
.note{font-size:12.5px;color:var(--mute)}
.chip{font-size:11.5px;font-weight:600;padding:1px 6px;border:1px solid currentColor;border-radius:2px;white-space:nowrap}
@media (max-width:760px){.figs{grid-template-columns:repeat(2,minmax(0,1fr))}.fig:nth-child(3){padding-left:0;border-left:0}
  .fig:nth-child(n+3){border-top:1px solid var(--rule)}.cols,.case{grid-template-columns:minmax(0,1fr)}.hide-sm{display:none}}
"""


def e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1,viewport-fit=cover'>"
            f"<title>{e(title)}</title>{FONTS}<style>{CSS}</style></head><body><main class=doc>{body}</main></body></html>")


def embed_images(html_text: str, base: Path, width: int = 900) -> str:
    """Inline every relative <img src> as a data URI (JPEG-compressed with macOS sips when available)."""
    import re

    cache: dict[str, str] = {}

    def conv(m: re.Match) -> str:
        src = m.group(1)
        if src.startswith(("data:", "http")):
            return m.group(0)
        if src not in cache:
            f = (base / src)
            if not f.exists():
                cache[src] = src
            else:
                data, mime = f.read_bytes(), "image/png"
                if shutil.which("sips"):
                    with tempfile.TemporaryDirectory() as d:
                        out = Path(d) / "x.jpg"
                        r = subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "72", "-Z", str(width),
                                            str(f), "--out", str(out)], capture_output=True)
                        if r.returncode == 0 and out.exists():
                            data, mime = out.read_bytes(), "image/jpeg"
                cache[src] = f"data:{mime};base64," + base64.b64encode(data).decode()
        return f'src="{cache[src]}"'

    html_text = re.sub(r'src="([^"]+)"', conv, html_text)
    # links to the same screenshots would point nowhere once detached from the run folder
    return re.sub(r'<a href="(cases|scan)/[^"]+">(.*?)</a>', r"\2", html_text, flags=re.S)
