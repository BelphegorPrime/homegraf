# The all-in-one stack

`compose.yaml` starts everything homegraf stands on, on one Docker host, and a **bootstrap** container configures it against each
other: you run two commands and answer a few questions. **This is phase 2 of four** (see the end); what is still manual is listed below.

## Start

```bash
./setup.sh                  # asks for the host's address, how names reach it, the administrator; writes .env with random secrets
docker compose up -d --wait # about a minute once the images are pulled
```

When it returns, read what the bootstrap printed: `docker compose logs bootstrap` (URLs, who signs in where, what is left to do).

`setup.sh` refuses to overwrite an existing `.env`, never prints a secret except the administrator password when it generated one, and
copies knobs it finds in its environment (`PORT_*`, `BIND_ADDRESS`, `PROJECT_PREFIX`, `HOMEGRAF_IMAGE`, `TZ`, ...) into `.env`.
`DNS_MODE=... HOST_IP=... ./setup.sh --yes` asks nothing. Every knob is documented in [.env.example](../.env.example).
To run next to other things change the ports and `PROJECT_PREFIX`.

**One name and one password for everything.** Your e-mail address (`ADMIN_EMAIL`, which `setup.sh` also writes as `ADMIN_USER`) and the administrator password (`ADMIN_PASSWORD`) are the sign-in of homegraf, Komodo, Nginx Proxy Manager,
Infisical and AdGuard Home, and of Forgejo (whose own user name, `owner`, also works). Infisical's policy is the strictest, so `setup.sh` asks for
**14 characters or more with a letter and a digit** (or special character); a generated one always fits.

**Sign in with Forgejo.** Everything of homegraf is behind its login page (the map and the status too; only `/api/health` is open for the container's health check). The page offers "Forgejo" next to the password: the accounts of the stack's own Forgejo are the users
of homegraf. The bootstrap hands homegraf the application (`OIDC_ISSUER=http://git.<domain>`, client id and secret, `HOMEGRAF_URL`) and names the
Forgejo administrator (`FORGEJO_ADMIN_USER`) in `OIDC_ADMINS`: at the first sign-in that account becomes an entry of the Users tab with every permission,
bound to the Forgejo account. Everybody else is added in the Users tab by their Forgejo name, with the permissions they should have. Forgejo asks
once whether to allow the application. The proxy answers to `git.<domain>` and `homegraf.<domain>` inside the stack's network too, so homegraf reaches
its provider by the address a browser uses. These names, like Forgejo's `ROOT_URL`, assume that the proxy listens on port 80 of the host (`PORT_HTTP`).

## What happens on `up`

```
databases and services start ──► forgejo-admin (creates the Forgejo administrator) ──► bootstrap ──► homegraf
```

`homegraf` starts last, after the bootstrap finished successfully, so that it can read what the bootstrap created. If the bootstrap fails,
`up` stops there and `docker compose logs bootstrap` says what to fix; nothing that is done is undone, run `docker compose up -d bootstrap` again.

### What the bootstrap does

| Service | Automated | Result |
| --- | --- | --- |
| Forgejo | the administrator (the one-shot `forgejo-admin` service runs Forgejo's own command in a container that shares its data volume, so nothing needs the Docker socket); an API token for homegraf (`write:repository`, `write:issue`, `read:user`); the private repository `<owner>/infrastructure` with the seed as its first commit; a webhook that calls Komodo on every push to `main`; the OAuth2 application `homegraf` (callback `http://homegraf.<domain>/api/auth/callback`) for the sign-in of homegraf | homegraf can read the repository, create branches and open pull requests |
| Komodo | an API key for homegraf; the git account for Forgejo; a Repo resource for the repository, **cloned** on this host; the procedure `update-all` (pull the repository, deploy every stack whose files changed) | a push to `main` pulls and deploys; homegraf has live status and can create, change and remove stacks |
| Nginx Proxy Manager | the administrator (NPM 2.16 has **no default user**: the first one is created through its setup call); a proxy host for `git`, `komodo`, `secrets`, `proxy` and `homegraf` under `<name>.<domain>`, plain HTTP, WebSockets on | the names work as soon as they reach this host |
| Infisical | bootstraps the instance (administrator and organisation `homegraf`); a machine identity `homegraf` with the organisation role Admin and Universal Auth | homegraf and the tools can keep stack secrets; they create the projects (`stacks-<server>`) and folders themselves when the first app needs them |
| Certificates (the service `certificates`, which runs last: ingressctl checks every upstream, and homegraf starts after the bootstrap) | in Infisical's Certificate Manager: a project `homegraf-ca`, an internal **root CA** (ten years), a policy that allows only `*.<domain>`, a profile, an application with API enrollment and the machine identity `ingressctl` (no rights in the organisation, `operator` in the application); in Nginx Proxy Manager the technical user `ingressctl@<domain>` (no administrator, no 2FA); in Komodo the stack `ingressctl` (built from `infra/ingress-controller` of your repository, joined to this stack's network) and the procedure `ingress-apply`, which is the last stage of `update-all`; in your repository the ids in `inventory/ingress.yaml`, the root certificate `inventory/homegraf-root-ca.pem` and `external/ingress.yaml` (the five services of this stack); the proxy hosts of the bootstrap are **adopted** by `ingressctl`, which then gives each a certificate | every proxy host serves a certificate of your own CA, renewed before it expires; every new app gets one with its merge |
| New apps (the same `certificates` service, after the certificate part) | in Infisical the project `stacks-<server>` for the host this stack runs on, the identity `stacks-<server>` that reads it at deploy time (`viewer`) and the machine identity `stack-provisioner` (no organisation rights, `member` of the `stacks-*` projects); in Komodo the variables the compose wrapper is filled from (`INFISICAL_API_URL`, `INFISICAL_PROJECT_ID_<SERVER>`, `INFISICAL_CLIENT_ID_<SERVER>`, `INFISICAL_CLIENT_SECRET_<SERVER>`), an API key `stack-provisioner` of its own, and the stage *Reconcile* of `update-all` (right after the pull); in your repository `infra/secrets/provisioner.env` with the credentials of both | a folder `stacks/<id>/` with a `compose.yaml` and an `ingress.yaml` sets itself up on push, see *A new app is a folder* |
| Technitium DNS (`DNS_MODE=technitium`, the default) | the zone `<domain>` (Primary), the record `*.<domain>` -> this host (TTL 300) and a non-expiring API token `homegraf`, kept in the state; the administrator `admin` gets the password from `ADMIN_PASSWORD` at the first start of the container | the LAN resolves every current and future name; the tools can use the token instead of a password |
| AdGuard Home (`DNS_MODE=adguard`) | the first-run setup and the wildcard rewrite `*.<domain>` -> this host | the LAN resolves every current and future name |
| homegraf | gets the Komodo key and secret, the Forgejo token and clone URL and the Infisical identity in a file on the `bootstrap-state` volume (`/state/homegraf.env`, mode 0600) that it reads at start (`HOMEGRAF_ENV_FILE`; the environment wins over the file) | the write actions are on and the map has live status, **without you typing a token anywhere** |

`docker compose logs certificates` shows the certificate part; a failure there is a warning, not an error (the hosts stay on plain HTTP, nothing is stopped), and
`docker compose up -d certificates` runs it again.

### A new app is a folder

Push `stacks/<id>/compose.yaml` and `stacks/<id>/ingress.yaml` to `main` and nothing else: no inventory entry, no wizard. The stack is derived from the folder
(id, path, namespace, compose files, and the server from `proxy.upstream.server` of the manifest; an entry in `inventory/stacks.yaml` would win). The
stage *Reconcile* of `update-all` runs `infra/komodo/reconcile_stacks.py` (also `./homelab.py reconcile`, a dry run without `--yes`) and, for each such
stack, only what is missing: its `${VAR}` secrets generated in Infisical, the Komodo stack with the Infisical wrapper, a first deploy (and a deploy on
every push for a stack that builds its own image). The last stage of `update-all`, `ingress-apply`, then gives it a proxy host and a certificate.
A failing stack deploy stops `update-all` before that last stage, so the procedure `ingress-sync` (made by the bootstrap) applies the `ingress.yaml` files
on its own every 15 minutes, without an alert: proxy hosts and certificates keep following the repository while a stack is broken.

A host added later with `add-host.sh` gets its own `stacks-<server>` project the same way, and `INFISICAL_API_URL` becomes the address of this host
(`http://<HOST_IP>:<PORT_INFISICAL>`), because the compose wrapper runs on that host, where the name `infisical` does not resolve. For the same reason
`BIND_ADDRESS` must not be `127.0.0.1` once there is more than one host. If the first push of an app on a new host says that the Infisical project does
not exist or the provisioner is not a member, run the certificates step again: `docker compose run --rm certificates`.

The bootstrap is a Python program with only the standard library (`bootstrap/`), run as an unprivileged user without the Docker socket, and **safe to run again**:
every step first looks at the real system and only does what is missing (tokens whose value was lost are replaced, resources that exist are left
alone, an existing `update-all` is never overwritten because the tools add stages to it). What it created is kept in `/state/state.json` (mode 0600).
`docker compose run --rm bootstrap show infisical-password` prints the Infisical password when `ADMIN_PASSWORD` did not meet the policy and a
generated one was used.

### What is still manual

1. **Names**, unless you chose `sslip` (nothing to do) or `adguard` (then point your router's DHCP at the host): with `external` create the wildcard
   entry the bootstrap prints, `*.<domain> -> <host address>`. See *Names* below.
2. **Trusting the root certificate.** The proxy hosts serve certificates of your own CA, so a browser warns until the root certificate is installed on
   your devices: download `inventory/homegraf-root-ca.pem` from your repository (the bootstrap prints the link) and import it as a trusted root
   (Windows: *certmgr*, macOS: Keychain Access, Linux: `ca-certificates`, Android and iOS: the settings for CA certificates). Then set
   `force_https: true` in `external/ingress.yaml` (and in the apps' `ingress.yaml`) so http redirects to https. The hosts stay on plain HTTP as well until you do.
3. **An Infisical or NPM that was set up before** with other credentials: the bootstrap cannot take it over and says what to create by hand (for Infisical:
   an identity with the organisation role Admin and Universal Auth, its client id and secret into `.env` as `INFISICAL_CLIENT_ID` / `INFISICAL_CLIENT_SECRET`).
4. **The tools and the inventory skeleton in your repository** come with the seed (phase 3). Until then the image carries a stub: a README and an empty
   inventory, so the map is empty and homegraf says its tools are missing. See *The seed*.
5. **Other hosts:** the stack runs on one host; another Docker host joins with `./scripts/add-host.sh NAME ADDRESS` (see *More hosts* below).

## More hosts

```
./scripts/add-host.sh worker-1 10.0.0.12      # on the host of the stack: NAME, and the address of the new host as this host reaches it
```

The script writes `worker-1.periphery.yaml`, the compose file of the Komodo agent for the new host (it holds the passkey of the stack:
mode 600, kept out of git). Copy it to the new host and start it there (`docker compose -f worker-1.periphery.yaml up -d`); the script waits up to
ten minutes for the agent to answer on port 8120 and then:

- registers the agent as the Komodo server `worker-1`,
- makes a repository resource of its own for that server (`infrastructure-worker-1`, one clone per agent) and clones your repository there,
- adds that clone to the stage *Pull* of `update-all`, so every push pulls it,
- lists the host in `inventory/servers.yaml` of your repository (a commit on `main`): the address the proxy reaches its apps at.

From then on `worker-1` is a server to choose in the wizard. What it needs: Docker with compose on the new host; the same path for
`DATA_DIR` as here (the tools build the run directory of every stack from one `repos_dir`); port 8120 of the new host reachable from the stack's host
(from there only: the agent is root on its host); Forgejo reachable from the new host at `HOST_IP:PORT_FORGEJO`, so not with `BIND_ADDRESS=127.0.0.1`.
Run it again after a failure; nothing that is done is repeated. Not covered, and not tried: Infisical projects for the stack secrets of the new host
(`secrets: infisical`), and certificates for apps whose address is on it.

## The seed

The repository the bootstrap creates in Forgejo gets its first content from a **seed**: the folder `seed/` of this repository, copied into the
bootstrap image at build time (`bootstrap/Dockerfile`; without `seed/` the image carries the stub in `bootstrap/seed-stub/`, and says so).
The seed is plain files; text files may contain these placeholders, which the bootstrap fills in (an unknown one is an error):

| Placeholder | Is replaced by |
| --- | --- |
| `@@DOMAIN@@` | the domain the services and apps are reached under |
| `@@HOST_IP@@` | the address of this host in the LAN |
| `@@SERVER@@` | the name Komodo registered this host under (`Local`) |
| `@@REPOSITORY@@` | `<owner>/infrastructure`, the repository on Forgejo |
| `@@REPOS_DIR@@` | where Komodo's agent keeps its clones on this host (`<DATA_DIR>/periphery/repos`, absolute, the same inside and outside the agent) |
| `@@FORGEJO_URL@@`, `@@INFISICAL_URL@@` | the services as the containers reach them (`http://forgejo:3000`, `http://infisical:8080`) |
| `@@FORGEJO_USER@@` | the administrator's name on Forgejo (`owner`), the owner of the repository and Komodo's git account |
| `@@NETWORK@@`, `@@CREDENTIALS_VOLUME@@`, `@@STATE_VOLUME@@` | the compose network of the stack, the volume with ingressctl's credentials and the one with its state (`inventory/ingressctl.compose.yaml` connects the Komodo stack `ingressctl` to both) |

The seed is committed once, when the repository is empty; a repository that has content is never touched.
homegraf reads **Komodo's clone** of the repository (`<DATA_DIR>/periphery/repos/infrastructure`, mounted at `/repo`, `REPO_DIR` overrides it):
it is current after every push, because the webhook runs `update-all`, which pulls it.

## Names (DNS)

The services are reached as `git.<domain>`, `komodo.<domain>`, `secrets.<domain>`, `proxy.<domain>` and `homegraf.<domain>`, and
Nginx Proxy Manager routes by that name, so **the names have to reach this host**. `setup.sh` asks for the host's address in your
LAN (it suggests the one it detects; the wildcard and the proxy hosts need it) and for one of four modes (`DNS_MODE`; `technitium` is the default):

| Mode | What it is | Good for |
| --- | --- | --- |
| `technitium` | [Technitium DNS Server](https://technitium.com/dns/) runs in this stack (compose profile `technitium`, switched on by `COMPOSE_PROFILES=technitium` in `.env`), **the bootstrap sets it up**: the zone, the wildcard record `*.<domain>` and an API token for the tools (Technitium has real API tokens, AdGuard Home has only its login); other names are forwarded (`DNS_FORWARDERS`, default 1.1.1.1 and 9.9.9.9) | a LAN of your own where you point the router's DHCP at this host; the default |
| `sslip` | zero configuration: the domain becomes `<host address with dashes>.sslip.io` (e.g. `git.192-168-1-10.sslip.io`), a public service that answers with the address inside the name | trying it out; the clients need internet DNS; the private CA works here too, the certificates carry the sslip.io names |
| `adguard` | [AdGuard Home](https://adguard.com/adguard-home.html) runs in this stack (compose profile `dns`, switched on by `COMPOSE_PROFILES=dns` in `.env`), **the bootstrap sets it up** and adds the wildcard rewrite | a LAN of your own, the way the author runs it |
| `external` | you already have DNS (router, Pi-hole, your own AdGuard); `setup.sh` and the bootstrap print the one wildcard entry to create: `*.<domain>  ->  <host address>` | an existing setup |

With a wildcard entry no per-app DNS step is ever needed: every app you add later is already reachable under its name.

**AdGuard mode in detail.** The bootstrap does the first-run setup (administrator `ADMIN_USER` with `ADMIN_PASSWORD`; its admin UI is on `PORT_DNS_ADMIN`, 8053)
and the rewrite `*.<domain> -> <host address>`. Pitfalls:

- **Port 53 on Linux hosts** is often taken by `systemd-resolved`. It listens on `127.0.0.53` only, so it does not collide: the stack binds
  DNS to the LAN address (`DNS_BIND`) alone, never to all addresses. `setup.sh` checks the port and stops with an explanation if something
  else holds it (dnsmasq, another resolver, a router function on the same host). Clients can only use port 53, so the answer to a busy port
  is to free it or to use `external`, not to pick another port (`PORT_DNS` exists for tests).
- **The router or DHCP server has to hand out this host as the DNS server**, or you configure it per device. Until then nothing finds the names.
- **Some devices need two DNS servers**: Android refuses a custom private DNS with only one. Give it a second address that also reaches
  AdGuard (a second address of the host, or a second AdGuard).
- The DNS server is a single point of failure for the LAN: when this host is down, so is name resolution. Keep a fallback in the DHCP settings
  if that matters to you.

## The parts

| Service | Role | Port (default) | Notes |
| --- | --- | --- | --- |
| `homegraf` | the map, the wizard, users and permissions | 8097 | read-only container; signs in with `ADMIN_USER` and `ADMIN_PASSWORD` (`HOMEGRAF_ADMIN`); starts after the bootstrap |
| `forgejo` + `forgejo-db` | the repository your homelab is defined in; every change from homegraf is a pull request here | 3000, ssh 2222 | install wizard locked, registration closed; its administrator is `FORGEJO_ADMIN_USER` (`owner`: the repository belongs to it, and a user name has no `@`; Forgejo accepts `ADMIN_EMAIL` to sign in as well) |
| `forgejo-admin` | one-shot: creates that administrator | | runs before the bootstrap, exits |
| `komodo-core` + `komodo-mongo` | deploys the stacks of that repository, reports their state | 9120 | the administrator is created from `ADMIN_USER` / `ADMIN_PASSWORD` on the very first start |
| `komodo-periphery` | the agent that runs `docker compose` on this host | (internal 8120) | **mounts the Docker socket, so it is root on this host**; registered as the first server `Local` |
| `infisical` + `infisical-db` + `infisical-redis` | secrets of the stacks, later the certificate authority | 8079 | set up by the bootstrap |
| `proxy` (Nginx Proxy Manager) | `http://<name>.<domain>` to the containers | 80, 443, admin 81 | set up by the bootstrap |
| `openhands` (Agent Canvas), **only with the profile `agents`** | Canvas, Agent Server and Automation Server in one image; runs an agent on an issue | 8000 (`PORT_OPENHANDS`) | `LOCAL_BACKEND_API_KEY` from `.env`; data in the volume `openhands-data`; the model provider key is entered in Canvas, not in `.env` (see *Agents: from an issue to a draft pull request*) |
| `forgehands`, **only with the profile `agents`** | a Forgejo webhook becomes a signed call to OpenHands' automation webhook; with `FORGEHANDS_OPENHANDS_API_URL` and `LABEL_ROUTES` (profile ids) a Forgejo label starts the matching agent profile instead | 8080 (`PORT_FORGEHANDS`) | webhook mode by default; direct mode is opt-in, pinned `v0.4.10`; state (ledger, deliveries) in the volume `forgehands-data`, runs in `forgehands-workspace` |
| `technitium` (Technitium DNS Server), **only with `DNS_MODE=technitium`** | answers for the domain: the wildcard to this host; an API token for the tools | 53 on the LAN address, console 8053 (user `admin`, not the e-mail address) | the profile `technitium`; set up by the bootstrap |
| `dns` (AdGuard Home), **only with `DNS_MODE=adguard`** | answers for the domain: the wildcard to this host | 53 on the LAN address, setup 3300, admin 8053 | the profile `dns`; set up by the bootstrap |
| `bootstrap` | one-shot: configures the services against each other | | image `ghcr.io/belphegorprime/homegraf-bootstrap`; keeps its state in the volume `bootstrap-state` |

| `certificates` | one-shot after `homegraf`: the private CA and ingressctl (see *What the bootstrap does*) | | the same image as the bootstrap; `docker compose logs certificates` |

### Optional services (compose profiles)

Off by default. `setup.sh` asks (or `OPTIONAL_SERVICES="renovate backup status" ./setup.sh --yes`); the choice is `COMPOSE_PROFILES` in `.env`.

| Profile | What | Notes |
| --- | --- | --- |
| `status` | Uptime Kuma at `status.<domain>` | the bootstrap adds the proxy host and ingressctl gives it a certificate; the first visit asks for a Kuma account |
| `agents` | `openhands` (Agent Canvas) and `forgehands`: label an issue `ai:ready` in an allowed repository and the agent works on it; with direct mode any of `ai:plan`, `ai:ready`, `ai:escalate`, `ai:review` starts its agent profile with its limits | `setup.sh` writes the secrets, the two logins, the model gateway settings and `FORGEHANDS_ALLOWED_REPOS`, sets `FORGEHANDS_BOOTSTRAP=1`, and the bootstrap proxies `openhands.<domain>`. With the opt-in the factory step creates the agent user, the labels and the webhook, the profiles and the routes (see *Agents: from an issue to a draft pull request*). Both containers have a healthcheck |
| `backup` | a dump of Forgejo's and Infisical's PostgreSQL and Komodo's MongoDB once a day, seven days kept, in `data/backups` | `BACKUP_INTERVAL` (seconds) and `BACKUP_KEEP_DAYS`; **on this host**, copy them elsewhere; `scripts/restore.sh` puts them back (see Restoring from the backups). Not in the dumps: the volumes with files (the repositories' git data, Komodo's keys, the proxy's data) |
| `runner` | a Forgejo Actions runner, label `docker` | jobs run in a Docker daemon of their own (`runner-dind`, docker-in-docker: **privileged**, but a job cannot reach this host's Docker socket); the runner registers with a secret in `.env` (`RUNNER_SECRET`), no token to copy |
| `renovate` | dependency updates for your repository as pull requests | brings the runner along. The bootstrap creates the user `renovate-bot`, its repository `renovate-bot/configure` (a workflow that runs Renovate twice a day on the runner, and `config.js` with `autodiscover`), the secret `RENOVATE_TOKEN` and adds the bot as **collaborator** of your repository: every repository the bot is a collaborator of is looked after, so add it to others the same way. Its first pull request is `Configure Renovate` (merge it to switch Renovate on) |

### Agents: from an issue to a draft pull request

Two containers, both only with the profile `agents`; with `COMPOSE_PROFILES=agents` (what `setup.sh` writes when you pick it) they join the
same stack as the rest.

How to operate the flow (watching and stopping runs, re-running an issue, the factory loop, limits and the known failure modes) is in
[agents.md](agents.md).

ForgeHands has two modes:

- **Webhook mode** (the default: `FORGEHANDS_OPENHANDS_API_URL` empty, so an `.env` from before direct mode changes nothing). ForgeHands
  verifies the Forgejo delivery, checks the allowlist and forwards a signed event to an OpenHands automation, which starts the agent. The
  webhook of Canvas is under *Manual, once* (step 3) below.
- **Direct mode** (opt-in: `FORGEHANDS_OPENHANDS_API_URL=http://openhands:8000` and `FORGEHANDS_LABEL_ROUTES` with the profile **ids**;
  with the factory opt-in the runtime handoff supplies both).
  ForgeHands calls `POST /api/conversations` itself: it passes the agent profile of the label, the iteration cap, the tag
  `source=forgehands` and the variables named in `AGENT_SECRETS`. That replaces the OpenHands automation webhook; the **Forgejo webhook is
  still required**, because Forgejo is what delivers the `issues` events in either mode. The OpenHands backend key is the stack's own
  `OPENHANDS_LOCAL_BACKEND_API_KEY`; ForgeHands holds no LLM credentials.

Direct mode does not start by itself: OpenHands takes the profile **ids**, not their names (`POST /api/conversations` rejects a name with
HTTP 422). With the opt-in the bootstrap creates the profiles and publishes the routes.

Fresh stack, in this order (`setup.sh` writes `FORGEHANDS_BOOTSTRAP=1` when you pick the profile `agents`):

1. `./setup.sh` writes `.env`: the secrets, the two logins, the models and the gateway (`FORGEHANDS_STRONG_MODEL`,
   `FORGEHANDS_CHEAP_MODEL`, `FORGEHANDS_GATEWAY_URL`, `FORGEHANDS_GATEWAY_API_KEY`), `FORGEHANDS_ALLOWED_REPOS`,
   `FORGEHANDS_MERGE_POLICY` and the limits.
2. `docker compose up -d --wait` starts Forgejo, Canvas and the rest; the one-shot `bootstrap` service runs `python -m hgboot run`.
3. The factory step (`bootstrap/hgboot/factory.py`) runs only with the opt-in and only after a read-only preflight (every allowed repository
   is writable, a runner with the label `docker` is ready), then in this order:
   - the Forgejo agent user and its token (`agent_user.py`): a login of its own, `FORGEHANDS_AGENT_LOGIN` (`setup.sh` writes
     `homegraf-agent`), not an administrator and not the human; a token with `write:repository`, `write:issue` and `read:user`; `write` on
     every allowed repository. `FORGEHANDS_HUMAN_LOGIN` is the operator's own login (`setup.sh` writes it), and `FORGEHANDS_ALLOWED_SENDERS`
     names the human first and the agent after him: every event from any other sender is ignored.
   - the labels and one `forgejo` webhook per allowed repository (`factory_repos.py`), events `issues`, `pull_request_label`, `action_run_failure`, `action_run_success`, `push` and `pull_request`; then the optional GitHub mirror (`push_mirror.py`) when
     `FORGEHANDS_MIRROR_URL` is set.
   - the CI half (`factory_ci.py`): the seed `.forgejo/workflows/ci.yaml`, the runner readiness above and the required check
     `ci / seed (*)` on the default branch.
   - the LLM profiles `smart` and `cheap` (and `free` when `FORGEHANDS_FREE_MODEL` is set) and the agent profiles (`agent_profiles.py`):
     `planner`, `worker`, `worker-strong`, `reviewer`, `sensitive-reviewer`, and `worker-free` with a free model. Every write is read back
     and compared; a write that did not stick fails the step and publishes no id.
   - the routes (`hgboot.agents.label_routes()`, published by `factory._routes()`): ten routes, thirteen with a free model.
   - the runtime handoff `forgehands.json` in `FORGEHANDS_RUNTIME_DIR` (`/factory-runtime`), written last. ForgeHands' launcher reads it
     (`FORGEHANDS_RUNTIME_FILE`) and starts only with it; a failed run publishes no new ready handoff. Existing handoff invalidation is best-effort, so an old ready file can survive an `OSError`; see [Runtime handoff](#runtime-handoff) for details.

The mail: the factory warns once (`mailer_warning()`) unless `FORGEJO_MAILER_ENABLED` is true, because Forgejo mails nobody when a run
mentions the human; fill the `FORGEJO_MAILER_*` variables in `.env` (`.env.example` lists them).

The routes; the profile column names the profile, the route carries its id:

| Route | Agent profile | Tier | `max_iterations` | Event |
| --- | --- | --- | --- | --- |
| `ai:plan` | `planner` | `smart` | 80 | the label |
| `ai:ready` | `worker` | `cheap` | 120 | the label |
| `ai:ready-free` | `worker-free` | `free` | — | out of quota, from `ai:ready` |
| `ai:escalate` | `worker-strong` | `smart` | 120 | the label |
| `ai:review` | `reviewer` | `smart` | 60 | the label |
| `ai:changes` | `worker` | `cheap` | 120 | `pull_request` |
| `ai:changes-free` | `worker-free` | `free` | — | out of quota, from `ai:changes` |
| `ci:failure` | `worker` | `cheap` | 120 | `ci_failure` |
| `ci:failure-free` | `worker-free` | `free` | — | out of quota, from `ci:failure` |
| `ci:success` | `reviewer` | `smart` | 60 | `ci_success` |
| `epic:done` | `reviewer` | `smart` | 60 | `epic_done` |
| `pr:conflict` | `worker-strong` | `smart` | 120 | `conflict` |
| `sensitive:review` | `sensitive-reviewer` | `smart` | 60 | `sensitive_review` |

The three `-free` routes need no label and exist only with a free model; `ai:ready`, `ai:changes` and `ci:failure` name them as
`fallback_to` while their tier is out of quota. `ai:changes` and `ci:failure` escalate their last round to `ai:escalate`. A route may also
set `max_budget` (one run) and `max_concurrent` (that tier); [agents.md](agents.md#limits) has the limits and the rounds.

Label semantics (ForgeHands 0.4.10; the stack pins `v0.4.10`):

- Forgejo reports a label added to an existing issue as an `issues` event with action `label_updated` and the *current* labels, not the
  added one. ForgeHands remembers the labels last seen per issue and starts a run for a trigger label that is **new**. So `ai:escalate`
  next to `ai:ready` starts the stronger worker, removing and re-adding a label runs the issue again, and an unrelated label does nothing.
- The ledger `jobs.json` (`repo#issue:label` -> conversation, queue, hourly count, timeout, retries) is in `forgehands-data`; a label on
  an issue with an active run starts no second run. Run one label twice: one run.

`setup.sh` writes the secrets, the logins, the models and `FORGEHANDS_ALLOWED_REPOS`; with the opt-in the runtime handoff supplies
`OPENHANDS_API_URL` and `LABEL_ROUTES` under ForgeHands' own names, and only where `.env` left them empty (the launcher keeps an
operator's value). Each can be set in `.env`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `FORGEHANDS_OPENHANDS_API_URL` | empty | set to `http://openhands:8000` to switch to direct mode; the handoff fills it with the opt-in |
| `FORGEHANDS_LABEL_ROUTES` | empty | direct mode: route -> profile id, tier, `max_iterations`; the handoff writes the table above |
| `OPENHANDS_LOCAL_BACKEND_API_KEY` | generated | handed to ForgeHands as `OPENHANDS_API_KEY` |
| `FORGEHANDS_AGENT_SECRETS` | `FORGEJO_TOKEN,FORGEJO_URL` | direct mode: variables handed to every conversation; without them the agent has no Forgejo access |
| `FORGEHANDS_FORGEJO_AGENT_TOKEN` | empty | the agent user's token; the bootstrap creates it, a value here supplies one instead. The operator's own `FORGEJO_TOKEN` never reaches ForgeHands |
| `FORGEHANDS_FORGEJO_URL` | `http://forgejo:3000` | direct mode: Forgejo as the agent reaches it |
| `FORGEHANDS_ALLOWED_REPOS`, `FORGEHANDS_ALLOWED_SENDERS` | `setup.sh` writes both | repositories and senders that may trigger runs; the senders are the human login, then the agent |
| `FORGEHANDS_MAX_CONCURRENT_PER_TIER` | 2 | queue per tier |
| `FORGEHANDS_MAX_CONCURRENT` | 4 | queue of the whole stack |
| `FORGEHANDS_MAX_RUNS_PER_HOUR` | 30 | hourly cap |
| `FORGEHANDS_MAX_RUN_MINUTES` | 120 | a run that takes longer is stopped |

Cost is steered in the LLM gateway (model choice, budgets, keys) and by `FORGEHANDS_MAX_RUNS_PER_HOUR`; a route can add `max_budget` for one
run. ForgeHands only knows a price when the gateway reports one; [agents.md](agents.md#limits) has the limits.

`WORKSPACE_DIR` (`/workspace`, volume `forgehands-workspace`) and `JOBS_FILE` (`/app/data/jobs.json`) are fixed in the compose file; the
ForgeHands container stays read-only and drops every capability. A started conversation is marked untrusted: the issue text goes into
the first message between markers.

Automatic (from `setup.sh` and Compose):

- The secrets: `OPENHANDS_LOCAL_BACKEND_API_KEY` (Canvas and Agent Server reject requests without it), `FORGEHANDS_FORGEJO_WEBHOOK_SECRET`
  (both modes), `FORGEHANDS_OPENHANDS_WEBHOOK_SECRET` (webhook mode) and `FORGEHANDS_ALLOWED_REPOS`.
- The webhook secret: ForgeHands reads `FORGEJO_WEBHOOK_SECRET`, the stack `.env` calls it `FORGEHANDS_FORGEJO_WEBHOOK_SECRET` (the prefix
  keeps stack variables apart), and `compose.yaml` maps one to the other (`FORGEJO_WEBHOOK_SECRET: ${FORGEHANDS_FORGEJO_WEBHOOK_SECRET:-}`).
- `openhands` starts with `AUTOMATION_BASE_URL=http://openhands:8000` (the service appends `/api/automation`). State lives in
  `openhands-data`; the port is `PORT_OPENHANDS` (default 8000).
- The bootstrap adds `openhands.<domain>` to the proxy and gets its certificate, like the other services (when the profile is already on
  before the bootstrap runs).
- `forgehands` starts after `openhands` is healthy, on `PORT_FORGEHANDS` (default 8080, configurable because 8080 is often taken), state in
  `forgehands-data`. Both have a healthcheck (`/health` and `/healthz`).

Manual, once (with the opt-in this is the mailer and the labels; a stack without `FORGEHANDS_BOOTSTRAP=1` does step 3 by hand too):

1. The mailer: fill `FORGEJO_MAILER_*` in `.env` (`.env.example` lists them) and set `FORGEJO_MAILER_ENABLED=true`; without it the
   bootstrap warns that a mention of the human is not mailed.
2. Label an issue `ai:plan`, `ai:ready`, `ai:escalate` or `ai:review` in an allowed repository (or a pull request `ai:changes`; the other
   routes start on their event), either when you create it or later. The matching profile works in its own checkout under `/workspace` and
   opens a draft pull request; nothing merges it.
3. Without the opt-in: `docker compose up -d openhands`, open Canvas at `http://<host>:${PORT_OPENHANDS}` and add a model provider key there
   (it stays in the `openhands-data` volume, not in `.env`), then create the LLM profiles (strong, cheap) and the agent profiles of the table.
   In Forgejo, `Settings -> Webhooks -> Add webhook -> Forgejo` in each repository the agents work in: target
   `http://forgehands:8080/webhooks/forgejo`, secret `FORGEHANDS_FORGEJO_WEBHOOK_SECRET`, events: issues. For webhook mode, in Canvas
   `Automate -> Events`, name it `forgehands`, event key expression `type`, signature header `X-Signature-256`, scheme `hmac_sha256_hex`,
   secret `FORGEHANDS_OPENHANDS_WEBHOOK_SECRET`; put the URL it returns in `.env` as `FORGEHANDS_OPENHANDS_WEBHOOK_URL` (ForgeHands
   refuses to start without it). For direct mode, set `FORGEHANDS_OPENHANDS_API_URL=http://openhands:8000` and `FORGEHANDS_LABEL_ROUTES`
   with the profile ids (`GET /api/agent-profiles`). If you create an automation instead of an event, set its `agent_profile_id`: an agent
   profile clears `model`. Unset or empty `OPENHANDS_LOCAL_BACKEND_API_KEY` leaves Canvas open to anyone who can reach the port: keep the
   port on the LAN or put it behind the proxy.

What stays manual: the model gateway account and its key, the mailer settings, the issues with their acceptance criteria, and — without the
opt-in — the profiles, the Forgejo webhook and the Canvas automation. Merging is ForgeHands' job and happens only by `MERGE_POLICY`. There
is no `forgehands.<domain>` proxy host: Forgejo reaches the bridge on the compose network in both modes, and the published 8080 is only for
debugging. Do not expose 8000 or 8080 unauthenticated.

The same two containers also exist as a wizard template: `./homelab.py new-app --template agents` creates `stacks/<id>/`
(`seed/infra/homelab/templates/agents/`) with its secrets in Infisical and two proxy hosts of its own, `<id>.<domain>` for ForgeHands and
`openhands.<domain>` for Canvas (the wizard picks a free port for each). It starts in webhook mode; its compose file sets `ALLOWED_REPOS`,
`ALLOWED_SENDERS`, `FORGEHANDS_AGENT_LOGIN`, `HUMAN_LOGIN`, `FORGEHANDS_STRONG_MODEL`, `FORGEHANDS_CHEAP_MODEL`, `FORGEHANDS_GATEWAY_URL` and the
limits `MAX_CONCURRENT_PER_TIER`, `MAX_CONCURRENT`, `MAX_RUNS_PER_HOUR`, `MAX_RUN_MINUTES` and `MAX_ROUNDS`. For direct mode, replace its
empty `OPENHANDS_API_URL` and `LABEL_ROUTES` with the service URL and the profile ids. Use it when the agents should be an app next to the
platform instead of part of the platform compose file.

Troubleshooting: `docker compose logs -f forgehands` shows verdicts, queue decisions and delivery failures. A 401 means the Forgejo secret
differs; a 502 means `OPENHANDS_WEBHOOK_URL` is not reachable from the container (webhook mode); in direct mode a refused
`POST /api/conversations` names the profile id or the key. A label that starts nothing: the repository or the sender is not in
`FORGEHANDS_ALLOWED_REPOS` / `ALLOWED_SENDERS`, the label is not a trigger, or it is unchanged since the last look. `jobs.json` holds the
label state; deleting it makes ForgeHands treat every current trigger label as new. A Forgejo webhook delivery log is the first place to
look for signature errors (`Settings -> Webhooks -> Recent Deliveries`).

The runner (and so Renovate) needs Forgejo on the **address of the host**, not on loopback: the job containers live in another Docker daemon, do not see
this network and use the address the runner registered with (`http://<HOST_IP>:<PORT_FORGEJO>`). `BIND_ADDRESS=127.0.0.1` therefore does not work with them.
The Renovate image is the slim one; `-full` (several GB, the tools of every package manager) goes into `.forgejo/workflows/renovate.yml` of the bot's repository.

#### Opting in and migrating

Only `FORGEHANDS_BOOTSTRAP=1` opts in; the compose profile `agents` alone never does. Without the flag, or with `0`, the factory stays
disabled, none of the variables below is read and the stack behaves as before; the bootstrap's factory step returns without doing anything.

`setup.sh` refuses to overwrite an existing `.env`: it prints that the file exists and exits 1. An `.env` from before the opt-in stays
untouched; you add the variables below by hand, the way `.env.example` lists them where it does. How the factory then runs is in
[agents.md](agents.md).

#### Writable primary and optional GitHub push mirror

Forgejo remains the writable primary. The optional mirror is push-only: the factory configures Forgejo to push to a GitHub repository; it does not create a GitHub source or pull mirror. Set both `FORGEHANDS_MIRROR_URL` and `FORGEHANDS_MIRROR_TOKEN`; When neither variable is set, the mirror stays off; setting only one is an error. For example, `FORGEHANDS_MIRROR_URL=https://github.com/example-owner/example-repo.git` and `FORGEHANDS_MIRROR_TOKEN=<token>`. The URL must name a GitHub repository (`owner/name`) and use HTTPS when a token is set.

The token must be able to read the target repository and write its contents (GitHub fine-grained token: **Contents: read and write**). Workflows in `.github/workflows/` declare no workflow-token write permissions: all have top-level `permissions: {}`; bootstrap jobs use `contents: read`, image jobs use `contents: read`, and the seed job uses `contents: read`.

Before writing, the reconciler refuses a missing source repository or a missing default-branch name reported by Forgejo, as well as a missing target repository and ambiguous multiple Forgejo push mirrors to that target. During post-sync verification, it also refuses if the named default branch is absent from Forgejo’s branch list. It checks every branch: a GitHub-only branch would be deleted by mirror sync and is refused; a GitHub-ahead or diverged branch is refused to avoid rewriting history. GitHub's compare endpoint decides whether a shared branch is behind, identical, ahead or diverged. If GitHub does not know the newer Forgejo commit yet (compare returns 404), Forgejo's compare endpoint must confirm GitHub's older commit is an ancestor; otherwise mirroring is refused. A behind branch is then brought up to date by sync. A drifted mirror is repaired by removing and recreating it. Forgejo runs mirror sync asynchronously on commit and every `8h0m0s`.

The GitHub token is sent only to the GitHub API and Forgejo's mirror credential field as the remote password; it is not put in a remote address, log or error. Authenticated requests do not follow redirects to another origin, and echoed service text is cleaned. Forgejo enforces which operator can manage mirrors; the helper does not check those rights. Bridging external GitHub pull requests back into Forgejo stays manual.

| Variable | Required | Default / note |
| --- | --- | --- |
| `FORGEHANDS_BOOTSTRAP` | — | `1` opts in; unset or `0` leaves the factory off |
| `FORGEHANDS_OPENHANDS_API_URL` | required | |
| `OPENHANDS_LOCAL_BACKEND_API_KEY` | required | |
| `FORGEHANDS_STRONG_MODEL` | required | |
| `FORGEHANDS_CHEAP_MODEL` | required | |
| `FORGEHANDS_FREE_MODEL` | optional | empty means no free tier |
| `FORGEHANDS_GATEWAY_URL` | required | |
| `FORGEHANDS_GATEWAY_API_KEY` | required | |
| `FORGEHANDS_ALLOWED_REPOS` | required | comma separated |
| `FORGEHANDS_ALLOWED_SENDERS` | required | comma separated; contains both logins below |
| `FORGEHANDS_AGENT_LOGIN` | optional | `homegraf-agent` |
| `FORGEHANDS_HUMAN_LOGIN` | required | not the agent |
| `FORGEHANDS_SECURITY_REVIEWERS` | optional | comma separated; required with `two_keys` |
| `FORGEHANDS_SECURITY_TOKEN` | optional | required with `two_keys` |
| `FORGEHANDS_FORGEJO_AGENT_TOKEN` | optional | the bootstrap creates it; a fresh setup leaves it empty |
| `FORGEHANDS_FORGEJO_WEBHOOK_SECRET` | required | |
| `FORGEHANDS_MIRROR_URL` | optional | |
| `FORGEHANDS_MIRROR_TOKEN` | optional | |
| `FORGEHANDS_RUNTIME_DIR` | optional | `/factory-runtime` |
| `FORGEHANDS_MAX_CONCURRENT_PER_TIER` | optional | 2 |
| `FORGEHANDS_MAX_CONCURRENT` | optional | 4 |
| `FORGEHANDS_MAX_RUNS_PER_HOUR` | optional | 30 |
| `FORGEHANDS_MAX_RUN_MINUTES` | optional | 120 |
| `FORGEHANDS_MERGE_POLICY` | optional | JSON, see Merge policy below |

**MCP isolation.** Each agent profile is written with `mcp_server_refs: []`, and `_agent_matches` accepts a stored profile only when that
list is empty too. The reason is the tool count: a profile with no MCP server offers the model fewer tools. The code records no other reason.

#### Runtime handoff

`forgehands.json` is written to `FORGEHANDS_RUNTIME_DIR` (default `/factory-runtime`). It is the last stage of a
factory run: only after preflight, the agent user, labels and hooks, the mirror, CI, the profiles and the routes have passed. The file has
mode `0600`, is replaced atomically, is at most 64 KiB and has the shape `{"ready": <bool>, "env": {...}}`. ForgeHands reads it from
`FORGEHANDS_RUNTIME_FILE` and starts direct mode only with it. `env` holds these names: `FORGEJO_URL`, `FORGEJO_TOKEN`,
`FORGEJO_WEBHOOK_SECRET`, `OPENHANDS_API_URL`, `OPENHANDS_API_KEY`, `AGENT_SECRETS`, `LABEL_ROUTES`, `ALLOWED_REPOS`, `ALLOWED_SENDERS`,
`HUMAN_LOGIN`, `MERGE_POLICY`, `MAX_CONCURRENT_PER_TIER`, `MAX_CONCURRENT`, `MAX_RUNS_PER_HOUR`, `MAX_RUN_MINUTES`, `MAX_ROUNDS` and,
only when set, `TIER_WEEKLY_BUDGETS`. The `env` object also includes an entry for each name listed in `AGENT_SECRETS`.

**Failure.** On any failed stage `_invalidate` rewrites an existing handoff as `{"ready": false, "env": {}}`; when the failure happens
before any handoff was written, no file is left behind. Invalidation is best-effort: `_invalidate` ignores an `OSError` from that rewrite,
so an old ready file can survive a failed run. ForgeHands starts direct mode only after reading a usable handoff; a missing file, invalid
JSON, a `ready` value other than exactly `true`, a file readable by group or others or a file over 64 KiB is rejected and retried until
the wait expires. That rejection is not a guarantee that a stale ready file cannot survive.

**Readback and repair.** Every LLM profile and agent profile write is read back and compared: LLM on model, base URL and key; agent on
name, LLM ref, prompt, skills and `mcp_server_refs`, and, when the profile already existed, the server must also keep its id. A mismatch
raises, so the failed run publishes no new profile ids or ready handoff; invalidation is best-effort as described above, so an old ready
handoff may survive an `OSError`. There is no retry loop. A rerun is idempotent: a stored LLM or agent
profile that differs from the wanted one is written again and verified ("corrected"), and a missing agent profile is created by cloning
the default agent profile ("created"). To watch a run or rerun an issue, see [agents.md](agents.md).

**Merge policy.** `FORGEHANDS_MERGE_POLICY` is operator-only JSON `repository -> {"deny": [globs]}`, never read from a repository file.
Example: `{"example-owner/example-repo": {"deny": [".forgejo/**", "infra/**"]}}`. Optional keys `reasons` (deny pattern -> why a human is needed) and `two_keys` (deny globs that may merge with an independent security review) exist; a `two_keys` pattern must be covered by a deny pattern and requires `FORGEHANDS_SECURITY_REVIEWERS` and `FORGEHANDS_SECURITY_TOKEN`. Bootstrap handoff exports only `deny`, not `reasons` or `two_keys`.

Semantics: a repository with no entry stays manual (`repository_merge_policy()` returns `None`); an explicit `{}` as the whole JSON object stays empty (nothing denied). An empty per-repository rule is invalid because each rule needs a `deny` list. The default deny list applies when the variable is unset or set to an empty string, and then only to the stack's own repository; operator policy is never replaced by the default. In either unset or empty case, `setup.sh` writes `FORGEHANDS_MERGE_POLICY` for the stack's own repository (`forgejo_user/infrastructure`) with the default deny globs below.

Default deny globs (from `DEFAULT_MERGE_DENY`, in order):

```
.forgejo/**
.github/**
AGENTS.md
CLAUDE.md
CONTRIBUTING.md
.agents/**
.claude/**
compose.yaml
**/compose.yaml
setup.sh
scripts/**
infra/**
stacks/**
```

The handoff publishes the policy as `MERGE_POLICY`; merging is done by ForgeHands, not by the agent. See [agents.md](agents.md) for the operating side.

### Restoring from the backups

With the profile `backup` on, **`scripts/restore.sh`** puts one database back from a dump:

```
scripts/restore.sh list                      # the dumps there are
scripts/restore.sh forgejo                   # the newest Forgejo dump (accounts, issues, pull requests, repository metadata)
scripts/restore.sh infisical forgejo-....gz  # a named one (a name in data/backups or a path); also: komodo
```

It checks the dump (a damaged or missing file stops it before anything changes), keeps a copy of the database as it is now as `pre-restore-<kind>-<time>` in the same folder
(that copy restores like any dump, so a restore can be undone; the backup service removes it with the other old dumps after `BACKUP_KEEP_DAYS`), stops what uses the database
(`forgejo` and `runner`, `infisical`, `komodo-core`), replaces the database with the dump and starts again what it stopped. Everything it asks is one question, or none with `--yes`.

What a dump does not hold stays as it is: the repositories' git data (volume `forgejo-data`), Komodo's keys and the proxy's data. **Infisical encrypts its secrets with
`INFISICAL_ENCRYPTION_KEY` from `.env`**: keep that file together with the dumps, because a dump restored under another key cannot be read. The dumps are on this host; copy them
(and `.env`) somewhere else, a backup on the disk that fails is none.

`scripts/e2e-restore.sh` proves it on this machine in a few minutes: it starts Forgejo, Infisical and Komodo's database with the backup profile (own project `hgrst`, a fresh data
directory), writes a marker, lets the backup services dump it, changes the data, restores each database, and checks the marker, that Forgejo and Infisical were restarted and are
healthy on the restored data, that a damaged dump is refused, and that the copy before a restore brings the old state back. It cleans up after itself.

### Starting over

`docker compose down -v` is not enough: Komodo starts ingressctl outside this project, and its stopped container keeps the volumes of its state and credentials, which a
new installation would then inherit (old hosts, old certificates). **`scripts/reset.sh`** takes everything down, including those. `data/` stays (clones, backups); delete it with `sudo` for a clean start.

Optional and off by default: the hardware layer. Set `UNIFI_URL`, `UNIFI_USER`, `UNIFI_PASSWORD` to read a UniFi controller; without
them (and without an `inventory/network.yaml`) homegraf has no Network tab at all. Nothing in the stack needs any particular hardware.

## Things to know

- **homegraf needs `HOMEGRAF_ENV_FILE` support** to pick up what the bootstrap wrote. It is in homegraf's source from the commit that adds `app/src/server/envfile.ts`
  on; an older published `ghcr.io/belphegorprime/homegraf` image ignores the file and starts with the write actions off. Check with
  `docker compose logs homegraf | grep "environment from"`. Without it, copy the values into `.env` by hand
  (`docker compose run --rm --entrypoint cat bootstrap /state/homegraf.env`; the keys are `KOMODO_API_KEY`, `KOMODO_API_SECRET`, `FORGEJO_TOKEN`,
  `HOMEGRAF_CLONE_URL`, `INFISICAL_CLIENT_ID` and `INFISICAL_CLIENT_SECRET`, the last two are `INFISICAL_BOOTSTRAP_CLIENT_ID/_SECRET` in the file).
- The images `ghcr.io/belphegorprime/homegraf` and `...-bootstrap` are private until the project is public: `docker login ghcr.io` first, or build them
  yourself (`docker build -t homegraf:local app`, `docker build -f bootstrap/Dockerfile -t homegraf-bootstrap:local .`) and set `HOMEGRAF_IMAGE` and `BOOTSTRAP_IMAGE`.
- `data/periphery` ends up owned by root (the agent runs as root); remove it with `sudo` or from a container.
- `down` keeps your data; `down -v` deletes the databases **and the bootstrap's state**. Do not delete `.env` while the volumes exist: the databases would no longer open.
- Ports 80 and 443 collide with anything else on the host that serves web pages; change `PORT_HTTP` / `PORT_HTTPS`.
- The webhook from Forgejo to Komodo needs `webhook.ALLOWED_HOST_LIST=private` in Forgejo (set in `compose.yaml`): Forgejo refuses to call a container in the same network otherwise.

## Verified

Run for real on a clean Docker host (Docker 29, Compose 5), project `hgboot`, with the images of `compose.yaml`:

- **From nothing:** `./setup.sh --yes` and `docker compose up -d --wait` brought up all services in 38 seconds; `forgejo-admin` and `bootstrap` exited with 0, `homegraf` was healthy.
- **What the bootstrap made:** a Forgejo administrator, token, private repository with a seed commit and a webhook; a Komodo API key, git account, Repo resource (cloned to
  `<DATA_DIR>/periphery/repos/infrastructure`) and `update-all`; the NPM administrator and five proxy hosts, each answering through the proxy with the right service
  (Forgejo, Komodo, Infisical, NPM, homegraf); an Infisical instance with the machine identity.
- **The handoff:** homegraf logged `environment from /state/homegraf.env: KOMODO_API_KEY, KOMODO_API_SECRET, FORGEJO_TOKEN, HOMEGRAF_CLONE_URL, INFISICAL_BOOTSTRAP_CLIENT_ID,
  INFISICAL_BOOTSTRAP_CLIENT_SECRET` and reported `create: enabled`. From inside its container the values worked: the Forgejo token (API, reading the repository,
  `git clone` over http), the Komodo key (reading **and** writing), the Infisical identity (Universal Auth login).
- **The loop:** a commit pushed to `main` in Forgejo ran the webhook, Komodo ran `update-all` (`PullRepo` ok) and its clone, the directory homegraf reads, was on the new commit.
- **Again:** a second run of the bootstrap changed nothing (every step said "already there") and exited 0.
- **DNS:** with `DNS_MODE=adguard` the bootstrap did AdGuard's first-run setup and added `*.<domain>`; the stack's DNS then answered every name under the domain with the host
  address over UDP and over TCP (a host address different from sslip.io's, so only AdGuard can have answered).
- **The image:** Trivy reports no HIGH or CRITICAL finding; the 49 unit tests (fake servers for every step, the whole run, idempotency, failure messages, no secret in the output)
  pass on Python 3.11 and 3.12.

- **A second host** (`scripts/e2e-host.sh`): a container with its own Docker daemon plays the host. `add-host.sh` registered it, an app was created on it
  through homegraf's API, merged, ran in that Docker and answered, and was removed in both halves.
- **A new app is a folder** (`scripts/e2e-folder.sh`): the same two hosts; one push of `stacks/<id>/compose.yaml` + `ingress.yaml` for an app on each host (nothing
  else) gave each a generated secret, a Komodo stack, a deploy on its own host, a proxy host and a certificate; both answer over https, verified against the CA of the stack.

Not verified: a second host on another machine (firewall, real network), and a host where port 53 is taken.

## The seed

`seed/` is the initial content of **your own infrastructure repository** in Forgejo, the one homegraf reads and changes by pull request.
The bootstrap (phase 2) pushes it there once and fills its placeholders; after that the repository is yours.

- **The tools** (`homelab.py`, the graph builder, the wizard, the Komodo and Infisical helpers, `ingressctl`; the list is `TOOLS` at the root) are developed in
  this repository and copied here; they carry nothing of any one homelab. What is specific to one installation (the domain of the
  apps, the repository on the forge, the Infisical address, which stacks are the platform) is read from `inventory/homegraf.yaml`.
- **The skeleton** (`inventory/*.yaml`, `stacks/`, `seed/README.md`) is written here, with `@@PLACEHOLDERS@@` documented in
  `seed/README.md`. The hardware layer is optional: the skeleton has no `network.yaml`, so the map has no Network tab until you add one
  (or connect a UniFi controller).
- **Proof:** `scripts/check-seed.sh` copies the seed to an empty directory, fills the placeholders with sample values, adds one sample
  app, runs `homelab.py` (`stacks`, `removable`, `addons`, `templates`, `new-app --print-compose`), builds the graph and runs the tools'
  tests (258). The workflow `seed.yaml` does that on Python 3.11 and 3.14 for every change.

## The four phases

| Phase | What | State |
| --- | --- | --- |
| 1 | `compose.yaml`, `setup.sh`, `.env.example`: all services, healthy, and the three ways names reach the host | **done** |
| 2 | the `bootstrap` container: Forgejo, Komodo, Nginx Proxy Manager, Infisical, AdGuard Home configured against each other; homegraf is handed what it needs | **done** (needs the seed of phase 3 for a full map, see above) |
| 3 | a seed repository for the bootstrap to push into Forgejo: the tools (`homelab.py`, the graph builder, the wizard) made independent of one homelab's domain and server names, with an inventory skeleton | **done**: `seed/` with the tools, proven by `scripts/check-seed.sh` (see The seed) |
| 4 | a private certificate authority in Infisical and automatic certificates for the proxy hosts (`ingressctl`), instead of plain HTTP or Let's Encrypt; the optional services as compose profiles (Forgejo runner, Renovate, Komodo backup, Uptime Kuma) | **done** (see What the bootstrap does and Optional services) |
