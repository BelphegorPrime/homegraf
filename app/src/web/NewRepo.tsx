import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { RepoProfileInfo } from "../shared/types";
import { rememberUser, startJob, useRepoProfiles } from "./api";
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
}: {
  profiles: RepoProfileInfo[];
  error?: string | null;
  loading?: boolean;
  fields: RepoFields;
  errors: Record<string, string>;
  onChange: <K extends keyof RepoFields>(field: K, value: RepoFields[K]) => void;
  onSubmit: () => void;
  credentials?: Credentials;
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
        <Button type="submit" disabled={!!credentials && !credentials.ready}>
          Create repository
        </Button>
      </div>
    </form>
  );
}

/** Create a repository from a profile: the form, validated here, sent as one job. */
export function NewRepo({ onBack }: { onBack?: () => void }) {
  const profiles = useRepoProfiles();
  const cred = useCredentials();
  const [draft, setDraft] = useState<RepoFields>({ name: "", profile: "", description: "", visibility: "private", branch: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const list = profiles.data ?? [];
  // Until the person chooses one, the first profile of the server is the one the form shows and sends.
  const fields: RepoFields = { ...draft, profile: draft.profile || (list[0]?.name ?? "") };
  const change = <K extends keyof RepoFields>(field: K, value: RepoFields[K]) => setDraft((f) => ({ ...f, [field]: value }));

  async function submit() {
    const problems = validateRepo(fields);
    setErrors(problems);
    if (Object.keys(problems).length > 0) return;
    setFailure(null);
    const session = cred.session();
    cred.spent(); // typed again for every action
    try {
      const id = await startJob("/api/repos", repoBody(fields), session);
      if (!session.sso) rememberUser(session.name);
      setJobId(id);
    } catch (e) {
      setFailure(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="list">
      <PageHeader title="New repository">{"Creates a repository from a profile; the wiring of the factory follows on its own."}</PageHeader>
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
        />
        {failure && <p className="warning">{failure}</p>}
        {jobId && <p className="muted">Job {jobId} started.</p>}
      </Section>
    </div>
  );
}
