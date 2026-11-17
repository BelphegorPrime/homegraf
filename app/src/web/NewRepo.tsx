import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { RepoInfo, RepoJobSnapshot, RepoProfileInfo } from "../shared/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { rememberUser, startJob, useRepoProfiles, useRepos } from "./api";
import { CredentialFields, useCredentials, type Credentials } from "./Credentials";
import { PageHeader, Section } from "./Page";

/** The values of the create-repository form. */
export interface RepoFields {
  name: string;
  profile: string;
  description: string;
  visibility: "private" | "public";
  branch: string;
}

/** The body of `POST /api/repos`, as the server reads it. */
export interface RepoBody {
  name: string;
  profile: string;
  description: string;
  visibility: "private" | "public";
  branch?: string;
}

// The server's rules (app/src/server/repos.ts).
const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/;
const BRANCH = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$/;

/** Field → message; an empty object means the form may be sent. Mirrors the server's checks on the trimmed values. */
export function validateRepo(f: RepoFields): Record<string, string> {
  const errors: Record<string, string> = {};
  const name = f.name.trim();
  if (!name) errors.name = "Enter a name.";
  else if (!NAME.test(name)) errors.name = "letters, digits, . _ - (up to 100 characters, not starting with . or -)";
  else if (name.includes("..")) errors.name = "the name must not contain ..";

  const profile = f.profile.trim();
  if (!profile) errors.profile = "Choose a profile.";
  else if (profile.length > 64) errors.profile = "at most 64 characters";

  const description = f.description.trim();
  if (!description) errors.description = "Enter a description.";
  else if (description.length > 255) errors.description = "at most 255 characters";

  const branch = f.branch.trim();
  if (branch) {
    if (branch.length > 255) errors.branch = "at most 255 characters";
    else if (!BRANCH.test(branch) || branch.includes("..") || branch.includes("//") || branch.endsWith("/") || branch.endsWith("."))
      errors.branch = "a branch name without .., //, or a trailing / or .";
  }
  return errors;
}

/** The request body for a valid form: trimmed, the branch left out when empty, private unless chosen otherwise. */
export function repoBody(f: RepoFields): RepoBody {
  const body: RepoBody = {
    name: f.name.trim(),
    profile: f.profile.trim(),
    description: f.description.trim(),
    visibility: f.visibility === "public" ? "public" : "private",
  };
  const branch = f.branch.trim();
  if (branch) body.branch = branch;
  return body;
}

/** The job as `NewRepo` follows it: its output so far, and its result once it is through. */
export interface RepoJob {
  log: string;
  done?: RepoJobSnapshot;
}

const linkable = (url: string) => url.startsWith("http://") || url.startsWith("https://");

/** The repository a job created, by its name or the `owner/`-prefixed name Forgejo may list. */
export function repoForJob(repos: RepoInfo[], name: string): RepoInfo | undefined {
  return repos.find((repo) => repo.name === name || repo.name.endsWith(`/${name}`));
}

/** A finished repository job: its badge and log, then the result and the wiring step it still needs. */
export function RepoProgress({ job, repo }: { job: RepoJob; repo?: RepoInfo }) {
  const done = job.done;
  const state = done?.state === "failed" ? "failed" : done?.state === "done" ? "done" : "running";
  const badge = {
    running: { text: "creating", cls: "border-warn text-warn" },
    failed: { text: "stopped", cls: "border-down text-down" },
    done: { text: "created", cls: "border-ok text-ok" },
  }[state];
  const allowed = repo?.factory.allowed;
  return (
    <div className="job">
      <Badge variant="outline" className={badge.cls}>
        {badge.text}
      </Badge>
      <pre>{job.log}</pre>
      {state === "failed" && (
        <p className="warning">The repository was not created: the command stopped with exit code {done?.exitCode ?? "unknown"}. The form can be sent again.</p>
      )}
      {state === "done" && (
        <>
          <p>
            {repo && linkable(repo.url) ? (
              <>
                Repository{" "}
                <a href={repo.url} target="_blank" rel="noreferrer">
                  {repo.name}
                </a>{" "}
                is created.
              </>
            ) : (
              "The repository is created."
            )}
          </p>
          <p className="muted">The first tickets for the repository are written by the operator; the factory only works issues that exist.</p>
          {allowed === false && (
            <p className="warning">
              ForgeHands' allowed repositories still need a pull request, made by the <code>--wire</code> option of the operator command (
              <code>new-repo ... --wire</code>): it adds the repository to <code>ALLOWED_REPOS</code> and <code>MERGE_POLICY</code> of the stack that runs
              ForgeHands, as a pull request; nothing is deployed.
            </p>
          )}
          {allowed === "unknown" && <p className="muted">Whether ForgeHands may work this repository could not be read.</p>}
        </>
      )}
    </div>
  );
}

/** The form itself, without state: profiles come from the server, the messages from `validateRepo`. */
export function NewRepoView({
  profiles,
  error,
  loading,
  fields,
  errors,
  onChange,
  onSubmit,
  credentials,
  busy,
}: {
  profiles: RepoProfileInfo[];
  error?: string | null;
  loading?: boolean;
  fields: RepoFields;
  errors: Record<string, string>;
  onChange: <K extends keyof RepoFields>(field: K, value: RepoFields[K]) => void;
  onSubmit: () => void;
  credentials?: Credentials;
  busy?: boolean;
}) {
  const selected = profiles.find((p) => p.name === fields.profile);
  return (
    <form
      className="flex max-w-xl flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
    >
      {loading && <p className="muted m-0">Loading profiles…</p>}
      {error && <p className="warning">{error}</p>}

      <label className="flex flex-col gap-1">
        <span className="text-sm font-medium">Name</span>
        <Input value={fields.name} placeholder="example-repo" aria-label="Name" onChange={(e) => onChange("name", e.target.value)} />
        <span className="muted text-xs">Letters, digits, . _ - (up to 100 characters).</span>
        {errors.name && <small className="error">{errors.name}</small>}
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-sm font-medium">Profile</span>
        <select
          className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm"
          aria-label="Profile"
          value={fields.profile}
          onChange={(e) => onChange("profile", e.target.value)}
        >
          {profiles.length === 0 && <option value="">No profiles</option>}
          {profiles.map((p) => (
            <option key={p.name} value={p.name}>
              {p.name}
            </option>
          ))}
        </select>
        {selected && (
          <span className="muted text-xs">
            {selected.description} <code>{selected.validate_command}</code>
          </span>
        )}
        {errors.profile && <small className="error">{errors.profile}</small>}
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-sm font-medium">Description</span>
        <Input
          value={fields.description}
          placeholder="What the repository is for"
          aria-label="Description"
          onChange={(e) => onChange("description", e.target.value)}
        />
        {errors.description && <small className="error">{errors.description}</small>}
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-sm font-medium">Visibility</span>
        <select
          className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm"
          aria-label="Visibility"
          value={fields.visibility}
          onChange={(e) => onChange("visibility", e.target.value as RepoFields["visibility"])}
        >
          <option value="private">private</option>
          <option value="public">public</option>
        </select>
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-sm font-medium">Default branch</span>
        <Input value={fields.branch} placeholder="main" aria-label="Default branch" onChange={(e) => onChange("branch", e.target.value)} />
        <span className="muted text-xs">Leave empty for the tool's default.</span>
        {errors.branch && <small className="error">{errors.branch}</small>}
      </label>

      <div className="flex flex-wrap items-center gap-2">
        {credentials && <CredentialFields c={credentials} />}
        <Button type="submit" disabled={busy || (!!credentials && !credentials.ready)}>
          Create repository
        </Button>
      </div>
    </form>
  );
}

/** Create a repository from a profile: the form, validated here, sent as one job and followed to its result. */
export function NewRepo({ onBack }: { onBack?: () => void }) {
  const profiles = useRepoProfiles();
  const repos = useRepos();
  const cred = useCredentials();
  const client = useQueryClient();
  const [draft, setDraft] = useState<RepoFields>({ name: "", profile: "", description: "", visibility: "private", branch: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [job, setJob] = useState<RepoJob | null>(null);
  const [sending, setSending] = useState(false);
  const source = useRef<EventSource | null>(null);
  useEffect(() => () => source.current?.close(), []);
  const list = profiles.data ?? [];
  // Until the person chooses one, the first profile of the server is the one the form shows and sends.
  const fields: RepoFields = { ...draft, profile: draft.profile || (list[0]?.name ?? "") };
  const change = <K extends keyof RepoFields>(field: K, value: RepoFields[K]) => setDraft((f) => ({ ...f, [field]: value }));
  const busy = sending || (!!job && !job.done);
  const created = job?.done?.state === "done" ? repoForJob(repos.data?.repos ?? [], job.done.repository) : undefined;

  function follow(jobId: string) {
    source.current?.close();
    setJob({ log: "" });
    const es = new EventSource(`/api/repos/jobs/${jobId}/events`);
    source.current = es;
    es.addEventListener("log", (ev) => setJob((j) => ({ ...j, log: (j?.log ?? "") + JSON.parse((ev as MessageEvent).data) })));
    es.addEventListener("done", (ev) => {
      setJob((j) => ({ log: j?.log ?? "", done: JSON.parse((ev as MessageEvent).data) }));
      es.close();
      void client.invalidateQueries({ queryKey: ["repos"] });
    });
  }

  async function submit() {
    const problems = validateRepo(fields);
    setErrors(problems);
    if (Object.keys(problems).length > 0) return;
    setFailure(null);
    const session = cred.session();
    cred.spent(); // typed again for every action
    setSending(true);
    try {
      const id = await startJob("/api/repos", repoBody(fields), session);
      if (!session.sso) rememberUser(session.name);
      follow(id);
    } catch (e) {
      setFailure(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="list">
      <PageHeader title="New repository">
        {"Creates a repository from a profile and follows the job; the factory wiring that still needs a pull request is shown after it."}
      </PageHeader>
      <Section
        title="Details"
        action={
          onBack && (
            <Button type="button" size="sm" variant="outline" onClick={onBack}>
              Back to repositories
            </Button>
          )
        }
      >
        <NewRepoView
          profiles={list}
          error={profiles.isError ? profiles.error.message : undefined}
          loading={profiles.isPending}
          fields={fields}
          errors={errors}
          onChange={change}
          onSubmit={() => void submit()}
          credentials={cred}
          busy={busy}
        />
        {failure && <p className="warning">{failure}</p>}
        {job && <RepoProgress job={job} repo={created} />}
      </Section>
    </div>
  );
}
