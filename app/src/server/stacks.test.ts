import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { createApp } from "./app.js";
import { applier, type Spawn } from "./apply.js";
import { authenticator } from "./auth.js";
import { loadConfig } from "./config.js";
import { testCreations, testHosts, testRemoval, testStacks } from "./testing.js";
import { userStore } from "./users.js";

const cfg = loadConfig({
  KOMODO_URL: "http://komodo:9120", KOMODO_API_KEY: "kkey", KOMODO_API_SECRET: "ksecret", HOMEGRAF_PASSWORD: "pw-1234",
  HOMEGRAF_CLONE_URL: "https://git.example/owner/infrastructure.git", FORGEJO_TOKEN: "ftoken",
  INFISICAL_URL: "https://inf.example", INFISICAL_BOOTSTRAP_CLIENT_ID: "iid", INFISICAL_BOOTSTRAP_CLIENT_SECRET: "isecret", REPO_DIR: "/repo",
});
const basic = (name: string, password: string) => `Basic ${Buffer.from(`${name}:${password}`).toString("base64")}`;
const paperless = { id: "paperless", server: "worker-1", komodoStack: "paperless", secrets: true };
const aws = { id: "aws", server: "aws", komodoStack: "aws-floci", secrets: false };

async function build(answer?: Parameters<typeof testStacks>[1]) {
  const spawned: { cmd: string; args: string[]; cwd?: string; env: Record<string, string> }[] = [];
  const spawn: Spawn = async (cmd, args, o, onData) => { spawned.push({ cmd, args, cwd: o.cwd, env: o.env }); if (cmd === "python3") onData("deploy succeeded\n"); return 0; };
  const users = userStore(join(mkdtempSync(join(tmpdir(), "homegraf-")), "users.json"));
  await users.add("sam", "sam manages secrets", ["secrets:manage"]);
  await users.add("dee", "dee may deploy things", ["stacks:deploy"]);
  const tool = testStacks([paperless, aws], answer);
  const app = createApp({
    loadGraph: async () => { throw new Error("unused"); },
    status: { get: () => ({ updated: null, source: "disabled", nodes: {} }), subscribe: () => () => {} },
    webDir: "dist/web", plan: async () => { throw new Error("unused"); }, preview: async () => { throw new Error("unused"); },
    addons: async () => [], templates: async () => [], unifi: () => ({ enabled: false }),
    certificates: { get: async () => ({ updated: null, certificates: [] }) },
    create: { problem: undefined, applier: applier({ cfg, spawn, exists: () => true, login: async () => "owner" }) },
    users: { enabled: true, store: users, authenticate: authenticator({ adminPassword: "pw-1234", users }) },
    removal: testRemoval(), creations: testCreations(), hosts: testHosts(), stacks: tool,
  });
  const call = (method: string, path: string, auth?: string, body?: unknown) => app.request(path, {
    method, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    headers: { "content-type": "application/json", ...(auth ? { authorization: auth } : {}) },
  });
  return { app, call, tool, spawned };
}
const sam = basic("sam", "sam manages secrets");
const dee = basic("dee", "dee may deploy things");

describe("stacks", () => {
  it("lists the stacks that may be managed", async () => {
    const { app } = await build();
    expect(await (await app.request("/api/stacks")).json()).toEqual([paperless, aws]);
  });
});

describe("secrets", () => {
  it("need secrets:manage, even to see the names", async () => {
    const { call, tool } = await build();
    for (const [m, p, b] of [["GET", "/api/stacks/paperless/secrets"], ["PUT", "/api/stacks/paperless/secrets/KEY", undefined, { value: "x" }], ["DELETE", "/api/stacks/paperless/secrets/KEY"]] as const) {
      expect((await call(m, p, undefined, b)).status).toBe(401);
      expect((await call(m, p, dee, b)).status).toBe(403);
    }
    expect(tool.calls).toEqual([]);
  });

  it("lists the names with whether a compose file uses them", async () => {
    const { call, tool } = await build(() => ({ code: 0, out: '[{"key":"DB_PASSWORD","used":true},{"key":"OLD","used":false}]\n' }));
    const res = await call("GET", "/api/stacks/paperless/secrets", sam);
    expect(await res.json()).toEqual({ keys: [{ key: "DB_PASSWORD", used: true }, { key: "OLD", used: false }], redeploy: "paperless" });
    expect(tool.calls).toEqual([{ args: ["list", "paperless", "--json"], stdin: undefined }]);
  });

  it("only for stacks that keep secrets in Infisical and are not the platform", async () => {
    const { call, tool } = await build();
    expect((await call("GET", "/api/stacks/aws/secrets", sam)).status).toBe(404);
    expect((await call("GET", "/api/stacks/infisical/secrets", sam)).status).toBe(404);
    expect((await call("GET", "/api/stacks/homegraf/secrets", sam)).status).toBe(404);
    expect(tool.calls).toEqual([]);
  });

  it("sets a typed value through stdin, never on the command line, and keeps it out of the answer", async () => {
    const { call, tool } = await build((_a, stdin) => ({ code: 0, out: `stacks-worker-1/prod/paperless/TOKEN created ${stdin}\n` }));
    const res = await call("PUT", "/api/stacks/paperless/secrets/TOKEN", sam, { value: "s3cr3t-value" });
    expect(res.status).toBe(200);
    const text = await res.text();
    expect(JSON.parse(text)).toEqual({ result: "created", redeploy: "paperless" });
    expect(text).not.toContain("s3cr3t-value");
    expect(tool.calls).toEqual([{ args: ["set", "paperless", "TOKEN"], stdin: "s3cr3t-value" }]);
    expect(JSON.stringify(tool.calls[0].args)).not.toContain("s3cr3t-value");
  });

  it("generates a value on request, which is never shown", async () => {
    const { call, tool } = await build(() => ({ code: 0, out: "stacks-worker-1/prod/paperless/TOKEN changed\n" }));
    const res = await call("PUT", "/api/stacks/paperless/secrets/TOKEN", sam, { generate: true });
    expect(await res.json()).toEqual({ result: "changed", redeploy: "paperless" });
    expect(tool.calls).toEqual([{ args: ["set", "paperless", "TOKEN", "--generate"], stdin: undefined }]);
  });

  it("refuses a bad name and a missing or huge value before the tool runs", async () => {
    const { call, tool } = await build();
    expect((await call("PUT", "/api/stacks/paperless/secrets/bad-name", sam, { value: "x" })).status).toBe(400);
    expect((await call("PUT", "/api/stacks/paperless/secrets/KEY", sam, {})).status).toBe(400);
    expect((await call("PUT", "/api/stacks/paperless/secrets/KEY", sam, { value: "" })).status).toBe(400);
    expect((await call("PUT", "/api/stacks/paperless/secrets/KEY", sam, { value: "x".repeat(5000) })).status).toBe(400);
    expect(tool.calls).toEqual([]);
  });

  it("passes on the tool's message when it fails, and scrubs the value from it", async () => {
    const { call } = await build((_a, stdin) => ({ code: 1, out: `error: Infisical said no to ${stdin}\n` }));
    const res = await call("PUT", "/api/stacks/paperless/secrets/KEY", sam, { value: "hunter2-hunter2" });
    expect(res.status).toBe(502);
    const text = await res.text();
    expect(text).toContain("Infisical said no to ***");
    expect(text).not.toContain("hunter2");
  });

  it("deletes a key, and answers 409 while a compose file still uses it unless forced", async () => {
    const { call, tool } = await build((a) => a.includes("--force") ? { code: 0, out: "deleted\n" } : { code: 1, out: "error: paperless still uses ${KEY}; change the compose file first, or --force\n" });
    const refused = await call("DELETE", "/api/stacks/paperless/secrets/KEY", sam);
    expect(refused.status).toBe(409);
    expect((await refused.json()).error).toMatch(/still uses/);
    expect((await call("DELETE", "/api/stacks/paperless/secrets/KEY?force=true", sam)).status).toBe(200);
    expect(tool.calls.map((c) => c.args)).toEqual([["delete", "paperless", "KEY", "--yes"], ["delete", "paperless", "KEY", "--yes", "--force"]]);
  });
});

describe("redeploy", () => {
  it("needs stacks:deploy", async () => {
    const { call, spawned } = await build();
    expect((await call("POST", "/api/stacks/paperless/deploy")).status).toBe(401);
    expect((await call("POST", "/api/stacks/paperless/deploy", sam)).status).toBe(403);
    expect(spawned).toEqual([]);
  });

  it("refuses the platform and unknown stacks", async () => {
    const { call, spawned } = await build();
    for (const id of ["infisical", "homegraf", "reverseProxy", "nope"]) expect((await call("POST", `/api/stacks/${id}/deploy`, dee)).status).toBe(404);
    expect(spawned).toEqual([]);
  });

  it("runs homelab.py deploy for the Komodo stack from the read-only checkout, as a job that names who asked", async () => {
    const { app, call, spawned } = await build();
    const res = await call("POST", "/api/stacks/aws/deploy", dee);
    expect(res.status).toBe(202);
    const { jobId } = await res.json();
    expect(await (await app.request(`/api/apps/jobs/${jobId}/events`)).text()).toContain("deploy succeeded");
    expect(spawned).toHaveLength(1);                                         // no clone, no git
    expect(spawned[0]).toMatchObject({ cmd: "python3", args: ["-I", "/repo/homelab.py", "deploy", "aws-floci", "--yes"], cwd: "/repo" });
    expect(await (await app.request(`/api/apps/jobs/${jobId}`)).json()).toMatchObject({ kind: "deploy", appId: "aws-floci", requestedBy: "dee", state: "done" });
  });
});
