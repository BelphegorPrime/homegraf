"""The Forgejo user the security review works as: its account, its read access, and the refusals that keep it independent.

A two_keys merge needs a security review that is not the author's. The agent user cannot give it: it is the author of the work it
would have to approve, and its token carries write rights. The security reviewer therefore gets a user of its own. It is created
through the admin API like the agent user (no forced password change, no notification mail, a noreply address, not an administrator,
not restricted), but its visibility is limited and it reads: it is a collaborator with read on every repository it reviews, and its
token carries read:repository and write:issue only, because the verdict is a comment on the pull request.

The step is idempotent and is not wired into hgboot yet. The HTTP call and the secret store are passed in, so the tests run it against
fakes. This module has the account, its read access and its token.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from . import agent_user
from .http import request
from .steps import StepError, log

STEP = "security-user"
TOKEN = "FORGEJO_SECURITY_TOKEN"  # the stack secret the security review conversations hold
TOKEN_PREFIX = "security-"        # every token this step makes is named so: older ones are found and deleted by it
TOKEN_SCOPES = ("read:repository", "write:issue")
PERMISSION = "read"               # read only: the reviewer never writes to a repository, it comments on the pull request
VISIBILITY = "limited"            # as on the reference installation: the reviewer is not listed for everyone


@dataclass(frozen=True)
class SecuritySettings:
    """What the later steps need: the login of the security reviewer."""
    login: str


def ensure_security_user(cfg, factory, store, *, http=request, repositories=None) -> SecuritySettings | None:
    """Creates the security reviewer's user, its token and its read access, or does nothing when no reviewer is configured.

    store is the secret store: get(name) and set(name, value). It holds the token as FORGEJO_SECURITY_TOKEN.
    repositories: owner/name of every repository the reviewer may read; each must exist. None means factory.allowed_repos.
    """
    if len(factory.security_reviewers) != 1:
        log(STEP, f"no security user: {len(factory.security_reviewers)} security reviewers configured, exactly one is needed")
        return None
    if not factory.security_token:
        log(STEP, "no security user: FORGEHANDS_SECURITY_TOKEN is not set")
        return None

    login = factory.security_reviewers[0]
    for name, reason in ((cfg.forgejo_user, "the human"), (factory.human_login, "the human"),
                         (factory.agent_login, "the agent user")):
        if name and login.casefold() == name.casefold():
            raise StepError(f"the security user must not be {reason} {name!r}: the security review needs an account of its own")

    api, admin = cfg.forgejo_url + "/api/v1", (cfg.forgejo_user, cfg.admin_password)
    password = ""
    r = http("GET", f"{api}/users/{login}", basic=admin)
    if r.status == 404:
        password = secrets.token_urlsafe(32)
        r = http("POST", f"{api}/admin/users", basic=admin, body={
            "username": login, "email": f"{login}@noreply.{cfg.domain}", "password": password,
            "must_change_password": False, "send_notify": False, "visibility": VISIBILITY, "admin": False,
            "restricted": False})
        if not r.ok:
            raise StepError(f"Forgejo refused to create the user {login}: {r.status}")
        log(STEP, f"user {login}: created (no forced password change, no mail, not an administrator, not restricted)")
    elif not r.ok:
        raise StepError(f"Forgejo answered {r.status} for the user {login}")
    elif (r.json() or {}).get("is_admin"):
        raise StepError(f"the user {login} is an administrator: the security review must not work with an administrator's rights")
    else:
        log(STEP, f"user {login}: already there")

    token = store.get(TOKEN) or factory.security_token
    if token and agent_user._works(http, api, token, login):
        log(STEP, "token: already there")
    else:
        if not password:
            password = agent_user._reset_password(http, api, login, admin)
        # before the token is stored: a failed web step must be retried by the next run, which still has no token then
        agent_user._notifications_off(cfg.forgejo_url, login, password)
        name, token = _create_token(http, api, login, password)
        store.set(TOKEN, token)                     # stored before the older one goes: a lost write would leave no token at all
        _delete_older(http, api, login, password, name)
        log(STEP, f"token: created (scopes {', '.join(TOKEN_SCOPES)})")

    if not agent_user._works(http, api, token, login):
        raise StepError(f"the token of {login} does not answer GET /user as {login}")
    r = http("GET", f"{api}/admin/users", headers={"Authorization": f"token {token}"})
    if r.status not in (401, 403):
        raise StepError(f"the token of {login} can read the administrator endpoints ({r.status}): it has more rights than "
                        f"{', '.join(TOKEN_SCOPES)}")

    for repo in factory.allowed_repos if repositories is None else repositories:
        r = http("GET", f"{api}/repos/{repo}", basic=admin)
        if r.status == 404:
            raise StepError(f"Forgejo has no repository {repo}: the security review cannot read it")
        if not r.ok:
            raise StepError(f"Forgejo answered {r.status} for the repository {repo}")
        r = http("PUT", f"{api}/repos/{repo}/collaborators/{login}", basic=admin, body={"permission": PERMISSION})
        if not r.ok:
            raise StepError(f"Forgejo refused to make {login} a collaborator of {repo}: {r.status}")
        log(STEP, f"{login} is a collaborator of {repo} ({PERMISSION})")

    return SecuritySettings(login=login)


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
