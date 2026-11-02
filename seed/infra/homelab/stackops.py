"""Everyday Komodo stack operations for people and agents: status, logs, deploy (homelab.py status|logs|deploy)."""
from __future__ import annotations

import datetime as dt
import re

import ops
import ui

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def status(komodo, stacks: list[str]) -> int:
    """One line per stack (all, or the named ones); details with containers for named stacks. 1 if any is not running."""
    servers = {s["id"]: s["name"] for s in komodo.read("ListServers")}
    rows = komodo.read("ListStacks", {"query": {"names": stacks}} if stacks else {})
    missing = set(stacks) - {r["name"] for r in rows}
    if missing:
        raise SystemExit(f"unknown Komodo stack(s): {sorted(missing)} (names as in inventory/stacks.yaml komodo_stack)")
    bad = 0
    for r in sorted(rows, key=lambda r: r["name"]):
        state, server = r["info"]["state"], servers.get(r["info"].get("server_id"), "?")
        bad += state not in ("running", "stopped") or (bool(stacks) and state != "running")
        print(f"{r['name']:28} {server:17} {state}")
        if stacks:
            names = {s.get("container_name") for s in komodo.read("GetStack", {"stack": r["name"]})["info"].get("deployed_services") or []}
            for c in komodo.read("ListDockerContainers", {"server": server}):
                if c["name"] in names or re.sub(r"-\d+$", "", c["name"]) in names:  # compose adds -<n> without container_name
                    print(f"    {c['name']:32} {c.get('state')} | {c.get('status')}")
            last = next((u for u in komodo.read("ListUpdates", {"query": {"target.type": "Stack", "target.id": r["id"]}, "page": 0})["updates"]
                         if u["operation"] in ("DeployStack", "DeployStackIfChanged")), None)
            if last:
                when = dt.datetime.fromtimestamp(last["start_ts"] / 1000).strftime("%Y-%m-%d %H:%M")
                print(f"    last deploy {when}: {'ok' if last['success'] else 'FAILED'} (update {last['id']})")
    return 1 if bad else 0


def logs(komodo, stack: str, tail: int, grep: list[str], services: list[str]) -> int:
    if not komodo.read("ListStacks", {"query": {"names": [stack]}}):
        raise SystemExit(f"unknown Komodo stack {stack} (names as in inventory/stacks.yaml komodo_stack)")
    if grep:
        log = komodo.read("SearchStackLog", {"stack": stack, "services": services, "terms": grep, "combinator": "Or",
                                             "invert": False})
    else:
        log = komodo.read("GetStackLog", {"stack": stack, "services": services, "tail": tail})
    print(ANSI.sub("", (log.get("stdout") or "") + (log.get("stderr") or "")).rstrip())
    return 0


def deploy(komodo, stack: str) -> int:
    """DeployStack with the failed stage's output; 1 on failure. Asks (people) or needs --yes (agents)."""
    state = komodo.read("ListStacks", {"query": {"names": [stack]}})
    if not state:
        raise SystemExit(f"unknown Komodo stack {stack}")
    print(f"{stack}: {state[0]['info']['state']}")
    if not ui.confirm(f"Deploy {stack} (docker compose pull + up through Komodo)?"):
        return 1
    u = komodo.execute_and_poll("DeployStack", {"stack": stack}, timeout=900)
    for log in u.get("logs") or []:
        print(f"  {'✓' if log['success'] else '✗'} {log['stage']}")
        if not log["success"]:
            print("    " + ANSI.sub("", (log.get("stdout", "") + log.get("stderr", ""))[-2000:]).replace("\n", "\n    "))
    print(f"deploy {'succeeded' if u.get('success') else 'FAILED'} (update {u.get('id') or (u.get('_id') or {}).get('$oid')})")
    return 0 if u.get("success") else 1


def managed() -> list[dict]:
    """The stacks the UI may redeploy and manage secrets of: those Komodo runs, without the platform ones (redeploying the
    secrets store, the proxy or the UI itself from the UI would cut off the branch it sits on)."""
    return [{"id": s["id"], "server": s["server"], "komodoStack": s["komodo_stack"], "secrets": s.get("secrets") == "infisical"}
            for s in sorted(ops.inventory().values(), key=lambda s: s["id"])
            if s.get("server") and s.get("komodo_stack") and not ops.is_platform(s["id"])]
