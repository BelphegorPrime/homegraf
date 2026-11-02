import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { createApp } from "./app.js";
import { applier, type Spawn } from "./apply.js";
import { authenticator } from "./auth.js";
import { loadConfig } from "./config.js";
import { creationStore, domainOfLog, stackStateReader } from "./creations.js";
import { testCreations, testHosts, testRemoval, testStacks } from "./testing.js";
import { userStore } from "./users.js";

const env = {
  KOMODO_URL: "http://komodo:9120", KOMODO_API_KEY: "kkey", KOMODO_API_SECRET: "ksecret",
  HOMEGRAF_PASSWORD: "pw-1234", HOMEGRAF_CLONE_URL: "https://git.example/owner/infrastructure.git", FORGEJO_TOKEN: "ftoken",
  INFISICAL_URL: "https://inf.example", INFISICAL_BOOTSTRAP_CLIENT_ID: "iid", INFISICAL_BOOTSTRAP_CLIENT_SECRET: "isecret",
};
const cfg = loadConfig(env);
const PR = "https://git.example/owner/infrastructure/pulls/9";
const record = { appId: "demo", server: "worker-1", pullRequestUrl: PR, domain: "demo.lab.example", requestedBy: "alice", at: "2026-11-01T10:00:00.000Z" };
const at = (iso: string) => () => Date.parse(iso);

describe("domainOfLog", () => {
  it("reads the domain from the tool's summary", () => {
    expect(domainOfLog("  secrets: none\n  routing: https://demo.lab.example -> worker-1:8080\n")).toBe("demo.lab.example");
    expect(domainOfLog("  routing: none\n")).toBeUndefined();
  });
});

describe("following a creation", () => {
  const follow = async (o: Parameters<typeof testCreations>[0], now = at("2026-11-01T10:05:00Z")) => {
    const c = testCreations(o, now);
    c.store.add(record);
    return { ...c, list: () => c.tracker.list() };
  };

  it("waits for the merge without asking Komodo or the proxy", async () => {
    const { list } = await follow({ pulls: { [PR]: "open" }, stacks: { demo: { health: "ok" } } });
    expect(await list()).toEqual([{ ...record, pr: "open", done: false }]);
  });

  it("shows the stack once merged, and asks for HTTPS only when the stack runs", async () => {
    const down = await follow({ pulls: { [PR]: "merged" }, stacks: { demo: { health: "down", detail: "Down" } }, certs: { "demo.lab.example": { matches: true } } });
    expect(await down.list()).toMatchObject([{ pr: "merged", stack: { health: "down" }, https: undefined, done: false }]);
    const running = await follow({ pulls: { [PR]: "merged" }, stacks: { demo: { health: "ok" } } });
    expect(await running.list()).toMatchObject([{ stack: { health: "ok" }, https: { ok: false, detail: "ECONNREFUSED" }, done: false }]);
  });

  it("is done when merged, running and the domain serves a certificate for itself; that entry goes an hour later", async () => {
    let now = Date.parse("2026-11-01T10:05:00Z");
    const c = testCreations({ pulls: { [PR]: "merged" }, stacks: { demo: { health: "ok" } }, certs: { "demo.lab.example": { matches: true } } }, () => now);
    c.store.add(record);
    expect(await c.tracker.list()).toMatchObject([{ appId: "demo", https: { ok: true }, done: true }]);
    expect(c.store.list()[0].settled).toBe("2026-11-01T10:05:00.000Z");
    now += 59 * 60_000;
    expect(await c.tracker.list()).toHaveLength(1);
    now += 2 * 60_000;
    expect(await c.tracker.list()).toEqual([]);
    expect(c.store.list()).toEqual([]);
  });

  it("needs no HTTPS step for an app without a domain", async () => {
    const c = testCreations({ pulls: { [PR]: "merged" }, stacks: { demo: { health: "ok" } } }, at("2026-11-01T10:05:00Z"));
    c.store.add({ ...record, domain: undefined });
    expect(await c.tracker.list()).toMatchObject([{ done: true, https: undefined }]);
  });

  it("keeps a closed pull request visible, and forgets anything after a week", async () => {
    const closed = await follow({ pulls: { [PR]: "closed" } });
    expect(await closed.list()).toMatchObject([{ pr: "closed", done: false }]);
    const old = await follow({ pulls: { [PR]: "closed" } }, at("2026-11-09T10:00:00Z"));
    expect(await old.list()).toEqual([]);
  });

  it("does not claim a certificate for another name", async () => {
    const { list } = await follow({ pulls: { [PR]: "merged" }, stacks: { demo: { health: "ok" } }, certs: { "demo.lab.example": { matches: false } } });
    expect(await list()).toMatchObject([{ https: { ok: false }, done: false }]);
  });
});

describe("creationStore", () => {
  it("keeps records across restarts under its own key", () => {
    const file = join(mkdtempSync(join(tmpdir(), "homegraf-")), "data", "creations.json");
    creationStore(file).add(record);
    expect(creationStore(file).list()).toEqual([record]);
  });
});

describe("stackStateReader", () => {
  it("reads one stack's state from Komodo and asks once for a few seconds", async () => {
    const calls: string[] = [];
    let now = 0;
    const read = stackStateReader(cfg, async (url) => {
      calls.push(String(url));
      return new Response(JSON.stringify([{ name: "demo", info: { state: "running", status: "Up 2 minutes" } }, { name: "other", info: { state: "down" } }]));
    }, () => now, 1000);
    expect(await read("demo")).toEqual({ health: "ok", detail: "Up 2 minutes" });
    expect(await read("other")).toMatchObject({ health: "down" });
    expect(await read("nope")).toBeUndefined();
    expect(calls).toEqual(["http://komodo:9120/read/ListStacks"]);
    now = 1001; await read("demo");
    expect(calls).toHaveLength(2);
  });

  it("knows nothing when Komodo is unreachable", async () => {
    expect(await stackStateReader(cfg, async () => { throw new Error("offline"); })("demo")).toBeUndefined();
  });
});

describe("creating an app records it for following", () => {
  const PLAN = { appId: "demo", server: "worker-1", image: "traefik/whoami:v1.11.0", containerPort: 80, hostPort: 8081 };
  const build = async (log: string) => {
    const spawn: Spawn = async (cmd, _args, _o, onData) => { if (cmd === "python3") onData(log); return 0; };
    const users = userStore(join(mkdtempSync(join(tmpdir(), "homegraf-")), "users.json"));
    await users.add("alice", "alice may create", ["apps:create"]);
    const creations = testCreations({ pulls: { [PR]: "open" } });
    const app = createApp({
      loadGraph: async () => { throw new Error("unused"); },
      status: { get: () => ({ updated: null, source: "disabled", nodes: {} }), subscribe: () => () => {} },
      webDir: "dist/web", plan: async () => { throw new Error("unused"); }, preview: async () => { throw new Error("unused"); },
      addons: async () => [], templates: async () => [], unifi: () => ({ enabled: false }),
      certificates: { get: async () => ({ updated: null, certificates: [] }) },
      create: { problem: undefined, applier: applier({ cfg, spawn, exists: () => true, login: async () => "owner" }) },
      users: { enabled: true, store: users, authenticate: authenticator({ adminPassword: "pw-1234", users }) },
      removal: testRemoval(), creations, hosts: testHosts(), stacks: testStacks(),
    });
    const res = await app.request("/api/apps/create", {
      method: "POST", body: JSON.stringify(PLAN),
      headers: { "content-type": "application/json", authorization: `Basic ${Buffer.from("alice:alice may create").toString("base64")}` },
    });
    expect(res.status).toBe(202);
    await (await app.request(`/api/apps/jobs/${(await res.json()).jobId}/events`)).text();
    return { app, creations };
  };

  it("keeps the pull request, the domain from the tool's summary and who asked", async () => {
    const { app, creations } = await build(`  routing: https://demo.lab.example -> worker-1:8081\n  pull request: ${PR}\n`);
    expect(creations.store.list()).toMatchObject([{ appId: "demo", server: "worker-1", pullRequestUrl: PR, domain: "demo.lab.example", requestedBy: "alice" }]);
    expect(await (await app.request("/api/creations")).json()).toMatchObject({ creations: [{ appId: "demo", pr: "open", done: false }] });
  });

  it("records nothing when the tool opened no pull request", async () => {
    const { creations } = await build("routing: https://demo.lab.example -> worker-1:8081\n");
    expect(creations.store.list()).toEqual([]);
  });
});
