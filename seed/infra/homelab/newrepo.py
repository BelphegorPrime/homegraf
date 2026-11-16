"""The `new-repo` command: create a Forgejo repository with the agent files and the factory wiring.

The work has three parts. The profile (infra/homelab/repoprofiles.py) says which files a new repository starts
with and renders them; the Forgejo side (bootstrap/hgboot/new_repo.py) creates the repository, adds the agent as a
collaborator, reconciles the labels and the ForgeHands webhook, commits the files and reads everything back. This
command only collects the inputs, prints the plan and reports what the Forgejo side verified.

Agent mode (homelab.py): every input comes from a flag, a default or the environment, never from a prompt. Without
--yes the command stops before the first request and prints the plan; exit 0. With --yes it creates the repository
and exits 0 only when the read-back verification passed. A rerun resumes: the Forgejo side creates nothing twice
and the webhook fingerprint in STATE_DIR (default ~/.local/state/homegraf) keeps it from recreating the hook.

Environment: FORGEJO_TOKEN (operator token, write access; never printed), FORGEJO_URL (the server; default: the host
of `origin`), FORGEHANDS_FORGEJO_WEBHOOK_URL (the endpoint Forgejo calls, e.g.
http://forgehands:8080/webhooks/forgejo) and FORGEHANDS_FORGEJO_WEBHOOK_SECRET. A missing one fails with exit 1 and
names it.
"""
from __future__ import annotations

import contextlib
import importlib
import json
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from types import SimpleNamespace

import forgejo
import repoprofiles
import ui

HERE = pathlib.Path(__file__).resolve().parent
CODE = HERE.parents[1]                                          # where the tools are (seed/, or /opt/homegraf in the image)
ROOT = pathlib.Path(os.environ.get("HOMELAB_ROOT") or CODE)     # the repository they work on
TOKEN_ENV = "FORGEJO_TOKEN"
SERVER_ENV = "FORGEJO_URL"
ENDPOINT_ENV = "FORGEHANDS_FORGEJO_WEBHOOK_URL"
SECRET_ENV = "FORGEHANDS_FORGEJO_WEBHOOK_SECRET"
STATE_ENV = "STATE_DIR"
DEFAULT_AGENT = "openhands"
STATE_FILE = "new-repo.json"

_ENV_HELP = {
    TOKEN_ENV: "a Forgejo token with write access to the repository and its pull requests",
    ENDPOINT_ENV: "the ForgeHands webhook endpoint Forgejo calls, e.g. http://forgehands:8080/webhooks/forgejo",
    SECRET_ENV: "the secret of the ForgeHands webhook (FORGEHANDS_FORGEJO_WEBHOOK_SECRET in .env)",
}

_HGBOOT = None


@dataclass(frozen=True)
class Plan:
    """The collected inputs and the request to the Forgejo side; nothing has left the machine."""
    request: object                        # hgboot.new_repo.NewRepository
    module: object                         # hgboot.new_repo (its LABELS and AGENT_EVENTS are the plan's)
    profile: repoprofiles.Profile
    server: str
    visibility: str
    description: str


def _setting(name: str) -> str:
    value = os.environ.get(name) or ""
    if not value.strip():
        raise SystemExit(f"new-repo: {name} is not set ({_ENV_HELP[name]})")
    return value.strip()


def _origin(needed: str) -> tuple[str, str, str]:
    """(scheme, host, owner/repository) of the `origin` remote of the repository the tools work on."""
    result = subprocess.run(["git", "-C", str(ROOT), "remote", "get-url", "origin"], capture_output=True, text=True)
    url = result.stdout.strip()
    if result.returncode != 0 or not url:
        raise SystemExit(f"new-repo: the repository at {ROOT} has no git remote 'origin': {needed}")
    try:
        return forgejo.parse_remote(url)
    except SystemExit as exc:
        raise SystemExit(f"new-repo: {exc}") from None


def _server() -> str:
    if os.environ.get(SERVER_ENV, "").strip():
        return os.environ[SERVER_ENV].strip().rstrip("/")
    scheme, host, _ = _origin(f"set {SERVER_ENV}")
    return f"{scheme if scheme in ('http', 'https') else 'https'}://{host}"


def _owner() -> str:
    return _origin("pass --owner")[2].split("/")[0]


def _hgboot():
    """The bootstrap's hgboot package (slice 2), from the repository next to the tools or from the image.

    The tools are copied out of seed/ into the homegraf image, which carries hgboot next to them (/opt/homegraf);
    a seed-only checkout has no bootstrap and gets a message that says where it belongs. The package is imported as
    `hgboot`, not as `bootstrap.hgboot`: the tools already import infra/infisical/bootstrap.py as `bootstrap`.
    """
    global _HGBOOT
    if _HGBOOT is None:
        for base in (HERE.parents[1], HERE.parents[2] / "bootstrap"):
            if not (base / "hgboot").is_dir():
                continue
            if str(base) not in sys.path:
                sys.path.insert(0, str(base))
            try:
                _HGBOOT = importlib.import_module("hgboot.new_repo")
                break
            except ModuleNotFoundError as exc:
                if exc.name and exc.name.startswith("hgboot"):
                    continue
                raise
        if _HGBOOT is None:
            raise SystemExit("new-repo: the bootstrap's hgboot (bootstrap/hgboot) is not next to the tools; run the "
                             "command from the homegraf repository or the homegraf image")
    return _HGBOOT


def _state():
    home = os.environ.get("HOME") or str(pathlib.Path.home())
    directory = pathlib.Path(os.environ.get(STATE_ENV) or (pathlib.Path(home) / ".local/state/homegraf"))
    module = _hgboot()
    state = importlib.import_module(f"{module.__package__}.state")
    return state.State(directory / STATE_FILE)


def collect(preset: dict) -> Plan:
    """Validate the flags and the environment and render the profile; no request leaves the machine."""
    try:
        profiles = repoprofiles.catalog()
    except repoprofiles.ProfileError as exc:
        raise SystemExit(f"new-repo: {exc}") from None
    names = ", ".join(sorted(profiles))
    profile_name = str(preset.get("profile") or "").strip()
    if not profile_name:
        raise SystemExit(f"new-repo: --profile is required (one of {names})")
    if profile_name not in profiles:
        raise SystemExit(f"new-repo: unknown profile {profile_name!r}; the profiles are {names}")
    profile = profiles[profile_name]

    name = str(preset.get("name") or "").strip()
    if not name:
        raise SystemExit("new-repo: --name is required")
    owner = str(preset.get("owner") or "").strip() or _owner()
    branch = str(preset.get("branch") or "main").strip()
    agent = str(preset.get("agent_login") or "").strip() or DEFAULT_AGENT
    visibility = preset.get("visibility") or "private"
    description = preset.get("description")
    description = profile.description if description is None else str(description)
    if not description.strip():
        raise SystemExit("new-repo: --description must not be empty")

    try:
        files = repoprofiles.render(profile, {"name": name, "description": description, "owner": owner,
                                              "default_branch": branch, "validate_command": profile.validate_command})
    except repoprofiles.ProfileError as exc:
        raise SystemExit(f"new-repo: {exc}") from None

    server = _server()
    module = _hgboot()
    try:
        request = module.NewRepository(
            SimpleNamespace(forgejo_url=server), _state(), token=_setting(TOKEN_ENV), owner=owner, name=name,
            description=description, visibility=visibility, default_branch=branch, agent_login=agent, files=files,
            forgehands_endpoint=_setting(ENDPOINT_ENV), webhook_secret=_setting(SECRET_ENV))
    except ValueError as exc:
        raise SystemExit(f"new-repo: {exc}") from None
    return Plan(request=request, module=module, profile=profile, server=server, visibility=visibility,
                description=description)


@contextlib.contextmanager
def _quiet():
    """The Forgejo side logs its steps to stdout; a caller's report must stay machine-readable."""
    previous = os.environ.get("HGBOOT_QUIET")
    os.environ["HGBOOT_QUIET"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("HGBOOT_QUIET", None)
        else:
            os.environ["HGBOOT_QUIET"] = previous


def _plan_json(plan: Plan) -> dict:
    return {"applied": False, "repository": plan.request.full_name, "visibility": plan.visibility,
            "description": plan.description, "default_branch": plan.request.branch,
            "agent_login": plan.request.agent_login, "profile": plan.profile.name,
            "files": sorted(plan.request.files), "labels": sorted(plan.module.LABELS),
            "hook": {"url": plan.request.endpoint, "events": list(plan.module.AGENT_EVENTS)}}


def _result_json(result) -> dict:
    return {"applied": True, "repository": result.repository, "repository_status": result.repository_status,
            "collaborator": result.collaborator, "collaborator_status": result.collaborator_status,
            "default_branch": result.default_branch, "commit": result.commit, "files": list(result.files),
            "labels_created": list(result.labels_created), "labels_updated": list(result.labels_updated),
            "hook_status": result.hook_status, "hook_events": list(result.hook_events)}


def _print_plan(plan: Plan) -> None:
    request = plan.request
    print(f"new repository {request.full_name} ({plan.visibility}) on {plan.server}")
    print(f"  description   {plan.description}")
    print(f"  branch        {request.branch}")
    print(f"  collaborator  {request.agent_login} with write")
    print(f"  profile       {plan.profile.name}: {len(request.files)} files")
    for path in sorted(request.files):
        print(f"    {path}")
    print(f"  labels        {len(plan.module.LABELS)}: {', '.join(sorted(plan.module.LABELS))}")
    print(f"  webhook       {request.endpoint}")
    print(f"                {', '.join(plan.module.AGENT_EVENTS)}")
    print("\nNothing was created: rerun with --yes to create the repository and its first commit.")


def _print_result(result) -> None:
    print(f"\nnew repository {result.repository}: {result.repository_status}")
    print(f"  collaborator  {result.collaborator}: {result.collaborator_status}")
    print(f"  branch        {result.default_branch}")
    print(f"  commit        {result.commit}")
    print(f"  labels        {len(result.labels_created)} created, {len(result.labels_updated)} updated")
    print(f"  webhook       {result.hook_status} ({', '.join(result.hook_events)})")
    print(f"  files         {', '.join(result.files)}")
    ui.ok("read back: repository, collaborator, labels, webhook, branch and files")


def run(preset: dict) -> bool:
    """Plan without --yes; create and verify with it. True means the command succeeded (exit 0)."""
    plan = collect(preset)
    if not ui.ASSUME_YES:
        if preset.get("json"):
            print(json.dumps(_plan_json(plan), indent=2, sort_keys=True))
        else:
            _print_plan(plan)
        return True
    try:
        with _quiet():
            result = plan.request.run()
    except (plan.module.StepError, ValueError) as exc:
        raise SystemExit(f"new-repo: {exc}") from None
    if preset.get("json"):
        print(json.dumps(_result_json(result), indent=2, sort_keys=True))
    else:
        _print_result(result)
    return True
