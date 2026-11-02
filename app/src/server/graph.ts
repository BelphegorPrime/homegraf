import { execFile } from "node:child_process";
import { promisify } from "node:util";
import type { Graph } from "../shared/types.js";
import { toolPath } from "./tools.js";

const run = promisify(execFile);

/** Runs infra/graph/build.py of the tools (the image's copy, so the builder always matches this app) on the repo checkout. */
export function graphLoader(repoDir: string, ttlMs: number, now: () => number = Date.now, extraArgs: () => Promise<string[]> = async () => []) {
  let cached: { at: number; graph: Graph } | undefined;
  let inflight: Promise<Graph> | undefined;

  async function build(): Promise<Graph> {
    const { stdout } = await run("python3", ["-I", toolPath(repoDir, "infra/graph/build.py"), "--root", repoDir, ...(await extraArgs())], {
      maxBuffer: 32 * 1024 * 1024,
      timeout: 30_000,
    });
    return JSON.parse(stdout) as Graph;
  }

  return async function load(): Promise<Graph> {
    if (cached && now() - cached.at < ttlMs) return cached.graph;
    inflight ??= build()
      .then((graph) => {
        cached = { at: now(), graph };
        return graph;
      })
      .finally(() => {
        inflight = undefined;
      });
    try {
      return await inflight;
    } catch (err) {
      if (cached) return cached.graph; // keep serving the last good graph while the checkout is mid-pull
      throw err;
    }
  };
}
