import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useJob } from "./useJob";
import { CredentialFields, useCredentials } from "./Credentials";
import { rememberUser, startJob, useHosts } from "./api";

/** Whether each host redirects HTTP to HTTPS, and a switch: the change is a pull request, the merge sets the NPM host. */
export function HostOptions() {
  const hosts = useHosts();
  const client = useQueryClient();
  const { job, follow } = useJob();
  const cred = useCredentials();
  const [error, setError] = useState<string | null>(null);
  const running = !!job && !job.done;

  async function change(domain: string, enabled: boolean) {
    setError(null);
    const session = cred.session();
    cred.spent(); // typed again for every action
    try {
      const jobId = await startJob("/api/hosts/force-https", { domain, enabled }, session);
      if (!session.sso) rememberUser(session.name);
      follow(jobId, () => void client.invalidateQueries({ queryKey: ["hosts"] }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (hosts.isError) return <p className="muted">Hosts: {hosts.error.message}</p>;
  if (!hosts.data?.length) return null;
  return (
    <section>
      <h2>Redirect HTTP to HTTPS</h2>
      <p className="muted max-w-3xl">
        With the redirect on, a page opened over http:// moves to https:// by itself. Turn it on once every device that uses the host trusts the root certificate above,
        otherwise the page stops loading there. The change is a pull request (needs the permission <code>hosts:edit</code>); after the merge update-all sets the NPM host.
        Hosts made by hand in NPM are changed in NPM.
      </p>
      {error && <p className="warning !px-0">{error}</p>}
      <CredentialFields c={cred} />
      <table className="mt-3 w-full max-w-4xl text-left text-sm">
        <thead className="muted"><tr><th className="pb-1 pr-4 font-normal">Host</th><th className="pb-1 pr-4 font-normal">Redirect</th><th /></tr></thead>
        <tbody>
          {hosts.data.map((h) => (
            <tr key={h.id + h.file} className="border-b align-top">
              <td className="py-2 pr-4">{h.domains.join(", ")}<div className="muted text-xs">{h.file}</div></td>
              <td className="py-2 pr-4"><Badge variant="outline" className={h.forceHttps ? "border-ok text-ok" : "text-muted-foreground"}>{h.forceHttps ? "on" : "off"}</Badge></td>
              <td className="py-2">
                <Button type="button" size="sm" variant="outline" disabled={!cred.ready || running} onClick={() => void change(h.domains[0], !h.forceHttps)}>
                  Turn {h.forceHttps ? "off" : "on"}
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {job && (
        <div className="job">
          <h2>Log {job.done ? <Badge variant="outline" className={job.done.state === "done" ? "border-ok text-ok" : "border-down text-down"}>{job.done.state === "done" ? "pull request open" : `failed (exit ${job.done.exitCode})`}</Badge> : <span className="muted">running…</span>}</h2>
          <pre>{job.log}</pre>
          {job.done?.pullRequestUrl && <p><a href={job.done.pullRequestUrl} target="_blank" rel="noreferrer">Open the pull request</a> and merge it; the redirect changes after the merge.</p>}
        </div>
      )}
    </section>
  );
}
