import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { createApp } from "./app.js";
import { applier, type Spawn } from "./apply.js";
import { authenticator } from "./auth.js";
import { loadConfig } from "./config.js";
import { pullStateReader, removalStore } from "./removals.js";
import { signedInReads, testCreations, testHosts, testRemoval, testStacks } from "./testing.js";
import { userStore } from "./users.js";

const env = {
  KOMODO_URL: "http://komodo:9120", KOMODO_API_KEY: "kkey", KOMODO_API_SECRET: "ksecret",
  HOMEGRAF_PASSWORD: "pw-1234", HOMEGRAF_CLONE_URL: "https://git.example/owner/infrastructure.git", FORGEJO_TOKEN: "ftoken",
  INFISICAL_URL: "https://inf.example", INFISICAL_BOOTSTRAP_CLIENT_ID: "iid", INFISICAL_BOOTSTRAP_CLIENT_SECRET: "isecret",
};
const cfg = loadConfig(env);
const basic = (name: string, password: string) => `Basic ${Buffer.from(`${name}:${password}`).toString("base64")}`;
const PR = "https://git.example/owner/infrastructure/pulls/7";
const demo = { id: "demo", server: "worker-1", komodoStack: "demo", secrets: true, domains: ["demo.lab.example"], dataPaths: ["/srv/docker/demo"] };

interface Call { cmd: string; args: string[]; env: Record<string, string> }

async function build(pulls: Record<string, "open" | "merged" | "closed" | "unknown"> = {}) {
  const calls: Call[] = [];
  const spawn: Spawn = async (cmd, args, o, onData) => {
    calls.push({ cmd, args, env: o.env });
    if (cmd === "python3") onData(args.includes("--finish") ? "demo is removed.\n" : `pull request: ${PR}\n`);
    return 0;
  };
  const store = userStore(join(mkdtempSync(join(tmpdir(), "homegraf-")), "users.json"));
  await store.add("alice", "alice may remove apps", ["apps:remove"]);
  await store.add("bob", "bob may only create", ["apps:create"]);
  const removal = testRemoval([demo], pulls);
  const app = signedInReads(createApp({
    loadGraph: async () => { throw new Error("unused"); },
    status: { get: () => ({ updated: null, source: "disabled", nodes: {} }), subscribe: () => () => {} },
    webDir: "dist/web", plan: async () => { throw new Error("unused"); }, preview: async () => { throw new Error("unused"); },
    addons: async () => [], templates: async () => [], unifi: () => ({ enabled: false }),
    certificates: { get: async () => ({ updated: null, certificates: [] }) },
    create: { problem: undefined, applier: applier({ cfg, spawn, exists: () => true, login: async () => "owner" }) },
    users: { enabled: true, store, authenticate: authenticator({ adminPassword: "pw-1234", users: store }) },
    removal, creations: testCreations(), hosts: testHosts(),
    stacks: testStacks(),
  }), "pw-1234");
  const post = (path: string, body: unknown, auth?: string) => app.request(path, {
    method: "POST", body: JSON.stringify(body), headers: { "content-type": "application/json", ...(auth ? { authorization: auth } : {}) },
  });
  const settle = async (jobId: string) => (await app.request(`/api/apps/jobs/${jobId}/events`)).text();
  return { app, post, calls, removal, settle };
}

describe("removing an app", () => {
  const alice = basic("alice", "alice may remove apps");

  it("lists the removable apps", async () => {
    const { app } = await build();
    expect(await (await app.request("/api/apps/removable")).json()).toEqual([demo]);
  });

  it("needs apps:remove: nobody, a wrong password and a user without the permission are turned away", async () => {
    const { post, calls } = await build();
    const body = { appId: "demo", confirm: "demo" };
    expect((await post("/api/apps/remove", body)).status).toBe(401);
    expect((await post("/api/apps/remove", body, basic("alice", "nope"))).status).toBe(401);
    const bob = await post("/api/apps/remove", body, basic("bob", "bob may only create"));
    expect(bob.status).toBe(403);
    expect((await bob.json()).error).toMatch(/needs apps:remove/);
    expect(calls).toEqual([]);
  });

  it("wants the app's name typed as confirmation and refuses what is not removable", async () => {
    const { post, calls } = await build();
    expect((await post("/api/apps/remove", { appId: "demo" }, alice)).status).toBe(400);
    expect((await post("/api/apps/remove", { appId: "demo", confirm: "dem" }, alice)).status).toBe(400);
    for (const appId of ["homegraf", "infisical", "nope"]) {
      expect((await post("/api/apps/remove", { appId, confirm: appId }, alice)).status).toBe(404);
    }
    expect(calls).toEqual([]);
  });

  it("runs the first half, names who asked, and remembers the pull request for the second half", async () => {
    const { post, calls, removal, settle } = await build();
    const res = await post("/api/apps/remove", { appId: "demo", confirm: "demo" }, alice);
    expect(res.status).toBe(202);
    const text = await settle((await res.json()).jobId);
    expect(text).toContain(PR);
    const tool = calls.find((c) => c.cmd === "python3")!;
    expect(tool.args.slice(1)).toEqual(["/work/homelab.py", "remove-app", "--app-id", "demo", "--pr", "--yes"]);
    expect(tool.env.GIT_AUTHOR_NAME).toBe("alice via homegraf");
    expect(removal.store.list()).toMatchObject([{ appId: "demo", server: "worker-1", pullRequestUrl: PR, requestedBy: "alice" }]);
  });
});

describe("finishing a removal", () => {
  const alice = basic("alice", "alice may remove apps");
  const started = async (pulls: Record<string, "open" | "merged" | "closed" | "unknown">) => {
    const b = await build(pulls);
    await b.settle((await (await b.post("/api/apps/remove", { appId: "demo", confirm: "demo" }, alice)).json()).jobId);
    return b;
  };

  it("lists the pending removal with the state of its pull request", async () => {
    const { app } = await started({ [PR]: "open" });
    expect(await (await app.request("/api/removals")).json()).toMatchObject({ removals: [{ appId: "demo", pr: "open", pullRequestUrl: PR }] });
  });

  it("waits for the merge", async () => {
    for (const state of ["open", "closed", "unknown"] as const) {
      const { post, calls } = await started({ [PR]: state });
      const before = calls.length;
      const res = await post("/api/apps/remove/finish", { appId: "demo" }, alice);
      expect(res.status).toBe(409);
      expect(calls).toHaveLength(before);
    }
  });

  it("knows nothing about an app that is not waiting, and needs the permission too", async () => {
    const { post } = await started({ [PR]: "merged" });
    expect((await post("/api/apps/remove/finish", { appId: "other" }, alice)).status).toBe(404);
    expect((await post("/api/apps/remove/finish", { appId: "demo" }, basic("bob", "bob may only create"))).status).toBe(403);
    expect((await post("/api/apps/remove/finish", { appId: "demo" })).status).toBe(401);
  });

  it("runs the second half after the merge and forgets the removal", async () => {
    const { app, post, calls, removal, settle } = await started({ [PR]: "merged" });
    const res = await post("/api/apps/remove/finish", { appId: "demo" }, alice);
    expect(res.status).toBe(202);
    await settle((await res.json()).jobId);
    const tool = calls.filter((c) => c.cmd === "python3").at(-1)!;
    expect(tool.args.slice(1)).toEqual(["/work/homelab.py", "remove-app", "--app-id", "demo", "--server", "worker-1", "--finish", "--yes"]);
    expect(removal.store.list()).toEqual([]);
    expect(await (await app.request("/api/removals")).json()).toEqual({ removals: [] });
  });
});

describe("removalStore", () => {
  it("keeps records across restarts, one per app", () => {
    const file = join(mkdtempSync(join(tmpdir(), "homegraf-")), "data", "removals.json");
    const store = removalStore(file);
    store.add({ appId: "demo", server: "s", pullRequestUrl: PR, at: "t1" });
    store.add({ appId: "demo", server: "s", pullRequestUrl: `${PR}8`, at: "t2" });
    store.add({ appId: "other", server: "s", pullRequestUrl: PR, at: "t3" });
    expect(removalStore(file).list().map((r) => [r.appId, r.at])).toEqual([["demo", "t2"], ["other", "t3"]]);
    store.remove("demo");
    expect(removalStore(file).list().map((r) => r.appId)).toEqual(["other"]);
  });
});

describe("pullStateReader", () => {
  const reader = (answers: Record<string, object | number>, calls: string[] = []) =>
    pullStateReader(cfg, async (url, init) => {
      calls.push(`${url} ${(init?.headers as Record<string, string>).authorization}`);
      const a = answers[String(url)];
      return typeof a === "number" ? new Response("", { status: a }) : new Response(JSON.stringify(a ?? {}));
    }, () => 0);

  it("reads merged, open and closed from the Forgejo API of the clone's repository", async () => {
    const calls: string[] = [];
    const read = reader({
      "https://git.example/api/v1/repos/owner/infrastructure/pulls/1": { merged: true, state: "closed" },
      "https://git.example/api/v1/repos/owner/infrastructure/pulls/2": { merged: false, state: "open" },
      "https://git.example/api/v1/repos/owner/infrastructure/pulls/3": { merged: false, state: "closed" },
    }, calls);
    expect(await read("https://git.example/owner/infrastructure/pulls/1")).toBe("merged");
    expect(await read("https://git.example/owner/infrastructure/pulls/2")).toBe("open");
    expect(await read("https://git.example/owner/infrastructure/pulls/3")).toBe("closed");
    expect(calls[0]).toBe("https://git.example/api/v1/repos/owner/infrastructure/pulls/1 token ftoken");
  });

  it("is unknown for an error, a strange URL and an unreachable Forgejo", async () => {
    expect(await reader({ "https://git.example/api/v1/repos/owner/infrastructure/pulls/4": 404 })("https://git.example/owner/infrastructure/pulls/4")).toBe("unknown");
    expect(await reader({})("https://git.example/somewhere/else")).toBe("unknown");
    const down = pullStateReader(cfg, async () => { throw new Error("offline"); });
    expect(await down("https://git.example/owner/infrastructure/pulls/5")).toBe("unknown");
  });

  it("keeps an answer for a few seconds", async () => {
    const calls: string[] = [];
    let now = 0;
    const read = pullStateReader(cfg, async (u) => { calls.push(String(u)); return new Response(JSON.stringify({ merged: true })); }, () => now, 1000);
    await read(PR); await read(PR);
    expect(calls).toHaveLength(1);
    now = 1001; await read(PR);
    expect(calls).toHaveLength(2);
  });
});
