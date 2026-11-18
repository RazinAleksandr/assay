"""Sign up, sign in, password reset and the account page: one narrow column, the providers first, then the email form.
Forms post with the request's CSRF token; fields carry the autocomplete tokens password managers expect."""
from __future__ import annotations

from urllib.parse import urlencode

from .ui import CSRF, e, icon, page_head, shell

# the providers' own marks, as their sign-in button guidelines ask
GOOGLE_MARK = ('<svg class=pmark viewBox="0 0 48 48" aria-hidden="true"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.3 0 24 0 '
               '14.6 0 6.6 5.4 2.7 13.3l7.9 6.1C12.5 13.6 17.8 9.5 24 9.5z"/><path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.7c-.6 '
               '3-2.3 5.5-4.8 7.2l7.7 6c4.5-4.2 6.9-10.3 6.9-17.7z"/><path fill="#FBBC05" d="M10.6 28.6c-.5-1.4-.8-3-.8-4.6s.3-3.2.8-4.6l-7.9-6.1C1 '
               '16.6 0 20.2 0 24s1 7.4 2.7 10.7l7.9-6.1z"/><path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.7-6c-2.1 1.4-4.9 2.3-8.2 '
               '2.3-6.2 0-11.5-4.1-13.4-9.9l-7.9 6.1C6.6 42.6 14.6 48 24 48z"/></svg>')
GITHUB_MARK = ('<svg class=pmark viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 '
               '5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 '
               '1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 '
               '0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 '
               '0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>')
MARKS = {"google": GOOGLE_MARK, "github": GITHUB_MARK}
NAMES = {"google": "Google", "github": "GitHub"}


def _csrf() -> str:
    return f'<input type=hidden name=csrf value="{e(CSRF.get())}">'


def _msg(text: str | None, kind: str = "bad") -> str:
    if not text:
        return ""
    return f'<p class="auth-msg {kind}" role={"alert" if kind == "bad" else "status"}>{e(text)}</p>'


def _providers(providers, nxt: str, verb: str) -> str:
    if not providers:
        return ""
    q = "?" + urlencode({"next": nxt}) if nxt and nxt != "/projects" else ""
    links = "".join(f'<a class="btn provider" href="/auth/{p}{e(q)}">{MARKS[p]}{verb} with {NAMES[p]}</a>' for p in providers)
    return f'<div class=auth-providers>{links}</div><p class=auth-or><span>or with your email</span></p>'


def _field(name: str, label: str, kind: str = "text", value: str = "", autocomplete: str = "", errors: dict | None = None,
           hint: str = "", focus: bool = False, extra: str = "", label_extra: str = "") -> str:
    """A labelled field; its error (or else its hint) under it, tied by aria-describedby, the field marked invalid."""
    err = (errors or {}).get(name)
    note_id = f"{name}-note"
    note = (f'<p class=field-err id={note_id}>{e(err)}</p>' if err else f'<p class=hint id={note_id}>{e(hint)}</p>' if hint else "")
    attrs = (f' aria-describedby={note_id}' if note else "") + (' aria-invalid=true' if err else "") + (" autofocus" if focus else "")
    val = f' value="{e(value)}"' if value and kind != "password" else ""
    field = (f'<input class=input id={name} name={name} type={kind}{val}{f" autocomplete={autocomplete}" if autocomplete else ""}'
             f'{" spellcheck=false" if kind in ("email", "password") else ""} required{extra}{attrs}>')
    if kind == "password":
        field = (f'<div class=pw>{field}<button type=button class=reveal data-reveal={name} aria-pressed=false '
                 f'aria-label="Show password">Show</button></div>')
    head = (f'<div class=lbl-row><label for={name}>{label}</label>{label_extra}</div>' if label_extra else f'<label for={name}>{label}</label>')
    return head + field + note


def _first(errors: dict | None, order: list[str]) -> str | None:
    return next((n for n in order if (errors or {}).get(n)), None)


def _password(name: str, label: str, autocomplete: str, hint: str = "", errors: dict | None = None) -> str:
    return _field(name, label, "password", autocomplete=autocomplete, errors=errors, hint=hint,
                  focus=_first(errors, [name]) == name, extra=" minlength=10" if hint else "")


def _page(title: str, inner: str) -> str:
    return shell(title, f'<div class=auth><div class=auth-box>{inner}</div></div>', active="auth")


def login_page(providers, error: str | None = None, info: str | None = None, nxt: str = "", email: str = "",
               errors: dict | None = None) -> str:
    focus = _first(errors, ["email", "password"]) or ("password" if email else "email")
    inner = (f'<h1>Sign in to Assay</h1><p class=auth-sub>New here? <a href="/signup{e("?" + urlencode({"next": nxt}) if nxt else "")}">Create an account</a></p>'
             + _msg(info, "ok") + _providers(providers, nxt, "Continue")
             + f'<form method=post action="/login" class=authf novalidate>{_csrf()}<input type=hidden name=next value="{e(nxt)}">'
             + _msg(error)
             + _field("email", "Email", "email", email, "username", errors, focus=focus == "email")
             + _field("password", "Password", "password", autocomplete="current-password", errors=errors, focus=focus == "password",
                      label_extra='<a href="/forgot">Forgot password?</a>')
             + '<button class="btn primary wide" type=submit>Sign in</button></form>')
    return _page("Sign in", inner)


def signup_page(providers, error: str | None = None, nxt: str = "", name: str = "", email: str = "", open_: bool = True,
                errors: dict | None = None) -> str:
    if not open_:
        inner = ('<h1>Create your Assay account</h1>' + _providers(providers, nxt, "Sign up").replace(
            '<p class=auth-or><span>or with your email</span></p>', "")
                 + '<p class=auth-sub>Sign-up with an email is closed on this server.</p>')
        return _page("Sign up", inner)
    focus = _first(errors, ["name", "email", "password"]) or "name"
    inner = (f'<h1>Create your Assay account</h1><p class=auth-sub>Already have one? <a href="/login{e("?" + urlencode({"next": nxt}) if nxt else "")}">Sign in</a></p>'
             + _providers(providers, nxt, "Sign up")
             + f'<form method=post action="/signup" class=authf novalidate>{_csrf()}<input type=hidden name=next value="{e(nxt)}">'
             + _msg(error)
             + _field("name", "Name", "text", name, "name", errors, focus=focus == "name")
             + _field("email", "Email", "email", email, "email", errors, focus=focus == "email")
             + _field("password", "Password", "password", autocomplete="new-password", errors=errors, hint="At least 10 characters.",
                      focus=focus == "password", extra=" minlength=10")
             + '<button class="btn primary wide" type=submit>Create account</button></form>'
             '<p class="auth-foot">Your projects appear here once CI uploads them. Sign in with GitHub to see your own '
             'repositories\' projects, or open a project from the link in its pull request.</p>')
    return _page("Sign up", inner)


def forgot_page(can_mail: bool, sent: bool = False, error: str | None = None) -> str:
    if not can_mail:
        inner = ('<h1>Reset your password</h1><p class=auth-sub>This server does not send email yet, so a password cannot be '
                 'reset by a link. Sign in with Google or GitHub if your account uses them, or ask the server\'s admin.</p>'
                 '<p><a href="/login">Back to sign in</a></p>')
        return _page("Reset password", inner)
    if sent:
        inner = ('<h1>Check your email</h1><p class=auth-sub>If an account uses that address, a link to set a new password is on '
                 'its way. It works for one hour, once.</p><p><a href="/login">Back to sign in</a></p>')
        return _page("Reset password", inner)
    inner = ('<h1>Reset your password</h1><p class=auth-sub>We will email you a link to set a new one.</p>' + _msg(error)
             + f'<form method=post action="/forgot" class=authf novalidate>{_csrf()}'
             '<label for=email>Email</label><input class=input id=email name=email type=email autocomplete=username spellcheck=false required autofocus>'
             '<button class="btn primary wide" type=submit>Send the link</button></form><p><a href="/login">Back to sign in</a></p>')
    return _page("Reset password", inner)


def reset_page(token: str, valid: bool, error: str | None = None) -> str:
    if not valid:
        inner = ('<h1>This link no longer works</h1><p class=auth-sub>A reset link works for one hour, once. '
                 '<a href="/forgot">Send a new one</a>.</p>')
        return _page("Reset password", inner)
    inner = ('<h1>Set a new password</h1>' + _msg(error)
             + f'<form method=post action="/reset" class=authf novalidate>{_csrf()}<input type=hidden name=t value="{e(token)}">'
             + _password("password", "New password", "new-password", "At least 10 characters.")
             + '<button class="btn primary wide" type=submit>Save and sign in</button></form>')
    return _page("Reset password", inner)


def account_page(u: dict, ids: dict, providers, projects: int, can_mail: bool, error: str | None = None,
                 info: str | None = None) -> str:
    """The signed-in person's account: profile, ways to sign in, devices, deleting it."""
    verified = ('<span class="chip pass">verified</span>' if u["email_verified"] else
                '<span class="chip warn">not verified</span>' + (
                    f'<form method=post action="/account" class=inline>{_csrf()}<input type=hidden name=do value=verify>'
                    '<button class="btn sm" type=submit>Send a verification email</button></form>' if can_mail else ""))
    methods = []
    has_pw = bool(u.get("password"))
    pw_form = (f'<form method=post action="/account" class="authf compact">{_csrf()}<input type=hidden name=do value=password>'
               + ('<label for=current>Current password</label><div class=pw><input class=input id=current name=current type=password '
                  'autocomplete=current-password required><button type=button class=reveal data-reveal=current aria-pressed=false '
                  'aria-label="Show password">Show</button></div>' if has_pw else "")
               + _password("new", "New password" if has_pw else "Password", "new-password", "At least 10 characters.")
               + f'<button class="btn" type=submit>{"Change password" if has_pw else "Set a password"}</button></form>')
    methods.append(f'<li><div class=m-h><b>Email and password</b><span class=mute>{"Set" if has_pw else "Not set"}</span></div>'
                   f'<details{" open" if not has_pw and not ids else ""}><summary>{"Change the password" if has_pw else "Set a password"}</summary>{pw_form}</details></li>')
    for p in ("google", "github"):
        if p not in ids and p not in providers:
            continue
        if p in ids:
            who = ids[p].get("login") or ids[p].get("email") or "connected"
            action = (f'<form method=post action="/account" class=inline>{_csrf()}<input type=hidden name=do value=disconnect>'
                      f'<input type=hidden name=provider value={p}><button class="btn sm" type=submit>Disconnect</button></form>')
            methods.append(f'<li><div class=m-h>{MARKS[p]}<b>{NAMES[p]}</b><span class=mute>{e(who)}</span>{action}</div></li>')
        else:
            methods.append(f'<li><div class=m-h>{MARKS[p]}<b>{NAMES[p]}</b><span class=mute>Not connected</span>'
                           f'<form method=post action="/account" class=inline>{_csrf()}<input type=hidden name=do value=connect>'
                           f'<input type=hidden name=provider value={p}><button class="btn sm" type=submit>Connect</button></form></div></li>')
    body = (page_head([("Account", None)], "Account", meta=[e(u["name"] or u["email"] or "")]) + '<div class=page><div class=acct>'
            + _msg(error) + _msg(info, "ok")
            + f'<section class=panel aria-labelledby=a-profile><div class=panel-h><h2 id=a-profile>Profile</h2></div><div class=panel-b>'
            f'<form method=post action="/account" class="authf compact row-form">{_csrf()}<input type=hidden name=do value=name>'
            f'<label for=name>Name</label><input class=input id=name name=name value="{e(u["name"] or "")}" autocomplete=name required>'
            f'<button class=btn type=submit>Save</button></form>'
            f'<p class="email-line">Email <b>{e(u["email"] or "none")}</b> {verified}</p></div></section>'
            f'<section class=panel aria-labelledby=a-methods><div class=panel-h><h2 id=a-methods>Ways to sign in</h2></div>'
            f'<ul class=methods role=list>{"".join(methods)}</ul></section>'
            f'<section class=panel aria-labelledby=a-projects><div class=panel-h><h2 id=a-projects>Projects</h2></div><div class=panel-b>'
            f'<p>{projects} project{"s" if projects != 1 else ""} in your account. <a href="/projects">Open your projects</a>.</p>'
            '<p class="mute fs-sm">A private project joins your account when you open it from the link in its pull request while '
            'signed in. With GitHub connected, the projects of your own repositories are yours too.</p></div></section>'
            f'<section class=panel aria-labelledby=a-devices><div class=panel-h><h2 id=a-devices>Devices</h2></div><div class=panel-b>'
            f'<form method=post action="/account" class=inline>{_csrf()}<input type=hidden name=do value=sessions>'
            '<button class=btn type=submit>Sign out of other devices</button></form></div></section>'
            f'<section class=panel aria-labelledby=a-delete><div class=panel-h><h2 id=a-delete>Delete the account</h2></div><div class=panel-b>'
            f'<form method=post action="/account" class=authf>{_csrf()}<input type=hidden name=do value=delete>'
            '<label class=check><input type=checkbox name=confirm value=yes required> Delete my account and its list of projects. '
            'The projects themselves stay: they belong to their repositories.</label>'
            '<button class="btn danger" type=submit>Delete the account</button></form></div></section>'
            '</div></div>')
    return shell("Account", body, active="account")


def verify_page(token: str, u: dict, valid: bool) -> str:
    if not valid:
        return _page("Confirm your email", '<h1>This link no longer works</h1><p class=auth-sub>Send a new one from '
                                           '<a href="/account">your account</a>.</p>')
    inner = (f'<h1>Confirm your email</h1><p class=auth-sub>Confirm <b>{e(u["email"] or "")}</b> as the email of this account.</p>'
             f'<form method=post action="/account" class=authf>{_csrf()}<input type=hidden name=do value=confirm>'
             f'<input type=hidden name=t value="{e(token)}"><button class="btn primary wide" type=submit>Confirm</button></form>')
    return _page("Confirm your email", inner)


def signout_page(u: dict | None) -> str:
    who = (u or {}).get("email") or (u or {}).get("name") or ""
    inner = ('<h1>Sign out of Assay</h1>' + (f'<p class=auth-sub>Signed in as <b>{e(who)}</b>.</p>' if who else "")
             + f'<form method=post action="/logout" class=authf>{_csrf()}'
             '<div class=auth-actions><button class="btn primary" type=submit>Sign out</button><a class=btn href="/projects">Cancel</a></div></form>')
    return _page("Sign out", inner)
