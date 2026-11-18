"""Sign in with Google or GitHub: the OAuth 2.0 authorization-code flow, server side.

Google is OpenID Connect: the code is exchanged with PKCE, and the ID token's signature (Google's keys), issuer,
audience (our client id), expiry and nonce are checked before its email is trusted, and only if Google verified it.
GitHub is plain OAuth with the least it needs (read:user, user:email): the user and their verified primary email.
Their numeric account id becomes a project owner they may see (CI stamps each project with its owner's id; a login
that changes hands matches nothing). Organisations are not: membership does not mean access to every private
repository, so an organisation's projects open, as before, from their pull requests' links. Both need a client id and secret in server.yaml:

    auth:
      google: {client_id: "...apps.googleusercontent.com", client_secret: "..."}
      github: {client_id: "...", client_secret: "..."}

The redirect URI to register is <public url>/auth/<provider>/callback. Google accepts it only on https with a domain.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request

from .access import AuthError, _b64url, rsa_sha256_verify

GOOGLE = {"authorize": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
          "jwks": "https://www.googleapis.com/oauth2/v3/certs", "issuers": {"https://accounts.google.com", "accounts.google.com"},
          "scope": "openid email profile"}
GITHUB = {"authorize": "https://github.com/login/oauth/authorize", "token": "https://github.com/login/oauth/access_token",
          "api": "https://api.github.com", "scope": "read:user user:email"}
PROVIDERS = {"google": "Google", "github": "GitHub"}


def _post_form(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Accept": "application/json", "User-Agent": "assay-server"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _get_json(url: str, token: str | None = None) -> dict | list:
    headers = {"Accept": "application/json", "User-Agent": "assay-server"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as r:
        return json.loads(r.read())


class Http:
    """What the flows call on the network; tests swap it for a fake."""
    post_form = staticmethod(_post_form)
    get_json = staticmethod(_get_json)


def configured(config: dict) -> dict[str, dict]:
    """Providers with a client id and secret in server.yaml (`auth:`)."""
    auth = (config or {}).get("auth") or {}
    return {p: auth[p] for p in PROVIDERS if isinstance(auth.get(p), dict) and auth[p].get("client_id") and auth[p].get("client_secret")}


def start(provider: str, cfg: dict, redirect_uri: str) -> tuple[str, dict]:
    """The provider's sign-in URL, and what must come back with the code (kept in a signed, short-lived cookie)."""
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    if provider == "google":
        q = {"client_id": cfg["client_id"], "redirect_uri": redirect_uri, "response_type": "code", "scope": GOOGLE["scope"],
             "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
             "prompt": "select_account"}
        url = GOOGLE["authorize"] + "?" + urllib.parse.urlencode(q)
    else:
        q = {"client_id": cfg["client_id"], "redirect_uri": redirect_uri, "scope": GITHUB["scope"], "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256", "allow_signup": "true"}
        url = GITHUB["authorize"] + "?" + urllib.parse.urlencode(q)
    return url, {"p": provider, "s": state, "n": nonce, "v": verifier, "t": int(time.time())}


class _Keys:
    """Google's signing keys, fetched when a token names one we do not have (at most once a minute)."""

    def __init__(self):
        self.keys: dict[str, tuple[int, int]] = {}
        self.fetched = 0.0
        self.lock = threading.Lock()

    def get(self, kid: str, http) -> tuple[int, int] | None:
        with self.lock:
            if kid not in self.keys and time.time() - self.fetched > 60:
                self.fetched = time.time()
                self.keys = {k["kid"]: (int.from_bytes(_b64url(k["n"]), "big"), int.from_bytes(_b64url(k["e"]), "big"))
                             for k in http.get_json(GOOGLE["jwks"]).get("keys", [])
                             if k.get("kty") == "RSA" and k.get("kid") and k.get("n") and k.get("e")}
            return self.keys.get(kid)


GOOGLE_KEYS = _Keys()


def verify_google_id_token(token: str, client_id: str, nonce: str, http=Http, now: float | None = None) -> dict:
    try:
        h64, p64, s64 = token.split(".")
        header, claims, sig = json.loads(_b64url(h64)), json.loads(_b64url(p64)), _b64url(s64)
    except Exception:
        raise AuthError("Google sent a token that is not a JWT") from None
    if not isinstance(header, dict) or not isinstance(claims, dict) or header.get("alg") != "RS256":
        raise AuthError("Google's token uses an unexpected algorithm")
    key = GOOGLE_KEYS.get(str(header.get("kid") or ""), http)
    if not key or not rsa_sha256_verify(key[0], key[1], f"{h64}.{p64}".encode(), sig):
        raise AuthError("the token's signature is not Google's")
    now = time.time() if now is None else now
    if claims.get("iss") not in GOOGLE["issuers"] or claims.get("aud") != client_id:
        raise AuthError("the token was not issued to this server")
    if not isinstance(claims.get("exp"), (int, float)) or now > claims["exp"] + 30:
        raise AuthError("the token has expired")
    if not nonce or claims.get("nonce") != nonce:
        raise AuthError("the token does not answer this sign-in")
    return claims


def finish(provider: str, cfg: dict, code: str, redirect_uri: str, pending: dict, http=Http) -> dict:
    """Exchange the code; returns a profile: subject, email, email_verified, name, login, owners (GitHub)."""
    if provider == "google":
        tok = http.post_form(GOOGLE["token"], {"code": code, "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
                                               "redirect_uri": redirect_uri, "grant_type": "authorization_code",
                                               "code_verifier": pending["v"]})
        if not tok.get("id_token"):
            raise AuthError("Google did not complete the sign-in")
        c = verify_google_id_token(tok["id_token"], cfg["client_id"], pending["n"], http)
        email = (c.get("email") or "").lower()
        # Google speaks for Gmail and for the Workspace domain it names (hd); another address only proves past control
        authoritative = email.endswith("@gmail.com") or (bool(c.get("hd")) and email.endswith("@" + str(c["hd"]).lower()))
        return {"subject": c["sub"], "email": c.get("email"), "email_verified": c.get("email_verified") is True,
                "authoritative": authoritative, "name": c.get("name") or "", "login": None, "owners": []}
    tok = http.post_form(GITHUB["token"], {"code": code, "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
                                           "redirect_uri": redirect_uri, "code_verifier": pending["v"]})
    at = tok.get("access_token")
    if not at:
        raise AuthError("GitHub did not complete the sign-in")
    me = http.get_json(GITHUB["api"] + "/user", at)
    emails = http.get_json(GITHUB["api"] + "/user/emails", at)
    primary = next((m for m in emails if isinstance(m, dict) and m.get("primary") and m.get("verified")), None)
    return {"subject": me["id"], "email": (primary or {}).get("email"), "email_verified": bool(primary), "authoritative": False,
            "name": me.get("name") or me.get("login") or "", "login": me.get("login"), "owners": [me["id"]]}
