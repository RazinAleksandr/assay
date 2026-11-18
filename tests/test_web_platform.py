"""The site follows the platform (Modern Web Guidance): landmarks and a skip link, the person's colour scheme rendered by
the server, no third party, a strict CSP with Trusted Types, prefetch rules that never change state, and native
dialog / popover / invoker markup instead of scripted look-alikes."""
import json
import urllib.request

from assay.ci import Server
from test_pages import FAILURE, SUSPECT, analysis, case, folder, get, host  # noqa: F401  (host is a fixture)

COND = [{"id": "new_failed_journeys", "name": "New failed journeys", "actual": 1, "op": "=", "threshold": 0, "ok": False}]


def fetch(url: str, headers: dict | None = None) -> tuple[int, str, dict]:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {})) as r:
        return r.status, r.read().decode(), dict(r.headers)


def project(host, tmp_path):
    srv = Server(host, "tok")
    m = srv.upload(folder(tmp_path, "main", analysis([case("comment.add", "PASS")], "PASSED", [])),
                   {"project": "gh/acme/morrow", "title": "Morrow", "branch": "main", "sha": "a" * 40})
    pr = analysis([case("comment.add", "FAIL", failure=FAILURE, suspects=[SUSPECT])], "FAILED", COND,
                  changes={"regressions": ["comment.add"], "fixed": []})
    srv.upload(folder(tmp_path, "pr", pr), {"project": "gh/acme/morrow", "title": "Morrow", "branch": "feat", "pr": "3",
                                           "target": "main", "sha": "b" * 40, "reference_id": m["id"]})


def test_every_page_has_landmarks_a_skip_link_and_no_third_party(host, tmp_path):
    project(host, tmp_path)
    for path in ("/", "/projects", "/docs", "/setup", "/login", "/gh/acme/morrow", "/gh/acme/morrow?pr=3", "/gh/acme/morrow/journeys"):
        st, body, h = fetch(host + path)
        assert st == 200, path
        assert body.index('class=skip href=\'#main\'') < body.index("<header"), path            # the first stop for the keyboard
        assert "<main id=main" in body and "<footer" in body and 'aria-label="Primary"' in body, path
        assert body.count("<h1") == 1, path
        assert "<meta name=color-scheme" in body and "<meta name=text-scale content=scale>" in body, path
        assert "googleapis" not in body and "gstatic" not in body, path                          # no third party
        assert "<dialog id=lightbox closedby=any" in body, path
        csp = h["Content-Security-Policy"]
        assert "script-src 'self'" in csp and "require-trusted-types-for 'script'" in csp and "googleapis" not in csp
        assert h["Speculation-Rules"] == '"/static/speculation.json"' and h["Cross-Origin-Opener-Policy"] == "same-origin"


def test_the_colour_scheme_is_rendered_from_the_cookie(host, tmp_path):
    _, body, _ = fetch(host + "/projects")
    assert "data-theme" not in body[:200] and "content='light dark'" in body                # follows the system
    _, body, _ = fetch(host + "/projects", {"Cookie": "assay_theme=dark"})
    assert "<html lang=en data-theme=dark>" in body and "<meta name=color-scheme content='dark'>" in body
    _, body, _ = fetch(host + "/projects", {"Cookie": "assay_theme=<script>"})              # only the two known values
    assert "data-theme" not in body[:200]


def test_native_components_replace_scripted_ones(host, tmp_path):
    project(host, tmp_path)
    _, body, _ = get(f"{host}/gh/acme/morrow?pr=3")
    assert "command=show-modal commandfor=lightbox" in body and "data-zoom" not in body       # screenshots open the dialog
    assert 'loading=lazy' in body and 'aria-label="Enlarge:' in body
    _, body, _ = get(f"{host}/gh/acme/morrow")
    assert "popovertarget=scope-picker" in body and 'id=scope-picker popover' in body        # the branch picker
    assert "<details class=sel" not in body
    assert '<nav class=crumbs aria-label="Breadcrumbs"><ol>' in body and "aria-current=page" in body
    assert "<dl class=hline>" in body                                                        # the numbers are a description list
    _, body, _ = get(f"{host}/setup")
    assert 'role=tablist' in body and 'aria-selected=true' in body and "command=--copy" in body and "role=status" in body
    _, body, _ = get(f"{host}/projects")
    assert "<search class=toolbar>" in body and "<label for=q-list" in body and 'aria-pressed=false' in body


def test_prefetch_rules_never_change_state(host, tmp_path):
    st, body, h = fetch(host + "/static/speculation.json")
    rules = json.loads(body)
    assert h["Content-Type"] == "application/speculationrules+json"
    excluded = json.dumps(rules["prefetch"][0]["where"])
    for never in ("/login", "/logout", "/api/*", "*view=*", "*k=*", "*format=*"):
        assert never in excluded, never
    project(host, tmp_path)                                    # a prefetch of the view toggle does not switch the level
    _, _, h = fetch(f"{host}/gh/acme/morrow?view=agent", {"Sec-Purpose": "prefetch"})
    assert "assay_view" not in h.get("Set-Cookie", "")
    _, _, h = fetch(f"{host}/gh/acme/morrow?view=agent")
    assert "assay_view=agent" in h.get("Set-Cookie", "")


def test_fonts_are_self_hosted_and_cached(host):
    for name in ("archivo.woff2", "jetbrains-mono.woff2"):
        with urllib.request.urlopen(f"{host}/static/{name}") as r:
            body, h = r.read(), dict(r.headers)
        assert body[:4] == b"wOF2" and h["Content-Type"] == "font/woff2" and "immutable" in h["Cache-Control"], name
    with urllib.request.urlopen(f"{host}/static/OFL-Archivo.txt") as r:
        assert b"SIL Open Font License" in r.read()
    _, body, _ = fetch(host + "/projects")
    assert "rel=preload href='/static/archivo.woff2' as=font" in body
