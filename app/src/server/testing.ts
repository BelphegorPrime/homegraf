import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Health, HostInfo, ManagedStack, RemovableApp } from "../shared/types.js";
import type { SecretTool } from "./apps.js";
import type { CreationsDeps, HostsDeps, RemovalDeps, StacksDeps } from "./app.js";
import { creationStore, creationTracker } from "./creations.js";
import { removalStore } from "./removals.js";

/** Removal dependencies for tests: the given apps, a store in a fresh temporary folder, and fixed pull request states. */
export function testRemoval(apps: RemovableApp[] = [], pulls: Record<string, "open" | "merged" | "closed" | "unknown"> = {}): RemovalDeps {
  return {
    apps: async () => apps,
    store: removalStore(join(mkdtempSync(join(tmpdir(), "homegraf-removals-")), "removals.json")),
    pullState: async (url) => pulls[url] ?? "unknown",
  };
}

/** Stack dependencies for tests: the given stacks and a tool that records its calls and answers like `homelab.py secrets`. */
export function testStacks(
  list: ManagedStack[] = [],
  answer: (args: string[], stdin?: string) => { code: number; out: string } = () => ({ code: 0, out: "" }),
): StacksDeps & { calls: { args: string[]; stdin?: string }[] } {
  const calls: { args: string[]; stdin?: string }[] = [];
  const secretTool: SecretTool = async (args, stdin) => {
    calls.push({ args, stdin });
    return answer(args, stdin);
  };
  return { list: async () => list, secretTool, calls };
}

/** Creation dependencies for tests: a store in a fresh temporary folder, fixed pull request and stack states, and certificates by domain. */
export function testCreations(
  {
    pulls = {},
    stacks = {},
    certs = {},
  }: {
    pulls?: Record<string, "open" | "merged" | "closed" | "unknown">;
    stacks?: Record<string, { health: Health; detail?: string }>;
    certs?: Record<string, { error?: string; matches?: boolean }>;
  } = {},
  now: () => number = Date.now,
): CreationsDeps {
  const store = creationStore(join(mkdtempSync(join(tmpdir(), "homegraf-creations-")), "creations.json"));
  const tracker = creationTracker({
    store,
    now,
    pullState: async (url) => pulls[url] ?? "unknown",
    stackState: async (name) => stacks[name],
    probe: async (domain) => ({ domain, ...(certs[domain] ?? { error: "ECONNREFUSED" }) }),
  });
  return { store, tracker };
}

/** Host dependencies for tests: the given hosts and a CA source (none by default). */
export function testHosts(list: HostInfo[] = [], rootCa: HostsDeps["rootCa"] = () => null): HostsDeps {
  return { list: async () => list, rootCa };
}

/**
 * The app as somebody signed in, for the read routes (GET): every route is behind the sign-in now, and those tests are about what a route
 * answers, not about who may ask. Requests that bring their own `Authorization` or cookie, and every write, go through as they are.
 */
export function signedInReads<A extends { request: (path: string, init?: RequestInit) => Response | Promise<Response> }>(
  app: A,
  password: string,
  name = "admin",
): A {
  const auth = `Basic ${Buffer.from(`${name}:${password}`).toString("base64")}`;
  const request = (path: string, init: RequestInit = {}) => {
    const headers = new Headers(init.headers);
    if ((init.method ?? "GET") === "GET" && !headers.has("authorization") && !headers.has("cookie")) headers.set("authorization", auth);
    return app.request(path, { ...init, headers });
  };
  return new Proxy(app, { get: (target, prop, receiver) => (prop === "request" ? request : Reflect.get(target, prop, receiver)) });
}
