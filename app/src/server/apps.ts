import { execFile } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { z } from "zod";
import type { AddonInfo, HostInfo, PlanResult, PreviewResult, RemovableApp, ManagedStack, TemplateInfo } from "../shared/types.js";

const ID = /^[a-z0-9][a-z0-9-]{1,40}$/;
const VAR = /^[A-Za-z_][A-Za-z0-9_]*$/;

export const planInput = z.object({
  appId: z.string().regex(ID, "lowercase letters, digits and dashes, 2-41 characters"),
  server: z.string().regex(/^[A-Za-z0-9._-]+$/),
  image: z.string().regex(/^[\w./:@-]+$/, "image with a fixed tag, e.g. traefik/whoami:v1.11.0").optional(),
  containerPort: z.number().int().min(1).max(65535).optional(),
  hostPort: z.number().int().min(1).max(65535).optional(),
  mount: z.string().regex(/^\/[\w./-]*$/, "absolute path inside the container").optional(),
  secretVars: z.array(z.string().regex(VAR)).max(20).default([]),
  routing: z.boolean().default(true),
  domain: z.string().regex(/^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$/).optional(),
  websockets: z.boolean().default(false),
  forceHttps: z.boolean().default(true),
  healthPath: z.string().regex(/^\/[\w./%-]*$/).optional(),
  statuses: z.string().regex(/^\d{3}(,\d{3})*$/).optional(),
  /** Volumes of the app: data = below /srv/docker/<id>, bind = host path, named, tmpfs, repo = path in this repository. */
  volumes: z.array(z.object({
    kind: z.enum(["data", "bind", "named", "tmpfs", "repo"]),
    source: z.string().max(300).default(""),
    target: z.string().regex(/^\/[\w./@+-]*$/, "absolute path inside the container"),
    readOnly: z.boolean().default(false),
    size: z.string().regex(/^(\d+(\.\d+)?[kKmMgG]?[bB]?)?$/, "e.g. 512m").default(""),
  })).max(20).default([]),
  /** Networks to join besides the stack's default one; external = an existing Docker network. */
  networks: z.array(z.object({ name: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/, "letters, digits, - _ ."), external: z.boolean().default(false) })).max(10).default([]),
  cpus: z.string().regex(/^(\d+(\.\d+)?)?$/, "a number, e.g. 1.5").default(""),
  memory: z.string().regex(/^([1-9]\d*[kKmMgG])?$/, "e.g. 512m or 2g").default(""),
  gpu: z.enum(["", "nvidia", "dri"]).default(""),
  /** Plain environment variables (secrets are secretVars). */
  env: z.record(z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/, "letters, digits and _"), z.string().max(2000)).default({}),
  /** Start from a template (nginx, httpd, haproxy): the files its volumes mount from the repo are written with the app. */
  template: z.string().regex(/^[a-z0-9-]{1,30}$/).optional(),
  /** Further templates in the same compose file: each is the service <id>-<template>, with building blocks and secrets of its own. */
  extraTemplates: z.array(z.string().regex(/^[a-z0-9-]{1,30}$/)).max(8).optional(),
  /** Edited copies of the template's files (path below the template folder -> text). */
  files: z.record(z.string().regex(/^(?!.*\.\.)[\w.@+-]+(\/[\w.@+-]+)*$/, "a relative path"), z.string().max(200_000)).default({})
    .refine((f) => Object.keys(f).length <= 20 && Object.values(f).reduce((n, s) => n + s.length, 0) <= 400_000, "too many or too large files"),
  /** Ready-made services next to the app (ids from `homelab.py addons`); the tool rejects unknown ones. */
  addons: z.array(z.string().regex(/^[a-z0-9-]{1,30}$/)).max(8).default([]),
  /** The compose file as shown and edited in the UI; used as it is instead of generating one. */
  compose: z.string().min(1).max(200_000).optional(),
  /** Published host port of the web UI when the compose file has several. */
  webPort: z.number().int().min(1).max(65535).optional(),
  /** Values the user typed for secrets of the compose file; every other `${VAR}` is generated. Creating only. */
  secretValues: z.record(z.string().regex(VAR), z.string().min(1).max(4096)).default({}),
});
export type PlanInput = z.infer<typeof planInput>;

/** Name of the environment variable that carries the typed value of a secret to the tool (never a command line). */
export const secretValueEnv = (v: string) => `SECRET_VALUE_${v}`;

/**
 * Arguments of `homelab.py new-app` for a form; never contains --yes, which only the apply step may add.
 * With composeFile the tool uses that file as it is, so the form's container fields no longer matter.
 */
export function newAppArgs(i: PlanInput, composeFile?: string, filesDir?: string): string[] {
  // A ${VAR} nobody typed a value for is generated; a secret that already exists in Infisical is never overwritten.
  const a = ["new-app", "--app-id", i.appId, "--server", i.server, "--generate-secrets"];
  const opt = (flag: string, v: string | number | undefined) => v !== undefined && a.push(flag, String(v));
  if (composeFile) {
    a.push("--compose-file", composeFile);
  } else {
    opt("--image", i.image);
    opt("--container-port", i.containerPort);
    opt("--host-port", i.hostPort);
    opt("--mount", i.mount);
    for (const v of i.volumes) a.push("--volume", `${v.kind}:${v.source}:${v.target}${v.readOnly ? ":ro" : v.size ? `:size=${v.size}` : ""}`);
    for (const n of i.networks) a.push(n.external ? "--external-network" : "--network", n.name);
    opt("--cpus", i.cpus || undefined);
    opt("--memory", i.memory || undefined);
    opt("--gpu", i.gpu || undefined);
    for (const [k, v] of Object.entries(i.env)) a.push("--env", `${k}=${v}`);
    for (const v of i.secretVars) a.push("--secret-var", v, "--secret", `${v}=generate`);
    for (const x of i.addons) a.push("--addon", x);
  }
  if (i.template) a.push("--template", i.template, ...(i.extraTemplates ?? []).flatMap((x) => ["--template", x]));
  if (filesDir) a.push("--files-dir", filesDir);
  // Typed values come last, so they win over a generated one for the same variable.
  for (const v of Object.keys(i.secretValues)) a.push("--secret", `${v}=env:${secretValueEnv(v)}`);
  if (!i.routing) a.push("--no-routing");
  else {
    opt("--domain", i.domain);
    opt("--web-port", i.webPort ?? (composeFile ? undefined : i.hostPort));
    a.push(i.websockets ? "--websockets" : "--no-websockets", i.forceHttps ? "--force-https" : "--no-force-https");
    opt("--health-path", i.healthPath);
    opt("--statuses", i.statuses);
  }
  return a;
}

/**
 * Runs fn with the edited compose text and the edited template files in a temporary folder (compose.yaml, files/<path>)
 * and removes it after. Without either there is no folder.
 */
export async function withWorkFiles<T>(
  input: { compose?: string; files: Record<string, string> },
  fn: (w: { composeFile?: string; filesDir?: string }) => Promise<T>,
): Promise<T> {
  const names = Object.keys(input.files);
  if (input.compose === undefined && names.length === 0) return fn({});
  const dir = await mkdtemp(join(tmpdir(), "homegraf-work-"));
  try {
    let composeFile: string | undefined;
    if (input.compose !== undefined) {
      composeFile = join(dir, "compose.yaml");
      await writeFile(composeFile, input.compose, { mode: 0o600 });
    }
    const filesDir = join(dir, "files");
    for (const name of names) {
      const target = join(filesDir, name);
      await mkdir(dirname(target), { recursive: true });
      await writeFile(target, input.files[name], { mode: 0o600 });
    }
    return await fn({ composeFile, filesDir: names.length ? filesDir : undefined });
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

const quote = (s: string) => (/^[\w./:@=,%-]+$/.test(s) ? s : `'${s.replaceAll("'", "'\\''")}'`);
/** The same invocation as a shell command, with --yes, for running it by hand. */
export const shellCommand = (args: string[]) => `./homelab.py ${[...args, "--yes"].map(quote).join(" ")}`;

export type Runner = (file: string, args: string[]) => Promise<{ code: number; out: string }>;

const ANSI = /\x1b\[[0-9;]*[A-Za-z]/g;

export const spawnRunner: Runner = (file, args) =>
  new Promise((resolve) => {
    const child = execFile(
      "python3",
      ["-I", file, ...args],
      // A clean environment: the plan must not see credentials, and the tool loads what it needs from the repo.
      { timeout: 60_000, maxBuffer: 1024 * 1024, env: { PATH: process.env.PATH ?? "", LANG: "C.UTF-8", PYTHONDONTWRITEBYTECODE: "1" } },
      (err, stdout, stderr) => {
        const code = err ? (typeof err.code === "number" ? err.code : 1) : 0;
        resolve({ code, out: `${stdout}${stderr}` });
      },
    );
    child.stdin?.end(); // no terminal: the tool runs in agent mode and fails instead of asking
  });

const NO_VALUES = { secretValues: {} };

/** Starts a serial queue: the tool runs one at a time, and a failing run does not block the next. */
function serial() {
  let busy: Promise<unknown> = Promise.resolve();
  return <T>(job: () => Promise<T>): Promise<T> => {
    const next = busy.then(job);
    busy = next.catch(() => undefined);
    return next;
  };
}

/** Dry run of new-app: stops before the first outward step, so it changes nothing. One run at a time. */
export function planner(repoDir: string, run: Runner = spawnRunner) {
  const queue = serial();
  return (input: PlanInput): Promise<PlanResult> =>
    queue(() => withWorkFiles(input, async ({ composeFile, filesDir }) => {
      const { code, out } = await run(`${repoDir}/homelab.py`, newAppArgs({ ...input, ...NO_VALUES }, composeFile, filesDir));
      // The command to run by hand: the compose file and the template files are then next to it.
      const command = shellCommand(newAppArgs({ ...input, ...NO_VALUES }, input.compose ? "compose.yaml" : undefined,
        Object.keys(input.files).length ? "files" : undefined));
      return { ok: code === 0, exitCode: code, output: out.replace(ANSI, "").trim(), command };
    }));
}

/** What the tool would use as compose file, with the secrets and ports in it (`--print-compose`); changes nothing. */
export function previewer(repoDir: string, run: Runner = spawnRunner) {
  const queue = serial();
  return (input: PlanInput): Promise<PreviewResult> =>
    queue(() => withWorkFiles(input, async ({ composeFile, filesDir }) => {
      const { code, out } = await run(`${repoDir}/homelab.py`, [...newAppArgs({ ...input, ...NO_VALUES }, composeFile, filesDir), "--print-compose"]);
      const text = out.replace(ANSI, "").trim();
      if (code !== 0) return { ok: false, error: text.replace(/^error: /m, "").split("\n").filter(Boolean).pop() ?? `exit ${code}` };
      try {
        return { ok: true, ...(JSON.parse(text) as Omit<PreviewResult, "ok">) };
      } catch {
        return { ok: false, error: "the tool's answer was not readable" };
      }
    }));
}

/** What a `homelab.py <command> --json` lists. Building blocks and templates only change with the tool, so their answer is kept a minute;
 * the apps change with every merge (update-all pulls), and a freshly deployed app has to be removable at once: a few seconds only. */
function cachedList<T>(repoDir: string, command: string, run: Runner, ttlMs: number, now: () => number) {
  let cached: { at: number; value: T[] } | undefined;
  return async (): Promise<T[]> => {
    if (cached && now() - cached.at < ttlMs) return cached.value;
    const { code, out } = await run(`${repoDir}/homelab.py`, [command, "--json"]);
    if (code !== 0) throw new Error(`homelab.py ${command} failed: ${out.trim().slice(-200)}`);
    cached = { at: now(), value: JSON.parse(out) as T[] };
    return cached.value;
  };
}

export const addonCatalog = (repoDir: string, run: Runner = spawnRunner, ttlMs = 60_000, now: () => number = Date.now) =>
  cachedList<AddonInfo>(repoDir, "addons", run, ttlMs, now);
export const templateCatalog = (repoDir: string, run: Runner = spawnRunner, ttlMs = 60_000, now: () => number = Date.now) =>
  cachedList<TemplateInfo>(repoDir, "templates", run, ttlMs, now);
export const removableCatalog = (repoDir: string, run: Runner = spawnRunner, ttlMs = 3_000, now: () => number = Date.now) =>
  cachedList<RemovableApp>(repoDir, "removable", run, ttlMs, now);
export const hostCatalog = (repoDir: string, run: Runner = spawnRunner, ttlMs = 3_000, now: () => number = Date.now) =>
  cachedList<HostInfo>(repoDir, "hosts", run, ttlMs, now);
export const stackCatalog = (repoDir: string, run: Runner = spawnRunner, ttlMs = 3_000, now: () => number = Date.now) =>
  cachedList<ManagedStack>(repoDir, "stacks", run, ttlMs, now);

/** Runs `homelab.py secrets …` with the credentials of the container (Infisical); the value, when there is one, goes in through stdin. */
export type SecretTool = (args: string[], stdin?: string) => Promise<{ code: number; out: string }>;

export function secretTool(repoDir: string, env: Record<string, string>): SecretTool {
  return (args, stdin) =>
    new Promise((resolve) => {
      const child = execFile(
        "python3", ["-I", `${repoDir}/homelab.py`, "secrets", ...args],
        { timeout: 60_000, maxBuffer: 1024 * 1024, env: { PATH: process.env.PATH ?? "", LANG: "C.UTF-8", PYTHONDONTWRITEBYTECODE: "1", HOME: "/tmp", ...env } },
        (err, stdout, stderr) => resolve({ code: err ? (typeof err.code === "number" ? err.code : 1) : 0, out: `${stdout}${stderr}`.replace(ANSI, "") }),
      );
      child.stdin?.end(stdin ?? "");
    });
}
