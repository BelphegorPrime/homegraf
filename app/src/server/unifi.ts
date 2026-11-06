import { execFile } from "node:child_process";
import { mkdir, rename, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { UnifiConfig } from "./config.js";
import { toolPath } from "./tools.js";

export type UnifiRun = (file: string, args: string[], env: Record<string, string>) => Promise<{ code: number; out: string; err: string }>;

export const execUnifi: UnifiRun = (file, args, env) =>
  new Promise((resolve) => {
    const child = execFile("python3", ["-I", file, ...args], { env, timeout: 60_000, maxBuffer: 16 * 1024 * 1024 }, (err, stdout, stderr) =>
      resolve({ code: err ? (typeof err.code === "number" ? err.code : 1) : 0, out: stdout, err: stderr }),
    );
    child.stdin?.end();
  });

export interface UnifiStatus {
  enabled: boolean;
  /** When the data in use was read from the controller. */
  fetched?: string;
  error?: string;
}

/**
 * The UniFi snapshot for the graph builder. infra/graph/unifi.py reads the controller (read-only); the answer is kept
 * for `ttlMs` so the controller is not asked for every graph. If a read fails the last good one is used for up to
 * `staleMs`, then the map falls back to the placeholders. Credentials go to that one process, nowhere else.
 */
export function unifiSource(opts: { cfg?: UnifiConfig; repoDir: string; dir?: string; run?: UnifiRun; ttlMs?: number; staleMs?: number; now?: () => number }) {
  const { cfg, repoDir, dir = tmpdir(), run = execUnifi, ttlMs = 300_000, staleMs = 3_600_000, now = Date.now } = opts;
  let good: { file: string; at: number; fetched: string } | undefined;
  let tried = 0;
  let error: string | undefined;
  let inflight: Promise<void> | undefined;

  async function refresh() {
    tried = now();
    const env: Record<string, string> = {
      PATH: process.env.PATH ?? "",
      LANG: "C.UTF-8",
      PYTHONDONTWRITEBYTECODE: "1",
      UNIFI_URL: cfg!.url,
      UNIFI_USER: cfg!.user,
      UNIFI_PASSWORD: cfg!.password,
      ...(cfg!.verify ? { UNIFI_VERIFY: "true" } : {}),
      ...(cfg!.fingerprint ? { UNIFI_FINGERPRINT: cfg!.fingerprint } : {}),
    };
    const { code, out, err } = await run(toolPath(repoDir, "infra/graph/unifi.py"), ["--root", repoDir], env);
    if (code !== 0) {
      error = (err.trim().split("\n").pop() ?? `exit ${code}`).replace(cfg!.password, "***");
      return;
    }
    const fetched = (JSON.parse(out) as { fetched?: string }).fetched ?? new Date(now()).toISOString();
    await mkdir(dir, { recursive: true });
    const file = join(dir, "homegraf-unifi.json");
    await writeFile(`${file}.tmp`, out, { mode: 0o600 });
    await rename(`${file}.tmp`, file);
    good = { file, at: now(), fetched };
    error = undefined;
  }

  return {
    /** Arguments for build.py: `--unifi FILE` when there is a snapshot to use, else none (placeholders). */
    async args(): Promise<string[]> {
      if (!cfg) return [];
      if (!good || now() - tried >= ttlMs) {
        inflight ??= refresh()
          .catch((e) => {
            error = e instanceof Error ? e.message : String(e);
          })
          .finally(() => {
            inflight = undefined;
          });
        await inflight;
      }
      if (good && now() - good.at > staleMs) good = undefined;
      return good ? ["--unifi", good.file] : [];
    },
    status(): UnifiStatus {
      return { enabled: !!cfg, fetched: good?.fetched, error };
    },
  };
}
