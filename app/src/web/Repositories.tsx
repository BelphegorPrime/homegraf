import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { RepoFactoryStatus, RepoInfo } from "../shared/types";
import { useRepos } from "./api";
import { PageHeader, Section } from "./Page";

/** What keeps a repository out of the factory, in the order the wiring is usually done. */
const CHECKS: [keyof RepoFactoryStatus, string][] = [
  ["webhook", "webhook"],
  ["labels", "labels"],
  ["agentAccess", "agent access"],
  ["allowed", "not in ForgeHands' allowed list"],
];

/** How far a repository is wired: the checks that are false, else unknown when Forgejo could not say. */
export function factoryState(f: RepoFactoryStatus): { state: "ready" | "missing" | "unknown"; missing: string[] } {
  const missing = CHECKS.filter(([check]) => f[check] === false).map(([, what]) => what);
  if (missing.length) return { state: "missing", missing };
  const unknown = CHECKS.some(([check]) => f[check] === "unknown");
  return { state: unknown ? "unknown" : "ready", missing };
}

const linkable = (url: string) => url.startsWith("http://") || url.startsWith("https://");

/** Forgejo's `updated_at` as a day, or a dash when it is missing or unreadable. */
function lastUpdate(updated: string): string {
  if (!updated) return "—";
  const date = new Date(updated);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleDateString();
}

function FactoryCell({ factory }: { factory: RepoFactoryStatus }) {
  const { state, missing } = factoryState(factory);
  if (state === "ready")
    return (
      <Badge variant="outline" className="border-ok text-ok">
        ready for factory
      </Badge>
    );
  if (state === "missing")
    return (
      <Badge variant="outline" className="border-warn text-warn">
        missing: {missing.join(", ")}
      </Badge>
    );
  return (
    <Badge variant="outline" className="text-muted-foreground" title="Forgejo could not be read, so the factory wiring is unknown.">
      unknown
    </Badge>
  );
}

/** The repositories of the operator's Forgejo owner, each with how far its factory wiring is. */
export function RepositoriesView({ repos, error, loading, onNew }: { repos: RepoInfo[]; error?: string | null; loading?: boolean; onNew?: () => void }) {
  if (loading) return <Skeleton className="h-24 w-full max-w-4xl" />;
  if (error) return <p className="warning">{error}</p>;
  const rows = [...repos].sort((a, b) => a.name.localeCompare(b.name));
  return (
    <>
      {onNew && (
        <div className="mb-3">
          <Button type="button" size="sm" variant="outline" onClick={onNew}>
            New repository
          </Button>
        </div>
      )}
      {rows.length === 0 ? (
        <p className="muted m-0">No repositories yet.</p>
      ) : (
        <table className="w-full text-left text-sm">
          <thead className="muted">
            <tr>
              <th className="pb-1 pr-4 font-normal">Name</th>
              <th className="pb-1 pr-4 font-normal">Visibility</th>
              <th className="pb-1 pr-4 font-normal">Default branch</th>
              <th className="pb-1 pr-4 font-normal">Last update</th>
              <th className="pb-1 font-normal">Factory</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((repo) => (
              <tr key={repo.name} className="border-b align-top">
                <td className="py-2 pr-4">
                  {linkable(repo.url) ? (
                    <a href={repo.url} target="_blank" rel="noopener noreferrer">
                      {repo.name}
                    </a>
                  ) : (
                    repo.name
                  )}
                </td>
                <td className="py-2 pr-4">
                  <Badge variant="outline" className="text-muted-foreground">
                    {repo.private ? "private" : "public"}
                  </Badge>
                </td>
                <td className="py-2 pr-4">{repo.default_branch}</td>
                <td className="py-2 pr-4">{lastUpdate(repo.updated)}</td>
                <td className="py-2">
                  <FactoryCell factory={repo.factory} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

export function Repositories({ onNew }: { onNew?: () => void }) {
  const repos = useRepos();
  return (
    <div className="list">
      <PageHeader title="Repositories">repositories of operator's Forgejo owner and how far each is wired for factory.</PageHeader>
      <Section title="Repositories">
        <RepositoriesView repos={repos.data?.repos ?? []} error={repos.isError ? repos.error.message : undefined} loading={repos.isPending} onNew={onNew} />
      </Section>
    </div>
  );
}
