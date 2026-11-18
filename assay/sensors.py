"""Always-on sensors: they watch every case and every scanned page without anybody
writing an assertion. Raw facts go to evidence; rules.py turns them into issues.

  console / pageerror / network   — reliability
  web vitals (LCP, CLS, INP, long tasks) — performance
  axe-core (WCAG 2.x A/AA)        — accessibility
  layout audit (overflow, tap targets, broken images, blank screen) — UX & mobile
  cookies / storage tokens        — security (lite)
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

AXE_JS = (Path(__file__).parent / "assets" / "axe.min.js").read_text()
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]

VITALS_INIT = r"""
(() => {
  if (window.__assay) return;
  const A = window.__assay = {lcp: null, cls: 0, longTasks: [], inp: null};
  const obs = (type, fn, extra) => { try { new PerformanceObserver(l => l.getEntries().forEach(fn)).observe(Object.assign({type, buffered: true}, extra || {})); } catch (e) {} };
  obs('largest-contentful-paint', e => { A.lcp = Math.max(A.lcp || 0, e.renderTime || e.loadTime || e.startTime); });
  obs('layout-shift', e => { if (!e.hadRecentInput) A.cls += e.value; });
  obs('longtask', e => { if (A.longTasks.length < 200) A.longTasks.push(Math.round(e.duration)); });
  obs('event', e => { if (e.interactionId) A.inp = Math.max(A.inp || 0, e.duration); }, {durationThreshold: 40});
})();
"""

AUDIT_JS = r"""
(opts) => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const describe = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    else if (el.getAttribute('data-testid')) s += `[data-testid="${el.getAttribute('data-testid')}"]`;
    else if (typeof el.className === 'string' && el.className.trim()) s += '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.');
    const label = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('title') || '').trim().replace(/\s+/g, ' ').slice(0, 40);
    return label ? `${s} "${label}"` : s;
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity || '1') > 0.05;
  };
  const doc = document.documentElement;
  const overflowX = doc.scrollWidth > vw + 2 && getComputedStyle(document.body).overflowX !== 'hidden' && getComputedStyle(doc).overflowX !== 'hidden';
  const overflowEls = [];
  if (overflowX) {
    for (const el of document.body.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (r.right > vw + 2 && r.width > 0 && r.width < doc.scrollWidth + 1 && visible(el)) {
        overflowEls.push(describe(el) + ` (right=${Math.round(r.right)}px > ${vw}px)`);
        if (overflowEls.length >= 5) break;
      }
    }
  }
  const smallTargets = [];
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],[role=tab],[role=checkbox],[role=switch],[role=menuitem]';
  for (const el of document.querySelectorAll(sel)) {
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    // WCAG 2.2 SC 2.5.8 exception: inline links inside a sentence
    if (el.tagName === 'A' && getComputedStyle(el).display === 'inline' && (el.parentElement?.innerText || '').length > (el.innerText || '').length + 20) continue;
    if (Math.min(r.width, r.height) < 24) {
      smallTargets.push(describe(el) + ` (${Math.round(r.width)}×${Math.round(r.height)})`);
      if (smallTargets.length >= 20) break;
    }
  }
  const brokenImages = [];
  for (const img of document.images) {
    if (img.complete && img.naturalWidth === 0 && (img.currentSrc || img.src)) brokenImages.push((img.currentSrc || img.src).slice(0, 160));
  }
  const text = (document.body?.innerText || '').trim();
  let media = 0;
  for (const el of document.querySelectorAll('canvas,img,svg,video')) {
    const r = el.getBoundingClientRect(); if (r.width * r.height > 10000 && visible(el)) media++;
  }
  const nav = performance.getEntriesByType('navigation')[0];
  return {
    url: location.href, title: document.title, viewport: [vw, vh], textLength: text.length,
    coarsePointer: matchMedia('(pointer: coarse)').matches,
    blank: text.length < 10 && media === 0,
    overflowX, overflowEls, smallTargets, brokenImages,
    vitals: window.__assay ? JSON.parse(JSON.stringify(window.__assay)) : null,
    timing: nav ? {ttfb: Math.round(nav.responseStart), dcl: Math.round(nav.domContentLoadedEventEnd), load: Math.round(nav.loadEventEnd), transferKB: Math.round((performance.getEntriesByType('resource').reduce((s, e) => s + (e.transferSize || 0), 0) + (nav.transferSize || 0)) / 1024)} : null,
    storageTokens: Object.keys(localStorage).filter(k => /^eyJ[\w-]+\.[\w-]+\.[\w-]+$/.test(String(localStorage.getItem(k) || '').trim())),
  };
}
"""


# messages the browser itself prints (not the app): never an app defect
BROWSER_NOISE = [
    r"^Button failed to load, iconName = ",          # WebKit media-control placards on macOS
    r"Download the React DevTools",
    r"\[vite\] (connecting|connected)",
]


def origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


class Recorder:
    """Collects console, page errors and network facts for one browser context."""

    API_CAP = 300                       # apps that poll would otherwise grow the api list without end

    def __init__(self, base_url: str, ignore: dict | None = None, extra_origins: list[str] | None = None):
        self.first_party = {origin_of(base_url)} | {origin_of(u) for u in (extra_origins or [])}
        ignore = ignore or {}
        self.ignore_console = [re.compile(p) for p in ignore.get("console", []) + BROWSER_NOISE]
        self.ignore_network = [re.compile(p) for p in ignore.get("network", [])]
        self.phase = "setup"
        self.step = None
        self.console: list[dict] = []
        self.page_errors: list[dict] = []
        self.failed_requests: list[dict] = []
        self.bad_responses: list[dict] = []
        self.slow_responses: list[dict] = []
        self.requests = 0
        self.offline = False
        self.urls: list[str] = []
        self.api: list[dict] = []           # the journey's own fetch/xhr calls: what links a failure to backend code
        self._api_open: dict = {}           # request → its api entry, until a response gives it a status
        self._t0 = time.time()

    # -- wiring -------------------------------------------------------------
    def attach(self, context) -> None:
        context.add_init_script(VITALS_INIT)
        context.on("page", self._attach_page)
        for p in context.pages:
            self._attach_page(p)

    def _attach_page(self, page) -> None:
        page.on("request", self._on_request)
        page.on("console", self._on_console)
        page.on("pageerror", self._on_pageerror)
        page.on("requestfailed", self._on_requestfailed)
        page.on("requestfinished", self._on_requestfinished)
        page.on("response", self._on_response)
        page.on("framenavigated", lambda fr: self._on_nav(page, fr))

    def _on_nav(self, page, frame) -> None:
        if frame == page.main_frame and frame.url not in self.urls[-1:] and self._first_party(frame.url):
            self.urls.append(frame.url)

    def _tag(self, d: dict) -> dict:
        d.update(phase=self.phase, step=self.step, t=round(time.time() - self._t0, 2))
        if self.offline:
            d["offline"] = True
        return d

    def _first_party(self, url: str) -> bool:
        return origin_of(url) in self.first_party

    def _ignored_net(self, url: str) -> bool:
        return any(p.search(url) for p in self.ignore_network)

    def _on_console(self, msg) -> None:
        if msg.type not in ("error", "warning"):
            return
        text = msg.text or ""
        if any(p.search(text) for p in self.ignore_console):
            return
        # resource 4xx/5xx console noise is already captured as network facts
        if re.match(r"^Failed to load resource: the server responded with a status of", text):
            return
        loc = msg.location or {}
        self.console.append(self._tag({"type": msg.type, "text": text[:600], "url": (loc.get("url") or "")[:200]}))

    def _on_pageerror(self, err) -> None:
        text = f"{getattr(err, 'name', 'Error')}: {getattr(err, 'message', str(err))}"
        if any(p.search(text) for p in self.ignore_console):
            return
        stack = (getattr(err, "stack", "") or "")[:800]
        self.page_errors.append(self._tag({"text": text[:600], "stack": stack}))

    def _on_request(self, req) -> None:
        # tagged when the request starts: its response may only arrive while the next step runs
        if req.resource_type not in ("fetch", "xhr") or len(self.api) >= self.API_CAP:
            return
        url = req.url
        if not self._first_party(url) or self._ignored_net(url):
            return
        d = {"method": req.method, "path": urlparse(url).path, "status": None, "step": self.step, "phase": self.phase}
        self.api.append(d)
        self._api_open[req] = d

    def _on_requestfailed(self, req) -> None:
        self._api_open.pop(req, None)           # no response: its api entry keeps status None
        url = req.url
        if self._ignored_net(url) or url.startswith("data:"):
            return
        failure = req.failure or ""
        if "ERR_ABORTED" in failure or "cancelled" in failure.lower():
            return  # navigations/aborts are normal in SPAs
        self.failed_requests.append(self._tag({"url": url[:300], "method": req.method, "failure": failure,
                                               "first_party": self._first_party(url), "type": req.resource_type}))

    def _on_requestfinished(self, req) -> None:
        self.requests += 1
        try:
            t = req.timing
            dur = (t.get("responseEnd") or 0) - (t.get("requestStart") or 0)
        except Exception:
            return
        if dur > 1500 and self._first_party(req.url) and req.resource_type in ("fetch", "xhr", "document"):
            self.slow_responses.append(self._tag({"url": req.url[:300], "method": req.method, "ms": round(dur)}))

    def _on_response(self, resp) -> None:
        call = self._api_open.pop(resp.request, None)
        if call is not None:
            call["status"] = resp.status
        if resp.status < 400:
            return
        url = resp.url
        if self._ignored_net(url):
            return
        req = resp.request
        self.bad_responses.append(self._tag({"url": url[:300], "method": req.method, "status": resp.status,
                                             "first_party": self._first_party(url), "type": req.resource_type}))

    def to_dict(self) -> dict[str, Any]:
        return {"console": self.console, "page_errors": self.page_errors,
                "failed_requests": self.failed_requests, "bad_responses": self.bad_responses,
                "slow_responses": self.slow_responses[:30], "requests": self.requests, "urls": self.urls[:200]}


def audit_page(page, *, a11y: bool = True) -> dict:
    """Snapshot UX/perf/a11y facts of the current page. Never raises."""
    out: dict[str, Any] = {}
    try:
        out = page.evaluate(AUDIT_JS, {})
    except Exception as e:  # page closed / navigating
        out = {"error": f"audit failed: {e}"[:300]}
    if a11y:
        out["a11y"] = run_axe(page)
    return out


def run_axe(page) -> dict:
    """axe-core WCAG audit: every violating element (not a sample), and the results axe could not decide on its own
    (`incomplete`: contrast over images, ARIA it cannot resolve), which need a person to look."""
    try:
        if not page.evaluate("() => typeof window.axe !== 'undefined'"):
            page.evaluate(AXE_JS)
        res = page.evaluate(
            """async (tags) => {
                const r = await axe.run(document, {runOnly: {type: 'tag', values: tags}, resultTypes: ['violations', 'incomplete']});
                const pack = (v) => ({id: v.id, impact: v.impact, help: v.help, helpUrl: v.helpUrl,
                  tags: v.tags.filter(t => t.startsWith('wcag')), nodes: v.nodes.length,
                  targets: v.nodes.slice(0, 200).map(n => (n.target || []).join(' ')),
                  html: v.nodes.slice(0, 200).map(n => (n.html || '').slice(0, 160)),
                  summary: (v.nodes[0] && v.nodes[0].failureSummary || '').slice(0, 300)});
                return {violations: r.violations.map(pack), incomplete: r.incomplete.map(pack)};
            }""",
            AXE_TAGS,
        )
        return res
    except Exception as e:
        return {"error": f"axe failed: {e}"[:300], "violations": [], "incomplete": []}


def cookie_facts(context, base_url: str) -> list[dict]:
    out = []
    try:
        host = urlparse(base_url).hostname or ""
        for c in context.cookies():
            if host and host not in c.get("domain", ""):
                continue
            out.append({"name": c["name"], "httpOnly": c.get("httpOnly"), "secure": c.get("secure"),
                        "sameSite": c.get("sameSite")})
    except Exception:
        pass
    return out
