import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { createApp } from "./app.js";
import { applier, type Spawn } from "./apply.js";
import { authenticator } from "./auth.js";
import { loadConfig } from "./config.js";
import { caBundlePath, describeBundle, rootCaSource } from "./rootCa.js";
import { signedInReads, testCreations, testHosts, testRemoval, testStacks } from "./testing.js";
import { userStore } from "./users.js";
import type { HostInfo } from "../shared/types.js";

const CA = `-----BEGIN CERTIFICATE-----
MIIBqTCCAU+gAwIBAgIUfxP8SBXRDX9sU7Q4DTWM6Xic738wCgYIKoZIzj0EAwIw
KjERMA8GA1UECgwIVGVzdCBMYWIxFTATBgNVBAMMDFRlc3QgUm9vdCBDQTAeFw0y
NjExMDExNzA0MzlaFw0zNjEwMjkxNzA0MzlaMCoxETAPBgNVBAoMCFRlc3QgTGFi
MRUwEwYDVQQDDAxUZXN0IFJvb3QgQ0EwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNC
AAR1sbtfgH+vdOI5IhrXq2hQtx6VPWV7WTz0k+tDlAeLN5guJy/Q18vLoEJaGFEo
Tw+vT3yDafoUoqLX+Shl+65Ao1MwUTAdBgNVHQ4EFgQUfjXZNx2QepzmXs3FUtNt
df0fqkAwHwYDVR0jBBgwFoAUfjXZNx2QepzmXs3FUtNtdf0fqkAwDwYDVR0TAQH/
BAUwAwEB/zAKBggqhkjOPQQDAgNIADBFAiEAiMvysFh5G3PmkaCIq/D57kMxmxqk
vj81VLGw6n6eBukCIE6RweY1MWqwTaSSes8hcexkF0lZa33/bPnLBJ0ftSiZ
-----END CERTIFICATE-----
`;
const FINGERPRINT = "94:B9:7D:FB:04:24:3D:2C:10:B8:07:EF:E5:DD:23:9C:72:9B:AE:EF:DB:9C:AC:B4:36:50:B2:94:F9:1D:D4:67";

const env = {
  KOMODO_URL: "http://komodo:9120", KOMODO_API_KEY: "kkey", KOMODO_API_SECRET: "ksecret",
  HOMEGRAF_PASSWORD: "pw-1234", HOMEGRAF_CLONE_URL: "https://git.example/owner/infrastructure.git", FORGEJO_TOKEN: "ftoken",
  INFISICAL_URL: "https://inf.example", INFISICAL_BOOTSTRAP_CLIENT_ID: "iid", INFISICAL_BOOTSTRAP_CLIENT_SECRET: "isecret",
};
const cfg = loadConfig(env);
const basic = (name: string, password: string) => `Basic ${Buffer.from(`${name}:${password}`).toString("base64")}`;

const repoWith = (ingress: string, files: Record<string, string> = {}) => {
  const dir = mkdtempSync(join(tmpdir(), "homegraf-repo-"));
  mkdirSync(join(dir, "inventory"));
  writeFileSync(join(dir, "inventory/ingress.yaml"), ingress);
  for (const [name, text] of Object.entries(files)) writeFileSync(join(dir, name), text);
  return dir;
};
const ISSUERS = (bundle: string) => `issuers:\n  homelab-private-ca:\n    type: infisical\n    ca_bundle: ${bundle}   # Kette zum Prüfen\n`;

describe("the CA certificate", () => {
  it("is described by subject, end, fingerprint and whether it is a root", () => {
    expect(describeBundle(CA)).toEqual([{ subject: "Test Root CA", issuer: "Test Root CA", notAfter: "2036-10-29T17:04:39.000Z", fingerprint: FINGERPRINT, root: true }]);
    expect(describeBundle(`${CA}\n${CA}`)).toHaveLength(2);
    expect(describeBundle("nothing here")).toEqual([]);
  });

  it("is found through ca_bundle of the issuer, and only inside the repository", () => {
    const dir = repoWith(ISSUERS("homelab-root-ca.pem"));
    expect(caBundlePath(dir)).toBe(join(dir, "homelab-root-ca.pem"));
    expect(caBundlePath(repoWith(ISSUERS("certs/ca.pem")))).toMatch(/certs\/ca\.pem$/);
    for (const bad of ["../secret.pem", "/etc/passwd", "a/../../x"]) expect(caBundlePath(repoWith(ISSUERS(bad)))).toBeUndefined();
    expect(caBundlePath(repoWith("issuers: {}\n"))).toBeUndefined();
    expect(caBundlePath(join(dir, "missing"))).toBeUndefined();
  });

  it("is read from the repository, and null while the file is missing or holds no certificate", () => {
    expect(rootCaSource(repoWith(ISSUERS("ca.pem"), { "ca.pem": CA }))()?.info.certificates[0].fingerprint).toBe(FINGERPRINT);
    expect(rootCaSource(repoWith(ISSUERS("ca.pem")))()).toBeNull();
    expect(rootCaSource(repoWith(ISSUERS("ca.pem"), { "ca.pem": "junk" }))()).toBeNull();
  });
});

const TDARR: HostInfo = { id: "tdarr", stack: "external", file: "external/ingress.yaml", domains: ["tdarr.lab.example", "tdarr2.lab.example"], forceHttps: false };
const PVE: HostInfo = { id: "pve", stack: "external", file: "external/ingress.yaml", domains: ["pve.lab.example"], forceHttps: true };
const PR = "https://git.example/owner/infrastructure/pulls/12";

async function build(rootCa: ReturnType<typeof rootCaSource> = () => null) {
  const calls: { cmd: string; args: string[] }[] = [];
  const spawn: Spawn = async (cmd, args, _o, onData) => { calls.push({ cmd, args }); if (cmd === "python3") onData(`pull request: ${PR}\n`); return 0; };
  const users = userStore(join(mkdtempSync(join(tmpdir(), "homegraf-")), "users.json"));
  await users.add("alice", "alice may edit hosts", ["hosts:edit"]);
  await users.add("bob", "bob may only create", ["apps:create"]);
  const app = signedInReads(createApp({
    loadGraph: async () => { throw new Error("unused"); },
    status: { get: () => ({ updated: null, source: "disabled", nodes: {} }), subscribe: () => () => {} },
    webDir: "dist/web", plan: async () => { throw new Error("unused"); }, preview: async () => { throw new Error("unused"); },
    addons: async () => [], templates: async () => [], unifi: () => ({ enabled: false }),
    certificates: { get: async () => ({ updated: null, certificates: [] }) },
    create: { problem: undefined, applier: applier({ cfg, spawn, exists: () => true, login: async () => "owner" }) },
    users: { enabled: true, store: users, authenticate: authenticator({ adminPassword: "pw-1234", users }) },
    removal: testRemoval(), creations: testCreations(), hosts: testHosts([TDARR, PVE], rootCa), stacks: testStacks(),
  }), "pw-1234");
  const post = (body: unknown, auth?: string) => app.request("/api/hosts/force-https", {
    method: "POST", body: JSON.stringify(body), headers: { "content-type": "application/json", ...(auth ? { authorization: auth } : {}) },
  });
  return { app, post, calls, settle: async (jobId: string) => (await app.request(`/api/apps/jobs/${jobId}/events`)).text() };
}

describe("downloading the CA certificate", () => {
  const withCa = rootCaSource(repoWith(ISSUERS("ca.pem"), { "ca.pem": CA }));

  it("serves the file and what is in it", async () => {
    const { app } = await build(withCa);
    const pem = await app.request("/api/ca.pem");
    expect(pem.status).toBe(200);
    expect(pem.headers.get("content-disposition")).toBe('attachment; filename="homelab-root-ca.pem"');
    expect(await pem.text()).toBe(CA);
    expect(await (await app.request("/api/ca")).json()).toMatchObject({ certificates: [{ subject: "Test Root CA", fingerprint: FINGERPRINT, root: true }] });
  });

  it("is a 404 with a reason while the repository has none", async () => {
    const { app } = await build();
    for (const path of ["/api/ca", "/api/ca.pem"]) {
      const res = await app.request(path);
      expect(res.status).toBe(404);
      expect((await res.json()).error).toMatch(/no CA certificate/);
    }
  });
});

describe("the redirect to HTTPS of a host", () => {
  const alice = basic("alice", "alice may edit hosts");

  it("lists the hosts", async () => {
    const { app } = await build();
    expect(await (await app.request("/api/hosts")).json()).toEqual([TDARR, PVE]);
  });

  it("needs hosts:edit", async () => {
    const { post, calls } = await build();
    const body = { domain: "tdarr.lab.example", enabled: true };
    expect((await post(body)).status).toBe(401);
    expect((await post(body, basic("alice", "nope"))).status).toBe(401);
    const bob = await post(body, basic("bob", "bob may only create"));
    expect(bob.status).toBe(403);
    expect((await bob.json()).error).toMatch(/needs hosts:edit/);
    expect(calls).toEqual([]);
  });

  it("refuses what makes no sense", async () => {
    const { post, calls } = await build();
    expect((await post({ domain: "tdarr.lab.example" }, alice)).status).toBe(400);
    expect((await post({ domain: "tdarr.lab.example", enabled: "yes" }, alice)).status).toBe(400);
    expect((await post({ domain: "nope.lab.example", enabled: true }, alice)).status).toBe(404);
    expect((await post({ domain: "pve.lab.example", enabled: true }, alice)).status).toBe(409);
    expect(calls).toEqual([]);
  });

  it("opens a pull request for any of the host's domains and names who asked", async () => {
    const { post, calls, settle } = await build();
    const res = await post({ domain: "tdarr2.lab.example", enabled: true }, alice);
    expect(res.status).toBe(202);
    expect(await settle((await res.json()).jobId)).toContain(PR);
    const tool = calls.find((c) => c.cmd === "python3")!;
    expect(tool.args.slice(1)).toEqual(["/work/homelab.py", "force-https", "tdarr2.lab.example", "on", "--pr", "--yes"]);
  });

  it("can turn it off again", async () => {
    const { post, calls, settle } = await build();
    await settle((await (await post({ domain: "pve.lab.example", enabled: false }, alice)).json()).jobId);
    expect(calls.find((c) => c.cmd === "python3")!.args.slice(-3)).toEqual(["off", "--pr", "--yes"]);
  });
});
