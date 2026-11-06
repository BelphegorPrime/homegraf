import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { createApp } from "./app.js";
import { applier } from "./apply.js";
import { authenticator } from "./auth.js";
import { loadConfig } from "./config.js";
import { signedInReads, testCreations, testHosts, testRemoval, testStacks } from "./testing.js";
import { userStore } from "./users.js";

const appWith = (loadGraph: () => Promise<never>) => {
  const store = userStore(join(mkdtempSync(join(tmpdir(), "homegraf-")), "users.json"));
  return signedInReads(
    createApp({
      loadGraph,
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
      users: { enabled: true, store, authenticate: authenticator({ adminPassword: "pw", users: store }) },
      removal: testRemoval(),
      creations: testCreations(),
      hosts: testHosts(),
      stacks: testStacks(),
    }),
    "pw",
  );
};

describe("GET /api/graph when the graph cannot be built", () => {
  it("tells a new setup to mount its repository instead of showing a Python error", async () => {
    const res = await appWith(async () => {
      throw new Error(
        "Command failed: python3 -I /repo/infra/graph/build.py --root /repo\npython3: can't open file '/repo/infra/graph/build.py': [Errno 2] No such file or directory",
      );
    }).request("/api/graph");
    expect(res.status).toBe(502);
    const { error } = await res.json();
    expect(error).toMatch(/mount a checkout of your homelab repository at \/repo/);
    expect(error).not.toMatch(/python3|Errno/);
  });

  it("keeps every other failure as it is", async () => {
    const res = await appWith(async () => {
      throw new Error("boom");
    }).request("/api/graph");
    expect(res.status).toBe(502);
    expect((await res.json()).error).toBe("graph build failed: boom");
  });
});
