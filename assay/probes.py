"""State probes: read the app's state as JSON, before and after a case.

A probe is declared in the profile and works for any stack:
  http:   GET an endpoint of the app's API (shares the browser's cookies)
  sqlite: run a read-only SQL query against the app's database file
  postgres: the same against a Postgres server (a deployed stand, Supabase) through the `psql` client
  js:     evaluate an expression in the page (localStorage, IndexedDB, window hooks, game state)
  cmd:    run a shell command that prints JSON (native apps, simulators, backends)
  files:  list a directory (uploads, exports, saves)

The diff of all probes before/after drives `state`, `changed` and `allowed_changes` checks:
it catches side effects that nobody wrote an assertion for.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from .config import Profile, env_refs, expand, request_headers


class ProbeError(Exception):
    pass


def read_probe(name: str, spec: dict, profile: Profile, context=None, page=None) -> Any:
    spec = expand(spec, profile)
    unset = sorted({v for v, _ in env_refs(spec, name)})
    if unset:       # e.g. DATABASE_URL: psql would otherwise try a database literally named "{env.DATABASE_URL}"
        raise ProbeError(f"probe {name}: {', '.join(unset)} not set in the environment")
    if "postgres" in spec:
        return _shape(postgres(name, spec, profile), spec.get("as", "rows"))
    if "sqlite" in spec:
        db = Path(spec["sqlite"])
        if not db.is_absolute():
            db = profile.root / db
        if not db.exists():
            raise ProbeError(f"probe {name}: database {db} not found")
        sql = _read_only(name, spec["sql"])
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute(sql).fetchall()]
        except sqlite3.OperationalError:
            # read-only WAL databases need the -shm file; fall back to a normal (still SELECT-only) connection
            con = sqlite3.connect(str(db), timeout=5)
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute(sql).fetchall()]
        finally:
            try:
                con.close()
            except Exception:
                pass
        return _shape(rows, spec.get("as", "rows"))
    if "http" in spec:
        if context is None:
            raise ProbeError(f"probe {name}: http probe needs a browser context")
        h = spec["http"]
        method, _, path = (h if isinstance(h, str) else f"{h.get('method', 'GET')} {h['path']}").partition(" ")
        headers = h.get("headers") if isinstance(h, dict) else None     # a stand's test/admin API: a token from env
        resp = context.request.fetch(profile.url(path), method=method, timeout=15000,
                                     headers=request_headers(profile, profile.url(path), headers))
        if resp.status >= 400:
            raise ProbeError(f"probe {name}: {method} {path} → HTTP {resp.status}")
        data = resp.json()
        return _select(data, spec.get("select"))
    if "js" in spec:
        if page is None:
            raise ProbeError(f"probe {name}: js probe needs a page")
        expr = spec["js"]
        body = expr if expr.strip().startswith(("async", "(", "function")) else f"async () => ({expr})"
        return _select(page.evaluate(body), spec.get("select"))
    if "cmd" in spec:
        out = subprocess.run(spec["cmd"], shell=True, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                             cwd=str(profile.root), env={**os.environ, **profile.env})
        if out.returncode != 0:
            raise ProbeError(f"probe {name}: command failed: {out.stderr[-300:]}")
        return _select(json.loads(out.stdout), spec.get("select"))
    if "files" in spec:
        d = Path(spec["files"])
        if not d.is_absolute():
            d = profile.root / d
        if not d.exists():
            return []
        pattern = spec.get("glob", "*")
        files = sorted((p for p in d.glob(pattern) if p.is_file()), key=lambda p: p.name)
        return [{"name": p.name, "size": p.stat().st_size} for p in files]
    raise ProbeError(f"probe {name}: unknown probe type {sorted(spec)}")


def postgres(name: str, spec: dict, profile: Profile) -> list[dict]:
    """Rows of a query through the `psql` client, so Assay needs no database driver. The query is wrapped in json_agg:
    psql prints one JSON value, numbers stay numbers, NULL stays null and no separator can clash with the data.
    SELECT/WITH only, as for SQLite; what really keeps a shared database safe is a read-only role in the URL."""
    url = str(spec["postgres"] or "")
    if not url.strip():     # psql would quietly try a local database instead
        raise ProbeError(f"probe {name}: the postgres URL is empty")
    sql = re.sub(r"[\s;]+$", "", _read_only(name, spec["sql"]))     # `… order by id;` is fine inside the wrapper
    env = {"PGCONNECT_TIMEOUT": "10", **os.environ, **profile.env}
    target = url
    parts = urlsplit(url) if "://" in url else None
    if parts and parts.password is not None:            # the password goes by env, not argv, where `ps` would show it
        env["PGPASSWORD"] = unquote(parts.password)
        target = urlunsplit(parts._replace(netloc=f"{parts.netloc.rsplit('@', 1)[0].split(':', 1)[0]}@{parts.netloc.rsplit('@', 1)[1]}"))
    cmd = ["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", target,    # argv, never a shell
           "-c", f"select coalesce(json_agg(t), '[]'::json) from (\n{sql}\n) t"]   # newlines: a trailing -- comment is safe
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, env=env)
    except FileNotFoundError:
        raise ProbeError(f"probe {name}: the postgres probe needs the psql client: apt-get install postgresql-client "
                         "(Debian, Ubuntu) or brew install libpq && brew link --force libpq (macOS)") from None
    except subprocess.TimeoutExpired:
        raise ProbeError(f"probe {name}: {masked(url)} did not answer within 60 s") from None
    if out.returncode != 0:
        raise ProbeError(f"probe {name}: psql {masked(url)}: {masked(out.stderr.strip(), url)[-300:]}")
    return json.loads(out.stdout.strip() or "[]")


def masked(text: str, url: str = "") -> str:
    """`text` without the database password: probe errors go into evidence, reports and pull request comments."""
    text = re.sub(r"(://[^:/@\s]*:)\S*@", r"\1***@", text)                 # postgres://user:secret@host
    text = re.sub(r"(password\s*=\s*)('(?:[^'\\]|\\.)*'|[^&\s]*)", r"\1***", text, flags=re.I)   # ?password=… and conninfo, quoted too
    pw = urlsplit(url).password if "://" in url else None
    for p in {pw, unquote(pw)} if pw else ():         # wherever else it appears, encoded or decoded; a very short
        if len(p) > 3:                                # one would garble every word it occurs in
            text = text.replace(p, "***")
    return text


def _read_only(name: str, sql: str) -> str:
    if not re.match(r"^\s*(select|with)\b", sql, re.I):
        raise ProbeError(f"probe {name}: only SELECT queries are allowed")
    return sql


def _shape(rows: list[dict], shape: str) -> Any:
    """`as: rows` (the default), `row` (the first one or null) or `value` (its first column or null)."""
    if shape == "value":
        return next(iter(rows[0].values())) if rows else None
    if shape == "row":
        return rows[0] if rows else None
    return rows


def snapshot(profile: Profile, context=None, page=None, only: list[str] | None = None,
             account: str | None = None) -> tuple[dict, dict]:
    """Read every probe. Returns (state, errors). A probe may be scoped to journeys of an account: `when_as: mila`,
    `when_as: [mila, noah]`, or `when_as: "*"` (any signed-in journey: e.g. an API that answers 401 to visitors).
    A probe out of scope is not part of that journey's state; a probe in scope that fails is an error (undecided)."""
    state: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, spec in profile.probes.items():
        if only and name not in only:
            continue
        when_as = spec.get("when_as") if isinstance(spec, dict) else None
        if when_as and not (account if when_as == "*" else account in (when_as if isinstance(when_as, list) else [when_as])):
            continue
        needs_page = "js" in spec
        if needs_page and page is None:
            continue
        try:
            state[name] = read_probe(name, {k: v for k, v in spec.items() if k != "when_as"}, profile,
                                     context=context, page=page)
        except Exception as e:  # recorded; a check that needs it becomes undecided
            errors[name] = str(e)[:400]
    return state, errors


def _select(data: Any, path: str | None) -> Any:
    if not path:
        return data
    return get_path(data, path)


# ---- paths and diffs ------------------------------------------------------

MISSING = object()


def get_path(data: Any, path: str) -> Any:
    cur = data
    for part in _split(path):
        if isinstance(cur, dict):
            cur = cur.get(part, MISSING)
        elif isinstance(cur, list):
            if part.isdigit() or (part.startswith("-") and part[1:].isdigit()):
                i = int(part)
                cur = cur[i] if -len(cur) <= i < len(cur) else MISSING
            elif part == "length":
                cur = len(cur)
            else:
                # pluck a field from every element
                cur = [x.get(part) for x in cur if isinstance(x, dict)]
        elif part == "length" and isinstance(cur, str):
            cur = len(cur)
        else:
            return MISSING
        if cur is MISSING:
            return MISSING
    return cur


def _split(path: str) -> list[str]:
    return [p for p in path.replace("[", ".").replace("]", "").split(".") if p]


def _key_list(items: list) -> dict | None:
    """Index list items by a stable id field so inserts don't shift every index."""
    if not items or not all(isinstance(x, dict) for x in items):
        return None
    for key in ("id", "uuid", "key", "slug", "name"):
        if all(key in x for x in items) and len({json.dumps(x[key]) for x in items}) == len(items):
            return {f"{key}={x[key]}": x for x in items}
    return None


def diff(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Paths that differ between two JSON trees (added, removed or changed)."""
    if type(before) is not type(after):
        return [prefix or "$"]
    if isinstance(before, dict):
        out: list[str] = []
        for k in sorted(set(before) | set(after), key=str):
            p = f"{prefix}.{k}" if prefix else str(k)
            if k not in before or k not in after:
                out.append(p)
            else:
                out += diff(before[k], after[k], p)
        return out
    if isinstance(before, list):
        kb, ka = _key_list(before), _key_list(after)
        if kb is not None and ka is not None:
            return diff(kb, ka, prefix)
        if len(before) != len(after):
            # report changed elements by index where comparable, and the tail
            out = []
            for i in range(min(len(before), len(after))):
                out += diff(before[i], after[i], f"{prefix}[{i}]")
            out.append(f"{prefix}[len]")
            return out
        out = []
        for i, (a, b) in enumerate(zip(before, after)):
            out += diff(a, b, f"{prefix}[{i}]")
        return out
    return [] if before == after else [prefix or "$"]


def path_matches(path: str, patterns: list[str]) -> bool:
    """`posts` allows `posts`, `posts.*`, `posts[3].x`; globs allowed."""
    norm = path.replace("[", ".[")
    for pat in patterns:
        if norm == pat or norm.startswith(pat + ".") or fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(path, pat) \
                or fnmatch.fnmatch(norm, pat + ".*"):
            return True
    return False
