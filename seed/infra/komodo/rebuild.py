#!/usr/bin/env python3
"""Stacks that build their own image from the repo are redeployed after every push: stage "Build" of the Komodo
procedure update-all (webhook on push) runs DeployStack for each of them, after the repos are pulled.

DeployStackIfChanged compares compose and env files only, so a change in the app's sources (stacks/homegraf/app)
would otherwise wait for a manual deploy. `docker compose up --build` is quick when nothing changed (build cache, no
container is recreated), the same pattern as ingress-apply for ingressctl.

  . infra/komodo/komodo.env
  python3 infra/komodo/rebuild.py add|remove|list [STACK]

new-app adds the stack when its compose file has `build:`; remove-app removes it again (a stage that names a
stack that no longer exists would make update-all fail).
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import komodo_api as k  # noqa: E402

PROCEDURE = "update-all"
STAGE = "Build"


def _execution(stack: str) -> dict:
    return {"execution": {"type": "DeployStack", "params": {"stack": stack}}, "enabled": True}


def _is_deploy_of(e: dict, names: set[str]) -> bool:
    """Komodo stores the stack by id once the procedure is saved, so a stack is known by its name and its id."""
    ex = e.get("execution") or {}
    return ex.get("type") == "DeployStack" and (ex.get("params") or {}).get("stack") in names


def with_stack(stages: list[dict], stack: str, names: set[str] | None = None) -> list[dict]:
    """The stage list with a DeployStack of `stack` in stage "Build" (created right after the deploy stage)."""
    names = names or {stack}
    stages = [dict(s, executions=list(s["executions"])) for s in stages]
    stage = next((s for s in stages if s["name"] == STAGE), None)
    if stage is None:
        deploy = next((i for i, s in enumerate(stages) if any((e.get("execution") or {}).get("type") == "BatchDeployStackIfChanged"
                                                              for e in s["executions"])), len(stages) - 1)
        stage = {"name": STAGE, "enabled": True, "executions": []}
        stages.insert(deploy + 1, stage)
    if not any(_is_deploy_of(e, names) for e in stage["executions"]):
        stage["executions"].append(_execution(stack))
    return stages


def without_stack(stages: list[dict], stack: str, names: set[str] | None = None) -> list[dict]:
    """The stage list without the DeployStack of `stack`; stage "Build" goes when it is empty."""
    names = names or {stack}
    out = []
    for s in stages:
        if s["name"] == STAGE:
            s = dict(s, executions=[e for e in s["executions"] if not _is_deploy_of(e, names)])
            if not s["executions"]:
                continue
        out.append(s)
    return out


def _names(stack: str) -> set[str]:
    """The stack's name and, if it exists, its id."""
    return {stack} | {s["id"] for s in k.read("ListStacks") if s["name"] == stack}


def _procedure():
    p = k.read("GetProcedure", {"procedure": PROCEDURE})
    return p["_id"]["$oid"], p["config"]["stages"]


def stacks() -> list[str]:
    """Names of the stacks in the Build stage."""
    _, stages = _procedure()
    by_id = {s["id"]: s["name"] for s in k.read("ListStacks")}
    refs = [e["execution"]["params"]["stack"] for s in stages if s["name"] == STAGE for e in s["executions"]]
    return [by_id.get(r, r) for r in refs]


def _write(proc_id: str, before: list[dict], after: list[dict]) -> bool:
    if after == before:
        return False
    k.write("UpdateProcedure", {"id": proc_id, "config": {"stages": after}})
    return True


def add(stack: str) -> bool:
    """True if update-all was changed."""
    proc_id, stages = _procedure()
    return _write(proc_id, stages, with_stack(stages, stack, _names(stack)))


def remove(stack: str) -> bool:
    """True if update-all was changed."""
    proc_id, stages = _procedure()
    return _write(proc_id, stages, without_stack(stages, stack, _names(stack)))


def main(argv: list[str]) -> int:
    k.load_env()
    if argv[:1] == ["list"]:
        print("\n".join(stacks()) or "(none)")
    elif len(argv) == 2 and argv[0] in ("add", "remove"):
        changed = (add if argv[0] == "add" else remove)(argv[1])
        print(f"{PROCEDURE}: {argv[0]} {argv[1]}: " + ("changed" if changed else "already so"))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
