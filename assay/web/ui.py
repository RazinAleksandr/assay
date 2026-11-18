"""Building blocks of the Assay server UI: page shell, navigation, icons, verdict badges, charts."""
from __future__ import annotations

import hashlib
import html
from contextvars import ContextVar
from datetime import datetime

from ..model import DIMENSIONS

# The person's colour scheme from their cookie (light, dark; empty = follow the system), set by the server per request:
# the page is rendered in it, so there is no flash and no inline script.
THEME: ContextVar[str] = ContextVar("theme", default="")
USER: ContextVar[dict | None] = ContextVar("user", default=None)    # the signed-in person: name, email
CSRF: ContextVar[str] = ContextVar("csrf", default="")               # the token every form posts back

DIM_SHORT = {"functional": "Functional", "reliability": "Reliability", "performance": "Performance", "accessibility": "Accessibility",
             "ux": "UX & Mobile", "security": "Security", "ai": "AI features"}

_P = {
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "alert": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    "branch": '<circle cx="6" cy="5" r="2.2"/><circle cx="6" cy="19" r="2.2"/><circle cx="18" cy="7" r="2.2"/><path d="M6 7.2v9.6M18 9.2c0 5-12 2.5-12 7.6"/>',
    "pr": '<circle cx="6" cy="5" r="2.2"/><circle cx="6" cy="19" r="2.2"/><circle cx="18" cy="19" r="2.2"/><path d="M6 7.2v9.6M18 16.8V10a3 3 0 0 0-3-3h-4"/><path d="m13 4.5-2.5 2.5L13 9.5"/>',
    "overview": '<rect x="3" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5"/>',
    "issues": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5h.01"/>',
    "journey": '<circle cx="6" cy="19" r="2.2"/><circle cx="18" cy="5" r="2.2"/><path d="M8.2 19H17a3.5 3.5 0 0 0 0-7H7a3.5 3.5 0 0 1 0-7h8.8"/>',
    "screens": '<rect x="2.5" y="4" width="19" height="12.5" rx="2"/><path d="M8 20.5h8M12 16.5v4"/>',
    "activity": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    "gate": '<path d="M12 3 4.5 6v5.5c0 4.7 3.2 8 7.5 9.5 4.3-1.5 7.5-4.8 7.5-9.5V6z"/><path d="m9 12 2.2 2.2L15.5 10"/>',
    "setup": '<path d="m5 16 5-4-5-4M12 19h7"/>',
    "external": '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "down": '<path d="m6 9 6 6 6-6"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.6 9.2a2.5 2.5 0 1 1 3.4 2.3c-.6.3-1 .8-1 1.5v.3M12 16.8h.01"/>',
    "rules": '<path d="M5 4.5A1.5 1.5 0 0 1 6.5 3H19v15H6.5A1.5 1.5 0 0 0 5 19.5z"/><path d="M5 19.5A1.5 1.5 0 0 0 6.5 21H19v-3M9 7h6M9 10.5h6"/>',
    "commit": '<circle cx="12" cy="12" r="3.5"/><path d="M3 12h5.5M15.5 12H21"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 2"/>',
    "phone": '<rect x="7" y="2.5" width="10" height="19" rx="2"/><path d="M11 18h2"/>',
    "flask": '<path d="M9 3h6M10 3v6.2L4.6 18.6A1.6 1.6 0 0 0 6 21h12a1.6 1.6 0 0 0 1.4-2.4L14 9.2V3"/><path d="M7.2 15h9.6"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "blocker": '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
    "critical": '<path d="m6 12.5 6-6 6 6M6 18.5l6-6 6 6"/>',
    "major": '<path d="m6 15 6-6 6 6"/>',
    "minor": '<path d="m6 9 6 6 6-6"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5h.01"/>',
    "data": '<ellipse cx="12" cy="5.5" rx="7.5" ry="2.8"/><path d="M4.5 5.5v13c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-13M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8"/>',
    "terminal": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M12.5 15H17"/>',
    "file": '<path d="M14 3H6.5A1.5 1.5 0 0 0 5 4.5v15A1.5 1.5 0 0 0 6.5 21h11a1.5 1.5 0 0 0 1.5-1.5V8z"/><path d="M14 3v5h5"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/>',
    "lock": '<rect x="5" y="11" width="14" height="9.5" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    "copy": '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h9"/>',
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
    "moon": '<path d="M20.5 14.2A8.5 8.5 0 0 1 9.8 3.5a8.5 8.5 0 1 0 10.7 10.7z"/>',
    "badge": '<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M12 7v10"/>',
}


def purl(key: str) -> str:
    """Address of a project: GitHub projects live at /gh/<owner>/<repo>, legacy keys at /projects/<key>."""
    return "/" + key if "/" in key else "/projects/" + key


def repo_name(key: str, title: str | None = None) -> str:
    """How a project is named in lists: owner/repo for GitHub projects, its title otherwise."""
    return key[3:] if key.startswith("gh/") else (title or key)


def e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def icon(name: str, extra: str = "") -> str:
    return f'<svg class="i {extra}" viewBox="0 0 24 24" aria-hidden="true">{_P[name]}</svg>'


# A gate status in a person's words: a pull request is shipped or not, a branch is healthy or not.
STATUS_TEXT = {"pr": {"PASSED": "Ship it", "FAILED": "Don't ship", "INCONCLUSIVE": "Not sure"},
               "branch": {"PASSED": "Healthy", "FAILED": "Broken", "INCONCLUSIVE": "Not sure"},
               "gate": {"PASSED": "Passed", "FAILED": "Failed", "INCONCLUSIVE": "Not sure"}}
STATUS_ICON = {"PASSED": "check", "FAILED": "x", "INCONCLUSIVE": "alert"}
STATE_GATE = {"ship": "PASSED", "no_ship": "FAILED", "unsure": "INCONCLUSIVE"}


def gate(status: str | None, label: bool = True, kind: str = "pr", quiet: bool = False) -> str:
    """The state stub; `quiet` where several sit together (filters): the colour as text, not a fill."""
    st = status if status in STATUS_ICON else "none"
    text = STATUS_TEXT[kind].get(st, "No analysis")
    return (f'<span class="gate {st}{" quiet" if quiet else ""}"{"" if label else f" title={chr(34)}{e(text)}{chr(34)}"}>'
            f'<span class=sq>{icon(STATUS_ICON.get(st, "clock"))}</span>{e(text) if label else f"<span class=sr>{e(text)}</span>"}</span>')


def verdict(v: str) -> str:
    """A journey's result: it works, it is broken, it is flaky (failed, then passed on a second run), or it could not be
    checked."""
    words = {"PASS": "Works", "FAIL": "Broken", "FLAKY": "Flaky", "INCONCLUSIVE": "Not checked"}
    return f'<span class="vd {e(v)}">{words.get(v, e(v) or "Not run")}</span>'


def donut(frac: float | None, size: int = 34) -> str:
    if frac is None:
        return ""
    r, c = size / 2 - 3.5, size / 2
    circ = 2 * 3.14159 * r
    col = "var(--pass)" if frac >= .9 else ("var(--cell-warn)" if frac >= .7 else "var(--fail)")
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" aria-hidden="true">'
            f'<circle cx="{c}" cy="{c}" r="{r}" fill="none" stroke="var(--border)" stroke-width="4"/>'
            f'<circle cx="{c}" cy="{c}" r="{r}" fill="none" style="stroke:{col}" stroke-width="4" stroke-linecap="round" '
            f'stroke-dasharray="{circ * max(0.0, min(1.0, frac)):.1f} {circ:.1f}" transform="rotate(-90 {c} {c})"/></svg>')


PALETTE = ["#2457c5", "#0f7b6c", "#8a3ffc", "#c2410c", "#be185d", "#4d7c0f", "#0e7490", "#7c3aed"]


def avatar(key: str, title: str, lg: bool = False) -> str:
    """A project's monogram: neutral, so the only colour on a page is state."""
    letter = (title or key.split("/")[-1] or "?").strip()[:1].upper()
    return f'<span class="avatar{" lg" if lg else ""}" aria-hidden="true">{e(letter)}</span>'


def local(iso: str | None) -> datetime | None:
    """A stored timestamp as this machine's local time (stamps with an offset are converted, naive ones are local)."""
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def when(iso: str | None) -> str:
    d = local(iso)
    return d.strftime("%-d %b %Y, %H:%M") if d else (iso or "—")


def ago(iso: str | None) -> str:
    d = local(iso)
    if d is None:
        return "—"
    s = (datetime.now() - d).total_seconds()
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        h = int(s // 3600)
        return f"{h} hour{'s' if h > 1 else ''} ago"
    d = int(s // 86400)
    return f"{d} day{'s' if d > 1 else ''} ago"


def topbar(active: str, q: str = "") -> str:
    nav = "".join(f'<a href="{href}"{" aria-current=page" if active == k else ""}>{label}</a>'
                  for k, href, label in (("projects", "/projects", "Projects"), ("docs", "/docs", "How it works")))
    theme = (f'<button type=button class="iconbtn themebtn" data-theme-toggle hidden aria-label="Switch colour theme">'
             f'{icon("moon", "to-dark")}{icon("sun", "to-light")}</button>')
    u = USER.get()
    if u:
        initial = (u.get("name") or u.get("email") or "?").strip()[:1].upper()
        who = (f'<button type=button class=acct-btn popovertarget=acct-menu aria-expanded=false aria-label="Account menu, {e(u.get("name") or u.get("email") or "")}">'
               f'{e(initial)}</button>'
               f'<div id=acct-menu popover class="menu picker acct-menu"><p class=who><b>{e(u.get("name") or "")}</b>'
               f'<span>{e(u.get("email") or "")}</span></p><ul><li><a href="/projects">Your projects</a></li>'
               + ('<li><a href="/account">Account</a></li>' if u.get("id") else "")
               + f'</ul><form method=post action="/logout"><input type=hidden name=csrf value="{e(CSRF.get())}">'
               f'<button type=submit class=menu-item>Sign out</button></form></div>')
        right = f'<a class="btn primary sm" href="/setup" aria-label="Connect a repository">{icon("plus")}<span class=lbl>Connect a repository</span></a>{who}'
    else:
        right = '<a class="btn sm" href="/login">Sign in</a><a class="btn primary sm" href="/signup">Sign up</a>'
    return (f'<header class=topbar><a class=brand href="/"><span class=mark><svg viewBox="0 0 24 24" aria-hidden="true">{_P["flask"]}</svg></span>assay</a>'
            f'<nav class=gnav aria-label="Primary">{nav}</nav>'
            f'<div class=tb-right><search class=search><form action="/projects"><label for=q-top class=sr>Search projects</label>{icon("search")}'
            f'<input id=q-top name=q type=search value="{e(q)}" placeholder="Search projects" autocomplete=off spellcheck=false enterkeyhint=search></form></search>'
            f'{theme}<a class=iconbtn href="/docs" aria-label="How Assay works">{icon("help")}</a>{right}</div></header>')


REPO = "https://github.com/RazinAleksandr/assay"

LIGHTBOX = ('<dialog id=lightbox closedby=any aria-labelledby=lightbox-title><div class=lb-head><h2 id=lightbox-title data-caption>Screenshot</h2>'
            '<form method=dialog><button class="btn sm" autofocus>Close</button></form></div>'
            '<div class=lb-body></div></dialog>')


def shell(title: str, body: str, *, active: str = "", snav: str | None = None, q: str = "") -> str:
    theme = THEME.get()
    scheme = theme if theme in ("light", "dark") else "light dark"
    return (f"<!doctype html><html lang=en{f' data-theme={theme}' if theme in ('light', 'dark') else ''}><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width, initial-scale=1'><meta name=color-scheme content='{scheme}'>"
            f"<meta name=text-scale content=scale>"
            f"<meta name=theme-color media='(prefers-color-scheme: light)' content='#ecede8'>"
            f"<meta name=theme-color media='(prefers-color-scheme: dark)' content='#1f211c'>"
            f"<title>{e(title)} · Assay</title><link rel=preload href='/static/archivo.woff2' as=font type=font/woff2 crossorigin>"
            f"<link rel=stylesheet href='/static/app.css'><link rel=stylesheet href='/static/agent.css'>"
            f"<link rel=expect href='#main' blocking=render><script src='/static/app.js' defer></script>"
            f"<link rel=icon href='data:image/svg+xml,{_favicon()}'></head><body>"
            f"<a class=skip href='#main'>Skip to content</a>{topbar(active, q)}"
            f"<div class='shell{'' if snav else ' nosnav'}'>{snav or ''}<div class=content><main id=main tabindex=-1>{body}</main>"
            f"<footer class=foot><span>Assay</span><a href='/docs'>How it works</a><a href='/setup'>Connect a repository</a>"
            f"<a href='{REPO}'>Plugin and action</a></footer></div></div>{LIGHTBOX}</body></html>")


def zoom(src: str, alt: str, caption: str, cls: str = "", size: tuple[int, int] | None = None) -> str:
    """A screenshot that opens full size in the page's dialog: a button (keyboard, screen reader) around a lazy image."""
    wh = f" width={size[0]} height={size[1]}" if size else ""
    return (f'<button type=button class="shot {e(cls)}" command=show-modal commandfor=lightbox data-full="{e(src)}" '
            f'data-caption="{e(caption)}" aria-label="Enlarge: {e(caption)}"><img src="{e(src)}" alt="{e(alt)}"{wh} '
            f'loading=lazy decoding=async></button>')


_COPY_N = iter(range(1, 1 << 62))


def copyable(text: str, label: str = "command", *, html_text: str | None = None, cls: str = "code") -> str:
    """A code block with a copy button; the result is said in a status bubble next to it (screen readers hear it)."""
    cid = f"cp{next(_COPY_N)}"
    return (f'<div class=copyable><pre class={cls} tabindex=0 role=region aria-label="{e(label.capitalize())}"><code id={cid}>{html_text if html_text is not None else e(text)}</code></pre>'
            f'<span class=copy><span class=tip role=status></span><button type=button class=copy-btn command=--copy commandfor={cid} '
            f'aria-label="Copy {e(label)}">{icon("copy", "ic")}{icon("check", "ok")}<span>Copy</span></button></span></div>')


def copy_line(text: str, label: str = "command") -> str:
    """One line of a command in a field, with its copy button."""
    cid = f"cp{next(_COPY_N)}"
    return (f'<div class=cmd><pre tabindex=0 role=region aria-label="{e(label.capitalize())}"><code id={cid}>{e(text)}</code></pre><span class=copy><span class=tip role=status></span>'
            f'<button type=button class=copy-btn command=--copy commandfor={cid} aria-label="Copy {e(label)}">'
            f'{icon("copy", "ic")}{icon("check", "ok")}<span>Copy</span></button></span></div>')


def _favicon() -> str:
    import urllib.parse
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='#0D5C6E'/>"
           f"<g transform='translate(4 4)' stroke='white' fill='none' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>{_P['flask']}</g></svg>")
    return urllib.parse.quote(svg)


def page_head(crumbs: list[tuple[str, str | None]], title: str, *, extra: str = "", right: str = "", meta: list[str] | None = None) -> str:
    items = "".join(f'<li><a href="{e(h)}">{e(t)}</a></li>' if h else f'<li><span aria-current=page class=ink>{e(t)}</span></li>'
                    for t, h in crumbs)
    m = ('<div class=pmeta>' + "".join(f'<span>{x}</span>' for x in meta) + '</div>') if meta else ""
    return (f'<div class=phead><nav class=crumbs aria-label="Breadcrumbs"><ol>{items}</ol></nav><div class=ptitle><h1 id=page-title>{e(title)}</h1>{extra}'
            f'<span class=grow></span>{right}</div>{m}</div>')


def line_chart(series: list[tuple[str, list[float | None], str]], labels: list[str], *, width: int = 820, height: int = 260,
               ymax: float | None = None, pct: bool = False, links: list[str] | None = None, dot_colors: list[str] | None = None) -> str:
    """An SVG line chart coloured by the theme: series colours are CSS values (`var(--chart-1)`), grid and text come
    from the stylesheet. Each point has a title; linked points are focusable."""
    n = len(labels)
    if n == 0:
        return '<div class=empty>No analyses yet.</div>'
    vals = [v for _, s, _ in series for v in s if v is not None]
    top = ymax if ymax is not None else max(1, max(vals or [1]))
    if not pct:
        step = 10 ** max(0, len(str(int(top))) - 1)
        top = ((int(top) // step) + 1) * step
    x0, x1, y0, y1 = 48, width - 16, 16, height - 34
    X = lambda i: x0 + (x1 - x0) * (i / (n - 1) if n > 1 else .5)
    Y = lambda v: y1 - (y1 - y0) * (v / top if top else 0)
    desc = "; ".join(f'{name}: {", ".join(f"{v:.0%}" if pct else str(v) for v in s if v is not None)}' for name, s, _ in series)
    out = [f'<svg class=chart viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{e(series[0][0])} over time: {e(desc[:400])}">']
    for k in range(5):
        v = top * k / 4
        out.append(f'<line class=gl x1="{x0}" x2="{x1}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>'
                   f'<text x="{x0 - 8}" y="{Y(v) + 4:.1f}" text-anchor="end">{f"{v:.0%}" if pct else int(v)}</text>')
    stride = max(1, n // 6)
    for i, lb in enumerate(labels):
        if i % stride == 0 or i == n - 1:
            anchor = "start" if i == 0 and n > 1 else ("end" if i == n - 1 and n > 1 else "middle")
            out.append(f'<text x="{X(i):.1f}" y="{height - 12}" text-anchor="{anchor}">{e(lb)}</text>')
    for name, s, col in series:
        pts = [(X(i), Y(v)) for i, v in enumerate(s) if v is not None]
        if len(pts) > 1:
            area = f"M{pts[0][0]:.1f},{y1} " + " ".join(f"L{x:.1f},{y:.1f}" for x, y in pts) + f" L{pts[-1][0]:.1f},{y1} Z"
            out.append(f'<path class=area d="{area}" style="fill:{e(col)}"/>')
            out.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in pts)}" fill="none" style="stroke:{e(col)}" stroke-width="2"/>')
        for i, v in enumerate(s):
            if v is None:
                continue
            dc = (dot_colors[i] if dot_colors else col)
            dot = (f'<circle class=dot cx="{X(i):.1f}" cy="{Y(v):.1f}" r="4.5" style="stroke:{e(dc)}">'
                   f'<title>{e(name)}: {f"{v:.0%}" if pct else v} · {e(labels[i])}</title></circle>')
            out.append(f'<a href="{e(links[i])}" aria-label="{e(name)} {f"{v:.0%}" if pct else v}, {e(labels[i])}">{dot}</a>' if links else dot)
    out.append("</svg>")
    return "".join(out)


def dims() -> dict:
    return DIMENSIONS
