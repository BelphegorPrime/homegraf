# Contributing

Thanks for looking. Bug reports, ideas and pull requests are welcome. For a security problem, read [SECURITY.md](SECURITY.md) first and do not open an issue.

## What is in this repository

| Path | What | Where it is developed |
| --- | --- | --- |
| `app/`, `docs/app.md` | the homegraf app (Hono API, React, Tailwind, shadcn/ui) and its documentation | **here** |
| `README.md`, `LICENSE`, `compose.yaml`, `setup.sh`, `.env.example`, `bootstrap/`, `scripts/`, `docs/stack.md`, `.github/`, `SECURITY.md`, `CONTRIBUTING.md` | the all-in-one stack, the bootstrap, the checks | **here** |
| `seed/inventory`, `seed/stacks`, `seed/README.md` | the skeleton of a new installation's own repository | **here** |
| `seed/` (the tools listed in [`TOOLS`](TOOLS): `homelab.py`, `infra/graph`, `infra/homelab`, `infra/infisical`, `infra/ingress-controller`, `infra/stackctl`, `infra/komodo/...`) | the command line the app runs, and the first content of a new installation's own repository | **here** |

The tools are developed here, in the same pull request as the app that calls them. They are exposed to other repositories as tagged versions of the paths in `TOOLS`: the
author's homelab repository takes a tag with `infra/tools/update-tools.sh` (it writes the files to their own paths, records the tag and a hash of the files, and its CI refuses
local edits). So a change to a tool is a pull request here, then a tag. The app and the tools talk through commands and JSON (see
[docs/app.md](docs/app.md#where-things-are-developed)): keep that contract working.

## Before you open a pull request

Run what applies to your change (the same things run on GitHub):

```bash
# the app (Node 26)
cd app && npm ci && npm run typecheck && npm test

# the bootstrap (Python 3.11 or newer, standard library only)
python3 -W error::ResourceWarning -m unittest discover -s bootstrap/tests -t bootstrap

# the seed: copies it to an empty directory and runs the tools' tests there (pip installs their dependencies, so it needs the network)
scripts/check-seed.sh
```

Two checks use Docker and take minutes; run the one that matches what you touched:

- `scripts/e2e-stack.sh`: the whole life of an installation: a fresh stack, certificates against your own CA, an app created, merged, reached over HTTPS, removed.
- `scripts/e2e-host.sh`: a second host (a container with its own Docker daemon) added with `scripts/add-host.sh`, an app created, run and removed there.
- `scripts/e2e-restore.sh`: the restore of Forgejo's, Infisical's and Komodo's databases from the backup profile's dumps.

Both use their own project name and a temporary data directory, and clean up. Do not run them with a `.env` of a real installation in the directory.

## Conventions

- **Commits:** [Conventional Commits](https://www.conventionalcommits.org) (`feat(stack): ...`, `fix(app): ...`, `docs: ...`), one concern per commit.
- **Python:** 3.11 and newer, the standard library plus PyYAML. **Shell:** `bash`, `set -euo pipefail`. **Language:** English in code, comments and docs.
- **Secrets:** never on a command line, never in a log. Values travel in the environment or through stdin; the tools say so in their `--help`.
- **Generic examples:** tests, docs and examples use neutral names: `lab.example` or `home.lab`, `10.0.0.x`, `worker-1`, `owner`. Nothing may name a real
  homelab (its domain, addresses, server names, paths) or carry a real key or token; the checks refuse such lines.
- **Tests with the change:** a fix comes with a test that fails without it; a new action comes with a test for who may do it and who may not.
- **Docs with the change:** the README or `docs/` say what the change does; documentation that no longer matches the code is a bug.

## Pull requests

Describe what changes and why, and how you checked it. Small and focused beats large. By contributing you agree that your contribution is licensed under the
[MIT License](LICENSE) of this repository.
