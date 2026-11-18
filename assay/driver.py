"""Web driver: executes the step DSL with Playwright.

A step is a one-key dict (`{click: {role: button, name: Share}}`). Targets are described the
way a user perceives the page (role + name, label, text, placeholder, test id), so cases read
like a manual test script and survive CSS refactors.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from .config import Profile, expand, request_headers

ACTIONS = ("goto", "click", "dblclick", "hover", "tap", "fill", "type", "press", "select", "check",
           "uncheck", "upload", "wait_for", "wait", "reload", "back", "offline", "viewport", "eval",
           "http", "draw", "drag", "scroll", "screenshot", "ask", "repeat", "login", "clear_storage", "clock",
           "storage", "expect", "new_tab", "switch_tab", "download", "dialogs")


CUSTOM_STEPS: dict[str, Any] = {}


def register_step(name: str, fn) -> None:
    """Plugin API: add a product-specific step, fn(driver, arg) -> dict | None."""
    CUSTOM_STEPS[name] = fn


class StepError(Exception):
    def __init__(self, msg: str, kind: str = "step_failed"):
        super().__init__(msg)
        self.kind = kind


def step_name(step: dict) -> str:
    if not isinstance(step, dict) or len(step) != 1:
        # allow extra modifiers like {click: ..., timeout: 5000}
        keys = [k for k in step if k in ACTIONS or k in CUSTOM_STEPS] if isinstance(step, dict) else []
        if len(keys) != 1:
            raise StepError(f"step must have exactly one action key: {step!r}", "case_invalid")
        return keys[0]
    return next(iter(step))


def describe_target(t: Any) -> str:
    if isinstance(t, str):
        return repr(t)
    if isinstance(t, dict):
        return ", ".join(f"{k}={v!r}" for k, v in t.items() if k not in ("value", "file", "files", "key"))
    return repr(t)


def strip(target: Any, *keys: str) -> Any:
    """Drop option keys (value, file, key, state...) from a dict target; strings pass through."""
    if isinstance(target, dict):
        return {k: v for k, v in target.items() if k not in keys}
    return target


def locate(page, target: Any):
    """Resolve a target description to a Playwright locator."""
    if isinstance(target, str):
        if target.startswith(("css=", "text=", "xpath=", "role=", "internal:", "#", ".", "[", "//")):
            return page.locator(target).first
        return page.get_by_text(target).first
    if not isinstance(target, dict):
        raise StepError(f"bad target {target!r}", "case_invalid")
    scope = page
    if "within" in target:
        scope = locate(page, target["within"])
    exact = target.get("exact")
    if "role" in target:
        kw: dict[str, Any] = {}
        if "name" in target:
            kw["name"] = target["name"]
            if exact is not None:
                kw["exact"] = exact
        loc = scope.get_by_role(target["role"], **kw)
    elif "label" in target:
        loc = scope.get_by_label(target["label"], exact=bool(exact))
    elif "placeholder" in target:
        loc = scope.get_by_placeholder(target["placeholder"], exact=bool(exact))
    elif "testid" in target:
        loc = scope.get_by_test_id(target["testid"])
    elif "alt" in target:
        loc = scope.get_by_alt_text(target["alt"], exact=bool(exact))
    elif "title" in target:
        loc = scope.get_by_title(target["title"], exact=bool(exact))
    elif "text" in target:
        loc = scope.get_by_text(target["text"], exact=bool(exact))
    elif "css" in target:
        loc = scope.locator(target["css"])
    else:
        raise StepError(f"target needs role/label/placeholder/testid/alt/title/text/css: {target!r}", "case_invalid")
    if "has_text" in target:
        loc = loc.filter(has_text=target["has_text"])
    if "nth" in target:
        return loc.nth(int(target["nth"]))
    if target.get("last"):
        return loc.last
    return loc.first


class WebDriver:
    def __init__(self, profile: Profile, page, context, shots_dir: Path, recorder=None,
                 on_screenshot: Callable[[str], None] | None = None, login: Callable[[str], None] | None = None):
        self.p = profile
        self.page = page
        self.context = context
        self.shots = shots_dir
        self.recorder = recorder
        self.timeout = int(profile.browser.get("timeout_ms", 10000))
        self.answers: list[dict] = []
        self.http_log: list[dict] = []
        import secrets
        self.vars: dict[str, Any] = {"uid": secrets.token_hex(3)}   # {vars.uid}: unique per case run
        self._login = login
        self.dialog_policy = "dismiss"
        self.dialogs: list[str] = []
        page.set_default_timeout(self.timeout)
        self._wire_dialogs(page)

    def _wire_dialogs(self, page) -> None:
        def handle(d):
            self.dialogs.append(f"{d.type}: {d.message}"[:200])
            try:
                d.accept() if self.dialog_policy == "accept" else d.dismiss()
            except Exception:
                pass
        page.on("dialog", handle)

    # -- helpers ------------------------------------------------------------
    def _target_and_opts(self, spec: Any) -> tuple[Any, dict]:
        if isinstance(spec, dict) and "target" in spec:
            return spec["target"], spec
        return spec, spec if isinstance(spec, dict) else {}

    def settle(self, idle_ms: int = 400, max_ms: int = 4000) -> None:
        """Wait until the network is quiet for a moment (best effort, never fails).
        Apps that poll forever never reach networkidle: set browser.settle: fixed."""
        if self.p.browser.get("settle", "idle") == "idle":
            try:
                self.page.wait_for_load_state("networkidle", timeout=max_ms)
            except Exception:
                pass
        self.page.wait_for_timeout(idle_ms)

    def screenshot(self, name: str, full_page: bool = False) -> str:
        path = self.shots / f"{name}.png"
        try:
            self.page.screenshot(path=str(path), full_page=full_page, timeout=10000, animations="disabled")
            return path.name
        except Exception:
            return ""

    # -- the step executor ----------------------------------------------------
    def run(self, step: dict) -> dict:
        step = expand(step, self.p, vars=self.vars)
        name = step_name(step)
        arg = step[name]
        timeout = int(step.get("timeout", self.timeout)) if isinstance(step, dict) else self.timeout
        page = self.page
        t0 = time.time()
        info: dict[str, Any] = {}
        try:
            if name == "goto":
                url = arg if isinstance(arg, str) else arg["url"]
                wait = "load" if isinstance(arg, str) else arg.get("wait", "load")
                resp = page.goto(self.p.url(url), wait_until=wait, timeout=max(timeout, 30000))
                if resp is not None and resp.status >= 500:
                    raise StepError(f"GET {url} → HTTP {resp.status}")
            elif name in ("click", "dblclick", "hover", "tap", "check", "uncheck"):
                target, opts = self._target_and_opts(arg)
                loc = locate(page, strip(target, "force", "position"))
                kw = {"timeout": timeout}
                if opts.get("force"):
                    kw["force"] = True
                if name in ("click", "tap") and opts.get("position"):
                    kw["position"] = dict(zip(("x", "y"), opts["position"]))
                if name in ("check", "uncheck"):
                    # controlled inputs (React) flip only after the app's own round-trip: click once,
                    # then wait for the state the user asked for instead of failing immediately
                    want = name == "check"
                    if loc.is_checked(timeout=timeout) != want:
                        loc.click(**kw)
                        deadline = time.time() + timeout / 1000
                        while loc.is_checked() != want:
                            if time.time() > deadline:
                                raise StepError(f"{name} {describe_target(target)}: state did not change")
                            page.wait_for_timeout(100)
                else:
                    getattr(loc, name)(**kw)
            elif name == "fill":
                target, opts = self._target_and_opts(arg)
                locate(page, strip(target, "value")).fill(str(opts.get("value", "")), timeout=timeout)
            elif name == "type":
                text = arg if isinstance(arg, str) else arg["text"]
                page.keyboard.type(text, delay=(arg.get("delay", 15) if isinstance(arg, dict) else 15))
            elif name == "press":
                if isinstance(arg, str):
                    page.keyboard.press(arg)
                else:
                    target, opts = self._target_and_opts(arg)
                    key = opts["key"]
                    tgt = strip(target, "key")
                    if tgt:
                        locate(page, tgt).press(key, timeout=timeout)
                    else:
                        page.keyboard.press(key)
            elif name == "select":
                target, opts = self._target_and_opts(arg)
                locate(page, strip(target, "value")).select_option(str(opts["value"]), timeout=timeout)
            elif name == "upload":
                target, opts = self._target_and_opts(arg)
                files = opts.get("files") or [opts["file"]]
                files = [str((self.p.root / f).resolve()) if not Path(f).is_absolute() else f for f in files]
                locate(page, strip(target, "file", "files")).set_input_files(files, timeout=timeout)
            elif name == "wait_for":
                target, opts = self._target_and_opts(arg)
                state = opts.get("state", "visible") if isinstance(opts, dict) else "visible"
                locate(page, strip(target, "state")).wait_for(state=state, timeout=max(timeout, 15000))
            elif name == "wait":
                page.wait_for_timeout(int(arg))
            elif name == "reload":
                page.reload(wait_until="load")
            elif name == "back":
                page.go_back(wait_until="load")
            elif name == "offline":
                self.context.set_offline(bool(arg))
                if self.recorder is not None:
                    self.recorder.offline = bool(arg)
            elif name == "viewport":
                vp = self.p.viewports.get(arg, arg) if isinstance(arg, str) else arg
                if "width" not in vp:
                    raise StepError("viewport step needs width/height (device emulation is per case)", "case_invalid")
                page.set_viewport_size({"width": int(vp["width"]), "height": int(vp["height"])})
            elif name == "eval":
                expr = arg if isinstance(arg, str) else arg["js"]
                body = expr if expr.strip().startswith(("async", "(", "function")) else f"async () => {{ {expr} }}"
                info["result"] = page.evaluate(body)
            elif name == "http":
                info.update(self._http(arg))
            elif name == "draw":
                self._draw(arg)
            elif name == "drag":
                self._drag(arg)
            elif name == "scroll":
                if isinstance(arg, dict) and "by" in arg:
                    page.mouse.wheel(0, int(arg["by"]))
                elif arg in ("bottom", "end"):
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                else:
                    page.evaluate("() => window.scrollTo(0, 0)")
            elif name == "screenshot":
                info["screenshot"] = self.screenshot(str(arg))
            elif name == "ask":
                info.update(self._ask(arg))
            elif name == "repeat":
                for _ in range(int(arg["times"])):
                    for s in arg["steps"]:
                        self.run(s)
            elif name == "login":
                if not self._login:
                    raise StepError("login step needs an auth section in the profile", "case_invalid")
                self._login(arg)
            elif name == "clock":
                self._clock(arg)
            elif name == "expect":
                self._expect(arg, timeout)
            elif name == "new_tab":
                # a second tab of the same user (same storage); later steps act on it until switch_tab
                newp = self.context.new_page()
                newp.set_default_timeout(self.timeout)
                self._wire_dialogs(newp)
                if isinstance(arg, str):
                    newp.goto(self.p.url(arg), wait_until="load")
                self.page = newp
                info["tab"] = len(self.context.pages) - 1
            elif name == "switch_tab":
                self.page = self.context.pages[int(arg)]
                self.page.bring_to_front()
            elif name == "download":
                target, opts = self._target_and_opts(arg)
                with page.expect_download(timeout=max(timeout, 15000)) as dl:
                    locate(page, strip(target, "save_as")).click(timeout=timeout)
                d = dl.value
                dest = self.shots / (opts.get("save_as") or d.suggested_filename)
                d.save_as(str(dest))
                self.vars.update(download_name=d.suggested_filename, download_size=dest.stat().st_size)
                info.update(download=d.suggested_filename, size=dest.stat().st_size)
            elif name == "dialogs":
                self.dialog_policy = str(arg)          # accept | dismiss (alert/confirm/beforeunload)
            elif name == "storage":
                arg = dict(arg)
                sets = {}
                for k, v in (arg.get("set") or {}).items():
                    if isinstance(v, dict) and set(v) == {"file"}:   # {file: fixtures/save.json}
                        v = (self.p.root / v["file"]).read_text()
                    sets[k] = v
                arg["set"] = sets
                if page.url in ("about:blank", ""):
                    page.goto(self.p.url("/"), wait_until="load")
                page.evaluate("""(kv) => { for (const [k, v] of Object.entries(kv.set || {}))
                    localStorage.setItem(k, typeof v === 'string' ? v : JSON.stringify(v));
                    for (const k of (kv.remove || [])) localStorage.removeItem(k); }""", arg)
                if arg.get("reload", True):
                    page.reload(wait_until="load")
            elif name == "clear_storage":
                page.evaluate("""async () => { localStorage.clear(); sessionStorage.clear();
                    if (indexedDB.databases) { for (const d of await indexedDB.databases()) indexedDB.deleteDatabase(d.name); } }""")
            elif name in CUSTOM_STEPS:
                info.update(CUSTOM_STEPS[name](self, arg) or {})
            else:
                raise StepError(f"unknown step {name!r}", "case_invalid")
        except StepError:
            raise
        except Exception as e:
            msg = str(e).split("\n")[0][:300]
            target = describe_target(arg) if name not in ("goto", "eval", "wait") else ""
            raise StepError(f"{name} {target}: {msg}".strip()) from e
        info["ms"] = round((time.time() - t0) * 1000)
        return info

    # -- complex steps ------------------------------------------------------
    def _http(self, arg: dict) -> dict:
        method = arg.get("method", "GET").upper()
        url = self.p.url(arg["path"] if "path" in arg else arg["url"])
        kw: dict[str, Any] = {"method": method, "timeout": 20000}
        if "json" in arg:
            kw["data"] = arg["json"]
        if "form" in arg:
            kw["form"] = arg["form"]
        if "multipart" in arg:
            mp = {}
            for k, v in arg["multipart"].items():
                if isinstance(v, dict) and "file" in v:
                    fp = (self.p.root / v["file"]).resolve()
                    mp[k] = {"name": fp.name, "mimeType": v.get("mime", "application/octet-stream"),
                             "buffer": fp.read_bytes()}
                else:
                    mp[k] = str(v)
            kw["multipart"] = mp
        headers = request_headers(self.p, url, {**expand(self.p.raw.get("http_headers") or {}, self.p, vars=self.vars),
                                                **(arg.get("headers") or {})})
        if headers:
            kw["headers"] = headers
        resp = self.context.request.fetch(url, **kw)
        expect = arg.get("expect_status")
        ok = (resp.status == expect) if expect else resp.status < 400
        entry = {"method": method, "url": url, "status": resp.status}
        self.http_log.append(entry)
        if not ok:
            body = ""
            try:
                body = resp.text()[:200]
            except Exception:
                pass
            raise StepError(f"{method} {arg.get('path', url)} → HTTP {resp.status} {body}")
        data = None
        try:
            data = resp.json()
            entry["json"] = data
        except Exception:
            pass
        from .probes import MISSING, get_path
        for var, path in (arg.get("save") or {}).items():
            v = get_path(data, path) if data is not None else MISSING
            if v is MISSING:
                raise StepError(f"save {var}: {path} not in response")
            self.vars[var] = v
        return {"status": resp.status}

    def _expect(self, arg: dict, timeout: int) -> None:
        """Mid-journey checkpoint: fail the step (→ FAIL) if the page is not as expected right now."""
        page = self.page
        wait = min(timeout, int(arg.get("within_ms", 4000)))
        for key, spec in arg.items():
            if key == "within_ms":
                continue
            try:
                if key == "visible":
                    locate(page, spec).wait_for(state="visible", timeout=wait)
                elif key == "not_visible":
                    locate(page, spec).wait_for(state="hidden", timeout=wait)
                elif key == "text":
                    page.get_by_text(spec).first.wait_for(state="visible", timeout=wait)
                elif key == "url":
                    page.wait_for_url(re.compile(spec), timeout=wait)
                elif key == "invariant":
                    body = spec if spec.strip().startswith(("async", "(", "function")) else f"() => ({spec})"
                    page.wait_for_function(body, timeout=wait)
                else:
                    raise StepError(f"expect: unknown key {key!r}", "case_invalid")
            except StepError:
                raise
            except Exception:
                raise StepError(f"checkpoint failed: {key} {describe_target(spec)}")

    def _clock(self, arg: dict) -> None:
        """Deterministic time for games/animations: install, freeze, fast-forward."""
        clock = self.page.clock
        if "install" in arg:
            t = arg["install"]
            clock.install(time=t) if t not in (True, None) else clock.install()
        if "fixed" in arg:
            clock.set_fixed_time(arg["fixed"])
        if "run_for" in arg:
            clock.run_for(int(arg["run_for"]))
        if "fast_forward" in arg:
            clock.fast_forward(int(arg["fast_forward"]))
        if "resume" in arg:
            clock.resume()

    def _box(self, target: Any) -> dict:
        loc = locate(self.page, target)
        loc.wait_for(state="visible", timeout=self.timeout)
        box = loc.bounding_box()
        if not box:
            raise StepError(f"target has no box: {describe_target(target)}")
        return box

    def _pt(self, box: dict, p: list) -> tuple[float, float]:
        x, y = float(p[0]), float(p[1])
        # relative (0..1) coordinates inside the target, or absolute pixels if > 1
        if x <= 1 and y <= 1:
            return box["x"] + x * box["width"], box["y"] + y * box["height"]
        return box["x"] + x, box["y"] + y

    def _draw(self, arg: dict) -> None:
        box = self._box(arg.get("within", {"css": "canvas"}))
        strokes = arg.get("strokes") or [arg["points"]]
        mouse = self.page.mouse
        for pts in strokes:
            x, y = self._pt(box, pts[0])
            mouse.move(x, y)
            mouse.down()
            for p in pts[1:]:
                x, y = self._pt(box, p)
                mouse.move(x, y, steps=int(arg.get("steps", 12)))
            mouse.up()
            self.page.wait_for_timeout(60)

    def _drag(self, arg: dict) -> None:
        if "within" in arg:
            box = self._box(arg["within"])
            a, b = self._pt(box, arg["from"]), self._pt(box, arg["to"])
        else:
            ba, bb = self._box(arg["from"]), self._box(arg["to"])
            a = (ba["x"] + ba["width"] / 2, ba["y"] + ba["height"] / 2)
            b = (bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
        m = self.page.mouse
        m.move(*a)
        m.down()
        m.move(*b, steps=15)
        m.up()

    def _ask(self, arg: Any) -> dict:
        """Chat surfaces (AI assistants): type a message, wait for the end of the turn, read the answer."""
        chat = self.p.chat
        if not chat:
            raise StepError("ask step needs a `chat` section in the profile", "case_invalid")
        text = arg if isinstance(arg, str) else arg["text"]
        if chat.get("open"):
            try:
                if not locate(self.page, chat["input"]).is_visible():
                    locate(self.page, chat["open"]).click()
            except Exception:
                locate(self.page, chat["open"]).click()
        answers = locate(self.page, chat["answer"]) if chat.get("answer") else None
        before = self.page.locator(_css_of(chat["answer"])).count() if chat.get("answer") else 0
        inp = locate(self.page, chat["input"])
        inp.fill(text)
        if chat.get("send"):
            locate(self.page, chat["send"]).click()
        else:
            inp.press("Enter")
        t0 = time.time()
        turn_timeout = int(chat.get("turn_timeout_s", 180)) * 1000
        done = chat.get("done")
        if done:
            locate(self.page, done).wait_for(state="visible", timeout=turn_timeout)
        else:
            self.page.wait_for_function(
                f"() => document.querySelectorAll({_css_of(chat['answer'])!r}).length > {before}",
                timeout=turn_timeout)
            self.settle(idle_ms=int(chat.get("idle_ms", 1500)), max_ms=turn_timeout)
        answer = ""
        if answers is not None:
            answer = self.page.locator(_css_of(chat["answer"])).last.inner_text()
        rec = {"question": text, "answer": answer, "latency_s": round(time.time() - t0, 2)}
        self.answers.append(rec)
        return rec


def _css_of(target: Any) -> str:
    if isinstance(target, dict) and "css" in target:
        return target["css"]
    if isinstance(target, str):
        return target
    raise StepError("chat.answer must be a css selector", "case_invalid")
