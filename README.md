# homegraf

**A live map of your homelab, and the tools to change it.** homegraf reads the repository your homelab is defined in
(compose files, an inventory, routing) and draws what runs where, what depends on what, how a request reaches a container,
and whether it is healthy right now. A wizard creates, removes and redeploys apps as reviewable pull requests.

It is one container (a Hono API, a React app, and the Python tools that read the repository), and the centre of a small stack:

| Part | Role |
| --- | --- |
| [Forgejo](https://forgejo.org) | holds the repository your homelab is defined in; every change from homegraf is a pull request there |
| [Komodo](https://komo.do) | deploys the stacks of that repository to your servers and reports their state |
| [Infisical](https://infisical.com) | keeps the secrets of the stacks (passwords, tokens), and can be the certificate authority of your LAN |
| [Nginx Proxy Manager](https://nginxproxymanager.com) | routes `https://<app>.<your domain>` to the containers |
| **homegraf** | the map, the wizard, and the permissions around them |

## What it shows

- **Catalog, Hosting, Request path, Dependencies, Platform:** every service with its links, which machine runs which stack,
  domain -> proxy -> container, what needs what, and who uses Komodo, Infisical, the proxy and the CA.
- **Status:** live state from Komodo for machines, stacks and containers (server-sent events), and what needs attention.
- **Certificates:** the certificate every domain serves right now, read over TLS, with a warning before one expires.
- **Network (optional):** your gateway, switches and access points with the port everything hangs on, read from a UniFi
  controller. Without UniFi (or a hand-written `inventory/network.yaml`) the tab is simply not there; homegraf does not need
  any hardware layer.

## What it changes

Every write action needs a signed-in user with the matching permission, and nothing reaches your main branch without a merge:

| Permission | Allows |
| --- | --- |
| `apps:create` | the **New app** wizard: image, ports, volumes, networks, limits, GPU, building blocks (PostgreSQL, Redis, S3, ...), VPN, templates; a live `compose.yaml` editor that stays in step with the form; creates the Komodo stack, the secrets and a pull request |
| `apps:remove` | destroys an app's stack and opens a pull request that removes it; after the merge it removes routing and secrets |
| `hosts:edit` | switches the redirect from HTTP to HTTPS of a host (a pull request that changes its manifest); the **Certificates** tab also offers the root certificate of your CA for download |
| `stacks:deploy` | redeploys a stack |
| `secrets:manage` | names of a stack's secrets, set, generate, delete (values are never shown) |
| `users:manage` | users and their permissions |

The platform stacks (secrets store, proxy, homegraf itself, Komodo's own) are never offered for removal, redeploy or secrets.

## Run the image

```bash
docker run -d --name homegraf --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  -p 8080:8080 -v /path/to/your/infrastructure-checkout:/repo:ro \
  ghcr.io/belphegorprime/homegraf:main
```

With only a repository mounted at `/repo` you get the map. The more of this you set, the more it does:

| Environment | Turns on |
| --- | --- |
| `KOMODO_URL`, `KOMODO_API_KEY`, `KOMODO_API_SECRET` | live status |
| `HOMEGRAF_PASSWORD` | sign-in: the password of the built-in administrator; more users are added in the UI (kept in the `/data` volume) |
| `HOMEGRAF_ADMIN` | the name of that administrator (default `admin`; the stack sets it to your e-mail address) |
| `FORGEJO_TOKEN`, `HOMEGRAF_CLONE_URL`, `INFISICAL_URL`, `INFISICAL_BOOTSTRAP_CLIENT_ID`, `INFISICAL_BOOTSTRAP_CLIENT_SECRET` (plus the Komodo ones) | the write actions; they run in their own clone in the `/work` volume |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `HOMEGRAF_URL` | sign-in through an OpenID Connect provider (Forgejo, Keycloak, Authentik, ...) next to the password; who may do what is set by name in the UI |
| `OIDC_ADMINS` | provider user names (comma or space separated) that get every permission at their first sign-in; the stack sets it to the administrator of its Forgejo. `OIDC_LABEL` names the button |
| `UNIFI_URL`, `UNIFI_USER`, `UNIFI_PASSWORD` (`UNIFI_FINGERPRINT` to pin the certificate) | the optional network layer |

The image is built by GitHub Actions for amd64 and arm64: `:main`, `:sha-<commit>`, and `:<version>` / `:latest` for tags `v*`.
It is scanned before it is published, and carries an SBOM and build provenance. More about the app itself: [docs/app.md](docs/app.md).

## The all-in-one stack (in progress)

The goal: start from zero with a stack like the author's by answering a few questions. One `compose.yaml` runs Forgejo, Komodo,
Infisical, Nginx Proxy Manager and homegraf together, and a bootstrap container configures them against each other.

```bash
./setup.sh                  # the host's address, how names reach it (sslip.io, AdGuard Home in the stack, or your own DNS), the administrator
docker compose up -d --wait # about a minute; then: docker compose logs bootstrap
```

**All four phases are done.** All services start healthy, and the bootstrap sets up the Forgejo administrator, token,
repository (seeded with the tools and an inventory skeleton) and webhook; Komodo's API key, repository resource and the `update-all` procedure; the
Nginx Proxy Manager administrator and the proxy hosts; the Infisical administrator and machine identity; a **private certificate authority** in
Infisical with `ingressctl`, so every proxy host serves a certificate of your own CA (install its root certificate once on your devices); AdGuard Home
with the wildcard rewrite (when you chose it); and hands homegraf everything it needs, so that you type no token anywhere. Optional services are compose profiles
(Renovate with a runner, database backups with `scripts/restore.sh` to put them back, Uptime Kuma). What is where, what is still manual, what was verified and the
plan: [docs/stack.md](docs/stack.md).

## Contributing and security

[CONTRIBUTING.md](CONTRIBUTING.md) says what is developed where and which checks to run, [SECURITY.md](SECURITY.md) how to report a vulnerability
and how to run the stack safely.

## License

[MIT](LICENSE)
