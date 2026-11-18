"""Assay server: the site CI links to (SonarQube's server role).

    assay server --data ./assay-data --port 9000

Projects, their analyses per branch and per pull request, history, the full run report of every analysis with its
screenshots and evidence, a status badge, and the API that `assay ci` uses to fetch the reference analysis and upload.

Accounts work like SonarCloud without the sign-up: the first analysis a GitHub Actions job uploads creates the
owner's account and the project `gh/<owner>/<repo>` (the job signs in with its OIDC token, see assay/access.py).

Storage: SQLite index + one folder per analysis (the uploaded run: analysis.json, reports, screenshots, evidence).
API (JSON):
  GET  /api/health
  GET  /api/projects/{key}                          settings: default branch, gate (if the server defines one)
  GET  /api/projects/{key}/analyses/latest?branch=  reference analysis (analysis.json + server/ci fields) or 404
  POST /api/analyses   Bearer: GitHub OIDC token or the admin token
                       body: run folder .tar.gz, X-Assay-Meta: base64 JSON → {id, url, project, project_url, account_url}
  GET  /api/projects/{key}/badge.svg?branch=        gate badge for a README (private projects: &token=)
Pages (assay/web): /  ·  /projects  ·  /gh/{owner}  ·  /gh/{owner}/{repo}[/issues|journeys|screens|activity|pull-requests|
  branches|quality-gate|setup]  ·  /projects/{legacy key}[/…]  ·  /rules  ·  /quality-gates  ·  /setup  ·  /login
  ·  /a/{id}/ (CI links) and its files (served sandboxed: uploads are untrusted)
Private project pages accept ?k=<view key> once: the key moves into a cookie and the address loses it.
"""
from __future__ import annotations

import base64
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .access import (COOKIE_ADMIN, AuthError, GitHubOIDC, Viewer, check_password, cookie_name, owner_of, parse_cookies,
                     project_key)
from . import __version__
from . import accounts as A
from . import oauth
from .uploads import check_analysis, check_bench, check_meta, unpack
from .web.ui import purl

STATIC = Path(__file__).parent / "web" / "static"
COOKIE_VIEW = "assay_view"                     # human | agent: the level a person last chose
COOKIE_SESSION = "assay_session"               # a signed-in person (accounts.py)
COOKIE_CSRF = "assay_csrf"                     # forms post it back
COOKIE_OAUTH = "assay_oauth"                   # a sign-in with Google or GitHub in flight (signed, 10 minutes)
AUTH_POSTS = {"/login", "/signup", "/logout", "/forgot", "/reset", "/account"}
AUTH_ERRORS = {"provider": "That sign-in did not work. Try again, or use your email and password.",
               "state": "That sign-in took too long or came from another tab. Try again.",
               "cancelled": "The sign-in was cancelled."}
AUTH_INFO = {"deleted": "Your account is deleted."}
ACCOUNT_ERRORS = {"current": "The current password is not right.", "short": "The new password needs at least 10 characters.",
                  "last": "Keep at least one way to sign in: set a password or connect another account first.",
                  "taken": "That account is already connected to another Assay account.",
                  "link": "That link no longer works. Send a new one.",
                  "other": "That link was sent to another account. Sign in to that one to confirm it.",
                  "wait": "Too many tries. Wait a few minutes."}
ACCOUNT_INFO = {"saved": "Saved.", "password": "Password saved. Other devices are signed out.",
                "disconnected": "Disconnected.", "connected": "Connected.", "sessions": "Other devices are signed out.",
                "sent": "A verification email is on its way.", "verified": "Your email is verified."}

SCHEMA = """
create table if not exists projects(key text primary key, title text, default_branch text, created text);
create table if not exists analyses(
  id integer primary key autoincrement, project text not null, title text, branch text, pr text, pr_title text,
  target text, sha text, provider text, ci_url text, created text, status text, cases int, pass int, fail int, inc int,
  issues int, new_issues int, ratings text, reference_id int, fixed int, regressed int);
create index if not exists analyses_project on analyses(project, branch, pr, created);
create table if not exists benchmarks(
  id integer primary key autoincrement, project text not null, branch text, pr text, sha text, ci_url text, created text,
  cases int, attempts int, baseline text, arms text);
create index if not exists benchmarks_project on benchmarks(project, created);
"""
# columns added after the first release: (table, column, type)
MIGRATIONS = [("projects", "owner", "text"), ("projects", "visibility", "text"), ("projects", "view_key", "text"),
              ("projects", "repo_id", "text"), ("projects", "owner_id", "text")]
OWNER_DAILY_LIMIT = 300             # analyses per account per 24 h
MAX_UPLOAD = 300 * 1024 ** 2
KEY_RE = re.compile(r"[\w.-]+|gh/[\w.-]+/[\w.-]+")
# a project path: /gh/<owner>/<repo>[/<page>[/<sub>]] or /projects/<legacy key>[/<page>[/<sub>]]
PROJECT_PATH = re.compile(r"/(?:(gh/[\w.-]+/[\w.-]+)|projects/([\w.-]+))(?:/([\w-]+))?(?:/(.+?))?/?")

def utc_iso(stamp: str | None) -> str:
    """Timestamps in the index are UTC with an offset. A stamp without one is taken as this server's local time
    (runs made before Assay recorded offsets); anything unreadable is kept as given and sorts last."""
    from datetime import datetime, timezone
    if not stamp:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        return datetime.fromisoformat(stamp).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return stamp


def analysis_path(r) -> str:
    """The page of one analysis: its project, scoped to its branch or pull request."""
    scope = {"pr": r["pr"]} if r["pr"] else {"branch": r["branch"] or ""}
    return f"{purl(r['project'])}?{urllib.parse.urlencode({**scope, 'analysis': r['id']})}"


def with_key(url: str, p) -> str:
    """A link for the people CI shows it to: private projects carry their view key (once; the server moves it to a cookie)."""
    if p is None or (p["visibility"] or "public") == "public":
        return url
    return url + ("&" if "?" in url else "?") + "k=" + p["view_key"]


def badge_token(p) -> str:
    return hmac.new((p["view_key"] or "").encode(), b"badge", "sha256").hexdigest()[:20]


class Store:
    def __init__(self, root: Path):
        self.root = root
        (root / "analyses").mkdir(parents=True, exist_ok=True)
        (root / "benchmarks").mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        with self.db() as c:
            c.executescript(SCHEMA)
            c.executescript(A.SCHEMA)
            for table, col, typ in MIGRATIONS:
                if col not in {r["name"] for r in c.execute(f"pragma table_info({table})")}:
                    c.execute(f"alter table {table} add column {col} {typ}")
            for r in c.execute("select key from projects where view_key is null").fetchall():
                c.execute("update projects set view_key=?, owner=? where key=?", (secrets.token_urlsafe(18), owner_of(r["key"]), r["key"]))

    @property
    def secret(self) -> str:
        """A random secret of this server: signs the sign-in state and form tokens. Made once, kept in the data folder."""
        f = self.root / "secret"
        if not f.exists():
            f.write_text(secrets.token_urlsafe(32))
            f.chmod(0o600)
        return f.read_text().strip()

    def db(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.root / "assay.db", timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def dir(self, aid: int) -> Path:
        return self.root / "analyses" / str(aid)

    def analysis(self, aid: int) -> dict | None:
        f = self.dir(aid) / "analysis.json"
        return json.loads(f.read_text()) if f.exists() else None

    def recent_by_owner(self, owner: str | None) -> int:
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        with self.db() as c:
            return c.execute("select count(*) from analyses a join projects p on p.key=a.project where p.owner=? and a.created>?",
                             (owner, since)).fetchone()[0]

    def project(self, key: str):
        with self.db() as c:
            return c.execute("select * from projects where key=?", (key,)).fetchone()

    def row(self, aid: int):
        with self.db() as c:
            return c.execute("select * from analyses where id=?", (aid,)).fetchone()

    def latest(self, project: str, branch: str | None = None, pr: str | None = None):
        with self.db() as c:
            if pr:
                return c.execute("select * from analyses where project=? and pr=? order by created desc, id desc limit 1",
                                 (project, pr)).fetchone()
            return c.execute("select * from analyses where project=? and pr is null and branch=? order by created desc, id desc limit 1",
                             (project, branch)).fetchone()

    def add(self, meta: dict, tar_path: Path, visibility: str | None = None, repo_id: str | None = None) -> int:
        """visibility: 'public' / 'private' from the uploading repository (OIDC), None keeps what the project has."""
        tmp = Path(tempfile.mkdtemp(dir=self.root / "analyses", prefix=".up-"))
        try:
            unpack(tar_path, tmp)
            a = check_analysis(json.loads((tmp / "analysis.json").read_text()))
            (tmp / "analysis.json").write_text(json.dumps(a, ensure_ascii=False))   # pages read the checked version
            return self._index(meta, a, tmp, visibility, repo_id)
        except Exception as ex:
            shutil.rmtree(tmp, ignore_errors=True)
            if isinstance(ex, ValueError):
                raise
            if isinstance(ex, (tarfile.TarError, OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError)):
                raise ValueError(f"the upload must be a .tar.gz of a run folder with a valid analysis.json ({type(ex).__name__})") from None
            raise

    @staticmethod
    def _ensure_project(c, key: str, meta: dict, created: str, visibility: str | None, repo_id: str | None) -> None:
        c.execute("insert or ignore into projects(key, title, default_branch, created, owner, visibility, view_key, repo_id) values(?,?,?,?,?,?,?,?)",
                  (key, meta.get("title") or key.split("/")[-1], meta.get("default_branch") or meta.get("target") or "main", created,
                   owner_of(key), visibility, secrets.token_urlsafe(18), repo_id))
        if visibility:
            c.execute("update projects set visibility=? where key=?", (visibility, key))
        if repo_id:
            c.execute("update projects set repo_id=? where key=? and repo_id is null", (repo_id, key))
        if meta.get("owner_id"):                  # GitHub's account id from CI's token: who owns it, whatever its login
            c.execute("update projects set owner_id=? where key=? and owner_id is null", (meta["owner_id"], key))
        if meta.get("default_branch"):
            c.execute("update projects set default_branch=? where key=?", (meta["default_branch"], key))
        if meta.get("title"):
            c.execute("update projects set title=? where key=?", (meta["title"], key))

    def bench_dir(self, bid: int) -> Path:
        return self.root / "benchmarks" / str(bid)

    def bench(self, bid: int) -> dict | None:
        f = self.bench_dir(bid) / "bench.json"
        return json.loads(f.read_text()) if f.exists() else None

    def add_bench(self, meta: dict, tar_path: Path, visibility: str | None = None, repo_id: str | None = None) -> int:
        tmp = Path(tempfile.mkdtemp(dir=self.root / "benchmarks", prefix=".up-"))
        try:
            unpack(tar_path, tmp, required="bench.json")
            b = check_bench(json.loads((tmp / "bench.json").read_text()))
            (tmp / "bench.json").write_text(json.dumps(b, ensure_ascii=False))
            created = utc_iso(meta.get("created"))
            arms = {a: {"pass@1": r["pass@1"], "ci95": r["ci95"], "flaky": len(r["flaky"]),
                        "diff": (r.get("diff_vs_baseline") or {}).get("mean"), "significant": (r.get("diff_vs_baseline") or {}).get("significant")}
                    for a, r in b["arms"].items()}
            with self.lock, self.db() as c:
                self._ensure_project(c, meta["project"], meta, created, visibility, repo_id)
                cur = c.execute("insert into benchmarks(project,branch,pr,sha,ci_url,created,cases,attempts,baseline,arms) values(?,?,?,?,?,?,?,?,?,?)",
                                (meta["project"], meta.get("branch"), meta.get("pr") or None, meta.get("sha"), meta.get("run_url"), created,
                                 b["cases"], b["attempts"], b["baseline"], json.dumps(arms)))
                bid = cur.lastrowid
                tmp.rename(self.bench_dir(bid))
            return bid
        except Exception as ex:
            shutil.rmtree(tmp, ignore_errors=True)
            if isinstance(ex, ValueError):
                raise
            if isinstance(ex, (tarfile.TarError, OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError)):
                raise ValueError(f"the upload must be a .tar.gz with a valid bench.json ({type(ex).__name__})") from None
            raise

    def bench_row(self, bid: int):
        with self.db() as c:
            return c.execute("select * from benchmarks where id=?", (bid,)).fetchone()

    def _index(self, meta: dict, a: dict, tmp: Path, visibility: str | None, repo_id: str | None) -> int:
        s, ch = a["summary"], a.get("changes") or {}
        created = utc_iso(meta.get("created") or (a.get("run") or {}).get("started"))
        key = meta["project"]
        with self.lock, self.db() as c:
            ref = meta.get("reference_id")
            if ref is not None:                    # a reference from another project would show its branch and commit here
                rr = c.execute("select project from analyses where id=?", (ref,)).fetchone()
                meta["reference_id"] = ref if rr and rr["project"] == key else None
            self._ensure_project(c, key, meta, created, visibility, repo_id)
            cur = c.execute(
                "insert into analyses(project,title,branch,pr,pr_title,target,sha,provider,ci_url,created,status,cases,pass,fail,inc,"
                "issues,new_issues,ratings,reference_id,fixed,regressed) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, meta.get("title"), meta.get("branch"), meta.get("pr") or None, meta.get("pr_title"), meta.get("target"),
                 meta.get("sha"), meta.get("provider"), meta.get("run_url"), created, a["gate"]["status"], s["cases"], s["PASS"],
                 s["FAIL"], s["INCONCLUSIVE"], s["issues"], s["new_issues"] if a.get("baseline") else None,
                 json.dumps(a["ratings"]), meta.get("reference_id"),          # overall ratings, as SonarQube's project page
                 len(ch.get("fixed", [])), len(ch.get("regressions", []))))
            aid = cur.lastrowid
            tmp.rename(self.dir(aid))
        return aid


# ---- pages: see assay/web --------------------------------------------------------------------------------------------------

def _where(r) -> str:
    return f"PR #{r['pr']}" if r["pr"] else (r["branch"] or "—")


def badge(status: str | None) -> str:
    label, value = "assay", (status or "none").lower()
    col = {"PASSED": "#2f6f55", "FAILED": "#a9443a", "INCONCLUSIVE": "#9a6c12"}.get(status or "", "#69727b")
    lw, vw = 46, 8 + 7 * len(value)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{lw + vw}" height="20" role="img" aria-label="{label}: {value}">'
            f'<rect width="{lw}" height="20" fill="#1f3d63"/><rect x="{lw}" width="{vw}" height="20" fill="{col}"/>'
            f'<g fill="#fff" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11" text-anchor="middle">'
            f'<text x="{lw / 2}" y="14">{label}</text><text x="{lw + vw / 2}" y="14">{value}</text></g></svg>')


# ---- HTTP -----------------------------------------------------------------------------------------------------------------

# uploaded files: an opaque origin, no scripts, no forms, no popups
SANDBOX = "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; font-src data:"
# the site's own pages: no inline script at all, so stray markup in an upload cannot run; no third party (system fonts);
# no DOM sink may take a string (Trusted Types with no policy: app.js only sets text and attributes)
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; "
            "img-src 'self' data: https://github.com https://avatars.githubusercontent.com; connect-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; require-trusted-types-for 'script'; trusted-types 'none'")
PAGE_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY",
                "Content-Security-Policy": PAGE_CSP, "Cross-Origin-Opener-Policy": "same-origin",
                "Cross-Origin-Resource-Policy": "same-origin",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"}
# pages prefetch the links a person is about to follow (Chromium; others ignore it); the rules exclude anything that
# changes state on GET: sign in/out, the view toggle and the CI key (both set cookies), exports, raw files
SPECULATION = {"Speculation-Rules": '"/static/speculation.json"'}
COOKIE_THEME = "assay_theme"                    # light | dark; absent: follow the system


def _eq(a: str | None, b: str | None) -> bool:
    """Constant-time comparison of secrets that may hold any characters."""
    return bool(a) and bool(b) and hmac.compare_digest(str(a).encode(), str(b).encode())


def make_handler(store: Store, token: str, public: str | None, config: dict, oidc: GitHubOIDC | None = None):
    forced_public = set(config.get("public") or [])
    sessions: dict[str, float] = {}            # admin sign-ins: random ids, 30 days, gone on restart
    secure = bool(public and public.startswith("https://"))
    cookie_flags = "Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")
    providers = oauth.configured(config)
    smtp = (config.get("smtp") or {}) if public else {}    # email links need the server's own address
    admins = {str(x).lower() for x in config.get("admins") or []}
    signup_open = ((config.get("auth") or {}).get("signup", True)) is not False
    secret = store.secret
    attempts: dict[str, list[float]] = {}          # sign-in tries per client and per email: a sliding window
    attempts_lock = threading.Lock()

    def limited(key: str, most: int, window: float = 600) -> bool:
        now = time.time()
        with attempts_lock:
            if len(attempts) > 10000:                  # forget only what has aged out of every window
                for k in [k for k, ts in attempts.items() if not ts or ts[-1] < now - 900]:
                    del attempts[k]
            seen = [t for t in attempts.get(key, []) if t > now - window]
            seen.append(now)
            attempts[key] = seen
            return len(seen) > most

    def safe_next(target: str | None) -> str:
        """Only a path on this site: no scheme, no host, no // or backslash tricks."""
        t = target or ""
        return t if len(t) < 500 and re.fullmatch(r"/(?![/\\])[\x21-\x7e]*", t) and "\\" not in t else "/projects"

    class H(BaseHTTPRequestHandler):
        server_version = f"assay-server/{__version__}"
        timeout = 60                           # a slow client cannot hold a thread forever

        def log_message(self, fmt, *args):  # quiet: one line per request, without view keys
            path = re.sub(r"([?&])(k|t|code|state|next)=[^&\s]+", r"\1\2=…", self.path)
            print(f"  {self.command} {path} → {args[1] if len(args) > 1 else ''}", flush=True)

        def _base(self) -> str:
            return public or f"http://{self.headers.get('Host', 'localhost')}"

        def _send(self, code: int, body: bytes | str, ctype: str = "text/html; charset=utf-8", extra: dict | None = None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            headers = {**PAGE_HEADERS, **(SPECULATION if ctype.startswith("text/html") else {}), **(extra or {})}
            if secure:
                headers["Strict-Transport-Security"] = "max-age=31536000"
            for k, v in [*headers.items(), *(("Set-Cookie", c) for c in getattr(self, "_cookies", []))]:
                if "\r" in v or "\n" in v:
                    raise ValueError("header value with a line break")
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str), "application/json")

        def _export(self, data: dict, fmt: str, table: str | None, name: str):
            """An agent page as data: all of it as JSON, or one of its tables as CSV."""
            if fmt == "json":
                return self._send(200, json.dumps(data, ensure_ascii=False, default=str, indent=1), "application/json",
                                  {"Content-Disposition": f'inline; filename="{name}.json"'})
            tables = data.get("tables") or {}
            rows = tables.get(table) if table else next(iter(tables.values()), [])
            if rows is None:
                return self._send(404, f"no table {table!r}: " + ", ".join(tables), "text/plain")
            import csv
            import io
            cols = list(dict.fromkeys(k for r in rows for k in r))
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            for r in rows:
                w.writerow(["" if r.get(c) is None else json.dumps(r[c], ensure_ascii=False, default=str)
                            if isinstance(r.get(c), (dict, list)) else r[c] for c in cols])
            return self._send(200, buf.getvalue(), "text/csv; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{name}-{table or "table"}.csv"'})

        def _form(self) -> dict | None:
            """A small urlencoded form, or None when it is not one (negative, missing or oversized length)."""
            try:
                n = int(self.headers.get("Content-Length") or "")
            except ValueError:
                return None
            if n < 0 or n > 16384:
                return None
            return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode(errors="replace")).items()}

        def _csrf(self, ck: dict) -> str:
            """The token forms post back (a cookie and a hidden field must match); made on the first page."""
            t = ck.get(COOKIE_CSRF)
            if not t or not re.fullmatch(r"[\w-]{20,64}", t):
                t = secrets.token_urlsafe(24)
                self._cookies.append(f"{COOKIE_CSRF}={t}; Max-Age=31536000; {cookie_flags}")
            return t

        def _post_ok(self, form: dict, ck: dict) -> bool:
            """A form from this site: same-site by the browser's word, and its token matches the cookie's."""
            site = self.headers.get("Sec-Fetch-Site")
            if site and site not in ("same-origin", "none"):
                return False
            return _eq(form.get("csrf"), ck.get(COOKIE_CSRF))

        def _client(self) -> str:
            if config.get("trusted_proxy"):           # behind a proxy we run: it appends the real peer last
                return (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip() or self.client_address[0]
            return self.client_address[0]

        def _sign_in(self, uid: int, ck: dict, nxt: str):
            """Start a session; the private projects whose links this browser holds join the account."""
            t = A.start_session(store, uid)
            self._cookies.append(f"{COOKIE_SESSION}={t}; Max-Age={A.SESSION_DAYS * 86400}; {cookie_flags}")
            with store.db() as c:
                rows = c.execute("select key, view_key from projects where visibility='private' and view_key is not null").fetchall()
            for r in rows:
                held = ck.get(cookie_name(r["key"]))
                if held and _eq(held, r["view_key"]):
                    A.remember_project(store, uid, r["key"])
            return self._redirect(safe_next(nxt), 303)

        def _redirect(self, to: str, code: int = 302):
            return self._send(code, "", extra={"Location": to})

        def _not_found(self, what: str = "not found"):
            from .web import pages as W
            return self._send(404, W.not_found_page(what))

        def _bearer(self) -> str:
            auth = self.headers.get("Authorization") or ""
            return auth[7:].strip() if auth.startswith("Bearer ") else ""

        def _viewer(self) -> Viewer:
            ck = parse_cookies(self.headers.get("Cookie"))
            sid = ck.get(COOKIE_ADMIN, "")
            v = Viewer(admin=sessions.get(sid, 0) > time.time(), cookies=ck)
            if (u := A.session_user(store, ck.get(COOKIE_SESSION))):
                v.user, v.keys, v.owners = u, A.project_keys(store, u["id"]), A.owners(store, u["id"])
                v.admin = v.admin or bool(u["email_verified"] and (u["email"] or "") in admins)
            cred = self._bearer()
            if _eq(cred, token):
                v.admin = True
            elif cred and oidc and cred.count(".") == 2:
                try:
                    claims = oidc.verify(cred)
                    v.repo, v.repo_id = claims["repository"], str(claims.get("repository_id") or "")
                except AuthError:
                    pass
            return v

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            from .web import pages as W
            u = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(u.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            self._cookies = []
            try:
                viewer = self._viewer()
                if m := re.fullmatch(r"/static/([\w.-]+)", path):
                    f = STATIC / m[1]
                    if not f.is_file():
                        return self._send(404, "not found", "text/plain")
                    ctype = ("application/speculationrules+json" if f.name == "speculation.json" else "font/woff2"
                             if f.suffix == ".woff2" else mimetypes.guess_type(f.name)[0] or "text/plain")
                    # fonts never change under one name; everything else is revalidated
                    cache = "public, max-age=31536000, immutable" if f.suffix == ".woff2" else "no-cache"
                    return self._send(200, f.read_bytes(), ctype, {"Cache-Control": cache})
                from .web import auth_pages as AP
                from .web.ui import CSRF, THEME, USER
                THEME.set(t if (t := viewer.cookies.get(COOKIE_THEME)) in ("light", "dark") else "")
                # forms live on the sign-in pages and in a signed-in person's menu: only then is a token needed
                needs_form = bool(viewer.user or viewer.admin) or path in ("/login", "/signup", "/forgot", "/reset", "/logout", "/account")
                CSRF.set(self._csrf(viewer.cookies) if needs_form else "")
                USER.set({"id": viewer.user["id"], "name": viewer.user["name"], "email": viewer.user["email"]} if viewer.user else
                         {"name": "Admin", "email": ""} if viewer.admin and not self._bearer() else None)
                if path in ("/login", "/signup") and viewer.user:
                    return self._redirect(safe_next(q.get("next")))
                if path == "/login":
                    return self._send(200, AP.login_page(list(providers), q.get("error") and AUTH_ERRORS.get(q["error"]),
                                                         AUTH_INFO.get(q.get("info") or ""), q.get("next") or ""))
                if path == "/signup":
                    return self._send(200, AP.signup_page(list(providers), nxt=q.get("next") or "", open_=signup_open))
                if path == "/forgot":
                    return self._send(200, AP.forgot_page(bool(smtp.get("host"))))
                if path == "/reset":
                    return self._send(200, AP.reset_page(q.get("t") or "", A.peek_email_token(store, q.get("t"), "reset")))
                if path == "/verify":
                    if not viewer.user:
                        return self._redirect("/login?" + urllib.parse.urlencode({"next": "/verify?t=" + (q.get("t") or "")}))
                    return self._send(200, AP.verify_page(q.get("t") or "", viewer.user, A.peek_email_token(store, q.get("t"), "verify")))
                if path == "/account":
                    if not viewer.user:
                        return self._redirect("/login?" + urllib.parse.urlencode({"next": "/account"}))
                    u = viewer.user
                    with store.db() as c:
                        mine = sum(1 for r in c.execute("select key, owner_id from projects").fetchall() if viewer.mine(r))
                    return self._send(200, AP.account_page(u, A.identities(store, u["id"]), list(providers), mine,
                                                           bool(smtp.get("host")), ACCOUNT_ERRORS.get(q.get("error") or ""),
                                                           ACCOUNT_INFO.get(q.get("info") or "")))
                if m := re.fullmatch(r"/auth/(google|github)", path):
                    prov = m[1]
                    if prov not in providers:
                        return self._redirect("/login?error=provider")
                    return self._oauth_start(prov, safe_next(q.get("next")) if q.get("next") else "", link=False)
                if m := re.fullmatch(r"/auth/(google|github)/callback", path):
                    prov = m[1]
                    raw = A.unsign(secret, viewer.cookies.get(COOKIE_OAUTH))
                    self._cookies.append(f"{COOKIE_OAUTH}=; Max-Age=0; {cookie_flags}")
                    try:
                        pending = json.loads(base64.urlsafe_b64decode(raw.encode())) if raw else None
                    except ValueError:
                        pending = None
                    if (not pending or pending.get("p") != prov or time.time() - pending.get("t", 0) > 600
                            or not q.get("state") or not _eq(q.get("state"), pending.get("s")) or prov not in providers):
                        return self._redirect("/login?error=state")
                    if q.get("error") or not q.get("code"):
                        return self._redirect("/login?error=cancelled")
                    try:
                        profile = oauth.finish(prov, providers[prov], q["code"], self._base() + f"/auth/{prov}/callback", pending)
                        current = viewer.user["id"] if pending.get("link") and viewer.user else None
                        uid, what = A.sign_in_with(store, prov, profile, current)
                    except ValueError:
                        return self._redirect("/account?error=taken" if pending.get("link") else "/login?error=provider")
                    except Exception as ex:                       # the provider refused or is unreachable
                        print(f"  sign-in with {prov} failed: {type(ex).__name__}: {ex}", flush=True)
                        return self._redirect("/login?error=provider")
                    if prov == "github" and profile.get("owners"):
                        A.set_owners(store, uid, [o for o in profile["owners"] if o])
                    if current:
                        return self._redirect("/account?info=connected")
                    return self._sign_in(uid, viewer.cookies, pending.get("next") or "/projects")
                if path == "/":
                    return self._send(200, W.landing_page(store, viewer, self._base()))
                if path == "/projects":
                    return self._send(200, W.projects_page(store, q, viewer))
                if path == "/docs":
                    return self._send(200, W.docs_page())
                if path == "/setup":
                    return self._send(200, W.setup_page(store, None, self._base()))
                if path in ("/rules", "/quality-gates"):     # pages for QA engineers, gone: how it works instead
                    return self._redirect("/docs")
                if path == "/logout":                           # signing out is a POST; this page asks for it
                    if not (viewer.user or viewer.admin):
                        return self._redirect("/login")
                    return self._send(200, AP.signout_page(viewer.user or {"name": "Admin"}))
                if m := re.fullmatch(r"/gh/([\w.-]+)/?", path):
                    html_text = W.account_page(store, "gh/" + m[1], viewer)
                    return self._send(200, html_text) if html_text else self._not_found("No such account, or its projects are private")
                if u.path.startswith("/projects/gh/"):     # legacy-style address of a GitHub project (raw path: no decoded CR/LF)
                    return self._redirect(u.path[len("/projects"):] + (f"?{u.query}" if u.query else ""), 301)
                if m := PROJECT_PATH.fullmatch(path):
                    key, page, sub = m[1] or m[2], m[3] or "", m[4]
                    p = store.project(key)
                    if "k" in q:                           # a link from CI: keep the key in a cookie, clean the address
                        if p is not None and _eq(q["k"], p["view_key"]):
                            self._cookies.append(f"{cookie_name(key)}={p['view_key']}; Max-Age=31536000; {cookie_flags}")
                            if viewer.user and "prefetch" not in (self.headers.get("Sec-Purpose") or ""):
                                A.remember_project(store, viewer.user["id"], key)
                        rest = urllib.parse.urlencode({k: v for k, v in q.items() if k != "k"})
                        return self._redirect(u.path + (f"?{rest}" if rest else ""))
                    if not viewer.can_view(p):
                        return self._not_found("No such project, or it is private. Open it from the link in your pull request.")
                    if viewer.user and (p["visibility"] or "public") == "private" and key not in viewer.keys and viewer.holds_key(p):
                        A.remember_project(store, viewer.user["id"], key)
                    if page in ("screens", "activity", "quality-gate") and not sub:
                        # pages for QA engineers, gone; links in old pull request comments land on the verdict
                        return self._redirect(purl(key) + (f"?{u.query}" if u.query else ""))
                    speculative = "prefetch" in (self.headers.get("Sec-Purpose") or "")
                    if q.get("view") in ("human", "agent") and not speculative:   # the Human | Agent toggle: remembered
                        self._cookies.append(f"{COOKIE_VIEW}={q['view']}; Max-Age=31536000; {cookie_flags}")
                    elif viewer.cookies.get(COOKIE_VIEW) == "agent":
                        q["view"] = "agent"
                    ctx = W.resolve(store, key, q, self._base())
                    html_text = None
                    kind = W.AGENT_PAGES.get(page, {}).get(bool(ctx.pr)) if not sub else None
                    if kind and ctx.view == "agent":
                        if q.get("format") in ("json", "csv"):
                            data = W.agent_data(ctx, kind)
                            name = f"assay-{key.split('/')[-1]}-{kind}{'-' + ctx.pr if ctx.pr else ''}"
                            return self._export(data, q["format"], q.get("table"), name)
                        html_text = W.agent_page(ctx, page, kind)
                    elif page == "" and not sub:
                        html_text = W.summary_page(ctx)
                    elif page == "journeys":
                        html_text = W.journey_page(ctx, sub) if sub else W.journeys_page(ctx)
                    elif page == "pull-requests":
                        html_text = W.prs_page(ctx)
                    elif page == "issues" and not sub:
                        html_text = W.issues_page(ctx)
                    elif page == "branches":
                        html_text = W.branches_page(ctx)
                    elif page == "benchmarks":
                        html_text = W.benchmark_page(ctx, sub) if sub else W.benchmarks_page(ctx)
                    elif page == "setup":
                        html_text = W.setup_page(store, ctx.key, self._base(), ctx, badge_token(p))
                    return self._send(200, html_text) if html_text else self._not_found()
                if path == "/api/health":
                    return self._json(200, {"ok": True, "oidc": bool(oidc)})
                if m := re.fullmatch(r"/api/projects/(.+?)(/analyses/latest|/badge\.svg)?", path):
                    key, what = m[1], m[2] or ""
                    p = store.project(key)
                    if what == "/badge.svg":
                        ok = viewer.can_view(p) or (p is not None and _eq(q.get("token"), badge_token(p)))
                        r = store.latest(key, branch=q.get("branch") or (p["default_branch"] if p else "main")) if ok else None
                        return self._send(200, badge(r["status"] if r else None), "image/svg+xml", {"Cache-Control": "no-cache"})
                    if not viewer.can_view(p):              # missing and private look the same
                        return self._json(404, {"error": "no such project"})
                    if what == "/analyses/latest":
                        r = store.latest(key, pr=q["pr"]) if q.get("pr") else store.latest(key, branch=q.get("branch") or "main")
                        if not r:
                            return self._json(404, {"error": "no analysis on this branch"})
                        a = store.analysis(r["id"])
                        a["server"] = {"id": r["id"], "url": self._base() + analysis_path(r)}
                        a["ci"] = {**(a.get("ci") or {}), "branch": r["branch"], "sha": r["sha"]}
                        return self._json(200, a)
                    gate = (config.get("projects", {}).get(key, {}) or {}).get("gate") or config.get("gate")
                    return self._json(200, {"key": key, "title": p["title"], "default_branch": p["default_branch"], "gate": gate})
                # old and CI links: /p/<key>, /p/<key>/pr/<n>, /a/<id>/ → the project pages
                if m := re.fullmatch(r"/p/([\w.-]+)/?", path):
                    return self._redirect(f"/projects/{m[1]}", 301)
                if m := re.fullmatch(r"/p/([\w.-]+)/pr/(\w+)/?", path):
                    return self._redirect(f"/projects/{m[1]}?pr={m[2]}")
                if m := re.fullmatch(r"/api/analyses/(\d+)", path):
                    # the whole analysis as CI uploaded it: the agent level's raw data
                    r = store.row(int(m[1]))
                    p = store.project(r["project"]) if r else None
                    if not r or p is None or not (viewer.can_view(p) or _eq(q.get("k"), p["view_key"])):
                        return self._json(404, {"error": "no such analysis, or it is private: use the link from the pull request"})
                    return self._json(200, store.analysis(r["id"]))
                if m := re.fullmatch(r"/api/analyses/(\d+)/brief", path):
                    # the brief for coding agents: JSON, readable with the key from the pull request link (no cookie)
                    r = store.row(int(m[1]))
                    p = store.project(r["project"]) if r else None
                    if not r or p is None or not (viewer.can_view(p) or _eq(q.get("k"), p["view_key"])):
                        return self._json(404, {"error": "no such analysis, or it is private: use the link from the pull request"})
                    from .brief import build
                    private = (p["visibility"] or "public") != "public"
                    try:          # files of a private project open with the same key as the brief
                        b = build(store.dir(r["id"]), case=q.get("case"), full=bool(q.get("full")),
                                  base=f"{self._base()}/a/{r['id']}", link_query=f"?k={p['view_key']}" if private else "",
                                  analysis_url=with_key(self._base() + analysis_path(r), p))
                    except KeyError as ex:
                        return self._json(404, {"error": str(ex).strip("'\"")})
                    return self._json(200, b)
                if m := re.fullmatch(r"/a/(\d+)(?:/(.*))?", path):
                    r = store.row(int(m[1]))
                    pa = store.project(r["project"]) if r else None
                    if not r or pa is None or not (viewer.can_view(pa) or (m[2] and _eq(q.get("k"), pa["view_key"]))):
                        return self._not_found("No such analysis")
                    if not m[2]:
                        return self._redirect(analysis_path(r))
                    base = store.dir(int(m[1])).resolve()
                    f = (base / m[2]).resolve()
                    if not f.is_relative_to(base) or not f.is_file():
                        return self._send(404, "not found", "text/plain")
                    ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                    # uploads are untrusted: their documents run in a sandbox with an opaque origin
                    return self._send(200, f.read_bytes(), ctype, {"Cache-Control": "private, max-age=86400",
                                                                    "Content-Security-Policy": SANDBOX})
                return self._not_found()
            except BrokenPipeError:
                pass
            except Exception as ex:  # one broken analysis must not take the server down
                import traceback
                traceback.print_exc()
                return self._send(500, f"server error: {type(ex).__name__}: {html.escape(str(ex))}")

        def _auth_post(self, route: str):
            from .web import auth_pages as AP
            from .web.ui import CSRF
            ck = parse_cookies(self.headers.get("Cookie"))
            form = self._form()
            if form is None:
                return self._send(413, "A form of at most 16 KB, please.", "text/plain; charset=utf-8")
            viewer = self._viewer()
            if not self._post_ok(form, ck):
                return self._send(403, "This form expired. Go back, reload the page and try again.", "text/plain; charset=utf-8")
            CSRF.set(ck.get(COOKIE_CSRF, ""))
            nxt = form.get("next") or ""
            if route == "/logout":
                A.end_session(store, ck.get(COOKIE_SESSION))
                sessions.pop(ck.get(COOKIE_ADMIN, ""), None)
                self._cookies += [f"{COOKIE_SESSION}=; Max-Age=0; {cookie_flags}", f"{COOKIE_ADMIN}=; Max-Age=0; {cookie_flags}"]
                return self._redirect("/", 303)
            if route == "/login":
                email, password = (form.get("email") or "").strip(), form.get("password") or ""
                if limited("ip:" + self._client(), 30) or limited("login:" + email.lower(), 10, 900):
                    return self._send(429, AP.login_page(list(providers), "Too many attempts. Wait a few minutes and try again.", nxt=nxt, email=email))
                if not email or not password:
                    return self._send(400, AP.login_page(list(providers), "Enter your email and password.", nxt=nxt, email=email,
                                                         errors={k: " " for k, x in (("email", email), ("password", password)) if not x}))
                admin = config.get("admin") or {}
                if "@" not in email and (_eq(password, token) or (admin.get("password") and email == admin.get("user", "admin")
                                                                    and check_password(password, admin["password"]))):
                    sid = secrets.token_urlsafe(24)                 # the server's own admin, from server.yaml
                    sessions[sid] = time.time() + 30 * 86400
                    self._cookies.append(f"{COOKIE_ADMIN}={sid}; Max-Age=2592000; {cookie_flags}")
                    return self._redirect(safe_next(nxt), 303)
                u = A.authenticate(store, A.norm_email(email) or "", password)
                if not u:
                    return self._send(401, AP.login_page(list(providers), "That email and password do not match an account.", nxt=nxt, email=email))
                return self._sign_in(u["id"], ck, nxt)
            if route == "/signup":
                name, email, password = (form.get("name") or "").strip(), A.norm_email(form.get("email")), form.get("password") or ""
                if not signup_open:
                    return self._send(403, AP.signup_page(list(providers), open_=False))
                if limited("ip:" + self._client(), 30):
                    return self._send(429, AP.signup_page(list(providers), "Too many attempts. Wait a few minutes and try again.", nxt, name, form.get("email") or ""))
                errors = {k: m for k, m in (("name", None if name else "Enter your name."),
                                             ("email", None if email else "Enter an email address, like name@example.com."),
                                             ("password", A.password_problem(password))) if m}
                if errors:
                    return self._send(400, AP.signup_page(list(providers), None, nxt, name, form.get("email") or "", errors=errors))
                try:
                    uid = A.create(store, email, name, password)
                except ValueError:
                    return self._send(409, AP.signup_page(list(providers), None, nxt, name, form.get("email") or "",
                                                          errors={"email": "An account with this email exists. Sign in, or reset its password."}))
                if smtp.get("host"):
                    self._mail_link(uid, email, "verify")
                return self._sign_in(uid, ck, nxt or "/projects")
            if route == "/forgot":
                email = A.norm_email(form.get("email"))
                if not smtp.get("host"):
                    return self._send(200, AP.forgot_page(False))
                if limited("ip:" + self._client(), 10) or not email:
                    return self._send(400 if not email else 429, AP.forgot_page(True, error="Enter the email of your account." if not email else
                                                                                "Too many requests. Wait a few minutes."))
                u = A.by_email(store, email)
                if u:
                    self._mail_link(u["id"], email, "reset")
                return self._send(200, AP.forgot_page(True, sent=True))     # the same answer whether or not it exists
            if route == "/reset":
                t, password = form.get("t") or "", form.get("password") or ""
                if (problem := A.password_problem(password)):
                    return self._send(400, AP.reset_page(t, A.peek_email_token(store, t, "reset"), problem))
                uid = A.use_email_token(store, t, "reset")
                if not uid:
                    return self._send(400, AP.reset_page(t, False))
                if not (A.user(store, uid) or {}).get("email_verified"):
                    A.reclaim(store, uid)                                    # the link reached the owner's inbox
                A.set_password(store, uid, password)
                A.update(store, uid, email_verified=1)
                A.end_all_sessions(store, uid)
                return self._sign_in(uid, ck, "/projects")
            if route == "/account":
                if not viewer.user:
                    return self._redirect("/login?next=/account", 303)
                u, do = viewer.user, form.get("do")
                if do == "name":
                    A.update(store, u["id"], name=(form.get("name") or "").strip()[:120] or u["name"])
                    return self._redirect("/account?info=saved", 303)
                if do == "password":
                    if limited(f"pw:{u['id']}", 5, 900):
                        return self._redirect("/account?error=wait", 303)
                    if u.get("password") and not A.authenticate(store, u["email"], form.get("current") or ""):
                        return self._redirect("/account?error=current", 303)
                    if A.password_problem(form.get("new") or ""):
                        return self._redirect("/account?error=short", 303)
                    A.set_password(store, u["id"], form["new"])
                    A.end_all_sessions(store, u["id"], keep=ck.get(COOKIE_SESSION))
                    return self._redirect("/account?info=password", 303)
                if do == "connect" and form.get("provider") in providers:
                    return self._oauth_start(form["provider"], "", link=True)
                if do == "confirm":                     # the verify link, confirmed by the signed-in owner of the token
                    if not A.peek_email_token(store, form.get("t"), "verify"):
                        return self._redirect("/account?error=link", 303)
                    with store.db() as c:
                        r = c.execute("select user_id from email_tokens where id=?", (A._h(form.get("t") or ""),)).fetchone()
                    if not r or r["user_id"] != u["id"]:
                        return self._redirect("/account?error=other", 303)
                    A.use_email_token(store, form.get("t"), "verify")
                    A.update(store, u["id"], email_verified=1)
                    return self._redirect("/account?info=verified", 303)
                if do == "disconnect":
                    ok = A.disconnect(store, u["id"], form.get("provider") or "")
                    return self._redirect("/account?info=disconnected" if ok else "/account?error=last", 303)
                if do == "sessions":
                    A.end_all_sessions(store, u["id"], keep=ck.get(COOKIE_SESSION))
                    return self._redirect("/account?info=sessions", 303)
                if do == "verify" and smtp.get("host") and u["email"] and not u["email_verified"]:
                    self._mail_link(u["id"], u["email"], "verify")
                    return self._redirect("/account?info=sent", 303)
                if do == "delete" and form.get("confirm") == "yes":
                    A.delete(store, u["id"])
                    self._cookies.append(f"{COOKIE_SESSION}=; Max-Age=0; {cookie_flags}")
                    return self._redirect("/login?info=deleted", 303)
                return self._redirect("/account", 303)
            return self._json(404, {"error": "not found"})

        def _oauth_start(self, prov: str, nxt: str, link: bool):
            url, pending = oauth.start(prov, providers[prov], self._base() + f"/auth/{prov}/callback")
            pending.update({"next": nxt, "link": link})
            value = base64.urlsafe_b64encode(json.dumps(pending).encode()).decode()
            self._cookies.append(f"{COOKIE_OAUTH}={A.sign(secret, value)}; Max-Age=600; {cookie_flags}")
            return self._redirect(url, 303)

        def _mail_link(self, uid: int, email: str, kind: str) -> None:
            ttl = 3600 if kind == "reset" else 3 * 86400
            t = A.email_token(store, uid, kind, ttl)
            link = f"{public}/{'reset' if kind == 'reset' else 'verify'}?t={t}"
            text = (f"Set a new password for your Assay account:\n\n{link}\n\nThe link works for one hour, once. "
                    "If you did not ask for it, ignore this email." if kind == "reset" else
                    f"Confirm the email of your Assay account:\n\n{link}\n\nIf you did not sign up, ignore this email.")
            def send():
                try:
                    A.send_mail(smtp, email, "Reset your Assay password" if kind == "reset" else "Confirm your email for Assay", text)
                except Exception as ex:                          # the form still answers; the server log says why
                    print(f"  email to {email.split('@')[-1]} failed: {type(ex).__name__}", flush=True)
            threading.Thread(target=send, daemon=True).start()

        def _read_body(self, n: int, sink) -> None:
            left = n
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                sink.write(chunk)
                left -= len(chunk)

        def do_POST(self):
            self._cookies = []
            route = self.path.split("?")[0]
            if route in AUTH_POSTS:
                try:
                    return self._auth_post(route)
                except BrokenPipeError:
                    return None
            if route not in ("/api/analyses", "/api/benchmarks"):
                return self._json(404, {"error": "not found"})
            cred, claims = self._bearer(), None
            if not cred:
                return self._json(401, {"error": "no credentials: on GitHub Actions give the job `permissions: id-token: write`; "
                                                 "elsewhere pass the server's admin token as ASSAY_TOKEN"})
            if not _eq(cred, token):
                if not oidc or cred.count(".") != 2:
                    return self._json(401, {"error": "wrong token (ASSAY_TOKEN)"})
                try:
                    claims = oidc.verify(cred)
                except AuthError as ex:
                    return self._json(401, {"error": f"GitHub sign-in refused: {ex}"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n <= 0 or n > MAX_UPLOAD:
                return self._json(413, {"error": f"empty or larger than {MAX_UPLOAD // 1024 ** 2} MB"})
            try:
                meta = check_meta(json.loads(base64.b64decode(self.headers.get("X-Assay-Meta") or "e30=")))
                visibility, repo_id = None, None
                meta.pop("owner_id", None)              # set from the token below, never by the client
                if claims:                              # the token decides the project, never the client
                    meta["project"] = project_key(claims)
                    meta["provider"] = "github"
                    meta["run_url"] = (f"https://github.com/{claims['repository']}/actions/runs/{claims['run_id']}"
                                       if str(claims.get("run_id") or "").isdigit() else None)
                    visibility = "public" if claims.get("repository_visibility") == "public" else "private"
                    repo_id = str(claims.get("repository_id") or "") or None
                    meta["owner_id"] = str(claims.get("repository_owner_id") or "") or None
                    p0 = store.project(meta["project"])
                    if p0 is not None and p0["repo_id"] and repo_id and p0["repo_id"] != repo_id:
                        return self._json(403, {"error": "this project belongs to an earlier repository with the same name"})
                    if store.recent_by_owner(owner_of(meta["project"])) >= OWNER_DAILY_LIMIT:
                        return self._json(429, {"error": f"more than {OWNER_DAILY_LIMIT} analyses in 24 hours for this account"})
                elif meta.get("visibility") in ("public", "private"):
                    visibility = meta["visibility"]
                if meta.get("project") in forced_public:
                    visibility = "public"
                if not KEY_RE.fullmatch(meta.get("project") or ""):
                    return self._json(400, {"error": "meta.project must be a key: letters, digits, . _ - (or gh/<owner>/<repo>)"})
                with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
                    self._read_body(n, tmp)
                    tmp.flush()
                    if route == "/api/benchmarks":
                        bid = store.add_bench(meta, Path(tmp.name), visibility, repo_id)
                    else:
                        aid = store.add(meta, Path(tmp.name), visibility, repo_id)
            except ValueError as ex:
                return self._json(400, {"error": str(ex)})
            base = self._base()
            if route == "/api/benchmarks":
                p = store.project(meta["project"])
                print(f"  benchmark #{bid} · {meta['project']} · {meta.get('branch') or ''}{' PR #' + meta['pr'] if meta.get('pr') else ''}", flush=True)
                return self._json(201, {"id": bid, "url": with_key(f"{base}{purl(p['key'])}/benchmarks/{bid}", p), "project": p["key"],
                                        "account_url": base + "/" + p["owner"] if p["owner"] else None})
            r, p = store.row(aid), store.project(meta["project"])
            print(f"  analysis #{aid} · {meta['project']} · {_where(r)} · {r['status']}", flush=True)
            return self._json(201, {"id": aid, "url": with_key(base + analysis_path(r), p), "status": r["status"],
                                    "brief_url": with_key(f"{base}/api/analyses/{aid}/brief", p),
                                    "project": p["key"], "project_url": with_key(base + purl(p["key"]), p),
                                    "account_url": base + "/" + p["owner"] if p["owner"] else None,
                                    "created_project": len(store_rows(store, p["key"])) == 1})
    return H


def store_rows(store: Store, key: str) -> list:
    with store.db() as c:
        return c.execute("select id from analyses where project=?", (key,)).fetchall()


def serve(args, log=print) -> int:
    root = Path(args.data).resolve()
    store = Store(root)
    token = os.environ.get("ASSAY_SERVER_TOKEN")
    if not token:
        tf = root / "token"
        if not tf.exists():
            tf.write_text(secrets.token_urlsafe(24))
            tf.chmod(0o600)
            log(f"  generated an admin token in {tf} (uploads from other CI, /login)")
        token = tf.read_text().strip()
    config = {}
    if args.config and Path(args.config).exists():
        import yaml
        config = yaml.safe_load(Path(args.config).read_text()) or {}
    public = args.public_url or f"http://{args.host}:{args.port}"
    oidc = GitHubOIDC([public] + list(config.get("oidc_audiences") or [])) if args.public_url else None
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(store, token, args.public_url, config, oidc))
    log(f"assay server · {public} · data {root}" + (" · GitHub Actions sign-in on" if oidc else ""))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
