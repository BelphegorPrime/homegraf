import { mkdtemp, readFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { loadConfig } from "./config.js";
import { unifiSource, type UnifiRun } from "./unifi.js";

const cfg = { url: "https://10.0.0.1", user: "reader", password: "s3cret-pw" };
const snapshot = JSON.stringify({ fetched: "2026-10-30T20:00:00+00:00", devices: [] });
const ok: UnifiRun = async () => ({ code: 0, out: snapshot, err: "" });

async function source(run: UnifiRun, extra: object = {}) {
  let t = 1_000;
  const dir = await mkdtemp(join(tmpdir(), "unifi-test-"));
  const s = unifiSource({ cfg, repoDir: "/repo", dir, run, ttlMs: 1000, staleMs: 5000, now: () => t, ...extra });
  return { s, dir, tick: (ms: number) => (t += ms) };
}

describe("config", () => {
  it("reads the controller from the environment, and is off without it", () => {
    expect(loadConfig({ UNIFI_URL: "https://udm/", UNIFI_USER: "u", UNIFI_PASSWORD: "p", UNIFI_FINGERPRINT: "ab" }).unifi).toEqual({
      url: "https://udm",
      user: "u",
      password: "p",
      verify: false,
      fingerprint: "ab",
    });
    expect(loadConfig({ UNIFI_URL: "https://udm", UNIFI_USER: "u" }).unifi).toBeUndefined();
  });
});

describe("unifiSource", () => {
  it("does nothing without a controller", async () => {
    const s = unifiSource({
      cfg: undefined,
      repoDir: "/repo",
      run: async () => {
        throw new Error("must not run");
      },
    });
    expect(await s.args()).toEqual([]);
    expect(s.status()).toEqual({ enabled: false, fetched: undefined, error: undefined });
  });

  it("reads the controller once, hands build.py the file, and asks again only after the time is up", async () => {
    let calls = 0;
    const { s, tick } = await source(async (f, a, e) => {
      calls++;
      return ok(f, a, e);
    });
    const args = await s.args();
    expect(args[0]).toBe("--unifi");
    expect(await readFile(args[1], "utf8")).toBe(snapshot);
    await s.args();
    expect(calls).toBe(1);
    tick(1001);
    await s.args();
    expect(calls).toBe(2);
    expect(s.status()).toMatchObject({ enabled: true, fetched: "2026-10-30T20:00:00+00:00", error: undefined });
  });

  it("gives the credentials to the one process that reads the controller and keeps the file private", async () => {
    let seen: { file: string; args: string[]; env: Record<string, string> } | undefined;
    const { s } = await source(async (file, args, env) => {
      seen = { file, args, env };
      return ok(file, args, env);
    });
    const [, file] = await s.args();
    expect(seen!.file).toBe("/repo/infra/graph/unifi.py");
    expect(seen!.args).toEqual(["--root", "/repo"]);
    expect(seen!.env).toMatchObject({ UNIFI_URL: "https://10.0.0.1", UNIFI_USER: "reader", UNIFI_PASSWORD: "s3cret-pw" });
    expect(seen!.env).not.toHaveProperty("KOMODO_API_SECRET");
    expect(((await stat(file)).mode & 0o777).toString(8)).toBe("600");
  });

  it("keeps the last good snapshot while a read fails, says why, and drops it when too old", async () => {
    let fail = false;
    const { s, tick } = await source(async (f, a, e) => (fail ? { code: 1, out: "", err: "error: reader s3cret-pw not reachable" } : ok(f, a, e)));
    expect((await s.args())[0]).toBe("--unifi");
    fail = true;
    tick(2000);
    expect((await s.args())[0]).toBe("--unifi"); // the failed read does not take the map away
    expect(s.status().error).toBe("error: reader *** not reachable"); // and the password is not in the message
    tick(5000);
    expect(await s.args()).toEqual([]); // too old: back to the placeholders
    expect(s.status()).toMatchObject({ enabled: true, fetched: undefined });
  });

  it("without any snapshot a failed read means placeholders and an error", async () => {
    const { s } = await source(async () => ({ code: 2, out: "", err: "error: UNIFI_URL is not set\n" }));
    expect(await s.args()).toEqual([]);
    expect(s.status().error).toBe("error: UNIFI_URL is not set");
  });

  it("asks once for graphs that load at the same time", async () => {
    let calls = 0;
    const { s } = await source(async (f, a, e) => {
      calls++;
      await new Promise((r) => setTimeout(r, 20));
      return ok(f, a, e);
    });
    await Promise.all([s.args(), s.args(), s.args()]);
    expect(calls).toBe(1);
  });
});
