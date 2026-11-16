# ingressctl

Reconciles `ingress.yaml` manifests next to the stacks with Nginx Proxy Manager (2.16.0) and the private Infisical CA (v0.167.2). Git holds the desired state. The SQLite state only records ownership, progress, and what was applied.

It runs as the Komodo stack `ingressctl` on `proxy-host`, the single writer. Use `python3 infra/komodo/run_ingressctl.py <command> …` to run it there (see the [README tutorials](../../README.md#4-give-a-localcodes-service-https)).

## Commands

| Command | Credentials | Effect |
| --- | --- | --- |
| `ingressctl validate --root .` | none | Schema, normalization, namespace/stack mapping, domain uniqueness across the repo, issuer domain allow-list; reports configuration gaps |
| `ingressctl plan --root . [--stack <id>]` | yes | Reads live state and shows changes, drift, and blockers; changes nothing |
| `ingressctl apply --root . --commit <sha> --stack <id> [--deployed-commit <sha>]` | yes | Applies only the manifests of the selected stacks, under the per-NPM-instance lock |
| `ingressctl renew --all-deployed --inventory-root .` | yes | Renews from the last successfully applied manifests stored in the state |
| `ingressctl status [--namespace ns] [--json]` | none | Shows the operational state |
| `ingressctl adopt --resource ns/id --npm-host-id N` | yes | Explicitly takes over an existing NPM host (domains must match exactly); stores a snapshot first |
| `ingressctl forget --resource ns/id` | yes | Removes the state row of a service whose NPM host and certificate are gone and that is no longer in any manifest |
| `ingressctl remove --resource ns/id` | yes | Deletes the NPM host and certificate of a managed service that is no longer in any manifest, then its state row. Only deletes the host recorded in the state, and only if its domains match exactly; keeps a certificate that other hosts still use |
| `ingressctl recover --mapping m.yaml` | yes | Rebuilds the state from an explicit mapping; checks domains and the certificate marker |

Exit codes: `0` ok, `1` error or blocker, `2` renewal failed, `3` issuance pending (Infisical approval).

## Environment

| Variable | Meaning |
| --- | --- |
| `INGRESSCTL_STATE_DIR` | State directory (default `/var/lib/ingressctl`; Docker volume `ingressctl-state` in the stack) with `state.sqlite3`, `locks/`, `pending/` (0700) |
| `NPM_IDENTITY[_FILE]`, `NPM_SECRET[_FILE]` | Technical NPM user (no 2FA, visibility "all") |
| `INFISICAL_CLIENT_ID[_FILE]`, `INFISICAL_CLIENT_SECRET[_FILE]` | Machine identity `ingressctl` (Universal Auth, application role `operator`) |
| `INGRESSCTL_API_CA_BUNDLE` | Optional CA bundle for HTTPS APIs; there is no `verify=False` |

The stack's entrypoint sources `infra/secrets/npm.env` and `infra/secrets/ingressctl.env` from the checkout.

## What apply does

1. **Commit order.** A commit older than the last applied one is skipped; rolling back requires `--allow-rollback`.
2. **Live check.** A host it does not own that serves the same domain stops the run before any change.
3. **Upstream check.** `healthcheck.path` must answer with one of `accepted_statuses`.
4. **Certificate, only when needed.** That is: new service; changed SANs, issuer, or validity; missing or diverging NPM certificate; or remaining lifetime below the lead time (capped at ⅓ of the real lifetime, so short-lived certificates do not loop).
   1. Generate the key locally and keep it in `pending/`.
   2. Have Infisical sign the CSR.
   3. Verify key, SANs, serverAuth, and the chain against `ca_bundle`.
   4. Upload it as a **new** NPM custom certificate named `ingressctl:<resource>:<op>`.
5. **Proxy host.** Create it, or PUT only the managed fields: domains, upstream, websockets, `ssl_forced`, certificate, enabled, and `advanced_config` when `proxy.public` is declared. The PUT reloads nginx; `meta.nginx_online=false` counts as an error.
6. **HTTPS check.** For each domain, connect to NPM:443 with SNI and `ca_bundle`; NPM must present the expected certificate. NPM acknowledges the PUT before nginx has finished reloading, so the check retries (6 × 2 s). If it still fails, the host is switched back to the previous certificate.
7. **Record and clean up.** Write the state. Delete the old NPM certificate only when nothing references it. Nothing is revoked in Infisical.

Every external step is journaled. A re-run after a timeout or crash first looks for its own objects (NPM certificate name marker, Infisical metadata `ingressctl-op`) instead of creating duplicates.

## Public paths

A private UI that stays closed but has to receive one signed webhook from the internet is the case this is for. `proxy.public` gives the host a list of paths, methods and source networks; every other request for that host ends at nginx.

```yaml
proxy:
  domains: [app.lab.example]
  upstream: {scheme: http, server: worker, port: 8080}
  public:
    paths: [/hook/provider, "/files/*"]
    methods: [POST]
    allow_cidrs: [203.0.113.0/24, 2001:db8:1::/48]
```

`paths` is required; `methods` and `allow_cidrs` have defaults. `validate` rejects unknown fields, and there is no `preset`.

| Field | Meaning |
| --- | --- |
| `paths` | Required, non-empty. An entry is an exact path unless it ends in `/*`, which makes it a prefix: `/hook/abc` does not match `/hook/abcd`; `/files/*` matches `/files/`, `/files/x` and `/files/x/y`, but not `/files`. Case-sensitive; the query string is not part of the path. Rejected: `/`, `/*`, `//`, a `.` or `..` segment, a `*` that is not a trailing `/*`, characters outside `A-Za-z0-9._~-/` (so no space, `%`, `;`, `{`, `}`, quote, `$`, `#`), more than 200 characters. |
| `methods` | Methods allowed on every listed path, default `GET`, `HEAD`, case-insensitive. One of `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`; an empty list is rejected. There are no per-path methods. |
| `allow_cidrs` | Optional IPv4/IPv6 networks, strict (`10.0.0.0/8`, not `10.1.2.3/8`). When set, every source outside the list is denied. |

The block becomes the host's NPM advanced config. NPM inserts advanced config at server level, in front of its own `location /`; ingressctl writes no `location`, writes only server-level directives, and starts the block with `# managed by ingressctl: public paths only`.

### What it protects

The checks run in this order and end the request before it reaches the service:

| Situation | Answer |
| --- | --- |
| the request path matches none of `paths` | `404` |
| the path is listed, the method is not in `methods` | `405` |
| both pass, `allow_cidrs` is set and the source address is not in it | `403` |
| all pass | the request goes to `upstream` |

The order is visible from outside: a source outside `allow_cidrs` gets 404 on an unlisted path and 405 on a wrong method, and 403 only on an allowed path with an allowed method. Without `allow_cidrs` every source reaches the allowed paths. `/.well-known/acme-challenge/` is exempt from the path and the method check so that HTTP-01 can work (see the pitfalls).

### What it does not

- **It is not authentication.** An exposed path is reachable by whoever may send to it: the allowlist decides where, not who. The service behind it has to verify the sender itself — a signature or HMAC over the raw body in constant time, a timestamp or event id against replays, the secret from Infisical. A wrong signature is the service's 4xx, not nginx's.
- **A path is not a secret.** Paths are in git, in the NPM UI and in logs. A token in a path is a published token; authenticate with a header or the body.
- **Nothing about the content.** No body or header inspection, no rate limit, no size limit, no WAF; these three fields are the only filtering `public:` does.
- **Not TLS.** NPM terminates TLS and ingressctl only issues the certificate, from the private CA until #21 brings public names. The allowlist neither encrypts nor proves identity; a sender outside your network has to trust that CA (or you turn verification off), and a webhook provider that does not trust it will refuse the connection.
- **Not reachability.** DNS, the router's port forward and the firewall decide whether the host is reachable at all; `public:` only decides what answers once a request arrives.
- **Not the upstream.** A service that publishes a port or is reachable from another network is bypassed by talking to it directly. Publish it only on the stack network.
- **Only this host.** Another proxy host or domain is not covered; neither is the NPM admin UI.

### Pitfalls

- **NPM's own `location /`.** Advanced config is server level: NPM renders it before its `location /` and before the locations you add by hand. ingressctl cannot disable or guard that location, it only puts the checks in front of it, and only directives that are valid at server level are used. A hand-made `location` that sets its own `allow`/`deny` replaces the address check for itself.
- **The ACME exemption.** `/.well-known/acme-challenge/` passes the path and the method check. With the private CA, NPM adds no ACME location for the host, so such a request is proxied to the service like any other. `allow_cidrs` still applies: with it set, a source outside the list gets 403 on the challenge too, and HTTP-01 from the internet fails (#21). A Let's Encrypt certificate makes NPM add its own `location ^~ /.well-known/acme-challenge/` with `allow all`, and that overrides the server-level `deny all`: the challenge path is then open to every source, whatever `allow_cidrs` says.
- **The address nginx sees.** NPM trusts `X-Real-IP` from RFC 1918 addresses and from its generated CDN ranges (`set_real_ip_from`, `real_ip_header X-Real-IP`, `real_ip_recursive on`), and `allow_cidrs` compares the result. Behind a CDN or a tunnel that arrives from a trusted address, the tunnel has to set the client address; if it does not, every request looks like the tunnel and is denied. The other way round, a sender that reaches NPM from a trusted network chooses the address with that header — `allow_cidrs` is a coarse filter for known senders, not a security boundary.
- **`$uri` is not the raw request.** The path check uses `$uri`: percent-decoded, `.` and `..` resolved, `//` collapsed, query string dropped. `/hook/%70rovider`, `/hook/./provider`, `/hook/x/../provider` and `/hook//provider` all match `/hook/provider`. NPM forwards `$request_uri` to the service — the raw spelling — so the app receives a different string than the one the allowlist matched. Verify the signature over the request target the app actually receives, not over a path the framework re-encodes or re-decodes.
- **The redirect comes first.** With `force_https`, NPM's 301 to https sits before the block, so on port 80 an outsider gets 301, not 404 or 403. Check over https.
- **A trailing slash is another path.** `/hook/provider` and `/hook/provider/` are different paths; `/*` covers the subtree below a path, not the path itself.

### Before you expose a host

- [ ] the service publishes no port and is reachable only through NPM.
- [ ] `healthcheck.path` answers with one of `accepted_statuses`; `apply` refuses the host otherwise.
- [ ] the service verifies the sender's signature itself, in constant time, over the raw body, with a timestamp or event id against replays; the secret is in Infisical, not in git.
- [ ] `paths` are minimal and exact where possible, `methods` minimal, no token in a path.
- [ ] `allow_cidrs` holds the sender's published ranges, if it publishes any.
- [ ] `force_https: true`; then `ingressctl validate --root .` (no credentials), `ingressctl plan --root . --stack <id>`, and `apply` last.
- [ ] after `apply`: hit, miss and wrong method from outside, and the 403 if `allow_cidrs` is set.

```bash
# a hit: the service answers (whatever it answers to this request: 200, 401, 4xx-json, …)
curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://app.lab.example/hook/provider --data '{}'
# a miss: 404 from nginx, the service never sees the request
curl -sS -o /dev/null -w '%{http_code}\n' https://app.lab.example/admin
# the right path with a method that is not in `methods`: 405 from nginx
curl -sS -o /dev/null -w '%{http_code}\n' https://app.lab.example/hook/provider
# with `allow_cidrs`, from a network that is not listed: 403
```

Run these from outside your network, not from the LAN: a request from the LAN can arrive over one of NPM's trusted ranges, and `X-Real-IP` then decides what `allow_cidrs` sees. `app.lab.example` is the documentation example; use your own host.

### Taking it back

Remove the `public:` block, keep the rest of the service, commit, and apply:

```bash
python3 infra/komodo/run_ingressctl.py apply --root . --commit <sha> --stack <id>
```

`apply` removes exactly the block ingressctl wrote — marker, and only while it is unchanged at the start of the advanced config — and leaves lines added by hand in place. The host then has **no path restriction at all**: removing `public:` reopens everything, it does not close the service. To close the host, remove the service from the manifest, commit, and run `ingressctl remove --resource <namespace>/<id>` (it refuses while any manifest still declares the service, then deletes the NPM host and its certificate); also drop the DNS record or the router rule, which is what made the host reachable in the first place.

`plan` shows the advanced-config diff. While `public:` is declared, ingressctl owns the whole advanced config: its value has to be exactly the rendered block, so a line added by hand counts as drift and the next `apply` removes it — put other directives into NPM's `/data/nginx/custom` files instead. A host without `public:` keeps its advanced config untouched. A hand-made advanced config on a host that should receive `public:` stops the run with a conflict that names the host, because adoption is explicit.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

`tests/fakes.py` reproduces the API behaviour of NPM 2.16.0 and Infisical v0.167.2 as verified from their source (see `docs/discovery.md`): duplicate-domain rejection, upload without reload, private key in certificate GETs. The live contract tests and the pilots (omniroute, seerr) are recorded in `docs/acceptance.md`.
