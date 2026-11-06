import type { Graph, Health, NodeStatus, StatusSnapshot } from "../shared/types.js";
import type { Config } from "./config.js";

export interface KomodoStack {
  name: string;
  info?: { state?: string; status?: string; services?: { service: string; update_available?: boolean }[] };
}
/** One service of a stack with its container, as `ListStackServices` answers (the part used here). */
export interface KomodoStackService {
  service: string;
  container?: { state?: string; status?: string } | null;
}
interface KomodoServer {
  name: string;
  info?: { state?: string };
}

const STACK_HEALTH: Record<string, Health> = {
  running: "ok",
  deploying: "warn",
  restarting: "warn",
  paused: "warn",
  unhealthy: "warn",
  created: "down",
  stopped: "down",
  down: "down",
  dead: "down",
  removing: "down",
};

/** What the UniFi controller calls a device's state (1 = connected); the rest are transitions or trouble. */
const UNIFI_STATE: Record<number, string> = {
  0: "offline",
  1: "connected",
  4: "updating",
  5: "provisioning",
  6: "heartbeat missed",
  7: "adopting",
  9: "adoption failed",
  11: "isolated",
};
export const unifiHealth = (state: number): NodeStatus => ({
  health: state === 1 ? "ok" : state === 0 || state === 9 || state === 6 ? "down" : "warn",
  detail: UNIFI_STATE[state] ?? `state ${state}`,
});

export const stackHealth = (state?: string): Health => STACK_HEALTH[(state ?? "").toLowerCase()] ?? "unknown";
export const serverHealth = (state?: string): Health => (state === "Ok" ? "ok" : state === "NotOk" ? "down" : state === "Disabled" ? "unknown" : "unknown");

/**
 * How the last run of a one-shot job went, from its containers: Komodo calls such a stack stopped, and its own status only counts
 * the exited services, but Docker's status of the container says `Exited (0) 16 hours ago`. Undefined when the containers say nothing
 * clear (restarting, created, paused), so the stack's own state applies.
 */
export function jobStatus(services: KomodoStackService[]): NodeStatus | undefined {
  const containers = services.map((s) => s.container).filter((c): c is NonNullable<typeof c> => !!c);
  if (!containers.length) return { health: "unknown", detail: "no run yet" };
  const states = containers.map((c) => (c.state ?? "").toLowerCase());
  if (states.includes("restarting")) return undefined;
  if (states.includes("running")) return { health: "ok", detail: "running now" };
  if (!states.every((s) => s === "exited")) return undefined;
  const runs = containers.map((c) => /^Exited \((\d+)\)\s*(.*)$/i.exec(c.status ?? ""));
  if (runs.some((r) => !r)) return undefined;
  const failed = runs.find((r) => r![1] !== "0");
  return failed
    ? { health: "down", detail: `last run failed (exit ${failed[1]})${failed[2] ? `, ${failed[2]}` : ""}` }
    : { health: "ok", detail: `last run ok${runs[0]![2] ? `, ${runs[0]![2]}` : ""}` };
}

/** Maps Komodo's stacks and servers onto graph node ids; containers inherit their stack's health. `jobs`: the last run of the one-shot stacks, by stack name. */
export function statusFromKomodo(
  graph: Graph,
  stacks: KomodoStack[],
  servers: KomodoServer[],
  jobs: Record<string, NodeStatus> = {},
): Record<string, NodeStatus> {
  const out: Record<string, NodeStatus> = {};
  const byName = new Map(stacks.map((s) => [s.name, s]));
  for (const n of graph.nodes) {
    if (n.type === "machine") {
      const s = servers.find((x) => x.name === n.label);
      if (s) out[n.id] = { health: serverHealth(s.info?.state), detail: s.info?.state };
    } else if (n.type === "stack") {
      const s = byName.get(n.label);
      if (s) {
        out[n.id] = {
          health: stackHealth(s.info?.state),
          detail: s.info?.status || s.info?.state,
          updateAvailable: (s.info?.services ?? []).some((svc) => svc.update_available) || undefined,
          ...(n.job ? jobs[n.label] : undefined),
        };
      }
    }
  }
  // The gateway, switches and access points report their own state to the UniFi controller.
  for (const n of graph.nodes) {
    if (["router", "switch", "device"].includes(n.type) && typeof n.unifiState === "number") out[n.id] = unifiHealth(n.unifiState);
  }
  for (const n of graph.nodes) {
    if (n.type === "container" && n.stack && out[`stack:${n.stack}`]) out[n.id] = { health: out[`stack:${n.stack}`].health };
  }
  // A domain is as healthy as what it proxies to; platform and appliance nodes follow the stack that runs them.
  const edgeTargets = new Map(graph.edges.filter((e) => e.type === "runs").map((e) => [e.source, e.target]));
  for (const [infra, stack] of edgeTargets) if (out[stack]) out[infra] = { health: out[stack].health };
  for (const n of graph.nodes) {
    if (n.type === "domain" && n.target && out[n.target]) out[n.id] = { health: out[n.target].health };
    if (n.type === "service" && n.server && out[`machine:${n.server}`]) out[n.id] = { health: out[`machine:${n.server}`].health };
  }
  return out;
}

export async function komodo<T>(cfg: NonNullable<Config["komodo"]>, type: string, fetchFn: typeof fetch, body: object = {}): Promise<T> {
  const res = await fetchFn(`${cfg.url}/read/${type}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Api-Key": cfg.key, "X-Api-Secret": cfg.secret },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw new Error(`Komodo ${type}: HTTP ${res.status}`);
  return (await res.json()) as T;
}

export function statusPoller(cfg: Config, loadGraph: () => Promise<Graph>, fetchFn: typeof fetch = fetch) {
  let snapshot: StatusSnapshot = {
    updated: null,
    source: cfg.komodo ? "komodo" : "disabled",
    nodes: {},
  };
  const listeners = new Set<(s: StatusSnapshot) => void>();

  async function refresh(): Promise<void> {
    if (!cfg.komodo) return;
    try {
      const [graph, stacks, servers] = await Promise.all([
        loadGraph(),
        komodo<KomodoStack[]>(cfg.komodo, "ListStacks", fetchFn),
        komodo<KomodoServer[]>(cfg.komodo, "ListServers", fetchFn),
      ]);
      // One-shot jobs: the exit code of the last run is in the containers, not in the stack's state.
      const jobs: Record<string, NodeStatus> = {};
      await Promise.all(
        graph.nodes
          .filter((n) => n.type === "stack" && n.job)
          .map(async (n) => {
            try {
              const run = jobStatus(await komodo<KomodoStackService[]>(cfg.komodo!, "ListStackServices", fetchFn, { stack: n.label }));
              if (run) jobs[n.label] = run;
            } catch {
              /* not known: the stack's own state shows */
            }
          }),
      );
      snapshot = {
        updated: new Date().toISOString(),
        source: "komodo",
        nodes: statusFromKomodo(graph, stacks, servers, jobs),
      };
    } catch (err) {
      // Keep the last known states; only flag that they are stale.
      snapshot = { ...snapshot, source: "error", error: err instanceof Error ? err.message : String(err) };
    }
    for (const l of listeners) l(snapshot);
  }

  return {
    get: () => snapshot,
    refresh,
    subscribe(l: (s: StatusSnapshot) => void) {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    start() {
      void refresh();
      return setInterval(() => void refresh(), cfg.statusIntervalMs);
    },
  };
}
