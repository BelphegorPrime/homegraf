import type { CertificateInfo, Health, PendingCreation } from "../shared/types.js";
import type { Config } from "./config.js";
import type { Probe } from "./certificates.js";
import { komodo, stackHealth, type KomodoStack } from "./status.js";
import { pendingStore, type PullStateReader } from "./removals.js";

export type StoredCreation = Pick<PendingCreation, "appId" | "server" | "pullRequestUrl" | "domain" | "requestedBy" | "at"> & {
  /** When every step was first seen through; the entry goes an hour later. */
  settled?: string;
};

export const creationStore = (file: string) => pendingStore<StoredCreation>(file, "creations");
export type CreationStore = ReturnType<typeof creationStore>;

const HOUR = 3_600_000;
const WEEK = 168 * HOUR;

/** The domain a finished `new-app` job routes to: its summary prints `routing: https://<domain> -> <server>:<port>`. */
export const domainOfLog = (log: string): string | undefined => /routing: https:\/\/(\S+) ->/.exec(log)?.[1];

/** Komodo's state of one stack by name; all stacks are asked at once and kept for `ttlMs`, because the page polls. */
export function stackStateReader(cfg: Config, fetchFn: typeof fetch = fetch, now: () => number = Date.now, ttlMs = 10_000) {
  let cache: { at: number; stacks: KomodoStack[] } | undefined;
  return async (name: string): Promise<{ health: Health; detail?: string } | undefined> => {
    if (!cfg.komodo) return undefined;
    try {
      if (!cache || now() - cache.at >= ttlMs) cache = { at: now(), stacks: await komodo<KomodoStack[]>(cfg.komodo, "ListStacks", fetchFn) };
      const s = cache.stacks.find((x) => x.name === name);
      return s && { health: stackHealth(s.info?.state), detail: s.info?.status || s.info?.state };
    } catch {
      return undefined; // Komodo unreachable: the page shows the step as not known yet
    }
  };
}
export type StackStateReader = ReturnType<typeof stackStateReader>;

export interface CreationsDeps {
  store: CreationStore;
  pullState: PullStateReader;
  stackState: StackStateReader;
  probe: Probe;
  now?: () => number;
}

/**
 * The pending creations with how far each is: pull request, stack, HTTPS. Steps are asked only once the one before is
 * through. Finished entries (and forgotten ones) go after a while, so the list does not grow.
 */
export function creationTracker({ store, pullState, stackState, probe, now = Date.now }: CreationsDeps) {
  const certificates = new Map<string, { at: number; result: NonNullable<PendingCreation["https"]> }>();
  async function https(domain: string): Promise<NonNullable<PendingCreation["https"]>> {
    const hit = certificates.get(domain);
    if (hit && now() - hit.at < 10_000) return hit.result;
    const cert: CertificateInfo = await probe(domain).catch((e: unknown) => ({ domain, error: e instanceof Error ? e.message : String(e) }));
    const result = cert.error
      ? { ok: false, detail: cert.error }
      : cert.matches
        ? { ok: true }
        : { ok: false, detail: "the proxy serves a certificate for another name yet" };
    certificates.set(domain, { at: now(), result });
    return result;
  }

  return {
    async list(): Promise<PendingCreation[]> {
      const out = await Promise.all(
        store.list().map(async (r): Promise<PendingCreation | undefined> => {
          const age = (since: string) => now() - Date.parse(since);
          if (r.settled ? age(r.settled) > HOUR : age(r.at) > WEEK) {
            try {
              store.remove(r.appId);
            } catch {
              /* the file is unreadable: nothing to prune */
            }
            return undefined;
          }
          const pr = await pullState(r.pullRequestUrl);
          const stack = pr === "merged" ? await stackState(r.appId) : undefined;
          const answering = stack?.health === "ok" && r.domain ? await https(r.domain) : undefined;
          const done = pr === "merged" && stack?.health === "ok" && (!r.domain || !!answering?.ok);
          if (done && !r.settled) {
            try {
              store.add({ ...r, settled: new Date(now()).toISOString() });
            } catch {
              /* shown anyway, kept until the file works */
            }
          }
          const { settled: _settled, ...shown } = r;
          return { ...shown, pr, stack, https: answering, done };
        }),
      );
      return out.filter((c): c is PendingCreation => !!c);
    },
  };
}
export type CreationTracker = ReturnType<typeof creationTracker>;
