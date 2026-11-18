"""Runtime coverage: which of the app's own source files actually ran during a journey (Chromium only).

Why: a pull request changes files and a journey fails; the files that journey executed are what links the two.
V8 precise coverage (CDP Profiler) counts calls per function. A function other than a script's top level with
a count > 0 means code really ran (a component rendered, a handler fired), not merely that a module loaded.
Dev servers (Vite) serve each module at its own URL, so the URL is the file; bundles go through their source
map (a separate .map or an inline data: map). This is a sensor: every failure is caught and reported in the
evidence, a journey never breaks because of it.
"""
from __future__ import annotations

import base64
import json
import re
import weakref
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from .sensors import origin_of

SOURCE_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".vue", ".svelte")
# third-party code and dev/bundler tooling: never "the app's own file", whatever origin serves it
EXCLUDE = ("node_modules", "/@vite/", "/@react-refresh", "/@id/", "vite/client", ".vite/deps",
           "vite/preload-helper", "vite/modulepreload-polyfill", "webpack/bootstrap", "webpack/runtime", "\x00")
# webpack names the modules it evals in dev by these (`webpack://app/./src/a.ts`, `webpack-internal:///(layer)/./src/a.ts`)
BUNDLER_URL = re.compile(r"^webpack(-internal)?://[^/]*/(\([\w-]+\)/)?")
MAX_MAP_BYTES = 20 * 1024 * 1024
CDP_TIMEOUT_MS = 15000                  # a hung renderer must not hang the journey with it
MINIFIED_LINE = 2000                    # a line this long without a map: a built bundle, not a module served as written
B64 = {c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")}


# ---- paths -------------------------------------------------------------------------

def normalise(src: str, first_party: set[str] | None = None) -> str | None:
    """A script URL or a source-map source → the repo-like path a diff names (`src/Feed.tsx`); None for third-party
    and tooling. `..` above the root is dropped: bundlers write sources relative to their output folder
    (`../../src/a.ts`), and the part after the climb is what matches the repository."""
    if not src or any(x in src for x in EXCLUDE):
        return None
    s = BUNDLER_URL.sub("", src)
    absolute = s.startswith("/")                 # a filesystem path written into a map stays absolute
    if "://" in s:
        u = urlparse(s)
        if u.scheme in ("http", "https") and first_party is not None and origin_of(s) not in first_party:
            return None
        s, absolute = u.path, u.scheme == "file"
    s = re.split(r"[?#]", s, maxsplit=1)[0]
    if s.startswith("/@fs/"):                    # Vite: a file outside its root, by absolute path
        s, absolute = s[4:], True
    parts: list[str] = []
    for p in s.split("/"):
        if p == "..":
            if parts:
                parts.pop()
        elif p not in ("", "."):
            parts.append(p)
    path = "/".join(parts)
    return ("/" + path if absolute else path) if path else None


# ---- source maps -------------------------------------------------------------------

def vlq(segment: str) -> list[int]:
    """Base64 VLQ of a source map segment: 5 data bits per char, bit 6 continues the value, its lowest bit is the sign."""
    out: list[int] = []
    value = shift = 0
    for ch in segment:
        d = B64[ch]
        value += (d & 31) << shift
        if d & 32:
            shift += 5
        else:
            out.append(-(value >> 1) if value & 1 else value >> 1)
            value = shift = 0
    return out


def decode_mappings(mappings: str) -> list[tuple[list[int], list[int]]]:
    """Per generated line: the segments' start columns and the source index each maps to (-1: maps to nothing).
    Only those two fields are kept; the source index is a delta running across lines, the column restarts per line."""
    lines: list[tuple[list[int], list[int]]] = []
    src = 0
    for line in mappings.split(";"):
        cols: list[int] = []
        srcs: list[int] = []
        col = 0
        for seg in line.split(",") if line else ():
            if not seg:
                continue
            f = vlq(seg)
            col += f[0]
            cols.append(col)
            if len(f) >= 4:
                src += f[1]
                srcs.append(src)
            else:
                srcs.append(-1)
        lines.append((cols, srcs))
    return lines


class SourceMap:
    """The part of a v3 source map that answers "which original file is this generated (line, column) from"."""

    def __init__(self, data: dict):
        root = (data.get("sourceRoot") or "").rstrip("/")
        self.sources = [f"{root}/{s}" if root and "://" not in s and not s.startswith("/") else s
                        for s in (data.get("sources") or [])]
        self.lines = decode_mappings(data.get("mappings") or "")

    def source_at(self, line: int, col: int) -> str | None:
        if line >= len(self.lines):
            return None
        cols, srcs = self.lines[line]
        i = bisect_right(cols, col) - 1
        if i < 0 or not 0 <= srcs[i] < len(self.sources):
            return None
        return self.sources[srcs[i]]


@dataclass
class Script:
    """What a run keeps of a fetched script: where its lines start and its source map (not the text itself)."""
    html: bool = False                  # a page: its inline <script>s report offsets inside a tag we cannot place
    built: bool = False                 # minified: its URL is an output file, not a source file
    map: SourceMap | None = None
    starts: list[int] = field(default_factory=lambda: [0])   # line starts in UTF-16 units, like V8 offsets

    def source_at(self, offset: int) -> str | None:
        if self.map is None:
            return None
        line = bisect_right(self.starts, offset) - 1
        return self.map.source_at(line, offset - self.starts[line])


def line_starts(text: str) -> list[int]:
    starts, pos = [0], 0
    for line in text.split("\n")[:-1]:
        pos += (len(line) if line.isascii() else len(line.encode("utf-16-le")) // 2) + 1
        starts.append(pos)
    return starts


def map_ref(text: str, headers: dict) -> str | None:
    """The script's source map reference: the last `//# sourceMappingURL=` comment, or the SourceMap header."""
    refs = re.findall(r"//[#@][ \t]*sourceMappingURL=([^\s'\"]+)", text)
    return refs[-1] if refs else headers.get("sourcemap") or headers.get("x-sourcemap")


def parse_data_url(url: str) -> str:
    head, _, payload = url.partition(",")
    return base64.b64decode(payload + "=" * (-len(payload) % 4)).decode() if ";base64" in head else unquote(payload)


class Sources:
    """Fetched scripts and decoded maps per URL for a whole run: a 2 MB bundle is fetched and decoded once,
    not once per journey."""

    def __init__(self):
        self._scripts: dict[str, Script] = {}

    def get(self, request, url: str) -> Script:
        if not url.startswith(("http://", "https://")):
            return Script()                     # webpack's eval'd modules: the name is all there is
        if url not in self._scripts:
            try:
                self._scripts[url] = self._load(request, url)
            except Exception:
                self._scripts[url] = Script()
        return self._scripts[url]

    def _load(self, request, url: str) -> Script:
        resp = request.get(url, timeout=15000)
        if not resp.ok:
            return Script()
        if "html" in resp.headers.get("content-type", ""):
            return Script(html=True)
        text = resp.text()
        ref = map_ref(text, resp.headers)
        if not ref:
            return Script(built=any(len(line) > MINIFIED_LINE for line in text.split("\n")))
        if ref.startswith("data:"):
            raw = parse_data_url(ref)
        else:
            m = request.get(urljoin(url, ref), timeout=30000)
            if not m.ok or int(m.headers.get("content-length") or 0) > MAX_MAP_BYTES:
                return Script()
            body = m.body()
            if len(body) > MAX_MAP_BYTES:
                return Script()
            raw = body.decode("utf-8", "replace")
        return Script(map=SourceMap(json.loads(raw)), starts=line_starts(text))


_RUN_SOURCES: "weakref.WeakKeyDictionary[Any, Sources]" = weakref.WeakKeyDictionary()


def sources_for(run: Any) -> Sources:
    """The Sources cache of one run (keyed by its Browsers): a later run in the same process starts clean,
    since the app may have changed in between."""
    if run not in _RUN_SOURCES:
        _RUN_SOURCES[run] = Sources()
    return _RUN_SOURCES[run]


# ---- the sensor ----------------------------------------------------------------------

def ran(fn: dict) -> bool:
    """Called at least once, and not the script's own top level (which runs whenever the script loads)."""
    r = (fn.get("ranges") or [{}])[0]
    top = not fn.get("functionName") and r.get("startOffset") == 0
    return not top and r.get("count", 0) > 0


class Coverage:
    """CDP precise coverage for one browser context: every page of the journey, collected once at its end."""

    def __init__(self, context, engine: str, first_party: set[str], sources: Sources):
        self.context = context
        self.engine = engine
        self.first_party = first_party
        self.sources = sources
        self.pages: list = []
        self.sessions: list = []
        self.error: str | None = None
        if engine == "chromium":
            context.on("page", self.watch)       # tabs the journey opens later (best effort: a popup may run first)

    def watch(self, page) -> None:
        """Start counting on a page, before it navigates, so the page's first scripts are counted too."""
        if self.engine != "chromium" or any(p is page for p in self.pages):
            return
        self.pages.append(page)
        try:
            cdp = self.context.new_cdp_session(page)
            cdp.send("Profiler.enable", timeout=CDP_TIMEOUT_MS)
            cdp.send("Profiler.startPreciseCoverage", {"callCount": True, "detailed": False}, timeout=CDP_TIMEOUT_MS)
            self.sessions.append(cdp)
        except Exception as e:
            self.error = self.error or f"CDP: {e}"[:300]

    def collect(self) -> tuple[list[str], dict]:
        """(frontend_files, coverage facts). Never raises; needs the context still open (it fetches scripts)."""
        if self.engine != "chromium":
            return [], {"engine": self.engine, "skipped": "only Chromium reports coverage"}
        try:
            return self._collect()
        except Exception as e:
            return [], {"error": f"coverage failed: {e}"[:300]}

    def _collect(self) -> tuple[list[str], dict]:
        executed: dict[str, set[int]] = {}
        scripts: set[str] = set()
        taken = 0
        for cdp in self.sessions:
            try:
                result = cdp.send("Profiler.takePreciseCoverage", timeout=CDP_TIMEOUT_MS)["result"]
                taken += 1
            except Exception as e:                # the tab was closed during the journey
                self.error = self.error or f"CDP: {e}"[:300]
                continue
            for s in result:
                url = s.get("url") or ""
                ours = origin_of(url) in self.first_party or BUNDLER_URL.match(url)
                if not ours or not normalise(url, self.first_party):
                    continue
                scripts.add(url)
                offsets = {f["ranges"][0]["startOffset"] for f in s.get("functions", []) if ran(f)}
                if offsets:
                    executed.setdefault(url, set()).update(offsets)
        if not taken:
            return [], {"error": self.error or "no page was profiled"}
        files: set[str] = set()
        no_map: list[str] = []
        for url, offsets in executed.items():
            got = self._files(url, offsets)
            if got is None:
                no_map.append(urlparse(url).path)
            else:
                files |= got
        out = sorted(files)
        return out, {"engine": "chromium", "scripts": len(scripts), "files": len(out),
                     "bundles_without_map": sorted(set(no_map))[:10]}

    def _files(self, url: str, offsets: set[int]) -> set[str] | None:
        """Source files behind the executed functions of one script; None when nothing can name them."""
        own = normalise(url, self.first_party) if urlparse(url).path.endswith(SOURCE_EXT) else None
        script = self.sources.get(self.context.request, url)
        if script.html:
            return set()
        if script.map is None:
            return {own} if own and not script.built else None
        names = {script.source_at(o) for o in offsets} - {None}
        # a dev-server module with an inline map of itself: the URL already is the file. The map still matters:
        # functions it does not map are tooling the server wrapped around the module (HMR, React refresh).
        if own and names and all(n.rsplit("/", 1)[-1].split("?")[0] == own.rsplit("/", 1)[-1] for n in names):
            return {own}
        return {f for f in (normalise(n, self.first_party) for n in names) if f}
