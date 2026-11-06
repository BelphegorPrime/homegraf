"""Steps shared by "New app" and "Remove app": inventory access, git commit + push, waiting for update-all, HTTPS check."""
from __future__ import annotations

import os
import pathlib
import socket
import ssl
import subprocess
import sys
import time

import yaml

CODE = pathlib.Path(__file__).resolve().parents[2]                      # where the tools are
ROOT = pathlib.Path(os.environ.get("HOMELAB_ROOT") or CODE)             # the repository they work on
for _p in ("infra/homelab", "infra/komodo"):
    sys.path.insert(0, str(CODE / _p))
import settings  # noqa: E402
import stacklist  # noqa: E402
import ui  # noqa: E402


def inventory() -> dict:
    return {s["id"]: s for s in stacklist.load(ROOT)}


def is_platform(app_id: str) -> bool:
    """The stacks everything else stands on (inventory/homegraf.yaml `platform_stacks`/`platform_prefixes`, and homegraf itself)."""
    return settings.is_platform(app_id, ROOT)


def ingress_cfg() -> dict:
    return yaml.safe_load((ROOT / "inventory/ingress.yaml").read_text())


def npm_address() -> str:
    cfg = ingress_cfg()
    return cfg["npm_instances"][cfg["controller"]["default_npm_instance"]]["tls_address"]


NET_ENV: dict[str, str] = {}   # credentials for git over https, set in pull request mode (see newapp.run)
NETWORK_COMMANDS = ("fetch", "push", "pull", "clone", "ls-remote")


def git(*args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
    """git in the repo. Commands that talk to the remote get NET_ENV, so a private https remote works without ssh keys."""
    extra = {**(NET_ENV if args and args[0] in NETWORK_COMMANDS else {}), **(env or {})}
    return subprocess.run(["git", "-C", str(ROOT), *args], check=check, capture_output=True, text=True,
                          env={**os.environ, **extra} if extra else None).stdout.strip()


def is_ancestor(a: str, b: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", a, b], capture_output=True).returncode == 0


def commit_and_push(paths: list[str], message: str) -> tuple[str, bool]:
    """Commit exactly these paths (also deletions) and push main. (commit with the change, pushed in this run)."""
    ui.step("Commit + push", "Komodo deploys from main: the push starts update-all (pull, deploy, ingress-apply).")
    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        raise SystemExit("not on branch main; Komodo deploys main only (git switch main)")
    pending = git("status", "--porcelain", "--", *paths)
    if pending:
        print("  " + pending.replace("\n", "\n  "))
        if not ui.confirm("Commit these changes?"):
            raise SystemExit("cancelled; nothing committed")
        git("add", "-A", "--", *paths)
        git("commit", "-q", "-m", message, "--", *paths)
        ui.ok(f"commit {git('rev-parse', '--short', 'HEAD')}: {message}")
    sha = git("rev-parse", "HEAD") if pending else (git("log", "-1", "--format=%H", "--", *paths) or git("rev-parse", "HEAD"))
    git("fetch", "-q", "origin", "main")
    if is_ancestor(sha, "origin/main"):
        ui.ok("already pushed")
        return sha, False
    if not is_ancestor("origin/main", "HEAD"):
        raise SystemExit("origin/main has new commits: run `git pull --rebase`, then start the tool again (it resumes)")
    print("  to push:\n  " + git("log", "--oneline", "origin/main..HEAD").replace("\n", "\n  "))
    if not ui.confirm("Push to origin/main now?"):
        raise SystemExit("cancelled; start again later to push (it resumes)")
    git("push", "-q", "origin", "main")
    ui.ok("pushed")
    return sha, True


def start_branch(name: str) -> str:
    """Switch to the branch for a pull request, created from origin/main (or continued from origin/<name> when an
    earlier run pushed it). Untracked files come along. Returns the branch to go back to afterwards."""
    ui.step("Branch", f"The change goes to {name}; main stays as it is.")
    back = git("rev-parse", "--abbrev-ref", "HEAD")
    if back == name:
        ui.ok(f"already on {name}")
        return back
    if git("status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("uncommitted changes to tracked files: commit or stash them, then start again")
    git("fetch", "-q", "origin")
    remote = git("rev-parse", "--verify", "-q", f"origin/{name}", check=False)
    try:
        git("switch", "-q", "-C", name, f"origin/{name}" if remote else "origin/main")
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"cannot switch to {name}: {e.stderr.strip()}") from None
    ui.ok(f"{name} from origin/{name if remote else 'main'}")
    return back


def open_pull_request(paths: list[str], message: str, title: str, body: str, forgejo, back: str) -> str:
    """Commit exactly these paths on the current branch, push it and open (or find) the pull request.
    Switches back to `back` at the end. Returns the pull request URL."""
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    ui.step("Pull request", f"Branch {branch} -> main; nothing reaches main until the pull request is merged.")
    pending = git("status", "--porcelain", "--", *paths)
    if pending:
        print("  " + pending.replace("\n", "\n  "))
        if not ui.confirm("Commit these changes?"):
            raise SystemExit("cancelled; nothing committed")
        git("add", "-A", "--", *paths)
        git("commit", "-q", "-m", message, "--", *paths)
        ui.ok(f"commit {git('rev-parse', '--short', 'HEAD')}: {message}")
    git("fetch", "-q", "origin")
    if not git("log", "--oneline", "origin/main..HEAD"):
        raise SystemExit("the branch has no change against origin/main; nothing to open a pull request for")
    if not is_ancestor("origin/main", "HEAD"):
        ui.warn("origin/main moved on; the pull request may need a rebase before it merges")
    remote = git("remote", "get-url", "origin")
    pushed = bool(git("rev-parse", "--verify", "-q", f"origin/{branch}", check=False)) and is_ancestor("HEAD", f"origin/{branch}")
    if not pushed:
        if not ui.confirm(f"Push branch {branch} to origin?"):
            raise SystemExit("cancelled; start again later to push (it resumes)")
        git("push", "-q", "-u", "origin", branch, env=forgejo.git_env(remote))
        ui.ok(f"pushed {branch}")
    else:
        ui.ok(f"{branch} is already pushed")
    pull = forgejo.find_pull(branch)
    if pull:
        ui.ok(f"pull request #{pull['number']} already open")
    else:
        if not ui.confirm("Open the pull request now?"):
            raise SystemExit("cancelled; start again later to open it (it resumes)")
        pull = forgejo.create_pull(branch, title, body)
        ui.ok(f"opened pull request #{pull['number']}")
    if back != branch:
        git("switch", "-q", back)
    return pull["html_url"]


def wait_for_update_all(server: str, sha: str, komodo, pushed_now: bool = True, timeout: int = 1500) -> None:
    """Wait until the server's clone contains sha and the update-all run started by the push has finished.
    For a commit pushed in an earlier run, only the clone has to contain it."""
    ui.step("Waiting for update-all", f"Komodo pulls the commit on {server}, deploys changed stacks and runs ingress-apply"
            " (usually 1-5 minutes).")
    import create_stack
    _, repo = create_stack.repo_resource(server)
    proc_id = komodo.read("GetProcedure", {"procedure": "update-all"})["_id"]["$oid"]
    start, pulled, asked = time.time(), False, False
    while time.time() - start < timeout:
        if not pulled:
            latest = komodo.read("GetRepo", {"repo": repo})["info"].get("latest_hash") or ""
            if latest and not is_ancestor(sha, latest):
                git("fetch", "-q", "origin", "main", check=False)
            pulled = bool(latest) and is_ancestor(sha, latest)
            if pulled:
                ui.ok(f"{repo} has the commit ({latest})")
        runs = [u for u in komodo.read("ListUpdates", {"query": {"target.type": "Procedure"}, "page": 0})["updates"]
                if u["target"]["id"] == proc_id and u["start_ts"] / 1000 > start - 300]
        if pulled and not pushed_now:
            return
        if pulled and runs and runs[0]["status"] == "Complete":
            ui.ok(f"update-all finished ({'success' if runs[0]['success'] else 'with errors, see Komodo'})")
            return
        if not runs and not asked and time.time() - start > 180:
            asked = True
            if ui.confirm("update-all has not started (webhook?). Start it now?"):
                komodo.call("execute", "RunProcedure", {"procedure": "update-all"})
        time.sleep(10)
    ui.warn("timed out; continuing with the checks")


def https_status(domain: str, path: str, address: str, cafile: pathlib.Path) -> int | str:
    """HTTP status through NPM (TLS with SNI, verified against the homelab CA), independent of DNS."""
    try:
        ctx = ssl.create_default_context(cafile=str(cafile))
        with socket.create_connection((address, 443), timeout=10) as raw, ctx.wrap_socket(raw, server_hostname=domain) as s:
            s.sendall(f"GET {path} HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n".encode())
            return int(s.recv(256).split(b"\r\n")[0].split()[1])
    except (OSError, ssl.SSLError, ValueError, IndexError) as e:
        return f"{type(e).__name__}: {e}"
