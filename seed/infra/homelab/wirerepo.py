"""The `--wire` step of `new-repo`: tell ForgeHands about a new repository, as a pull request.

The setting lives in the operator's own repository, in the compose file of the stack that runs ForgeHands: `ALLOWED_REPOS`
lists the repositories the agents may work on and `MERGE_POLICY` says what may be merged automatically. A repository
without a policy entry is never merged automatically. This module edits the text of that one file (comments and layout
stay), and the delivery goes through the pull-request client (ops.start_branch/open_pull_request, forgejo.py), like
`new-app --pr`: nothing is deployed here.

The compose file is found, not named: exactly one stack under stacks/ may run ForgeHands (the image
`ghcr.io/belphegorprime/forgehands`); none or several is an error that names what was found. Both the mapping form
(`ALLOWED_REPOS: a,b`, `MERGE_POLICY: '{...}'`) and the environment-list form (`- ALLOWED_REPOS=a,b`,
`- 'MERGE_POLICY={...}'`) are edited in place. A value that is a compose variable (`${FORGEHANDS_ALLOWED_REPOS:-}`) is
refused: adding a literal there would not reach ForgeHands when the variable is set.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from dataclasses import dataclass

import forgejo
import ops
import ui

FORGEHANDS_IMAGE = "ghcr.io/belphegorprime/forgehands"
DEFAULT_DENY = (".forgejo/**", ".github/**", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", ".agents/**", ".claude/**",
                "compose.yaml", "**/compose.yaml", "setup.sh", "scripts/**")
TOKEN_ENV = "FORGEJO_TOKEN"

_IMAGE = re.compile(r"^[ \t]*(?:-[ \t]+)?image:[ \t]*(?P<value>.*?)[ \t]*$")
_ENTRY = re.compile(r"^(?P<lead>[ \t]*)(?P<item>-[ \t]+)?(?P<q>['\"]?)(?P<name>ALLOWED_REPOS|MERGE_POLICY)"
                    r"(?P<sep>:[ \t]*|=)(?P<value>.*)$")
_COMMENT = re.compile(r"[ \t]+#")


@dataclass(frozen=True)
class Wiring:
    """What the edit of one compose file changes; `edited == text` means the repository is already wired."""
    path: pathlib.Path
    text: str
    edited: str
    repository: str
    allowed_here: bool             # ALLOWED_REPOS already listed the repository
    policy_here: bool              # MERGE_POLICY already had an entry

    @property
    def changed(self) -> bool:
        return self.edited != self.text

    @property
    def stack(self) -> str:
        return self.path.parent.name


def _stacks(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted({p for p in (root / "stacks").glob("*/compose.yaml")} | {p for p in (root / "stacks").glob("*/compose.yml")})


def _runs_forgehands(path: pathlib.Path) -> bool:
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _IMAGE.match(line)
        if m and FORGEHANDS_IMAGE in m["value"]:
            return True
    return False


def locate(root: pathlib.Path) -> pathlib.Path:
    """The one stack compose file that runs ForgeHands; none or several is an error that names what was found."""
    stacks = _stacks(root)
    if not stacks:
        raise SystemExit(f"new-repo --wire: no stacks/<id>/compose.yaml in {root}: there is nothing to wire ForgeHands in")
    found = [p for p in stacks if _runs_forgehands(p)]
    if len(found) == 1:
        return found[0]
    if found:
        names = ", ".join(str(p.relative_to(root)) for p in found)
        raise SystemExit(f"new-repo --wire: several stacks run ForgeHands ({FORGEHANDS_IMAGE}): {names}; keep one")
    names = ", ".join(p.parent.name for p in stacks)
    raise SystemExit(f"new-repo --wire: none of the {len(stacks)} stacks runs ForgeHands ({FORGEHANDS_IMAGE}): {names}")


def _split(value: str) -> tuple[str, str]:
    """(value, the rest of the line) of one YAML entry; the rest is a trailing comment, kept exactly as written.
    A quoted value ends at its closing quote, so a `#` inside the JSON stays part of the value."""
    stripped = value.rstrip()
    if stripped[:1] in ("'", '"'):
        end = stripped.find(stripped[0], 1)
        if end != -1 and (not stripped[end + 1:].strip() or stripped[end + 1:].lstrip().startswith("#")):
            return stripped[:end + 1], value[end + 1:]
    m = _COMMENT.search(stripped)
    if m:
        return stripped[:m.start()].rstrip(), value[len(stripped[:m.start()].rstrip()):]
    return stripped, value[len(stripped):]


def _unquote(value: str) -> tuple[str, str]:
    return (value[0], value[1:-1]) if len(value) > 1 and value[0] in "'\"" and value[-1] == value[0] else ("", value)


def _refuse_variable(value: str, name: str) -> None:
    if "${" in value:
        raise ValueError(f"{name} is a compose variable ({value}); it must name the repositories and the policy itself, "
                         "or setting the variable would hide the entry")


def _add_repository(value: str, repository: str) -> str | None:
    """The comma list with the repository appended, or None when it is already there."""
    _refuse_variable(value, "ALLOWED_REPOS")
    parts = [p.strip() for p in value.split(",")]
    if repository in parts:
        return None
    separator = ", " if ", " in value else ","
    return (value + separator + repository) if value.strip() else repository


def _add_policy(value: str, repository: str, quoted: bool = False) -> str | None:
    """The JSON object with the repository's default deny entry, in the quoting the line already uses; None if present.
    `quoted` is the quote of an environment-list item (`- 'MERGE_POLICY={...}'`), which surrounds the whole item."""
    _refuse_variable(value, "MERGE_POLICY")
    quote, bare = _unquote(value)
    try:
        policy = json.loads(bare)
    except ValueError:
        raise ValueError(f"MERGE_POLICY is not valid JSON: {value}") from None
    if not isinstance(policy, dict):
        raise ValueError("MERGE_POLICY must be a JSON object of repository -> {deny: [globs]}")
    if repository in policy:
        return None
    policy[repository] = {"deny": list(DEFAULT_DENY)}
    dumped = json.dumps(policy, separators=(",", ":"), ensure_ascii=False)
    if quoted:
        return dumped
    return f"{quote}{dumped}{quote}" if quote else f"'{dumped}'"


def edit(text: str, repository: str) -> tuple[str, bool, bool]:
    """The compose text with the repository in ALLOWED_REPOS and MERGE_POLICY. (text, allowed_here, policy_here);
    allowed_here/policy_here say whether the entry was already there. Everything else stays byte for byte."""
    lines = text.splitlines(keepends=True)
    seen: dict[str, int] = {}
    already: dict[str, bool] = {}
    for number, line in enumerate(lines):
        body = line.rstrip("\r\n")
        m = _ENTRY.match(body)
        if not m or m["name"] in seen:
            continue
        name = m["name"]
        seen[name] = number
        value, rest = _split(m["value"])
        quote = m["q"]
        if quote:
            if value.rstrip().endswith(quote):
                value = value.rstrip()[:-1]
            else:
                quote = ""
        replace = (_add_policy(value, repository, quoted=bool(quote)) if name == "MERGE_POLICY"
                   else _add_repository(value, repository))
        already[name] = replace is None
        if replace is not None:
            lines[number] = f"{m['lead']}{m['item'] or ''}{quote}{name}{m['sep']}{replace}{quote}{rest}{line[len(body):]}"
    missing = [name for name in ("ALLOWED_REPOS", "MERGE_POLICY") if name not in seen]
    if missing:
        raise ValueError(f"{' and '.join(missing)} not found: the stack's compose file must set it")
    return "".join(lines), already["ALLOWED_REPOS"], already["MERGE_POLICY"]


def plan(repository: str, root: pathlib.Path | None = None) -> Wiring:
    """The edit of the ForgeHands stack in `root` (default: the repository the tools work on); nothing is written."""
    root = pathlib.Path(root or ops.ROOT)
    path = locate(root)
    text = path.read_text()
    try:
        edited, allowed, policy = edit(text, repository)
    except ValueError as exc:
        raise SystemExit(f"new-repo --wire: {path.relative_to(root)}: {exc}") from None
    return Wiring(path=path, text=text, edited=edited, repository=repository, allowed_here=allowed, policy_here=policy)


def body(wiring: Wiring) -> str:
    added = [f"- `{wiring.path.relative_to(ops.ROOT).as_posix()}`: the stack that runs ForgeHands"]
    if wiring.allowed_here:
        added.append(f"- `ALLOWED_REPOS`: `{wiring.repository}` is already listed; nothing to add")
    else:
        added.append(f"- `ALLOWED_REPOS`: add `{wiring.repository}`")
    if wiring.policy_here:
        added.append(f"- `MERGE_POLICY`: `{wiring.repository}` already has an entry; nothing to add")
    else:
        added.append(f"- `MERGE_POLICY`: add `{wiring.repository}` with the default deny list: "
                     + ", ".join(f"`{glob}`" for glob in DEFAULT_DENY))
    return "\n".join([
        f"Wire `{wiring.repository}` into ForgeHands, proposed by `./homelab.py new-repo --wire`.", "",
        *added, "",
        f"Without an entry in `MERGE_POLICY` the repository is never merged automatically; this entry is the default, "
        f"so keep it or remove it as you decide.",
        "Nothing is deployed by this command: the merge applies the change (the push deploys the stack)."])


def deliver(repository: str, client=None) -> str | None:
    """Open (or find) the pull request that wires the repository. None when the entry is in main and no pull request is
    open. The change lives on the branch only; main is not touched."""
    remote = ops.git("remote", "get-url", "origin")
    if client is None:
        if not os.environ.get(TOKEN_ENV, "").strip():
            raise SystemExit(f"new-repo --wire: {TOKEN_ENV} is not set (a Forgejo token with write access to this "
                             "repository and its pull requests)")
        client = forgejo.Forgejo.from_env(remote)
    ops.NET_ENV = client.git_env(remote)
    branch = f"new-repo/{repository.split('/')[-1]}"
    current = plan(repository)
    if not current.changed:
        here = ops.git("rev-parse", "--abbrev-ref", "HEAD")
        ui.ok(f"{repository} is already wired in {here}; nothing to propose")
        return None
    back = ops.start_branch(branch)
    wiring = plan(repository)
    if wiring.changed:
        wiring.path.write_text(wiring.edited)
    pull = client.find_pull(branch)
    if not wiring.changed and pull is None:
        ui.ok(f"{repository} is already wired in main; nothing to propose")
        if back != branch:
            ops.git("switch", "-q", back)
        return None
    relative = current.path.relative_to(ops.ROOT).as_posix()
    message = f"feat(new-repo): wire {repository} into ForgeHands"
    return ops.open_pull_request([relative], message, message, body(current), client, back)
