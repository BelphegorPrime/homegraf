# seed

The initial content of **your own infrastructure repository** in Forgejo. The bootstrap of the stack pushes it there once and
fills the placeholders; after that the repository is yours: apps, inventory and settings change by pull requests.

| Path | Comes from | |
| --- | --- | --- |
| `homelab.py`, `infra/graph`, `infra/homelab`, `infra/infisical`, `infra/ingress-controller`, `infra/stackctl`, `infra/komodo/{komodo_api,create_stack,rebuild,run_ingressctl}.py` | the homegraf repository (its `TOOLS` list), copied once by the bootstrap | the tools homegraf runs; improvements belong upstream, in the homegraf repository |
| `inventory/*.yaml`, `stacks/`, this file | this repository | the skeleton, edited here |

## Placeholders

The files in `inventory/` hold placeholders between double `@` signs (the domain, the host's address and name, the repository on the forge,
the URLs of Infisical and Forgejo, the administrator's name). The bootstrap fills them in when it pushes this folder, and refuses a
placeholder it does not know. The full list, with what each means, is in `docs/stack.md` of the stack repository.
The certificate authority entries in `inventory/ingress.yaml` are `null` until the step that sets up the private CA fills them.

`inventory/homegraf.yaml` is documented in `infra/homelab/settings.py`. Hardware layers are optional: without
`inventory/network.yaml` and without a UniFi controller the map simply has no network tab.
