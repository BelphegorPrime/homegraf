import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { testCreations, testHosts, testRemoval, testStacks } from "./testing.js";
import { createApp } from "./app.js";
import { applier } from "./apply.js";
import { loadConfig } from "./config.js";
import { userStore } from "./users.js";
import {
  addonCatalog,
  newAppArgs,
  planInput,
  planner,
  previewer,
  removableCatalog,
  secretValueEnv,
  shellCommand,
  stackCatalog,
  templateCatalog,
  type Runner,
} from "./apps.js";

const parse = (v: unknown) => planInput.parse(v);

describe("new-app arguments", () => {
  it("maps the form to flags and generates secrets, never --yes", () => {
    const args = newAppArgs(
      parse({
        appId: "whoami-test",
        server: "worker-1",
        image: "traefik/whoami:v1.11.0",
        containerPort: 80,
        hostPort: 8123,
        mount: "/data",
        secretVars: ["API_TOKEN"],
        websockets: true,
      }),
    );
    expect(args).toEqual([
      "new-app",
      "--app-id",
      "whoami-test",
      "--server",
      "worker-1",
      "--generate-secrets",
      "--image",
      "traefik/whoami:v1.11.0",
      "--container-port",
      "80",
      "--host-port",
      "8123",
      "--mount",
      "/data",
      "--secret-var",
      "API_TOKEN",
      "--secret",
      "API_TOKEN=generate",
      "--web-port",
      "8123",
      "--websockets",
      "--force-https",
    ]);
    expect(args).not.toContain("--yes");
  });

  it("drops the routing flags without routing", () => {
    const args = newAppArgs(parse({ appId: "worker", server: "aws", routing: false, domain: "x.lab.example" }));
    expect(args).toContain("--no-routing");
    expect(args).not.toContain("--domain");
  });

  it("the copy-paste command adds --yes and quotes what needs it", () => {
    expect(shellCommand(["new-app", "--app-id", "a b"])).toBe("./homelab.py new-app --app-id 'a b' --yes");
  });
});

describe("input validation", () => {
  it.each([
    [{ appId: "Bad_Id", server: "aws" }],
    [{ appId: "ok-id", server: "aws; rm -rf /" }],
    [{ appId: "ok-id", server: "aws", image: "x --yes" }],
    [{ appId: "ok-id", server: "aws", secretVars: ["A B"] }],
    [{ appId: "ok-id", server: "aws", containerPort: 70000 }],
    [{ appId: "ok-id", server: "aws", mount: "data" }],
  ])("rejects %j", (input) => {
    expect(planInput.safeParse(input).success).toBe(false);
  });
});

describe("planner", () => {
  it("runs the repo's homelab.py with the arguments and cleans the output", async () => {
    const calls: [string, string[]][] = [];
    const run: Runner = async (file, args) => {
      calls.push([file, args]);
      return { code: 0, out: "\x1b[1mSummary\x1b[0m\n  app x\n" };
    };
    const res = await planner("/repo", run)(parse({ appId: "x1", server: "aws" }));
    expect(calls[0][0]).toBe("/repo/homelab.py");
    expect(res).toMatchObject({ ok: true, exitCode: 0, output: "Summary\n  app x" });
  });

  it("reports a failing tool run as not ok, with its message", async () => {
    const run: Runner = async () => ({ code: 1, out: "error: unknown server\n" });
    expect(await planner("/repo", run)(parse({ appId: "x1", server: "nope" }))).toMatchObject({ ok: false, exitCode: 1, output: "error: unknown server" });
  });

  it("runs one plan at a time", async () => {
    const order: string[] = [];
    const run: Runner = async (_f, args) => {
      order.push(`start ${args[2]}`);
      await new Promise((r) => setTimeout(r, 20));
      order.push(`end ${args[2]}`);
      return { code: 0, out: "" };
    };
    const plan = planner("/repo", run);
    await Promise.all([plan(parse({ appId: "aa", server: "s" })), plan(parse({ appId: "bb", server: "s" }))]);
    expect(order).toEqual(["start aa", "end aa", "start bb", "end bb"]);
  });
});

describe("POST /api/apps/plan", () => {
  const app = createApp({
    loadGraph: async () => {
      throw new Error("unused");
    },
    status: { get: () => ({ updated: null, source: "disabled", nodes: {} }), subscribe: () => () => {} },
    webDir: "dist/web",
    plan: async (i) => ({ ok: true, exitCode: 0, output: `plan for ${i.appId}`, command: "x" }),
    preview: async (i) => ({ ok: true, compose: `services:\n  ${i.appId}: {}\n`, secrets: [], ports: [] }),
    addons: async () => [
      {
        id: "redis",
        label: "Redis",
        description: "cache",
        secrets: [],
        image: "redis:8-alpine",
        ui: false,
        requiresAny: [],
        publishesPort: false,
        urlPath: "/",
        exclusive: false,
        vpn: false,
      },
    ],
    templates: async () => [],
    unifi: () => ({ enabled: false }),
    certificates: { get: async () => ({ updated: null, certificates: [] }) },
    removal: testRemoval(),
    creations: testCreations(),
    hosts: testHosts(),
    stacks: testStacks(),
    create: { problem: undefined, applier: applier({ cfg: loadConfig({}) }) },
    users: {
      enabled: true,
      authenticate: async () => ({ ok: true, principal: { name: "admin", permissions: [], source: "builtin" } }),
      store: userStore(join(mkdtempSync(join(tmpdir(), "homegraf-")), "users.json")),
    },
  });
  const post = (body: unknown) =>
    app.request("/api/apps/plan", { method: "POST", body: JSON.stringify(body), headers: { "content-type": "application/json" } });

  it("returns the plan", async () => {
    const res = await post({ appId: "demo-app", server: "aws" });
    expect(res.status).toBe(200);
    expect((await res.json()).output).toBe("plan for demo-app");
  });

  it("answers 400 with the offending fields", async () => {
    const res = await post({ appId: "no pe!", server: "aws" });
    expect(res.status).toBe(400);
    expect((await res.json()).issues[0].field).toBe("appId");
  });

  it("answers 400 for a body that is not JSON", async () => {
    expect((await app.request("/api/apps/plan", { method: "POST", body: "nope" })).status).toBe(400);
  });
});

describe("building blocks and an edited compose file", () => {
  it("maps addons to --addon flags", () => {
    const args = newAppArgs(parse({ appId: "demo-app", server: "aws", image: "x:1", addons: ["postgres", "redis"] }));
    expect(args).toEqual(expect.arrayContaining(["--addon", "postgres", "--addon", "redis"]));
  });

  it("with a compose file the container fields are dropped, secrets are generated, typed ones come from the environment", () => {
    const input = parse({
      appId: "demo-app",
      server: "aws",
      image: "x:1",
      containerPort: 80,
      hostPort: 8123,
      addons: ["redis"],
      secretVars: ["A"],
      compose: "services: {}",
      webPort: 9000,
      secretValues: { DB_PASS: "s3cret-value" },
    });
    const args = newAppArgs(input, "/tmp/x/compose.yaml");
    expect(args).toEqual(
      expect.arrayContaining([
        "--compose-file",
        "/tmp/x/compose.yaml",
        "--generate-secrets",
        "--secret",
        "DB_PASS=env:SECRET_VALUE_DB_PASS",
        "--web-port",
        "9000",
      ]),
    );
    for (const dropped of ["--image", "--container-port", "--host-port", "--addon", "--secret-var"]) expect(args).not.toContain(dropped);
    expect(args.join(" ")).not.toContain("s3cret-value");
    expect(secretValueEnv("DB_PASS")).toBe("SECRET_VALUE_DB_PASS");
  });

  it("the plan gets the compose file as a temporary file, no typed secrets, and removes the file afterwards", async () => {
    let seen: { args: string[]; text: string } | undefined;
    const { readFileSync, existsSync } = await import("node:fs");
    const run: Runner = async (_f, args) => {
      seen = { args, text: readFileSync(args[args.indexOf("--compose-file") + 1], "utf8") };
      return { code: 0, out: "plan" };
    };
    const res = await planner("/repo", run)(parse({ appId: "demo-app", server: "aws", compose: "services:\n  web: {}\n", secretValues: { K: "v1" } }));
    expect(seen!.text).toBe("services:\n  web: {}\n");
    expect(seen!.args.join(" ")).not.toMatch(/SECRET_VALUE/);
    expect(existsSync(seen!.args[seen!.args.indexOf("--compose-file") + 1])).toBe(false);
    expect(res.command).toContain("--compose-file compose.yaml");
    expect(res.command).not.toContain("/tmp");
  });

  it("the preview adds --print-compose and reads the tool's JSON, or its error", async () => {
    const good: Runner = async () => ({
      code: 0,
      out: JSON.stringify({ compose: "c", existing: false, secrets: ["A"], ports: [{ service: "web", host: 8000, container: 80 }] }),
    });
    expect(await previewer("/repo", good)(parse({ appId: "demo-app", server: "aws", image: "x:1" }))).toEqual({
      ok: true,
      compose: "c",
      existing: false,
      secrets: ["A"],
      ports: [{ service: "web", host: 8000, container: 80 }],
    });
    let args: string[] = [];
    await previewer("/repo", async (_f, a) => {
      args = a;
      return good("", []);
    })(parse({ appId: "demo-app", server: "aws" }));
    expect(args.at(-1)).toBe("--print-compose");
    const bad: Runner = async () => ({ code: 1, out: "\nerror: --compose-file: needs a `services:` mapping\n" });
    expect(await previewer("/repo", bad)(parse({ appId: "demo-app", server: "aws" }))).toEqual({
      ok: false,
      error: "--compose-file: needs a `services:` mapping",
    });
    expect(await previewer("/repo", async () => ({ code: 0, out: "not json" }))(parse({ appId: "demo-app", server: "aws" }))).toMatchObject({ ok: false });
  });

  it("lists the building blocks and keeps the answer for a while", async () => {
    let calls = 0,
      now = 0;
    const run: Runner = async () => {
      calls++;
      return { code: 0, out: '[{"id":"redis","label":"Redis","description":"d","secrets":[],"image":"redis:8"}]' };
    };
    const list = addonCatalog("/repo", run, 1000, () => now);
    expect((await list())[0].id).toBe("redis");
    await list();
    expect(calls).toBe(1);
    now = 1001;
    await list();
    expect(calls).toBe(2);
    await expect(addonCatalog("/repo", async () => ({ code: 1, out: "boom" }))()).rejects.toThrow(/addons failed/);
  });

  it("sees a freshly deployed app within seconds: the lists of apps are not kept for a minute", async () => {
    let apps: string[] = [],
      now = 0;
    const run: Runner = async () => ({ code: 0, out: JSON.stringify(apps.map((id) => ({ id }))) });
    const removable = removableCatalog("/repo", run, undefined, () => now);
    const stacks = stackCatalog("/repo", run, undefined, () => now);
    expect(await removable()).toEqual([]);
    apps = ["web"]; // update-all pulled a merge
    now = 10_000; // the 60 s of a building-block list would still hide it
    expect(await removable()).toEqual([{ id: "web" }]);
    expect(await stacks()).toEqual([{ id: "web" }]);
  });

  it("validates addons, the compose size and typed secret names", () => {
    expect(planInput.safeParse({ appId: "demo-app", server: "aws", addons: ["Bad Name"] }).success).toBe(false);
    expect(planInput.safeParse({ appId: "demo-app", server: "aws", compose: "x".repeat(200_001) }).success).toBe(false);
    expect(planInput.safeParse({ appId: "demo-app", server: "aws", secretValues: { "A B": "v" } }).success).toBe(false);
    expect(planInput.safeParse({ appId: "demo-app", server: "aws", secretValues: { A: "" } }).success).toBe(false);
  });
});

describe("volumes, networks, limits, GPU and environment", () => {
  it("map to the tool's flags", () => {
    const args = newAppArgs(
      parse({
        appId: "demo-app",
        server: "aws",
        image: "x:1",
        volumes: [
          { kind: "data", source: "", target: "/data" },
          { kind: "bind", source: "/mnt/tank/media", target: "/media", readOnly: true },
          { kind: "tmpfs", target: "/tmp", size: "512m" },
          { kind: "named", source: "cache", target: "/cache" },
        ],
        networks: [{ name: "internal" }, { name: "protonmail_default", external: true }],
        cpus: "1.5",
        memory: "2g",
        gpu: "nvidia",
        env: { TZ: "Europe/Berlin", URL: "http://x:8080/a?b=c" },
      }),
    );
    expect(args).toEqual(
      expect.arrayContaining([
        "--volume",
        "data::/data",
        "--volume",
        "bind:/mnt/tank/media:/media:ro",
        "--volume",
        "tmpfs::/tmp:size=512m",
        "--volume",
        "named:cache:/cache",
        "--network",
        "internal",
        "--external-network",
        "protonmail_default",
        "--cpus",
        "1.5",
        "--memory",
        "2g",
        "--gpu",
        "nvidia",
        "--env",
        "TZ=Europe/Berlin",
        "--env",
        "URL=http://x:8080/a?b=c",
      ]),
    );
  });

  it("add nothing when empty, and are not used with a compose file (the file says it all)", () => {
    const empty = newAppArgs(parse({ appId: "demo-app", server: "aws", image: "x:1" }));
    for (const f of ["--volume", "--network", "--external-network", "--cpus", "--memory", "--gpu", "--env"]) expect(empty).not.toContain(f);
    const withFile = newAppArgs(parse({ appId: "demo-app", server: "aws", volumes: [{ kind: "data", target: "/d" }], memory: "1g" }), "/tmp/c.yaml");
    expect(withFile).not.toContain("--volume");
    expect(withFile).not.toContain("--memory");
  });

  it("are validated", () => {
    const bad = (extra: object) => planInput.safeParse({ appId: "demo-app", server: "aws", ...extra }).success;
    expect(bad({ volumes: [{ kind: "disk", target: "/x" }] })).toBe(false);
    expect(bad({ volumes: [{ kind: "data", target: "relative" }] })).toBe(false);
    expect(bad({ volumes: [{ kind: "tmpfs", target: "/t", size: "big" }] })).toBe(false);
    expect(bad({ networks: [{ name: "bad name" }] })).toBe(false);
    expect(bad({ cpus: "lots" })).toBe(false);
    expect(bad({ memory: "0g" })).toBe(false);
    expect(bad({ gpu: "amd" })).toBe(false);
    expect(bad({ env: { "1BAD": "x" } })).toBe(false);
    expect(bad({ volumes: [{ kind: "data", target: "/x" }], networks: [{ name: "n" }], cpus: "2", memory: "1g", gpu: "dri", env: { A: "b" } })).toBe(true);
  });
});

describe("templates and their files", () => {
  it("maps the template and the files folder to flags, with or without a compose file", () => {
    const input = parse({ appId: "demo-app", server: "aws", template: "nginx", files: { "default.conf": "x" } });
    expect(newAppArgs(input, undefined, "/tmp/w/files")).toEqual(expect.arrayContaining(["--template", "nginx", "--files-dir", "/tmp/w/files"]));
    expect(newAppArgs(input, "/tmp/w/compose.yaml", "/tmp/w/files")).toEqual(
      expect.arrayContaining(["--compose-file", "/tmp/w/compose.yaml", "--template", "nginx"]),
    );
    expect(newAppArgs(parse({ appId: "demo-app", server: "aws", image: "x:1" }))).not.toContain("--template");
  });

  it("takes the app ID in any case and uses it in lowercase", () => {
    const input = parse({ appId: "EngineX", server: "aws", image: "x:1" });
    expect(input.appId).toBe("enginex");
    expect(newAppArgs(input).slice(0, 3)).toEqual(["new-app", "--app-id", "enginex"]);
    expect(planInput.safeParse({ appId: "Engine X", server: "aws" }).success).toBe(false); // still only letters, digits and dashes
  });

  it("passes further templates as more --template flags, the first one first", () => {
    const args = newAppArgs(parse({ appId: "demo-app", server: "aws", template: "mealie", extraTemplates: ["paperless", "kuma"] }));
    expect(args.filter((x, i) => args[i - 1] === "--template")).toEqual(["mealie", "paperless", "kuma"]);
    expect(newAppArgs(parse({ appId: "demo-app", server: "aws", image: "x:1", extraTemplates: ["kuma"] }))).not.toContain("--template"); // no first template: none
    expect(planInput.safeParse({ appId: "demo-app", server: "aws", template: "mealie", extraTemplates: ["No Good"] }).success).toBe(false);
  });

  it("writes the edited files below files/, also in folders, and removes everything afterwards", async () => {
    const { readFileSync, existsSync } = await import("node:fs");
    let seen: { args: string[]; conf: string; page: string; dir: string } | undefined;
    const run: Runner = async (_f, args) => {
      const dir = args[args.indexOf("--files-dir") + 1];
      seen = { args, dir, conf: readFileSync(`${dir}/default.conf`, "utf8"), page: readFileSync(`${dir}/html/index.html`, "utf8") };
      return { code: 0, out: "plan" };
    };
    const res = await planner(
      "/repo",
      run,
    )(parse({ appId: "demo-app", server: "aws", template: "nginx", files: { "default.conf": "A", "html/index.html": "B" } }));
    expect([seen!.conf, seen!.page]).toEqual(["A", "B"]);
    expect(existsSync(seen!.dir)).toBe(false);
    expect(res.command).toContain("--template nginx --files-dir files");
  });

  it("validates the template id and the file paths and sizes", () => {
    const ok = (extra: object) => planInput.safeParse({ appId: "demo-app", server: "aws", ...extra }).success;
    expect(ok({ template: "nginx", files: { "html/index.html": "x" } })).toBe(true);
    expect(ok({ template: "No Good" })).toBe(false);
    for (const bad of ["../etc/passwd", "/abs", "a/../b", "a//b", ""]) expect(ok({ files: { [bad]: "x" } })).toBe(false);
    expect(ok({ files: { a: "x".repeat(200_001) } })).toBe(false);
    expect(ok({ files: { a: "x".repeat(150_000), b: "x".repeat(150_000), c: "x".repeat(150_000) } })).toBe(false);
    expect(ok({ files: Object.fromEntries(Array.from({ length: 21 }, (_, i) => [`f${i}`, "x"])) })).toBe(false);
  });

  it("lists the templates through the tool and keeps the answer", async () => {
    let calls = 0;
    const run: Runner = async (_f, args) => {
      calls++;
      expect(args).toEqual(["templates", "--json"]);
      return { code: 0, out: '[{"id":"nginx","label":"nginx","description":"d","healthPath":"/","defaults":{},"files":["default.conf"]}]' };
    };
    const list = templateCatalog("/repo", run);
    expect((await list())[0].id).toBe("nginx");
    await list();
    expect(calls).toBe(1);
  });
});

describe("preview and addons routes", () => {
  const app = createApp({
    loadGraph: async () => {
      throw new Error("unused");
    },
    status: { get: () => ({ updated: null, source: "disabled", nodes: {} }), subscribe: () => () => {} },
    webDir: "dist/web",
    plan: async () => {
      throw new Error("unused");
    },
    preview: async (i) => ({ ok: true, compose: `for ${i.appId}` }),
    addons: async () => {
      throw new Error("no tool");
    },
    templates: async () => [],
    unifi: () => ({ enabled: false }),
    certificates: { get: async () => ({ updated: null, certificates: [] }) },
    removal: testRemoval(),
    creations: testCreations(),
    hosts: testHosts(),
    stacks: testStacks(),
    create: { problem: undefined, applier: applier({ cfg: loadConfig({}) }) },
    users: {
      enabled: true,
      authenticate: async () => ({ ok: true, principal: { name: "admin", permissions: [], source: "builtin" } }),
      store: userStore(join(mkdtempSync(join(tmpdir(), "homegraf-")), "users.json")),
    },
  });
  const post = (body: unknown) =>
    app.request("/api/apps/preview", { method: "POST", body: JSON.stringify(body), headers: { "content-type": "application/json" } });

  it("previews and rejects invalid input with the fields", async () => {
    expect(await (await post({ appId: "demo-app", server: "aws" })).json()).toEqual({ ok: true, compose: "for demo-app" });
    const bad = await post({ appId: "X", server: "aws" });
    expect(bad.status).toBe(400);
    expect((await bad.json()).issues[0].field).toBe("appId");
  });

  it("answers 502 when the tool cannot list the blocks", async () => {
    const res = await app.request("/api/addons");
    expect(res.status).toBe(502);
    expect((await res.json()).error).toBe("no tool");
  });
});
