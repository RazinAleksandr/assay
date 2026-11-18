"""Runtime capture: the API calls a journey made and the app's own source files that ran (CDP coverage + source maps)."""
import base64
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from assay.config import Profile
from assay.coverage import Coverage, Script, SourceMap, Sources, decode_mappings, line_starts, map_ref, normalise, parse_data_url, ran, vlq
from assay.model import Case
from assay.runner import Browsers, run_case

FP = {"http://127.0.0.1:5199"}


# ---- units ------------------------------------------------------------------------

def test_vlq_decoding():
    assert vlq("AAAA") == [0, 0, 0, 0]
    assert vlq("AAgBC") == [0, 0, 16, 1]          # g = 32: continuation bit, the next char carries 16 << 1
    assert vlq("D") == [-1] and vlq("F") == [-2]
    assert vlq("2HwcqxB") == [123, 456, 789]


def test_mappings_keep_column_and_running_source_index():
    # line 0: col 0 → src 0, col 10 → src 1; line 1 empty; line 2: col 0 → src 0 (delta -1), col 5 maps to nothing
    lines = decode_mappings("AAAA,UCAA;;ADAA,K")
    assert lines == [([0, 10], [0, 1]), ([], []), ([0, 5], [0, -1])]


def test_offset_maps_through_a_hand_written_source_map():
    smap = SourceMap({"version": 3, "sourceRoot": "", "sources": ["../../src/a.ts", "webpack://app/./src/b.ts"],
                      "mappings": "AAAA,UCAA;;ADAA,K"})
    assert smap.source_at(0, 4) == "../../src/a.ts"
    assert smap.source_at(0, 10) == smap.source_at(0, 99) == "webpack://app/./src/b.ts"
    assert smap.source_at(1, 0) is None and smap.source_at(2, 7) is None and smap.source_at(9, 0) is None
    text = "function a(){}  b();\n\nx(); y();\n"
    script = Script(map=smap, starts=line_starts(text))
    assert script.source_at(text.index("b()")) == "webpack://app/./src/b.ts"
    assert script.source_at(text.index("x()")) == "../../src/a.ts"
    assert normalise(script.source_at(4)) == "src/a.ts"
    # V8 offsets count UTF-16 units: an emoji on an earlier line shifts them by two, not one
    assert line_starts("'😀'\nb") == [0, 5]


def test_source_map_reference_inline_or_separate():
    data = {"version": 3, "sources": ["src/x.ts"], "mappings": "AAAA"}
    inline = "data:application/json;base64," + base64.b64encode(json.dumps(data).encode()).decode()
    assert map_ref(f"x()\n//# sourceMappingURL={inline}\n", {}) == inline
    assert json.loads(parse_data_url(inline)) == data
    assert map_ref("a()\n//# sourceMappingURL=old.map\nb()\n//# sourceMappingURL=app.js.map", {}) == "app.js.map"
    assert map_ref("a()", {"sourcemap": "/a.js.map"}) == "/a.js.map"


@pytest.mark.parametrize("src,want", [
    ("http://127.0.0.1:5199/src/Feed.tsx?t=123", "src/Feed.tsx"),
    ("webpack://my-app/./src/a.ts", "src/a.ts"),
    ("webpack:///src/b.tsx", "src/b.tsx"),
    ("../../src/a.ts", "src/a.ts"),
    ("./src/c.ts#frag", "src/c.ts"),
    ("src/x/../y.ts", "src/y.ts"),
    ("http://127.0.0.1:5199/@fs/Users/me/app/shared/types.ts", "/Users/me/app/shared/types.ts"),
])
def test_normalise(src, want):
    assert normalise(src, FP) == want


@pytest.mark.parametrize("src", [
    "http://127.0.0.1:5199/node_modules/.vite/deps/react-dom_client.js?v=1a2b",
    "http://127.0.0.1:5199/@vite/client", "http://127.0.0.1:5199/@react-refresh",
    "http://127.0.0.1:5199/@id/__x00__virtual", "webpack://app/node_modules/react/index.js",
    "../node_modules/vite/client.mjs", "https://cdn.example.com/src/app.ts", "",
])
def test_third_party_and_tooling_are_excluded(src):
    assert normalise(src, FP) is None


def test_only_called_functions_other_than_the_top_level_count():
    top = {"functionName": "", "ranges": [{"startOffset": 0, "endOffset": 90, "count": 1}]}
    called = {"functionName": "", "ranges": [{"startOffset": 12, "endOffset": 40, "count": 2}]}
    idle = {"functionName": "unused", "ranges": [{"startOffset": 41, "endOffset": 80, "count": 0}]}
    assert [ran(f) for f in (top, called, idle)] == [False, True, False]


class _NoCDP:
    def on(self, *a):
        pass

    def new_cdp_session(self, page):
        raise RuntimeError("Target closed")


def test_coverage_failures_are_reported_never_raised():
    cov = Coverage(_NoCDP(), "chromium", FP, Sources())
    cov.watch(object())
    assert cov.collect() == ([], {"error": "CDP: Target closed"})
    assert Coverage(_NoCDP(), "webkit", FP, Sources()).collect() == \
        ([], {"engine": "webkit", "skipped": "only Chromium reports coverage"})


# ---- real Chromium ------------------------------------------------------------------

def _b64map(sources: list[str], mappings: str) -> str:
    data = json.dumps({"version": 3, "sources": sources, "mappings": mappings})
    return "data:application/json;base64," + base64.b64encode(data.encode()).decode()


APP_JS = """function used() { fetch('/api/ping').then(function (r) { return r.json(); }).then(function (d) { document.getElementById('out').textContent = 'pong ' + d.ok; }); }
function unused() { document.getElementById('out').textContent = 'never'; }
document.getElementById('go').addEventListener('click', used);
//# sourceMappingURL=app.js.map
"""
# a dev server's modules: line 0 is tooling the server wrapped around the module (unmapped, and it runs on load)
DIRECT_TSX = f"""const __hot = (() => 1)();
export function Direct() {{ document.getElementById('out2').textContent = 'direct'; }}
Direct();
//# sourceMappingURL={_b64map(["Direct.tsx"], ";AAAA;AAAA")}
"""
IDLE_TSX = f"""const __hot = (() => 1)();
export function Idle() {{ document.title = 'idle'; }}
//# sourceMappingURL={_b64map(["/abs/web/src/Idle.tsx"], ";AAAA")}
"""
FILES = {
    "index.html": '<!doctype html><title>t</title><button id="go">Go</button><p id="out"></p>'
                  '<script>(function () { window.inline = 1; })();</script><script src="/app.js"></script>',
    "app.js": APP_JS,
    "app.js.map": json.dumps({"version": 3, "sources": ["../../src/used.ts", "../../src/unused.ts"],
                              "mappings": "AAAA;ACAA;ADAA"}),
    "dev.html": '<!doctype html><title>d</title><p id="out2"></p>'
                '<script type="module" src="/src/Direct.tsx?t=1"></script><script type="module" src="/src/Idle.tsx"></script>',
    "src/Direct.tsx": DIRECT_TSX,
    "src/Idle.tsx": IDLE_TSX,
}


class _Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".tsx": "text/javascript", ".map": "application/json"}

    def do_GET(self):
        if self.path.startswith("/api/ping"):
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *a):
        pass


@pytest.fixture()
def site(tmp_path):
    root = tmp_path / "www"
    for name, text in FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Handler, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_journey_records_api_calls_and_the_source_files_that_ran(site, tmp_path):
    prof = Profile(name="t", root=tmp_path, base_url=site, sensors={"a11y": False}, browser={"settle_ms": 100})
    with Browsers(prof) as b:
        try:
            b.new_context(None)[0].close()
        except Exception as e:
            pytest.skip(f"Chromium cannot launch here: {e}")
        bundle = run_case(Case.from_dict({"id": "bundle", "steps": [
            {"goto": "/index.html"}, {"click": {"css": "#go"}}, {"wait_for": {"text": "pong true"}}]}), prof, b, tmp_path / "run")
        dev = run_case(Case.from_dict({"id": "dev", "steps": [
            {"goto": "/dev.html"}, {"wait_for": {"text": "direct"}}]}), prof, b, tmp_path / "run")
        tabs = run_case(Case.from_dict({"id": "tabs", "steps": [
            {"goto": "/dev.html"}, {"new_tab": "/index.html"}, {"click": {"css": "#go"}}, {"wait_for": {"text": "pong true"}}]}),
            prof, b, tmp_path / "run")
    assert bundle["cause"] is None, bundle["error"]
    assert bundle["api"] == [{"method": "GET", "path": "/api/ping", "status": 200, "step": 1, "phase": "act"}]
    assert bundle["frontend_files"] == ["src/used.ts"]          # unused() was loaded, never called
    assert bundle["coverage"] == {"engine": "chromium", "scripts": 2, "files": 1, "bundles_without_map": []}
    # a module served at its own URL is its own file; a module where only the server's wrapper ran is not listed
    assert dev["cause"] is None, dev["error"]
    assert dev["frontend_files"] == ["src/Direct.tsx"] and dev["api"] == []
    # a tab the journey opens later is profiled too
    assert tabs["cause"] is None, tabs["error"]
    assert tabs["frontend_files"] == ["src/Direct.tsx", "src/used.ts"] and tabs["api"][0]["step"] == 2
    saved = json.loads((tmp_path / "run" / "cases" / "bundle" / "evidence.json").read_text())
    assert saved["frontend_files"] == ["src/used.ts"] and saved["api"][0]["status"] == 200
