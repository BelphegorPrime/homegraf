"""The Forgejo user the agents work as: its account, its token, its rights, and the senders ForgeHands accepts.

Until now the agents and ForgeHands acted as the human: his login, his token. That blocks three things. Forgejo does not mail the
actor of an action, so nothing can mail the human when a human is needed. A formal review by the human on the agents' pull requests
would be a review by their author. And no record says who did what. With a user of its own everything the agents do is attributed
to it.

The shape is the one of the reference installation, done by hand there: the user is created through the admin API (no forced
password change, no notification mail, a noreply address, not an administrator, not restricted); its mail notification
preference is turned off, because Forgejo mails the participants of a pull request and the noreply address cannot deliver (every
such mail bounces to the operator); its token carries only write:repository, write:issue and read:user, and the admin endpoints
answer 403 for it; the user is a collaborator with write on every repository the agents may work on; and ALLOWED_SENDERS names the
human first and the agent user after him, because every label the agents set, every push, comment and CI run they cause has the
agent user as its sender, and ForgeHands ignores events from senders that are not listed.

The step is idempotent and is not wired into hgboot yet: #58 calls it before the profiles and the webhooks. The HTTP call and the
secret store are passed in, so the tests run it against fakes.
"""
from __future__ import annotations

import re
import secrets
import urllib.parse
from dataclasses import dataclass

from .http import request
from .steps import StepError, log

STEP = "agent-user"
LOGIN = "homegraf-agent"
TOKEN = "FORGEJO_TOKEN"          # the stack secret ForgeHands holds and hands to every agent run
TOKEN_PREFIX = "agent-"          # every token this step makes is named so: older ones are found and deleted by it
TOKEN_SCOPES = ("write:repository", "write:issue", "read:user")
PERMISSION = "write"
ACCOUNT = "/user/settings/account"
FORM = "application/x-www-form-urlencoded"
PREFERENCE = "disabled"          # its noreply address cannot deliver, so every notification mail would bounce to the operator
_CSRF = (re.compile(r'name="_csrf"[^>]*value="([^"]+)"'), re.compile(r'value="([^"]+)"[^>]*name="_csrf"'))
_PREFERENCE_SELECT = re.compile(r'<select\b[^>]*name="preference"[^>]*>(.*?)</select>', re.IGNORECASE | re.DOTALL)
_OPTION = re.compile(r"<option\b[^>]*>", re.IGNORECASE)
_VALUE = re.compile(r'value="([^"]*)"', re.IGNORECASE)


@dataclass(frozen=True)
class AgentSettings:
    """What the later steps need: the agent login and ALLOWED_SENDERS as ForgeHands takes it (the human first)."""
    login: str
    allowed_senders: str


def ensure_agent_user(cfg, store, *, http=request, human=None, rotate=False, login=LOGIN,
                      repositories=()) -> AgentSettings:
    """Creates the agent user, its token and its collaborations, and returns the settings the later steps need.

    store is the secret store: get(name) and set(name, value). It holds the token as FORGEJO_TOKEN; a token there that does not
    answer GET /user with the agent login is replaced. rotate: make a new token even if the old one still works.
    repositories: owner/name of every repository the agents may work on; each must exist.
    """
    if login == cfg.forgejo_user:
        raise StepError(f"the agent user must not be the human {login!r}: the agents need an account of their own")
    api, admin = cfg.forgejo_url + "/api/v1", (cfg.forgejo_user, cfg.admin_password)
    human = human or cfg.forgejo_user

    password = ""
    r = http("GET", f"{api}/users/{login}", basic=admin)
    if r.status == 404:
        password = secrets.token_urlsafe(32)
        r = http("POST", f"{api}/admin/users", basic=admin, body={
            "username": login, "email": f"{login}@noreply.{cfg.domain}", "password": password, "must_change_password": False,
            "send_notify": False, "visibility": "private", "admin": False, "restricted": False})
        if not r.ok:
            raise StepError(f"Forgejo refused to create the user {login}: {r.status}")
        log(STEP, f"user {login}: created (no forced password change, no mail, not an administrator, not restricted)")
    elif not r.ok:
        raise StepError(f"Forgejo answered {r.status} for the user {login}")
    elif (r.json() or {}).get("is_admin"):
        raise StepError(f"the user {login} is an administrator: the agents must not work with an administrator's rights")
    else:
        log(STEP, f"user {login}: already there")

    token = "" if rotate else (store.get(TOKEN) or "")
    if token and _works(http, api, token, login):
        log(STEP, f"token of {login}: already there")
    else:
        if not password:
            password = _reset_password(http, api, login, admin)
        # before the token is stored: a failed web step must be retried by the next run, which still has no token then
        _notifications_off(http, cfg.forgejo_url, login, password)
        name, token = _create_token(http, api, login, password)
        store.set(TOKEN, token)                     # stored before the older one goes: a lost write would leave no token at all
        _delete_older(http, api, login, password, name)
        log(STEP, f"token of {login}: created (scopes {', '.join(TOKEN_SCOPES)})")
    if rotate:
        log(STEP, f"token of {login}: rotated")

    if not _works(http, api, token, login):
        raise StepError(f"the token of {login} does not answer GET /user as {login}")

    r = http("GET", f"{api}/admin/users", headers={"Authorization": f"token {token}"})
    if r.status not in (401, 403):
        raise StepError(f"the token of {login} can read the administrator endpoints ({r.status}): it has more rights than "
                        f"{', '.join(TOKEN_SCOPES)}")

    for repo in repositories:
        r = http("GET", f"{api}/repos/{repo}", basic=admin)
        if r.status == 404:
            raise StepError(f"Forgejo has no repository {repo}: the agents cannot be a collaborator of it")
        if not r.ok:
            raise StepError(f"Forgejo answered {r.status} for the repository {repo}")
        r = http("PUT", f"{api}/repos/{repo}/collaborators/{login}", basic=admin, body={"permission": PERMISSION})
        if not r.ok:
            raise StepError(f"Forgejo refused to make {login} a collaborator of {repo}: {r.status}")
        log(STEP, f"{login} is a collaborator of {repo} ({PERMISSION})")

    senders = ", ".join(dict.fromkeys((human, login)))       # the human first: he is the one a run reports to
    log(STEP, f"ALLOWED_SENDERS: {senders}")
    return AgentSettings(login=login, allowed_senders=senders)


def _works(http, api: str, token: str, login: str) -> bool:
    r = http("GET", f"{api}/user", headers={"Authorization": f"token {token}"})
    return r.ok and (r.json() or {}).get("login") == login


def _reset_password(http, api: str, login: str, admin: tuple[str, str]) -> str:
    """A user that already existed gets a random password through the admin API: a token is created with basic auth, and the password
    is never stored."""
    password = secrets.token_urlsafe(32)
    r = http("PATCH", f"{api}/admin/users/{login}", basic=admin,
             body={"password": password, "must_change_password": False, "send_notify": False})
    if not r.ok:
        raise StepError(f"Forgejo refused to reset the password of {login}: {r.status}")
    return password


def _create_token(http, api: str, login: str, password: str) -> tuple[str, str]:
    """A token under a name of its own (Forgejo shows a token once, so an older name is not reused)."""
    name = TOKEN_PREFIX + secrets.token_hex(4)
    r = http("POST", f"{api}/users/{login}/tokens", basic=(login, password), body={"name": name, "scopes": list(TOKEN_SCOPES)})
    if not r.ok:
        raise StepError(f"Forgejo refused to create a token for {login}: {r.status}")
    return name, r.json()["sha1"]


def _delete_older(http, api: str, login: str, password: str, keep: str) -> None:
    """Deletes the tokens of this step that the new one replaces: Forgejo shows a token once, so one whose value is lost is useless."""
    r = http("GET", f"{api}/users/{login}/tokens", basic=(login, password))
    if not r.ok:
        raise StepError(f"Forgejo refused to list the tokens of {login}: {r.status}")
    older = [t["name"] for t in r.json() or [] if t["name"].startswith(TOKEN_PREFIX) and t["name"] != keep]
    for old in older:
        r = http("DELETE", f"{api}/users/{login}/tokens/{old}", basic=(login, password))
        if not r.ok:
            raise StepError(f"Forgejo refused to delete the older token {old} of {login}: {r.status}")
    if older:
        log(STEP, f"token of {login}: the older one is deleted")


def _notifications_off(http, web: str, login: str, password: str) -> None:
    """Turns the mail notifications of the agent user off: Forgejo mails the participants of a pull request, and its noreply address
    cannot deliver, so every such mail bounces to the operator. There is no API for the preference; the account page and its form
    do it, and the page is read back. Needs the password, so it happens in the run that created or reset it; the cookie jar stays
    in memory, and neither the password nor a cookie goes into a log line or an error."""
    jar: dict[str, str] = {}
    page = _sign_in(http, web, login, password, jar)
    if _preference(page) == PREFERENCE:
        log(STEP, f"user {login}: mail notifications already off")
        return
    r = _web(http, web, "POST", ACCOUNT + "/email", jar,
             form={"_csrf": _csrf(page, login), "_method": "NOTIFICATION", "preference": PREFERENCE})
    if r.status not in (200, 302, 303):
        raise StepError(f"Forgejo refused to turn off the mail notifications of {login}: {r.status}")
    page = _account(http, web, login, jar)
    if _preference(page) != PREFERENCE:
        raise StepError(f"Forgejo accepted the account form of {login} but its mail notifications are not {PREFERENCE}")
    log(STEP, f"user {login}: mail notifications {PREFERENCE} (the noreply address does not deliver)")


def _sign_in(http, web: str, login: str, password: str, jar: dict[str, str]) -> str:
    """A web session as the agent user, and the account page it answers; its csrf token comes from the sign-in form."""
    r = _web(http, web, "GET", "/user/login", jar)
    if not r.ok:
        raise StepError(f"Forgejo answered {r.status} for the web sign-in of {login}: the mail notifications cannot be turned off")
    r = _web(http, web, "POST", "/user/login", jar,
             form={"user_name": login, "password": password, "_csrf": _csrf(r.text, login)})
    if r.status not in (302, 303):
        raise StepError(f"Forgejo refused the web sign-in of {login} ({r.status}): the mail notifications cannot be turned off")
    return _account(http, web, login, jar)


def _account(http, web: str, login: str, jar: dict[str, str]) -> str:
    r = _web(http, web, "GET", ACCOUNT, jar)
    if not r.ok:
        raise StepError(f"Forgejo answered {r.status} for the account page of {login}: the web session is not accepted")
    return r.text


def _web(http, web: str, method: str, path: str, jar: dict[str, str], *, form: dict | None = None):
    """One request of the web session: the cookies of the jar go in, the ones of the answer come out. A redirect is an answer of
    its own here, because that is where the sign-in sets the session cookie."""
    headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in jar.items())} if jar else {}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form)
        headers["Content-Type"] = FORM
    r = http(method, web + path, headers=headers, body=body, redirect=False)
    for line in r.header("set-cookie").split("\n"):
        name, sep, value = line.split(";", 1)[0].strip().partition("=")
        if sep and name.strip():
            jar[name.strip()] = value.strip()
    return r


def _csrf(page: str, login: str) -> str:
    for pattern in _CSRF:
        found = pattern.search(page)
        if found:
            return found.group(1)
    raise StepError(f"the web form of {login} carries no csrf token: the mail notifications cannot be turned off")


def _preference(page: str) -> str | None:
    """The selected option of the notification preference on the account page (the page renders `selected` on it). The page has
    other selects (language, theme), so only the one named preference counts."""
    select = _PREFERENCE_SELECT.search(page)
    if not select:
        return None
    for tag in _OPTION.findall(select.group(1)):
        if re.search(r"\bselected\b", tag, re.IGNORECASE):
            value = _VALUE.search(tag)
            return value.group(1) if value else None
    return None
