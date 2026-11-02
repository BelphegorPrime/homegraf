import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import type { PendingRemoval } from "../shared/types";
import { useJob } from "./useJob";
import { CredentialFields, useCredentials } from "./Credentials";
import { rememberUser, startJob, useCapabilities, useRemovable, useRemovals } from "./api";

const STATE: Record<PendingRemoval["pr"], { text: string; cls: string }> = {
  open: { text: "waiting for the merge", cls: "border-warn text-warn" },
  merged: { text: "merged", cls: "border-ok text-ok" },
  closed: { text: "closed without merge", cls: "border-down text-down" },
  unknown: { text: "state unknown", cls: "text-muted-foreground" },
};

export function RemoveApp() {
  const caps = useCapabilities().data;
  const apps = useRemovable();
  const removals = useRemovals();
  const client = useQueryClient();
  const { job, follow } = useJob();
  const [appId, setAppId] = useState("");
  const [confirm, setConfirm] = useState("");
  const cred = useCredentials();
  const [error, setError] = useState<string | null>(null);
  const running = !!job && !job.done;
  const app = apps.data?.find((a) => a.id === appId);

  const refresh = () => void Promise.all([client.invalidateQueries({ queryKey: ["removals"] }), client.invalidateQueries({ queryKey: ["removable"] }), client.invalidateQueries({ queryKey: ["graph"] })]);

  async function act(path: string, body: unknown) {
    setError(null);
    const session = cred.session();
    cred.spent(); // typed again for every action
    try {
      const jobId = await startJob(path, body, session);
      if (!session.sso) rememberUser(session.name);
      follow(jobId, refresh);
      setConfirm("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (caps && !caps.create.enabled) return <div className="list"><p className="muted">Removing from here is off{caps.create.reason ? ` (${caps.create.reason})` : ""}.</p></div>;

  const credentials = <CredentialFields c={cred} />;

  return (
    <div className="list">
      <p className="muted max-w-3xl">
        A removal has two halves, because routing and secrets may only go once the main branch no longer declares the app.
        First half: the Komodo stack is destroyed <strong>at once</strong> (the app is down) and a pull request removes its files.
        Second half, after the merge: the NPM host, the certificate and the Infisical folder. Data on the server is never touched.
        Needs the permission <code>apps:remove</code>.
      </p>
      {error && <p className="warning !px-0">{error}</p>}

      <h2>Waiting for the second half</h2>
      {removals.data?.problem && <p className="warning !px-0">{removals.data.problem}</p>}
      {!removals.data?.removals.length ? <p className="muted">Nothing is waiting.</p> : (
        <>
          {!cred.sso && <p className="muted">User and password are typed again for every action.</p>}
          {credentials}
          <table className="mt-3 w-full max-w-4xl text-left text-sm">
            <thead className="muted"><tr><th className="pb-1 pr-4 font-normal">App</th><th className="pb-1 pr-4 font-normal">Pull request</th><th className="pb-1 pr-4 font-normal">State</th><th /></tr></thead>
            <tbody>
              {removals.data.removals.map((r) => (
                <tr key={r.appId} className="border-b align-top">
                  <td className="py-2 pr-4">{r.appId}<div className="muted text-xs">{r.server}{r.requestedBy ? ` · by ${r.requestedBy}` : ""}</div></td>
                  <td className="py-2 pr-4"><a href={r.pullRequestUrl} target="_blank" rel="noreferrer">Open</a></td>
                  <td className="py-2 pr-4"><Badge variant="outline" className={STATE[r.pr].cls}>{STATE[r.pr].text}</Badge></td>
                  <td className="py-2">
                    <Button type="button" size="sm" disabled={r.pr !== "merged" || !cred.ready || running}
                            onClick={() => void act("/api/apps/remove/finish", { appId: r.appId })}>Finish removal</Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <h2>Remove an app</h2>
      {apps.isError && <p className="warning !px-0">{apps.error.message}</p>}
      <div className="max-w-xl">
        <NativeSelect aria-label="App" value={appId} onChange={(e) => { setAppId(e.target.value); setConfirm(""); }}>
          <option value="">Choose an app…</option>
          {apps.data?.map((a) => <option key={a.id} value={a.id}>{a.id} ({a.server})</option>)}
        </NativeSelect>
      </div>
      {app && (
        <form className="mt-3 max-w-3xl" onSubmit={(e) => { e.preventDefault(); void act("/api/apps/remove", { appId: app.id, confirm }); }}>
          <ul className="list-disc pl-5">
            <li>Komodo stack <strong>{app.komodoStack}</strong> on {app.server}: containers down, stack deleted. <strong>The app stops now.</strong></li>
            <li>Pull request: removes <code>stacks/{app.id}/</code> and its inventory entry.</li>
            <li>After the merge: {app.domains.length ? <>NPM host and certificate for {app.domains.join(", ")}</> : "routing, if ingressctl manages one"}{app.secrets ? "; the Infisical folder with its secrets" : ""}.</li>
            <li className="muted">Kept: {app.dataPaths.length ? `data in ${app.dataPaths.join(", ")}` : "no data paths found"}{app.domains.length ? `; the DNS rewrite for ${app.domains.join(", ")} in AdGuard` : ""}.</li>
          </ul>
          <div className="mt-3 grid max-w-sm gap-2">
            <Input autoComplete="off" placeholder={`Type ${app.id} to confirm`} aria-label="Confirmation" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
            {credentials}
          </div>
          <div className="actions">
            <Button type="submit" variant="destructive" disabled={confirm !== app.id || !cred.ready || running}>Destroy the stack and open the pull request</Button>
          </div>
        </form>
      )}

      {job && (
        <div className="job">
          <h2>Log {job.done ? <Badge variant="outline" className={job.done.state === "done" ? "border-ok text-ok" : "border-down text-down"}>{job.done.state === "done" ? "done" : `failed (exit ${job.done.exitCode})`}</Badge> : <span className="muted">running…</span>}</h2>
          <pre>{job.log}</pre>
          {job.done?.pullRequestUrl && <p><a href={job.done.pullRequestUrl} target="_blank" rel="noreferrer">Open the pull request</a>, merge it, then finish the removal here.</p>}
        </div>
      )}
    </div>
  );
}
