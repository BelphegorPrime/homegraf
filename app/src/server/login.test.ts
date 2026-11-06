import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { createApp } from "./app.js";
import { applier } from "./apply.js";
import { authenticator } from "./auth.js";
import { loadConfig } from "./config.js";
import { testCreations, testHosts, testRemoval, testStacks } from "./testing.js";
import { userStore } from "./users.js";

async function build(enabled = true) {
  const store = userStore(join(mkdtempSync(join(tmpdir(), "homegraf-")), "users.json"));
  await store.add("alice", "alice has apps rights", ["apps:create"]);
  const app = createApp({
    loadGraph: async () => ({ generated: "now", nodes: [], edges: [], warnings: [] }),
    status: { get: () => ({ updated: null, source: "disabled", nodes: {} }), subscribe: () => () => {} },
    webDir: "dist/web",
    plan: async () => {
      throw new Error("unused");
    },
    preview: async () => {
      throw new Error("unused");
    },
    addons: async () => [],
    templates: async () => [],
    unifi: () => ({ enabled: false }),
    certificates: { get: async () => ({ updated: null, certificates: [] }) },
    create: { problem: undefined, applier: applier({ cfg: loadConfig({}) }) },
    users: { enabled, store, authenticate: authenticator({ adminPassword: enabled ? "pw-admin-1234" : undefined, users: store }) },
    removal: testRemoval(),
    creations: testCreations(),
    hosts: testHosts(),
    stacks: testStacks(),
  });
  const login = (name: string, password: string) =>
    app.request("/api/auth/password", { method: "POST", body: JSON.stringify({ name, password }), headers: { "content-type": "application/json" } });
  /** Signs in and returns the cookie to send back and the session's token for writes. */
  const session = async (name: string, password: string) => {
    const res = await login(name, password);
    const cookie = (res.headers.get("set-cookie") ?? "").split(";")[0];
    const { csrf } = (await (await app.request("/api/auth/session", { headers: { cookie } })).json()).user;
    return { res, cookie, csrf };
  };
  return { app, login, session };
}

describe("everything is behind the sign-in", () => {
  it("refuses every route but the health probe and the sign-in itself", async () => {
    const { app } = await build();
    for (const path of [
      "/api/graph",
      "/api/status",
      "/api/events",
      "/api/certificates",
      "/api/capabilities",
      "/api/ca",
      "/api/ca.pem",
      "/api/hosts",
      "/api/stacks",
      "/api/apps/removable",
      "/api/templates",
    ]) {
      expect((await app.request(path)).status, path).toBe(401);
    }
    expect((await app.request("/api/health")).status).toBe(200);
    expect((await app.request("/api/auth/session")).status).toBe(200);
  });

  it("lets a signed-in user through with the cookie alone", async () => {
    const { app, session } = await build();
    const { cookie } = await session("alice", "alice has apps rights");
    expect((await app.request("/api/graph", { headers: { cookie } })).status).toBe(200);
    expect((await app.request("/api/events", { headers: { cookie } })).status).toBe(200);
  });
});

describe("signing in with a name and a password", () => {
  it("opens a cookie session that cannot be read by scripts and tells the page who is signed in", async () => {
    const { session, app } = await build();
    const { res, cookie } = await session("admin", "pw-admin-1234");
    expect(res.status).toBe(200);
    expect(res.headers.get("set-cookie")).toMatch(/HttpOnly; SameSite=Lax/);
    const body = await (await app.request("/api/auth/session", { headers: { cookie } })).json();
    expect(body.user).toMatchObject({ name: "admin", source: "builtin" });
    expect(body.user.permissions).toContain("users:manage");
    expect(body.password).toEqual({ enabled: true });
  });

  it("marks the cookie Secure behind an HTTPS proxy", async () => {
    const { app } = await build();
    const res = await app.request("/api/auth/password", {
      method: "POST",
      body: JSON.stringify({ name: "alice", password: "alice has apps rights" }),
      headers: { "content-type": "application/json", "x-forwarded-proto": "https" },
    });
    expect(res.headers.get("set-cookie")).toMatch(/; Secure/);
  });

  it("refuses a wrong password without a cookie, and a body without the fields", async () => {
    const { login, app } = await build();
    const res = await login("alice", "nope");
    expect(res.status).toBe(401);
    expect(res.headers.get("set-cookie")).toBeNull();
    expect((await app.request("/api/auth/password", { method: "POST", body: "{}" })).status).toBe(400);
  });

  it("says why nobody can sign in while no password is set", async () => {
    const { app, login } = await build(false);
    expect((await login("admin", "x")).status).toBe(503);
    expect((await (await app.request("/api/auth/session")).json()).password).toEqual({ enabled: false, reason: "HOMEGRAF_PASSWORD is not set" });
  });

  it("asks for the session's token on writes, and applies the user's rights", async () => {
    const { app, session } = await build();
    const { cookie, csrf } = await session("alice", "alice has apps rights");
    const put = (headers: Record<string, string>) =>
      app.request("/api/me/password", {
        method: "PUT",
        body: JSON.stringify({ password: "another long password" }),
        headers: { "content-type": "application/json", cookie, ...headers },
      });
    expect((await put({})).status).toBe(403);
    expect((await put({ "x-csrf-token": "forged" })).status).toBe(403);
    expect((await app.request("/api/users", { headers: { cookie } })).status).toBe(403); // alice may not manage users
    expect((await put({ "x-csrf-token": csrf })).status).toBe(204);
  });

  it("ends the session at once when the user is removed, and on sign-out", async () => {
    const { app, session } = await build();
    const alice = await session("alice", "alice has apps rights");
    const admin = await session("admin", "pw-admin-1234");
    expect((await app.request("/api/users/alice", { method: "DELETE", headers: { cookie: admin.cookie, "x-csrf-token": admin.csrf } })).status).toBe(204);
    expect((await app.request("/api/graph", { headers: { cookie: alice.cookie } })).status).toBe(401);

    const out = await app.request("/api/auth/logout", { method: "POST", headers: { cookie: admin.cookie } });
    expect(out.headers.get("set-cookie")).toMatch(/Max-Age=0/);
    expect((await app.request("/api/graph", { headers: { cookie: admin.cookie } })).status).toBe(401);
  });

  it("still takes a name and password in the Authorization header, for scripts", async () => {
    const { app } = await build();
    const basic = `Basic ${Buffer.from("alice:alice has apps rights").toString("base64")}`;
    expect((await app.request("/api/graph", { headers: { authorization: basic } })).status).toBe(200);
  });
});
