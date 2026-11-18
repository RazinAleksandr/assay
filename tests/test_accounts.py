"""Accounts as people expect them: sign up, sign in, sign out; Google and GitHub; a password reset by email; an account
page; private projects that stay yours; and the attacks a sign-in invites (CSRF, open redirects, guessing, takeover)."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from assay import accounts as A
from assay import oauth
from assay.server import Store, make_handler
from test_access import JWKS, b64, sign

CONFIG = {"auth": {"google": {"client_id": "gid.apps.googleusercontent.com", "client_secret": "gs"},
                   "github": {"client_id": "hid", "client_secret": "hs"}},
          "smtp": {"host": "smtp.test", "from": "Assay <no-reply@assay.test>"}, "admins": ["boss@acme.test"]}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Browser:
    """A cookie jar and no automatic redirects: each response is looked at."""

    def __init__(self, host):
        self.host, self.jar = host, {}

    def _take(self, headers):
        for c in headers.get_all("Set-Cookie") or []:
            name, _, rest = c.partition("=")
            value = rest.split(";")[0]
            if "Max-Age=0" in c:
                self.jar.pop(name, None)
            else:
                self.jar[name] = value

    def go(self, path, data=None, headers=None):
        h = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.jar.items()), **(headers or {})}
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(self.host + path, data=body, headers=h, method="POST" if data is not None else "GET")
        try:
            with urllib.request.build_opener(NoRedirect).open(req) as r:
                self._take(r.headers)
                return r.status, r.read().decode(), r.headers.get("Location")
        except urllib.error.HTTPError as e:
            self._take(e.headers)
            return e.code, e.read().decode(errors="replace"), e.headers.get("Location")

    def post(self, path, **fields):
        if "assay_csrf" not in self.jar:
            self.go("/login")
        return self.go(path, {"csrf": self.jar["assay_csrf"], **fields})


@pytest.fixture()
def site(tmp_path, monkeypatch):
    mail = []
    monkeypatch.setattr(A, "send_mail", lambda smtp, to, subject, text: mail.append((to, subject, text)) or True)
    monkeypatch.setattr(A, "PASSWORD_ROUNDS", 1000)             # fast tests; the real count is checked below
    monkeypatch.setattr(oauth.Http, "post_form", oauth.Http.post_form)      # restored after each test
    monkeypatch.setattr(oauth.Http, "get_json", oauth.Http.get_json)
    store = Store(tmp_path / "data")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, "admintok", None, CONFIG))
    host = f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.RequestHandlerClass = make_handler(store, "admintok", host, CONFIG)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield host, store, mail
    httpd.shutdown()


def signup(b, name="Mila", email="mila@acme.test", password="correct horse battery"):
    return b.post("/signup", name=name, email=email, password=password)


def test_sign_up_sign_out_sign_in(site):
    host, store, mail = site
    b = Browser(host)
    st, body, _ = b.go("/signup")
    assert st == 200 and "Continue" not in body and "Sign up with Google" in body and "Sign up with GitHub" in body
    assert 'autocomplete=new-password' in body and "name=csrf" in body
    st, _, loc = signup(b)
    assert st == 303 and loc == "/projects" and "assay_session" in b.jar
    assert mail and "Confirm your email" in mail[-1][1] and "/verify?t=" in mail[-1][2]          # a verification link
    st, body, _ = b.go("/account")
    assert st == 200 and "mila@acme.test" in body and "not verified" in body
    st, body, _ = b.go("/")
    assert "Account menu, Mila" in body and "Sign up" not in body.split("</header>")[0]
    b.post("/logout")
    assert "assay_session" not in b.jar and b.go("/account")[2] == "/login?next=%2Faccount"
    st, body, _ = b.post("/login", email="mila@acme.test", password="wrong password!")
    assert st == 401 and "do not match" in body
    st, _, loc = b.post("/login", email="MILA@acme.test ", password="correct horse battery", next="/account")
    assert st == 303 and loc == "/account"
    st, body, _ = signup(Browser(host))
    assert st == 409 and "exists" in body
    verify = mail[0][2].split("/verify?t=")[1].split()[0]
    assert "Confirm" in b.go("/verify?t=" + verify)[1] and not A.by_email(store, "mila@acme.test")["email_verified"]   # a GET changes nothing
    assert b.post("/account", do="confirm", t=verify)[2] == "/account?info=verified" and A.by_email(store, "mila@acme.test")["email_verified"]


def test_forms_need_their_token_and_this_site(site):
    host, _, _ = site
    b = Browser(host)
    b.go("/login")
    assert b.go("/signup", {"name": "X", "email": "x@acme.test", "password": "correct horse battery"})[0] == 403
    assert b.go("/signup", {"csrf": "forged", "name": "X", "email": "x@acme.test", "password": "correct horse battery"})[0] == 403
    st = b.go("/signup", {"csrf": b.jar["assay_csrf"], "name": "X", "email": "x@acme.test", "password": "correct horse battery"},
              {"Sec-Fetch-Site": "cross-site"})[0]
    assert st == 403
    assert signup(b, password="short")[0] == 400                       # at least 10 characters


def test_no_open_redirect_after_sign_in(site):
    host, _, _ = site
    for nxt in ("//evil.test/x", "https://evil.test", "/\\evil.test", "javascript:alert(1)", "/\t/evil.test", "/ /evil.test"):
        b = Browser(host)
        st, _, loc = b.post("/signup", name="M", email=f"m{abs(hash(nxt))}@acme.test", password="correct horse battery", next=nxt)
        assert st == 303 and loc == "/projects", nxt


def test_guessing_passwords_is_slowed(site):
    host, _, _ = site
    b = Browser(host)
    signup(b)
    codes = [b.post("/login", email="mila@acme.test", password=f"guess number {i}")[0] for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[-1] == 429
    assert A.PASSWORD_ROUNDS == 1000 and A.MIN_PASSWORD == 10


def test_password_reset_by_email(site):
    host, store, mail = site
    signup(Browser(host))
    b = Browser(host)
    st, body, _ = b.post("/forgot", email="mila@acme.test")
    assert st == 200 and "Check your email" in body
    st, body, _ = b.post("/forgot", email="nobody@acme.test")
    assert st == 200 and "Check your email" in body and len([m for m in mail if "Reset" in m[1]]) == 1   # no account leak
    t = [m for m in mail if "Reset" in m[1]][0][2].split("/reset?t=")[1].split()[0]
    assert "Set a new password" in b.go("/reset?t=" + t)[1]
    st, _, loc = b.post("/reset", t=t, password="a brand new passphrase")
    assert st == 303 and "assay_session" in b.jar
    assert b.post("/reset", t=t, password="another new passphrase")[0] == 400           # once
    assert A.authenticate(store, "mila@acme.test", "a brand new passphrase")


def google_flow(b, sub="g-1", email="mila@acme.test", verified=True, aud="gid.apps.googleusercontent.com", link=False, hd="acme.test"):
    st, _, loc = b.post("/account", do="connect", provider="google") if link else b.go("/auth/google")
    assert st in (302, 303) and loc.startswith(oauth.GOOGLE["authorize"])
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))
    assert q["code_challenge_method"] == "S256" and q["redirect_uri"].endswith("/auth/google/callback")

    def id_token():
        h = b64(json.dumps({"alg": "RS256", "kid": "k1"}).encode())
        p = b64(json.dumps({"iss": "https://accounts.google.com", "aud": aud, "sub": sub, "email": email, "email_verified": verified,
                            "name": "Mila Chen", "nonce": q["nonce"], "exp": time.time() + 300, **({"hd": hd} if hd else {})}).encode())
        return f"{h}.{p}.{b64(sign(f'{h}.{p}'.encode()))}"
    oauth.Http.post_form = staticmethod(lambda url, data: {"id_token": id_token()} if data.get("code_verifier") else {})
    oauth.Http.get_json = staticmethod(lambda url, token=None: JWKS)
    oauth.GOOGLE_KEYS.keys, oauth.GOOGLE_KEYS.fetched = {}, 0
    return b.go(f"/auth/google/callback?state={q['state']}&code=c0de")


def test_sign_in_with_google(site):
    host, store, _ = site
    b = Browser(host)
    st, _, loc = google_flow(b)
    assert st == 303 and loc == "/projects" and "assay_session" in b.jar
    u = A.by_email(store, "mila@acme.test")
    assert u["email_verified"] and not u["password"] and "google" in A.identities(store, u["id"])
    b2 = Browser(host)                                     # the same Google account signs in to the same Assay account
    google_flow(b2)
    assert A.session_user(store, b2.jar["assay_session"])["id"] == u["id"]
    b3 = Browser(host)                                     # a state that does not match: refused
    b3.go("/auth/google")
    assert b3.go("/auth/google/callback?state=other&code=x")[2] == "/login?error=state"
    st, _, loc = google_flow(Browser(host), sub="g-2", email="x@acme.test", aud="someone-else")
    assert loc == "/login?error=provider"                  # a token for another client


def test_a_squatted_email_is_reclaimed_by_its_owner(site):
    """Someone registers the victim's address with a password; the victim later signs in with Google."""
    host, store, _ = site
    attacker = Browser(host)
    signup(attacker, email="mila@acme.test", password="attacker passphrase")
    github_flow(attacker, gid=666, login="evil", email="evil@evil.test", link=True)      # their own way back in
    google_flow(Browser(host))
    u = A.by_email(store, "mila@acme.test")
    assert u["email_verified"] and not u["password"]                       # the squatter's password is gone
    assert A.session_user(store, attacker.jar["assay_session"]) is None    # and their session
    assert not A.authenticate(store, "mila@acme.test", "attacker passphrase")
    assert "github" not in A.identities(store, u["id"])                    # and their GitHub
    comeback = Browser(host)
    github_flow(comeback, gid=666, login="evil", email="evil@evil.test")
    assert A.session_user(store, comeback.jar["assay_session"])["id"] != u["id"]


def test_google_joins_only_addresses_it_speaks_for(site):
    host, store, _ = site
    signup(Browser(host), email="boss@acme.test")
    A.update(store, A.by_email(store, "boss@acme.test")["id"], email_verified=1)
    b = Browser(host)                       # an old consumer Google account on the boss's address: no Workspace domain
    google_flow(b, sub="g-old", email="boss@acme.test", hd=None)
    assert A.session_user(store, b.jar["assay_session"])["id"] != A.by_email(store, "boss@acme.test")["id"]


def test_a_verify_link_cannot_make_a_squatter_admin(site):
    host, store, mail = site
    attacker = Browser(host)
    signup(attacker, email="boss@acme.test", password="attacker passphrase")
    t = mail[-1][2].split("/verify?t=")[1].split()[0]
    scanner = Browser(host)                                              # a mail scanner, or the boss, opens the link
    assert scanner.go("/verify?t=" + t)[2].startswith("/login?")
    boss = Browser(host)
    signup(boss, email="boss2@acme.test")
    assert boss.post("/account", do="confirm", t=t)[2] == "/account?error=other"
    assert not A.by_email(store, "boss@acme.test")["email_verified"]
    with store.db() as c:
        c.execute("insert into projects(key,title,default_branch,created,owner,visibility,view_key) values"
                  "('gh/acme/x','X','main','2026-11-16','gh/acme','private','kk')")
    assert attacker.go("/gh/acme/x")[0] == 404


def test_requests_that_abuse_the_transport(site):
    host, _, _ = site
    b = Browser(host)
    signup(b)
    codes = [b.go("/login", {"csrf": b.jar["assay_csrf"], "email": "mila@acme.test", "password": f"guess {i} long"},
                  {"X-Forwarded-For": f"10.0.{i}.1"})[0] for i in range(12)]
    assert codes[-1] == 429                                              # a forged client address changes nothing
    req = urllib.request.Request(host + "/login", data=b"x" * 10, method="POST", headers={"Content-Length": "-1"})
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        assert e.code == 413


def github_flow(b, gid=42, login="Mila-Dev", email="mila@acme.test", link=False):
    st, _, loc = b.post("/account", do="connect", provider="github") if link else b.go("/auth/github")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))
    assert loc.startswith(oauth.GITHUB["authorize"]) and q["scope"] == "read:user user:email"     # no org or repo access asked
    oauth.Http.post_form = staticmethod(lambda url, data: {"access_token": "gho_x"})
    oauth.Http.get_json = staticmethod(lambda url, token=None: {"id": gid, "login": login, "name": "Mila"} if url.endswith("/user") else
                                       [{"email": email, "primary": True, "verified": True}])
    return b.go(f"/auth/github/callback?state={q['state']}&code=c")


def test_sign_in_with_github_sees_your_own_repositories(site):
    host, store, _ = site
    with store.db() as c:
        c.execute("insert into projects(key,title,default_branch,created,owner,visibility,view_key,owner_id) values"
                  "('gh/mila-dev/shop','Shop','main','2026-11-16','gh/mila-dev','private','k1','42'),"
                  "('gh/acme/core','Core','main','2026-11-16','gh/acme','private','k2','900')")
    b = Browser(host)
    st, _, loc = github_flow(b)
    assert st == 303 and loc == "/projects"
    assert b.go("/gh/mila-dev/shop")[0] == 200            # their own repository's project
    assert b.go("/gh/acme/core")[0] == 404                 # an organisation's: only from its pull request's link
    assert "Shop" in b.go("/")[1] and "Your projects" in b.go("/")[1]
    squatter = Browser(host)                               # the login changed hands: a new GitHub account named Mila-Dev
    github_flow(squatter, gid=77, email="new@acme.test")
    assert squatter.go("/gh/mila-dev/shop")[0] == 404


def test_a_project_opened_from_its_link_stays_yours(site):
    host, store, _ = site
    with store.db() as c:
        c.execute("insert into projects(key,title,default_branch,created,owner,visibility,view_key) values"
                  "('gh/acme/web','Web','main','2026-11-16','gh/acme','private','secretkey123')")
    b = Browser(host)
    assert "Web" not in b.go("/")[1] and "Sign in" in b.go("/projects")[1]          # a visitor sees no projects on the landing
    b.go("/gh/acme/web?k=secretkey123")                                             # the link from the pull request
    signup(b)                                                                       # then an account
    assert "gh/acme/web" in A.project_keys(store, A.by_email(store, "mila@acme.test")["id"])
    other = Browser(host)                                                           # another device, no key cookie
    other.post("/login", email="mila@acme.test", password="correct horse battery")
    assert other.go("/gh/acme/web")[0] == 200 and "Your projects" in other.go("/")[1]


def test_the_account_page(site):
    host, store, _ = site
    b = Browser(host)
    signup(b)
    uid = A.by_email(store, "mila@acme.test")["id"]
    b.post("/account", do="name", name="Mila Chen")
    assert A.user(store, uid)["name"] == "Mila Chen"
    assert b.post("/account", do="password", current="wrong one!!", new="another passphrase")[2] == "/account?error=current"
    assert b.post("/account", do="password", current="correct horse battery", new="another passphrase")[2] == "/account?info=password"
    google_flow(b, link=True)
    assert "google" in A.identities(store, uid)
    assert b.post("/account", do="disconnect", provider="google")[2] == "/account?info=disconnected"
    A.set_password(store, uid, None)
    google_flow(b, link=True)
    assert b.post("/account", do="disconnect", provider="google")[2] == "/account?error=last"   # the last way in stays
    assert b.post("/account", do="delete", confirm="yes")[2] == "/login?info=deleted" and A.user(store, uid) is None


def test_admins_by_verified_email(site):
    host, store, _ = site
    with store.db() as c:
        c.execute("insert into projects(key,title,default_branch,created,owner,visibility,view_key) values"
                  "('gh/acme/x','X','main','2026-11-16','gh/acme','private','kk')")
    b = Browser(host)
    signup(b, email="boss@acme.test")
    assert b.go("/gh/acme/x")[0] == 404                    # an unverified address is not the admin's
    A.update(store, A.by_email(store, "boss@acme.test")["id"], email_verified=1)
    assert b.go("/gh/acme/x")[0] == 200


def test_real_password_hashing_cost():
    from pathlib import Path
    assert "PASSWORD_ROUNDS = 600_000" in Path(A.__file__).read_text()         # the tests above run it at 1000
