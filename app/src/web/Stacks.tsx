import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import type { ManagedStack, SecretKey } from "../shared/types";
import { signedIn, startJob, useCapabilities, useStacks, type Session } from "./api";
import { SignIn, type Me } from "./Users";
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
      <Button type="button" variant={hint ? "default" : "outline"} size="sm" disabled={start.isPending} onClick={() => { setError(null); start.mutate(); }}>
        Redeploy {stack.komodoStack}
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
    onSuccess: (_r, _v, ctx) => { setValue(""); setError(null); void ctx; },
    onError: (e) => setError(message(e)),
  });
  const act = (what: string, job: () => Promise<unknown>) => run.mutate(async () => { await job(); changed(`${secret.key} ${what}`); });
  return (
    <tr className="border-b align-top">
      <td className="py-2 pr-4">{secret.key} {!secret.used && <Badge variant="outline" className="border-warn text-warn" title="No compose file of the stack uses ${KEY}">unused</Badge>}</td>
      <td className="py-2">
        <div className="flex flex-wrap items-center gap-2">
          <Input type="password" autoComplete="new-password" className="w-64" placeholder="New value" aria-label={`New value for ${secret.key}`} value={value} onChange={(e) => setValue(e.target.value)} />
          <Button type="button" variant="outline" size="sm" disabled={!value || run.isPending} onClick={() => act("changed", () => signedIn(session, "PUT", url, { value }))}>Set</Button>
          <Button type="button" variant="outline" size="sm" disabled={run.isPending} title="A random value that nobody sees"
                  onClick={() => confirm(`Replace ${secret.key} with a generated value?`) && act("generated", () => signedIn(session, "PUT", url, { generate: true }))}>Generate</Button>
          <Button type="button" variant="destructive" size="sm" disabled={run.isPending}
                  onClick={() => confirm(`Delete ${secret.key}?`) && act("deleted", () => signedIn(session, "DELETE", url))}>Delete</Button>
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
    mutationFn: (generate: boolean) => signedIn(session, "PUT", `/api/stacks/${encodeURIComponent(stack.id)}/secrets/${encodeURIComponent(key.trim())}`, generate ? { generate: true } : { value }),
    onSuccess: () => { changed(`${key.trim()} added`); setKey(""); setValue(""); setError(null); },
    onError: (e) => setError(message(e)),
  });
  return (
    <form className="mt-4 max-w-xl" onSubmit={(e) => { e.preventDefault(); add.mutate(false); }}>
      <h2>Add a secret</h2>
      <div className="grid gap-2">
        <Input autoComplete="off" placeholder="NAME, like an environment variable" aria-label="Secret name" value={key} onChange={(e) => setKey(e.target.value)} />
        <Input type="password" autoComplete="new-password" placeholder="Value" aria-label="Value of the new secret" value={value} onChange={(e) => setValue(e.target.value)} />
      </div>
      <div className="actions">
        <Button type="submit" disabled={!key.trim() || !value || add.isPending}>Add</Button>
        <Button type="button" variant="outline" disabled={!key.trim() || add.isPending} onClick={() => add.mutate(true)}>Add with a generated value</Button>
      </div>
      {error && <p className="warning !px-0">{error}</p>}
    </form>
  );
}

function Secrets({ stack, session, changed }: { stack: ManagedStack; session: Session; changed: (what: string) => void }) {
  const client = useQueryClient();
  const q = useQuery({ queryKey: ["secrets", stack.id, session.name], queryFn: () => signedIn<{ keys: SecretKey[] }>(session, "GET", `/api/stacks/${encodeURIComponent(stack.id)}/secrets`), retry: false });
  const done = (what: string) => { changed(what); void client.invalidateQueries({ queryKey: ["secrets", stack.id] }); };
  return (
    <>
      <h2>Secrets</h2>
      <p className="muted max-w-3xl">Stored in Infisical, project <code>stacks-{stack.server}</code>, folder <code>/{stack.id}</code>. The page shows names only; values are never read back.</p>
      {q.isError && <p className="warning !px-0">{message(q.error)}</p>}
      {q.data && (q.data.keys.length === 0 ? <p className="muted">No secrets yet.</p> : (
        <table className="w-full max-w-4xl text-left text-sm">
          <tbody>{q.data.keys.map((k) => <SecretRow key={k.key} stack={stack} secret={k} session={session} changed={done} />)}</tbody>
        </table>
      ))}
      {q.data && <AddSecret stack={stack} session={session} changed={done} />}
    </>
  );
}

export function Stacks({ session, onSession }: { session: Session | null; onSession: (s: Session | null) => void }) {
  const caps = useCapabilities().data;
  const stacks = useStacks();
  const { job, follow } = useJob();
  const [stackId, setStackId] = useState("");
  const [changes, setChanges] = useState<string[]>([]);
  const stack = stacks.data?.find((s) => s.id === stackId);
  const me = useQuery({ queryKey: ["me", session?.name], enabled: !!session, retry: false, queryFn: () => signedIn<Me>(session!, "GET", "/api/me") });

  if (caps && !caps.create.enabled) return <div className="list"><p className="muted">Managing stacks from here is off{caps.create.reason ? ` (${caps.create.reason})` : ""}.</p></div>;
  if (!session || !me.data) {
    return (
      <div className="list">
        <p className="muted max-w-3xl">Redeploy a stack and manage its secrets. Needs the permissions <code>stacks:deploy</code> and <code>secrets:manage</code>.</p>
        {me.isError && <p className="warning !px-0">{message(me.error)}</p>}
        <SignIn onSignedIn={(s) => onSession(s)} />
      </div>
    );
  }
  const can = (p: string) => me.data.permissions.includes(p as never);
  const startJobLog = (jobId: string) => follow(jobId, () => undefined);
  return (
    <div className="list">
      <p>Signed in as <strong>{me.data.name}</strong>. <Button type="button" variant="ghost" size="sm" onClick={() => { onSession(null); setChanges([]); }}>Sign out</Button></p>
      {stacks.isError && <p className="warning !px-0">{stacks.error.message}</p>}
      <div className="max-w-xl">
        <Select aria-label="Stack" className="w-full" value={stackId} onValueChange={(v) => { setStackId(v); setChanges([]); }}
                options={[{ value: "", label: "Choose a stack…" }, ...(stacks.data ?? []).map((s) => ({ value: s.id, label: `${s.id} (${s.server})` }))]} />
      </div>
      <p className="muted max-w-3xl">The platform stacks (secrets store, reverse proxy, this UI, Komodo's own) are not listed.</p>
      {stack && (
        <>
          <h2>Redeploy</h2>
          {can("stacks:deploy")
            ? <Redeploy stack={stack} session={session} onStart={startJobLog} hint={changes.length ? `${changes.join(", ")}: takes effect with the next deploy` : undefined} />
            : <p className="muted">You may not redeploy (needs <code>stacks:deploy</code>).</p>}
          {job && (
            <div className="job">
              <h2>Log {job.done ? <Badge variant="outline" className={job.done.state === "done" ? "border-ok text-ok" : "border-down text-down"}>{job.done.state === "done" ? "deployed" : `failed (exit ${job.done.exitCode})`}</Badge> : <span className="muted">running…</span>}</h2>
              <pre>{job.log}</pre>
            </div>
          )}
          {stack.secrets
            ? (can("secrets:manage") ? <Secrets stack={stack} session={session} changed={(what) => setChanges((c) => [...c, what])} /> : <p className="muted">You may not manage secrets (needs <code>secrets:manage</code>).</p>)
            : <p className="muted">{stack.id} keeps no secrets in Infisical.</p>}
        </>
      )}
    </div>
  );
}
