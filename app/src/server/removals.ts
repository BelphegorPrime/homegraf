import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import type { PendingRemoval } from "../shared/types.js";
import type { Config } from "./config.js";

export type StoredRemoval = Omit<PendingRemoval, "pr">;

/**
 * Records of work that waits for a merge, one per app, in a small JSON file on the volume so a restart of the container
 * does not forget them. `what` names them in messages and is the key of the list in the file.
 */
export function pendingStore<T extends { appId: string }>(file: string, what: string) {
  let items: T[] = [];
  let problem: string | undefined;
  try {
    if (existsSync(file)) items = (JSON.parse(readFileSync(file, "utf8")) as Record<string, T[]>)[what];
    if (!Array.isArray(items)) throw new Error(`no ${what} list`);
  } catch (err) {
    items = [];
    problem = `${file} is unreadable (${err instanceof Error ? err.message : err}); pending ${what} cannot be recorded until it is fixed`;
  }
  const save = () => {
    if (problem) throw new Error(problem);
    mkdirSync(dirname(file), { recursive: true });
    const tmp = `${file}.tmp`;
    writeFileSync(tmp, JSON.stringify({ [what]: items }, null, 2) + "\n", { mode: 0o600 });
    renameSync(tmp, file);
  };
  return {
    get problem() { return problem; },
    list: () => items.map((i) => ({ ...i })),
    get: (appId: string) => items.find((i) => i.appId === appId),
    add(r: T) { items = [...items.filter((i) => i.appId !== r.appId), r]; save(); },
    remove(appId: string) { items = items.filter((i) => i.appId !== appId); save(); },
  };
}
export type PendingStore<T extends { appId: string }> = ReturnType<typeof pendingStore<T>>;

/** Removals whose first half is done and whose second half (routing, secrets) waits for the merge. */
export const removalStore = (file: string) => pendingStore<StoredRemoval>(file, "removals");
export type RemovalStore = ReturnType<typeof removalStore>;

/** The state of a pull request on Forgejo, from its URL (`…/pulls/<n>`); cached briefly because the page polls. */
export function pullStateReader(cfg: Config, fetchFn: typeof fetch = fetch, now: () => number = Date.now, ttlMs = 10_000) {
  const cache = new Map<string, { at: number; state: PendingRemoval["pr"] }>();
  const c = cfg.create;
  return async (pullRequestUrl: string): Promise<PendingRemoval["pr"]> => {
    const hit = cache.get(pullRequestUrl);
    if (hit && now() - hit.at < ttlMs) return hit.state;
    let state: PendingRemoval["pr"] = "unknown";
    try {
      const number = /\/pulls\/(\d+)$/.exec(pullRequestUrl)?.[1];
      const clone = c.cloneUrl ? new URL(c.cloneUrl) : undefined;
      const base = (c.forgejoUrl ?? clone?.origin ?? "").replace(/\/$/, "");
      const repo = clone?.pathname.replace(/^\/|\.git$|\/$/g, "");
      if (number && base && repo && c.forgejoToken) {
        const res = await fetchFn(`${base}/api/v1/repos/${repo}/pulls/${number}`, { headers: { authorization: `token ${c.forgejoToken}` }, signal: AbortSignal.timeout(8000) });
        if (res.ok) {
          const pull = (await res.json()) as { merged?: boolean; state?: string };
          state = pull.merged ? "merged" : pull.state === "open" ? "open" : "closed";
        }
      }
    } catch {
      /* Forgejo unreachable: unknown, the page shows that */
    }
    cache.set(pullRequestUrl, { at: now(), state });
    return state;
  };
}
export type PullStateReader = ReturnType<typeof pullStateReader>;
