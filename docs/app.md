# homegraf

A live map of the homelab: machines, Komodo stacks, containers, domains and the platform services behind them, in several views (catalog, hosting, network, request path, dependencies, platform, status). Everything is generated from the repository your homelab is defined in and updates itself on every push; the live state comes from Komodo.

One container: Hono serves the API and the built React app, and runs `infra/graph/build.py` of your repository (a copy of it is in `seed/`) against a read-only checkout of it, mounted at `/repo`.

## Data

| Source | Gives |
| --- | --- |
| `inventory/servers.yaml`, `stacks.yaml` | machines, stacks, which stack runs where |
| `stacks/*/compose*.yaml` | containers, images, published ports, `depends_on` |
| `stacks/*/ingress.yaml`, `external/ingress.yaml` | domains and the container or appliance behind each |
| `inventory/npm-hosts.yaml` | snapshot of the NPM hosts made by hand; refresh with `python3 infra/graph/npm_snapshot.py` (read-only, uses `infra/secrets/npm.env`) |
| `inventory/network.yaml` | hand-kept layer: Proxmox and the LXCs, machines outside Komodo, annotations; its ISP, router and switch placeholders show only while the UniFi source below is off |
| UniFi controller (`infra/graph/unifi.py`) | the network layer: internet connections, gateway, switches and access points with the port each hangs on, VLANs and WLANs, and the switch port or access point of every machine the repository knows by address; read-only, every few minutes |
| Komodo API (`ListStacks`, `ListServers`) | live state, pushed to the browser over SSE |

Dependencies between stacks are inferred from environment values that name another stack's domain or `<server>:<port>`; they are drawn dashed. Add anything else as a `links:` entry in `inventory/network.yaml`.

## Run

The image is `ghcr.io/belphegorprime/homegraf`; how to start it alone, and which environment turns on what, is in the [README](../README.md#run-the-image). The all-in-one stack in [stack.md](stack.md) runs it together with Forgejo, Komodo, Infisical and the proxy.

In a homelab that Komodo deploys, homegraf is a stack like any other: the compose file names the image with a fixed version tag, and the repository checkout it reads can be Komodo's own clone on the server (`<periphery-root>/data/komodo/repos/<repo>`), which `update-all` refreshes on each push. A new version is a change of the tag.

## UniFi

With `UNIFI_URL`, `UNIFI_USER` and `UNIFI_PASSWORD` (Infisical) the server reads the local UniFi OS controller (UDM SE) through `infra/graph/unifi.py` and hands the snapshot to the graph builder, which replaces the placeholders of `inventory/network.yaml`. The account only needs to read (a local user with the read-only role). The script logs in, reads devices, networks, WLANs and clients, and logs out; of the clients it keeps counts per device and, for the machines the repository knows by address, the switch port or access point. No client names, no MAC addresses of other clients, no WLAN passwords. The answer is kept five minutes; if a read fails the last good one is used for up to an hour, then the map falls back to the placeholders. The header shows `UniFi live` or `UniFi unavailable` (hover for the reason). The state each device reports (connected, offline, updating …) is its health on the map, like a stack's: an access point or switch that drops off turns red and appears in the **Status** tab.

The controller's certificate is self-signed, so it is not verified; set `UNIFI_FINGERPRINT` (SHA-256 of the certificate) to pin it instead. Guests of a hand-kept host (the LXCs of the Proxmox host in `machine_uplinks`) stay on that host, not on the switch port they share with it.

## Develop

```bash
cd app && npm ci
REPO_DIR=../seed npm run dev:server  # API on :8080; the seed is a small sample repository, or point REPO_DIR at your own checkout
npm run dev:web                      # Vite with /api proxied to :8080
npm run typecheck && npm test
```

The interface uses Tailwind CSS 4 and [shadcn/ui](https://ui.shadcn.com) (Base UI, preset `b2BWxfZkg`: `base-nova`, base colour mist, green primary, Geist Mono, Lucide icons). `components.json` holds the preset; the components live in `src/web/components/ui/` and belong to this repository, add more with `npx shadcn@latest add <name>` and pin the versions it installs in `package.json` like the others. The theme tokens and the app's own classes (graph cards, status colours, the New app layout) are in `src/web/index.css`; light and dark follow the system until the toggle in the header is used.

The tools the app runs are in `seed/infra/` and `seed/homelab.py`, listed in [`TOOLS`](../TOOLS); they are developed here (see [CONTRIBUTING.md](../CONTRIBUTING.md)). The app's tests (`npm test`) and the type check run on GitHub for every pull request ([image.yaml](../.github/workflows/image.yaml)), before an image is published.

## Where things are developed

This repository is the home of the app: `app/` and this file are developed and changed here, and GitHub Actions builds, scans and publishes the image from it. The README, the license, the all-in-one `compose.yaml`, the bootstrap, the scripts and the workflows are here too.

The tools in `seed/` (`homelab.py`, `infra/graph`, `infra/homelab`, `infra/infisical`, `infra/ingress-controller`, `infra/stackctl`, `infra/komodo/...`, the list is [`TOOLS`](../TOOLS)) are the initial content of a new installation's own infrastructure repository, and the app calls them (`homelab.py <command> --json`, the graph builder). They are developed here: a new template, a change to the wizard or to the graph builder comes with the change to the app that shows it. **Exposed:** a tag of this repository pins a version of exactly the paths in `TOOLS`, and the author's homelab repository takes such a tag with `infra/tools/update-tools.sh`, which writes the files to the same paths there (servers, Komodo and CI use them in place), records the tag and a hash of the files in a lock, and lets CI fail when a file differs from the lock. The skeleton around the tools (`seed/inventory`, `seed/stacks`, `seed/README.md`) is not exposed.

`scripts/check-seed.sh` refuses private addresses and home folders in the tools and runs their tests from an empty copy of `seed/`; the tools stay generic, and what is specific to one installation lives in `inventory/`.

The contract between the two: the app depends on the commands and the JSON they print (`addons`, `templates`, `removable`, `stacks`, `hosts`, `new-app --print-compose`, the job commands) and on the graph builder's output. A change on either side keeps the other working, or raises the version of the app in the same step.
