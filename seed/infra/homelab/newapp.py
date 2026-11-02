"""Wizard "New app": compose, inventory, routing + certificate, Komodo stack, secrets, push, deploy, checks.

Every step checks whether it is already done, so an interrupted run continues with the same app ID.
Order (docs/plans/05-infisical-stack-secrets.md, docs/operations.md#adding-a-service):
  files -> validation -> Komodo stack + secrets (before the push, so the first deploy has them) -> commit + push
  -> wait for update-all (pull, DeployStackIfChanged, ingress-apply) -> only if needed: deploy, ingress apply
  -> HTTPS and DNS checks.
"""
from __future__ import annotations

import contextlib
import dataclasses
import fnmatch
import io
import os
import pathlib
import re
import secrets
import socket
import subprocess
import sys
import time

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
for _p in ("infra/homelab", "infra/komodo", "infra/infisical", "infra/stackctl"):
    sys.path.insert(0, str(ROOT / _p))
import addons  # noqa: E402
import settings  # noqa: E402
import serviceopts  # noqa: E402
import templates  # noqa: E402
import ops  # noqa: E402
import ui  # noqa: E402
from stack_secrets import project_slug  # noqa: E402

APP_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")
VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(:?[-?+])?[^}]*\}|([A-Za-z_][A-Za-z0-9_]*))")
DEFAULT_STATUSES = [200, 301, 302, 307, 401]
WIZARD_NOTE = "# Created with ./homelab.py (New app)."


# --- pure functions (tested) --------------------------------------------------------------------------------

def compose_vars(text: str) -> tuple[set[str], set[str]]:
    """(${VAR} without default, ${VAR:-…}/${VAR-…}/${VAR:+…} with default); $$ is an escaped $."""
    text = "\n".join(line for line in text.replace("$$", "").splitlines() if not line.lstrip().startswith("#"))
    required, optional = set(), set()
    for m in VAR_RE.finditer(text):
        name, op = m.group(1) or m.group(3), m.group(2) or ""
        (optional if op.lstrip(":") in ("-", "+") else required).add(name)
    return required, optional - required


def compose_ports(text: str) -> list[tuple[str, int, int]]:
    """Published ports: (service, host port, container port); ports with ${VAR} or without host port are skipped."""
    out = []
    for name, svc in ((yaml.safe_load(text) or {}).get("services") or {}).items():
        for p in (svc or {}).get("ports") or []:
            if isinstance(p, dict):
                host, target = p.get("published"), p.get("target")
            else:
                parts = str(p).split("/")[0].split(":")
                if len(parts) < 2:
                    continue
                host, target = parts[-2], parts[-1]
            if str(host).isdigit() and str(target).isdigit():
                out.append((name, int(host), int(target)))
    return out


VPN_CONFIG = "stacks/gluetun/wg0.conf"


def vpn_config_problem(root: pathlib.Path | None = None) -> str | None:
    """Why the VPN block cannot be used yet: the stacks mount the WireGuard file from the repo, so it has to be there
    (committed, since the servers pull the repo) or the app would not start."""
    root = root or ROOT
    if not (root / VPN_CONFIG).is_file():
        return f"{VPN_CONFIG} is not in the repo: put the WireGuard config there and commit it (the servers pull it)"
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch", VPN_CONFIG], capture_output=True)
    if tracked.returncode == 1:   # git works and does not know the file; other codes: not a usable checkout, the file counts
        return f"{VPN_CONFIG} is not committed yet: commit and push it (it is ignored by stacks/gluetun/.gitignore, use git add -f)"
    return None


def db_of(block: addons.Addon, chosen: list[addons.Addon]) -> str:
    """The block from block.requires_any that is part of this app (the first one chosen)."""
    return next((b.id for b in chosen if b.id in block.requires_any), "")


def chosen_templates(p: dict) -> list["templates.Template"]:
    """--template NAME, repeatable: the first drives the form, the others come along with services of their own."""
    names = p.get("templates") or p.get("template") or []
    known = templates.catalog()
    out = []
    for name in dict.fromkeys([names] if isinstance(names, str) else names):
        if name not in known:
            raise SystemExit(f"--template {name}: not one of {sorted(known)}")
        out.append(known[name])
    return out


def get_template(p: dict) -> "templates.Template | None":
    """The first --template, or None."""
    return next(iter(chosen_templates(p)), None)


def template_service(app_id: str, t: "templates.Template", chosen: list["templates.Template"]) -> str:
    """The service that is the template's own app: <id> for the first template, <id>-<template> for the others."""
    return app_id if t is chosen[0] else f"{app_id}-{t.id}"


def template_defaults(t: "templates.Template") -> dict:
    """The form's defaults of a template: read from the service "app" of its compose file."""
    got = form_values(t.compose, "app")
    return {k: got[k] for k in ("image", "containerPort", "volumes", "env", "networks", "cpus", "memory", "gpu", "secretVars")}


def with_template_defaults(p: dict, t: "templates.Template") -> dict:
    """The presets plus the template's defaults, when the image is not given (a form always gives it)."""
    if p.get("image"):
        return p
    d = template_defaults(t)
    q = dict(p)
    q["image"] = d["image"]
    if q.get("container_port") is None and d["containerPort"]:
        q["container_port"] = d["containerPort"]
    if not q.get("volume"):
        q["volume"] = [serviceopts.Volume(v["kind"], v["source"], v["target"], v["readOnly"], v["size"]).spec() for v in d["volumes"]]
    if not q.get("env"):
        q["env"] = [f"{k}={v}" for k, v in d["env"].items()]
    if q.get("secret_vars") is None:
        q["secret_vars"] = d["secretVars"]
    for flag, key in (("cpus", "cpus"), ("memory", "memory"), ("gpu", "gpu")):
        q[flag] = q.get(flag) or d[key]
    return q


def routing_defaults(p: dict, t: "templates.Template") -> dict:
    """The routing answers a template knows (WebSockets, the check path, the codes that count as up), unless given."""
    q = dict(p)
    if q.get("websockets") is None:
        q["websockets"] = t.websockets
    if not q.get("health_path"):
        q["health_path"] = t.health_path
    if not q.get("statuses") and t.statuses:
        q["statuses"] = list(t.statuses)
    return q


def placeholders(app_id: str, domain: str, url: str):
    """Fills __APP__, __URL__ (where the app is reached) and __DOMAIN__ in what a template brings."""
    def fill(text: str) -> str:
        return text.replace("__APP__", app_id).replace("__URL__", url).replace("__DOMAIN__", domain)
    return fill


def app_url(app_id: str, server: str, p: dict, host_port: int | None) -> tuple[str, str]:
    """(domain, URL) the app will be reached at: https://<domain> with routing, else the server's address and port."""
    domain = p.get("domain") or f"{app_id}.{settings.domain(ops.ROOT)}"
    if p.get("routing") is not False:
        return domain, f"https://{domain}"
    return domain, f"http://{server_address(server) or 'localhost'}" + (f":{host_port}" if host_port else "")


def unchosen_paths(compose: str, app_id: str, t: "templates.Template") -> list[str]:
    """Container paths of host_paths whose volume still has the template's stand-in as source."""
    stand_in = {v["target"]: v["source"] for v in template_defaults(t)["volumes"]}
    chosen = {v["target"]: v["source"] for v in form_values(compose, app_id)["volumes"]}
    return [t_ for t_ in t.host_paths if chosen.get(t_) == stand_in.get(t_)]


def _present(compose: str, service: str) -> bool:
    return service in ((yaml.safe_load(compose) or {}).get("services") or {})


def stand_ins(compose: str, app_id: str, chosen: list["templates.Template"]) -> list[tuple[str, str]]:
    """(service, container path) of every template folder that still has the stand-in of the template as source."""
    out = []
    for t in chosen:
        service = template_service(app_id, t, chosen)
        if _present(compose, service):
            out += [(service, path) for path in unchosen_paths(compose, service, t)]
    return out


def template_files(compose: str, app_id: str, t: "templates.Template | list[templates.Template] | None",
                   files_dir: str | None) -> dict[str, str]:
    """The template files this compose file mounts from the repo, with the text of an edited copy in files_dir if any.
    With several templates each one's files are those its own service mounts; two with the same file name are refused."""
    chosen = t if isinstance(t, list) else [t] if t else []
    out, owner = {}, {}
    for tpl in chosen:
        service = template_service(app_id, tpl, chosen)
        if not _present(compose, service):
            continue
        rows = form_values(compose, service)["volumes"]
        for name in templates.referenced(tpl, [v["source"] for v in rows if v["kind"] == "repo"]):
            if name in out:
                raise SystemExit(f"--template {owner[name]} and {tpl.id} both bring the file {name}: they cannot share one app")
            edited = pathlib.Path(files_dir) / name if files_dir else None
            out[name], owner[name] = (edited.read_text() if edited and edited.is_file() else tpl.files[name]), tpl.id
    return out


class _Indented(yaml.SafeDumper):
    """Lists inside a mapping are indented under their key, like in the generated compose file."""
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def skeleton(app_id: str, image: str, container_port: int | None, host_port: int | None,
             secret_vars: list[str], mount: str | None, blocks: list[addons.Addon] | None = None,
             block_ports: dict[str, int] | None = None, server_address: str | None = None,
             opts: serviceopts.ServiceOptions | None = None, template: "templates.Template | None" = None,
             fill=lambda text: text) -> str:
    """The compose file of a new app: one service for the image, plus the building blocks (database, cache, tools).
    block_ports: the published host port of each block that has one; server_address: where the tools are opened.
    template: adds its sidecars and the keys of its service the form does not model; fill: its placeholders."""
    blocks, block_ports = blocks or [], block_ports or {}
    sidecars = template.sidecars if template else {}
    if app_id in {b.id for b in blocks} | set(sidecars):
        raise SystemExit(f"--app-id {app_id}: the service name is taken by the building block or sidecar of the same name")
    vpn = next((b for b in blocks if b.vpn), None)
    if vpn and not (container_port and host_port):
        raise SystemExit(f"--addon {vpn.id}: the VPN needs the app's port (--container-port)")
    lines = [WIZARD_NOTE, "services:", f"  {app_id}:", f"    image: {image}", f"    container_name: {app_id}",
             "    restart: unless-stopped"]
    if template and template.extras:
        dumped = yaml.dump(template.extras, Dumper=_Indented, sort_keys=False, default_flow_style=False, width=10_000)
        lines += ["    " + line for line in fill(dumped).rstrip("\n").splitlines()]
    if vpn:
        lines += [f'    network_mode: "service:{vpn.id}"']   # the app is in the VPN's network: the port is published there
    elif container_port and host_port:
        lines += ["    ports:", f"      - {host_port}:{container_port}"]
    used = [b for b in blocks if not b.ui]    # tools for people are not something the app waits for or talks to
    opts = opts or serviceopts.ServiceOptions()
    if vpn and opts.networks:
        raise SystemExit(f"--addon {vpn.id}: the app is in the VPN's network, it cannot join other networks")
    env = [f"      {v}: ${{{v}}}" for v in secret_vars]
    env += serviceopts.env_lines({k: v for k, v in opts.env.items() if k not in secret_vars})
    for b in used if not template or template.addon_env else []:
        env += [f"      {k}: {v}" for k, v in b.app_env.items() if k not in secret_vars and k not in opts.env]
    if env:
        lines += ["    environment:"] + env
    own = serviceopts.ServiceOptions([serviceopts.Volume("data", "", mount)] * bool(mount) + opts.volumes, opts.networks,
                                     opts.cpus, opts.memory, opts.gpu)
    for section in serviceopts.service_lines(own, app_id).values():
        lines += section
    waits = [(b.id, "service_healthy") for b in used]
    waits += [(n, "service_healthy" if "healthcheck" in (yaml.safe_load(t)[n] or {}) else "service_started") for n, t in sidecars.items()]
    if waits:
        lines += ["    depends_on:"] + [f"      {n}:\n        condition: {c}" for n, c in waits]
    text = "\n".join(lines) + "\n"
    for t in sidecars.values():
        text += "\n" + fill(t)
    for b in blocks:
        text += "\n" + b.render(app_id, block_ports.get(b.id), db_of(b, blocks), server_address,
                                (host_port, container_port) if host_port and container_port else None)
    return text + "\n".join(serviceopts.top_level(own)) + ("\n" if serviceopts.top_level(own) else "")


SERVICE_RE = re.compile(r"^  ([A-Za-z0-9._-]+):[ \t]*$", re.M)
TOP_LEVEL_RE = re.compile(r"^(?:volumes|networks):", re.M)


def var_prefix(t: "templates.Template") -> str:
    """The prefix of the secrets of a further template: MEALIE_ for mealie (POSTGRES_PASSWORD -> MEALIE_POSTGRES_PASSWORD)."""
    return re.sub(r"\W", "_", t.id).upper() + "_"


def prefixed(prefix: str, var: str) -> str:
    return var if var.startswith(prefix) else prefix + var


def isolate(body: str, main: str, prefix: str) -> str:
    """The services of one template made safe to sit next to another one's: every service but `main` is named
    <main>-<name> (its depends_on, network_mode and the host names in its environment follow), every ${VAR} gets the prefix.
    Containers, hosts and secrets of two templates then never meet."""
    new = {n: f"{main}-{n}" for n in SERVICE_RE.findall(body) if n != main}
    host = re.compile(r"(?:^|(?<=//)|(?<=@)|(?<=[\s\"']))(%s)(?=$|:\d|/|[\"'\s])" % "|".join(map(re.escape, sorted(new, key=len, reverse=True)))) if new else None
    out, key = [], ""
    for line in body.splitlines():
        if (m := SERVICE_RE.match(line)):
            key = ""
            line = f"  {new.get(m.group(1), m.group(1))}:"
        elif (m := re.match(r"^    ([A-Za-z_][\w-]*):", line)):
            key = m.group(1)
            if key == "network_mode":
                line = re.sub(r"service:([\w.-]+)", lambda x: "service:" + new.get(x.group(1), x.group(1)), line)
        elif key == "depends_on" and (m := re.match(r"^(      (?:- )?)([\w.-]+)(:?)\s*$", line)) and m.group(2) in new:
            line = m.group(1) + new[m.group(2)] + m.group(3)
        elif key == "environment" and host and (m := re.match(r"^(      [\w.-]+:\s*)(.*)$", line)):
            line = m.group(1) + host.sub(lambda x: new[x.group(1)], m.group(2))
        if not line.lstrip().startswith("#"):
            line = re.sub(r"\$\{([A-Za-z_]\w*)(?=[}:\-+?])", lambda x: "${" + prefixed(prefix, x.group(1)), line)
        out.append(line)
    return "\n".join(out) + "\n"


def extra_template(app_id: str, server: str, t: "templates.Template", taken: set[int]) -> str:
    """The services a further template adds to the app's compose file (see isolate): its own app, its building blocks,
    its sidecars. It publishes a free port of its own (added to taken); its folders below /srv/docker/<id>-<template>."""
    name = f"{app_id}-{t.id}"
    d = template_defaults(t)
    cport = d["containerPort"]
    hport = next(n for n in range(cport if cport >= 1024 else 8000, 65535) if n not in taken) if cport else None
    if hport:
        taken.add(hport)
    blocks = [addons.catalog()[n] for n in t.addons]
    block_ports = {}
    for b in blocks:
        if b.needs_port:
            block_ports[b.id] = next(n for n in range(8000, 65535) if n not in taken)
            taken.add(block_ports[b.id])
    address = server_address(server)
    fill = placeholders(name, "", f"http://{address or 'localhost'}" + (f":{hport}" if hport else ""))
    opts = serviceopts.options_from(with_template_defaults({}, t))
    opts = dataclasses.replace(opts, env={k: fill(v) for k, v in opts.env.items()})
    text = skeleton(name, d["image"], cport, hport, d["secretVars"], None, blocks, block_ports, address, opts, t, fill)
    body, tail = _split_top_level(text.partition("\nservices:\n")[2])
    if tail:
        raise SystemExit(f"--template {t.id}: its volumes or networks need top-level entries, it cannot share an app with another template")
    return isolate(body, name, var_prefix(t))


def _split_top_level(text: str) -> tuple[str, str]:
    """(services, top-level volumes:/networks:) of the text below `services:`."""
    m = TOP_LEVEL_RE.search(text)
    return (text[:m.start()], text[m.start():]) if m else (text, "")


def with_extra_services(text: str, extra: str) -> str:
    """The text of a compose file with further services before its top-level volumes:/networks:."""
    head, _, rest = text.partition("\nservices:\n")
    body, tail = _split_top_level(rest)
    return head + "\nservices:\n" + body.rstrip("\n") + "\n\n" + extra.rstrip("\n") + "\n" + (("\n" + tail) if tail else "")


def extra_secrets(t: "templates.Template") -> set[str]:
    """The prefixed variables of a further template: its own and those of its building blocks."""
    blocks = addons.catalog()
    return {prefixed(var_prefix(t), v) for v in t.secrets() | {s for n in t.addons for s in blocks[n].secrets}}


def extra_note(compose: str, app_id: str, server: str, t: "templates.Template") -> str:
    """The first-run note of a further template, filled in for its own service, folders, port and variables."""
    name = f"{app_id}-{t.id}"
    port = next((h for s, h, _ in compose_ports(compose) if s == name), None)
    address = server_address(server) or "localhost"
    text = t.first_run
    for var in sorted(t.secrets() | {s for n in t.addons for s in addons.catalog()[n].secrets}, key=len, reverse=True):
        text = re.sub(r"(?<!\w)%s(?!\w)" % re.escape(var), prefixed(var_prefix(t), var), text)
    text = text.replace("/srv/docker/__APP__", f"/srv/docker/{name}")
    return placeholders(app_id, "", f"http://{address}" + (f":{port}" if port else ""))(text)


def extra_notes(compose: str, app_id: str, server: str, chosen: list["templates.Template"]) -> list[tuple["templates.Template", str]]:
    return [(t, extra_note(compose, app_id, server, t)) for t in chosen[1:] if t.first_run and _present(compose, f"{app_id}-{t.id}")]


def inventory_block(app: "App") -> str:
    block = (f"- id: {app.id}\n  namespace: infrastructure\n  path: stacks/{app.id}\n  compose_files:\n  - compose.yaml\n"
             f"  komodo_stack: {app.id}\n  server: {app.server}\n  project_name: {app.id}\n  status: verified\n")
    return block + ("  secrets: infisical\n" if app.secrets else "")


def manifest(app: "App") -> str:
    doc = {"apiVersion": "homelab.ingress/v1", "namespace": "infrastructure", "services": [{
        "id": app.id, "stack": app.id, "lifecycle": "present",
        "proxy": {"domains": [app.domain],
                  "upstream": {"scheme": "http", "server": app.server, "port": app.upstream_port},
                  "websockets": app.websockets, "force_https": app.force_https},
        "certificate": {"issuer_ref": "homelab-private-ca", "validity_days": 90, "renew_before_days": 30},
        "healthcheck": {"path": app.health_path, "accepted_statuses": app.statuses},
    }]}
    return WIZARD_NOTE + "\n" + yaml.safe_dump(doc, sort_keys=False, default_flow_style=None)


def known_domains(root: pathlib.Path | None = None) -> dict[str, str]:
    """Domain -> manifest file, across all ingress.yaml."""
    root = root or ROOT
    out = {}
    for f in list(root.glob("stacks/*/ingress.yaml")) + list(root.glob("external/*/ingress.yaml")):
        for svc in (yaml.safe_load(f.read_text()) or {}).get("services") or []:
            for d in ((svc.get("proxy") or {}).get("domains") or []):
                out[d] = str(f.relative_to(root))
    return out


def server_address(server: str, root: pathlib.Path | None = None) -> str | None:
    """Address of a Komodo server from inventory/servers.yaml."""
    servers = yaml.safe_load(((root or ROOT) / "inventory/servers.yaml").read_text()).get("servers") or {}
    return (servers.get(server) or {}).get("address")


def used_host_ports(server: str, root: pathlib.Path | None = None) -> set[int]:
    root = root or ROOT
    used = set()
    for s in yaml.safe_load((root / "inventory/stacks.yaml").read_text()).get("stacks") or []:
        if s.get("server") != server or s.get("status") == "inactive":
            continue
        for f in s.get("compose_files") or []:
            p = root / s["path"] / f
            if p.is_file():
                used |= {host for _, host, _ in compose_ports(p.read_text())}
    return used


# --- state and questions ------------------------------------------------------------------------------------

@dataclasses.dataclass
class App:
    id: str
    server: str
    compose: str                      # content of stacks/<id>/compose.yaml
    compose_is_new: bool
    domain: str | None = None         # None = no routing
    upstream_port: int | None = None
    websockets: bool = False
    force_https: bool = True
    health_path: str = "/"
    statuses: list[int] = dataclasses.field(default_factory=lambda: list(DEFAULT_STATUSES))
    secrets: dict[str, str | None] = dataclasses.field(default_factory=dict)   # None = keep the existing value
    files: dict[str, str] = dataclasses.field(default_factory=dict)            # extra files of stacks/<id> (from a template)
    template: str = ""
    first_run: str = ""               # what to do once it runs (from a template)
    host_paths: list[str] = dataclasses.field(default_factory=list)   # folders the template wants chosen on the server

    @property
    def dir(self) -> pathlib.Path:
        return ROOT / "stacks" / self.id

    @property
    def entry(self) -> dict:
        """Inventory entry: the existing one, otherwise the one the wizard writes."""
        return ops.inventory().get(self.id) or yaml.safe_load(inventory_block(self))[0]

    @property
    def stack(self) -> str:
        return self.entry["komodo_stack"]


def _check_app_id(a: str) -> str | None:
    return None if APP_ID_RE.fullmatch(a) else "lowercase letters, digits and -, 2 to 41 characters"


def _check_image(a: str) -> str | None:
    return None if ":" in a.rsplit("/", 1)[-1] and not a.endswith(":latest") else "add a fixed tag, e.g. :1.2.3 (not :latest)"


def _choose_addons(p: dict) -> list[addons.Addon]:
    """--addon NAME (repeatable), or asked: ready-made services added next to the app."""
    known = addons.catalog()
    if p.get("addon") is not None:
        names = p["addon"]
    elif template := get_template(p):
        names = list(template.addons)
    else:
        asked = ui.ask(f"Building blocks to add, comma-separated (empty = none; {', '.join(known)})", check=lambda a: None)
        names = [n.strip() for n in asked.split(",") if n.strip()]
    bad = [n for n in names if n not in known]
    if bad:
        raise SystemExit(f"--addon {', '.join(bad)}: not one of {sorted(known)}")
    chosen = [known[n] for n in dict.fromkeys(names)]
    for b in chosen:
        if b.exclusive and len(chosen) > 1:
            raise SystemExit(f"--addon {b.id}: cannot be combined with other blocks "
                             f"({', '.join(c.id for c in chosen if c is not b)})")
    for b in chosen:
        if b.requires_any and not any(r in {c.id for c in chosen} for r in b.requires_any):
            raise SystemExit(f"--addon {b.id}: needs one of {', '.join(b.requires_any)} next to it")
    return chosen


MAX_COMPOSE = 200_000


def _read_compose_file(path: str, app_id: str, server: str) -> str:
    """--compose-file: a finished compose file (e.g. edited in the homegraf UI) used as it is. Checked as far as the
    wizard can before anything is created; stackctl validate has the final word."""
    f = pathlib.Path(path)
    if not f.is_file() or f.stat().st_size > MAX_COMPOSE:
        raise SystemExit(f"--compose-file {path}: not a file, or larger than {MAX_COMPOSE} bytes")
    text = f.read_text()
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SystemExit(f"--compose-file: not valid YAML: {str(e).splitlines()[0] if str(e) else e}") from None
    services = doc.get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict) or not services or not all(isinstance(s, dict) for s in services.values()):
        raise SystemExit("--compose-file: needs a `services:` mapping with at least one service")
    taken = used_host_ports(server)
    clash = sorted({h for _, h, _ in compose_ports(text) if h in taken})
    if clash:
        raise SystemExit(f"--compose-file: host port(s) {clash} are already used on {server} (taken: {sorted(taken)})")
    return text


def _ask_compose(app_id: str, server: str, p: dict, blocks: list[addons.Addon]) -> str:
    if template := get_template(p):
        p = with_template_defaults(p, template)
    ui.step("Compose file", f"Nothing found at stacks/{app_id}/compose.yaml, so the wizard writes a minimal one.\n"
            f"   For bigger apps, cancel, write stacks/{app_id}/compose.yaml yourself and start again: it is read then.")
    image = p.get("image") or ui.ask("Image with a fixed version (Renovate updates it)", example="traefik/whoami:v1.11.0",
                                     check=_check_image, flag="--image")
    if err := _check_image(image):
        raise SystemExit(f"--image: {err}")
    cport = str(p["container_port"]) if p.get("container_port") is not None else ui.ask(
        "Port the app listens on inside the container (empty = none)", example="80",
        check=lambda a: None if a == "" or a.isdigit() else "a number")
    hport = None
    if cport:
        used = used_host_ports(server)
        free = next(n for n in range(int(cport) if int(cport) >= 1024 else 8000, 65535) if n not in used)
        hport = str(p["host_port"]) if p.get("host_port") is not None else ui.ask(
            f"Port on {server} (free ports suggested; taken: {', '.join(map(str, sorted(used))) or 'none'})",
            str(free), check=lambda a: None if a.isdigit() and int(a) not in used else "taken or not a number")
        if int(hport) in used:
            raise SystemExit(f"host port {hport} is taken on {server} (taken: {sorted(used)})")
    if p.get("secret_vars") is not None:
        secret_vars = p["secret_vars"]
    else:
        names = ui.ask("Secret variables, comma-separated (empty = none)", example="DB_PASSWORD,API_KEY",
                       check=lambda a: None)
        secret_vars = [v.strip() for v in names.split(",") if v.strip()]
    mount = p["mount"] if p.get("mount") is not None else ui.ask(
        f"Data directory inside the container, stored on the host in /srv/docker/{app_id} (empty = none)",
        example="/data", check=lambda a: None)
    bad = [v for v in secret_vars if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v)]
    if bad:
        raise SystemExit(f"invalid variable names: {bad} (letters, digits, _ only)")
    if any(b.vpn for b in blocks) and (problem := vpn_config_problem()):
        raise SystemExit(f"--addon gluetun: {problem}")
    taken = used_host_ports(server) | ({int(hport)} if hport else set())
    block_ports: dict[str, int] = {}
    for b in blocks:
        if b.needs_port:
            block_ports[b.id] = next(n for n in range(8000, 65535) if n not in taken)
            taken.add(block_ports[b.id])
    opts = serviceopts.options_from(p)
    fill = placeholders(app_id, *app_url(app_id, server, p, int(hport) if hport else None))
    opts = dataclasses.replace(opts, env={k: fill(v) for k, v in opts.env.items()})
    chosen = chosen_templates(p)
    text = skeleton(app_id, image, int(cport) if cport else None, int(hport) if hport else None, secret_vars, mount or None,
                    blocks, block_ports, server_address(server), opts, chosen[0] if chosen else None, fill)
    for t in chosen[1:]:
        text = with_extra_services(text, extra_template(app_id, server, t, taken))
    return text


def _ask_routing(app: App, ports: list[tuple[str, int, int]], p: dict) -> bool:
    ui.step("Routing + certificate", "NPM gets a proxy host for the domain; ingressctl issues its certificate from the"
            " homelab CA\n   (Infisical) and renews it.")
    if p.get("web_port") is not None:
        if p["web_port"] not in [h for _, h, _ in ports]:
            raise SystemExit(f"--web-port {p['web_port']} is not a published host port ({[h for _, h, _ in ports]})")
        app.upstream_port = p["web_port"]
    elif len(ports) == 1:
        app.upstream_port = ports[0][1]
        ui.ok(f"web port: {ports[0][1]} on {app.server} (container port {ports[0][2]})")
    else:
        i = ui.choose("Which published port is the web interface?",
                      [f"{s}: host {h} -> container {c}" for s, h, c in ports], flag="--web-port")
        if i is None:
            return False
        app.upstream_port = ports[i][1]
    allowed = settings.policy_domains(ops.ROOT) or [f"*.{settings.domain(ops.ROOT)}"]
    taken = known_domains()
    suffix = settings.domain(ops.ROOT)

    def check_domain(d: str) -> str | None:
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", d):
            return "not a valid host name"
        if not settings.matches_policy(d, ops.ROOT):
            return f"only {allowed} (policy of the certificate authority)"
        return f"already used in {taken[d]}" if d in taken else None

    if p.get("domain"):
        if err := check_domain(p["domain"]):
            raise SystemExit(f"--domain {p['domain']}: {err}")
        app.domain = p["domain"]
    else:
        app.domain = ui.ask("Domain", f"{app.id}.{suffix}", check=check_domain, example=f"{app.id}.{suffix}",
                            flag="--domain")
    app.websockets = p["websockets"] if p.get("websockets") is not None else ui.yes(
        "Pass WebSockets through? (yes for live UIs, e.g. Home Assistant, code-server)")
    app.force_https = p["force_https"] if p.get("force_https") is not None else ui.yes(
        "Redirect http:// to https://? (no if API clients call it via http://)", True)
    app.health_path = p.get("health_path") or ui.ask("Path for the reachability check", "/", example="/ or /health",
                                                     check=lambda a: None if a.startswith("/") else "start with /")
    if p.get("statuses"):
        app.statuses = p["statuses"]
    else:
        st = ui.ask("HTTP status codes that count as up without login", ",".join(map(str, DEFAULT_STATUSES)),
                    example="200,302", check=lambda a: None if all(x.strip().isdigit() for x in a.split(",")) else "numbers, comma-separated")
        app.statuses = [int(x) for x in st.split(",")]
    return True


def resolve_secret(var: str, spec: str) -> str | None:
    """--secret VAR=spec: generate | generate:BYTES | env (value from the environment variable VAR) | keep."""
    kind, _, arg = spec.partition(":")
    if kind == "generate":
        n = int(arg) if arg.isdigit() and int(arg) > 0 else 32
        return secrets.token_hex(n)
    if kind == "env":
        value = os.environ.get(arg or var)
        if not value:
            raise SystemExit(f"--secret {var}=env: environment variable {arg or var} is not set or empty")
        return value
    if kind == "keep":
        return None
    raise SystemExit(f"--secret {var}={spec}: use generate, generate:BYTES, env, env:OTHER_VAR or keep")


def gather(p: dict | None = None) -> App | None:
    """Questions; p holds presets (agent mode flags), missing ones are asked (or fail in agent mode)."""
    p = p or {}
    ui.step("Step 1: app", "The app ID names the folder stacks/<id>, the Komodo stack and the inventory entry.")
    app_id = p.get("app_id") or ui.ask("App ID", check=_check_app_id, example="whoami-test", flag="--app-id")
    if err := _check_app_id(app_id):
        raise SystemExit(f"--app-id {app_id}: {err}")
    inv = ops.inventory()
    if app_id in inv:
        server = inv[app_id].get("server") or sys.exit(f"{app_id}: inventory entry without server")
        if p.get("server") and p["server"] != server:
            raise SystemExit(f"{app_id} is in the inventory on {server}, not {p['server']}")
        ui.ok(f"{app_id} is already in the inventory (server {server}): continuing that run")
    else:
        servers = yaml.safe_load((ROOT / "inventory/servers.yaml").read_text()).get("servers") or {}
        names = sorted(n for n, s in servers.items() if s.get("address") and s.get("status") == "verified")
        if p.get("server"):
            if p["server"] not in names:
                raise SystemExit(f"--server {p['server']}: not one of {names}")
            server = p["server"]
        else:
            counts = {n: sum(s.get("server") == n for s in inv.values()) for n in names}
            i = ui.choose("Step 2: which Komodo server runs it?",
                          [f"{n}  ({servers[n]['address']}, {counts[n]} stacks)" for n in names], flag="--server")
            if i is None:
                return None
            server = names[i]

    compose_path = ROOT / "stacks" / app_id / "compose.yaml"
    blocks: list[addons.Addon] = []
    if compose_path.is_file():
        ui.ok(f"reading the existing {compose_path.relative_to(ROOT)}")
        if p.get("compose_file"):
            raise SystemExit(f"--compose-file: {compose_path.relative_to(ROOT)} exists already; change it in git")
        if p.get("addon"):
            ui.warn("--addon is ignored: the compose file exists, add the services to it yourself")
        app = App(app_id, server, compose_path.read_text(), False)
    elif app_id in inv:
        raise SystemExit(f"{app_id} is in the inventory, but {compose_path.relative_to(ROOT)} is missing")
    elif p.get("compose_file"):
        app = App(app_id, server, _read_compose_file(p["compose_file"], app_id, server), True)
        ui.ok("using the compose file from --compose-file")
    else:
        blocks = _choose_addons(p)
        app = App(app_id, server, _ask_compose(app_id, server, p, blocks), True)
    chosen = chosen_templates(p) if app.compose_is_new else []
    if chosen:
        template = chosen[0]
        app.template, app.files = ", ".join(t.id for t in chosen), template_files(app.compose, app_id, chosen, p.get("files_dir"))
        notes = [(template, template.first_run)] + extra_notes(app.compose, app_id, server, chosen)
        app.first_run = "\n".join(f"{t.label}: {text}" if len(chosen) > 1 else text for t, text in notes if text)
        app.host_paths = [path if service == app_id else f"{path} of {service}" for service, path in stand_ins(app.compose, app_id, chosen)]
        p = routing_defaults(p, template)
    elif p.get("template") or p.get("templates"):
        ui.warn("--template is ignored: the compose file exists")
    generated = {v for b in blocks for v in b.secrets}   # the blocks' passwords: nobody needs to choose them
    for t in chosen:
        generated |= t.secrets() if t is chosen[0] else extra_secrets(t)   # and the templates' own keys

    ingress_path = app.dir / "ingress.yaml"
    ports = [p_ for p_ in compose_ports(app.compose) if p_[0] not in tool_services(app.compose)]   # the app's, not a tool's
    if ingress_path.is_file():
        svc = (yaml.safe_load(ingress_path.read_text())["services"] or [{}])[0]
        app.domain, app.upstream_port = svc["proxy"]["domains"][0], svc["proxy"]["upstream"]["port"]
        app.health_path = (svc.get("healthcheck") or {}).get("path", "/")
        app.statuses = (svc.get("healthcheck") or {}).get("accepted_statuses", DEFAULT_STATUSES)
        ui.ok(f"routing from {ingress_path.relative_to(ROOT)}: https://{app.domain} -> {server}:{app.upstream_port}")
    elif not ports:
        ui.warn("the compose file publishes no port: no routing (add `ports:` and start again if you want it)")
    elif p.get("routing") if p.get("routing") is not None else ui.yes(
            f"\nStep 3: make it reachable via HTTPS, e.g. https://{app_id}.{settings.domain(ops.ROOT)}?", True):
        if not _ask_routing(app, ports, p):
            return None

    required, optional = compose_vars(app.compose)
    preset = p.get("secrets") or {}
    unknown = set(preset) - required
    if unknown:
        raise SystemExit(f"--secret for variables the compose file does not use as ${{VAR}}: {sorted(unknown)}")
    if required:
        ui.step("Step 4: secrets", "Each ${VAR} in the compose file gets its value from Infisical at deploy time;"
                " values never go to Git.")
        if optional:
            print(f"   with a default in the compose file, not asked: {', '.join(sorted(optional))}")
        for var in sorted(required):
            if var in preset or var in generated or p.get("generate_secrets"):
                app.secrets[var] = resolve_secret(var, preset.get(var, "generate"))
            else:
                app.secrets[var] = ui.secret_value(var, allow_skip=True)[0]

    ui.step("Summary")
    print(f"  app {app.id} on {app.server}, Komodo stack {app.stack}")
    print(f"  compose: stacks/{app.id}/compose.yaml ({'new' if app.compose_is_new else 'existing'})")
    if blocks:
        print(f"  building blocks: {', '.join(b.id for b in blocks)}")
    if app.files:
        print(f"  files from the template {app.template}: {', '.join(app.files)}")
    print(f"  routing: {f'https://{app.domain} -> {app.server}:{app.upstream_port}' if app.domain else 'none'}")
    print(f"  secrets: {', '.join(k + ('' if v else ' (keep existing / later)') for k, v in app.secrets.items()) or 'none'}")
    for target in app.host_paths:
        ui.warn(f"the template's folder for {target} is a stand-in: change the volume to a folder that exists on {app.server}")
    return app if ui.confirm("Set it up now? (asks again before Komodo, Infisical and the push)") else None


# --- steps --------------------------------------------------------------------------------------------------

def write_files(app: App) -> list[str]:
    ui.step("Files", "Only in your working tree until the commit.")
    paths = [f"stacks/{app.id}", "inventory/stacks.yaml"]
    app.dir.mkdir(parents=True, exist_ok=True)
    if app.compose_is_new:
        (app.dir / "compose.yaml").write_text(app.compose)
        ui.ok(f"wrote stacks/{app.id}/compose.yaml")
    for name, text in app.files.items():
        target = app.dir / name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            ui.ok(f"wrote stacks/{app.id}/{name} (template {app.template})")
    if app.domain and not (app.dir / "ingress.yaml").is_file():
        (app.dir / "ingress.yaml").write_text(manifest(app))
        ui.ok(f"wrote stacks/{app.id}/ingress.yaml")
    inv_path = ROOT / "inventory/stacks.yaml"
    if app.id not in ops.inventory():
        text = inv_path.read_text()
        inv_path.write_text(text + ("" if text.endswith("\n") else "\n") + inventory_block(app))
        ui.ok("added the entry to inventory/stacks.yaml")
    elif app.secrets and ops.inventory()[app.id].get("secrets") != "infisical":
        raise SystemExit(f"{app.id}: add `secrets: infisical` to its inventory entry, then start again")
    else:
        ui.ok("inventory entry exists")
    return paths


def validate(app: App) -> None:
    ui.step("Validation", "Same checks as CI: inventory, compose files, ingress manifest.")
    import stackctl
    errors, warnings = stackctl.validate(ROOT)
    for w in warnings:
        ui.warn(w)
    if errors:
        raise SystemExit("stackctl validate:\n  " + "\n  ".join(errors))
    ui.ok("inventory and compose files")
    if not app.domain:
        return
    sys.path.insert(0, str(ROOT / "infra/ingress-controller/src"))
    try:
        from ingressctl.cli import main as ingressctl_main
    except ImportError:
        ui.warn("ingressctl is not installed here (pip install -e infra/ingress-controller); CI checks the manifest")
        return
    if ingressctl_main(["validate", "--root", str(ROOT)]) not in (0, None):
        raise SystemExit("ingressctl validate reports errors (see above)")
    ui.ok("ingress.yaml")


def komodo_stack(app: App, komodo) -> None:
    ui.step("Komodo stack", "Points at the server's clone of this repo; it sees the files after the push and pull.")
    import create_stack
    import rebuild
    if any(s["name"] == app.stack for s in komodo.read("ListStacks")):
        ui.ok(f"stack {app.stack} exists")
    else:
        config = create_stack.stack_config(app.entry)
        print(f"  run_directory = {config['run_directory']}")
        if not ui.confirm(f"Create Komodo stack {app.stack} on {app.server}?"):
            raise SystemExit("cancelled")
        create_stack.create(app.entry, config)
        ui.ok("created")
    if create_stack.builds_image(app.entry):
        if rebuild.add(app.stack):
            ui.ok("rebuilt and redeployed after every push (stage Build of update-all)")


def infisical_secrets(app: App, infisical, komodo) -> None:
    if not app.secrets:
        return
    ui.step("Secrets in Infisical", f"Project {project_slug(app.server)}, folder /{app.id}; the Komodo stack gets the"
            " wrapper that injects them.")
    if not ui.confirm("Store the values and set the wrapper now?"):
        raise SystemExit("cancelled")
    import stack_secrets
    values = {k: v for k, v in app.secrets.items() if v}
    stack_secrets.sync(infisical(), komodo, [app.entry], os.environ["INFISICAL_URL"].rstrip("/"),
                       {app.id: values} if values else {}, apply=True)


def ensure_deployed(app: App, komodo) -> bool:
    ui.step("Deploy")
    state = komodo.read("ListStacks", {"query": {"names": [app.stack]}})[0]["info"]["state"]
    if state == "running":
        ui.ok("running")
        return True
    ui.warn(f"state {state}")
    if not ui.confirm(f"Deploy {app.stack} now?"):
        return False
    u = komodo.execute_and_poll("DeployStack", {"stack": app.stack}, timeout=900)
    for log in u.get("logs") or []:
        if not log["success"]:
            print(f"  {log['stage']}:\n" + (log.get("stdout", "") + log.get("stderr", ""))[-1500:])
    time.sleep(10)
    state = komodo.read("ListStacks", {"query": {"names": [app.stack]}})[0]["info"]["state"]
    (ui.ok if state == "running" else ui.warn)(f"state {state}")
    return state == "running"


def ensure_routing(app: App, sha: str) -> bool:
    if not app.domain:
        return True
    ui.step("Routing check", "HTTPS through NPM with the homelab CA, then DNS.")
    address = ops.npm_address()
    cafile = ROOT / ops.ingress_cfg()["issuers"]["homelab-private-ca"]["ca_bundle"]
    status = ops.https_status(app.domain, app.health_path, address, cafile)
    if status not in app.statuses:
        ui.warn(f"https://{app.domain}{app.health_path} -> {status}")
        if ui.confirm("Run ingressctl apply for this app now (NPM host + certificate)?"):
            import run_ingressctl
            run_ingressctl.run(["apply", "--root", "/repo", "--commit", sha, "--stack", app.id])
            status = ops.https_status(app.domain, app.health_path, address, cafile)
    if status in app.statuses:
        ui.ok(f"https://{app.domain}{app.health_path} -> {status}, certificate from the homelab CA is valid")
    else:
        ui.warn(f"https://{app.domain}{app.health_path} -> {status}; check the healthcheck status codes or the upstream")
    try:
        resolved = socket.gethostbyname(app.domain)
    except OSError:
        resolved = None
    if resolved == address:
        ui.ok(f"DNS: {app.domain} -> {address}")
    else:
        ui.warn(f"DNS: {app.domain} -> {resolved or 'does not resolve'}; add a DNS rewrite in AdGuard:"
                f" {app.domain} -> {address}")
    return status in app.statuses


def block_services(text: str) -> set[str]:
    """Services of a compose file that are one of the building blocks: named like the block and running its image."""
    services = (yaml.safe_load(text) or {}).get("services") or {}
    out = set()
    for b in addons.catalog().values():
        image = (services.get(b.id) or {}).get("image") if isinstance(services.get(b.id), dict) else None
        if image and str(image).split(":")[0] == b.info()["image"].split(":")[0]:
            out.add(b.id)
    return out


def tool_services(text: str) -> set[str]:
    """Block services with a port of their own (UIs, the S3 console): not the app's, never a domain for it."""
    catalog = addons.catalog()
    return {s for s in block_services(text) if catalog[s].needs_port}


def form_values(text: str, app_id: str) -> dict:
    """What the generator would have been given to produce this compose file, as far as it can be read back: the values
    of the form for the app's own service (named like the app, else the first one). Lets a UI show an edited file's
    image, port, volumes, networks, limits, GPU, environment, secrets and building blocks in its fields."""
    doc = yaml.safe_load(text) or {}
    services = doc.get("services") or {}
    name = app_id if app_id in services else next(iter(services), "")
    svc = services.get(name) or {}
    blocks = addons.catalog()
    found = sorted((s for s in block_services(text) if s != name), key=list(blocks).index)
    mode = str(svc.get("network_mode") or "")
    owner = mode.split(":", 1)[1] if mode.startswith("service:") else name   # behind a VPN the port is published there
    port = next(((h, c) for s, h, c in compose_ports(text) if s == owner), None)
    # Variables the blocks add to the app are theirs; `NAME: ${NAME}` is a secret variable, anything else plain environment.
    theirs = {k for b in found for k in blocks[b].app_env}
    env = serviceopts.env_dict(svc.get("environment"))
    secret_vars = sorted(k for k, v in env.items() if v == "${%s}" % k and k not in theirs)
    opts = serviceopts.read_options(svc, app_id, doc, theirs)
    mount = next((v["target"] for v in opts["volumes"] if v["kind"] == "data" and not v["source"] and not v["readOnly"]), None)
    return {"image": svc.get("image"), "containerPort": port[1] if port else None, "hostPort": port[0] if port else None,
            "ports": [[h, c] for s, h, c in compose_ports(text) if s == owner],   # all of them: the first is not always the form's
            "mount": mount, "secretVars": secret_vars, "addons": found, **opts}


def compose_preview(p: dict) -> dict:
    """What `new-app` would use as compose file for these flags (the existing one, --compose-file, or the generated
    one) and what the wizard finds in it. Changes nothing."""
    app_id = p.get("app_id") or ""
    if err := _check_app_id(app_id):
        raise SystemExit(f"--app-id {app_id}: {err}")
    existing = ROOT / "stacks" / app_id / "compose.yaml"
    server = (ops.inventory().get(app_id) or {}).get("server") or p.get("server")
    servers = yaml.safe_load((ROOT / "inventory/servers.yaml").read_text()).get("servers") or {}
    if not existing.is_file() and (server not in servers or not servers[server].get("address")):
        raise SystemExit(f"--server {server}: not one of {sorted(n for n, s in servers.items() if s.get('address'))}")
    if existing.is_file():
        text = existing.read_text()
    elif p.get("compose_file"):
        text = _read_compose_file(p["compose_file"], app_id, server)
    else:
        with contextlib.redirect_stdout(io.StringIO()):   # the generator talks to people; only the result is the answer
            text = _ask_compose(app_id, server, p, _choose_addons(p))
    required, optional = compose_vars(text)
    tools = tool_services(text)
    chosen = [] if existing.is_file() else chosen_templates(p)
    files = template_files(text, app_id, chosen, p.get("files_dir"))
    return {"compose": text, "existing": existing.is_file(), "files": files, "secrets": sorted(required), "optional": sorted(optional),
            "standIn": [{"service": s, "path": path} for s, path in stand_ins(text, app_id, chosen)],
            "notes": [{"template": t.id, "text": note} for t, note in extra_notes(text, app_id, server, chosen)],
            "ports": [{"service": s, "host": h, "container": c} for s, h, c in compose_ports(text) if s not in tools],
            "blockPorts": [{"service": s, "host": h, "container": c, "path": addons.catalog()[s].url_path}
                           for s, h, c in compose_ports(text) if s in tools],
            "form": form_values(text, app_id)}


def first_run(app: App) -> str:
    """The template's note on what to do once the app runs, with its address filled in."""
    return placeholders(app.id, app.domain or "", f"https://{app.domain}" if app.domain else "")(app.first_run)


def pr_body(app: App) -> str:
    lines = [f"New app `{app.id}` on `{app.server}`, created with `./homelab.py new-app --pr`.", "",
             f"- files: `stacks/{app.id}/`, entry in `inventory/stacks.yaml`",
             f"- routing: {f'https://{app.domain} -> {app.server}:{app.upstream_port}' if app.domain else 'none'}",
             f"- secrets in Infisical (`{project_slug(app.server)}`, folder `/{app.id}`): {', '.join(sorted(app.secrets)) or 'none'}", "",
             "The Komodo stack and the secrets already exist; the merge makes `update-all` deploy the app.",
             *([f"First run: {first_run(app)}"] if app.first_run else []),
             f"If this is closed instead: `./homelab.py remove-app --app-id {app.id} --server {app.server} --yes` removes stack and secrets."]
    return "\n".join(lines)


def run(infisical, komodo, preset: dict | None = None) -> bool:
    """True when the app ends up running (and reachable, with routing); with preset["pr"] when the pull request is open.
    preset: agent mode flags."""
    preset = preset or {}
    forgejo = None
    if preset.get("pr"):
        import forgejo as forgejo_client
        remote = ops.git("remote", "get-url", "origin")
        forgejo = forgejo_client.Forgejo.from_env(remote)  # fail before anything changes
        ops.NET_ENV = forgejo.git_env(remote)              # fetch and push of the branch authenticate with the token
    app = gather(preset)
    if app is None:
        print("cancelled")
        return False
    back = ops.start_branch(f"new-app/{app.id}") if forgejo else ""
    paths = write_files(app)
    validate(app)
    komodo_stack(app, komodo)
    infisical_secrets(app, infisical, komodo)
    message = f"feat({app.id}): add app via homelab tool"
    if forgejo:
        url = ops.open_pull_request(paths, message, message, pr_body(app), forgejo, back)
        ui.step("Done")
        print(f"  pull request: {url}")
        print(f"  Merge it to deploy {app.id} on {app.server}; update-all does the rest.")
        if app.first_run:
            print(f"  First run: {first_run(app)}")
        return True
    sha, pushed_now = ops.commit_and_push(paths, message)
    ops.wait_for_update_all(app.server, sha, komodo, pushed_now)
    healthy = ensure_deployed(app, komodo) and ensure_routing(app, sha)
    ui.step("Done" if healthy else "Done, with problems (see above)")
    print(f"  {app.id} on {app.server}" + (f": https://{app.domain}" if app.domain else ""))
    if app.first_run:
        print(f"  First run: {first_run(app)}")
    print("  Change secrets later: ./homelab.py (Manage secrets) or ./homelab.py secrets set " + app.id + " <KEY>")
    return healthy
