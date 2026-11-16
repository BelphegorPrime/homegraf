import { randomUUID } from "node:crypto";
import { Hono, type MiddlewareHandler } from "hono";
import { streamSSE } from "hono/streaming";
import { z } from "zod";
import type { Permission, RepoCheck, RepoInfo, RepoJobSnapshot, RepoProfileInfo } from "../shared/types.js";
import { nodeSpawn, type Spawn } from "./apply.js";
import { spawnRunner, type Runner } from "./apps.js";
import type { Principal } from "./auth.js";
import { toolPath, toolRoot } from "./tools.js";

/** The labels the factory needs (bootstrap/hgboot/factory_repos.py, LABELS): matched by name only. */
export const FACTORY_LABELS = ["ai:plan", "ai:ready", "ai:escalate", "ai:review", "ai:changes", "ai:approved", "ai:close"] as const;
/** The events its ForgeHands hook has to deliver (AGENT_EVENTS there). */
export const FACTORY_EVENTS = ["issues", "pull_request_label", "action_run_failure", "action_run_success", "push"] as const;

/** Agent logins that may write to a repository the factory works in. */
const WRITE = ["write", "admin", "owner"];
const PAGE = 50;
const PAGES = 20;
const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/;
const BRANCH = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$/;
// the escape character is the point: this strips terminal colours
// oxlint-disable-next-line no-control-regex
const ANSI = /\x1b\[[0-9;]*[A-Za-z]/g;

const enc = encodeURIComponent;

/**
 * What the repository routes read and run with. The token is the operator's Forgejo token (FORGEJO_TOKEN): it goes to Forgejo
 * and into the job's environment, never into a response or a log line.
 */
export interface RepoSettings {
  /** The Forgejo server: FORGEJO_URL, else the origin of HOMEGRAF_CLONE_URL. */
  baseUrl?: string;
  token?: string;
  /** The owner whose repositories are listed: the owner of HOMEGRAF_CLONE_URL, else the first allowed repository's. */
  owner?: string;
  /** FORGEHANDS_ALLOWED_REPOS (owner/name); undefined when this server was not given the list. */
  allowed?: string[];
  /** The agent's login (FORGEHANDS_AGENT_LOGIN, else new-repo's default). */
  agent: string;
  /** The endpoint Forgejo calls (FORGEHANDS_FORGEJO_WEBHOOK_URL): what a wired webhook has to point at. */
  webhookUrl?: string;
  webhookSecret?: string;
  /** The repository the tools work on (their `origin` names the owner) and where the new-repo state file goes. */
  repoDir: string;
  stateDir: string;
}

/** The owner of a clone URL (`https://git.example/owner/repository.git` -> `owner`). */
export function ownerOf(cloneUrl: string | undefined): string | undefined {
  if (!cloneUrl) return undefined;
  try {
    return new URL(cloneUrl).pathname.split("/").filter(Boolean)[0] || undefined;
  } catch {
    return undefined;
  }
}

function originOf(cloneUrl: string | undefined): string | undefined {
  if (!cloneUrl) return undefined;
  try {
    return new URL(cloneUrl).origin;
  } catch {
    return undefined;
  }
}

/** The settings the server reads from its environment; no token is ever written into the page. */
export function repoSettings(env: NodeJS.ProcessEnv = process.env): RepoSettings {
  const cloneUrl = env.HOMEGRAF_CLONE_URL;
  const allowed = (env.FORGEHANDS_ALLOWED_REPOS ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return {
    baseUrl: (env.FORGEJO_URL || originOf(cloneUrl))?.replace(/\/$/, "") || undefined,
    token: env.FORGEJO_TOKEN || undefined,
    owner: ownerOf(cloneUrl) ?? (allowed[0]?.split("/")[0] || undefined),
    allowed: allowed.length ? allowed : undefined,
    agent: env.FORGEHANDS_AGENT_LOGIN || "openhands",
    webhookUrl: env.FORGEHANDS_FORGEJO_WEBHOOK_URL || undefined,
    webhookSecret: env.FORGEHANDS_FORGEJO_WEBHOOK_SECRET || undefined,
    repoDir: env.REPO_DIR ?? "/repo",
    stateDir: env.STATE_DIR || `${env.DATA_DIR ?? "/data"}/new-repo`,
  };
}

const missing = (needs: [string, unknown][]): string | undefined => {
  const gone = needs.filter(([, v]) => !v).map(([k]) => k);
  return gone.length ? `not configured: ${gone.join(", ")}` : undefined;
};

/** Why the list cannot be read, or undefined when it can. */
export const repoReadProblem = (s: RepoSettings): string | undefined =>
  missing([
    ["FORGEJO_URL", s.baseUrl],
    ["FORGEJO_TOKEN", s.token],
    ["HOMEGRAF_CLONE_URL (for the owner)", s.owner],
  ]);

/** Why creating cannot be offered, or undefined when it can. */
export const newRepoProblem = (s: RepoSettings): string | undefined =>
  missing([
    ["FORGEJO_TOKEN", s.token],
    ["FORGEHANDS_FORGEJO_WEBHOOK_URL", s.webhookUrl],
    ["FORGEHANDS_FORGEJO_WEBHOOK_SECRET", s.webhookSecret],
  ]);

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

interface Answer {
  ok: boolean;
  status: number;
  body: unknown;
}

async function ask(s: RepoSettings, path: string, f: typeof fetch): Promise<Answer> {
  const res = await f(`${s.baseUrl}/api/v1${path}`, {
    headers: { authorization: `token ${s.token}`, accept: "application/json" },
    signal: AbortSignal.timeout(10_000),
  });
  const body = await res.json().catch(() => undefined);
  return { ok: res.ok, status: res.status, body };
}

/** A hook as the factory creates it (bootstrap/hgboot/factory_repos.py, _hook_matches_endpoint). */
export function hookWired(hook: unknown, endpoint: string): boolean {
  if (!isRecord(hook)) return false;
  const config = isRecord(hook.config) ? hook.config : {};
  const same = (a: unknown) => typeof a === "string" && a.replace(/\/$/, "") === endpoint.replace(/\/$/, "");
  return (
    String(hook.type ?? "").toLowerCase() === "forgejo" &&
    hook.active === true &&
    Array.isArray(hook.events) &&
    FACTORY_EVENTS.every((e) => (hook.events as unknown[]).includes(e)) &&
    same(config.url) &&
    String(config.content_type ?? "").toLowerCase() === "json" &&
    !hook.branch_filter
  );
}

const labelNamed = (label: unknown): string | undefined => (isRecord(label) && typeof label.name === "string" ? label.name : undefined);

/**
 * The repositories of the operator's Forgejo owner, each with its factory status. A part that cannot be read is "unknown" for
 * that repository only: one unreadable repository never fails the list.
 */
export function repoLister(settings: RepoSettings, f: typeof fetch = fetch) {
  const owner = settings.owner ?? "";

  /** Every page of a list; undefined when a page cannot be read. */
  const all = async (path: string): Promise<unknown[] | undefined> => {
    const out: unknown[] = [];
    for (let page = 1; page <= PAGES; page++) {
      const r = await ask(settings, `${path}?limit=${PAGE}&page=${page}`, f);
      if (!r.ok || !Array.isArray(r.body)) return undefined;
      out.push(...r.body);
      if (r.body.length < PAGE) return out;
    }
    return out;
  };

  const repoPath = (name: string) => `/repos/${enc(owner)}/${enc(name)}`;

  const labels = async (name: string): Promise<RepoCheck> => {
    const found = await all(`${repoPath(name)}/labels`);
    if (!found) return "unknown";
    const names = new Set(found.map(labelNamed).filter((n): n is string => !!n));
    return FACTORY_LABELS.every((l) => names.has(l));
  };

  const hooks = async (name: string): Promise<RepoCheck> => {
    if (!settings.webhookUrl) return "unknown"; // nothing to match against
    const found = await all(`${repoPath(name)}/hooks`);
    return found ? found.some((h) => hookWired(h, settings.webhookUrl as string)) : "unknown";
  };

  const agentAccess = async (name: string): Promise<RepoCheck> => {
    const r = await ask(settings, `${repoPath(name)}/collaborators/${enc(settings.agent)}/permission`, f);
    // A user that does not exist has no access; 403 means this token may not ask for someone else's permissions.
    if (r.status === 404) return false;
    if (!r.ok || !isRecord(r.body) || typeof r.body.permission !== "string") return "unknown";
    return WRITE.includes(r.body.permission.toLowerCase());
  };

  const allowed = (name: string): RepoCheck => (settings.allowed ? settings.allowed.includes(`${owner}/${name}`) : "unknown");

  const checked = async (read: () => Promise<RepoCheck>): Promise<RepoCheck> => {
    try {
      return await read();
    } catch {
      return "unknown";
    }
  };

  /** The owner's repositories; an owner that is an organisation answers under /orgs. */
  const owned = async (): Promise<unknown[]> => {
    for (const base of [`/users/${enc(owner)}/repos`, `/orgs/${enc(owner)}/repos`]) {
      const first = await ask(settings, `${base}?limit=${PAGE}&page=1`, f);
      if (!first.ok) {
        if (base.startsWith("/users/") && first.status === 404) continue;
        throw new Error(`Forgejo answered HTTP ${first.status} for the repository list`);
      }
      if (!Array.isArray(first.body)) throw new Error("Forgejo answered the repository list with something else than a list");
      const repos = [...first.body];
      for (let page = 2; page <= PAGES && first.body.length >= PAGE; page++) {
        const next = await ask(settings, `${base}?limit=${PAGE}&page=${page}`, f);
        if (!next.ok || !Array.isArray(next.body)) throw new Error(`Forgejo answered HTTP ${next.status} for the repository list`);
        repos.push(...next.body);
        if (next.body.length < PAGE) break;
      }
      return repos;
    }
    throw new Error(`Forgejo does not know the owner ${owner}`);
  };

  return async (): Promise<RepoInfo[]> => {
    const entries = (await owned()).filter(isRecord).filter((r) => typeof r.name === "string");
    return Promise.all(
      entries.map(async (r): Promise<RepoInfo> => {
        const name = r.name as string;
        const [webhook, labelStatus, access] = await Promise.all([checked(() => hooks(name)), checked(() => labels(name)), checked(() => agentAccess(name))]);
        return {
          name,
          private: r.private === true,
          default_branch: typeof r.default_branch === "string" ? r.default_branch : "",
          updated: typeof r.updated_at === "string" ? r.updated_at : "",
          factory: { webhook, labels: labelStatus, agentAccess: access, allowed: allowed(name) },
        };
      }),
    );
  };
}

/** What `homelab.py repo-profiles --json` says; it changes only with the tool, so the answer is kept a minute. */
export function repoProfileCatalog(repoDir: string, run: Runner = spawnRunner, ttlMs = 60_000, now: () => number = Date.now) {
  let cached: { at: number; value: RepoProfileInfo[] } | undefined;
  return async (): Promise<RepoProfileInfo[]> => {
    if (cached && now() - cached.at < ttlMs) return cached.value;
    const { code, out } = await run(toolPath(repoDir, "homelab.py"), ["repo-profiles", "--json"], repoDir);
    if (code !== 0) throw new Error(`homelab.py repo-profiles failed: ${out.trim().slice(-200)}`);
    try {
      cached = { at: now(), value: JSON.parse(out) as RepoProfileInfo[] };
    } catch {
      throw new Error("homelab.py repo-profiles answered something that is not JSON");
    }
    return cached.value;
  };
}

const input = z.object({
  name: z
    .string()
    .regex(NAME, "letters, digits, . _ - (up to 100 characters, not starting with . or -)")
    .refine((n) => !n.includes(".."), "the name must not contain .."),
  profile: z.string().min(1).max(64),
  description: z
    .string()
    .max(255)
    .refine((d) => d.trim().length > 0, "must not be empty")
    .optional(),
  visibility: z.enum(["private", "public"]).default("private"),
  branch: z
    .string()
    .max(255)
    .refine(
      (b) => BRANCH.test(b) && !b.includes("..") && !b.includes("//") && !b.endsWith("/") && !b.endsWith("."),
      "a branch name without .., //, or a trailing / or .",
    )
    .optional(),
});
export type RepoInput = z.infer<typeof input>;

/** Arguments of `homelab.py new-repo`; the tool creates and reads back, and prints its result as JSON. */
export function newRepoArgs(i: RepoInput): string[] {
  const a = ["new-repo", "--name", i.name, "--profile", i.profile];
  if (i.description !== undefined) a.push("--description", i.description);
  a.push(i.visibility === "public" ? "--public" : "--private");
  if (i.branch !== undefined) a.push("--branch", i.branch);
  a.push("--yes", "--json");
  return a;
}

export interface RepoJob extends RepoJobSnapshot {
  subscribers: Set<(chunk: string | null) => void>;
}

export interface RepoJobDeps {
  settings: RepoSettings;
  spawn?: Spawn;
}

/** Runs `homelab.py new-repo … --yes --json` as one job at a time and keeps its output for the page. */
export function repoJobRunner({ settings, spawn: run = nodeSpawn }: RepoJobDeps) {
  const jobs = new Map<string, RepoJob>();
  let running: RepoJob | undefined;

  // The tool needs the operator's token and the ForgeHands settings; the container's own environment stays out.
  const toolEnv = (): Record<string, string> => ({
    PATH: process.env.PATH ?? "",
    LANG: "C.UTF-8",
    HOME: "/tmp",
    PYTHONDONTWRITEBYTECODE: "1",
    GIT_TERMINAL_PROMPT: "0",
    STATE_DIR: settings.stateDir,
    FORGEJO_TOKEN: settings.token ?? "",
    FORGEHANDS_AGENT_LOGIN: settings.agent,
    ...(settings.baseUrl ? { FORGEJO_URL: settings.baseUrl } : {}),
    ...(settings.webhookUrl ? { FORGEHANDS_FORGEJO_WEBHOOK_URL: settings.webhookUrl } : {}),
    ...(settings.webhookSecret ? { FORGEHANDS_FORGEJO_WEBHOOK_SECRET: settings.webhookSecret } : {}),
  });

  const scrub = (text: string) => [settings.token, settings.webhookSecret].filter((v): v is string => !!v).reduce((out, v) => out.replaceAll(v, "***"), text);

  async function execute(job: RepoJob, i: RepoInput) {
    const emit = (chunk: string) => {
      const clean = scrub(chunk.replace(ANSI, ""));
      job.log += clean;
      job.subscribers.forEach((f) => f(clean));
    };
    try {
      const code = await run(
        "python3",
        ["-I", toolPath(settings.repoDir, "homelab.py"), ...newRepoArgs(i)],
        {
          cwd: settings.repoDir,
          env: { ...toolEnv(), ...toolRoot(settings.repoDir) },
          timeoutMs: 10 * 60_000,
        },
        emit,
      );
      job.exitCode = code;
      job.state = code === 0 ? "done" : "failed";
    } catch (err) {
      emit(`\n✗ ${err instanceof Error ? err.message : err}\n`);
      job.exitCode ??= 1;
      job.state = "failed";
    } finally {
      running = undefined;
      job.subscribers.forEach((f) => f(null));
    }
  }

  return {
    /** Starts creating a repository; undefined while another job runs. */
    start(i: RepoInput, requestedBy?: string): RepoJob | undefined {
      if (running) return undefined;
      const job: RepoJob = { id: randomUUID(), repository: i.name, requestedBy, state: "running", log: "", subscribers: new Set() };
      jobs.set(job.id, job);
      // Keep the last few jobs only.
      for (const id of [...jobs.keys()].slice(0, Math.max(0, jobs.size - 10))) jobs.delete(id);
      running = job;
      void execute(job, i);
      return job;
    },
    get: (id: string) => jobs.get(id),
    snapshot: (j: RepoJob): RepoJobSnapshot => ({
      id: j.id,
      repository: j.repository,
      requestedBy: j.requestedBy,
      state: j.state,
      exitCode: j.exitCode,
      log: j.log,
    }),
  };
}
export type RepoJobs = ReturnType<typeof repoJobRunner>;

type RepoEnv = { Variables: { principal: Principal } };

export interface RepoRoutesOptions {
  settings?: RepoSettings;
  /** `homelab.py repo-profiles`; a test passes its own. */
  run?: Runner;
  /** `homelab.py new-repo`; a test passes its own. */
  spawn?: Spawn;
  now?: () => number;
}

export interface RepoRoutesDeps extends RepoRoutesOptions {
  authorize: (permission?: Permission) => MiddlewareHandler<RepoEnv>;
}

/** The repository routes; the app mounts them under /api behind its sign-in. */
export function reposRoutes({ authorize, ...options }: RepoRoutesDeps) {
  const settings = options.settings ?? repoSettings();
  const app = new Hono<RepoEnv>();
  const profiles = repoProfileCatalog(settings.repoDir, options.run ?? spawnRunner, 60_000, options.now ?? Date.now);
  const jobs = repoJobRunner({ settings, spawn: options.spawn });

  const invalid = (parsed: { error: { issues: { path: PropertyKey[]; message: string }[] } }) => ({
    error: "invalid input",
    issues: parsed.error.issues.map((i) => ({ field: i.path.join("."), message: i.message })),
  });

  // The owner's repositories with how far each is wired for the factory; one unreadable part is "unknown", not an error.
  app.get("/repos", async (c) => {
    const problem = repoReadProblem(settings);
    if (problem) return c.json({ error: problem }, 503);
    try {
      return c.json({ repos: await repoLister(settings)() });
    } catch (err) {
      return c.json({ error: `repositories: ${err instanceof Error ? err.message : String(err)}` }, 502);
    }
  });

  // The files a new repository starts with.
  app.get("/repo-profiles", async (c) => {
    try {
      return c.json(await profiles());
    } catch (err) {
      return c.json({ error: err instanceof Error ? err.message : String(err) }, 502);
    }
  });

  // Creates the repository through the operator command as a job; who may do this is who may create apps.
  app.post("/repos", authorize("apps:create"), async (c) => {
    const problem = newRepoProblem(settings);
    if (problem) return c.json({ error: problem }, 503);
    const parsed = input.safeParse(await c.req.json().catch(() => null));
    if (!parsed.success) return c.json(invalid(parsed), 400);
    let catalog: RepoProfileInfo[];
    try {
      catalog = await profiles();
    } catch (err) {
      return c.json({ error: err instanceof Error ? err.message : String(err) }, 502);
    }
    if (!catalog.some((p) => p.name === parsed.data.profile))
      return c.json({ error: `unknown profile ${parsed.data.profile}; the profiles are ${catalog.map((p) => p.name).join(", ") || "none"}` }, 400);
    const by = c.get("principal").name;
    const job = jobs.start(parsed.data, by);
    if (!job) return c.json({ error: "another repository is being created, wait for it to finish" }, 409);
    console.log(`new repository ${parsed.data.name} from profile ${parsed.data.profile} requested by ${by}`);
    return c.json({ jobId: job.id }, 202);
  });

  app.get("/repos/jobs/:id", (c) => {
    const job = jobs.get(c.req.param("id"));
    return job ? c.json(jobs.snapshot(job)) : c.json({ error: "unknown job" }, 404);
  });

  // The job's output: everything so far, then each chunk as it arrives, then one `done` event with the result.
  app.get("/repos/jobs/:id/events", (c) => {
    const job = jobs.get(c.req.param("id"));
    if (!job) return c.json({ error: "unknown job" }, 404);
    return streamSSE(c, async (stream) => {
      let chain: Promise<unknown> = Promise.resolve();
      const send = (event: string, data: string) => (chain = chain.then(() => stream.writeSSE({ event, data })));
      let finish!: () => void;
      const finished = new Promise<void>((r) => (finish = r));
      send("log", JSON.stringify(job.log));
      const listener = (chunk: string | null) => {
        if (chunk !== null) return void send("log", JSON.stringify(chunk));
        send("done", JSON.stringify(jobs.snapshot(job)));
        finish();
      };
      if (job.state === "running") job.subscribers.add(listener);
      else listener(null);
      stream.onAbort(() => void job.subscribers.delete(listener));
      await finished;
      await chain;
    });
  });

  return app;
}
