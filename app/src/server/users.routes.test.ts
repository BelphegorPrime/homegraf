import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { testCreations, testHosts, testRemoval, testStacks } from "./testing.js";
import { createApp } from "./app.js";
import { applier } from "./apply.js";
import { authenticator } from "./auth.js";
import { loadConfig } from "./config.js";
import { userStore } from "./users.js";

const basic = (name: string, password: string) => `Basic ${Buffer.from(`${name}:${password}`).toString("base64")}`;
const ADMIN = basic("admin", "pw-admin-1234");

async function build() {
  const store = userStore(join(mkdtempSync(join(tmpdir(), "homegraf-")), "users.json"));
  await store.add("alice", "alice has apps rights", ["apps:create"]);
  const app = createApp({
    loadGraph: async () => { throw new Error("unused"); },
    status: { get: () => ({ updated: null, source: "disabled", nodes: {} }), subscribe: () => () => {} },
    webDir: "dist/web", plan: async () => { throw new Error("unused"); }, preview: async () => { throw new Error("unused"); },
    addons: async () => [], templates: async () => [], unifi: () => ({ enabled: false }),
    certificates: { get: async () => ({ updated: null, certificates: [] }) },
    removal: testRemoval(), creations: testCreations(), hosts: testHosts(),
    stacks: testStacks(),
    create: { problem: undefined, applier: applier({ cfg: loadConfig({}) }) },
    users: { enabled: true, store, authenticate: authenticator({ adminPassword: "pw-admin-1234", users: store }) },
  });
  const call = (method: string, path: string, auth?: string, body?: unknown) => app.request(path, {
    method, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    headers: { "content-type": "application/json", ...(auth ? { authorization: auth } : {}) },
  });
  return { call, store };
}

describe("who am I", () => {
  it("answers with the signed-in user and refuses everybody else", async () => {
    const { call } = await build();
    expect(await (await call("GET", "/api/me", basic("alice", "alice has apps rights"))).json()).toEqual({ name: "alice", permissions: ["apps:create"], source: "user" });
    expect((await call("GET", "/api/me", basic("alice", "nope"))).status).toBe(401);
    expect((await call("GET", "/api/me")).status).toBe(401);
  });
});

describe("user management", () => {
  it("needs users:manage for everything", async () => {
    const { call } = await build();
    const alice = basic("alice", "alice has apps rights");
    for (const [m, p, b] of [["GET", "/api/users"], ["POST", "/api/users", { name: "x1", password: "long enough password" }], ["PATCH", "/api/users/alice", {}], ["DELETE", "/api/users/alice"]] as const)
      expect((await call(m, p, alice, b)).status).toBe(403);
    expect((await call("GET", "/api/users")).status).toBe(401);
  });

  it("lists the built-in administrator first and never a hash", async () => {
    const { call } = await build();
    const body = await (await call("GET", "/api/users", ADMIN)).json();
    expect(body.users[0]).toMatchObject({ name: "admin", builtin: true });
    expect(body.users[1]).toMatchObject({ name: "alice", permissions: ["apps:create"] });
    expect(JSON.stringify(body)).not.toMatch(/scrypt|hash/);
  });

  it("adds, changes and removes a user, who can then sign in with the new rights", async () => {
    const { call } = await build();
    const created = await call("POST", "/api/users", ADMIN, { name: "carol", password: "carol picks this one", permissions: ["users:manage"] });
    expect(created.status).toBe(201);
    expect(JSON.stringify(await created.json())).not.toContain("carol picks");
    expect((await call("GET", "/api/users", basic("carol", "carol picks this one"))).status).toBe(200);

    expect((await call("PATCH", "/api/users/carol", ADMIN, { permissions: [] })).status).toBe(200);
    expect((await call("GET", "/api/users", basic("carol", "carol picks this one"))).status).toBe(403);
    expect((await call("PATCH", "/api/users/carol", ADMIN, { password: "carol picks another" })).status).toBe(200);
    expect((await call("GET", "/api/me", basic("carol", "carol picks this one"))).status).toBe(401);

    expect((await call("DELETE", "/api/users/carol", ADMIN)).status).toBe(204);
    expect((await call("GET", "/api/me", basic("carol", "carol picks another"))).status).toBe(401);
    expect((await call("DELETE", "/api/users/carol", ADMIN)).status).toBe(404);
  });

  it("explains what is wrong with the input", async () => {
    const { call } = await build();
    const refused = async (b: unknown) => { const r = await call("POST", "/api/users", ADMIN, b); return [r.status, (await r.json()).error]; };
    expect(await refused({ name: "admin", password: "long enough password" })).toEqual([400, expect.stringMatching(/reserved/)]);
    expect(await refused({ name: "dave", password: "short" })).toEqual([400, expect.stringMatching(/at least 12/)]);
    expect(await refused({ name: "dave", password: "long enough password", permissions: ["root"] })).toEqual([400, expect.stringMatching(/permissions must be/)]);
    expect(await refused({ name: "alice", password: "long enough password" })).toEqual([409, expect.stringMatching(/exists/)]);
  });

  it("leaves the built-in administrator alone", async () => {
    const { call } = await build();
    expect((await call("PATCH", "/api/users/admin", ADMIN, { password: "something long enough" })).status).toBe(400);
    expect((await call("DELETE", "/api/users/admin", ADMIN)).status).toBe(400);
  });

  it("lets everybody change their own password, but not the administrator's", async () => {
    const { call } = await build();
    expect((await call("PUT", "/api/me/password", basic("alice", "alice has apps rights"), { password: "alice has a new one" })).status).toBe(204);
    expect((await call("GET", "/api/me", basic("alice", "alice has apps rights"))).status).toBe(401);
    expect((await call("GET", "/api/me", basic("alice", "alice has a new one"))).status).toBe(200);
    expect((await call("PUT", "/api/me/password", basic("alice", "alice has a new one"), { password: "short" })).status).toBe(400);
    expect((await call("PUT", "/api/me/password", ADMIN, { password: "something long enough" })).status).toBe(400);
  });
});
