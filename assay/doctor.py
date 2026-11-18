"""Preflight (`assay doctor`): is the stand ready before anyone spends time and judge money?
Checks the profile and cases, the environment variables it reads, browsers, the app (starts it if it is down, or
says which deployed app it tests), each account's login, each probe, and the judge backend — and says what to fix."""
from __future__ import annotations

import time
from pathlib import Path

from .config import URL_SETTINGS, Profile, env_refs, load_cases, load_plugins, unset_env, url_problem
from .judge import Judge


def env_lines(profile: Profile) -> list[tuple[str, str]]:
    """(what, detail) for every {env.X} the profile reads that is not set, naming the settings that use it: on a
    deployed stand the address, the test accounts and the database URL all come from CI secrets."""
    return [(f"{var} is not set", "used by " + ", ".join(where)) for var, where in unset_env(profile).items()]


def doctor(profile: Profile, log=print) -> int:
    ok = True

    def line(good: bool, what: str, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        log(f"  {'✓' if good else '✗'} {what}" + (f" — {detail}" if detail else ""))

    log(f"assay doctor · {profile.title or profile.name}")
    try:
        plugins = load_plugins(profile)
        cases = load_cases(profile)
        line(True, f"profile and {len(cases)} cases parse", f"plugins: {plugins}" if plugins else "")
    except Exception as e:
        line(False, "profile/cases", str(e))
        return 3
    for what, detail in env_lines(profile):
        line(False, what, detail)
    if problem := url_problem(profile):      # no address, nothing else to check
        if not any(w in URL_SETTINGS for ws in unset_env(profile).values() for w in ws):   # else named just above
            line(False, "app address", problem)
        log("  fix the ✗ items above before running")
        return 3
    from .runner import AppProcess, Browsers, app_running, login
    from .probes import snapshot
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="assay-doctor-"))   # app.log of this preflight only
    remote = not profile.app.get("start")
    if remote:
        log(f"  · testing a deployed app at {profile.base_url}: no app.start, so Assay neither starts nor resets it")
    with app_running(profile, tmp) as down:
        if remote:
            refs = [v for v, _ in env_refs(profile.raw.get("base_url", ""), "base_url")] or ["base_url"]
            line(down is None, f"deployed app at {AppProcess(profile, tmp / 'app.log').ready_url}",
                 f"not reachable: is the stand up, is {' / '.join(refs)} right?" if down else "")
        else:
            line(down is None, f"app reachable at {profile.base_url}", down or "")
        if down:
            return 2
        with Browsers(profile) as b:
            engines = {(profile.viewports.get(v) or {}).get("browser") or ("webkit" if "device" in (profile.viewports.get(v) or {}) else "chromium")
                       for v in profile.viewports}
            for vp in profile.viewports:
                try:
                    ctx, eng = b.new_context(vp)
                    ctx.close()
                    line(True, f"browser for viewport {vp}", eng)
                except Exception as e:
                    line(False, f"browser for viewport {vp}", f"{e}"[:160] + " — run `uv run playwright install`")
            for acct in profile.accounts:
                ctx, _ = b.new_context(None)
                page = ctx.new_page()
                try:
                    t0 = time.time()
                    login(profile, ctx, page, acct)
                    line(True, f"login as {acct}", f"{time.time() - t0:.1f}s")
                except Exception as e:
                    line(False, f"login as {acct}", str(e)[:200])
                ctx.close()
            ctx, _ = b.new_context(None)
            page = ctx.new_page()
            acct = profile.scan.get("as") or next(iter(profile.accounts), None)
            try:
                if acct:
                    login(profile, ctx, page, acct)
                page.goto(profile.base_url, wait_until="load", timeout=30000)
            except Exception as e:
                line(False, "open base_url", str(e)[:160])
            state, errs = snapshot(profile, ctx, page, account=acct)
            for name in profile.probes:
                if name in errs:
                    line(False, f"probe {name}", errs[name])
                elif name in state:
                    v = state[name]
                    size = f"{len(v)} rows" if isinstance(v, list) else type(v).__name__
                    line(True, f"probe {name}", size)
                else:
                    log(f"  · probe {name} — scoped to an account, checked during cases")
            ctx.close()
    j = Judge(profile.judge)
    line(j.available or j.backend == "none", f"judge backend {j.backend}/{j.model}",
         "" if j.available else "not available: judge items will be INCONCLUSIVE")
    log("  ready" if ok else "  fix the ✗ items above before running")
    return 0 if ok else 2
