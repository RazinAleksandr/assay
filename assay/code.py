"""From a failing journey to the code a change touched (suspects), and the journey map that links the two.

Product → code, by the data: a failed state check knows which record and field the app got wrong (`comments.body`);
the run knows which API routes the journey called, which tables changed, which frontend files ran and what its steps
aimed at. Each hunk of the change's diff is scored by those signals: writes the wrong field > in the handler of a
route the journey called > mentions the table > the failing step's text > ran during the journey > the rest.
Everything here is deterministic: candidates and their order come from recorded facts, never from a model.

The journey map keeps the same signals per journey on every full run (analysis.json `journey_map`); assay/select.py
matches a pull request's diff against the reference's map to pick the journeys it can break.
"""
from __future__ import annotations

import os
import posixpath
import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from .probes import diff as state_diff
from .rules import route_of

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
SOURCE_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro", ".py", ".rb", ".go", ".java",
              ".kt", ".php", ".cs", ".rs", ".ex", ".exs", ".scala", ".swift", ".sql", ".html", ".css", ".scss", ".sass",
              ".less", ".graphql", ".gql", ".prisma"}


# ---- git -------------------------------------------------------------------------------------------------------------------

def _git(*args: str, cwd: str | Path | None = None) -> str | None:
    """git with output that does not depend on the user's config (no external diff, plain paths, a/ b/ prefixes)."""
    if args and args[0] == "diff":
        args = ("diff", "--no-ext-diff", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/", *args[1:])
    try:
        out = subprocess.run(["git", "-c", "core.quotePath=false", *args], capture_output=True, text=True, timeout=60, cwd=cwd)
    except Exception:
        return None
    return out.stdout if out.returncode == 0 else None


def repo_root(start: str | Path | None = None) -> Path | None:
    out = _git("rev-parse", "--show-toplevel", cwd=start)
    return Path(out.strip()) if out else None


def ci_diff(base_sha: str | None, cwd: str | Path | None = None) -> str | None:
    """The pull request's change in CI: the checked-out commit (GitHub's merge commit) against the target's commit.
    Shallow checkouts lack the base: fetch just that commit."""
    if not base_sha or not re.fullmatch(r"[0-9a-f]{7,40}", base_sha):
        return None
    if _git("cat-file", "-e", f"{base_sha}^{{commit}}", cwd=cwd) is None:
        _git("fetch", "--no-tags", "--quiet", "--depth=1", "origin", base_sha, cwd=cwd)
    # where the branch left the target: a checkout of the branch's head (not GitHub's merge commit) would otherwise
    # show the target's newer commits as reverted. A shallow merge commit has no history; its first parent is the base.
    fork = (_git("merge-base", base_sha, "HEAD", cwd=cwd) or "").strip() or base_sha
    return _git("diff", "--no-color", "-U3", fork, "HEAD", cwd=cwd)


def local_diff(start: str | Path | None = None) -> str | None:
    """What this branch changes on a laptop: the working tree (committed or not) against where it left the default
    branch. None outside git or on the default branch itself."""
    root = repo_root(start if start and Path(start).is_dir() else None)
    if not root:
        return None
    for ref in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        base = _git("merge-base", "HEAD", ref, cwd=root)
        if base:
            base = base.strip()
            head = (_git("rev-parse", "HEAD", cwd=root) or "").strip()
            text = _git("diff", "--no-color", "-U3", base, cwd=root)
            if base == head and not text:
                return None
            return text
    return None


# ---- the diff ----------------------------------------------------------------------------------------------------------------

@dataclass
class Hunk:
    file: str
    start: int                       # first line in the new file
    end: int
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)   # the hunk as the diff shows it (+, -, context)

    @property
    def changed(self) -> str:
        return "\n".join(self.added + self.removed)

    @property
    def code(self) -> str:
        """The changed lines without comments: a table named in a comment is not code that writes it."""
        out = []
        for line in self.added + self.removed:
            t = line.strip()
            if not t or t.startswith(("//", "#", "/*", "*", "--", "<!--")):
                continue
            out.append(re.sub(r"\s(//|#)\s.*$", "", line))
        return "\n".join(out)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _path(raw: str) -> str | None:
    raw = raw.strip().split("\t")[0]
    if raw == "/dev/null":
        return None
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
    return re.sub(r"^[ab]/", "", raw)


def parse_diff(text: str | None) -> list[Hunk]:
    """Hunks of a unified diff. A deleted file keeps its old path (its lines are all `removed`)."""
    hunks: list[Hunk] = []
    file = old = None
    header = False
    cur: Hunk | None = None
    for line in (text or "").splitlines():
        if line.startswith("diff --git "):
            file, old, cur, header = None, None, None, True
        elif header and line.startswith("--- "):
            old = _path(line[4:])
        elif header and line.startswith("+++ "):
            file = _path(line[4:]) or old
        elif line.startswith("@@") and file:
            header = False
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if not m:
                continue
            start, n = int(m[1]), int(m[2] if m[2] is not None else 1)
            cur = Hunk(file, max(start, 1), max(start, 1) + max(n, 1) - 1, lines=[line])
            hunks.append(cur)
        elif cur is not None and not header and line[:1] in ("+", "-", " "):
            cur.lines.append(line)
            if line[0] == "+":
                cur.added.append(line[1:])
            elif line[0] == "-":
                cur.removed.append(line[1:])
    return hunks


def changed_files(hunks: list[Hunk], text: str | None = None) -> list[str]:
    """Files the change touches, also those without text hunks (binary, mode-only)."""
    files = list(dict.fromkeys(h.file for h in hunks))
    for m in re.finditer(r"^(?:\+\+\+|---) (.+)$", text or "", re.M):
        f = _path(m[1])
        if f and f not in files:
            files.append(f)
    for m in re.finditer(r"^Binary files (.+?) and (.+?) differ$", text or "", re.M):
        for raw in (m[2], m[1]):
            f = _path(raw)
            if f and f not in files:
                files.append(f)
    return files


# ---- routes in code ----------------------------------------------------------------------------------------------------------

ROUTE_DECL = [
    # Express, Koa, Fastify, Hono, Elysia: app.post("/x", …), router.get('/x/:id', …)
    re.compile(r"\.(get|post|put|patch|delete|all|head|options)\s*\(\s*[\"'`](/[^\"'`\s]*)[\"'`]", re.I),
    # Flask, FastAPI, Starlette, Litestar: @app.get("/x"), @router.post("/x/{id}"), @bp.route("/x", methods=[…])
    re.compile(r"@\w+(?:\.\w+)*\.(get|post|put|patch|delete|route|api_route)\s*\(\s*[rf]?[\"'](/[^\"']*)[\"']", re.I),
    # Django: path("x/<int:id>/", …)
    re.compile(r"\b(re_)?path\s*\(\s*r?[\"'](\^?[\w/<>:.\-{}()?+*\\$]*)[\"']"),
    # Spring, ASP.NET, JAX-RS: @PostMapping("/x"), [HttpGet("x")], @Path("/x")
    re.compile(r"@(Get|Post|Put|Patch|Delete|Request)Mapping\s*\(\s*(?:value\s*=\s*)?[\"'](/?[^\"']*)[\"']"),
    re.compile(r"\[Http(Get|Post|Put|Patch|Delete)\s*\(\s*\"([^\"]*)\""),
]


@lru_cache(maxsize=4096)
def route_regex(pattern: str) -> re.Pattern | None:
    """A route as code declares it (/posts/:id, /posts/{id}, /posts/<int:id>, /posts/[id]) → a regex for the path a
    journey called, anchored at the end (routers mount under prefixes like /api). Needs one literal segment."""
    p = pattern.strip().lstrip("^").rstrip("$").strip("/")
    segs = [s for s in p.split("/") if s]
    if not segs or not any(re.fullmatch(r"[\w.\-~]+", s) and not s.startswith(":") for s in segs):
        return None
    out = []
    for s in segs:
        if re.fullmatch(r":\w+\??|\{[^}]+\}|<[^>]+>|\[\.{0,3}[^\]]+\]|\*", s):
            out.append(r"[^/]+")
        elif re.fullmatch(r"[\w.\-~]+", s):
            out.append(re.escape(s))
        else:
            return None
    return re.compile(r"(?:^|/)" + "/".join(out) + r"/?$")


def _decl_in(line: str) -> list[tuple[str, str]]:
    out = []
    for rx in ROUTE_DECL:
        for m in rx.finditer(line):
            method = (m.group(1) or "").upper()
            method = method if method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS") else ""
            out.append((method, m.group(2)))
    return out


def file_route(path: str) -> list[tuple[str, str]]:
    """File-system routes (Next.js, Remix, SvelteKit, Nuxt): app/api/posts/[id]/route.ts → /api/posts/[id]."""
    m = re.search(r"(?:^|/)(?:app|pages|routes|server/api|src/routes|src/app|src/pages)/(.+?)/?(?:route|\+server|index)?\.\w+$", path)
    if not m:
        return []
    route = "/" + re.sub(r"\([^)]*\)/?", "", m[1])          # route groups (marketing)/ are not in the URL
    return [("", route)] if route_regex(route) else []


def enclosing_routes(h: Hunk, root: Path | None, window: int = 160) -> list[tuple[str, str]]:
    """Routes a hunk belongs to: the ones it declares, and the nearest declaration above its first changed line in the
    new file (the handler the change is in). File-system routes come from the path."""
    found = [r for line in h.added + h.removed for r in _decl_in(line)]
    first = h.start
    for x in h.lines[1:]:
        if x[:1] in "+-":
            break
        first += 1
    if root and (root / h.file).is_file():
        try:
            lines = (root / h.file).read_text(errors="replace").splitlines()
        except OSError:
            lines = []
        for i in range(min(first - 1, len(lines) - 1), max(-1, first - 1 - window), -1):
            if (decl := _decl_in(lines[i])):
                found += [d for d in decl if d not in found]
                break
    return found + file_route(h.file)


def route_matches(decl: tuple[str, str], call: tuple[str, str]) -> bool:
    method, pattern = decl
    rx = route_regex(pattern)
    return bool(rx and (not method or method == call[0]) and rx.search(call[1]))


# ---- who imports a file ------------------------------------------------------------------------------------------------------

JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte", ".astro")
# import … from './x', export … from './x', import './x', require('./x'), import('./x'): relative paths only (packages
# are not the repository's code)
JS_IMPORT = re.compile(r"(?:\bfrom|\bimport|\brequire\s*\(|\bimport\s*\()\s*[\"'`](\.\.?(?:/[^\"'`\n]*)?)[\"'`]")
PY_FROM = re.compile(r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[^\n]*)", re.M)
PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([\w. \t,]+)", re.M)
NOT_CODE_DIRS = {"node_modules", "dist", "build", "out", "coverage", "vendor", "target", "__pycache__", "venv"}


class Repo:
    """The repository's files, listed and read at most once: a selection asks many questions of the same files (who
    imports this one, which routes that one declares). Tracked files (`git ls-files`), or a walk outside git."""

    def __init__(self, root: Path | None):
        self.root = Path(root) if root else None
        self._files: list[str] | None = None
        self._text: dict[str, str] = {}
        self._rev: dict[str, set[str]] | None = None

    @property
    def files(self) -> list[str]:
        if self._files is None:
            listed = _git("ls-files", cwd=self.root) if self.root and self.root.is_dir() else None
            self._files = listed.splitlines() if listed is not None else []
            if listed is None and self.root and self.root.is_dir():
                for d, dirs, names in os.walk(self.root):
                    dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in NOT_CODE_DIRS)
                    rel = Path(d).relative_to(self.root).as_posix()
                    self._files += [n if rel == "." else f"{rel}/{n}" for n in sorted(names)]
                    if len(self._files) > 50_000:
                        break
        return self._files

    def text(self, f: str) -> str:
        if f not in self._text:
            try:
                p = self.root / f
                self._text[f] = p.read_text(errors="replace") if p.stat().st_size < 1_000_000 else ""   # not minified bundles
            except (OSError, TypeError):
                self._text[f] = ""
        return self._text[f]

    def routes(self, f: str) -> list[tuple[str, str]]:
        """Routes the file declares anywhere in it (a declaration may span lines), and its file-system route."""
        return list(dict.fromkeys(_decl_in(self.text(f)) + file_route(f)))

    def importers(self, f: str) -> list[str]:
        """Files that run in the app and import `f`. JS/TS: relative imports, re-exports, require() and import(), the
        extension and /index optional, `./x.js` naming x.ts; Python: relative and absolute imports (src/ layouts too)."""
        if self._rev is None:
            known = set(self.files)
            modules: dict[str, list[tuple[str, str]]] = {}      # last name → (dotted path, file): pkg/x.py → pkg.x
            for x in self.files:
                if x.endswith(".py"):
                    dotted = x[:-3].removesuffix("/__init__").replace("/", ".")
                    modules.setdefault(dotted.rsplit(".", 1)[-1], []).append((dotted, x))
            self._rev = {}
            for x in self.files:
                if x.startswith(".assay/") or not is_source(x):
                    continue                      # tests and docs import code but do not run in the app
                targets = (_js_imports(x, self.text(x), known) if x.endswith(JS_EXT) else
                           _py_imports(x, self.text(x), known, modules) if x.endswith(".py") else set())
                for t in targets - {x}:
                    self._rev.setdefault(t, set()).add(x)
        return sorted(self._rev.get(f, ()))


def _js_imports(f: str, text: str, known: set[str]) -> set[str]:
    out, d = set(), posixpath.dirname(f)
    for spec in JS_IMPORT.findall(text):
        base = posixpath.normpath(posixpath.join(d, spec.split("?")[0]))
        stem = re.sub(r"\.[mc]?jsx?$", "", base)            # TypeScript's ESM style: import "./db.js" is db.ts
        for c in (base, *(base + e for e in JS_EXT), *(f"{base}/index{e}" for e in JS_EXT),
                  *((stem + e for e in (".ts", ".tsx", ".mts", ".cts")) if stem != base else ())):
            if c in known:
                out.add(c)
                break
    return out


def _py_imports(f: str, text: str, known: set[str], modules: dict[str, list[tuple[str, str]]]) -> set[str]:
    out: set[str] = set()
    pkg = [x for x in posixpath.dirname(f).split("/") if x]

    def absolute(dotted: str) -> None:
        out.update(x for name, x in modules.get(dotted.rsplit(".", 1)[-1], ()) if name == dotted or name.endswith("." + dotted))
    for dots, mod, names in PY_FROM.findall(text):
        names = [n for n in re.findall(r"\w+", re.sub(r"#.*", "", names)) if n != "as"]   # `from pkg import x`: x may be a module
        if not dots:
            for dotted in [f"{mod}.{n}" for n in names] + [mod]:
                absolute(dotted)
            continue
        if len(dots) - 1 > len(pkg):
            continue
        base = pkg[:len(pkg) - (len(dots) - 1)] + (mod.split(".") if mod else [])
        for parts in [base + [n] for n in names] + [base]:
            for c in ("/".join(parts) + ".py", "/".join(parts + ["__init__.py"])):
                if c in known:
                    out.add(c)
    for listed in PY_IMPORT.findall(text):
        for part in listed.split(","):
            if part.split():
                absolute(part.split()[0])
    return out


# ---- the journey map ---------------------------------------------------------------------------------------------------------

def probe_tables(probes: dict) -> dict[str, list[str]]:
    from .explain import tables_of
    return {name: tables_of(name, probes) for name in probes or {}}


def _targets(raw_steps: list) -> list[str]:
    out = []
    for st in raw_steps or []:
        if not isinstance(st, dict):
            continue
        for arg in st.values():
            if isinstance(arg, dict):
                for k in ("name", "label", "text", "placeholder", "testid"):
                    if isinstance(arg.get(k), str) and len(arg[k]) >= 3:
                        out.append(arg[k])
            elif isinstance(arg, str) and 3 <= len(arg) <= 60 and not arg.startswith(("/", "http", "#", ".", "[")):
                out.append(arg)
    return list(dict.fromkeys(out))


def journey_entry(ev: dict, probes: dict | None = None) -> dict:
    """What one journey touched: API calls (method + path), probes and tables whose data changed, frontend files that
    ran, pages it visited and the texts its steps aim at."""
    calls = [(c.get("method", "GET"), c.get("path") or urlparse(c.get("url", "")).path) for c in ev.get("api") or []]
    calls += [(c.get("method", "GET"), urlparse(c.get("url", "")).path) for c in ev.get("http") or []]
    before, after = ev.get("state_before") or {}, ev.get("state_after") or {}
    changed = sorted({p.split(".")[0].split("[")[0] for p in state_diff({k: before[k] for k in before if k in after},
                                                                       {k: after[k] for k in after if k in before})})
    tables = probe_tables(probes or {})
    return {"routes": sorted({f"{m} {p}" for m, p in calls if p}),
            "probes": changed, "tables": sorted({t for p in changed for t in tables.get(p, [])}),
            "files": sorted(ev.get("frontend_files") or []),
            "pages": sorted({route_of(u) for u in (ev.get("sensors") or {}).get("urls", [])}),
            "targets": _targets(((ev.get("case") or {}).get("steps") or []))[:40]}


def map_files(entries: dict[str, dict], root: Path | None) -> dict[str, dict]:
    """Frontend files as the browser saw them (`/src/Feed.tsx`, `src/Feed.tsx`) → paths in the repository, by suffix."""
    if not root:
        return entries
    listed = _git("ls-files", cwd=root)
    if not listed:
        return entries
    files = listed.splitlines()
    by_name: dict[str, list[str]] = {}
    for f in files:
        by_name.setdefault(f.rsplit("/", 1)[-1], []).append(f)
    cache: dict[str, str | None] = {}

    def resolve(p: str) -> str | None:
        if p in cache:
            return cache[p]
        q = p.lstrip("/")
        hits = [f for f in by_name.get(q.rsplit("/", 1)[-1], []) if f == q or f.endswith("/" + q) or q.endswith("/" + f)]
        cache[p] = min(hits, key=len) if len(hits) >= 1 else None
        return cache[p]
    for e in entries.values():
        e["files"] = sorted({resolve(f) or f for f in e.get("files", [])})
    return entries


# ---- suspects ----------------------------------------------------------------------------------------------------------------

def _word(w: str, text: str) -> bool:
    return bool(w) and re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", text) is not None


def _stack_files(failure: dict, ev: dict) -> set[str]:
    text = " ".join([str(failure.get("stack") or "")] + [str(x.get("stack") or x.get("url") or "")
                                                          for x in (ev.get("sensors") or {}).get("page_errors", [])])
    return {urlparse(m).path.lstrip("/") for m in re.findall(r"https?://[^\s)]+?\.\w+(?=:\d)", text)}


def suspects_for(case: dict, ev: dict, diff_text: str | None, jmap: dict | None = None, root: Path | None = None,
                 limit: int = 5) -> list[dict]:
    """The hunks of the change most likely to have caused this journey's failure, best first, each with why."""
    hunks = parse_diff(diff_text)
    if not hunks:
        return []
    root = root or repo_root() or Path.cwd()
    f = case.get("failure") or {}
    addr = (f.get("address") or {}) if isinstance(f, dict) else {}
    fields = [x for x in addr.get("fields") or [] if len(x) >= 2]
    tables = addr.get("tables") or []
    entry = journey_entry(ev) if ev else ((jmap or {}).get(case["case_id"]) or {})
    files = set(map_files({"x": {"files": entry.get("files", [])}}, root)["x"]["files"]) if entry.get("files") else set()
    fail_step = (f.get("step") or {}).get("i")
    calls = [(c.get("method", "GET"), c.get("path") or "", c.get("step")) for c in ev.get("api") or []] if ev else \
        [(r.split(" ", 1)[0], r.split(" ", 1)[1], None) for r in entry.get("routes", [])]
    targets = []
    if fail_step is not None and ev:
        raw = ((ev.get("case") or {}).get("steps") or [])
        targets = _targets(raw[fail_step:fail_step + 1]) if fail_step < len(raw) else []
    stack = _stack_files(f, ev or {})
    scored = []
    for h in hunks:
        why, score = [], 0.0
        hit = [x for x in fields if _word(x, h.code)]
        if hit:
            score += 4
            field_ref = ", ".join(f"`{addr.get('probe')}.{x}`" if addr.get("probe") else f"`{x}`" for x in hit)
            why.append(f"changes code that writes {', '.join(f'`{x}`' for x in hit)} (the wrong value is in {field_ref})")
        tab = [t for t in tables if _word(t, h.code + "\n" + "\n".join(x[1:] for x in h.lines[1:] if x[:1] == " "))]
        if tab:
            score += 2
            why.append(f"touches table {', '.join(f'`{t}`' for t in tab)}")
        decls = enclosing_routes(h, root)
        for d in decls:
            hits = [c for c in calls if route_matches(d, c[:2])]
            if hits:
                m, p, st = hits[0]
                w = m in WRITE_METHODS
                score += 3 + (1 if w else 0)
                why.append(f"in the handler of `{d[0] + ' ' if d[0] else ''}{d[1]}`, which the journey called"
                           + (f" at step {st + 1}" if isinstance(st, int) else "") + f" (`{m} {p}`)")
                break
        txt = [t for t in targets if _word(t, h.code)]
        if txt:
            score += 2
            why.append(f"changes the text the failing step looks for ({', '.join(f'“{t}”' for t in txt[:2])})")
        if any(h.file.endswith(s) or s.endswith(h.file) for s in stack if s):
            score += 2
            why.append("in the stack of the error")
        if h.file in files:
            score += 1
            why.append("ran during the journey")
        if tab and re.search(r"migrat|schema|model|entity|\.sql$|prisma", h.file, re.I):
            score += 1
            why.append("schema or model of that table")
        scored.append((score, h, why))
    scored.sort(key=lambda x: -x[0])
    out = [x for x in scored if x[0] >= 2][:limit]
    if len(out) < 3:                      # the rest of the change, weakest last: honest about what is not linked
        out += [x for x in scored if x[0] < 2][: 3 - len(out)]
    return [{"file": h.file, "lines": f"{h.start}-{h.end}", "score": s,
             "why": "; ".join(why) if why else "changed in this change (no direct link to the failure found)",
             "hunk": "\n".join(h.lines[:40])} for s, h, why in out]


def stamp(cases: list[dict], evidence: dict[str, dict], diff_text: str | None, jmap: dict | None,
          only: set[str] | None = None) -> None:
    """Attach suspects to the failing journeys of an analysis (in place); `only`: the ones this change broke (a journey
    that failed the same way on the target branch was not broken by it)."""
    if not diff_text:
        return
    root = repo_root()
    for c in cases:
        if c.get("verdict") == "FAIL" and (only is None or c["case_id"] in only):
            c["suspects"] = suspects_for(c, evidence.get(c["case_id"]) or {}, diff_text, jmap, root)


NOT_RUNTIME = re.compile(r"(^|/)(__tests__|tests?|spec|e2e|cypress|playwright)/|\.(test|spec|stories)\.\w+$"
                         r"|\.(md|rst|txt|adoc)$|(^|/)(LICENSE|CHANGELOG|CODEOWNERS|CONTRIBUTING)[^/]*$"
                         r"|(^|/)\.(gitignore|gitattributes|editorconfig|prettierrc[^/]*|eslintrc[^/]*|eslintignore|prettierignore)$"
                         r"|^docs/", re.I)


def is_source(path: str) -> bool:
    """Whether a file can change what users get. Only files known not to (docs, tests, editor and lint config) are
    excluded; templates, locales, assets and anything unknown count, so an unmapped one runs every journey."""
    return not NOT_RUNTIME.search(path)


def ci_base_sha(ctx: dict, env: dict | None = None) -> str | None:
    env = dict(os.environ if env is None else env)
    return ctx.get("base_sha") or env.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") or env.get("ASSAY_BASE_SHA")
