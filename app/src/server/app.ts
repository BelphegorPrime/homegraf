import { serveStatic } from "@hono/node-server/serve-static";
import { Hono, type Context } from "hono";
import { createMiddleware } from "hono/factory";
import { streamSSE } from "hono/streaming";
import type { CertificateSource } from "./certificates.js";
import type { RootCaSource } from "./rootCa.js";
import type { UnifiStatus } from "./unifi.js";
import type { AddonInfo, Capabilities, Graph, HostInfo, ManagedStack, PendingRemoval, RemovableApp, SecretKey, SsoSession, PlanResult, PreviewResult, StatusSnapshot, TemplateInfo } from "../shared/types.js";
import { planInput, type PlanInput } from "./apps.js";
import type { Applier } from "./apply.js";
import type { SecretTool } from "./apps.js";
import type { PullStateReader, RemovalStore } from "./removals.js";
import { domainOfLog, type CreationStore, type CreationTracker } from "./creations.js";
import type { Authenticator, Denied, Principal } from "./auth.js";
import { cookieValue, SESSION_COOKIE, STATE_COOKIE, type Oidc } from "./oidc.js";
import { ADMIN, UserError, type UserStore } from "./users.js";
import { PERMISSIONS, type Permission, type UserInfo } from "../shared/types.js";

export interface CreateDeps {
  /** Why creating is off (missing configuration); undefined when it is available. */
  problem?: string;
  applier: Applier;
}

export interface RemovalDeps {
  /** The apps that may be removed (the tool leaves out the platform). */
  apps: () => Promise<RemovableApp[]>;
  store: RemovalStore;
  pullState: PullStateReader;
}

export interface CreationsDeps {
  /** Apps whose pull request is open, kept until they answer over HTTPS. */
  store: CreationStore;
  tracker: CreationTracker;
}

export interface HostsDeps {
  /** The hosts ingressctl manages. */
  list: () => Promise<HostInfo[]>;
  rootCa: RootCaSource;
}

export interface StacksDeps {
  /** The stacks that may be redeployed and have secrets managed (never the platform). */
  list: () => Promise<ManagedStack[]>;
  secretTool: SecretTool;
}

export interface UsersDeps {
  /** Without it (no HOMEGRAF_PASSWORD) nobody can sign in and every write action is off. */
  enabled: boolean;
  authenticate: Authenticator;
  store: UserStore;
  /** The built-in administrator's name (HOMEGRAF_ADMIN); "admin" when not given. */
  admin?: string;
}

const refusal: Record<Denied, [401 | 429 | 503, string]> = {
  missing: [401, "sign in with a user name and password"],
  wrong: [401, "wrong user name or password"],
  locked: [429, "too many wrong passwords for this user, try again in a few minutes"],
  disabled: [503, "signing in is off: HOMEGRAF_PASSWORD is not set"],
};

type Env = { Variables: { principal: Principal } };

export interface Deps {
  loadGraph: () => Promise<Graph>;
  status: { get: () => StatusSnapshot; subscribe: (l: (s: StatusSnapshot) => void) => () => void };
  webDir: string;
  plan: (input: PlanInput) => Promise<PlanResult>;
  /** The compose file and what is in it, for the editor of the New app tab. */
  preview: (input: PlanInput) => Promise<PreviewResult>;
  addons: () => Promise<AddonInfo[]>;
  templates: () => Promise<TemplateInfo[]>;
  unifi: () => UnifiStatus;
  certificates: CertificateSource;
  create: CreateDeps;
  users: UsersDeps;
  removal: RemovalDeps;
  creations: CreationsDeps;
  hosts: HostsDeps;
  stacks: StacksDeps;
  /** Signing in through an OpenID Connect provider; none when it is not set up. */
  sso?: { oidc?: Oidc; problem?: string };
}

export function createApp({ loadGraph, status, webDir, plan, preview, addons, templates, unifi, certificates, create, users, removal, creations, hosts, stacks, sso }: Deps) {
  const app = new Hono<Env>();

  /** Signs the request in (a cookie session of the identity provider, else name and password), and, when a permission is named, checks that the user has it. */
  const authorize = (permission?: Permission) => createMiddleware<Env>(async (c, next) => {
    const session = sso?.oidc?.principal(cookieValue(c.req.header("cookie"), SESSION_COOKIE));
    if (session) {
      // A cookie goes along with every request a page of another site makes; the token only this page knows keeps those out.
      if (!["GET", "HEAD"].includes(c.req.method) && c.req.header("x-csrf-token") !== session.csrf) {
        return c.json({ error: "this page's session token is missing: reload the page and try again" }, 403);
      }
      if (permission && !session.principal.permissions.includes(permission)) return c.json({ error: `${session.principal.name} may not do this (needs ${permission})` }, 403);
      c.set("principal", session.principal);
      return next();
    }
    const result = await users.authenticate(c.req.header("authorization"));
    if (!result.ok) {
      const [status, error] = refusal[result.reason];
      return c.json({ error }, status);
    }
    if (permission && !result.principal.permissions.includes(permission)) return c.json({ error: `${result.principal.name} may not do this (needs ${permission})` }, 403);
    c.set("principal", result.principal);
    await next();
  });

  app.get("/api/health", (c) => c.json({ ok: true }));

  // Single sign-on: the page asks who is signed in; /login sends the browser to the provider, /callback is where it comes back.
  app.get("/api/auth/session", (c) => {
    const oidc = sso?.oidc;
    if (!oidc) return c.json({ enabled: false, problem: sso?.problem } satisfies SsoSession);
    const s = oidc.principal(cookieValue(c.req.header("cookie"), SESSION_COOKIE));
    return c.json({ enabled: true, label: oidc.cfg.label, user: s && { name: s.principal.name, permissions: s.principal.permissions, csrf: s.csrf } } satisfies SsoSession);
  });
  app.get("/api/auth/login", async (c) => {
    if (!sso?.oidc) return c.json({ error: "single sign-on is not set up" }, 404);
    try {
      const { location, stateCookie } = await sso.oidc.login();
      c.header("Set-Cookie", stateCookie);
      return c.redirect(location, 302);
    } catch (err) {
      return c.redirect(`/?sso_error=${encodeURIComponent(err instanceof Error ? err.message : String(err))}#users`, 302);
    }
  });
  app.get("/api/auth/callback", async (c) => {
    if (!sso?.oidc) return c.json({ error: "single sign-on is not set up" }, 404);
    const result = await sso.oidc.callback({ code: c.req.query("code"), state: c.req.query("state"), error: c.req.query("error") }, cookieValue(c.req.header("cookie"), STATE_COOKIE));
    c.header("Set-Cookie", sso.oidc.clearStateCookie(), { append: true });
    if (!result.ok) return c.redirect(`/?sso_error=${encodeURIComponent(result.error)}#users`, 302);
    c.header("Set-Cookie", sso.oidc.sessionCookie(result.sessionId, result.maxAgeSeconds), { append: true });
    return c.redirect("/", 302);
  });
  app.post("/api/auth/logout", (c) => {
    sso?.oidc?.logout(cookieValue(c.req.header("cookie"), SESSION_COOKIE));
    if (sso?.oidc) c.header("Set-Cookie", sso.oidc.clearSessionCookie());
    return c.body(null, 204);
  });

  app.get("/api/graph", async (c) => {
    try {
      return c.json(await loadGraph());
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      // An empty or missing /repo is the first thing a new setup runs into: say what to do instead of a Python error.
      if (/can't open file .*build\.py/.test(message)) {
        return c.json({ error: "there is no repository to read: mount a checkout of your homelab repository at /repo (it holds infra/graph/build.py, inventory/ and stacks/)" }, 502);
      }
      return c.json({ error: `graph build failed: ${message}` }, 502);
    }
  });

  app.get("/api/status", (c) => c.json(status.get()));

  // The certificates the domains serve right now (probed over TLS, kept for a few minutes).
  app.get("/api/certificates", async (c) => {
    try {
      return c.json(await certificates.get());
    } catch (err) {
      return c.json({ error: `certificates: ${err instanceof Error ? err.message : err}` }, 502);
    }
  });

  // The homelab CA certificates for download (public: clients need them before they can trust anything) and what is in them.
  app.get("/api/ca", (c) => {
    const ca = hosts.rootCa();
    return ca ? c.json(ca.info) : c.json({ error: "there is no CA certificate in the repository yet" }, 404);
  });
  app.get("/api/ca.pem", (c) => {
    const ca = hosts.rootCa();
    if (!ca) return c.json({ error: "there is no CA certificate in the repository yet" }, 404);
    return c.body(ca.pem, 200, { "content-type": "application/x-pem-file", "content-disposition": 'attachment; filename="homelab-root-ca.pem"' });
  });

  // The hosts whose options can be changed here (not the ones made by hand in NPM).
  app.get("/api/hosts", async (c) => {
    try {
      return c.json(await hosts.list());
    } catch (err) {
      return c.json({ error: err instanceof Error ? err.message : String(err) }, 502);
    }
  });

  // Turns the HTTP to HTTPS redirect of a host on or off: a pull request that changes its manifest; the merge sets the NPM host.
  app.post("/api/hosts/force-https", async (c, next) => (create.problem ? c.json({ error: create.problem }, 503) : next()), authorize("hosts:edit"), async (c) => {
    const b = await body(c);
    const domain = typeof b.domain === "string" ? b.domain : "";
    if (typeof b.enabled !== "boolean") return c.json({ error: "enabled must be true or false" }, 400);
    const host = (await hosts.list().catch(() => [])).find((h) => h.domains.includes(domain));
    if (!host) return c.json({ error: `${domain || "this domain"} is not a host managed here (hosts made by hand in NPM are changed in NPM)` }, 404);
    if (host.forceHttps === b.enabled) return c.json({ error: `the redirect is already ${b.enabled ? "on" : "off"}` }, 409);
    const by = c.get("principal").name;
    const job = create.applier.startHostOption(domain, b.enabled, by);
    if (!job) return c.json({ error: "another job is running, wait for it to finish" }, 409);
    console.log(`force-https ${b.enabled ? "on" : "off"} for ${domain} requested by ${by}`);
    return c.json({ jobId: job.id }, 202);
  });

  // Dry run of `homelab.py new-app`: shows what would be created and changes nothing.
  app.post("/api/apps/plan", async (c) => {
    const parsed = planInput.safeParse(await c.req.json().catch(() => null));
    if (!parsed.success) {
      return c.json({ error: "invalid input", issues: parsed.error.issues.map((i) => ({ field: i.path.join("."), message: i.message })) }, 400);
    }
    return c.json(await plan(parsed.data));
  });

  const invalid = (parsed: { error: { issues: { path: PropertyKey[]; message: string }[] } }) =>
    ({ error: "invalid input", issues: parsed.error.issues.map((i) => ({ field: i.path.join("."), message: i.message })) });

  app.get("/api/addons", async (c) => {
    try {
      return c.json(await addons());
    } catch (err) {
      return c.json({ error: err instanceof Error ? err.message : String(err) }, 502);
    }
  });

  app.get("/api/templates", async (c) => {
    try {
      return c.json(await templates());
    } catch (err) {
      return c.json({ error: err instanceof Error ? err.message : String(err) }, 502);
    }
  });

  // The compose file for the form (or the edited one, checked) with its secrets and ports; changes nothing.
  app.post("/api/apps/preview", async (c) => {
    const parsed = planInput.safeParse(await c.req.json().catch(() => null));
    return parsed.success ? c.json(await preview(parsed.data)) : c.json(invalid(parsed), 400);
  });

  app.get("/api/capabilities", (c) => c.json({ create: { enabled: !create.problem, reason: create.problem }, users: { enabled: users.enabled, reason: users.enabled ? users.store.problem : "HOMEGRAF_PASSWORD is not set" }, unifi: unifi() } satisfies Capabilities));

  // Creates the app for real: the signed-in user needs apps:create, then `homelab.py new-app --pr --yes` runs as a background job.
  app.post("/api/apps/create", async (c, next) => (create.problem ? c.json({ error: create.problem }, 503) : next()), authorize("apps:create"), async (c) => {
    const parsed = planInput.safeParse(await c.req.json().catch(() => null));
    if (!parsed.success) return c.json(invalid(parsed), 400);
    const by = c.get("principal").name;
    const job = create.applier.start(parsed.data, by, (j) => {
      if (!j.pullRequestUrl) return;
      const domain = parsed.data.routing ? domainOfLog(j.log) ?? parsed.data.domain : undefined;
      try {
        creations.store.add({ appId: parsed.data.appId, server: parsed.data.server, pullRequestUrl: j.pullRequestUrl, domain, requestedBy: by, at: new Date().toISOString() });
      } catch (err) {
        console.error(`creations: ${parsed.data.appId} not recorded: ${err instanceof Error ? err.message : err}`);
      }
    });
    if (!job) return c.json({ error: "another app is being created, wait for it to finish" }, 409);
    console.log(`create ${parsed.data.appId} on ${parsed.data.server} requested by ${by}`);
    return c.json({ jobId: job.id }, 202);
  });

  // Who am I: checks the credentials without doing anything.
  app.get("/api/me", authorize(), (c) => c.json(c.get("principal")));

  const info = (u: { name: string; permissions: Permission[]; created?: string; sso?: boolean }): UserInfo => ({ name: u.name, permissions: u.permissions, created: u.created, sso: u.sso || undefined });
  const userFailure = (c: Context, err: unknown) => {
    if (err instanceof UserError) return c.json({ error: err.message }, err.status);
    throw err;
  };
  const body = async (c: Context) => ((await c.req.json().catch(() => null)) ?? {}) as Record<string, unknown>;

  app.get("/api/users", authorize("users:manage"), (c) =>
    c.json({ users: [{ name: users.admin ?? ADMIN, permissions: Object.keys(PERMISSIONS), builtin: true }, ...users.store.list().map(info)] }));

  app.post("/api/users", authorize("users:manage"), async (c) => {
    const b = await body(c);
    try {
      if (b.sso === true && !sso?.oidc) return c.json({ error: "single sign-on is not set up" }, 400);
      const user = b.sso === true
        ? await users.store.addSso(String(b.name ?? ""), (b.permissions ?? []) as Permission[])
        : await users.store.add(String(b.name ?? ""), b.password as string, (b.permissions ?? []) as Permission[]);
      console.log(`user ${user.name} created by ${c.get("principal").name}`);
      return c.json(info(user), 201);
    } catch (err) { return userFailure(c, err); }
  });

  app.patch("/api/users/:name", authorize("users:manage"), async (c) => {
    const b = await body(c);
    const name = c.req.param("name");
    if (name === (users.admin ?? ADMIN)) return c.json({ error: "the administrator comes from the environment" }, 400);
    try {
      const user = await users.store.update(name, { password: b.password as string | undefined, permissions: b.permissions as Permission[] | undefined });
      console.log(`user ${name} changed by ${c.get("principal").name}`);
      return c.json(info(user));
    } catch (err) { return userFailure(c, err); }
  });

  app.delete("/api/users/:name", authorize("users:manage"), (c) => {
    const name = c.req.param("name");
    if (name === (users.admin ?? ADMIN)) return c.json({ error: "the administrator comes from the environment" }, 400);
    try {
      users.store.remove(name);
      console.log(`user ${name} removed by ${c.get("principal").name}`);
      return c.body(null, 204);
    } catch (err) { return userFailure(c, err); }
  });

  // Everybody changes their own password; it needs the current one (the request is signed in with it).
  app.put("/api/me/password", authorize(), async (c) => {
    const me = c.get("principal");
    if (me.source === "builtin") return c.json({ error: "the administrator's password is HOMEGRAF_PASSWORD in the environment" }, 400);
    if (me.source === "sso") return c.json({ error: "you sign in through the identity provider: your password is there" }, 400);
    try {
      await users.store.update(me.name, { password: (await body(c)).password as string });
      return c.body(null, 204);
    } catch (err) { return userFailure(c, err); }
  });

  // The apps that may be removed, with what removing each touches.
  app.get("/api/apps/removable", async (c) => {
    try {
      return c.json(await removal.apps());
    } catch (err) {
      return c.json({ error: err instanceof Error ? err.message : String(err) }, 502);
    }
  });

  // Apps whose pull request waits for the merge or whose deploy is on its way, with how far each is.
  app.get("/api/creations", async (c) => c.json({ creations: await creations.tracker.list(), problem: creations.store.problem }));

  // Removals waiting for their second half, with the state of their pull request.
  app.get("/api/removals", async (c) => {
    const items = removal.store.list();
    const pending: PendingRemoval[] = await Promise.all(items.map(async (r) => ({ ...r, pr: await removal.pullState(r.pullRequestUrl) })));
    return c.json({ removals: pending, problem: removal.store.problem });
  });

  // First half: the stack is destroyed now, the files go in a pull request. Needs the app's name typed again as confirmation.
  app.post("/api/apps/remove", async (c, next) => (create.problem ? c.json({ error: create.problem }, 503) : next()), authorize("apps:remove"), async (c) => {
    const b = await body(c);
    const appId = typeof b.appId === "string" ? b.appId : "";
    if (!appId || b.confirm !== appId) return c.json({ error: "type the app's name to confirm" }, 400);
    const found = (await removal.apps().catch(() => [])).find((a) => a.id === appId);
    if (!found) return c.json({ error: `${appId} cannot be removed here (unknown, or part of the platform)` }, 404);
    if (removal.store.problem) return c.json({ error: removal.store.problem }, 503);
    const by = c.get("principal").name;
    const job = create.applier.startRemoval(appId, by, (j) => {
      if (j.pullRequestUrl) removal.store.add({ appId, server: found.server, pullRequestUrl: j.pullRequestUrl, requestedBy: by, at: new Date().toISOString() });
    });
    if (!job) return c.json({ error: "another job is running, wait for it to finish" }, 409);
    console.log(`remove ${appId} (first half) requested by ${by}`);
    return c.json({ jobId: job.id }, 202);
  });

  // Second half, once the pull request is merged: NPM host, certificate and secrets.
  app.post("/api/apps/remove/finish", async (c, next) => (create.problem ? c.json({ error: create.problem }, 503) : next()), authorize("apps:remove"), async (c) => {
    const appId = String((await body(c)).appId ?? "");
    const pending = removal.store.get(appId);
    if (!pending) return c.json({ error: `no removal of ${appId} is waiting` }, 404);
    if ((await removal.pullState(pending.pullRequestUrl)) !== "merged") return c.json({ error: "the pull request is not merged yet" }, 409);
    const by = c.get("principal").name;
    const job = create.applier.finishRemoval(appId, pending.server, by, () => removal.store.remove(appId));
    if (!job) return c.json({ error: "another job is running, wait for it to finish" }, 409);
    console.log(`remove ${appId} (second half) requested by ${by}`);
    return c.json({ jobId: job.id }, 202);
  });

  // The stacks that may be redeployed, and whose secrets may be managed.
  app.get("/api/stacks", async (c) => {
    try {
      return c.json(await stacks.list());
    } catch (err) {
      return c.json({ error: err instanceof Error ? err.message : String(err) }, 502);
    }
  });

  const KEY = /^[A-Za-z_][A-Za-z0-9_]*$/;
  /** The stack's id from the URL if it is one the UI may manage; otherwise the answer to send. */
  const managed = async (c: Context, needSecrets: boolean) => {
    const stack = (await stacks.list().catch(() => [])).find((s) => s.id === c.req.param("id"));
    if (!stack) return c.json({ error: `${c.req.param("id")} cannot be managed here (unknown, or part of the platform)` }, 404);
    if (needSecrets && !stack.secrets) return c.json({ error: `${stack.id} keeps no secrets in Infisical` }, 404);
    return stack;
  };
  const lastLine = (out: string) => out.trim().split("\n").filter(Boolean).pop()?.replace(/^error: /, "") ?? "the tool failed";

  // Names of the secrets, never their values.
  app.get("/api/stacks/:id/secrets", authorize("secrets:manage"), async (c) => {
    const stack = await managed(c, true);
    if (stack instanceof Response) return stack;
    const { code, out } = await stacks.secretTool(["list", stack.id, "--json"]);
    if (code !== 0) return c.json({ error: lastLine(out) }, 502);
    try {
      return c.json({ keys: JSON.parse(out.trim().split("\n").pop() ?? "[]") as SecretKey[], redeploy: stack.komodoStack });
    } catch {
      return c.json({ error: "the tool's answer was not readable" }, 502);
    }
  });

  // Creates or changes one secret: the typed value, or a generated one (never shown). Takes effect with the next redeploy.
  app.put("/api/stacks/:id/secrets/:key", authorize("secrets:manage"), async (c) => {
    const stack = await managed(c, true);
    if (stack instanceof Response) return stack;
    const key = c.req.param("key");
    if (!KEY.test(key)) return c.json({ error: "a name is letters, digits and _, like an environment variable" }, 400);
    const b = await body(c);
    const value = typeof b.value === "string" ? b.value : undefined;
    if (b.generate !== true && (!value || value.length > 4096)) return c.json({ error: "give a value (up to 4096 characters), or generate one" }, 400);
    const { code, out } = b.generate === true
      ? await stacks.secretTool(["set", stack.id, key, "--generate"])
      : await stacks.secretTool(["set", stack.id, key], value);
    const clean = value ? out.replaceAll(value, "***") : out;
    if (code !== 0) return c.json({ error: lastLine(clean) }, 502);
    console.log(`secret ${stack.id}/${key} ${b.generate === true ? "generated" : "set"} by ${c.get("principal").name}`);
    return c.json({ result: /created/.test(clean) ? "created" : "changed", redeploy: stack.komodoStack });
  });

  app.delete("/api/stacks/:id/secrets/:key", authorize("secrets:manage"), async (c) => {
    const stack = await managed(c, true);
    if (stack instanceof Response) return stack;
    const key = c.req.param("key");
    if (!KEY.test(key)) return c.json({ error: "a name is letters, digits and _, like an environment variable" }, 400);
    const { code, out } = await stacks.secretTool(["delete", stack.id, key, "--yes", ...(c.req.query("force") === "true" ? ["--force"] : [])]);
    if (code !== 0) return c.json({ error: lastLine(out) }, /still uses/.test(out) ? 409 : 502);
    console.log(`secret ${stack.id}/${key} deleted by ${c.get("principal").name}`);
    return c.json({ redeploy: stack.komodoStack });
  });

  // Redeploys the stack through Komodo (docker compose pull + up) as a job with its log.
  app.post("/api/stacks/:id/deploy", async (c, next) => (create.problem ? c.json({ error: create.problem }, 503) : next()), authorize("stacks:deploy"), async (c) => {
    const stack = await managed(c, false);
    if (stack instanceof Response) return stack;
    const by = c.get("principal").name;
    const job = create.applier.startDeploy(stack.komodoStack, by);
    if (!job) return c.json({ error: "another job is running, wait for it to finish" }, 409);
    console.log(`deploy ${stack.komodoStack} requested by ${by}`);
    return c.json({ jobId: job.id }, 202);
  });

  app.get("/api/apps/jobs/:id", (c) => {
    const job = create.applier.get(c.req.param("id"));
    return job ? c.json(create.applier.snapshot(job)) : c.json({ error: "unknown job" }, 404);
  });

  // The job's output: everything so far, then each chunk as it arrives, then one `done` event with the result.
  app.get("/api/apps/jobs/:id/events", (c) => {
    const job = create.applier.get(c.req.param("id"));
    if (!job) return c.json({ error: "unknown job" }, 404);
    return streamSSE(c, async (stream) => {
      let chain: Promise<unknown> = Promise.resolve();
      const send = (event: string, data: string) => (chain = chain.then(() => stream.writeSSE({ event, data })));
      let finish!: () => void;
      const finished = new Promise<void>((r) => (finish = r));
      send("log", JSON.stringify(job.log));
      const listener = (chunk: string | null) => {
        if (chunk !== null) return void send("log", JSON.stringify(chunk));
        send("done", JSON.stringify(create.applier.snapshot(job)));
        finish();
      };
      if (job.state === "running") job.subscribers.add(listener);
      else listener(null);
      stream.onAbort(() => void job.subscribers.delete(listener));
      await finished;
      await chain;
    });
  });

  // Pushes the status on connect and on every change; the comment line keeps idle proxies from closing the stream.
  app.get("/api/events", (c) =>
    streamSSE(c, async (stream) => {
      const send = (s: StatusSnapshot) => stream.writeSSE({ event: "status", data: JSON.stringify(s) });
      await send(status.get());
      const off = status.subscribe((s) => void send(s));
      stream.onAbort(off);
      while (!stream.aborted) {
        await stream.sleep(25_000);
        await stream.write(": keepalive\n\n");
      }
    }),
  );

  app.use("/*", serveStatic({ root: webDir }));
  // Single-page app: unknown non-API paths get index.html.
  app.get("*", serveStatic({ path: `${webDir}/index.html` }));
  return app;
}
