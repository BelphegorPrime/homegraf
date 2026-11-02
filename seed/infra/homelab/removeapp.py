"""Wizard "Remove app": the reverse of "New app", in a safe order.

  Komodo stack (containers down, stack deleted) -> files + inventory entry (commit + push) -> wait for update-all
  -> ingressctl remove (NPM host + certificate, only once no manifest declares the service) -> Infisical folder.
Every step checks whether it is already done, so an interrupted removal continues with the same app ID.
Data on the server (bind mounts) and the AdGuard DNS rewrite are left alone; the wizard lists them.

With --pr the removal takes two runs, because routing and secrets may only go once main no longer declares the app:
  1. --pr:      Komodo stack destroyed at once, the files and the inventory entry removed on a branch + pull request.
  2. --finish:  after the merge (and update-all): ingressctl remove, Infisical folder.
The platform stacks (Infisical, the reverse proxy, this UI, the Komodo ones) are never removed by this tool.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import time

import yaml

CODE = pathlib.Path(__file__).resolve().parents[2]                      # where the tools are
ROOT = pathlib.Path(os.environ.get("HOMELAB_ROOT") or CODE)             # the repository they work on
for _p in ("infra/homelab", "infra/komodo", "infra/infisical"):
    sys.path.insert(0, str(CODE / _p))
import ops  # noqa: E402
import ui  # noqa: E402
from stack_secrets import project_slug  # noqa: E402


def remove_inventory_entry(text: str, app_id: str) -> str:
    """Drop the `- id: <app_id>` block (up to the next entry or top-level comment)."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip("\n") == f"- id: {app_id}"), None)
    if start is None:
        return text
    end = start + 1
    while end < len(lines) and not lines[end].startswith(("- ", "#")):
        end += 1
    return "".join(lines[:start] + lines[end:])


def bind_mounts(app_dir: pathlib.Path) -> list[str]:
    """Absolute host paths mounted by the compose file (not removed; listed for the operator)."""
    out = []
    f = app_dir / "compose.yaml"
    if not f.is_file():
        return out
    for svc in ((yaml.safe_load(f.read_text()) or {}).get("services") or {}).values():
        for v in (svc or {}).get("volumes") or []:
            src = v.get("source") if isinstance(v, dict) else str(v).split(":")[0]
            if src and src.startswith("/") and src not in out:
                out.append(src)
    return out


is_platform = ops.is_platform
RETRIES, BUSY_WAIT = 10, 30


def manifest_domains(app_dir: pathlib.Path) -> list[str]:
    manifest = app_dir / "ingress.yaml"
    services = yaml.safe_load(manifest.read_text())["services"] if manifest.is_file() else []
    return [d for svc in services for d in svc["proxy"]["domains"]]


def removable() -> list[dict]:
    """The apps this tool may remove, with what removing them touches (the UI lists them)."""
    out = []
    for s in sorted(ops.inventory().values(), key=lambda s: s["id"]):
        if not (s.get("server") and s.get("komodo_stack")) or is_platform(s["id"]):
            continue
        app_dir = ROOT / s["path"]
        out.append({"id": s["id"], "server": s["server"], "komodoStack": s["komodo_stack"], "secrets": s.get("secrets") == "infisical",
                    "domains": manifest_domains(app_dir), "dataPaths": bind_mounts(app_dir)})
    return out


def removed_on_main(app_id: str, path: str) -> str | None:
    """The commit on origin/main that removed the app, or None while main still has it."""
    ops.git("fetch", "-q", "origin", "main")
    if ops.git("ls-tree", "-r", "--name-only", "origin/main", "--", path):
        return None
    if f"- id: {app_id}" in ops.git("show", "origin/main:inventory/stacks.yaml").splitlines():
        return None
    return ops.git("log", "-1", "--format=%H", "origin/main", "--", path, "inventory/stacks.yaml") or ops.git("rev-parse", "origin/main")


def pr_body(entry: dict, domains: list[str], mounts: list[str]) -> str:
    app_id, server = entry["id"], entry["server"]
    return "\n".join([
        f"Remove app `{app_id}` from `{server}`, started with `./homelab.py remove-app --pr`.", "",
        f"- removes `{entry['path']}/` and its entry in `inventory/stacks.yaml`",
        f"- the Komodo stack `{entry['komodo_stack']}` is already destroyed and deleted, so the app is down",
        f"- still to do after the merge: NPM host + certificate{f' for {chr(44).join(domains)}' if domains else ''} and the Infisical folder",
        f"- kept: data on the server{f' ({chr(44).join(mounts)})' if mounts else ''}", "",
        f"After the merge: `./homelab.py remove-app --app-id {app_id} --server {server} --finish --yes`.",
        "Closing this instead leaves the app without its stack; run `new-app` again or restore the stack from Komodo."])


def pick(p: dict | None = None) -> dict | None:
    """The app to remove; p holds presets (agent mode: app_id, server for a half-removed app)."""
    p = p or {}
    inv = ops.inventory()
    if p.get("app_id"):
        app_id = p["app_id"]
        if is_platform(app_id):
            raise SystemExit(f"{app_id} is part of the platform (secrets store, proxy, this UI, Komodo): this tool does not remove it")
        if app_id in inv:
            if p.get("server") and p["server"] != inv[app_id].get("server"):
                raise SystemExit(f"{app_id} is in the inventory on {inv[app_id].get('server')}, not {p['server']}")
            return inv[app_id]
        if not p.get("server"):
            raise SystemExit(f"{app_id} is not in the inventory; for a half-removed app pass --server as well")
        return {"id": app_id, "server": p["server"], "komodo_stack": app_id, "path": f"stacks/{app_id}",
                "secrets": "infisical"}
    servers = sorted({s["server"] for s in inv.values() if s.get("server") and s.get("komodo_stack")})
    i = ui.choose("Step 1: which Komodo server runs the app?", servers)
    if i is None:
        return None
    apps = sorted(s["id"] for s in inv.values() if s.get("server") == servers[i] and s.get("komodo_stack") and not is_platform(s["id"]))
    j = ui.choose(f"Step 2: which app on {servers[i]}? (last entry: continue a removal that already dropped the"
                  " inventory entry)", apps + ["(type the app ID of a half-removed app)"])
    if j is None:
        return None
    if j < len(apps):
        return inv[apps[j]]
    app_id = ui.ask("App ID", example="whoami-test", check=lambda a: "input required" if not a else f"{a} is part of the platform" if is_platform(a) else None)
    return {"id": app_id, "server": servers[i], "komodo_stack": app_id, "path": f"stacks/{app_id}", "secrets": "infisical"}


def destroy_stack(komodo, stack: str) -> None:
    ui.step("Komodo stack", "docker compose down on the server, then the stack resource is deleted.")
    u = komodo.execute_and_poll("DestroyStack", {"stack": stack}, timeout=900)
    if not u.get("success"):
        raise SystemExit(f"DestroyStack failed, see update {u.get('id')} in Komodo; nothing else changed")
    import rebuild
    if rebuild.remove(stack):   # a stage naming a stack that is gone would make update-all fail
        ui.ok("removed from the Build stage of update-all")
    komodo.write("DeleteStack", {"id": stack})
    ui.ok(f"stack {stack} destroyed and deleted")


def drop_files(entry: dict) -> None:
    app_id, app_dir = entry["id"], ROOT / entry["path"]
    ui.step("Files", "Removed from the working tree; the next step commits them.")
    if app_dir.exists():
        shutil.rmtree(app_dir)
        ui.ok(f"removed {entry['path']}/")
    inv_path = ROOT / "inventory/stacks.yaml"
    text = inv_path.read_text()
    if (new := remove_inventory_entry(text, app_id)) != text:
        inv_path.write_text(new)
        ui.ok("removed the inventory entry")


def remove_routing(app_id: str) -> bool:
    ui.step("Routing", "ingressctl deletes only the NPM host recorded in its state, and only if the domains match.")
    import run_ingressctl
    # ingressctl is also run by update-all (ingress-apply) after the merge; while that run holds the stack Komodo answers
    # "Resource is busy". Wait for it instead of failing.
    for attempt in range(1, RETRIES + 1):
        try:
            rc, out = run_ingressctl.run(["remove", "--root", "/repo", "--resource", f"infrastructure/{app_id}"], echo=False)
        except RuntimeError as e:
            if "matching ingressctl" in str(e):   # an installation without the certificate step has no ingressctl stack
                ui.ok("this installation has no ingressctl stack: no routing to remove")
                return True
            ui.warn(f"ingressctl could not be run: {e}")
            return False
        if rc == 0 or "busy" not in out or attempt == RETRIES:
            break
        ui.ok(f"ingressctl is busy (update-all is applying the merge), trying again in {BUSY_WAIT} s ({attempt}/{RETRIES})")
        time.sleep(BUSY_WAIT)
    ok = rc == 0 or "nicht im State" in out
    if "nicht im State" in out:
        ui.ok("ingressctl manages nothing for this app (already removed or never routed)")
    elif rc == 0:
        ui.ok("NPM host and certificate deleted")
    else:
        print("  " + out.strip().replace("\n", "\n  "))
        ui.warn("ingressctl remove failed (see above); fix it, then start Remove app again for this ID")
    return ok


def drop_secrets(infisical, entry: dict) -> None:
    if entry.get("secrets") != "infisical":
        return
    ui.step("Infisical")
    import secret
    folder = secret.Folder(infisical(), entry, must_exist=False)
    if folder.exists:
        folder.drop()
        ui.ok(f"deleted folder {folder.where}")
    else:
        ui.ok(f"folder {folder.where} is already gone")


def run(infisical, komodo, preset: dict | None = None) -> bool:
    """True when everything is gone (with pr: when the pull request is open; with finish: routing and secrets are gone).
    preset: agent mode flags."""
    preset = preset or {}
    pr, finish = preset.get("pr"), preset.get("finish")
    if pr and finish:
        raise SystemExit("--pr and --finish are the two halves of one removal: run --pr, merge, then --finish")
    forgejo = None
    if pr or (finish and os.environ.get("FORGEJO_TOKEN")):
        import forgejo as forgejo_client
        remote = ops.git("remote", "get-url", "origin")
        forgejo = forgejo_client.Forgejo.from_env(remote)   # fail before anything changes
        ops.NET_ENV = forgejo.git_env(remote)
    ui.step("Remove app", "Deletes the Komodo stack, the files in this repo, the NPM host + certificate and the"
            " Infisical secrets." + (" Files go through a pull request, routing and secrets follow with --finish." if pr else ""))
    entry = pick(preset)
    if entry is None:
        print("cancelled")
        return False
    app_id, server, stack = entry["id"], entry["server"], entry["komodo_stack"]
    app_dir = ROOT / entry["path"]
    has_stack = any(s["name"] == stack for s in komodo.read("ListStacks"))
    domains = manifest_domains(app_dir)
    mounts = bind_mounts(app_dir)
    merged_sha = None
    if finish:
        merged_sha = removed_on_main(app_id, entry["path"])
        if not merged_sha:
            raise SystemExit(f"{app_id} is still on main: merge the pull request first (remove-app --pr), then --finish")

    ui.step("What happens")
    if not finish:
        print(f"  Komodo:    stack {stack} on {server}: " + ("containers down + stack deleted" if has_stack else "already gone"))
        print(f"  Git:       {entry['path']}/ and the inventory entry removed, "
              + ("on a branch + pull request (merge it, then --finish)" if pr else "commit + push")
              + ("" if app_dir.exists() or app_id in ops.inventory() else " (already done)"))
    if not pr:
        print(f"  Routing:   ingressctl remove infrastructure/{app_id}: NPM host + certificate"
              + (f" for {', '.join(domains)}" if domains else " (if ingressctl manages one)"))
        print(f"  Infisical: folder {project_slug(server)}/prod/{app_id} with its secrets" if entry.get("secrets") == "infisical"
              else "  Infisical: nothing (no `secrets: infisical`)")
    else:
        print("  Later:     routing (NPM host + certificate) and the Infisical folder, with --finish after the merge")
    print(f"  Kept:      data on {server}: {', '.join(mounts) if mounts else 'no bind mounts found'}"
          + (f"; DNS rewrite for {', '.join(domains)} in AdGuard" if domains else ""))
    if ui.AUTO:
        ui.confirm(f"Remove {app_id}?")
    elif ui.ask(f"Type the app ID to confirm (e.g. {app_id})", check=lambda a: None) != app_id:
        print("cancelled")
        return False

    if not finish:
        back = ops.start_branch(f"remove-app/{app_id}") if pr else ""
        if has_stack:
            destroy_stack(komodo, stack)
        drop_files(entry)
        if pr:
            message = f"chore({app_id}): remove app via homelab tool"
            url = ops.open_pull_request([entry["path"], "inventory/stacks.yaml"], message, message, pr_body(entry, domains, mounts), forgejo, back)
            ui.step("Done")
            print(f"  pull request: {url}")
            print(f"  The stack is gone, the app is down. Merge the pull request, then: "
                  f"./homelab.py remove-app --app-id {app_id} --server {server} --finish --yes")
            return True
        sha, pushed_now = ops.commit_and_push([entry["path"], "inventory/stacks.yaml"], f"chore({app_id}): remove app via homelab tool")
    else:
        sha, pushed_now = merged_sha, False
    ops.wait_for_update_all(ops.ingress_cfg()["controller"]["management_server"], sha, komodo, pushed_now)

    routing_ok = remove_routing(app_id)
    drop_secrets(infisical, entry)
    ui.step("Done" if routing_ok else "Done, except routing (see above)")
    print(f"  {app_id} is removed." + (f" Data still on {server}: {', '.join(mounts)}" if mounts else ""))
    if domains:
        print(f"  If you added a DNS rewrite for {', '.join(domains)} in AdGuard, remove it there.")
    return routing_ok
