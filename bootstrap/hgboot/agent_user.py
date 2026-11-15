"""The Forgejo user the agents work as: its account, its token, its rights, and the senders ForgeHands accepts.

Until now the agents and ForgeHands acted as the human: his login, his token. That blocks three things. Forgejo does not mail the
actor of an action, so nothing can mail the human when a human is needed. A formal review by the human on the agents' pull requests
would be a review by their author. And no record says who did what. With a user of its own everything the agents do is attributed
to it.

The shape is the one of the reference installation, done by hand there: the user is created through the admin API (no forced
password change, no notification mail, a noreply address, not an administrator, not restricted); its token carries only
write:repository, write:issue and read:user, and the admin endpoints answer 403 for it; the user is a collaborator with write on
every repository the agents may work on; and ALLOWED_SENDERS names the human first and the agent user after him, because every label
the agents set, every push, comment and CI run they cause has the agent user as its sender, and ForgeHands ignores events from
senders that are not listed.

The step is idempotent and is not wired into hgboot yet: #58 calls it before the profiles and the webhooks. The HTTP call and the
secret store are passed in, so the tests run it against fakes.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from .http import request
from .steps import StepError, log

STEP = "agent-user"
LOGIN = "homegraf-agent"
TOKEN = "FORGEJO_TOKEN"          # the stack secret ForgeHands holds and hands to every agent run
TOKEN_PREFIX = "agent-"          # every token this step makes is named so: older ones are found and deleted by it
TOKEN_SCOPES = ("write:repository", "write:issue", "read:user")
PERMISSION = "write"


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
