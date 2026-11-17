"""The Forgejo user the security review works as: its account, its read access, and the refusals that keep it independent.

A two_keys merge needs a security review that is not the author's. The agent user cannot give it: it is the author of the work it
would have to approve, and its token carries write rights. The security reviewer therefore gets a user of its own. It is created
through the admin API like the agent user (no forced password change, no notification mail, a noreply address, not an administrator,
not restricted), but its visibility is limited and it reads: it is a collaborator with read on every repository it reviews, and its
token carries read:repository and write:issue only, because the verdict is a comment on the pull request.

The step is idempotent and is not wired into hgboot yet. The HTTP call and the secret store are passed in, so the tests run it against
fakes. This module has the account and its read access; the token of the reviewer is added by the next part of the series and keeps
the signature.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

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
    """Creates the security reviewer's user and its read access, or does nothing when no reviewer is configured.

    store is the secret store: get(name) and set(name, value). It holds the token as FORGEJO_SECURITY_TOKEN in the next part.
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
    r = http("GET", f"{api}/users/{login}", basic=admin)
    if r.status == 404:
        r = http("POST", f"{api}/admin/users", basic=admin, body={
            "username": login, "email": f"{login}@noreply.{cfg.domain}", "password": secrets.token_urlsafe(32),
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
