import { createHash } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { createApp } from "./app.js";
import { applier } from "./apply.js";
import { authenticator } from "./auth.js";
import { loadConfig, type OidcConfig } from "./config.js";
import { createOidc, cookieValue, SESSION_COOKIE } from "./oidc.js";
import { testCreations, testHosts, testRemoval, testStacks } from "./testing.js";
import { userStore } from "./users.js";

const ISSUER = "https://id.example";
const cfg: OidcConfig = { issuer: ISSUER, clientId: "homegraf", clientSecret: "client-secret", publicUrl: "https://map.example", scopes: "openid profile", usernameClaim: "preferred_username", label: "id.example", sessionHours: 8 };
const jwt = (claims: object) => ["e30", Buffer.from(JSON.stringify(claims)).toString("base64url"), "sig"].join(".");

/** A provider that answers discovery, the token endpoint (checking PKCE and the client) and userinfo. */
function provider(change: { claims?: Record<string, unknown>; token?: Response; userinfo?: Record<string, unknown> } = {}) {
  const seen = { nonce: "", challenge: "", tokens: [] as URLSearchParams[], userinfo: 0 };
  const fetchFn = (async (url: string, init?: RequestInit) => {
    if (url === `${ISSUER}/.well-known/openid-configuration`) {
      return new Response(JSON.stringify({ issuer: ISSUER, authorization_endpoint: `${ISSUER}/authorize`, token_endpoint: `${ISSUER}/token`, userinfo_endpoint: `${ISSUER}/userinfo` }));
    }
    if (url === `${ISSUER}/token`) {
      const body = new URLSearchParams(String(init?.body));
      seen.tokens.push(body);
      if (change.token) return change.token;
      const ok = body.get("code") === "good-code" && body.get("client_secret") === "client-secret" && body.get("grant_type") === "authorization_code"
        && createHash("sha256").update(body.get("code_verifier") ?? "").digest("base64url") === seen.challenge;
      if (!ok) return new Response("{}", { status: 400 });
      const claims = { iss: ISSUER, aud: "homegraf", exp: Math.floor(Date.now() / 1000) + 600, sub: "u-1", nonce: seen.nonce, preferred_username: "Alice", ...change.claims };
      return new Response(JSON.stringify({ id_token: jwt(claims), access_token: "at-1" }));
    }
    if (url === `${ISSUER}/userinfo`) {
      seen.userinfo++;
      return new Response(JSON.stringify(change.userinfo ?? {}));
    }
    return new Response("", { status: 404 });
  }) as unknown as typeof fetch;
  return { seen, fetchFn };
}

function setup(change: Parameters<typeof provider>[0] = {}, now: () => number = Date.now, config: OidcConfig = cfg, admin?: string) {
  const users = userStore(join(mkdtempSync(join(tmpdir(), "homegraf-")), "users.json"));
  const p = provider(change);
  const oidc = createOidc({ cfg: config, users, admin, fetchFn: p.fetchFn, now });
  /** Starts a sign-in like a browser and comes back with `code`. */
  async function signIn(code = "good-code", overrides: { cookie?: string; state?: string } = {}) {
    const { location, stateCookie } = await oidc.login();
    const q = new URL(location).searchParams;
    p.seen.nonce = q.get("nonce")!;
    p.seen.challenge = q.get("code_challenge")!;
    const state = q.get("state")!;
    return oidc.callback({ code, state: overrides.state ?? state }, overrides.cookie ?? cookieValue(stateCookie, "hg_oidc"));
  }
  return { users, p, oidc, signIn };
}

describe("starting a sign-in", () => {
  it("sends the browser to the provider with PKCE, state and nonce, and ties the answer to this browser by a cookie", async () => {
    const { oidc } = setup();
    const { location, stateCookie } = await oidc.login();
    const url = new URL(location);
    expect(`${url.origin}${url.pathname}`).toBe(`${ISSUER}/authorize`);
    expect(Object.fromEntries(url.searchParams)).toMatchObject({
      response_type: "code", client_id: "homegraf", redirect_uri: "https://map.example/api/auth/callback", scope: "openid profile", code_challenge_method: "S256",
    });
    expect(url.searchParams.get("state")).toHaveLength(43);
    expect(stateCookie).toContain(`hg_oidc=${url.searchParams.get("state")}`);
    expect(stateCookie).toMatch(/Path=\/api\/auth; Max-Age=600; HttpOnly; SameSite=Lax; Secure$/);
  });

  it("says why when the provider cannot be reached or is somebody else", async () => {
    const users = userStore(join(mkdtempSync(join(tmpdir(), "homegraf-")), "users.json"));
    await expect(createOidc({ cfg, users, fetchFn: (async () => new Response("", { status: 502 })) as unknown as typeof fetch }).login()).rejects.toThrow(/HTTP 502/);
    const other = (async () => new Response(JSON.stringify({ issuer: "https://other.example", authorization_endpoint: "x", token_endpoint: "y" }))) as unknown as typeof fetch;
    await expect(createOidc({ cfg, users, fetchFn: other }).login()).rejects.toThrow(/not https:\/\/id\.example/);
  });
});

describe("coming back from the provider", () => {
  it("trades the code (with the verifier of PKCE and the client's secret) for a session of the lower-cased user name", async () => {
    const { oidc, signIn, p } = setup();
    const result = await signIn();
    expect(result).toMatchObject({ ok: true, maxAgeSeconds: 8 * 3600 });
    expect(p.seen.tokens[0].get("redirect_uri")).toBe("https://map.example/api/auth/callback");
    const session = oidc.principal((result as { sessionId: string }).sessionId);
    expect(session?.principal).toEqual({ name: "alice", permissions: [], source: "sso" });
  });

  it("refuses an answer that does not belong to this browser, and a state used twice", async () => {
    const { signIn, oidc } = setup();
    expect(await signIn("good-code", { cookie: "another" })).toMatchObject({ ok: false, error: expect.stringMatching(/did not start in this browser/) });
    expect(await signIn("good-code", { cookie: undefined, state: "invented" })).toMatchObject({ ok: false });
    const { location, stateCookie } = await oidc.login();
    const state = new URL(location).searchParams.get("state")!;
    const cookie = cookieValue(stateCookie, "hg_oidc");
    await oidc.callback({ code: "x", state }, cookie);
    expect(await oidc.callback({ code: "good-code", state }, cookie)).toMatchObject({ ok: false, error: expect.stringMatching(/start it again/) });
  });

  it("refuses a sign-in that took longer than ten minutes", async () => {
    let now = 1_000_000;
    const { oidc } = setup({}, () => now);
    const { location, stateCookie } = await oidc.login();
    now += 11 * 60_000;
    expect(await oidc.callback({ code: "good-code", state: new URL(location).searchParams.get("state")! }, cookieValue(stateCookie, "hg_oidc"))).toMatchObject({ ok: false });
  });

  it("passes on what the provider says when it refuses or the token endpoint fails", async () => {
    expect(await setup().oidc.callback({ error: "access_denied" }, undefined)).toEqual({ ok: false, error: "the provider refused the sign-in (access_denied)" });
    expect(await setup({ token: new Response("", { status: 500 }) }).signIn()).toMatchObject({ ok: false, error: expect.stringMatching(/HTTP 500/) });
    expect(await setup({ token: new Response(JSON.stringify({ access_token: "at" })) }).signIn()).toMatchObject({ ok: false, error: expect.stringMatching(/no id token/) });
    expect(await setup().signIn("wrong-code")).toMatchObject({ ok: false, error: expect.stringMatching(/HTTP 400/) });
  });

  it.each([
    ["another issuer", { iss: "https://evil.example" }, /another issuer/],
    ["another client", { aud: "somebody-else" }, /another client/],
    ["an expired token", { exp: 1 }, /expired/],
    ["a foreign nonce", { nonce: "not-mine" }, /does not belong to this sign-in/],
    ["no subject", { sub: "" }, /no subject/],
  ])("checks the id token: %s", async (_why, claims, error) => {
    expect(await setup({ claims }).signIn()).toMatchObject({ ok: false, error: expect.stringMatching(error) });
  });

  it("accepts a list of audiences that holds the client", async () => {
    expect(await setup({ claims: { aud: ["other", "homegraf"] } }).signIn()).toMatchObject({ ok: true });
  });

  it("asks userinfo for the name when the token does not carry it", async () => {
    const { signIn, p, oidc } = setup({ claims: { preferred_username: undefined }, userinfo: { preferred_username: "Bob" } });
    const result = await signIn();
    expect(p.seen.userinfo).toBe(1);
    expect(oidc.principal((result as { sessionId: string }).sessionId)?.principal.name).toBe("bob");
    expect(await setup({ claims: { preferred_username: undefined } }).signIn()).toMatchObject({ ok: false, error: expect.stringMatching(/no "preferred_username"/) });
  });

  it("never lets a provider account become the built-in administrator, and refuses names it cannot use", async () => {
    expect(await setup({ claims: { preferred_username: "Admin" } }).signIn()).toMatchObject({ ok: false, error: expect.stringMatching(/reserved/) });
    expect(await setup({ claims: { preferred_username: "Boss" } }, Date.now, cfg, "boss").signIn()).toMatchObject({ ok: false, error: expect.stringMatching(/"boss" is reserved/) });
    expect(await setup({ claims: { preferred_username: "a" } }).signIn()).toMatchObject({ ok: false, error: expect.stringMatching(/not a name/) });
    expect(await setup({ claims: { preferred_username: "x y" } }).signIn()).toMatchObject({ ok: false });
  });
});

describe("what a session may do", () => {
  it("is what the Users tab says for that name, read on every request", async () => {
    const { users, signIn, oidc } = setup();
    await users.addSso("alice", ["apps:create"]);
    const id = ((await signIn()) as { sessionId: string }).sessionId;
    expect(oidc.principal(id)?.principal.permissions).toEqual(["apps:create"]);
    await users.update("alice", { permissions: ["hosts:edit"] });
    expect(oidc.principal(id)?.principal.permissions).toEqual(["hosts:edit"]);
    users.remove("alice");
    expect(oidc.principal(id)?.principal.permissions).toEqual([]);
  });

  it("is nothing for a name that is unknown, and nothing for a name that belongs to a password user", async () => {
    const { users, signIn, oidc } = setup();
    expect(oidc.principal(((await signIn()) as { sessionId: string }).sessionId)?.principal.permissions).toEqual([]);
    await users.add("alice", "a long enough password", ["users:manage"]);
    expect(oidc.principal(((await signIn()) as { sessionId: string }).sessionId)?.principal.permissions).toEqual([]);
  });

  it("binds the entry to the provider's account at the first sign-in and refuses another account with that name later", async () => {
    const { users, signIn } = setup();
    await users.addSso("alice", []);
    await signIn();
    expect(users.get("alice")?.sub).toBe("u-1");
    expect(await signIn()).toMatchObject({ ok: true });
    const rebound = setup({ claims: { sub: "u-2" } });
    await rebound.users.addSso("alice", []);
    rebound.users.bind("alice", "u-1");
    expect(await rebound.signIn()).toMatchObject({ ok: false, error: expect.stringMatching(/bound to another account/) });
  });

  it("ends after the session hours, and at the sign-out", async () => {
    let now = 1_000_000;
    const { signIn, oidc } = setup({}, () => now);
    const id = ((await signIn()) as { sessionId: string }).sessionId;
    now += 7 * 3_600_000;
    expect(oidc.principal(id)).toBeDefined();
    now += 2 * 3_600_000;
    expect(oidc.principal(id)).toBeUndefined();
    now = 1_000_000;
    const again = ((await signIn()) as { sessionId: string }).sessionId;
    oidc.logout(again);
    expect(oidc.principal(again)).toBeUndefined();
    expect(oidc.principal(undefined)).toBeUndefined();
  });

  it("sets its cookies without Secure for an address that is not https", async () => {
    const { oidc } = setup({}, Date.now, { ...cfg, publicUrl: "http://map.lan:8097" });
    expect(oidc.sessionCookie("id", 60)).toBe("hg_session=id; Path=/; Max-Age=60; HttpOnly; SameSite=Lax");
    expect(oidc.clearSessionCookie()).toContain("Max-Age=0");
  });
});

describe("the cookie header", () => {
  it("finds one cookie among several", () => {
    expect(cookieValue("a=1; hg_session=abc=; b=2", "hg_session")).toBe("abc=");
    expect(cookieValue(undefined, "x")).toBeUndefined();
  });
});

describe("the routes", () => {
  const env = {
    KOMODO_URL: "http://komodo:9120", KOMODO_API_KEY: "kkey", KOMODO_API_SECRET: "ksecret",
    HOMEGRAF_PASSWORD: "pw-1234", HOMEGRAF_CLONE_URL: "https://git.example/o/r.git", FORGEJO_TOKEN: "ftoken",
    INFISICAL_URL: "https://inf.example", INFISICAL_BOOTSTRAP_CLIENT_ID: "iid", INFISICAL_BOOTSTRAP_CLIENT_SECRET: "isecret",
  };
  async function build(withSso = true) {
    const { users, oidc, signIn, p } = setup();
    await users.addSso("carol", ["users:manage"]);
    await users.addSso("erin", []);
    await users.add("pat", "pat has a long password", ["users:manage"]);
    const app = createApp({
      loadGraph: async () => { throw new Error("unused"); },
      status: { get: () => ({ updated: null, source: "disabled", nodes: {} }), subscribe: () => () => {} },
      webDir: "dist/web", plan: async () => { throw new Error("unused"); }, preview: async () => { throw new Error("unused"); },
      addons: async () => [], templates: async () => [], unifi: () => ({ enabled: false }),
      certificates: { get: async () => ({ updated: null, certificates: [] }) },
      create: { problem: undefined, applier: applier({ cfg: loadConfig(env), spawn: async () => 0, exists: () => true, login: async () => "o" }) },
      users: { enabled: true, store: users, authenticate: authenticator({ adminPassword: "pw-1234", users }) },
      removal: testRemoval(), creations: testCreations(), hosts: testHosts(), stacks: testStacks(),
      sso: withSso ? { oidc } : { problem: "single sign-on needs OIDC_ISSUER as well" },
    });
    /** Goes through /login and /callback like a browser and returns the cookies it ends up with. */
    async function browser() {
      p.seen.nonce = "";
      const login = await app.request("/api/auth/login");
      const location = new URL(login.headers.get("location")!);
      const stateCookie = login.headers.get("set-cookie")!.split(";")[0];
      p.seen.nonce = location.searchParams.get("nonce")!;
      p.seen.challenge = location.searchParams.get("code_challenge")!;
      const back = await app.request(`/api/auth/callback?code=good-code&state=${location.searchParams.get("state")}`, { headers: { cookie: stateCookie } });
      return { login, back, cookies: back.headers.getSetCookie() };
    }
    return { app, users, browser, p };
  }

  it("says whether single sign-on is on, and why not when it is half set up", async () => {
    expect(await (await (await build(false)).app.request("/api/auth/session")).json()).toEqual({ enabled: false, problem: "single sign-on needs OIDC_ISSUER as well" });
    const { app } = await build();
    expect(await (await app.request("/api/auth/session")).json()).toEqual({ enabled: true, label: "id.example" });
    expect((await (await build(false)).app.request("/api/auth/login")).status).toBe(404);
  });

  it("sends the browser away with a state cookie, and back to the page with a session cookie", async () => {
    const { browser } = await build();
    const { login, back, cookies } = await browser();
    expect(login.status).toBe(302);
    expect(login.headers.get("location")).toMatch(/^https:\/\/id\.example\/authorize\?/);
    expect(back.status).toBe(302);
    expect(back.headers.get("location")).toBe("/");
    expect(cookies.some((c) => c.startsWith("hg_oidc=;") && c.includes("Max-Age=0"))).toBe(true);
    expect(cookies.some((c) => c.startsWith("hg_session=") && c.includes("HttpOnly"))).toBe(true);
  });

  it("brings a failed sign-in back to the page with the reason", async () => {
    const { app } = await build();
    const res = await app.request("/api/auth/callback?code=x&state=nope");
    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toMatch(/^\/\?sso_error=.*#users$/);
    expect(decodeURIComponent(res.headers.get("location")!)).toContain("start it again");
  });

  async function signedIn(name = "carol") {
    const b = await build();
    // the provider's account is "alice" in the fake; the entry the session resolves to is the one with that name
    await b.users.addSso("alice", name === "carol" ? ["users:manage"] : []);
    const { cookies } = await b.browser();
    const session = cookies.find((c) => c.startsWith("hg_session="))!.split(";")[0];
    const who = await (await b.app.request("/api/auth/session", { headers: { cookie: session } })).json();
    return { ...b, session, csrf: who.user.csrf as string, who };
  }

  it("shows who is signed in, with the token that write requests send back", async () => {
    const { who } = await signedIn();
    expect(who).toMatchObject({ enabled: true, user: { name: "alice", permissions: ["users:manage"] } });
    expect(who.user.csrf).toHaveLength(32);
  });

  it("lets a session read, and write only with the token and the permission", async () => {
    const { app, session, csrf } = await signedIn();
    const post = (headers: Record<string, string>) => app.request("/api/users", { method: "POST", body: JSON.stringify({ name: "dave", sso: true, permissions: [] }), headers: { "content-type": "application/json", cookie: session, ...headers } });
    expect((await app.request("/api/users", { headers: { cookie: session } })).status).toBe(200);
    const noToken = await post({});
    expect(noToken.status).toBe(403);
    expect((await noToken.json()).error).toMatch(/session token/);
    expect((await post({ "x-csrf-token": "guess" })).status).toBe(403);
    const added = await post({ "x-csrf-token": csrf });
    expect(added.status).toBe(201);
    expect(await added.json()).toMatchObject({ name: "dave", sso: true });
  });

  it("turns a session without the permission away", async () => {
    const { app, session, csrf } = await signedIn("erin");
    const res = await app.request("/api/users", { method: "POST", body: JSON.stringify({ name: "dave", sso: true }), headers: { "content-type": "application/json", cookie: session, "x-csrf-token": csrf } });
    expect(res.status).toBe(403);
    expect((await res.json()).error).toMatch(/needs users:manage/);
  });

  it("does not let a session change a password it does not have", async () => {
    const { app, session, csrf } = await signedIn();
    const res = await app.request("/api/me/password", { method: "PUT", body: JSON.stringify({ password: "a new long password" }), headers: { "content-type": "application/json", cookie: session, "x-csrf-token": csrf } });
    expect(res.status).toBe(400);
    expect((await res.json()).error).toMatch(/identity provider/);
  });

  it("ends the session at the sign-out", async () => {
    const { app, session } = await signedIn();
    const out = await app.request("/api/auth/logout", { method: "POST", headers: { cookie: session } });
    expect(out.status).toBe(204);
    expect(out.headers.get("set-cookie")).toContain("Max-Age=0");
    expect((await app.request("/api/users", { headers: { cookie: session } })).status).toBe(401);
  });

  it("still takes a name and a password, with or without a session cookie", async () => {
    const { app } = await build();
    const basic = `Basic ${Buffer.from("pat:pat has a long password").toString("base64")}`;
    expect((await app.request("/api/users", { headers: { authorization: basic } })).status).toBe(200);
    expect((await app.request("/api/users", { headers: { authorization: basic, cookie: `${SESSION_COOKIE}=stale` } })).status).toBe(200);
    expect((await app.request("/api/users")).status).toBe(401);
  });

  it("has no password for an entry that signs in through the provider, and cannot add one without single sign-on", async () => {
    const { app, users } = await build();
    const basic = `Basic ${Buffer.from("carol:anything at all, long").toString("base64")}`;
    expect((await app.request("/api/users", { headers: { authorization: basic } })).status).toBe(401);
    await expect(users.update("carol", { password: "a new long password" })).rejects.toThrow(/no password/);
    const off = await build(false);
    const admin = `Basic ${Buffer.from("admin:pw-1234").toString("base64")}`;
    const res = await off.app.request("/api/users", { method: "POST", body: JSON.stringify({ name: "dave", sso: true }), headers: { "content-type": "application/json", authorization: admin } });
    expect(res.status).toBe(400);
  });
});
