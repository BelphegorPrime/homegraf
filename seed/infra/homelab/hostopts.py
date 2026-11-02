"""Options of the hosts ingressctl manages: which hosts there are, and whether a host redirects HTTP to HTTPS.

The manifests are external/ingress.yaml and stacks/<id>/ingress.yaml. `force_https` is changed in the text of the file, so
its comments and layout stay; the change goes in as a pull request (--pr) or as a push to main. Either way update-all then
runs ingress-apply, which sets the NPM host.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

import yaml

CODE = pathlib.Path(__file__).resolve().parents[2]                      # where the tools are
ROOT = pathlib.Path(os.environ.get("HOMELAB_ROOT") or CODE)             # the repository they work on
for _p in ("infra/homelab", "infra/komodo"):
    sys.path.insert(0, str(CODE / _p))
import ops  # noqa: E402
import ui  # noqa: E402


def manifests() -> list[pathlib.Path]:
    found = [ROOT / "external/ingress.yaml", *sorted((ROOT / "stacks").glob("*/ingress.yaml"))]
    return [p for p in found if p.is_file()]


def hosts() -> list[dict]:
    """The hosts ingressctl manages, one per service: the domains it serves and whether it redirects HTTP to HTTPS."""
    out = []
    for path in manifests():
        for svc in (yaml.safe_load(path.read_text()) or {}).get("services") or []:
            proxy = svc.get("proxy") or {}
            out.append({"id": svc["id"], "stack": svc.get("stack", ""), "file": str(path.relative_to(ROOT)),
                        "domains": list(proxy.get("domains") or []), "forceHttps": bool(proxy.get("force_https", True))})
    return sorted(out, key=lambda h: h["domains"][:1])


def find(domain: str) -> dict:
    found = [h for h in hosts() if domain in h["domains"]]
    if not found:
        raise SystemExit(f"{domain} is not a host ingressctl manages here (hosts made by hand in NPM are changed in NPM)")
    return found[0]


_FORCE = re.compile(r"(force_https:\s*)(true|false)\b")


def set_force_https(text: str, service_id: str, value: bool) -> str:
    """The manifest text with force_https of one service changed; everything else is kept as it is."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if re.match(rf"-\s+id:\s*{re.escape(service_id)}\s*(#.*)?$", l.rstrip())), None)
    if start is None:
        raise SystemExit(f"service {service_id} not found in the manifest")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("- ")), len(lines))
    block = "".join(lines[start:end])
    if not _FORCE.search(block):
        raise SystemExit(f"service {service_id} has no force_https line to change; add `force_https: {str(value).lower()}` under proxy: by hand")
    block = _FORCE.sub(lambda m: m[1] + str(value).lower(), block, count=1)
    return "".join(lines[:start]) + block + "".join(lines[end:])


def pr_body(host: dict, want: bool) -> str:
    domains = ", ".join(host["domains"])
    lines = [f"{'Redirect' if want else 'Stop redirecting'} HTTP to HTTPS for `{domains}`, started with `./homelab.py force-https --pr`.", "",
             f"- `{host['file']}`: `force_https: {str(want).lower()}` for the service `{host['id']}`",
             "- after the merge update-all runs ingress-apply, which sets the NPM host"]
    if want:
        lines += ["", "Every client needs the homelab root certificate first, otherwise the page stops loading for it."]
    return "\n".join(lines)


def run(preset: dict) -> bool:
    """True when the setting is as wanted (with pr: when the pull request is open). preset: agent mode flags."""
    host = find(preset["domain"])
    want = preset["state"] == "on"
    ui.step("Redirect HTTP to HTTPS", f"{', '.join(host['domains'])}: force_https {'on' if want else 'off'} in {host['file']}.")
    if host["forceHttps"] == want:
        print(f"  already {'on' if want else 'off'}; nothing to do")
        return True
    pr = preset.get("pr")
    forgejo = None
    if pr:
        import forgejo as forgejo_client
        remote = ops.git("remote", "get-url", "origin")
        forgejo = forgejo_client.Forgejo.from_env(remote)   # fail before anything changes
        ops.NET_ENV = forgejo.git_env(remote)
    print(f"  {host['file']}: force_https {str(host['forceHttps']).lower()} -> {str(want).lower()}"
          + (" (on a branch + pull request)" if pr else " (commit + push to main)"))
    ui.confirm(f"Turn the redirect {'on' if want else 'off'} for {host['domains'][0]}?")
    back = ops.start_branch(f"force-https/{host['id']}-{'on' if want else 'off'}") if pr else ""
    path = ROOT / host["file"]
    path.write_text(set_force_https(path.read_text(), host["id"], want))
    message = f"chore({host['stack'] or host['id']}): force https {'on' if want else 'off'} for {host['domains'][0]}"
    if pr:
        url = ops.open_pull_request([host["file"]], message, message, pr_body(host, want), forgejo, back)
        ui.step("Done")
        print(f"  pull request: {url}")
        print("  Merge it; update-all then sets the NPM host.")
        return True
    ops.commit_and_push([host["file"]], message)
    ui.step("Done")
    print("  Pushed; update-all sets the NPM host.")
    return True
