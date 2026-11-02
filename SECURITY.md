# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Use GitHub's private vulnerability reporting instead:
**Security** tab of this repository, then **Report a vulnerability**
(<https://github.com/BelphegorPrime/homegraf/security/advisories/new>).

Say what you found, how to reproduce it (a version or commit, the steps, what you saw), and what you think an attacker gains.
This is a one-person project: I answer when I can, normally within a week, and there is no service level to promise.
A fix is released as a new image, and you are named in the advisory unless you prefer not to be.

## Supported versions

Only the latest release (and `main`) gets fixes. Images are published as `ghcr.io/belphegorprime/homegraf` (`:main`, `:sha-<commit>`,
`:<version>`, `:latest`), so updating means pulling a newer tag.

## What homegraf is, security-wise

homegraf is an administration tool, and its write actions are powerful on purpose. Know what you are installing:

- To create and remove apps it holds a **Komodo API key with write access**, the **Infisical machine identity** that manages the
  stacks' secrets, and a **Forgejo token** that can write to your infrastructure repository. A flaw in homegraf is a flaw with those rights.
  Komodo's agent (Periphery) in turn has the Docker socket of the host, which is root on that host.
- Reading the map needs no sign-in. Every write action needs a signed-in user with the matching permission. Passwords are typed again for every
  action and are never kept by the page; five wrong passwords lock a name for ten minutes. Single sign-on (OpenID Connect) uses a cookie session
  with a CSRF token. Details: [docs/app.md](docs/app.md#users).
- Nothing reaches your main branch without a merge: every change homegraf makes to the repository is a pull request.
- The tools that read the repository run in the same container. A malicious repository is therefore code you run: only mount a repository you trust.

### Run it like this

- **Keep it on your LAN.** Do not publish homegraf, Komodo, Forgejo, Infisical or the proxy's admin port to the internet. `BIND_ADDRESS` in `.env`
  (default `0.0.0.0`) decides on which address the ports listen; `127.0.0.1` keeps them on the host.
- **Use HTTPS.** The stack gives every proxy host a certificate of your own CA; install its root certificate on your devices, and turn on the redirect
  from HTTP to HTTPS once they trust it. Without TLS the passwords of the sign-in cross the network in clear.
- **Protect `.env` and `data/`.** `setup.sh` writes `.env` readable by its owner only. It holds the passwords of the stack and the key that Infisical encrypts
  its secrets with (`INFISICAL_ENCRYPTION_KEY`). The database dumps in `data/backups` hold your secrets (encrypted with that key, so keep the two
  apart from strangers, and keep both: a dump cannot be read without the key).
- **Give users only what they need.** The permissions are `apps:create`, `apps:remove`, `hosts:edit`, `stacks:deploy`, `secrets:manage` and `users:manage`;
  the built-in `admin` has all of them, so use it to set the others up.

## Scope

In scope: this repository: the app in `app/`, the bootstrap, `setup.sh`, the compose file and its defaults, the scripts, and the tools in `seed/`.

Out of scope:

- Vulnerabilities in the components the stack runs (Forgejo, Komodo, Infisical, Nginx Proxy Manager, AdGuard Home, Uptime Kuma, PostgreSQL, ...): report them
  to those projects.
- Things that need an administrator's access to start with (the host, the Docker socket, the `.env` file).
- Decisions that are documented: for example the database tools Mongoku and RedisInsight have no login and sit on a LAN port, and say so in the docs.
