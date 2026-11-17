# Operating the agent flow

The `agents` profile turns a labelled Forgejo issue into a draft pull request: `forgehands` receives the Forgejo webhook and starts an
OpenHands conversation for the label, the agent works in its own checkout and pushes a branch `ai/issue-<n>`, and the platform's own rules
decide what happens next. The setup is in [stack.md](stack.md#agents-from-an-issue-to-a-draft-pull-request); this page is how to watch the
runs, stop them, start an issue again, and what goes wrong.

The factory needs ForgeHands 0.4.2 or newer; `compose.yaml` pins `v0.4.2`. This page describes what that version does; the
[README of the tag](https://github.com/BelphegorPrime/ForgeHands/blob/v0.4.2/README.md) lists every setting with its default, and its
`docs/UPGRADING.md` the move from 0.3.

To create the repository the agents work in, and to allow it in ForgeHands, see [new-repo.md](new-repo.md).

## Seeing and controlling runs

ForgeHands tags every conversation it starts with `source=forgehands` and `issue=<n>`. `GET /api/conversations/search` lists conversations,
newest first; it takes `limit` (default 100), `page_id` and `status`, and has no tag filter, so keep the items whose tags match. The states
are `running`, `finished`, `error`, `stuck` and `paused`; the API also knows `idle`, `waiting_for_confirmation` and `deleting`. The search and
the pause call use the stack's OpenHands backend key.

**Stop a run:** `POST /api/conversations/{id}/pause`. The call returns before the run stops: it takes effect after the current LLM call, and
the state changes to `paused` later. A `stuck` run holds its queue slot until it ends; pausing it stops the work but the slot comes back only
when the run ends or the run timeout passes.

**Re-run an issue:** remove its trigger label and add it again. ForgeHands starts a run only for a trigger label that is new since the last
event it saw; removing and re-adding is what makes it new. The same label twice while a run is active still starts one run.

**The ledger.** `jobs.json` (`JOBS_FILE`, in the `forgehands-data` volume) holds the jobs with their conversation ids and queue state, the
hourly count, the run timeouts, and the labels last seen per issue (`repo#issue` -> the labels and when they were seen). It is how ForgeHands
knows a label is new and that an issue already has a run. Deleting it forgets both: every trigger label currently on an issue then counts as
new once and can start a run again.

**The status page.** `/status`, `/status.json` and `/metrics` answer on the ForgeHands port. With `STATUS_TOKEN` set (`FORGEHANDS_STATUS_TOKEN`
in `.env`, at least 16 characters; `compose.yaml` passes it) they need `Authorization: Bearer <token>` or HTTP basic auth with the token as
the password; `/healthz` stays open. Without it all three are open and ForgeHands logs a warning at start, so keep them behind the proxy.
`/status` shows, per issue, the run state with its cost, the rounds used (`n/3`, "escalated to <label>" when the last one ran stronger),
`fell_back` when a job started on its fallback route, and the tier's quota hold with the time it tries again. `/metrics` is Prometheus text
(`forgehands_jobs`, `forgehands_runs_finished_total`, `forgehands_merge_refusals_total`, ...); its labels come from fixed sets and carry no
issue numbers. See [Status page and metrics](https://github.com/BelphegorPrime/ForgeHands/blob/v0.4.2/README.md#status-page-and-metrics).

## Limits

The queue and timeout limits are set in `.env` and passed to ForgeHands by `compose.yaml`; `setup.sh` writes the commented defaults for the
agents profile. `max_iterations` (and the optional `max_concurrent` and `max_budget`) are part of a route.

| Setting | Default | What it limits |
| --- | --- | --- |
| `FORGEHANDS_MAX_CONCURRENT_PER_TIER` | 2 | runs of one tier at once; the rest wait in the queue |
| `FORGEHANDS_MAX_CONCURRENT` | 4 | runs of the whole stack at once |
| `FORGEHANDS_MAX_RUNS_PER_HOUR` | 30 | runs started per hour, a cap against runaway fan-out |
| `FORGEHANDS_MAX_RUN_MINUTES` | 120 | how long a run keeps its slot; the conversation itself is not deleted |
| `max_iterations` in `LABEL_ROUTES` | 80 planner, 120 worker, 120 worker-strong, 60 reviewer and security review | LLM calls of one run |
| `max_concurrent` in a route | unset | per-tier override of the first row |
| `max_budget` in a route | unset | cost of one run |

`bootstrap/hgboot/agents.py` holds the label, profile, tier and `max_iterations` table and builds `LABEL_ROUTES` from the profile ids.

**Tiers.** A route names a tier, and the tier decides two things in ForgeHands: the per-tier queue (`FORGEHANDS_MAX_CONCURRENT_PER_TIER`) and
the quota hold. Which model answers is the gateway's choice for the OpenHands profile, not ForgeHands'. This stack uses `smart` for the
planner, the reviewers, `worker-strong` and the security review, `cheap` for the worker routes and `free` for the fallback routes. A route
may name `"fallback_to": "<another route>"`: while its own tier is held and the target's is not, the run starts with the target's profile and
tier instead of waiting. `ai:ready`, `ai:changes` and `ci:failure` fall back to `ai:ready-free`, `ai:changes-free` and `ci:failure-free`; one
hop only, the target needs no label and has no fallback of its own. See
[Fallback route](https://github.com/BelphegorPrime/ForgeHands/blob/v0.4.2/README.md#humans-and-failures).

**Out of quota, or no model available.** Neither case ends at the human. When a run ends in `error` and its last events say it was a rate
limit, the job is queued again and its whole tier is held until the reset time (at least a minute, at most `QUOTA_HOLD_MAX_HOURS`, 8 in
`compose.yaml`; 15 minutes when the error names no time), and given up only after `QUOTA_GIVE_UP_HOURS` (24). An error that says no model
could answer (`LLMServiceUnavailableError`, `BadGatewayError`, `LLMTimeoutError`) is treated like a used-up quota with growing pauses (1, 2,
4, 8, 16, then 30 minutes). While a tier is held, a route's `fallback_to` takes over; `/status` shows "model quota used up, tries again after
HH:MM UTC". The hold lives in memory, so a restart means a retry now. See
[Humans and failures](https://github.com/BelphegorPrime/ForgeHands/blob/v0.4.2/README.md#humans-and-failures).

Cost is not capped by ForgeHands on its own: a run has a price only when the LLM gateway reports one, and the gateway is where spend is
steered (model choice, budgets, keys). The guards inside ForgeHands are `MAX_RUNS_PER_HOUR` (runs started per hour) and a route's
`max_budget` (one run); a weekly cap per tier does not exist any more. Read the gateway's own accounting for what was spent.

## The factory loop

The loop is: a label starts a worker, the worker pushes a branch and opens a draft pull request, CI runs on the commit, a green commit
starts a review, and a review either approves or asks for another round. Agents never merge; ForgeHands merges by the operator's policy.

### Rounds

- `ai:changes` on the pull request of an agent issue (head branch `ai/issue-<n>`) starts a follow-up round of a worker on the **same**
  branch. The reviewer sets it, or you do. The worker reads the reviews and their comments, the conversation of the pull request and the
  issue, fixes what the trusted senders asked for, pushes to the same branch and comments; it never opens a second branch or pull request.
  The route is a `LABEL_ROUTES` entry with `"on": "pull_request"`; the first such route names the label the reviewer uses.
- A red CI run starts a **fix round** through the route with `"on": "ci_failure"`: reproduce the failing checks, fix, push to the same
  branch, comment.
- A label round and a fix round both count against `MAX_ROUNDS` (default 3, a ForgeHands environment setting) per issue. When they are used
  up no further round starts: the pull request gets a comment and the label `ai:blocked` (`BLOCKED_LABEL`). A review does not count as a
  round.
- **The last round escalates.** `ai:changes` and `ci:failure` name `"escalate_to": "ai:escalate"`, so the last round (`MAX_ROUNDS`, 3) runs
  with the `worker-strong` profile on `smart` before the pull request is blocked and the human mentioned. Branch, kind, prompt and the route's
  own iteration and budget limits stay; the target's tier limits apply; nobody triggers the stronger profile from an issue, and `/status`
  shows the job as escalated. See
  [Follow-up rounds and escalation](https://github.com/BelphegorPrime/ForgeHands/blob/v0.4.2/README.md#follow-up-rounds-and-escalation).
- A green commit (all workflows) starts the **review** through the route with `"on": "ci_success"`, but only when the **combined** commit
  status is `success`, so a pending or failed second workflow holds the review back. Without `FORGEJO_URL` and `FORGEJO_TOKEN` ForgeHands
  cannot check the status and starts no review. The reviewer reads the issue, the diff and the discussion, writes one comment with the
  evidence (met or not per acceptance criterion, with file and line) and adds exactly one label: `ai:approved` (`APPROVED_LABEL`) or the
  changes label, `ai:changes`. It does not push, edit or merge.
- A new round or a new review removes `ai:approved` first, so an approval never covers commits it has not seen. A fix that pushes a new
  commit causes new CI events and a new review, until the rounds are used up.

### Merging

`ai:approved` on the pull request of an agent issue makes ForgeHands merge it when, at the same time: the pull request is open, it comes from
`ai/issue-<n>`, it is mergeable, the combined commit status of its head commit is green, and no changed file (a rename counts with both
names) matches a deny pattern of `MERGE_POLICY` for that repository.

`MERGE_POLICY` is a JSON value in ForgeHands' own environment (`FORGEHANDS_MERGE_POLICY`; the bootstrap writes it into the runtime handoff
and `compose.yaml` passes it on), for example `{"owner/repo": {"deny": ["stacks/**"], "reasons": {"stacks/**": "what runs on the host"}}}`;
`*` matches within a folder, `**` across folders, a plain name is an exact path, and a reason is optional per pattern. It is deliberately not
a file of the repository, because the pull request it should restrain could change that file. A repository without an entry in `MERGE_POLICY`
is **never** merged automatically.

A pull request that touches a protected path is left to the human with **one** comment that mentions him: what is done (commit approved, CI
green), why a human — each matching pattern with its reason from `MERGE_POLICY` and the files — what the security review found for that
commit and whether Forgejo reports a conflict. While a security review of the commit still runs, the comment waits for it. Approved and green
ends the work in progress, so the `WIP:` marker is removed first; Forgejo would not let the human merge it otherwise. See
[Auto-merge](https://github.com/BelphegorPrime/ForgeHands/blob/v0.4.2/README.md#auto-merge).

The merge uses `MERGE_METHOD` (default `merge`), is bound to the head commit that was checked, deletes the branch and closes the issue. It
needs `FORGEJO_URL` and `FORGEJO_TOKEN` in ForgeHands' own environment; `compose.yaml` passes the agent user's token.

### Security review of protected paths

A route with `"on": "sensitive_review"` — `sensitive:review` here, profile `sensitive-reviewer` on `smart` — starts a second, read-only
review when a commit is green and a changed file matches the deny list, once per commit and next to the normal review, never a round. It
posts one comment that starts `## Security review: OK` or `## Security review: CONCERNS` and names the commit, sets no label, approves and
merges nothing. A run without that comment is made once more, then only logged, and a failed one never blocks. The route names its own forge
token (`forgejo_token_secret`, `FORGEJO_SECURITY_TOKEN` in `AGENT_SECRETS`), so the security reviewer writes as a user the worker's token
cannot sign as. Without the route nothing changes. See
[Security review for protected paths](https://github.com/BelphegorPrime/ForgeHands/blob/v0.4.2/README.md#security-review-for-protected-paths-advisory).

### Keeping pull requests current

After a push to the default branch has been quiet for `BRANCH_UPDATE_DELAY_SECONDS` (default 60), ForgeHands brings open agent pull requests
up to date through Forgejo (`POST /pulls/<n>/update?style=rebase`, no agent involved); CI and the review then run on the new commit. This is
`UPDATE_BRANCHES` (default on), and it acts only when an update is cheap and needed: the default branch changed a file the pull request changes
too, CI is not running on its head, and the last update was more than `BRANCH_UPDATE_MIN_INTERVAL_MINUTES` (30) ago. The webhook must send
`push`; ForgeHands only looks at pushes to the default branch, so its own updates and the agents' pushes cause nothing.

A pull request is left alone when it has an approval or an OK review for its current commit (a rebase would throw that away), when it
carries `ai:blocked`, or while an agent runs on its issue; approved and reviewed pull requests merge as they are. A protected-path pull
request that waits for the human must not stay conflicted, so when the forge reports such a pull request as not mergeable it gets a conflict
round too, after which CI, the review and the security review run again. If the update answers 409, the branch conflicts: the `pr:conflict`
route (`"on": "conflict"`) starts a conflict round — `worker-strong` on `smart` here — once per tip of the default branch and counted
against `MAX_ROUNDS`. The worker rebases onto the default branch, resolves every conflict so that the purpose of the pull request stays and
what the default branch brought in is kept, runs the checks, pushes once with `--force-with-lease` and comments what it resolved. This is
the only round in which a rebase and a force push are allowed.

Forgejo reports `mergeable: false` for a pull request titled `WIP:` even without a conflict, so that field is not the test for bringing one up
to date; the update call is. Once the `WIP:` marker is gone, after approval, it is reliable, which is how a conflicting approved pull request
is found. Without the `pr:conflict` route, or with the rounds used up, the pull request gets `ai:blocked` and a comment.

### Reminders

With `REMIND_AFTER_DAYS` (`FORGEHANDS_REMIND_AFTER_DAYS`, 2 in `compose.yaml`; needs `FORGEJO_URL`, `FORGEJO_TOKEN` and `HUMAN_LOGIN`)
ForgeHands looks once a day, and once after a restart, at open pull requests from `ai/issue-<n>` and reminds the human of three kinds: marked
`ai:blocked`, approved and green on a protected path (left to him), and untouched for `n` days. If its last comment is at least `n` days old
it writes one comment that starts with `@<login>`, says why the pull request waits and since when, and counts (`ForgeHands: reminder i of k`)
— at most `REMIND_MAX` (3), the last with `STALE_LABEL` when that is set. A comment by the human restarts the count; the state lives in the
comments, so a restart only empties the `/status` list until the next pass. See
[Reminders](https://github.com/BelphegorPrime/ForgeHands/blob/v0.4.2/README.md#reminders).

### Webhooks and events

The repository needs a Forgejo webhook with the events `issues`, `pull_request_label`, `action_run_failure`, `action_run_success` and `push`.
Forgejo accepts the two `action_run_*` events only when the hook is created: a PATCH does not add them, so delete the hook and create it
again. Forgejo sends one event per finished workflow, which is why the review waits for the combined status.

### Two Forgejo accounts

The agents write as `homegraf-agent` (`FORGEHANDS_AGENT_LOGIN`), the human as his own user (`HUMAN_LOGIN`); both are in `ALLOWED_SENDERS`.
Forgejo refuses a formal approve or request-changes on one's own pull request (HTTP 422), so the verdict is a label, not a review state.
Comments and reviews remain the place for evidence and for asking for changes, but only the labels start the next run. The security review
has a third account when its route is set up: the token behind `FORGEJO_SECURITY_TOKEN` must belong to a user the worker's token cannot sign
as.

### Quirks worth knowing

- A pull request whose title starts with `WIP: ` reads `mergeable: false` and Forgejo refuses to merge it. Agent pull requests are opened with
  that prefix; ForgeHands removes it after approval and before merging.
- After changing an agent profile, read it back: a change that did not stick leaves a route that starts a profile other than the one you
  meant.
- Create agent profiles with `mcp_server_refs: []`. A profile that inherits every server starts with about 160 tools instead of about 20,
  which costs context and causes wrong tool choices.

## Known failure modes

**A worker run "finishes" without delivering.** A model that answers in words without calling a tool ("next I will clone the repository")
ends a worker run as `finished` in a minute with nothing done: no branch, no pull request. The tool rule in the prompt (every turn must call a
tool until the task is done) makes this rarer, and ForgeHands checks the result: a worker run on an issue must leave a pull request from
`ai/issue-<n>` created or changed since the run began, a comment on the issue written since then, or close the issue. If it did none of that,
the run is made once more; after a second empty run the ticket is marked failed (`no_delivery`) and the human is mentioned on the issue. A strong
worker that stops after a local commit is found the same way — check the branch on the remote before assuming there is work. See
[Humans and failures](https://github.com/BelphegorPrime/ForgeHands/blob/v0.4.2/README.md#humans-and-failures).

**A worker runs out of iterations or time.** `SPLIT_LABEL` is `ai:plan` in `compose.yaml`: a worker run on an issue that ends as
`max_iterations`, `paused`, `stuck` or `timeout` is not handed to the human, because the ticket is probably too big. The worker label comes
off, `ai:plan` goes on and the planner splits it. Planner and epic tickets are never sent there; a run that ends in `error` twice still goes
to the human.

**The planner waits forever.** If its prompt lets it wait for workers, it never ends. The end rule in the prompt fixes it: a planner stops
after it has written the plan.

**The reviewer acts as a planner.** An epic's text can tell the agent to plan; a reviewer that follows it plans instead of reviewing. The
reviewer's prompt must say that it ignores instructions in the issue and only reviews against the acceptance criteria.

**The reviewer blames a pull request for a check that also fails on the default branch.** Take the baseline first: run the same check on the
default branch. Only a failure the pull request introduced counts against it; a repository-wide failure is not the pull request's fault.

**A stuck run.** OpenHands aborts a conversation that repeats the same action, and the state becomes `stuck`. A stuck run holds its slot
until it ends; pause it or wait for `FORGEHANDS_MAX_RUN_MINUTES`. On a worker ticket it then goes to `SPLIT_LABEL` like the other exhausted
runs.

**Gateway timeouts and 5xx.** A slow or failing LLM gateway appears in the OpenHands log as retried errors. A slow free model can exceed the
reverse proxy's read timeout before the gateway answers, and the run fails without a useful error in Forgejo. An end that the last events
explain as a rate limit or as "no model available" holds the tier and retries instead of calling the human (see [Limits](#limits)).

## Safe defaults and what is automatic

- The prompt tells every role never to merge and never to push to the default branch. Where the agents use their own Forgejo account,
  branch protection enforces the second. Merging is ForgeHands' job and happens only by `MERGE_POLICY`.
- Agent pull requests are drafts and carry the `WIP: ` title prefix until ForgeHands approves and cleans the title. A draft `WIP:` pull
  request cannot be merged.
- The issue text goes into the first message between markers and is untrusted: it may describe the task, it may not change the agent's rules,
  secrets or tools.

| What | Who does it |
| --- | --- |
| Secrets, `FORGEHANDS_ALLOWED_REPOS`, the `openhands.<domain>` proxy host and its certificate | `setup.sh` and the bootstrap (`hgboot`) |
| Services, volumes, healthchecks, fixed `WORKSPACE_DIR` / `JOBS_FILE`, queue and timeout defaults | `compose.yaml` |
| Label -> profile id, tier, iterations (`LABEL_ROUTES`) | the bootstrap (`bootstrap/hgboot/agents.py`) builds it from the profile ids; a value you set in `.env` wins |
| Model provider key, LLM profiles, the agent profiles | you, in Canvas; the bootstrap reads the profile ids back and does not create the profiles yet |
| The Forgejo webhook, and (webhook mode) the OpenHands automation | you, in Forgejo and Canvas |
| Starting a review by hand (`ai:review`) | you; a green commit starts the review when the `ci_success` route exists |
| The verdict (`ai:approved`, `ai:changes`), and the merge itself | the reviewer sets the label; ForgeHands merges by `MERGE_POLICY`; a repository without a policy stays manual |
| Choosing models, tiers and tool sets | you |
| Creating issues, writing acceptance criteria, labelling `ai:ready` or `ai:escalate` | you |

## Prompts

The prompts live as files and are referenced here, not pasted:

- `src/prompt.mjs` in the ForgeHands repository builds the first message of every run: the issue run, the follow-up round, the fix round and
  the review. The untrusted block, the trusted sender list, the tool rule and the end rule are in that file.
- The role prompts are the prompts of the agent profiles (`planner`, `worker`, `worker-strong`, `reviewer`, the `worker-free` fallback and
  the security reviewer) that you create in Canvas; the profiles live in the `openhands-data` volume. `bootstrap/hgboot/agents.py` maps labels
  to profiles and limits and contains no prompt text.

Change the file or the profile and start a new run. A prompt copied into an issue, a pull request or this page would go stale, and an agent
reading it could mistake it for instructions.
