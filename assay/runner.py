"""Runner: drives the real app and records evidence. It never decides PASS/FAIL.

Per case:  context → login → setup → snapshot(before) → read-back preconditions
           → act (steps or goal) → settle → snapshot(after) → observe live checks
           → audit (layout, vitals, axe) → screenshot → evidence.json
Anything that breaks before the act is the environment's fault → INCONCLUSIVE later.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import traceback
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from . import checks as C
from .config import Profile, _origin, browser_headers, env_refs, expand, request_headers
from .coverage import Coverage, sources_for
from .driver import StepError, WebDriver, describe_target, step_name
from .model import Case, now_iso
from .probes import snapshot
from .sensors import Recorder, audit_page, cookie_facts

ENV_CAUSES = ("app_unreachable", "login_failed", "setup_failed", "probe_failed", "precondition_mismatch",
              "browser_crashed", "agent_unavailable")


# ---- app process ---------------------------------------------------------------

def http_ok(url: str, timeout: float = 3.0, headers: dict | None = None) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
            return r.status < 500
    except Exception:
        return False


def first_party_headers(context, profile: Profile) -> None:
    """`browser.headers` on the app's own requests only: a secret for a protected preview must not reach the analytics or
    fonts a page loads, and a header the page sets itself (its own Authorization) is never replaced."""
    extra, origin = browser_headers(profile), _origin(profile.base_url)

    def add(route, request):
        if _origin(request.url) != origin:
            return route.continue_()
        have = request.all_headers()
        route.continue_(headers={**have, **{k: v for k, v in extra.items() if k.lower() not in have}})
    context.route("**/*", add)


class AppProcess:
    """Start/stop the app under test from the profile (`app.start`, `app.ready`, `app.reset`)."""

    def __init__(self, profile: Profile, log_path: Path):
        self.p = profile
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None

    def _cmd(self, spec: dict | str) -> tuple[str, Path, dict]:
        if isinstance(spec, str):
            spec = {"cmd": spec}
        spec = expand(spec, self.p)
        cwd = Path(spec.get("cwd", "."))
        cwd = cwd if cwd.is_absolute() else (self.p.root / cwd).resolve()
        env = {**os.environ, **self.p.env, **{k: str(v) for k, v in (spec.get("env") or {}).items()}}
        return spec["cmd"], cwd, env

    @property
    def ready_url(self) -> str:
        r = self.p.app.get("ready") or {}
        return expand(r.get("url", self.p.base_url), self.p)

    def is_up(self) -> bool:
        return http_ok(self.ready_url, headers=browser_headers(self.p))

    def reset(self) -> None:
        if not self.p.app.get("reset"):
            return
        cmd, cwd, env = self._cmd(self.p.app["reset"])
        out = subprocess.run(cmd, shell=True, cwd=cwd, env=env, capture_output=True, text=True, timeout=600,
                             stdin=subprocess.DEVNULL)       # under `assay mcp` stdin is the protocol
        with open(self.log_path, "a") as f:
            f.write(f"$ {cmd}\n{out.stdout}\n{out.stderr}\n")
        if out.returncode != 0:
            raise RuntimeError(f"reset failed: {out.stderr[-400:]}")

    def start(self) -> None:
        if not self.p.app.get("start"):
            return
        cmd, cwd, env = self._cmd(self.p.app["start"])
        log = open(self.log_path, "a")
        log.write(f"$ {cmd}  (cwd={cwd})\n")
        log.flush()
        self.proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL,       # dev servers (nodemon) read stdin: not the protocol's
                                     start_new_session=True)
        timeout = float((self.p.app.get("ready") or {}).get("timeout_s", 90))
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"app exited with code {self.proc.returncode}; see {self.log_path}")
            if self.is_up():
                time.sleep(float((self.p.app.get("ready") or {}).get("grace_s", 0.5)))
                return
            time.sleep(0.5)
        raise RuntimeError(f"app not ready at {self.ready_url} after {timeout:.0f}s; see {self.log_path}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    pass


# ---- browser session -------------------------------------------------------------

class Browsers:
    """Lazily launched browsers keyed by engine; a case picks one by viewport/device."""

    def __init__(self, profile: Profile, headed: bool = False):
        self.p = profile
        self.headed = headed
        self._pw = None
        self._browsers: dict[str, Any] = {}

    def __enter__(self):
        self._pw = sync_playwright().start()
        return self

    def __exit__(self, *a):
        for b in self._browsers.values():
            try:
                b.close()
            except Exception:
                pass
        if self._pw:
            self._pw.stop()

    def context_options(self, viewport: str | None) -> tuple[str, dict]:
        name = viewport or self.p.browser.get("viewport", "desktop")
        vp = self.p.viewports.get(name)
        if vp is None:
            raise StepError(f"unknown viewport {name!r}", "case_invalid")
        engine = vp.get("browser") or self.p.browser.get("name", "chromium")
        opts: dict[str, Any] = {"base_url": self.p.base_url, "ignore_https_errors": True,
                                "locale": self.p.browser.get("locale", "en-US"),
                                "timezone_id": self.p.browser.get("timezone", "UTC")}
        if "device" in vp:
            dev = dict(self._pw.devices[vp["device"]])
            engine = vp.get("browser") or dev.pop("default_browser_type", engine)
            dev.pop("default_browser_type", None)
            opts.update(dev)
        else:
            opts["viewport"] = {"width": int(vp["width"]), "height": int(vp["height"])}
            if vp.get("touch"):
                opts["has_touch"] = True
                opts["is_mobile"] = True
        if self.p.browser.get("permissions"):
            opts["permissions"] = self.p.browser["permissions"]
        if self.p.browser.get("color_scheme"):
            opts["color_scheme"] = self.p.browser["color_scheme"]
        if self.p.browser.get("http_credentials"):          # basic auth, sent to the app's own origin only
            opts["http_credentials"] = {**expand(self.p.browser["http_credentials"], self.p), "origin": _origin(self.p.base_url)}
        return engine, opts

    def new_context(self, viewport: str | None, storage_state: str | None = None):
        engine, opts = self.context_options(viewport)
        if engine not in self._browsers:
            launcher = getattr(self._pw, engine)
            self._browsers[engine] = launcher.launch(headless=not self.headed)
        if storage_state:
            opts["storage_state"] = storage_state
        context = self._browsers[engine].new_context(**opts)
        if self.p.browser.get("headers"):
            first_party_headers(context, self.p)
        return context, engine


# ---- auth --------------------------------------------------------------------------

def account_values(profile: Profile, account: str) -> dict:
    """The account's fields for `{account.x}` in `auth`, expanded now, at login, not when the profile was loaded: the
    test accounts of a deployed stand come from CI secrets (`password: "{env.QA_PASSWORD}"`), exported by the job."""
    if account not in profile.accounts:
        raise StepError(f"unknown account {account!r}", "case_invalid")
    return {"name": account, **expand(profile.accounts[account] or {}, profile)}


def login(profile: Profile, context, page, account: str, driver: WebDriver | None = None) -> dict:
    """Log the browser context in as `account`. Returns variables saved from the login response."""
    acct = account_values(profile, account)
    auth = expand(profile.auth, profile, account=acct)
    unset = sorted({v for v, _ in env_refs([acct, auth], "")})     # left unexpanded: not in the environment
    if unset:   # the environment's fault (a secret the job does not export), so INCONCLUSIVE rather than a failed login
        raise StepError(f"login as {account}: {', '.join(unset)} not set in the environment", "login_failed")
    saved: dict[str, Any] = {}
    if "http" in auth:
        h = auth["http"]
        kw: dict[str, Any] = {"method": h.get("method", "POST"), "timeout": 20000}
        if "json" in h:
            kw["data"] = h["json"]
        if "form" in h:
            kw["form"] = h["form"]
        kw["headers"] = request_headers(profile, profile.url(h["path"]), h.get("headers"))
        resp = context.request.fetch(profile.url(h["path"]), **kw)
        if resp.status >= 400:
            raise StepError(f"login as {account}: HTTP {resp.status} {resp.text()[:160]}", "login_failed")
        if h.get("save"):
            from .probes import get_path
            data = resp.json()
            saved = {k: get_path(data, v) for k, v in h["save"].items()}
    if "steps" in auth:
        d = driver or WebDriver(profile, page, context, Path("."))
        for s in auth["steps"]:
            try:
                d.run(s)
            except StepError as e:
                raise StepError(f"login as {account}: {e}", "login_failed") from e
    if "check" in auth:
        c = auth["check"]
        url = profile.url(c["path"] if isinstance(c, dict) else c.split(" ", 1)[-1])
        resp = context.request.fetch(url, headers=request_headers(profile, url))
        if resp.status >= 400:
            raise StepError(f"login as {account}: check {c} → HTTP {resp.status}", "login_failed")
    if driver is not None:
        driver.vars.update(saved)
    return saved


# ---- one case ----------------------------------------------------------------------

def _step_summary(step: dict) -> str:
    try:
        name = step_name(step)
    except StepError:
        return json.dumps(step)[:120]
    arg = step[name]
    if name in ("goto", "wait", "reload", "back", "offline", "viewport", "press", "type", "screenshot", "ask", "login"):
        return str(arg if not isinstance(arg, dict) else arg.get("text") or arg.get("url") or arg.get("key") or arg)[:120]
    if name == "http":
        return f"{arg.get('method', 'GET')} {arg.get('path', arg.get('url', ''))}"
    if name in ("draw", "drag"):
        return f"on {describe_target(arg.get('within', 'canvas'))}"
    if name == "repeat":
        return f"×{arg.get('times')}"
    if name == "fill" and isinstance(arg, dict):
        return f"{describe_target(arg)} ← {str(arg.get('value', ''))[:40]!r}"
    return describe_target(arg)[:120]


def run_case(case: Case, profile: Profile, browsers: Browsers, out_dir: Path, *, agent=None,
             auth_cache: dict | None = None) -> dict:
    case_dir = out_dir / "cases" / case.id
    case_dir.mkdir(parents=True, exist_ok=True)
    ev: dict[str, Any] = {
        "case_id": case.id, "title": case.title, "case": case.raw, "goal": case.goal,
        "account": case.as_account, "viewport": case.viewport or profile.browser.get("viewport", "desktop"),
        "started": now_iso(), "steps": [], "setup": [], "observations": {},
        "cause": None, "error": None,
    }
    t0 = time.time()
    context = None
    coverage = None
    recorder = Recorder(profile.base_url, profile.ignore, profile.raw.get("first_party"))
    try:
        cached = (auth_cache or {}).get((case.as_account, ev["viewport"])) if case.as_account else None
        storage = cached[0] if cached else None
        context, engine = browsers.new_context(case.viewport, storage_state=storage)
        ev["browser"] = engine
        context.set_default_timeout(int(profile.browser.get("timeout_ms", 10000)))
        recorder.attach(context)
        page = context.new_page()
        if profile.sensor_on("coverage"):
            coverage = Coverage(context, engine, recorder.first_party, sources_for(browsers))
            coverage.watch(page)                # before the first navigation: the page's first scripts count too
        else:
            ev["coverage"] = {"engine": engine, "skipped": "sensors.coverage is off"}
        drv = WebDriver(profile, page, context, case_dir, recorder)
        drv._login = lambda acct: login(profile, context, page, acct, drv)
        if cached:
            drv.vars.update(cached[1])

        # 1. login
        recorder.phase = "setup"
        if case.as_account and not storage:
            try:
                login(profile, context, page, case.as_account, drv)
            except StepError as e:
                ev["cause"], ev["error"] = (e.kind if e.kind != "step_failed" else "login_failed"), str(e)
                raise _Stop()
            if auth_cache is not None and profile.auth.get("cache", True):
                st = case_dir / ".auth.json"
                context.storage_state(path=str(st))
                auth_cache[(case.as_account, ev["viewport"])] = (str(st), dict(drv.vars))

        # 2. setup (fixtures through API/UI) — failure is the environment's, not the product's
        for i, s in enumerate(case.setup):
            rec = {"i": i, "action": _safe_name(s), "summary": _step_summary(s), "ok": True}
            try:
                rec.update(drv.run(s))
            except StepError as e:
                rec.update(ok=False, error=str(e))
                ev["setup"].append(rec)
                ev["cause"] = "case_invalid" if e.kind == "case_invalid" else "setup_failed"
                ev["error"] = f"setup step {i}: {e}"
                raise _Stop()
            ev["setup"].append(rec)
        if case.setup:
            drv.settle(idle_ms=int(profile.browser.get("settle_ms", 500)))
        if any("js" in (p or {}) for p in profile.probes.values()) and page.url in ("about:blank", ""):
            drv.run({"goto": "/"})
            drv.settle()

        # 3. state before + read-back of preconditions
        before, errs = snapshot(profile, context, page, account=case.as_account)
        ev["state_before"], ev["probe_errors_before"] = before, errs
        if case.expect_before:
            seen = {**before, "vars": dict(drv.vars)}       # values saved by setup `http` steps are checkable too
            status, detail = C.d_state(expand(case.expect_before, profile, vars=drv.vars),
                                       {"state_after": seen, "state_before": seen, "probe_errors_after": errs}, None, {})
            ev["precondition"] = {"status": status, "detail": detail}
            if status != "pass":
                ev["cause"] = "precondition_mismatch" if status == "fail" else "probe_failed"
                ev["error"] = f"start state not as expected: {detail}"
                raise _Stop()

        # 4. act
        recorder.phase = "act"
        if case.goal and agent is not None:
            res = agent.achieve(case, drv, ev)
            ev["agent"] = res
            if not res.get("reached"):
                ev["cause"] = "agent_unavailable" if res.get("unavailable") else "goal_not_reached"
                ev["error"] = res.get("reason", "agent could not reach the goal")
        elif case.goal and not case.steps:
            ev["cause"], ev["error"] = "case_invalid", "goal-only case needs the agent (--agent)"
            raise _Stop()
        else:
            for i, s in enumerate(case.steps):
                recorder.step = i
                rec = {"i": i, "action": _safe_name(s), "summary": _step_summary(s), "ok": True}
                try:
                    rec.update(drv.run(s))
                except StepError as e:
                    rec.update(ok=False, error=str(e))
                    rec["screenshot"] = drv.screenshot(f"step-{i:02d}-failed")
                    ev["steps"].append(rec)
                    ev["cause"] = "case_invalid" if e.kind == "case_invalid" else "step_failed"
                    ev["error"] = f"step {i} ({rec['action']}): {e}"
                    break
                if profile.browser.get("screenshots", "steps") == "steps" and rec["action"] not in ("wait", "eval", "http", "screenshot"):
                    rec["screenshot"] = drv.screenshot(f"step-{i:02d}")
                ev["steps"].append(rec)
        recorder.step = None
        drv.settle(idle_ms=int(profile.browser.get("settle_ms", 500)))
        ev["vars"] = {k: v for k, v in drv.vars.items() if not re.search(r"csrf|token|secret|password", k, re.I)}

        # 5. observe: live checks, state after, audit, screenshot (on the tab the journey ended on)
        page = drv.page
        recorder.phase = "check"
        expanded = {lvl: expand(getattr(case, lvl), profile, vars=drv.vars) for lvl in ("must", "should")}
        ev["case"] = {**case.raw, **expanded}
        for level in ("must", "should"):
            for i, item in enumerate(expanded[level]):
                try:
                    name = C.check_name(item)
                except ValueError:
                    continue
                if name in C.OBSERVERS:
                    try:
                        ev["observations"][f"{level}.{i}"] = C.OBSERVERS[name](item[name], page)
                    except Exception as e:
                        ev["observations"][f"{level}.{i}"] = {"error": str(e)[:200]}
        after, errs = snapshot(profile, context, page, account=case.as_account)
        ev["state_after"], ev["probe_errors_after"] = after, errs
        ev["audit"] = audit_page(page, a11y=profile.sensor_on("a11y"))
        ev["cookies"] = cookie_facts(context, profile.base_url)
        ev["final_url"] = page.url
        try:
            ev["page_text"] = page.locator("body").inner_text(timeout=3000)[:20000]
        except Exception:
            ev["page_text"] = ""
        ev["final_screenshot"] = drv.screenshot("final")
        ev["answers"] = drv.answers
        ev["dialogs"] = drv.dialogs
        ev["http"] = drv.http_log
    except _Stop:
        pass
    except Exception as e:
        ev["cause"] = ev["cause"] or "browser_crashed"
        ev["error"] = ev["error"] or f"{type(e).__name__}: {e}"[:500]
        ev["traceback"] = traceback.format_exc()[-2000:]
    finally:
        ev["sensors"] = recorder.to_dict()
        ev["api"] = recorder.api
        ev["frontend_files"] = []
        if context is not None:
            try:
                if not ev.get("final_screenshot") and context.pages:
                    p = context.pages[0]
                    p.screenshot(path=str(case_dir / "final.png"), timeout=5000)
                    ev["final_screenshot"] = "final.png"
            except Exception:
                pass
            if coverage is not None:            # here, so a journey that stopped early is collected too
                ev["frontend_files"], ev["coverage"] = coverage.collect()
            try:
                context.close()
            except Exception:
                pass
        ev.setdefault("coverage", {"error": "no browser page to profile"})
        ev["duration_s"] = round(time.time() - t0, 2)
        (case_dir / "evidence.json").write_text(json.dumps(ev, indent=1, ensure_ascii=False, default=str))
    return ev


class _Stop(Exception):
    pass


def _safe_name(step: dict) -> str:
    try:
        return step_name(step)
    except StepError:
        return "?"


@contextmanager
def app_running(profile: Profile, out_dir: Path, start: bool = True, reset: bool = True):
    """Start the app if needed (and stop it afterwards); yield an INCONCLUSIVE reason or None."""
    proc = AppProcess(profile, out_dir / "app.log")
    reason = None
    try:
        if not proc.is_up():
            if start and profile.app.get("start"):
                try:
                    if reset:
                        proc.reset()
                    proc.start()
                except RuntimeError as e:
                    reason = str(e)
            else:
                # a deployed app (no app.start) may be waking up from a cold start or finishing a deploy: give it
                # app.ready.timeout_s before the run is INCONCLUSIVE
                deadline = time.time() + (0 if profile.app.get("start") else
                                          float((profile.app.get("ready") or {}).get("timeout_s", 30)))
                up = False
                while not up and time.time() < deadline:
                    time.sleep(2)
                    up = proc.is_up()
                if not up:
                    reason = f"app is not reachable at {proc.ready_url}"
        yield reason
    finally:
        proc.stop()
