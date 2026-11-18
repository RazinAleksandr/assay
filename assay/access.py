"""Who may upload to the Assay server and who may look.

Uploading. A GitHub Actions job signs in with its OIDC token (`permissions: id-token: write`): no secret to configure.
The token is signed by GitHub and names the repository, so the first analysis creates the owner's account and the
project `gh/<owner>/<repo>`, and a job can only ever write to its own repository's project. The server's admin token
(ASSAY_SERVER_TOKEN) uploads to any key: other CI systems, history imports.

Looking. Projects of public repositories are public, like their code. A private project is shown to whoever holds its
view key: the links CI posts in the pull request and the job summary carry it once, the server keeps it in a cookie
and drops it from the address. Whoever can read the repository's pull requests can open its analyses; nobody else
learns that the project exists. Legacy projects (uploaded with the admin token, no visibility) stay public, as before.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.request
from dataclasses import dataclass, field

PUBLIC_SERVER = "http://46.225.104.43:8080"     # the hosted Assay service: the action's default host-url
GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_JWKS = GITHUB_ISSUER + "/.well-known/jwks"
COOKIE_ADMIN = "assay_admin"
COOKIE_PREFIX = "ak_"


class AuthError(Exception):
    """The request's credentials were missing or not accepted. The message is safe to show to the client."""


def _b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# DigestInfo of SHA-256 (RFC 8017, 9.2 note 1)
_SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def rsa_sha256_verify(n: int, e: int, message: bytes, signature: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256. The whole expected block is rebuilt and compared, nothing is parsed out of the
    decrypted signature, so the classic padding-parsing forgeries do not apply."""
    k = (n.bit_length() + 7) // 8
    if k < 256 or len(signature) != k:            # 2048-bit keys or larger
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = _SHA256_INFO + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "assay-server"}), timeout=10) as r:
        return json.loads(r.read())


class GitHubOIDC:
    """Verifies GitHub Actions OIDC tokens issued for this server (audience = the server's public URL)."""

    def __init__(self, audiences: list[str], fetch=_fetch_json, issuer: str = GITHUB_ISSUER, jwks_url: str = GITHUB_JWKS):
        self.audiences = {a.rstrip("/") for a in audiences if a}
        self.fetch, self.issuer, self.jwks_url = fetch, issuer, jwks_url
        self._keys: dict[str, tuple[int, int]] = {}
        self._fetched = 0.0
        self._lock = threading.Lock()

    def _key(self, kid: str) -> tuple[int, int] | None:
        with self._lock:
            if kid not in self._keys and time.time() - self._fetched > 60:   # new kid: refresh, at most once a minute
                self._fetched = time.time()
                keys = {}
                for k in self.fetch(self.jwks_url).get("keys", []):
                    if k.get("kty") == "RSA" and k.get("kid") and k.get("n") and k.get("e"):
                        keys[k["kid"]] = (int.from_bytes(_b64url(k["n"]), "big"), int.from_bytes(_b64url(k["e"]), "big"))
                self._keys = keys
            return self._keys.get(kid)

    def verify(self, token: str, now: float | None = None) -> dict:
        try:
            h64, p64, s64 = token.split(".")
            header, claims, sig = json.loads(_b64url(h64)), json.loads(_b64url(p64)), _b64url(s64)
        except Exception:
            raise AuthError("not a JWT") from None
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise AuthError("not a JWT")
        if header.get("alg") != "RS256":
            raise AuthError("unsupported token algorithm")
        try:
            key = self._key(str(header.get("kid") or ""))
        except Exception:                                  # GitHub's key list unreachable: refuse, do not crash
            raise AuthError("cannot fetch GitHub's signing keys") from None
        if not key or not rsa_sha256_verify(key[0], key[1], f"{h64}.{p64}".encode(), sig):
            raise AuthError("token signature is not GitHub's")
        if claims.get("iss") != self.issuer:
            raise AuthError("token is not from GitHub Actions")
        aud = claims.get("aud")
        if not ({str(a).rstrip("/") for a in (aud if isinstance(aud, list) else [aud])} & self.audiences):
            raise AuthError(f"token audience {aud!r} is not this server ({', '.join(sorted(self.audiences))})")
        now = time.time() if now is None else now
        if not isinstance(claims.get("exp"), (int, float)) or now > claims["exp"] + 30 or now < claims.get("nbf", 0) - 30:
            raise AuthError("token expired")
        if not claims.get("repository") or "/" not in claims["repository"]:
            raise AuthError("token names no repository")
        return claims


def project_key(claims: dict) -> str:
    return "gh/" + claims["repository"]


def owner_of(key: str) -> str | None:
    """`gh/<owner>/<repo>` → `gh/<owner>`; legacy keys have no owner."""
    parts = key.split("/")
    return "/".join(parts[:2]) if len(parts) == 3 else None


def cookie_name(key: str) -> str:
    return COOKIE_PREFIX + hashlib.sha256(key.encode()).hexdigest()[:12]


def parse_cookies(header: str | None) -> dict:
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


@dataclass
class Viewer:
    """What the person behind a request may see: everything (admin) or public projects plus the ones whose keys they hold."""
    admin: bool = False
    cookies: dict = field(default_factory=dict)
    repo: str | None = None          # a CI job (OIDC) sees its own project
    repo_id: str | None = None
    user: dict | None = None         # a signed-in person (accounts.py)
    keys: set = field(default_factory=set)       # private projects remembered in their account
    owners: set = field(default_factory=set)     # the numeric id of their own GitHub account

    def holds_key(self, p) -> bool:
        held = self.cookies.get(cookie_name(p["key"]))
        return bool(held and p["view_key"] and hmac.compare_digest(held.encode(), p["view_key"].encode()))

    def mine(self, p) -> bool:
        """A project of this account: remembered from a link, or of their own GitHub account."""
        if p is None:
            return False
        oid = p["owner_id"] if "owner_id" in p.keys() else None
        return p["key"] in self.keys or bool(oid and str(oid) in self.owners)

    def can_view(self, p) -> bool:
        if p is None:
            return False
        if self.admin or (p["visibility"] or "public") == "public" or self.mine(p):
            return True
        if self.repo and p["key"] == "gh/" + self.repo and (not p["repo_id"] or not self.repo_id or p["repo_id"] == self.repo_id):
            return True
        return self.holds_key(p)


def hash_password(password: str, salt: str | None = None, rounds: int = 200_000) -> str:
    """pbkdf2_sha256$<rounds>$<salt>$<hash>: what server.yaml keeps instead of the password itself."""
    import secrets as _s
    salt = salt or _s.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), rounds)
    return f"pbkdf2_sha256${rounds}${salt}${dk.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    try:
        algo, rounds, salt, _ = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        return hmac.compare_digest(hash_password(password, salt, int(rounds)).encode(), stored.encode())
    except (ValueError, TypeError):
        return False
