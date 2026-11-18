"""Accounts and access: GitHub OIDC sign-in, projects created by their first analysis, private projects, sandboxed uploads."""
import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from assay.access import AuthError, GitHubOIDC, rsa_sha256_verify
from assay.ci import Server
from assay.server import Store, make_handler

# a throwaway 2048-bit test key (public exponent 65537), never used for anything else
N = 0xcd8a54fb203f59aca06a32d968164a5908c2129b66f89fcf1d69b4464f94119e5f4f80d1c32b24522c7539a2573422a233619ec3d05d4b1221c2b9acfd7139adb527a9b6dd2b29fbe83cc514da3330d61e0a954fba0027655fc04a9054b75a4e627eca7e0fd0e47276e54d8022adbcca5901c1d537315dd1cccb4561966caf2341bdabe7f10d142c336c624bb8644de93063f8996b403ff4b7d9bb8b3c413ef5fb967a6e5ba683244a1d46aa17627b2f862c8ab8b414ef26d3c15340658a12877a27cdc04b87c804255565b952b30dfb45f0a1fce1e44e44df408a03916593d9f8f66f146cd91c1b8dfe069765bd3f42c15e9f765087a1073cfde15720dd96d5
D = 0x3c105a201ebed4864144778297cbb7c347ef6abf95fa3ebd2823bd2634d818c3f6a7458646e9e30dce671d460704b9c03b92a1f22800335163a3435e3d9bea568c534061ab45135a457608c3bcb5ebe60e565a66b77c9706a5520c981be18ed6cc2ab022e19d523ceee8b85aa307b15d52605493e9b297ef98d0a8004a148da311b66b118b7d2f1b3a2648400a49bb0440dfa60e022f79456bccbd278e34bc8410b43f968eef86016dc290bba80d97318dcadedcb2b996cf6c612ad95b4ef8ccd32dc2acf9623d9849c619f5bb8ba60b95ae95548deb8f439c753a723657dab92376b31b0a199a42962b101bde6966a3f610bfa81b9ec01e0b2409668810cc5b
E = 65537
AUD = "http://assay.test"
INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sign(msg: bytes) -> bytes:
    k = (N.bit_length() + 7) // 8
    t = INFO + hashlib.sha256(msg).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), D, N).to_bytes(k, "big")


def token(repo="acme/shop", visibility="private", aud=AUD, iss="https://token.actions.githubusercontent.com", exp=None, kid="k1", repo_id=None):
    h = b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}).encode())
    p = b64(json.dumps({"iss": iss, "aud": aud, "exp": exp or time.time() + 300, "nbf": time.time() - 5, "repository": repo,
                        "repository_owner": repo.split("/")[0], "repository_visibility": visibility, "run_id": "77",
                        "repository_id": repo_id or str(abs(hash(repo)) % 10 ** 9)}).encode())
    return f"{h}.{p}.{b64(sign(f'{h}.{p}'.encode()))}"


JWKS = {"keys": [{"kty": "RSA", "kid": "k1", "n": b64(N.to_bytes(256, "big")), "e": b64(E.to_bytes(3, "big"))}]}


def oidc():
    return GitHubOIDC([AUD], fetch=lambda url: JWKS)


def test_rsa_signature_check():
    msg = b"header.payload"
    assert rsa_sha256_verify(N, E, msg, sign(msg))
    assert not rsa_sha256_verify(N, E, b"header.payloaD", sign(msg))
    assert not rsa_sha256_verify(N, E, msg, sign(msg)[:-1])


def test_oidc_accepts_github_tokens_for_this_server_only():
    v = oidc()
    assert v.verify(token())["repository"] == "acme/shop"
    h, p, s = token().split(".")
    forged = b64(json.dumps({**json.loads(base64.urlsafe_b64decode(p + "==")), "repository": "victim/app"}).encode())
    for bad in (f"{h}.{forged}.{s}", token(aud="https://other.example"), token(exp=time.time() - 3600),
                token(iss="https://evil.example"), token(kid="nope"), "not.a.jwt", "x"):
        with pytest.raises(AuthError):
            v.verify(bad)


def _analysis():
    return {"run": {"id": "r", "app": "shop", "started": "2026-11-12T10:00:00+00:00"}, "ci": {}, "app": {"name": "shop", "title": "Shop"},
            "baseline": None, "gate": {"status": "PASSED", "conditions": []},
            "summary": {"cases": 1, "PASS": 1, "FAIL": 0, "INCONCLUSIVE": 0, "pass_rate": 1, "issues": 0, "new_issues": 0,
                        "by_severity": {}, "pages_scanned": 0, "routes_scanned": 0},
            "ratings": {}, "ratings_new": {}, "dimensions": {}, "cases": [], "issues": [], "pages": [], "coverage": None,
            "changes": {}, "judge": {}}


@pytest.fixture()
def site(tmp_path):
    store = Store(tmp_path / "data")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, "admintok", AUD, {}, oidc()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    run = tmp_path / "run"
    (run / "cases").mkdir(parents=True)
    (run / "analysis.json").write_text(json.dumps(_analysis()))
    (run / "report.html").write_text("<script>fetch('/projects')</script>")
    yield f"http://127.0.0.1:{httpd.server_address[1]}", run
    httpd.shutdown()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def get(url, cookie=None):
    req = urllib.request.Request(url, headers={"Cookie": cookie} if cookie else {})
    try:
        with urllib.request.build_opener(NoRedirect).open(req) as r:
            return r.status, r.read().decode(errors="replace"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace"), e.headers


def admin_login(host, user, pw):
    """Sign in on the form as a browser does: the page sets the form token, the post sends it back."""
    with urllib.request.urlopen(host + "/login") as r:
        csrf = next(c.split(";")[0] for c in r.headers.get_all("Set-Cookie") if c.startswith("assay_csrf="))
    req = urllib.request.Request(host + "/login", data=f"email={user}&password={pw}&csrf={csrf.split('=', 1)[1]}".encode(),
                                 method="POST", headers={"Cookie": csrf})
    try:
        with urllib.request.build_opener(NoRedirect).open(req) as r:
            return r.status, None, None
    except urllib.error.HTTPError as e:
        cookie = next((c.split(";")[0] for c in e.headers.get_all("Set-Cookie") or [] if c.startswith("assay_admin=")), None)
        return e.code, e.headers.get("Location"), cookie


def test_first_analysis_creates_account_and_private_project(site):
    host, run = site
    res = Server(host, token("acme/shop", "private")).upload(run, {"project": "someone-else", "title": "Shop", "branch": "main", "sha": "a" * 40})
    assert res["project"] == "gh/acme/shop" and res["created_project"]           # the token decides the project
    assert res["account_url"].endswith("/gh/acme") and "k=" in res["url"]
    path = res["url"][len(AUD):]                    # links use the server's public address
    clean = path.split("&k=")[0]
    # strangers see nothing: not the project, not its files, not the list, not the API
    for p in (clean, f"/a/{res['id']}/report.html", "/api/projects/gh/acme/shop/analyses/latest?branch=main"):
        assert get(host + p)[0] == 404, p
    assert "Shop" not in get(host + "/projects")[1] and "Shop" not in get(host + "/gh/acme")[1]
    # the link CI posted: the key moves into a cookie, the address loses it
    st, _, hd = get(host + path)
    assert st == 302 and "k=" not in hd["Location"]
    cookie = hd["Set-Cookie"].split(";")[0]
    assert get(host + clean, cookie)[0] == 200
    assert "Shop" in get(host + "/gh/acme", cookie)[1]
    st, body, hd = get(host + f"/a/{res['id']}/report.html", cookie)
    assert st == 200 and "sandbox" in hd["Content-Security-Policy"]          # uploads never run with the site's origin
    assert get(host + clean.replace("shop", "other"), cookie)[0] == 404
    # the job itself reads its reference with its token; another repository's token does not
    assert Server(host, token("acme/shop")).reference("gh/acme/shop", "main")["server"]["id"] == res["id"]
    assert Server(host, token("evil/app")).reference("gh/acme/shop", "main") is None


def test_public_repositories_are_public_and_tokens_are_checked(site):
    host, run = site
    res = Server(host, token("acme/site", "public")).upload(run, {"title": "Site", "branch": "main"})
    assert "k=" not in res["url"] and get(host + res["url"][len(AUD):])[0] == 200
    assert "Site" in get(host + "/projects")[1] and "Site" not in get(host + "/")[1]     # the landing lists projects only to someone signed in
    with pytest.raises(RuntimeError, match="401"):
        Server(host, token(aud="https://other.example")).upload(run, {"title": "X"})
    with pytest.raises(RuntimeError, match="401"):
        Server(host, None).upload(run, {"project": "x"})
    for p in ("/", "/setup", "/login", "/docs", "/gh/nobody"):
        assert get(host + p)[0] == (404 if p == "/gh/nobody" else 200), p


def test_admin_sees_everything(site):
    host, run = site
    Server(host, token("acme/shop", "private")).upload(run, {"title": "Shop", "branch": "main"})
    cookie = admin_login(host, "admin", "admintok")[2]
    assert "Shop" in get(host + "/projects", cookie)[1]
    assert get(host + "/gh/acme/shop", cookie)[0] == 200


def _upload_json(host, run, a, tok=None, meta=None):
    (run / "analysis.json").write_text(json.dumps(a))
    return Server(host, tok or token("evil/app", "public")).upload(run, meta or {"title": "Evil", "branch": "main"})


def test_uploads_cannot_inject_markup_or_reach_other_projects(site):
    host, run = site
    victim = Server(host, token("acme/shop", "private")).upload(run, {"title": "Shop", "branch": "secret-branch"})
    bad = _analysis()
    bad["summary"]["pages_scanned"] = "<img src=x onerror=alert(1)>"
    with pytest.raises(RuntimeError, match="400"):
        _upload_json(host, run, bad)
    bad = _analysis()
    bad["cases"] = [{"case_id": "../../1/cases/login", "title": "x", "verdict": "PASS", "steps": []}]
    with pytest.raises(RuntimeError, match="400"):
        _upload_json(host, run, bad)
    bad = _analysis()
    bad["gate"]["status"] = "<b>PASSED</b>"
    with pytest.raises(RuntimeError, match="400"):
        _upload_json(host, run, bad)
    # a reference to another tenant's analysis is dropped, so its branch never shows up here
    res = _upload_json(host, run, _analysis(), meta={"title": "Evil", "branch": "main", "reference_id": victim["id"]})
    assert "secret-branch" not in get(host + res["url"][len(AUD):].split("&k=")[0])[1]
    # pages forbid inline script, uploads run sandboxed
    st, _, hd = get(host + "/")
    assert "script-src 'self'" in hd["Content-Security-Policy"]


def test_private_projects_do_not_leak_existence(site):
    host, run = site
    Server(host, token("acme/shop", "private")).upload(run, {"title": "Shop", "branch": "main"})
    for path in ("/api/projects/gh/acme/shop", "/api/projects/gh/acme/none"):
        st, body, _ = get(host + path)
        assert st == 404 and "no such project" in body, path
    assert get(host + "/gh/acme")[0] == get(host + "/gh/nobody")[0] == 404
    assert get(host + "/gh/acme/shop?k=%C3%A9")[0] == get(host + "/gh/acme/none?k=%C3%A9")[0] == 302
    st, _, hd = get(host + "/projects/gh/x%0d%0aSet-Cookie:%20pwn=1")
    assert "pwn" not in (hd.get("Set-Cookie") or "")


def test_a_renamed_repository_cannot_take_over_the_old_project(site):
    host, run = site
    Server(host, token("acme/shop", "private", repo_id="111")).upload(run, {"title": "Shop", "branch": "main"})
    with pytest.raises(RuntimeError, match="403"):
        Server(host, token("acme/shop", "private", repo_id="999")).upload(run, {"title": "Shop", "branch": "main"})
    assert Server(host, token("acme/shop", repo_id="999")).reference("gh/acme/shop", "main") is None


def test_real_analyses_pass_the_upload_check():
    """The checks must never refuse what the engine itself writes: HTTP-only journeys have no screenshot ('')."""
    from assay.uploads import check_analysis
    a = _analysis()
    a["cases"] = [{"case_id": "admin.forbidden_for_members", "title": "Members cannot read the queue", "verdict": "PASS",
                   "screenshot": "", "steps": [{"i": 0, "action": "http", "ms": 12, "screenshot": None}]},
                  {"case_id": "post.comment", "title": "Comment", "verdict": "FAIL", "severity": "major",
                   "screenshot": "cases/post.comment/final.png", "steps": [{"i": 0, "ms": 5, "screenshot": "step-00.png"}]}]
    out = check_analysis(a)
    assert out["cases"][0]["screenshot"] is None and out["cases"][1]["steps"][0]["screenshot"] == "step-00.png"



def test_admin_signs_in_with_login_and_password(tmp_path):
    from assay.access import check_password, hash_password
    h = hash_password("u-test-pass")
    assert check_password("u-test-pass", h) and not check_password("wrong", h) and not check_password("x", "garbage")
    store = Store(tmp_path / "data")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, "admintok", AUD, {"admin": {"user": "admin", "password": h}}, oidc()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert admin_login(host, "admin", "u-test-pass")[1] == "/projects"
        assert admin_login(host, "admin", "wrong")[0] == 401
        assert admin_login(host, "root", "u-test-pass")[0] == 401
        req = urllib.request.Request(host + "/login", data=b"email=admin&password=u-test-pass", method="POST")
        try:
            urllib.request.build_opener(NoRedirect).open(req)
        except urllib.error.HTTPError as e:
            assert e.code == 403                     # no form token: refused, whatever the password
    finally:
        httpd.shutdown()
