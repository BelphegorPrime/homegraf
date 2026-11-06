import { describe, expect, it } from "vitest";
import { parse } from "yaml";
import { diff, formChanges, mergeFormChange } from "./composeMerge.js";

const gen = (id: string, o: { image?: string; port?: string; blocks?: ("postgres" | "valkey")[] } = {}) => {
  const blocks = o.blocks ?? [];
  const lines = [
    "# Created with ./homelab.py (New app).",
    "services:",
    `  ${id}:`,
    `    image: ${o.image ?? "x:1"}`,
    `    container_name: ${id}`,
    "    restart: unless-stopped",
    "    ports:",
    `      - ${o.port ?? "8000"}:80`,
  ];
  const env = [
    ...(blocks.includes("postgres") ? ["      POSTGRES_URL: postgresql://app:${POSTGRES_PASSWORD}@postgres:5432/app"] : []),
    ...(blocks.includes("valkey") ? ["      VALKEY_URL: redis://valkey:6379"] : []),
  ];
  if (env.length) lines.push("    environment:", ...env);
  lines.push("    volumes:", `      - /srv/docker/${id}:/data`);
  if (blocks.length) lines.push("    depends_on:", ...blocks.flatMap((b) => [`      ${b}:`, "        condition: service_healthy"]));
  let text = lines.join("\n") + "\n";
  if (blocks.includes("postgres"))
    text += `\n  postgres:\n    image: postgres:18\n    volumes:\n      - /srv/docker/${id}/postgres:/var/lib/postgresql\n    healthcheck:\n      test: ["CMD-SHELL", "pg_isready -U app -d app"]\n`;
  if (blocks.includes("valkey")) text += `\n  valkey:\n    image: valkey/valkey:9-alpine\n    healthcheck:\n      test: ["CMD", "valkey-cli", "ping"]\n`;
  return text;
};
const ids = (oldId: string, newId = oldId) => ({ oldId, newId });
const merged = (text: string, before: string, after: string, i = ids("demo")) => {
  const m = mergeFormChange(text, before, after, i);
  if (!m.ok) throw new Error(m.error);
  return m.text;
};

// What the user made of the generated file: comments, an extra port, extra environment, an extra service.
// (Writing a changed file normalises the space before a trailing comment to one; everything else stays.)
const handmade = `# my notes
services:
  demo:
    image: x:1
    container_name: demo
    restart: unless-stopped
    ports:
      - 8000:80
      - 9999:81 # metrics
    environment:
      MY_FLAG: "on"
    volumes:
      - /srv/docker/demo:/data
    labels:
      owner: me

  sidecar:
    image: busybox:1 # keep
`;

describe("diff", () => {
  it("compares maps key by key and lists of scalars item by item", () => {
    expect(diff({ a: { b: 1, c: 2 }, l: ["x", "y"] }, { a: { b: 1, d: 3 }, l: ["y", "z"] })).toEqual([
      { kind: "delete", path: ["a", "c"] },
      { kind: "set", path: ["a", "d"], value: 3 },
      { kind: "listRemove", path: ["l"], item: "x" },
      { kind: "listAdd", path: ["l"], item: "z" },
    ]);
  });
  it("is empty for equal input and treats lists of objects as one value", () => {
    expect(diff(parse(gen("demo")), parse(gen("demo")))).toEqual([]);
    expect(diff({ l: [{ a: 1 }] }, { l: [{ a: 2 }] })).toEqual([{ kind: "set", path: ["l"], value: [{ a: 2 }] }]);
  });
  it("sees a renamed app service as a rename, not as delete and add", () => {
    const ops = formChanges(parse(gen("old")), parse(gen("new")), ids("old", "new"));
    expect(ops[0]).toEqual({ kind: "rename", path: ["services", "old"], to: "new" });
    expect(ops.some((o) => o.kind === "delete" && o.path.join() === "services,old")).toBe(false);
    expect(ops).toContainEqual({ kind: "set", path: ["services", "new", "container_name"], value: "new" });
  });
});

describe("mergeFormChange keeps the user's own text", () => {
  it("changes the image and nothing else", () => {
    const out = merged(handmade, gen("demo"), gen("demo", { image: "x:2" }));
    expect(out).toBe(handmade.replace("image: x:1\n    container_name", "image: x:2\n    container_name"));
  });

  it("replaces only the generated port entry and keeps the one the user added", () => {
    const out = merged(handmade, gen("demo"), gen("demo", { port: "8123" }));
    expect(out).toContain("- 8123:80");
    expect(out).not.toContain("- 8000:80");
    expect(out).toContain("- 9999:81 # metrics");
    expect(out).toContain("# my notes");
  });

  it("adds a building block: its service, the dependency and the connection variable, next to the user's edits", () => {
    const out = merged(handmade, gen("demo"), gen("demo", { blocks: ["postgres"] }));
    const doc = parse(out);
    expect(Object.keys(doc.services)).toEqual(["demo", "sidecar", "postgres"]);
    expect(doc.services.demo.depends_on).toEqual({ postgres: { condition: "service_healthy" } });
    expect(doc.services.demo.environment).toEqual({ MY_FLAG: "on", POSTGRES_URL: "postgresql://app:${POSTGRES_PASSWORD}@postgres:5432/app" });
    expect(doc.services.demo.labels).toEqual({ owner: "me" });
    expect(out).toContain("image: busybox:1 # keep");
    expect(doc.services.postgres.healthcheck.test).toEqual(["CMD-SHELL", "pg_isready -U app -d app"]);
  });

  it("removes a building block with everything the form added for it, and the empty sections it leaves", () => {
    const withBlock = merged(gen("demo"), gen("demo"), gen("demo", { blocks: ["postgres"] }));
    const out = merged(withBlock, gen("demo", { blocks: ["postgres"] }), gen("demo"));
    expect(parse(out)).toEqual(parse(gen("demo")));
    expect(out).not.toContain("environment:");
    expect(out).not.toContain("depends_on:");
  });

  it("keeps the user's environment when the last generated variable goes", () => {
    const withBlock = merged(handmade, gen("demo"), gen("demo", { blocks: ["valkey"] }));
    const out = merged(withBlock, gen("demo", { blocks: ["valkey"] }), gen("demo"));
    expect(parse(out).services.demo.environment).toEqual({ MY_FLAG: "on" });
    expect(parse(out).services.valkey).toBeUndefined();
  });

  it("renames the app's service and its paths, and keeps what the user added to it", () => {
    const out = merged(handmade, gen("demo"), gen("shop"), ids("demo", "shop"));
    const doc = parse(out);
    expect(Object.keys(doc.services)).toEqual(["shop", "sidecar"]);
    expect(doc.services.shop).toMatchObject({ container_name: "shop", environment: { MY_FLAG: "on" }, labels: { owner: "me" } });
    expect(doc.services.shop.volumes).toEqual(["/srv/docker/shop:/data"]);
    expect(doc.services.shop.ports).toEqual(["8000:80", "9999:81"]);
    expect(out).toContain("# my notes");
  });

  it("adds to the user's own sections instead of replacing them", () => {
    const noPorts = (id: string) => gen(id).replace(/    ports:\n      - \d+:80\n/, "");
    const user = 'services:\n  demo:\n    image: x:1\n    ports:\n      - 7000:70\n    environment:\n      MINE: "1"\n';
    const out = merged(user, noPorts("demo"), gen("demo", { blocks: ["valkey"] }));
    expect(parse(out).services.demo.ports).toEqual(["7000:70", "8000:80"]);
    expect(parse(out).services.demo.environment).toEqual({ MINE: "1", VALKEY_URL: "redis://valkey:6379" });
  });

  it("drops a whole service the form added, also when the user changed it", () => {
    const withBlock = merged(gen("demo"), gen("demo"), gen("demo", { blocks: ["valkey"] }));
    const tweaked = withBlock.replace("valkey/valkey:9-alpine", "valkey/valkey:9-alpine\n    mem_limit: 1g");
    expect(parse(merged(tweaked, gen("demo", { blocks: ["valkey"] }), gen("demo"))).services.valkey).toBeUndefined();
  });

  it("removes only the form's network from the shared top-level section, and the section when it is empty", () => {
    const withNet = (extra: string) =>
      `services:\n  demo:\n    image: x:1\n    networks:\n      - default\n      - ext\nnetworks:\n  ext:\n    external: true\n${extra}`;
    const before = withNet(""),
      after = "services:\n  demo:\n    image: x:1\n";
    const mine = withNet("  mine: {}\n");
    const out = merged(mine, before, after);
    expect(parse(out).networks).toEqual({ mine: {} });
    expect(parse(out).services.demo.networks).toBeUndefined();
    expect(parse(merged(before, before, after))).toEqual(parse(after)); // nothing of the user's: the section goes
  });

  it("an unchanged form leaves the text byte for byte alone", () => {
    const m = mergeFormChange(handmade, gen("demo"), gen("demo"), ids("demo"));
    expect(m).toEqual({ ok: true, text: handmade, changed: false });
  });

  it("is idempotent: applying a change the user already made by hand changes nothing", () => {
    const byHand = handmade.replace("x:1", "x:2");
    const m = mergeFormChange(byHand, gen("demo"), gen("demo", { image: "x:2" }), ids("demo"));
    expect(m).toMatchObject({ ok: true, text: byHand });
  });

  it("refuses text that is not valid YAML or has no services, and says why", () => {
    const broken = mergeFormChange("services:\n  demo: [\n", gen("demo"), gen("demo", { image: "x:2" }), ids("demo"));
    expect(broken.ok).toBe(false);
    const none = mergeFormChange("version: '3'\n", gen("demo"), gen("demo", { image: "x:2" }), ids("demo"));
    expect(none).toEqual({ ok: false, error: "the file has no `services:` mapping" });
  });
});
