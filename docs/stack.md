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

**Sign in with Forgejo.** homegraf's sign-in page offers "Forgejo" next to the password: the accounts of the stack's own Forgejo are the users
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
LAN (it suggests the one it detects; the wildcard and the proxy hosts need it) and for one of three modes (`DNS_MODE`):

| Mode | What it is | Good for |
| --- | --- | --- |
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
| `dns` (AdGuard Home), **only with `DNS_MODE=adguard`** | answers for the domain: the wildcard to this host | 53 on the LAN address, setup 3300, admin 8053 | the profile `dns`; set up by the bootstrap |
| `bootstrap` | one-shot: configures the services against each other | | image `ghcr.io/belphegorprime/homegraf-bootstrap`; keeps its state in the volume `bootstrap-state` |

| `certificates` | one-shot after `homegraf`: the private CA and ingressctl (see *What the bootstrap does*) | | the same image as the bootstrap; `docker compose logs certificates` |

### Optional services (compose profiles)

Off by default. `setup.sh` asks (or `OPTIONAL_SERVICES="renovate backup status" ./setup.sh --yes`); the choice is `COMPOSE_PROFILES` in `.env`.

| Profile | What | Notes |
| --- | --- | --- |
| `status` | Uptime Kuma at `status.<domain>` | the bootstrap adds the proxy host and ingressctl gives it a certificate; the first visit asks for a Kuma account |
| `backup` | a dump of Forgejo's and Infisical's PostgreSQL and Komodo's MongoDB once a day, seven days kept, in `data/backups` | `BACKUP_INTERVAL` (seconds) and `BACKUP_KEEP_DAYS`; **on this host**, copy them elsewhere; `scripts/restore.sh` puts them back (see Restoring from the backups). Not in the dumps: the volumes with files (the repositories' git data, Komodo's keys, the proxy's data) |
| `runner` | a Forgejo Actions runner, label `docker` | jobs run in a Docker daemon of their own (`runner-dind`, docker-in-docker: **privileged**, but a job cannot reach this host's Docker socket); the runner registers with a secret in `.env` (`RUNNER_SECRET`), no token to copy |
| `renovate` | dependency updates for your repository as pull requests | brings the runner along. The bootstrap creates the user `renovate-bot`, its repository `renovate-bot/configure` (a workflow that runs Renovate twice a day on the runner, and `config.js` with `autodiscover`), the secret `RENOVATE_TOKEN` and adds the bot as **collaborator** of your repository: every repository the bot is a collaborator of is looked after, so add it to others the same way. Its first pull request is `Configure Renovate` (merge it to switch Renovate on) |

The runner (and so Renovate) needs Forgejo on the **address of the host**, not on loopback: the job containers live in another Docker daemon, do not see
this network and use the address the runner registered with (`http://<HOST_IP>:<PORT_FORGEJO>`). `BIND_ADDRESS=127.0.0.1` therefore does not work with them.
The Renovate image is the slim one; `-full` (several GB, the tools of every package manager) goes into `.forgejo/workflows/renovate.yml` of the bot's repository.

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
