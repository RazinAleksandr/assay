"""People on the Assay server: accounts, sessions, and what each account may see.

An account signs in with an email and a password, with Google, or with GitHub; one account may hold all three.
Sessions live in the database (only a hash of the cookie's token is stored), 30 days, ended by signing out.

What an account sees, besides public projects:
- the projects of its own GitHub account, once it signed in with GitHub: matched by GitHub's numeric account id,
  which CI's token stamps on each project, never by the login (a login that changes hands keeps nothing);
- the private projects it opened from a pull request's link: the link's key is remembered in the account, so they
  are "your projects" on any device.

Linking. A Google sign-in joins the account with the same email only where Google is the authority for it (a Gmail
address, or a Workspace domain it names); GitHub never joins by email, it is connected from the account page. If the
joined account's email was never verified, everything its first holder set up goes: password, other sign-in methods,
sessions, remembered projects. Whoever registered the address first keeps no way into the account of its owner.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from .access import check_password, hash_password

SCHEMA = """
create table if not exists users(id integer primary key autoincrement, email text unique, name text, password text,
  email_verified int default 0, created text, last_login text);
create table if not exists identities(provider text, subject text, user_id int not null, email text, login text,
  created text, primary key(provider, subject));
create index if not exists identities_user on identities(user_id);
create table if not exists sessions(id text primary key, user_id int not null, created text, expires real);
create index if not exists sessions_user on sessions(user_id);
create table if not exists user_projects(user_id int not null, project text not null, via text, created text,
  primary key(user_id, project));
create table if not exists user_owners(user_id int not null, owner text not null, primary key(user_id, owner));
create table if not exists email_tokens(id text primary key, user_id int not null, kind text, expires real);
"""

SESSION_DAYS = 30
PASSWORD_ROUNDS = 600_000                    # PBKDF2-SHA256, OWASP 2023
MIN_PASSWORD = 10
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}")
_DUMMY = hash_password("not a password", rounds=PASSWORD_ROUNDS)      # equal work for unknown emails
_HASHING = threading.BoundedSemaphore(4)     # password hashing is the costly part of a request: a few at a time


def _hash(password: str) -> str:
    with _HASHING:
        return hash_password(password, rounds=PASSWORD_ROUNDS)


def _check(password: str, stored: str) -> bool:
    with _HASHING:
        return check_password(password, stored)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def norm_email(email: str | None) -> str | None:
    e = (email or "").strip().lower()
    return e if len(e) <= 254 and _EMAIL.fullmatch(e) else None


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(password) > 200:
        return "Use at most 200 characters."
    return None


def user(store, uid: int | None) -> dict | None:
    if not uid:
        return None
    with store.db() as c:
        r = c.execute("select * from users where id=?", (uid,)).fetchone()
    return dict(r) if r else None


def by_email(store, email: str) -> dict | None:
    with store.db() as c:
        r = c.execute("select * from users where email=?", (email,)).fetchone()
    return dict(r) if r else None


def create(store, email: str | None, name: str, password: str | None = None, verified: bool = False) -> int:
    """A new account; raises ValueError if the email already has one."""
    with store.db() as c:
        if email and c.execute("select 1 from users where email=?", (email,)).fetchone():
            raise ValueError("exists")
        cur = c.execute("insert into users(email,name,password,email_verified,created) values(?,?,?,?,?)",
                        (email, name.strip()[:120] or (email or "").split("@")[0], _hash(password) if password else None,
                         int(verified), _now()))
        return cur.lastrowid


def authenticate(store, email: str, password: str) -> dict | None:
    """The account for this email and password, or None; the same work either way, so timing tells nothing."""
    u = by_email(store, email) if email else None
    ok = _check(password, (u or {}).get("password") or _DUMMY)
    return u if u and u.get("password") and ok else None


def set_password(store, uid: int, password: str | None) -> None:
    with store.db() as c:
        c.execute("update users set password=? where id=?", (_hash(password) if password else None, uid))


def update(store, uid: int, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in ("name", "email_verified", "last_login")}
    if allowed:
        with store.db() as c:
            c.execute(f"update users set {', '.join(f'{k}=?' for k in allowed)} where id=?", (*allowed.values(), uid))


def delete(store, uid: int) -> None:
    with store.db() as c:
        for t in ("sessions", "identities", "user_projects", "user_owners", "email_tokens"):
            c.execute(f"delete from {t} where user_id=?", (uid,))
        c.execute("delete from users where id=?", (uid,))


# ---- sessions --------------------------------------------------------------------------------------------------------

def start_session(store, uid: int) -> str:
    token = secrets.token_urlsafe(32)
    with store.db() as c:
        c.execute("insert into sessions(id,user_id,created,expires) values(?,?,?,?)",
                  (_h(token), uid, _now(), time.time() + SESSION_DAYS * 86400))
        c.execute("delete from sessions where expires < ?", (time.time(),))
        c.execute("update users set last_login=? where id=?", (_now(), uid))
    return token


def session_user(store, token: str | None) -> dict | None:
    if not token or len(token) > 100:
        return None
    with store.db() as c:
        r = c.execute("select user_id, expires from sessions where id=?", (_h(token),)).fetchone()
    return user(store, r["user_id"]) if r and r["expires"] > time.time() else None


def end_session(store, token: str | None) -> None:
    if token:
        with store.db() as c:
            c.execute("delete from sessions where id=?", (_h(token),))


def end_all_sessions(store, uid: int, keep: str | None = None) -> None:
    with store.db() as c:
        c.execute("delete from sessions where user_id=? and id<>?", (uid, _h(keep) if keep else ""))


# ---- what an account sees ------------------------------------------------------------------------------------------

def remember_project(store, uid: int, key: str, via: str = "link") -> None:
    with store.db() as c:
        c.execute("insert or ignore into user_projects(user_id,project,via,created) values(?,?,?,?)", (uid, key, via, _now()))


def project_keys(store, uid: int) -> set[str]:
    with store.db() as c:
        return {r["project"] for r in c.execute("select project from user_projects where user_id=?", (uid,))}


def set_owners(store, uid: int, owners: list[str]) -> None:
    """The GitHub account this person is, by its numeric id (projects carry their owner's id from CI's token)."""
    with store.db() as c:
        c.execute("delete from user_owners where user_id=?", (uid,))
        c.executemany("insert or ignore into user_owners(user_id,owner) values(?,?)",
                      [(uid, str(o)) for o in owners if re.fullmatch(r"\d{1,20}", str(o))])


def owners(store, uid: int) -> set[str]:
    with store.db() as c:
        return {r["owner"] for r in c.execute("select owner from user_owners where user_id=?", (uid,))}


# ---- sign-in with a provider -----------------------------------------------------------------------------------------

def identities(store, uid: int) -> dict[str, dict]:
    with store.db() as c:
        return {r["provider"]: dict(r) for r in c.execute("select * from identities where user_id=?", (uid,))}


def reclaim(store, uid: int, keep_provider: str | None = None) -> None:
    """The owner of the address takes the account back: what its first holder set up is gone."""
    set_password(store, uid, None)
    with store.db() as c:
        c.execute("delete from identities where user_id=? and provider<>?", (uid, keep_provider or ""))
        for t in ("sessions", "user_owners", "user_projects", "email_tokens"):
            c.execute(f"delete from {t} where user_id=?", (uid,))
    update(store, uid, email_verified=1)


def sign_in_with(store, provider: str, profile: dict, current: int | None = None) -> tuple[int, str]:
    """The account a provider's profile signs in to: (user id, what happened). `profile`: subject, email,
    email_verified, authoritative (the provider owns that address), name, login. With `current`, the identity is
    connected to that signed-in account instead."""
    sub = str(profile["subject"])
    raw = norm_email(profile.get("email"))
    email = raw if profile.get("email_verified") and profile.get("authoritative") else None
    with store.db() as c:
        known = c.execute("select user_id from identities where provider=? and subject=?", (provider, sub)).fetchone()
    if known:
        if current and known["user_id"] != current:
            raise ValueError("This account is already connected to another Assay account.")
        return known["user_id"], "signed_in"
    if current:
        uid, what = current, "connected"
    elif email and (u := by_email(store, email)):
        uid, what = u["id"], "joined"
        if not u["email_verified"]:              # whoever registered the address first loses the way in
            reclaim(store, uid)
    else:                                        # a new account; an address it cannot vouch for is kept unverified
        free = raw and not by_email(store, raw)
        uid = create(store, email or (raw if free else None), profile.get("name") or profile.get("login") or "", verified=bool(email))
        what = "created"
    with store.db() as c:
        c.execute("insert into identities(provider,subject,user_id,email,login,created) values(?,?,?,?,?,?)",
                  (provider, sub, uid, email, profile.get("login"), _now()))
    return uid, what


def disconnect(store, uid: int, provider: str) -> bool:
    """Remove a sign-in method, unless it is the last one (a password or another provider must remain)."""
    u, ids = user(store, uid), identities(store, uid)
    if provider not in ids or (not (u or {}).get("password") and len(ids) == 1):
        return False
    with store.db() as c:
        c.execute("delete from identities where user_id=? and provider=?", (uid, provider))
        if provider == "github":
            c.execute("delete from user_owners where user_id=?", (uid,))
    return True


# ---- email links: verify an address, reset a password ----------------------------------------------------------------

def email_token(store, uid: int, kind: str, ttl: int) -> str:
    token = secrets.token_urlsafe(32)
    with store.db() as c:
        c.execute("delete from email_tokens where user_id=? and kind=?", (uid, kind))
        c.execute("insert into email_tokens(id,user_id,kind,expires) values(?,?,?,?)", (_h(token), uid, kind, time.time() + ttl))
    return token


def use_email_token(store, token: str | None, kind: str) -> int | None:
    """The account a link was sent to, once: the token is spent whether or not it is still valid."""
    if not token or len(token) > 100:
        return None
    with store.db() as c:
        r = c.execute("select user_id, kind, expires from email_tokens where id=?", (_h(token),)).fetchone()
        if r:
            c.execute("delete from email_tokens where id=?", (_h(token),))
    return r["user_id"] if r and r["kind"] == kind and r["expires"] > time.time() else None


def peek_email_token(store, token: str | None, kind: str) -> bool:
    if not token or len(token) > 100:
        return False
    with store.db() as c:
        r = c.execute("select kind, expires from email_tokens where id=?", (_h(token),)).fetchone()
    return bool(r and r["kind"] == kind and r["expires"] > time.time())


# ---- signed values: OAuth state, CSRF ---------------------------------------------------------------------------------

def sign(secret: str, value: str) -> str:
    return value + "." + hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]


def unsign(secret: str, signed: str | None) -> str | None:
    if not signed or "." not in signed:
        return None
    value, mac = signed.rsplit(".", 1)
    return value if hmac.compare_digest(sign(secret, value).rsplit(".", 1)[1], mac) else None


# ---- email -----------------------------------------------------------------------------------------------------------

def send_mail(smtp: dict, to: str, subject: str, text: str) -> bool:
    """Send a plain-text email through the SMTP server in server.yaml (`smtp: {host, port, user, password, from}`)."""
    import smtplib
    from email.message import EmailMessage
    if not (smtp or {}).get("host"):
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = smtp.get("from") or smtp.get("user"), to, subject
    msg.set_content(text)
    port = int(smtp.get("port") or 587)
    with (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)(smtp["host"], port, timeout=15) as s:
        if port != 465:
            s.starttls()
        if smtp.get("user"):
            s.login(smtp["user"], smtp.get("password") or "")
        s.send_message(msg)
    return True
