"""Zero-config scan: crawl the running app like a first-time visitor on desktop and mobile and run
every sensor on every screen. Gives a quality report in minutes before a single case is written,
and the discovered routes become the denominator of app coverage.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .config import Profile
from .rules import route_of
from .runner import Browsers, login
from .sensors import VITALS_INIT, Recorder, audit_page, cookie_facts

LINKS_JS = r"""
() => Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href'))
  .filter(h => h && !h.startsWith('mailto:') && !h.startsWith('tel:') && !h.startsWith('javascript:'))
"""


def _normalise(base: str, href: str, origin: str) -> str | None:
    url = urljoin(base, href)
    p = urlparse(url)
    if f"{p.scheme}://{p.netloc}" != origin:
        return None
    frag = p.fragment if p.fragment.startswith("/") else ""
    return f"{origin}{p.path or '/'}" + (f"?{p.query}" if p.query else "") + (f"#{frag}" if frag else "")


def scan(profile: Profile, browsers: Browsers, out_dir: Path, *, routes: list[str] | None = None,
         viewports: list[str] | None = None, max_pages: int | None = None, account: str | None = None,
         log=print) -> list[dict]:
    cfg = profile.scan
    routes = routes or cfg.get("routes") or ["/"]
    viewports = viewports or cfg.get("viewports") or ["desktop", "mobile"]
    max_pages = max_pages or int(cfg.get("max_pages", 20))
    account = account or cfg.get("as")
    exclude = cfg.get("exclude", [])
    origin = "{0.scheme}://{0.netloc}".format(urlparse(profile.base_url))
    scan_dir = out_dir / "scan"
    scan_dir.mkdir(parents=True, exist_ok=True)
    pages: list[dict] = []
    for vp in viewports:
        context, engine = browsers.new_context(vp)
        context.add_init_script(VITALS_INIT)
        if account:
            page = context.new_page()
            try:
                login(profile, context, page, account)
            except Exception as e:
                log(f"  scan: login as {account} failed: {e}")
            page.close()
        queue = [profile.url(r) for r in routes]
        seen_routes: set[str] = set()
        n = 0
        while queue and n < max_pages:
            url = queue.pop(0)
            route = route_of(url)
            if route in seen_routes or any(x in url for x in exclude):
                continue
            seen_routes.add(route)
            n += 1
            rec = Recorder(profile.base_url, profile.ignore, profile.raw.get("first_party"))
            rec.phase = "act"
            page = context.new_page()
            rec._attach_page(page)
            ev = {"kind": "page", "url": url, "route": route, "viewport": vp, "browser": engine}
            t0 = time.time()
            try:
                resp = page.goto(url, wait_until="load", timeout=30000)
                ev["status"] = resp.status if resp else None
                try:
                    page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                page.wait_for_timeout(int(cfg.get("settle_ms", 1200)))
                ev["audit"] = audit_page(page, a11y=profile.sensor_on("a11y"))
                ev["final_url"] = page.url
                ev["cookies"] = cookie_facts(context, profile.base_url)
                shot = scan_dir / f"{vp}-{n:02d}.png"
                page.screenshot(path=str(shot), timeout=10000, animations="disabled")
                ev["screenshot"] = f"scan/{shot.name}"
                for href in sorted(set(page.evaluate(LINKS_JS) or [])):   # stable order → the same screens every run
                    u = _normalise(page.url, href, origin)
                    if u and route_of(u) not in seen_routes and u not in queue:
                        queue.append(u)
            except Exception as e:
                ev["error"] = f"{type(e).__name__}: {e}"[:300]
            ev["sensors"] = rec.to_dict()
            ev["load_s"] = round(time.time() - t0, 2)
            pages.append(ev)
            a = ev.get("audit") or {}
            log(f"  scan {vp:<8} {route:<40} a11y:{len((a.get('a11y') or {}).get('violations', [])):>2} "
                f"errors:{len(rec.page_errors) + sum(c['type'] == 'error' for c in rec.console):>2}")
            page.close()
        context.close()
    (scan_dir / "pages.json").write_text(json.dumps(pages, indent=1, ensure_ascii=False, default=str))
    return pages
