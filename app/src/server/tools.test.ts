import { afterEach, describe, expect, it, vi } from "vitest";
import { planner } from "./apps.js";
import { toolPath, toolRoot } from "./tools.js";

afterEach(() => vi.unstubAllEnvs());

describe("toolPath", () => {
  it("is the tools' own folder when TOOLS_DIR is set (the image)", () =>
    expect(toolPath("/repo", "homelab.py", { TOOLS_DIR: "/opt/homegraf" })).toBe("/opt/homegraf/homelab.py"));
  it("is the repository itself without it (development, tests)", () =>
    expect(toolPath("/repo", "infra/graph/build.py", {})).toBe("/repo/infra/graph/build.py"));
  it("ignores an empty TOOLS_DIR", () => expect(toolPath("/work", "homelab.py", { TOOLS_DIR: "" })).toBe("/work/homelab.py"));
});

describe("toolRoot", () => {
  it("names the repository the tool works on", () => expect(toolRoot("/work")).toEqual({ HOMELAB_ROOT: "/work" }));
});

describe("a tool call from the app", () => {
  it("runs the tool of the image on the repository checkout", async () => {
    vi.stubEnv("TOOLS_DIR", "/opt/homegraf");
    const calls: [string, string[], string | undefined][] = [];
    const run = async (file: string, args: string[], root?: string) => {
      calls.push([file, args, root]);
      return { code: 0, out: "" };
    };
    await planner(
      "/repo",
      run,
    )({
      appId: "demo",
      server: "worker-1",
      image: "nginx:1",
      routing: true,
      addons: [],
      files: {},
      volumes: [],
      networks: [],
      env: {},
      secretVars: [],
      cpus: "",
      memory: "",
      gpu: "",
      forceHttps: true,
      websockets: false,
    } as never);
    expect(calls[0][0]).toBe("/opt/homegraf/homelab.py");
    expect(calls[0][2]).toBe("/repo");
  });
});
