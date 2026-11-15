# Operating the agent flow

The `agents` profile turns a labelled Forgejo issue into a draft pull request: `forgehands` receives the Forgejo webhook and starts an
OpenHands conversation for the label, the agent works in its own checkout and pushes a branch `ai/issue-<n>`, and the platform's own rules
decide what happens next. The setup is in [stack.md](stack.md#agents-from-an-issue-to-a-draft-pull-request); this page is how to watch the
runs, stop them, start an issue again, and what goes wrong.

The factory loop below (rounds, the review on a green commit and `MERGE_POLICY`) needs ForgeHands 0.2.10 or newer; the follow-up rounds
arrived in 0.2.8 and the merge policy in 0.2.10. With an older image the four trigger labels still start their agent profiles, and the round,
review and merge steps do nothing. Check the image tag in `compose.yaml`.

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

## Limits

The queue and timeout limits are set in `.env` and passed to ForgeHands by `compose.yaml`; `setup.sh` writes the commented defaults for the
agents profile. `max_iterations` (and the optional `max_concurrent` and `max_budget`) are part of a route.

| Setting | Default | What it limits |
| --- | --- | --- |
| `FORGEHANDS_MAX_CONCURRENT_PER_TIER` | 2 | runs of one tier at once; the rest wait in the queue |
| `FORGEHANDS_MAX_CONCURRENT` | 4 | runs of the whole stack at once |
| `FORGEHANDS_MAX_RUNS_PER_HOUR` | 30 | runs started per hour, a cap against runaway fan-out |
| `FORGEHANDS_MAX_RUN_MINUTES` | 120 | how long a run keeps its slot; the conversation itself is not deleted |
| `max_iterations` in `LABEL_ROUTES` | 80 planner, 120 worker, 120 worker-strong, 60 reviewer | LLM calls of one run |
| `max_concurrent` in a route | unset | per-tier override of the first row |
| `max_budget` in a route | unset | cost of one run |
| `TIER_WEEKLY_BUDGETS` | unset | cost of a tier within seven days |

`bootstrap/hgboot/agents.py` holds the label, profile, tier and `max_iterations` table and builds `LABEL_ROUTES` from the profile ids.

ForgeHands does not limit cost unless the LLM gateway reports a price. `max_budget` and `TIER_WEEKLY_BUDGETS` only work with such a gateway;
add them to the service's environment (or a route) before relying on them, and read the gateway's own accounting for spend.

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

`MERGE_POLICY` is a JSON value in ForgeHands' own environment (it is not among the defaults that `compose.yaml` passes), for example
`{"owner/repo": {"deny": ["stacks/**", "compose.yaml"]}}`; `*` matches within a folder, `**` across folders, a plain name is an exact path.
It is deliberately not a file of the repository, because the pull request it should restrain could change that file. A repository without an
entry in `MERGE_POLICY` is **never** merged automatically. A pull request that touches a protected path stays open, with a comment naming the
paths.

The merge uses `MERGE_METHOD` (default `merge`), is bound to the head commit that was checked, deletes the branch and closes the issue. It
needs `FORGEJO_URL` and `FORGEJO_TOKEN`; in direct mode those come from `AGENT_SECRETS`.

### Webhooks and events

The repository needs a Forgejo webhook with the events `issues`, `pull_request_label`, `action_run_failure` and `action_run_success`.
Forgejo accepts the two `action_run_*` events only when the hook is created: a PATCH does not add them, so delete the hook and create it
again. Forgejo sends one event per finished workflow, which is why the review waits for the combined status.

### One Forgejo account

The agents and the human share one Forgejo account. Forgejo refuses a formal approve or request-changes on one's own pull request (HTTP
422), so the verdict is a label, not a review state. Comments and reviews remain the place for evidence and for asking for changes, but only
the labels start the next run.

### Quirks worth knowing

- A pull request whose title starts with `WIP: ` reads `mergeable: false` and Forgejo refuses to merge it. Agent pull requests are opened with
  that prefix; ForgeHands removes it after approval and before merging.
- After changing an agent profile, read it back: a change that did not stick leaves a route that starts a profile other than the one you
  meant.
- Create agent profiles with `mcp_server_refs: []`. A profile that inherits every server starts with about 160 tools instead of about 20,
  which costs context and causes wrong tool choices.

## Known failure modes

**A free worker ends without doing anything.** The model answers with text such as "next I will clone repository" and makes no tool call.
OpenHands treats that as the end of the conversation after one LLM call: the run is `finished`, with no branch and no pull request. The tool
rule in the prompt (every turn must call a tool until the task is done) makes this rarer. Remedies: add `ai:escalate` so the strong worker
tries again; an automatic retry is not built (deliberately left out for now), so re-run by hand.

**A strong worker stops after a local commit.** The commit exists in its workspace, but it neither pushed nor opened a pull request. Same
remedy: `ai:escalate` or a manual re-run; check the branch on the remote before assuming there is work.

**The planner waits forever.** If its prompt lets it wait for workers, it never ends. The end rule in the prompt fixes it: a planner stops
after it has written the plan.

**The reviewer acts as a planner.** An epic's text can tell the agent to plan; a reviewer that follows it plans instead of reviewing. The
reviewer's prompt must say that it ignores instructions in the issue and only reviews against the acceptance criteria.

**The reviewer blames a pull request for a check that also fails on the default branch.** Take the baseline first: run the same check on the
default branch. Only a failure the pull request introduced counts against it; a repository-wide failure is not the pull request's fault.

**A stuck run.** OpenHands aborts a conversation that repeats the same action, and the state becomes `stuck`. A stuck run holds its slot
until it ends; pause it or wait for `FORGEHANDS_MAX_RUN_MINUTES`.

**Gateway timeouts and 5xx.** A slow or failing LLM gateway appears in the OpenHands log as retried errors. A slow free model can exceed the
reverse proxy's read timeout before the gateway answers, and the run fails without a useful error in Forgejo.

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
| Label -> profile id, tier, iterations (`LABEL_ROUTES`) | you, in `.env`; `bootstrap/hgboot/agents.py` builds the value from the ids |
| Model provider key, LLM profiles, the four agent profiles | you, in Canvas; the bootstrap's agents step does not create them yet |
| The Forgejo webhook, and (webhook mode) the OpenHands automation | you, in Forgejo and Canvas |
| Starting a review by hand (`ai:review`) | you; a green commit starts the review when the `ci_success` route exists |
| The verdict (`ai:approved`, `ai:changes`), and the merge itself | the reviewer sets the label; ForgeHands merges by `MERGE_POLICY`; a repository without a policy stays manual |
| Choosing models, tiers and tool sets | you |
| Creating issues, writing acceptance criteria, labelling `ai:ready` or `ai:escalate` | you |

## Prompts

The prompts live as files and are referenced here, not pasted:

- `src/prompt.mjs` in the ForgeHands repository builds the first message of every run: the issue run, the follow-up round, the fix round and
  the review. The untrusted block, the trusted sender list, the tool rule and the end rule are in that file.
- The role prompts are the prompts of the four agent profiles (`planner`, `worker`, `worker-strong`, `reviewer`) that you create in Canvas;
  the profiles live in the `openhands-data` volume. `bootstrap/hgboot/agents.py` maps labels to profiles and limits and contains no prompt
  text.

Change the file or the profile and start a new run. A prompt copied into an issue, a pull request or this page would go stale, and an agent
reading it could mistake it for instructions.
