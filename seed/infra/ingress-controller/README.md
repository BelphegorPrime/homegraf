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
5. **Proxy host.** Create it, or PUT only the managed fields: domains, upstream, websockets, `ssl_forced`, certificate, enabled. The PUT reloads nginx; `meta.nginx_online=false` counts as an error.
6. **HTTPS check.** For each domain, connect to NPM:443 with SNI and `ca_bundle`; NPM must present the expected certificate. NPM acknowledges the PUT before nginx has finished reloading, so the check retries (6 × 2 s). If it still fails, the host is switched back to the previous certificate.
7. **Record and clean up.** Write the state. Delete the old NPM certificate only when nothing references it. Nothing is revoked in Infisical.

Every external step is journaled. A re-run after a timeout or crash first looks for its own objects (NPM certificate name marker, Infisical metadata `ingressctl-op`) instead of creating duplicates.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

`tests/fakes.py` reproduces the API behaviour of NPM 2.16.0 and Infisical v0.167.2 as verified from their source (see `docs/discovery.md`): duplicate-domain rejection, upload without reload, private key in certificate GETs. The live contract tests and the pilots (omniroute, seerr) are recorded in `docs/acceptance.md`.
