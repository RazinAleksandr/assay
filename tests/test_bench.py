"""The benchmark workflow: arms that differ only in settings, the published result, its pages and its checks."""
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from assay.bench import stats, with_env
from assay.ci import Server, bench_markdown
from assay.config import Profile
from assay.server import Store, make_handler
from assay.uploads import check_bench
from test_access import AUD, get, oidc, token


def _bench():
    verdicts = {"sonnet": {"ai.refund": ["PASS", "PASS", "PASS"], "ai.spanish": ["PASS", "PASS", "PASS"], "ai.urgent": ["PASS", "FAIL", "PASS"]},
                "haiku": {"ai.refund": ["PASS", "PASS", "PASS"], "ai.spanish": ["FAIL", "FAIL", "PASS"], "ai.urgent": ["FAIL", "FAIL", "FAIL"]}}
    res = stats(verdicts)
    res.update(app="Relay", generated="2026-11-12 21:00", case_titles={"ai.refund": "Refund", "ai.spanish": "Spanish", "ai.urgent": "Urgent"},
               arms_meta={"sonnet": {"title": "Relay", "base_url": "http://127.0.0.1:5410", "env": {"ASSISTANT_MODEL": "sonnet"}, "median_journey_s": 6.1},
                          "haiku": {"title": "Relay", "base_url": "http://127.0.0.1:5410", "env": {"ASSISTANT_MODEL": "haiku"}, "median_journey_s": 4.2}})
    return res


def test_env_arms_start_the_same_app_with_other_settings(tmp_path):
    p = Profile(name="relay", root=tmp_path, base_url="http://127.0.0.1:5410",
                app={"start": {"cmd": "node server.mjs", "env": {"PORT": 5410}}})
    arm, kv = with_env(p, "ASSISTANT_MODEL=haiku,PROMPT=v2")
    assert kv == {"ASSISTANT_MODEL": "haiku", "PROMPT": "v2"}
    assert arm.app["start"]["env"] == {"PORT": 5410, "ASSISTANT_MODEL": "haiku", "PROMPT": "v2"}
    assert p.app["start"]["env"] == {"PORT": 5410}                     # the baseline profile is untouched
    with pytest.raises(ValueError):
        with_env(p, "no-equals-sign")


def test_benchmark_stats_and_summary():
    res = _bench()
    assert res["arms"]["sonnet"]["flaky"] == ["ai.urgent"] and res["arms"]["haiku"]["flaky"] == ["ai.spanish"]
    assert res["arms"]["haiku"]["pass@1"] < res["arms"]["sonnet"]["pass@1"]
    text = bench_markdown(res, "http://assay/b/1", {"pr": "3", "sha": "a" * 40}, "relay")
    assert "assay-benchmark:relay" in text and "ASSISTANT_MODEL=haiku" in text and "http://assay/b/1" in text


def test_bench_upload_is_checked():
    bad = _bench()
    bad["arms"]["sonnet"]["pass@1"] = "<script>"
    with pytest.raises(ValueError):
        check_bench(bad)
    bad = _bench()
    bad["per_case"]["../../x"] = {"sonnet": ["PASS"]}
    with pytest.raises(ValueError):
        check_bench(bad)
    assert check_bench(_bench())["baseline"] == "sonnet"


def test_benchmark_is_published_and_private_like_the_project(tmp_path):
    store = Store(tmp_path / "data")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, "admintok", AUD, {}, oidc()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        d = tmp_path / "bench"
        d.mkdir()
        (d / "bench.json").write_text(json.dumps(_bench()))
        res = Server(host, token("acme/relay", "private")).upload_bench(d, {"title": "Relay", "branch": "main", "sha": "b" * 40})
        assert "/gh/acme/relay/benchmarks/" in res["url"] and "k=" in res["url"]
        path = res["url"][len(AUD):]
        clean = path.split("?")[0]
        assert get(host + clean)[0] == 404                               # private: strangers see nothing
        st, _, hd = get(host + path)
        cookie = hd["Set-Cookie"].split(";")[0]
        st, body, _ = get(host + clean, cookie)
        assert st == 200 and "Benchmark #" in body and "ASSISTANT_MODEL=haiku" in body and "flaky" in body
        st, body, _ = get(host + "/gh/acme/relay/benchmarks", cookie)
        assert st == 200 and "Benchmarks" in body
    finally:
        httpd.shutdown()
