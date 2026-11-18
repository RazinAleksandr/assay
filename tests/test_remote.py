"""Testing an app that is already deployed: the address, the test accounts and the database URL come from the environment
(CI secrets), state is read remotely (the postgres probe through psql), and doctor names what is missing."""
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from assay.cli import main
from assay.config import Profile, load_profile
from assay.doctor import doctor, env_lines
from assay.driver import StepError
from assay.probes import ProbeError, masked, read_probe, snapshot
from assay.runner import account_values, app_running, login

VARS = ("ASSAY_BASE_URL", "QA_USER", "QA_PASSWORD", "STAND_KEY", "DATABASE_URL", "PGCONNECT_TIMEOUT")
STAGING = """
name: staging
base_url: "{env.ASSAY_BASE_URL}"
accounts: {qa: {username: "{env.QA_USER}", password: "{env.QA_PASSWORD}"}}
auth:
  http: {method: POST, path: /api/login, headers: {X-Stand-Key: "{env.STAND_KEY}"},
         json: {email: "{account.username}", password: "{account.password}"}}
probes:
  comments: {postgres: "{env.DATABASE_URL}", sql: "select id, body from comments where author = 'qa' order by id;"}
"""
DB = "postgres://qa_ro:hunter22@db.example.com:5432/app?sslmode=require"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in VARS:
        monkeypatch.delenv(v, raising=False)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "assay.yaml").write_text(STAGING)
    return tmp_path


# ---- the address --------------------------------------------------------------------------------------------------------

def test_base_url_comes_from_the_environment(project, monkeypatch):
    monkeypatch.setenv("ASSAY_BASE_URL", "https://pr-42.preview.example.com")
    prof = load_profile(project)
    assert prof.base_url == "https://pr-42.preview.example.com"
    assert prof.url("/feed") == "https://pr-42.preview.example.com/feed"


def test_an_unset_base_url_variable_is_a_configuration_error(project, monkeypatch, capsys):
    with pytest.raises(ValueError, match=r"base_url needs the environment variable ASSAY_BASE_URL, which is not set"):
        load_profile(project)
    assert "{env." in load_profile(project, strict=False).base_url        # doctor loads it anyway to list everything
    assert main(["run", str(project)]) == 3 and main(["ci", str(project)]) == 3
    assert "ASSAY_BASE_URL" in capsys.readouterr().err
    monkeypatch.setenv("ASSAY_BASE_URL", "")                          # GitHub's value for a variable nobody defined
    with pytest.raises(ValueError, match=r"base_url is '', not a URL: is ASSAY_BASE_URL empty\?"):
        load_profile(project)


def test_the_ready_url_is_needed_up_front_too(tmp_path):
    (tmp_path / "assay.yaml").write_text('base_url: https://staging.example.com\napp: {ready: {url: "{env.HEALTH_URL}"}}\n')
    with pytest.raises(ValueError, match="app.ready.url needs the environment variable HEALTH_URL"):
        load_profile(tmp_path)


def test_a_deployed_app_gets_time_to_wake_up(tmp_path, monkeypatch):
    from assay import runner
    answers = iter([False, False, True])                          # a cold start: the third check answers
    monkeypatch.setattr(runner.AppProcess, "is_up", lambda self: next(answers))
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    prof = Profile(name="t", root=tmp_path, base_url="https://staging.example.com")
    with app_running(prof, tmp_path) as down:
        assert down is None
    local = Profile(name="t", root=tmp_path, base_url="http://127.0.0.1:1", app={"start": "npm start"})
    monkeypatch.setattr(runner.AppProcess, "is_up", lambda self: False)
    with app_running(local, tmp_path, start=False) as down:      # --no-start on a local app: no waiting
        assert down == "app is not reachable at http://127.0.0.1:1"


# ---- test accounts from secrets ------------------------------------------------------------------------------------------

def test_account_values_expand_at_login_not_at_load(project, monkeypatch):
    monkeypatch.setenv("ASSAY_BASE_URL", "https://staging.example.com")
    prof = load_profile(project)                                  # the job exports the secrets after this
    monkeypatch.setenv("QA_USER", "qa@example.com")
    monkeypatch.setenv("QA_PASSWORD", "s3cret")
    monkeypatch.setenv("STAND_KEY", "k1")
    assert account_values(prof, "qa") == {"name": "qa", "username": "qa@example.com", "password": "s3cret"}
    sent = []
    ctx = SimpleNamespace(request=SimpleNamespace(fetch=lambda url, **kw: sent.append((url, kw)) or SimpleNamespace(status=200)))
    assert login(prof, ctx, None, "qa") == {}
    url, kw = sent[0]
    assert url == "https://staging.example.com/api/login"
    assert kw["data"] == {"email": "qa@example.com", "password": "s3cret"} and kw["headers"] == {"X-Stand-Key": "k1"}


def test_a_missing_secret_is_the_environments_fault(project, monkeypatch):
    monkeypatch.setenv("ASSAY_BASE_URL", "https://staging.example.com")
    monkeypatch.setenv("QA_USER", "qa@example.com")
    prof = load_profile(project)
    with pytest.raises(StepError) as e:
        login(prof, None, None, "qa")                             # stops before any request
    assert e.value.kind == "login_failed"                         # → INCONCLUSIVE, never a product failure
    assert str(e.value) == "login as qa: QA_PASSWORD, STAND_KEY not set in the environment"
    with pytest.raises(StepError, match="unknown account 'mila'"):
        account_values(prof, "mila")


# ---- the postgres probe ---------------------------------------------------------------------------------------------------

def _psql(monkeypatch, stdout="[]", returncode=0, stderr="", exc=None):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        if exc:
            raise exc
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _prof(tmp_path, **probe):
    return Profile(name="t", root=tmp_path, base_url="https://staging.example.com",
                   probes={"comments": {"postgres": "{env.DATABASE_URL}", "sql": "select id, body from comments", **probe}})


def test_postgres_probe_runs_psql_without_a_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB)
    calls = _psql(monkeypatch, stdout='[{"id":1,"body":"Lovely light","score":null}]\n')
    prof = _prof(tmp_path, sql="select id, body, score from comments where author = 'qa' order by id ; ")
    assert snapshot(prof) == ({"comments": [{"id": 1, "body": "Lovely light", "score": None}]}, {})
    cmd, kw = calls[0]
    assert cmd[:-1] == ["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", DB.replace(":hunter22", ""), "-c"]
    assert kw["env"]["PGPASSWORD"] == "hunter22" and "hunter22" not in " ".join(cmd)      # not on the command line
    assert cmd[-1] == ("select coalesce(json_agg(t), '[]'::json) from (\n"
                       "select id, body, score from comments where author = 'qa' order by id\n) t")   # no `;` inside
    assert not kw.get("shell") and kw["env"]["PGCONNECT_TIMEOUT"] == "10" and kw["env"]["DATABASE_URL"] == DB


def test_postgres_probe_shapes_rows_like_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB)
    _psql(monkeypatch, stdout='[{"n":3,"latest":"hi"}]')
    spec = {"postgres": "{env.DATABASE_URL}", "sql": "with c as (select 1) select count(*) n, max(body) latest from comments"}
    assert read_probe("c", {**spec, "as": "value"}, _prof(tmp_path)) == 3
    assert read_probe("c", {**spec, "as": "row"}, _prof(tmp_path)) == {"n": 3, "latest": "hi"}
    _psql(monkeypatch, stdout="[]\n")
    assert read_probe("c", {**spec, "as": "row"}, _prof(tmp_path)) is None
    assert read_probe("c", spec, _prof(tmp_path)) == []


def test_postgres_probe_never_shows_the_password(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB)
    _psql(monkeypatch, returncode=2, stderr=f'psql: error: missing "=" after "{DB}" in connection info string\n'
                                            'FATAL:  password authentication failed for user "qa_ro" (hunter22)')
    state, errors = snapshot(_prof(tmp_path))
    assert state == {} and "hunter22" not in errors["comments"]
    assert "postgres://qa_ro:***@db.example.com:5432/app" in errors["comments"]
    assert "password authentication failed" in errors["comments"]
    _psql(monkeypatch, exc=subprocess.TimeoutExpired(["psql", "-d", DB], 60))
    with pytest.raises(ProbeError) as e:
        read_probe("comments", _prof(tmp_path).probes["comments"], _prof(tmp_path))
    assert "hunter22" not in str(e.value) and "did not answer" in str(e.value)
    assert masked("host=db user=qa password=hunter22 dbname=app") == "host=db user=qa password=*** dbname=app"
    assert masked("postgres://u:p%40ss%3Aword@h/db", "postgres://u:p%40ss%3Aword@h/db") == "postgres://u:***@h/db"
    assert masked("host=db user=u password='pa ss word' dbname=app") == "host=db user=u password=*** dbname=app"


def test_postgres_probe_needs_psql_a_url_and_a_select(tmp_path, monkeypatch):
    calls = _psql(monkeypatch, exc=FileNotFoundError("psql"))
    with pytest.raises(ProbeError, match="DATABASE_URL not set in the environment"):
        read_probe("comments", _prof(tmp_path).probes["comments"], _prof(tmp_path))
    monkeypatch.setenv("DATABASE_URL", DB)
    with pytest.raises(ProbeError, match=r"apt-get install postgresql-client .* brew install libpq"):
        read_probe("comments", _prof(tmp_path).probes["comments"], _prof(tmp_path))
    calls.clear()
    for sql in ("delete from comments", "update comments set body = '' returning *", "  insert into x values (1)"):
        with pytest.raises(ProbeError, match="only SELECT queries are allowed"):
            read_probe("comments", {"postgres": DB, "sql": sql}, _prof(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "")
    with pytest.raises(ProbeError, match="the postgres URL is empty"):
        read_probe("comments", _prof(tmp_path).probes["comments"], _prof(tmp_path))
    assert calls == []                                            # nothing reached psql


def test_http_probe_reaches_a_test_api_with_a_token(tmp_path, monkeypatch):
    seen = []
    resp = SimpleNamespace(status=200, json=lambda: {"items": [{"id": 7}]})
    ctx = SimpleNamespace(request=SimpleNamespace(fetch=lambda url, **kw: seen.append((url, kw)) or resp))
    spec = {"http": {"path": "/api/test/orders?owner=qa", "headers": {"Authorization": "Bearer {env.STAND_KEY}"}},
            "select": "items"}
    with pytest.raises(ProbeError, match="STAND_KEY not set"):
        read_probe("orders", spec, _prof(tmp_path), context=ctx)
    monkeypatch.setenv("STAND_KEY", "t0k")
    assert read_probe("orders", spec, _prof(tmp_path), context=ctx) == [{"id": 7}]
    assert seen == [("https://staging.example.com/api/test/orders?owner=qa",
                     {"method": "GET", "timeout": 15000, "headers": {"Authorization": "Bearer t0k"}})]


# ---- doctor --------------------------------------------------------------------------------------------------------------

def test_doctor_names_each_missing_variable_and_where_it_is_used(project, monkeypatch):
    monkeypatch.setenv("ASSAY_BASE_URL", "https://staging.example.com")
    monkeypatch.setenv("QA_USER", "qa@example.com")
    assert env_lines(load_profile(project)) == [
        ("QA_PASSWORD is not set", "used by accounts.qa.password"),
        ("STAND_KEY is not set", "used by auth.http.headers.X-Stand-Key"),
        ("DATABASE_URL is not set", "used by probes.comments.postgres")]
    (project / "assay.yaml").write_text(STAGING + 'env: {DATABASE_URL: "postgres://localhost/app", STAND_KEY: k}\n')
    assert [w for w, _ in env_lines(load_profile(project))] == ["QA_PASSWORD is not set"]   # the profile's env: counts


def test_doctor_stops_without_an_address(project):
    out = []
    assert doctor(load_profile(project, strict=False), log=out.append) == 3
    assert out[1:3] == ["  ✓ profile and 0 cases parse", "  ✗ ASSAY_BASE_URL is not set — used by base_url"]
    assert out[-1] == "  fix the ✗ items above before running" and not any("app address" in x for x in out)


def test_browser_headers_reach_every_request_and_name_a_missing_secret(tmp_path, monkeypatch):
    from assay.config import load_profile
    from assay.runner import browser_headers
    (tmp_path / "assay.yaml").write_text(
        "name: x\nbase_url: http://127.0.0.1:1\n"
        "browser: {headers: {x-vercel-protection-bypass: '{env.BYPASS}', x-vercel-set-bypass-cookie: 'true'}}\n")
    monkeypatch.delenv("BYPASS", raising=False)
    prof = load_profile(tmp_path)
    with pytest.raises(ValueError, match="BYPASS"):
        browser_headers(prof)
    monkeypatch.setenv("BYPASS", "s3cret")
    assert browser_headers(prof) == {"x-vercel-protection-bypass": "s3cret", "x-vercel-set-bypass-cookie": "true"}


def _chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


@pytest.mark.skipif(not _chromium(), reason="Chromium is not installed (assay browsers)")
def test_browser_headers_go_to_the_app_only_and_never_replace_its_own(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from assay.config import load_profile
    from assay.runner import Browsers
    seen: dict[str, dict] = {}

    def server(body: bytes, ctype: str):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                seen[f"{self.server.server_port}{self.path}"] = {k.lower(): v for k, v in self.headers.items()}
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            log_message = lambda *a: None
        s = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        return s
    other = server(b"GIF89a", "image/gif")
    app = server(b"", "text/html")
    page_html = (f'<img src="http://127.0.0.1:{other.server_port}/pixel.gif">'
                 "<script>fetch('/api', {headers: {Authorization: 'Bearer app-jwt'}})</script>").encode()
    app.RequestHandlerClass.do_GET.__globals__  # keep flake8 quiet about the closure
    orig = app.RequestHandlerClass.do_GET

    def do_get(self):
        if self.path == "/":
            seen[f"{app.server_port}/"] = {k.lower(): v for k, v in self.headers.items()}
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(page_html)
        else:
            orig(self)
    app.RequestHandlerClass.do_GET = do_get
    monkeypatch.setenv("BYPASS", "s3cret")
    (tmp_path / "assay.yaml").write_text(f"name: x\nbase_url: http://127.0.0.1:{app.server_port}\n"
                                         "browser: {headers: {x-vercel-protection-bypass: '{env.BYPASS}', Authorization: 'Basic eDp5'}}\n")
    prof = load_profile(tmp_path)
    try:
        with Browsers(prof) as b:
            ctx, _ = b.new_context(None)
            page = ctx.new_page()
            page.goto("/", wait_until="networkidle")
            ctx.close()
    finally:
        app.shutdown(), other.shutdown()
    assert seen[f"{app.server_port}/"]["x-vercel-protection-bypass"] == "s3cret"
    assert seen[f"{app.server_port}/api"]["authorization"] == "Bearer app-jwt"          # the page's own header wins
    assert "x-vercel-protection-bypass" not in seen[f"{other.server_port}/pixel.gif"]  # nothing for another origin
