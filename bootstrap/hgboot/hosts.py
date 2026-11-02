"""Another host: its Komodo agent (Periphery) is registered, it gets its own clone of the repository, and its address goes into
inventory/servers.yaml, where the tools take the upstreams of its apps from."""
from __future__ import annotations

import base64
import re

from .komodo import PROCEDURE, REPO_NAME
from .steps import StepError, log

STEP = "host"
AGENT_IMAGE = "ghcr.io/moghtech/komodo-periphery:2.3.4"     # the same as komodo-periphery in compose.yaml (a test compares them)
AGENT_PORT = 8120
SERVERS_FILE = "inventory/servers.yaml"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")
ADDRESS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")


def check_name(name: str) -> str:
    if not NAME_RE.fullmatch(name) or name == "local":
        raise StepError(f"{name!r} is no name for a host: 2 to 32 characters, lower case letters, digits and hyphens (and not 'local')")
    return name


def check_address(address: str) -> str:
    if not ADDRESS_RE.fullmatch(address):
        raise StepError(f"{address!r} is no address: the IP address or the name of the host, as this stack reaches it")
    return address


def agent_compose(cfg, name: str) -> str:
    """The compose file for the new host: the agent of this stack, reachable from it. The data directory has the same path as here,
    because the tools build the run directory of every stack from one path (repos_dir in inventory/homegraf.yaml)."""
    if not cfg.komodo_passkey:
        raise StepError("KOMODO_PASSKEY is not set (run ./setup.sh, it writes .env)")
    root = f"{cfg.data_dir}/periphery"
    return f"""# The Komodo agent of the host "{name}": started here, registered by the stack ({'add-host.sh'} {name} <address>).
# It runs docker compose for the stacks of this host and mounts the Docker socket, so it is root on this host: let only the host of
# the stack reach port {AGENT_PORT}. It holds a secret (the passkey of the stack): keep this file to yourself.
name: homegraf-agent
services:
  periphery:
    image: {AGENT_IMAGE}
    container_name: homegraf-periphery
    restart: unless-stopped
    cap_add:
      - SYS_PTRACE
    security_opt:
      - apparmor:unconfined
    environment:
      PERIPHERY_ROOT_DIRECTORY: {root}
      PERIPHERY_PASSKEYS: {cfg.komodo_passkey}
      PERIPHERY_SSL_ENABLED: "true"
      PERIPHERY_DISABLE_TERMINALS: "false"
      PERIPHERY_INCLUDE_DISK_MOUNTS: /etc/hostname
    ports:
      - "{AGENT_PORT}:{AGENT_PORT}"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /proc:/proc
      - {root}:{root}
"""


class Hosts:
    def __init__(self, cfg, state, komodo, forgejo, *, wait):
        self.cfg, self.state, self.komodo, self.forgejo, self.wait = cfg, state, komodo, forgejo, wait

    def add(self, name: str, address: str, *, timeout: int = 600) -> None:
        check_name(name)
        check_address(address)
        k = self.komodo
        k.wait(lambda: k.http("GET", self.cfg.komodo_url + "/version").ok, "Komodo", timeout=240)
        k.login()
        server_id = self.ensure_server(name, address, timeout)
        domain = f"{self.cfg.host_ip}:{self.cfg.ports['FORGEJO']}"
        k.ensure_git_account(self.state.get("forgejo", "token"), domain)
        repo = self.ensure_repository(name, server_id, domain)
        self.pull_in_update_all(repo)
        self.ensure_server_entry(name, address)
        log(STEP, f"host {name} ({address}): ready. Apps are placed on it with the server {name}.")

    def ensure_server(self, name: str, address: str, timeout: int) -> str:
        k = self.komodo
        url = f"https://{address}:{AGENT_PORT}"
        have = next((s for s in k.call("read", "ListServers") if s["name"] == name), None)
        if have is None:
            k.call("write", "CreateServer", {"name": name, "config": {"address": url, "enabled": True}})
            log(STEP, f"server {name}: created ({url})")
        else:
            current = k.call("read", "GetServer", {"server": name})["config"].get("address")
            if current != url:
                k.call("write", "UpdateServer", {"id": have["id"], "config": {"address": url}})
                log(STEP, f"server {name}: address changed to {url}")
            else:
                log(STEP, f"server {name}: already there")

        def ok() -> bool:
            return any(s["name"] == name and s["info"]["state"] == "Ok" for s in k.call("read", "ListServers"))
        try:
            self.wait(ok, f"the agent of {name} at {url}", timeout=timeout)
        except TimeoutError as e:
            raise StepError(f"{e}: is it running on that host (docker compose up -d) and does port {AGENT_PORT} reach it from here? "
                            "Run this again when it does.") from None
        log(STEP, f"server {name}: Ok")
        return next(s["id"] for s in k.call("read", "ListServers") if s["name"] == name)

    def ensure_repository(self, name: str, server_id: str, domain: str) -> str:
        """One repository resource per server: each agent has a clone of its own."""
        k, repo = self.komodo, f"{REPO_NAME}-{name}"
        wanted = {"server_id": server_id, "git_provider": domain, "git_https": False, "git_account": self.cfg.forgejo_user,
                  "repo": self.cfg.repository, "branch": "main"}
        have = next((r for r in k.call("read", "ListRepos") if r["name"] == repo), None)
        if have is None:
            k.call("write", "CreateRepo", {"name": repo, "config": wanted})
            log(STEP, f"repository resource {repo}: created")
        else:
            current = k.call("read", "GetRepo", {"repo": repo})
            if any(current["config"].get(key) != v for key, v in wanted.items()):
                k.call("write", "UpdateRepo", {"id": have["id"], "config": wanted})
                log(STEP, f"repository resource {repo}: updated")
            else:
                log(STEP, f"repository resource {repo}: already there")
        if not k.call("read", "GetRepo", {"repo": repo})["info"].get("latest_hash"):
            k.finish(k.call("execute", "CloneRepo", {"repo": repo}), f"cloning the repository on {name}")
            log(STEP, f"repository cloned to {self.cfg.repos_dir}/{repo} on {name}")
        return repo

    def pull_in_update_all(self, repo: str) -> None:
        """update-all pulls every clone before it deploys: the stage Pull gets one more PullRepo."""
        k = self.komodo
        current = k.call("read", "GetProcedure", {"procedure": PROCEDURE})
        stages = current["config"]["stages"]
        pull = next((s for s in stages if s["name"] == "Pull"), None)
        if pull is None:
            raise StepError(f"the procedure {PROCEDURE} has no stage 'Pull' (it was changed): add a PullRepo of {repo} to it in Komodo")
        if any(e["execution"].get("type") == "PullRepo" and e["execution"]["params"].get("repo") == repo for e in pull["executions"]):
            log(STEP, f"procedure {PROCEDURE}: already pulls {repo}")
            return
        pull["executions"].append({"execution": {"type": "PullRepo", "params": {"repo": repo}}, "enabled": True})
        k.call("write", "UpdateProcedure", {"id": current["_id"]["$oid"], "config": {"stages": stages}})
        log(STEP, f"procedure {PROCEDURE}: a push now pulls {repo} too")

    def ensure_server_entry(self, name: str, address: str) -> None:
        """inventory/servers.yaml in the repository: the address the proxy reaches the apps of this host at."""
        f = self.forgejo
        url = f"{f.api}/repos/{self.cfg.repository}/contents/{SERVERS_FILE}"
        r = f.http("GET", url, basic=f.basic)
        if not r.ok:
            raise StepError(f"Forgejo has no {SERVERS_FILE} in {self.cfg.repository} ({r.status}): add the host to the repository by hand")
        info = r.json()
        text = base64.b64decode(info["content"]).decode()
        if "servers:" not in text:
            raise StepError(f"{SERVERS_FILE} has no 'servers:' list (it was changed): add {name} to it by hand")
        entry = re.search(rf'^[ \t]*"?{re.escape(name)}"?[ \t]*:.*$', text, re.M)
        if entry:
            if address not in entry.group(0):
                raise StepError(f"{SERVERS_FILE} lists {name} with another address ({entry.group(0).strip()}): change it in the repository")
            log(STEP, f"{SERVERS_FILE}: {name} is already listed")
            return
        text = text.rstrip("\n") + f'\n  "{name}": {{ address: "{address}", status: verified }}\n'
        r = f.http("PUT", url, basic=f.basic, body={"content": base64.b64encode(text.encode()).decode(), "sha": info["sha"],
                                                     "message": f"Add the host {name}", "branch": "main"})
        if not r.ok:
            raise StepError(f"Forgejo refused to change {SERVERS_FILE}: {r.status} {r.text[:200]}")
        log(STEP, f"{SERVERS_FILE}: {name} added ({address})")
