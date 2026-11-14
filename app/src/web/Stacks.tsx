import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ManagedStack, SecretKey } from "../shared/types";
import { signedIn, startJob, useCapabilities, useStacks, type Session } from "./api";
import type { Me } from "./Users";
import { PageHeader, Section } from "./Page";
import { useJob } from "./useJob";

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** A change takes effect with the next deploy: Komodo does not notice a changed secret. */
function Redeploy({ stack, session, hint, onStart }: { stack: ManagedStack; session: Session; hint?: string; onStart: (jobId: string) => void }) {
  const [error, setError] = useState<string | null>(null);
  const start = useMutation({
    mutationFn: () => startJob(`/api/stacks/${encodeURIComponent(stack.id)}/deploy`, {}, session),
    onSuccess: onStart,
    onError: (e) => setError(message(e)),
  });
  return (
    <span>
      <Button
        type="button"
        variant={hint ? "default" : "outline"}
        size="sm"
        disabled={start.isPending}
        onClick={() => {
          setError(null);
          start.mutate();
        }}
      >
        Redeploy
      </Button>
      {hint && <span className="muted ml-2">{hint}</span>}
      {error && <span className="warning !px-2">{error}</span>}
    </span>
  );
}

function SecretRow({ stack, secret, session, changed }: { stack: ManagedStack; secret: SecretKey; session: Session; changed: (what: string) => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const url = `/api/stacks/${encodeURIComponent(stack.id)}/secrets/${encodeURIComponent(secret.key)}`;
  const run = useMutation({
    mutationFn: (job: () => Promise<unknown>) => job(),
    onSuccess: (_r, _v, ctx) => {
      setValue("");
      setError(null);
      void ctx;
    },
    onError: (e) => setError(message(e)),
  });
  const act = (what: string, job: () => Promise<unknown>) =>
    run.mutate(async () => {
      await job();
      changed(`${secret.key} ${what}`);
    });
  return (
    <tr className="border-b align-top">
      <td className="py-2 pr-4">
        {secret.key}{" "}
        {!secret.used && (
          <Badge variant="outline" className="border-warn text-warn" title="No compose file of the stack uses ${KEY}">
            unused
          </Badge>
        )}
      </td>
      <td className="py-2">
        <div className="flex flex-wrap items-center gap-2">
          <Input
            type="password"
            autoComplete="new-password"
            className="w-64"
            placeholder="New value"
            aria-label={`New value for ${secret.key}`}
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={!value || run.isPending}
            onClick={() => act("changed", () => signedIn(session, "PUT", url, { value }))}
          >
            Set
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={run.isPending}
            title="A random value that nobody sees"
            onClick={() =>
              confirm(`Replace ${secret.key} with a generated value?`) && act("generated", () => signedIn(session, "PUT", url, { generate: true }))
            }
          >
            Generate
          </Button>
          <Button
            type="button"
            variant="destructive"
            size="sm"
            disabled={run.isPending}
            onClick={() => confirm(`Delete ${secret.key}?`) && act("deleted", () => signedIn(session, "DELETE", url))}
          >
            Delete
          </Button>
        </div>
        {error && <div className="warning !px-0">{error}</div>}
      </td>
    </tr>
  );
}

function AddSecret({ stack, session, changed }: { stack: ManagedStack; session: Session; changed: (what: string) => void }) {
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const add = useMutation({
    mutationFn: (generate: boolean) =>
      signedIn(
        session,
        "PUT",
        `/api/stacks/${encodeURIComponent(stack.id)}/secrets/${encodeURIComponent(key.trim())}`,
        generate ? { generate: true } : { value },
      ),
    onSuccess: () => {
      changed(`${key.trim()} added`);
      setKey("");
      setValue("");
      setError(null);
    },
    onError: (e) => setError(message(e)),
  });
  return (
    <form
      className="mt-5 max-w-xl border-t pt-4"
      onSubmit={(e) => {
        e.preventDefault();
        add.mutate(false);
      }}
    >
      <p className="m-0 mb-2 text-sm font-medium">Add a secret</p>
      <div className="grid gap-2">
        <Input
          autoComplete="off"
          placeholder="NAME, like an environment variable"
          aria-label="Secret name"
          value={key}
          onChange={(e) => setKey(e.target.value)}
        />
        <Input
          type="password"
          autoComplete="new-password"
          placeholder="Value"
          aria-label="Value of the new secret"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
      </div>
      <div className="actions !mb-0 !mt-3">
        <Button type="submit" disabled={!key.trim() || !value || add.isPending}>
          Add
        </Button>
        <Button type="button" variant="outline" disabled={!key.trim() || add.isPending} onClick={() => add.mutate(true)}>
          Add with a generated value
        </Button>
      </div>
      {error && <p className="warning !px-0">{error}</p>}
    </form>
  );
}

function Secrets({ stack, session, changed }: { stack: ManagedStack; session: Session; changed: (what: string) => void }) {
  const client = useQueryClient();
  const q = useQuery({
    queryKey: ["secrets", stack.id, session.name],
    queryFn: () => signedIn<{ keys: SecretKey[] }>(session, "GET", `/api/stacks/${encodeURIComponent(stack.id)}/secrets`),
    retry: false,
  });
  const done = (what: string) => {
    changed(what);
    void client.invalidateQueries({ queryKey: ["secrets", stack.id] });
  };
  return (
    <Section
      title="Secrets"
      description={
        <>
          Passwords and keys the stack's compose files read as <code>${"{NAME}"}</code>. Kept in Infisical (project <code>stacks-{stack.server}</code>, folder{" "}
          <code>/{stack.id}</code>). Only names are shown here; values are never read back.
        </>
      }
    >
      {q.isPending && <p className="muted m-0">Loading…</p>}
      {q.isError && <p className="warning !px-0">{message(q.error)}</p>}
      {q.data &&
        (q.data.keys.length === 0 ? (
          <p className="muted m-0">This stack has no secrets yet.</p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead className="muted">
              <tr>
                <th className="pb-1 pr-4 font-normal">Name</th>
                <th className="pb-1 font-normal">Change</th>
              </tr>
            </thead>
            <tbody>
              {q.data.keys.map((k) => (
                <SecretRow key={k.key} stack={stack} secret={k} session={session} changed={done} />
              ))}
            </tbody>
          </table>
        ))}
      {q.data && <AddSecret stack={stack} session={session} changed={done} />}
    </Section>
  );
}

export function Stacks({ session }: { session: Session }) {
  const caps = useCapabilities().data;
  const stacks = useStacks();
  const { job, follow } = useJob();
  const [stackId, setStackId] = useState("");
  const [changes, setChanges] = useState<string[]>([]);
  const stack = stacks.data?.find((s) => s.id === stackId);
  const me = useQuery({ queryKey: ["me", session.name], retry: false, queryFn: () => signedIn<Me>(session, "GET", "/api/me") });

  if (caps && !caps.create.enabled)
    return (
      <div className="list">
        <PageHeader title="Stacks">Managing stacks from here is off{caps.create.reason ? ` (${caps.create.reason})` : ""}.</PageHeader>
      </div>
    );
  if (!me.data) return <div className="list">{me.isError && <p className="warning !px-0">{message(me.error)}</p>}</div>;
  const can = (p: string) => me.data.permissions.includes(p as never);
  const startJobLog = (jobId: string) => follow(jobId, () => undefined);
  const list = stacks.data ?? [];
  return (
    <div className="list">
      <PageHeader title="Stacks">
        A stack is one app's set of containers, deployed through Komodo. Pick a stack to redeploy it or to manage the secrets it reads. The platform stacks
        (secrets store, reverse proxy, this UI, Komodo itself) are not listed.
      </PageHeader>
      {stacks.isError && <p className="warning !px-0">{stacks.error.message}</p>}
      {stacks.isPending && <p className="muted">Loading stacks…</p>}
      {stacks.data && list.length === 0 && <p className="muted">No stacks to manage yet. Create an app first.</p>}
      <div className="tiles mb-5 max-w-4xl" role="listbox" aria-label="Stacks">
        {list.map((s) => (
          <button
            key={s.id}
            type="button"
            role="option"
            aria-selected={s.id === stackId}
            className={`block cursor-pointer rounded-xl border bg-card px-3 py-2.5 text-left hover:bg-muted/50 ${s.id === stackId ? "ring-2 ring-primary" : ""}`}
            onClick={() => {
              setStackId(s.id);
              setChanges([]);
            }}
          >
            <strong className="block truncate font-medium">{s.id}</strong>
            <span className="muted block text-xs">
              on {s.server} · {s.secrets ? "has secrets" : "no secrets"}
            </span>
          </button>
        ))}
      </div>
      {stack && !can("stacks:deploy") && !can("secrets:manage") && (
        <p className="muted max-w-3xl">
          You are signed in as {me.data.name}, who may neither redeploy stacks nor manage secrets. Ask an administrator for <code>stacks:deploy</code> or{" "}
          <code>secrets:manage</code> (Users page).
        </p>
      )}
      {stack && (can("stacks:deploy") || can("secrets:manage")) && (
        <>
          {can("stacks:deploy") && (
            <Section
              title={`Redeploy ${stack.id}`}
              description={`Pulls the latest images and restarts the Komodo stack ${stack.komodoStack} on ${stack.server}. Needs the permission stacks:deploy.`}
            >
              <Redeploy
                stack={stack}
                session={session}
                onStart={startJobLog}
                hint={changes.length ? `${changes.join(", ")}: takes effect with the next deploy` : undefined}
              />
              {job && (
                <div className="job mt-3">
                  <p className="m-0 mb-1 text-sm font-medium">
                    Log{" "}
                    {job.done ? (
                      <Badge variant="outline" className={job.done.state === "done" ? "border-ok text-ok" : "border-down text-down"}>
                        {job.done.state === "done" ? "deployed" : `failed (exit ${job.done.exitCode})`}
                      </Badge>
                    ) : (
                      <span className="muted">running…</span>
                    )}
                  </p>
                  <pre>{job.log}</pre>
                </div>
              )}
            </Section>
          )}
          {can("secrets:manage") &&
            (stack.secrets ? (
              <Secrets stack={stack} session={session} changed={(what) => setChanges((c) => [...c, what])} />
            ) : (
              <Section title="Secrets" description="Passwords and keys this stack reads.">
                <p className="muted m-0">{stack.id} keeps no secrets in Infisical.</p>
              </Section>
            ))}
        </>
      )}
    </div>
  );
}
