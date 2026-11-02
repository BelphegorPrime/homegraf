#!/usr/bin/env python3
"""Homelab tool: set up a new app (compose, routing, certificate, Komodo stack, secrets), remove an app, manage secrets.

  ./homelab.py                                   # interactive menus (for people)
  ./homelab.py new-app --app-id ID ... [--yes]   # agent mode: no prompts, dry run without --yes (--pr: pull request)
  ./homelab.py addons [--json]                   # building blocks for new-app --addon (postgres, redis, ...)
  ./homelab.py templates [--json]                # starting points for new-app --template (web servers, ready-made apps)
  ./homelab.py remove-app --app-id ID [--yes]
  ./homelab.py hosts [--json]                    # hosts ingressctl manages, and whether each redirects HTTP to HTTPS
  ./homelab.py force-https DOMAIN on|off [--pr] [--yes]
  ./homelab.py secrets list|get|set|delete ...   # same as infra/infisical/secret.py
  ./homelab.py status [STACK ...]                # Komodo state (read-only)
  ./homelab.py logs STACK [--tail N] [--grep T]  # container logs (read-only)
  ./homelab.py deploy STACK [--yes]              # DeployStack, e.g. after a secret change

Agent mode: every question comes from a flag or its default; a missing answer fails with the flag to pass.
Without --yes the run stops before the first outward step (Komodo, Infisical, git push) and changes nothing.
Exit codes: 0 done (or dry run complete), 1 error or failed check, 130 interrupted.
Komodo access from infra/komodo/komodo.env; Infisical (org-admin identity) from INFISICAL_URL,
INFISICAL_BOOTSTRAP_CLIENT_ID, INFISICAL_BOOTSTRAP_CLIENT_SECRET, asked interactively if missing.
Agent guide: AGENTS.md. Background: docs/plans/05-infisical-stack-secrets.md, docs/operations.md#adding-a-service.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
for _p in ("infra/homelab", "infra/komodo", "infra/infisical"):
    sys.path.insert(0, str(ROOT / _p))
import addons  # noqa: E402
import hostopts  # noqa: E402
import komodo_api  # noqa: E402
import newapp  # noqa: E402
import removeapp  # noqa: E402
import secret  # noqa: E402
import secrets_menu  # noqa: E402
import stackops  # noqa: E402
import templates  # noqa: E402
import ui  # noqa: E402

_infisical = None


def infisical():
    """Infisical client, logged in on first use."""
    global _infisical
    if _infisical is None:
        secret.credentials()
        from bootstrap import Api
        try:
            _infisical = Api()
        except SystemExit:  # wrong credentials: ask again next time
            for k in ("INFISICAL_BOOTSTRAP_CLIENT_ID", "INFISICAL_BOOTSTRAP_CLIENT_SECRET"):
                os.environ.pop(k, None)
            raise
    return _infisical


MENU = [
    "New app (or continue an interrupted one)  e.g. a new service from an image, reachable at https://<app>.<your domain>",
    "Remove app                                e.g. a test app: stack, files, routing, secrets",
    "Manage secrets                            e.g. change the DB password of paperless",
]


def interactive() -> int:
    while True:
        i = ui.choose("Homelab", MENU)
        try:
            if i is None:
                return 0
            if i == 0:
                newapp.run(infisical, komodo_api)
            elif i == 1:
                removeapp.run(infisical, komodo_api)
            elif not secrets_menu.run(infisical):
                return 0
        except SystemExit as e:  # a flow was cancelled: back to the main menu
            if e.code not in (None, 0):
                print(f"\n  ✗ {e.code}")


def _kv(s: str) -> tuple[str, str]:
    var, sep, spec = s.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"expected VAR=SPEC, e.g. DB_PASSWORD=generate, not {s!r}")
    return var, spec


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Without a subcommand: interactive menus. Agent guide: AGENTS.md")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("new-app", help="set up (or continue setting up) an app",
                       description="Set up an app end to end. Reads stacks/<id>/compose.yaml if it exists, otherwise "
                                   "writes a skeleton from --image/--container-port/--secret-var/--mount.",
                       epilog="example: ./homelab.py new-app --app-id whoami-test --server worker-1 "
                              "--image traefik/whoami:v1.11.0 --container-port 80 --secret-var API_TOKEN "
                              "--secret API_TOKEN=generate --yes")
    p.add_argument("--app-id", required=True, help="folder stacks/<id>, Komodo stack, inventory ID (e.g. whoami-test)")
    p.add_argument("--server", help="Komodo server from inventory/servers.yaml (e.g. worker-1); required for a new app")
    g = p.add_argument_group("compose skeleton (only when stacks/<id>/compose.yaml does not exist)")
    g.add_argument("--image", help="image with a fixed tag (e.g. traefik/whoami:v1.11.0)")
    g.add_argument("--container-port", type=int, help="port inside the container (e.g. 80); omit for none")
    g.add_argument("--host-port", type=int, help="published port on the server (default: first free one)")
    g.add_argument("--secret-var", action="append", default=None, metavar="VAR", help="secret env var, repeatable")
    g.add_argument("--addon", action="append", default=None, metavar="NAME",
                   help="add a ready-made service next to the app (see ./homelab.py addons), repeatable, e.g. postgres")
    g.add_argument("--volume", action="append", default=None, metavar="KIND:SOURCE:TARGET[:ro]",
                   help="volume of the app, repeatable. KIND: data (below /srv/docker/<id>), bind (host path), named, "
                        "tmpfs (tmpfs::/tmp:size=512m), repo (./path in this repo); e.g. data::/data, bind:/mnt/tank/media:/media:ro")
    g.add_argument("--network", action="append", default=None, metavar="NAME", help="also join this network of the stack, repeatable")
    g.add_argument("--external-network", action="append", default=None, metavar="NAME", help="also join this existing Docker network, repeatable")
    g.add_argument("--cpus", metavar="N", help="CPU limit, e.g. 1.5")
    g.add_argument("--memory", metavar="SIZE", help="memory limit, e.g. 512m or 2g")
    g.add_argument("--gpu", choices=("nvidia", "dri"), help="GPU: nvidia (reservation of all GPUs) or dri (/dev/dri, Intel/AMD)")
    g.add_argument("--env", action="append", default=None, metavar="NAME=value", help="plain environment variable, repeatable (secrets: --secret-var)")
    g.add_argument("--template", metavar="NAME", action="append", help="start from a template (see ./homelab.py templates): its image, port, "
                   "volumes and environment are the defaults when --image is not given, it brings its building blocks and sidecar services, "
                   "and the files its volumes mount from the repo are written to stacks/<id>/. Repeat it to put several templates in one compose "
                   "file: the first is the app <id>, each further one is the service <id>-<template> with building blocks, folders and secrets "
                   "of its own (the form's flags are for the first)")
    g.add_argument("--files-dir", metavar="DIR", help="edited copies of the template's files; used instead of the template's own")
    g.add_argument("--compose-file", metavar="PATH",
                   help="use this finished compose file as stacks/<id>/compose.yaml instead of generating one")
    g.add_argument("--mount", default=None, help="data dir in the container, host side /srv/docker/<id> (e.g. /data)")
    g = p.add_argument_group("routing (https://<domain> through NPM, certificate from the homelab CA)")
    g.add_argument("--no-routing", dest="routing", action="store_false", default=None, help="no NPM host")
    g.add_argument("--domain", help="default <id>.<domain>, the domain of inventory/homegraf.yaml")
    g.add_argument("--web-port", type=int, help="published host port of the web UI, if the compose file has several")
    g.add_argument("--websockets", action=argparse.BooleanOptionalAction, default=None, help="default: off")
    g.add_argument("--force-https", action=argparse.BooleanOptionalAction, default=None, help="default: on")
    g.add_argument("--health-path", help="default /")
    g.add_argument("--statuses", help="accepted HTTP codes without login, default 200,301,302,307,401")
    g = p.add_argument_group("secrets (values never on the command line)")
    g.add_argument("--generate-secrets", action="store_true",
                   help="generate a value for every ${VAR} without an explicit --secret (for a compose file you wrote)")
    g.add_argument("--secret", action="append", type=_kv, default=[], metavar="VAR=SPEC",
                   help="SPEC: generate | generate:BYTES | env (value from $VAR) | env:OTHER | keep; one per ${VAR}")
    p.add_argument("--print-compose", action="store_true",
                   help="only print what the flags give as compose file, plus its secrets and ports, as JSON; changes nothing")
    p.add_argument("--pr", action="store_true",
                   help="deliver the files as a Forgejo pull request instead of pushing main (needs FORGEJO_TOKEN); "
                        "stack and secrets are created at once, the merge deploys")
    p.add_argument("--yes", action="store_true", help="apply; without it the run stops before the first outward step")

    p = sub.add_parser("addons", help="list the building blocks for new-app --addon",
                       epilog="example: ./homelab.py addons --json")
    p.add_argument("--json", action="store_true", help="machine-readable: id, label, description, secrets, image")

    p = sub.add_parser("templates", help="list the templates for new-app --template",
                       epilog="example: ./homelab.py templates --json")
    p.add_argument("--json", action="store_true", help="machine-readable: id, label, description, defaults and files")

    p = sub.add_parser("remove-app", help="remove an app (stack, files, routing, secrets)",
                       epilog="example: ./homelab.py remove-app --app-id whoami-test --yes")
    p.add_argument("--app-id", required=True)
    p.add_argument("--server", help="only for an app already gone from the inventory (half-removed, or with --finish)")
    p.add_argument("--pr", action="store_true",
                   help="first half: destroy the Komodo stack now, deliver the removal of files and inventory entry as a "
                        "Forgejo pull request (needs FORGEJO_TOKEN); routing and secrets stay for --finish")
    p.add_argument("--finish", action="store_true",
                   help="second half, after the pull request is merged: NPM host + certificate and the Infisical folder")
    p.add_argument("--yes", action="store_true", help="apply; without it the run only shows what would happen")

    p = sub.add_parser("stacks", help="list the stacks the UI may redeploy and manage secrets of",
                       epilog="example: ./homelab.py stacks --json")
    p.add_argument("--json", action="store_true", help="machine-readable: id, server, Komodo stack, secrets in Infisical")

    p = sub.add_parser("removable", help="list the apps remove-app may remove",
                       epilog="example: ./homelab.py removable --json")
    p.add_argument("--json", action="store_true", help="machine-readable: id, server, domains, data paths")

    p = sub.add_parser("hosts", help="list the hosts ingressctl manages (external/ingress.yaml, stacks/*/ingress.yaml)",
                       epilog="example: ./homelab.py hosts --json")
    p.add_argument("--json", action="store_true", help="machine-readable: id, stack, file, domains, forceHttps")

    p = sub.add_parser("force-https", help="redirect HTTP to HTTPS for a host, or stop doing so",
                       epilog="example: ./homelab.py force-https app.lab.example on --pr --yes")
    p.add_argument("domain", help="a domain of a host from ./homelab.py hosts")
    p.add_argument("state", choices=("on", "off"))
    p.add_argument("--pr", action="store_true", help="deliver the change as a Forgejo pull request instead of pushing main (needs FORGEJO_TOKEN)")
    p.add_argument("--yes", action="store_true", help="apply; without it the run stops before the first outward step")

    p = sub.add_parser("secrets", help="list|get|set|delete keys of a stack; see ./homelab.py secrets --help",
                       add_help=False)
    p.add_argument("rest", nargs=argparse.REMAINDER)

    p = sub.add_parser("status", help="Komodo state of all stacks, or details for the named ones (read-only)",
                       epilog="exit 1 if a named stack is not running. example: ./homelab.py status paperless immich")
    p.add_argument("stacks", nargs="*", metavar="STACK", help="Komodo stack name (inventory komodo_stack)")

    p = sub.add_parser("logs", help="container logs of a stack (read-only)",
                       epilog="example: ./homelab.py logs paperless --tail 200 --service webserver")
    p.add_argument("stack")
    p.add_argument("--tail", type=int, default=100)
    p.add_argument("--grep", action="append", default=[], metavar="TERM", help="search the last 5000 lines instead")
    p.add_argument("--service", action="append", default=[], help="only this compose service, repeatable")

    p = sub.add_parser("deploy", help="deploy a stack through Komodo (pull + up)",
                       epilog="example: ./homelab.py deploy paperless --yes")
    p.add_argument("stack")
    p.add_argument("--yes", action="store_true", help="apply; without it only the current state is shown")
    return ap.parse_args(argv)


def preset(args: argparse.Namespace) -> dict:
    p = {k: v for k, v in vars(args).items() if k not in ("cmd", "yes", "secret", "secret_var")}
    if args.cmd == "new-app":
        p["secrets"] = dict(args.secret)
        p["secret_vars"] = args.secret_var
        if args.statuses:
            p["statuses"] = [int(x) for x in args.statuses.split(",")]
    return p


def print_addons(as_json: bool) -> int:
    blocks = addons.catalog()
    if as_json:
        import json
        print(json.dumps([b.info() for b in blocks.values()], indent=2))
    else:
        for b in blocks.values():
            needs = f" (needs {' or '.join(b.requires_any)})" if b.requires_any else ""
            print(f"{b.id:12} {b.label}: {b.description}{needs}")
    return 0


def print_templates(as_json: bool) -> int:
    found = templates.catalog()
    if as_json:
        import json
        print(json.dumps([{"id": t.id, "label": t.label, "description": t.description, "healthPath": t.health_path,
                           "category": t.category, "addons": list(t.addons), "websockets": t.websockets,
                           "statuses": list(t.statuses), "hostPaths": list(t.host_paths), "firstRun": t.first_run,
                           "sidecars": list(t.sidecars), "defaults": newapp.template_defaults(t), "files": list(t.files)}
                          for t in found.values()], indent=2))
    else:
        for t in found.values():
            print(f"{t.id:14} [{t.category}] {t.label}: {t.description}")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["secrets"]:  # passed through whole, including --help
        return secret.main(argv[1:], prog="./homelab.py secrets")
    args = parse_args(argv)
    komodo_api.load_env()
    try:
        if args.cmd is None:
            return interactive()
        ui.AUTO, ui.ASSUME_YES = True, getattr(args, "yes", False)
        if args.cmd == "addons":
            return print_addons(args.json)
        if args.cmd == "templates":
            return print_templates(args.json)
        if args.cmd == "stacks":
            import json
            rows = stackops.managed()
            print(json.dumps(rows, indent=2) if args.json else "\n".join(f"{r['id']:28} {r['server']:17} {r['komodoStack']}" for r in rows))
            return 0
        if args.cmd == "removable":
            import json
            apps = removeapp.removable()
            print(json.dumps(apps, indent=2) if args.json else "\n".join(f"{a['id']:28} {a['server']}" for a in apps))
            return 0
        if args.cmd == "hosts":
            import json
            rows = hostopts.hosts()
            print(json.dumps(rows, indent=2) if args.json else "\n".join(f"{', '.join(h['domains']):40} https-redirect {'on' if h['forceHttps'] else 'off'}  {h['file']}" for h in rows))
            return 0
        if args.cmd == "force-https":
            return 0 if hostopts.run(preset(args)) else 1
        if args.cmd == "new-app" and args.print_compose:
            import json
            print(json.dumps(newapp.compose_preview(preset(args))))
            return 0
        if args.cmd == "status":
            return stackops.status(komodo_api, args.stacks)
        if args.cmd == "logs":
            return stackops.logs(komodo_api, args.stack, args.tail, args.grep, args.service)
        if args.cmd == "deploy":
            return stackops.deploy(komodo_api, args.stack)
        run = newapp.run if args.cmd == "new-app" else removeapp.run
        return 0 if run(infisical, komodo_api, preset(args)) else 1
    except ui.DryRun as e:
        print(f"\n{e.code}")
        return 0
    except SystemExit as e:
        if isinstance(e.code, str):
            sys.stdout.flush()
            print(f"\nerror: {e.code}", file=sys.stderr)
            return 1
        return e.code or 0
    except (KeyboardInterrupt, EOFError):
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
