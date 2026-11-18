"""App profile (assay.yaml) and suite loading.

The profile is the only product-specific part. For most apps it is pure config:
how to start the app, how to log in, where state lives (probes), what to ignore.
Code plugins are optional (custom steps / probes / checks).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .model import Case

DEFAULT_VIEWPORTS = {
    "desktop": {"width": 1366, "height": 860},
    "mobile": {"device": "iPhone 13"},
}

DEFAULT_GATE = {
    "fail_on_case_fail": True,          # any must-FAIL case blocks the release
    "max_new": {"blocker": 0, "critical": 0},
    "min_rating": {},                   # e.g. {reliability: B, accessibility: C}
    "max_inconclusive": 0.2,            # above this share the gate is INCONCLUSIVE, not PASS
}


@dataclass
class Profile:
    name: str
    root: Path
    base_url: str
    title: str = ""
    kind: str = "web"
    app: dict = field(default_factory=dict)
    accounts: dict = field(default_factory=dict)
    auth: dict = field(default_factory=dict)
    probes: dict = field(default_factory=dict)
    volatile: list[str] = field(default_factory=list)
    sensors: dict = field(default_factory=dict)
    ignore: dict = field(default_factory=dict)
    viewports: dict = field(default_factory=lambda: dict(DEFAULT_VIEWPORTS))
    judge: dict = field(default_factory=dict)
    gate: dict = field(default_factory=lambda: dict(DEFAULT_GATE))
    scan: dict = field(default_factory=dict)
    chat: dict = field(default_factory=dict)
    agent: dict = field(default_factory=dict)
    browser: dict = field(default_factory=dict)
    suites: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def sensor_on(self, name: str) -> bool:
        v = self.sensors.get(name, True)
        return bool(v) if not isinstance(v, dict) else v.get("enabled", True)

    def url(self, path: str) -> str:
        if re.match(r"^[a-z]+://", path):
            return path
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")


_MISSING = object()


def _lookup(ctx: Any, key: str) -> Any:
    """The value at a dotted path (`env.HOME`, `row.customer.name`), or _MISSING."""
    for part in key.split("."):
        if not (isinstance(ctx, dict) and part in ctx):
            return _MISSING
        ctx = ctx[part]
    return ctx


def _expand(value: Any, ctx: dict) -> Any:
    """Expand {env.X}, {root}, {account.x} and friends inside strings, recursively. A placeholder the context does not
    know stays as written, so expansion can happen in stages: {row.x} when the suite loads, {vars.uid} at run time."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            v = _lookup(ctx, m.group(1))
            return m.group(0) if v is _MISSING else str(v)
        return re.sub(r"\{([a-zA-Z_][\w.]*)\}", repl, value)
    if isinstance(value, list):
        return [_expand(v, ctx) for v in value]
    if isinstance(value, dict):          # keys too: a dataset names state paths by its rows (`tickets.{row.id}.status`)
        return {(_expand(k, ctx) if isinstance(k, str) else k): _expand(v, ctx) for k, v in value.items()}
    return value


def expand(value: Any, profile: Profile, **extra: Any) -> Any:
    ctx = {"env": {**os.environ, **profile.env}, "root": str(profile.root),
           "base_url": profile.base_url, "origin": _origin(profile.base_url), **extra}
    return _expand(value, ctx)


def _origin(url: str) -> str:
    m = re.match(r"^([a-z]+://[^/]+)", url or "")
    return m.group(1) if m else url


ENV_REF = re.compile(r"\{env\.(\w+)\}")
URL_SETTINGS = ("base_url", "app.ready.url")          # needed before anything runs; the rest expands when it is used


def env_refs(value: Any, where: str) -> list[tuple[str, str]]:
    """(variable, setting) for every {env.X} inside a value, so a missing secret is reported with the place that needs
    it (`accounts.qa.password`) instead of as a login that fails for no visible reason."""
    if isinstance(value, str):
        return [(v, where) for v in ENV_REF.findall(value)]
    if isinstance(value, dict):
        return [r for k, v in value.items() for r in env_refs(v, f"{where}.{k}")]
    if isinstance(value, list):
        return [r for i, v in enumerate(value) for r in env_refs(v, f"{where}[{i}]")]
    return []


def browser_headers(profile: Profile) -> dict:
    """`browser.headers`: sent with the run's requests to the app's own origin (its pages and API calls, the probes' and
    logins' requests, the readiness check). What a protected preview needs:
    `x-vercel-protection-bypass: "{env.VERCEL_AUTOMATION_BYPASS_SECRET}"`."""
    h = expand(profile.browser.get("headers") or {}, profile)
    missing = sorted({m for v in h.values() for m in re.findall(r"\{env\.(\w+)\}", str(v))})
    if missing:
        raise ValueError(f"browser.headers needs the environment variable {missing[0]}, which is not set")
    return {str(k): str(v) for k, v in h.items()}


def request_headers(profile: Profile, url: str, own: dict | None = None) -> dict | None:
    """Headers for a request Assay makes itself (login, probes, `http` steps): `browser.headers` when it goes to the app's
    own origin, under the request's own headers."""
    extra = browser_headers(profile) if profile.browser.get("headers") and _origin(url) == _origin(profile.base_url) else {}
    return {**extra, **(own or {})} or None


def unset_env(profile: Profile) -> dict[str, list[str]]:
    """The environment variables the profile reads as {env.X} that are not set, each with the settings that use it.
    Values expand when they are used (a CI job may export a secret after the profile is loaded), so this is the one
    place that sees them all up front: `assay doctor` lists them, `load_profile` stops on the ones the URL needs."""
    env = {**os.environ, **profile.env}
    out: dict[str, list[str]] = {}
    for key in ("base_url", "app", "accounts", "auth", "http_headers", "probes", "browser"):
        for var, where in env_refs(profile.raw.get(key, getattr(profile, key, None)), key):
            if var not in env:
                out.setdefault(var, []).append(where)
    return out


def url_problem(profile: Profile) -> str | None:
    """Why the app's address cannot be used, or None. A deployed app is usually named by the environment
    (`base_url: "{env.ASSAY_BASE_URL}"`): unset, that is a configuration error naming the variable, not a URL with
    braces in it. Set but empty (GitHub gives "" for a variable nobody defined) it is not a URL either."""
    for var, where in unset_env(profile).items():
        used = [w for w in where if w in URL_SETTINGS]
        if used:
            return (f"{' and '.join(used)} {'needs' if len(used) == 1 else 'need'} the environment variable {var}, "
                    f"which is not set (export {var}=https://staging.example.com, or set it in the CI job's env)")
    if not re.match(r"^[a-z][a-z0-9+.-]*://[^/\s]", profile.base_url or ""):
        refs = [v for v, _ in env_refs(profile.raw.get("base_url", ""), "base_url")]
        return f"base_url is {profile.base_url!r}, not a URL" + (f": is {' or '.join(refs)} empty?" if refs else "")
    return None


def load_profile(path: str | Path, strict: bool = True) -> Profile:
    """Read assay.yaml. `strict` (every command but doctor) stops on an app address that cannot be used; doctor loads
    leniently and lists every missing variable at once."""
    path = Path(path)
    if path.is_dir():
        path = path / "assay.yaml"
    raw = yaml.safe_load(path.read_text()) or {}
    root = path.parent.resolve()
    env = {k: str(v) for k, v in (raw.get("env") or {}).items()}
    # env values may reference {root}
    env = {k: v.replace("{root}", str(root)) for k, v in env.items()}
    gate = dict(DEFAULT_GATE)
    gate.update(raw.get("gate") or {})
    viewports = dict(DEFAULT_VIEWPORTS)
    viewports.update(raw.get("viewports") or {})
    prof = Profile(
        name=raw.get("name") or root.name, root=root, base_url=raw["base_url"],
        title=raw.get("title", ""), kind=raw.get("kind", "web"), app=raw.get("app") or {},
        accounts=raw.get("accounts") or {}, auth=raw.get("auth") or {}, probes=raw.get("probes") or {},
        volatile=list(raw.get("volatile") or []), sensors=raw.get("sensors") or {},
        ignore=raw.get("ignore") or {}, viewports=viewports, judge=raw.get("judge") or {},
        gate=gate, scan=raw.get("scan") or {}, chat=raw.get("chat") or {}, agent=raw.get("agent") or {},
        browser=raw.get("browser") or {}, suites=list(raw.get("suites") or ["cases"]),
        plugins=list(raw.get("plugins") or []), env=env, raw=raw,
    )
    prof.base_url = expand(prof.base_url, prof)
    if strict and (problem := url_problem(prof)):
        raise ValueError(f"{path}: {problem}")
    from .rules import set_route_patterns
    set_route_patterns(prof.scan.get("route_patterns"))
    return prof


def load_cases(profile: Profile, selectors: list[str] | None = None) -> list[Case]:
    """Load every case from the profile's suites (YAML or JSON, a list or {cases: [...]}). A journey's group is its file
    inside the suite without the extension (cases/social/feed.yaml → social/feed) unless it names one."""
    files: list[tuple[Path, Path]] = []                  # (file, the folder its group is relative to)
    for s in profile.suites:
        p = (profile.root / s)
        if p.is_dir():
            files += [(x, p) for x in sorted(p.rglob("*")) if x.suffix in (".yaml", ".yml", ".json")]
        elif p.exists():
            files.append((p, p.parent))
    cases: list[Case] = []
    seen: set[str] = set()
    for f, base in files:
        data = json.loads(f.read_text()) if f.suffix == ".json" else yaml.safe_load(f.read_text())
        if isinstance(data, dict):
            data = data.get("cases", [])
        for d in (x for t in data or [] for x in _each(t, profile.root)):
            c = Case.from_dict(d, source=str(f.relative_to(profile.root)), group=f.relative_to(base).with_suffix("").as_posix())
            if c.id in seen:
                raise ValueError(f"duplicate case id {c.id!r} in {f}")
            seen.add(c.id)
            cases.append(c)
    if selectors:
        cases = [c for c in cases if any(_select(c, s) for s in selectors)]
    return cases


ROW_ID = re.compile(r"[\w.:@+-]{1,80}")         # it becomes part of a case id: a folder name and a URL


def _each(d: Any, root: Path) -> list:
    """A case with `each:` is one journey over a dataset, as evals tools run a test template: one case per row, with
    `{row.<field>}` filled in every string of it and the id `<id>[<row id>]` (the row's `id`, else its number from 1).
    Other placeholders stay for run time ({vars.uid}, {env.X}). A title or promise that names no row field gets the row
    id, so the rows read apart in reports. Each case keeps its row (`row`) for the evidence."""
    if not isinstance(d, dict) or "each" not in d:
        return [d]
    cid = d.get("id")
    if not cid:
        raise ValueError("a case with `each` needs an id")
    rows = _rows(d["each"], root, cid)
    body = {k: v for k, v in d.items() if k not in ("id", "each")}
    fields = set(re.findall(r"\{row\.([\w.]+)\}", json.dumps(body, default=str, ensure_ascii=False)))
    out = []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"case {cid!r}: row {i} of `each` must be a mapping of fields, got {row!r:.60}")
        rid = str(row["id"]) if row.get("id") not in (None, "") else str(i)
        if not ROW_ID.fullmatch(rid):
            raise ValueError(f"case {cid!r}: row id {rid!r:.60} must be a plain name (letters, digits, . _ - : @ +)")
        missing = sorted(f for f in fields if _lookup(row, f) is _MISSING)
        if missing:
            raise ValueError(f"case {cid!r}, row {rid!r}: no field {missing[0]!r} for {{row.{missing[0]}}}")
        x = _expand(body, {"row": row})
        for k in ("title", "promise"):
            if x.get(k) and "{row." not in str(body[k]):
                x[k] = f"{x[k]} ({rid})"
        out.append({"id": f"{cid}[{rid}]", **x, "row": row})
    return out


def _rows(each: Any, root: Path, cid: str) -> list:
    """The rows of `each:`: a list in the case, or a .csv (the header names the fields; values are text), .json or
    .yaml file with a list of objects, relative to the .assay folder."""
    if isinstance(each, str):
        f = root / each
        if f.suffix == ".csv":
            import csv
            with f.open(newline="", encoding="utf-8-sig") as fh:        # -sig: spreadsheets save a BOM
                each = list(csv.DictReader(fh, restval=""))
        elif f.suffix in (".json", ".yaml", ".yml"):
            each = json.loads(f.read_text()) if f.suffix == ".json" else yaml.safe_load(f.read_text())
        else:
            raise ValueError(f"case {cid!r}: `each` names a .csv, .json or .yaml file, got {each!r}")
    if not isinstance(each, list) or not each:
        raise ValueError(f"case {cid!r}: `each` must be a non-empty list of rows, or a .csv/.json/.yaml file of them")
    return each


def _select(case: Case, sel: str) -> bool:
    """`-k` selectors: a glob on the id, `tag:<name>`, `group:<glob>` (`group:social/*`), `is:critical` or `is:ai`.
    An exact id is matched first: `[...]` in a dataset row's id (`draft.reply[late-delivery]`) is a character class to
    a glob. The id as written (`draft.reply`) selects all its rows."""
    import fnmatch
    if sel.startswith("tag:"):
        return sel[4:] in case.tags
    if sel.startswith("group:"):
        return fnmatch.fnmatchcase(case.group, sel[6:])
    if sel in ("is:critical", "is:ai"):
        return bool(getattr(case, sel[3:]))
    if sel == case.id or ("row" in case.raw and case.id.rpartition("[")[0] == sel):
        return True
    return fnmatch.fnmatch(case.id, sel)


def load_plugins(profile: Profile) -> list[str]:
    """Import plugin modules (dotted names or .py paths relative to the project) that register steps/checks."""
    import importlib
    import importlib.util
    loaded = []
    for ref in profile.plugins:
        if ref.endswith(".py"):
            path = (profile.root / ref).resolve()
            spec = importlib.util.spec_from_file_location(f"assay_plugin_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        else:
            mod = importlib.import_module(ref)
        if hasattr(mod, "register"):
            mod.register(profile)
        loaded.append(ref)
    return loaded
