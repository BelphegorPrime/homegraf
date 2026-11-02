import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import type { JobKind, JobSnapshot } from "../shared/types.js";
import { newAppArgs, secretValueEnv, withWorkFiles, type PlanInput } from "./apps.js";
import type { Config } from "./config.js";
import { toolPath, toolRoot } from "./tools.js";

/** Runs a command in cwd with env, hands its output to onData and resolves with the exit code. */
export type Spawn = (cmd: string, args: string[], o: { cwd?: string; env: Record<string, string>; timeoutMs: number }, onData: (s: string) => void) => Promise<number>;

const ANSI = /\x1b\[[0-9;]*[A-Za-z]/g;
const PR_URL = /pull request: (https?:\/\/\S+)/;

export const nodeSpawn: Spawn = (cmd, args, { cwd, env, timeoutMs }, onData) =>
  new Promise((resolve) => {
    const child = spawn(cmd, args, { cwd, env, stdio: ["ignore", "pipe", "pipe"] });
    const timer = setTimeout(() => {
      onData(`\n! timed out after ${Math.round(timeoutMs / 1000)} s, stopping\n`);
      child.kill("SIGKILL");
    }, timeoutMs);
    child.stdout.on("data", (b: Buffer) => onData(b.toString()));
    child.stderr.on("data", (b: Buffer) => onData(b.toString()));
    child.on("error", (e) => { onData(`! ${e.message}\n`); clearTimeout(timer); resolve(127); });
    child.on("close", (code) => { clearTimeout(timer); resolve(code ?? 1); });
  });

export interface Job extends JobSnapshot {
  subscribers: Set<(chunk: string | null) => void>;
}

export interface ApplierDeps {
  cfg: Config;
  spawn?: Spawn;
  exists?: (path: string) => boolean;
  /** Login of the token's user, needed for git over https. */
  login?: () => Promise<string>;
}

async function forgejoLogin(cfg: Config): Promise<string> {
  const base = cfg.create.forgejoUrl ?? new URL(cfg.create.cloneUrl ?? "").origin;
  const res = await fetch(`${base.replace(/\/$/, "")}/api/v1/user`, { headers: { authorization: `token ${cfg.create.forgejoToken}` } });
  if (!res.ok) throw new Error(`Forgejo ${base}: HTTP ${res.status} for the token`);
  return ((await res.json()) as { login: string }).login;
}

/**
 * Runs `homelab.py new-app --pr --yes` in a dedicated clone, one job at a time, and keeps the output for the UI.
 * The clone is reset to origin/main before each run; the tool then puts the change on a branch and opens a pull request.
 */
export function applier({ cfg, spawn: run = nodeSpawn, exists = existsSync, login = () => forgejoLogin(cfg) }: ApplierDeps) {
  const jobs = new Map<string, Job>();
  let running: Job | undefined;
  const c = cfg.create;

  // Everything the tool and git need, nothing else; the container's own environment stays out.
  const baseEnv = (by?: string): Record<string, string> => ({
    PATH: process.env.PATH ?? "", LANG: "C.UTF-8", HOME: "/tmp", PYTHONDONTWRITEBYTECODE: "1", GIT_TERMINAL_PROMPT: "0",
    // The commit of the pull request names who asked for it (user names are letters, digits and . _ - only).
    GIT_AUTHOR_NAME: by ? `${by} via homegraf` : "homegraf", GIT_AUTHOR_EMAIL: "homegraf@localhost", GIT_COMMITTER_NAME: "homegraf", GIT_COMMITTER_EMAIL: "homegraf@localhost",
  });
  const toolEnv = (by?: string): Record<string, string> => ({
    ...baseEnv(by),
    KOMODO_URL: cfg.komodo?.url ?? "", KOMODO_API_KEY: cfg.komodo?.key ?? "", KOMODO_API_SECRET: cfg.komodo?.secret ?? "",
    INFISICAL_URL: c.infisical?.url ?? "", INFISICAL_BOOTSTRAP_CLIENT_ID: c.infisical?.clientId ?? "",
    INFISICAL_BOOTSTRAP_CLIENT_SECRET: c.infisical?.clientSecret ?? "",
    FORGEJO_TOKEN: c.forgejoToken ?? "", ...(c.forgejoUrl ? { FORGEJO_URL: c.forgejoUrl } : {}),
  });
  const secretValues = (typed: string[]) =>
    [c.password, c.forgejoToken, c.infisical?.clientSecret, cfg.komodo?.secret, cfg.komodo?.key, ...typed].filter((v): v is string => !!v);

  /** What a job does once the clone is ready: the tool call, who asked, and the typed values to keep out of the log. */
  interface Task {
    by?: string;
    typed: string[];
    tool: (ctx: { cwd: string; env: Record<string, string>; emit: (s: string) => void }) => Promise<number>;
    onDone?: (job: Job) => void;
    /** Runs from the read-only repository checkout and needs no clone, no Forgejo login (deploy). */
    readOnly?: boolean;
  }

  async function execute(job: Job, task: Task) {
    const scrub = (s: string) => secretValues(task.typed).reduce((out, v) => out.replaceAll(v, "***"), s.replace(ANSI, ""));
    const emit = (s: string) => {
      const clean = scrub(s);
      job.log += clean;
      job.pullRequestUrl ??= PR_URL.exec(clean)?.[1];
      job.subscribers.forEach((f) => f(clean));
    };
    try {
      if (task.readOnly) {
        const code = await task.tool({ cwd: cfg.repoDir, env: toolEnv(task.by), emit });
        job.exitCode = code;
        job.state = code === 0 ? "done" : "failed";
        if (code === 0) task.onDone?.(job);
        return;
      }
      // Git over https authenticates through GIT_CONFIG_*, so the token is in no command line and not in .git/config.
      const user = await login();
      const basic = Buffer.from(`${user}:${c.forgejoToken}`).toString("base64");
      const gitEnv = { ...baseEnv(), GIT_CONFIG_COUNT: "1", GIT_CONFIG_KEY_0: "http.extraHeader", GIT_CONFIG_VALUE_0: `Authorization: Basic ${basic}` };
      const git = (...args: string[]) => run("git", args, { env: gitEnv, timeoutMs: 120_000 }, emit);
      emit("── Preparing the repository clone ──\n");
      const steps = exists(`${c.workDir}/.git`)
        ? [["-C", c.workDir, "fetch", "-q", "origin"]]
        : [["clone", "-q", c.cloneUrl ?? "", c.workDir]];
      steps.push(["-C", c.workDir, "switch", "-q", "-f", "main"], ["-C", c.workDir, "reset", "-q", "--hard", "origin/main"], ["-C", c.workDir, "clean", "-fdq"]);
      for (const s of steps) {
        const code = await git(...s);
        if (code !== 0) throw new Error(`git ${s.filter((a) => a !== "-C" && a !== c.workDir)[0]} failed (exit ${code})`);
      }
      emit("\n");
      const code = await task.tool({ cwd: c.workDir, env: toolEnv(task.by), emit });
      job.exitCode = code;
      job.state = code === 0 ? "done" : "failed";
      if (code === 0) task.onDone?.(job);
    } catch (err) {
      emit(`\n✗ ${err instanceof Error ? err.message : err}\n`);
      job.exitCode ??= 1;
      job.state = "failed";
    } finally {
      running = undefined;
      job.subscribers.forEach((f) => f(null));
    }
  }

  function begin(kind: JobKind, appId: string, task: Task): Job | undefined {
    if (running) return undefined;
    const job: Job = { id: randomUUID(), kind, appId, requestedBy: task.by, state: "running", log: "", subscribers: new Set() };
    jobs.set(job.id, job);
    // Keep the last few jobs only.
    for (const id of [...jobs.keys()].slice(0, Math.max(0, jobs.size - 10))) jobs.delete(id);
    running = job;
    void execute(job, task);
    return job;
  }

  const removeArgs = (appId: string, ...rest: string[]) => ["-I", toolPath(c.workDir, "homelab.py"), "remove-app", "--app-id", appId, ...rest, "--yes"];

  return {
    /** Starts creating an app; undefined while another job runs. */
    start(input: PlanInput, requestedBy?: string, onDone?: (job: Job) => void): Job | undefined {
      return begin("create", input.appId, {
        by: requestedBy, onDone,
        typed: Object.values(input.secretValues),
        tool: ({ cwd, env, emit }) => {
          // Typed secret values travel in the environment (--secret VAR=env:NAME), the edited compose file as a temporary file.
          const typed = Object.fromEntries(Object.entries(input.secretValues).map(([k, v]) => [secretValueEnv(k), v]));
          return withWorkFiles(input, ({ composeFile, filesDir }) =>
            run("python3", ["-I", toolPath(cwd, "homelab.py"), ...newAppArgs(input, composeFile, filesDir), "--pr", "--yes"],
              { cwd, env: { ...env, ...toolRoot(cwd), ...typed }, timeoutMs: 15 * 60_000 }, emit));
        },
      });
    },
    /** First half of a removal: the stack goes now, the files go in a pull request. */
    startRemoval(appId: string, requestedBy?: string, onDone?: (job: Job) => void): Job | undefined {
      return begin("remove", appId, {
        by: requestedBy, typed: [], onDone,
        tool: ({ cwd, env, emit }) => run("python3", removeArgs(appId, "--pr"), { cwd, env: { ...env, ...toolRoot(cwd) }, timeoutMs: 20 * 60_000 }, emit),
      });
    },
    /** Second half, after the merge: NPM host, certificate and secrets. */
    finishRemoval(appId: string, server: string, requestedBy?: string, onDone?: (job: Job) => void): Job | undefined {
      return begin("finish", appId, {
        by: requestedBy, typed: [], onDone,
        tool: ({ cwd, env, emit }) => run("python3", removeArgs(appId, "--server", server, "--finish"), { cwd, env: { ...env, ...toolRoot(cwd) }, timeoutMs: 30 * 60_000 }, emit),
      });
    },
    /** Turns the HTTP to HTTPS redirect of a host on or off: the change to its manifest goes in a pull request. */
    startHostOption(domain: string, forceHttps: boolean, requestedBy?: string, onDone?: (job: Job) => void): Job | undefined {
      return begin("host", domain, {
        by: requestedBy, typed: [], onDone,
        tool: ({ cwd, env, emit }) => run("python3", ["-I", toolPath(c.workDir, "homelab.py"), "force-https", domain, forceHttps ? "on" : "off", "--pr", "--yes"], { cwd, env: { ...env, ...toolRoot(cwd) }, timeoutMs: 10 * 60_000 }, emit),
      });
    },
    /** Redeploys a Komodo stack (pull and up); changed secrets and bind-mounted files need it. */
    startDeploy(komodoStack: string, requestedBy?: string): Job | undefined {
      return begin("deploy", komodoStack, {
        by: requestedBy, typed: [], readOnly: true,
        tool: ({ cwd, env, emit }) => run("python3", ["-I", toolPath(cwd, "homelab.py"), "deploy", komodoStack, "--yes"], { cwd, env: { ...env, ...toolRoot(cwd) }, timeoutMs: 20 * 60_000 }, emit),
      });
    },
    get: (id: string) => jobs.get(id),
    snapshot: (j: Job): JobSnapshot => ({ id: j.id, kind: j.kind, appId: j.appId, requestedBy: j.requestedBy, state: j.state, exitCode: j.exitCode, pullRequestUrl: j.pullRequestUrl, log: j.log }),
  };
}
export type Applier = ReturnType<typeof applier>;
