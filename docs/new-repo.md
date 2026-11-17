# Creating a repository for the factory

The factory turns labelled issues into draft pull requests, but it does not create the repositories it works in. An
operator does that once per project, with `./homelab.py new-repo`: it creates the Forgejo repository, writes the files of
a profile, adds the agent as a collaborator, installs the factory labels and the ForgeHands webhook, and can propose the
ForgeHands allow-list and merge-policy entries as a pull request.

## Why the operator does it

Creating a repository needs a Forgejo token that may create repositories, add collaborators, install webhooks and push.
That is the operator's token, not the factory's. The factory never receives it, and an agent cannot make itself a new
home: agents work only in repositories an operator has already created and wired.

The command never prints, stores or echoes the token. In homegraf the same command runs as a job with the token homegraf
was started with; the browser asks for your homegraf login, never for the Forgejo token.

## Before you start

Run the command from the infrastructure repository (the checkout that holds `homelab.py`), with these settings:

| Setting | Meaning |
| --- | --- |
| `FORGEJO_TOKEN` | a Forgejo token with write access to create the repository, its labels, webhook and pull request |
| `FORGEJO_URL` | optional; the Forgejo server, defaulting to the host of your `origin` remote |
| `FORGEHANDS_FORGEJO_WEBHOOK_URL` | the endpoint Forgejo calls, for example `http://forgehands:8080/webhooks/forgejo` |
| `FORGEHANDS_FORGEJO_WEBHOOK_SECRET` | the secret of that webhook |

The agent's Forgejo user must already exist; it becomes a collaborator with write access. `--agent-login` changes the
login (default `openhands`).

List the profiles first:

```sh
./homelab.py repo-profiles
```

## Dry run, then create

Without `--yes` the command only prints the plan and makes no request:

```sh
./homelab.py new-repo --name example-repo --owner owner --profile python --description "Example service" --private
```

```
new repository owner/example-repo (private) on https://forgejo.lab.example
  description   Example service
  branch        main
  collaborator  openhands with write
  profile       python: 5 files
    .forgejo/workflows/ci.yaml
    .gitignore
    AGENTS.md
    CONTRIBUTING.md
    README.md
  labels        7: ai:approved, ai:changes, ai:close, ai:escalate, ai:plan, ai:ready, ai:review
  webhook       http://forgehands:8080/webhooks/forgejo
                issues, pull_request_label, action_run_failure, action_run_success, push

Nothing was created or changed: rerun with --yes to create the repository and its first commit.
```

`--profile` and `--name` are required. The owner defaults to the owner of `origin`; the description defaults to the
profile's description; the branch defaults to `main`; without `--public` the repository is private.

Create it by repeating the command with `--yes`:

```sh
./homelab.py new-repo --name example-repo --owner owner --profile python --description "Example service" --private --yes
```

Add `--wire` to the same command to also open the pull request that adds the repository to ForgeHands (see below).
`--json` prints the plan without `--yes`, and the verified result with it.

What the command does, in order: it renders the profile, creates the repository, adds the agent as a collaborator,
creates the seven `ai:*` labels the factory uses, creates the webhook for issues, label, action-run and push events,
commits the profile files to the default branch, and reads all of it back. It exits 0 only when that read-back matches.
A missing setting fails before the first request and names the setting.

A rerun is safe: the repository, labels and webhook are not created twice. A file that already has different content is
not overwritten; the command stops and names the path.

## Profiles

`./homelab.py repo-profiles` lists the profiles on the machine, and `--json` gives the same for the app. The built-in
profiles are:

| Profile | Files | Validate command |
| --- | --- | --- |
| `generic` | `AGENTS.md`, `README.md`, `CONTRIBUTING.md`, `.gitignore` | `git diff --check` |
| `python` | the same, plus `.forgejo/workflows/ci.yaml` for pytest | `python -m pytest -q` |
| `node` | the same, plus `.forgejo/workflows/ci.yaml` for npm | `npm run test --if-present && npm run typecheck --if-present && npm run lint --if-present` |

A profile is a folder under `infra/homelab/repo_profiles/` with a `profile.yaml` and its templates:

```text
infra/homelab/repo_profiles/example/
  profile.yaml
  agents.md.tmpl
  readme.md.tmpl
  contributing.md.tmpl
  gitignore.tmpl
  forgejo-ci.yaml.tmpl
```

```yaml
name: example
description: "an example project"
validate_command: git diff --check
files:
  - {source: agents.md.tmpl, target: AGENTS.md}
  - {source: readme.md.tmpl, target: README.md}
  - {source: contributing.md.tmpl, target: CONTRIBUTING.md}
  - {source: gitignore.tmpl, target: .gitignore}
  - {source: forgejo-ci.yaml.tmpl, target: .forgejo/workflows/ci.yaml}
```

The folder name is what `--profile` takes; `name` is what `repo-profiles` shows. `files` writes each source template to
its target inside the new repository, so a target cannot be absolute or contain `..`. Include an `AGENTS.md`: that is
where the agent finds the validate command.

Templates use `{{name}}`, `{{description}}`, `{{owner}}`, `{{default_branch}}` and `{{validate_command}}`. Everything
else is copied as written. A workflow expression such as `${{ github.token }}` is not a placeholder; only `{{` without
a `$` in front of it is one. An unknown or unclosed placeholder stops the command before anything is created.

Adding a profile is adding a folder. `repo-profiles` and `new-repo` pick it up without a code change.

## Wiring the repository for ForgeHands

ForgeHands works only repositories listed in the `ALLOWED_REPOS` setting of the stack that runs it, and it merges an
agent pull request automatically only when that repository has an entry in `MERGE_POLICY`. `--wire` proposes both
entries in one pull request:

```sh
./homelab.py new-repo --name example-repo --owner owner --profile python --description "Example service" --private --wire --yes
```

It looks for exactly one stack under `stacks/` whose `compose.yaml` (or `compose.yml`) runs the ForgeHands image. It
adds `owner/example-repo` to `ALLOWED_REPOS` and a default entry to `MERGE_POLICY` with the deny list below, keeps the
file's layout and comments, and opens a pull request on branch `new-repo/example-repo` titled
`feat(new-repo): wire owner/example-repo into ForgeHands`. Nothing is deployed by the command.

The default deny list is `.forgejo/**`, `.github/**`, `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `.agents/**`,
`.claude/**`, `compose.yaml`, `**/compose.yaml`, `setup.sh` and `scripts/**`. A pull request that touches one of those
paths is never merged automatically, even with `ai:approved`; a reviewer merges it by hand. The wiring pull request
itself touches `compose.yaml`, so it is always a manual merge.

A repository without a `MERGE_POLICY` entry is **never** merged automatically. The entry is the opt-in that says which
paths the factory may merge without a person; leaving it out keeps the repository entirely manual. `ALLOWED_REPOS` is
the same kind of decision for starting runs: a trigger label on a repository that is not listed is ignored.

It stops when no stack or several stacks run ForgeHands, when an `ALLOWED_REPOS` or `MERGE_POLICY` value is a compose
variable (`${...}`), because a literal added there would not reach ForgeHands when the variable is set, and when the
stack does not set those keys at all. In those cases edit that compose file by hand in a pull request and merge it. If
ForgeHands is part of the root compose file rather than a stack under `stacks/`, the command cannot find it and the same
manual edit applies.

A repository created without `--wire` can be wired later by running the same command again with `--wire`; the first
half recognises the repository and only adds the pull request.

## In the app

The **New repository** form in homegraf runs the same command. It needs the `apps:create` permission, and the server
must have `FORGEJO_TOKEN`, `FORGEHANDS_FORGEJO_WEBHOOK_URL` and `FORGEHANDS_FORGEJO_WEBHOOK_SECRET`; otherwise the
request answers `not configured: ...`.

Open **Repositories** and choose **New repository**. The form asks for:

- **Name**: letters, digits, `.`, `_` and `-`, up to 100 characters; not starting with `.` or `-`, and no `..`.
- **Profile**: one of the server's profiles; the form shows its description and validate command.
- **Description**: required, up to 255 characters.
- **Visibility**: `private` (default) or `public`.
- **Default branch**: optional; empty uses the command's default, `main`.

If you signed in with a name and password, the form asks for your homegraf name and password for the action (single
sign-on does not). Nothing asks for a Forgejo token: the server uses its own.

**Create repository** starts one job and streams its output. Only one repository is created at a time; a second request
gets `another repository is being created, wait for it to finish`. When the job is done the page links the new
repository, and, if ForgeHands does not list it yet, says that the allow-list still needs the pull request made by
`new-repo ... --wire`. The form itself does not pass `--wire`.

## After the repository exists

1. If you used `--wire`, review and merge the wiring pull request, then deploy or restart ForgeHands so it reads the new
   `ALLOWED_REPOS` and `MERGE_POLICY`. Without an allow-list entry the factory ignores the repository.
2. Write the first issue in the new repository yourself: the factory only works issues that exist. Label it `ai:plan` to
   have the planner split it into slices, or `ai:ready` to start a worker on it. In the webhook setup, `ai:ready` is the
   trigger that starts a run; see [agents.md](agents.md).
3. The worker pushes a branch `ai/issue-<n>` and opens a draft pull request. A reviewer labels it `ai:approved` or
   `ai:changes`. Approval merges it only when the repository has a matching `MERGE_POLICY` entry and no changed file
   matches a deny pattern; otherwise merging stays with you.
