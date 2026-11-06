import { describe, expect, it } from "vitest";
import type { Graph } from "../shared/types.js";
import { loadConfig } from "./config.js";
import { jobStatus, statusFromKomodo, statusPoller, unifiHealth } from "./status.js";

const graph = (nodes: Graph["nodes"]): Graph => ({ nodes, edges: [], warnings: [], generated: "t" }) as unknown as Graph;

describe("unifiHealth", () => {
  it("is ok when connected, in trouble when offline, a warning while the device changes", () => {
    expect(unifiHealth(1)).toEqual({ health: "ok", detail: "connected" });
    expect(unifiHealth(0)).toEqual({ health: "down", detail: "offline" });
    expect(unifiHealth(9).health).toBe("down");
    expect(unifiHealth(6).health).toBe("down");
    expect(unifiHealth(4)).toEqual({ health: "warn", detail: "updating" });
    expect(unifiHealth(11).health).toBe("warn");
    expect(unifiHealth(42)).toEqual({ health: "warn", detail: "state 42" });
  });
});

describe("statusFromKomodo and the UniFi devices", () => {
  it("gives the gateway, switches and access points the state their controller reports, and leaves other nodes alone", () => {
    const status = statusFromKomodo(
      graph([
        { id: "router:udm", type: "router", label: "UDM", unifiState: 1 },
        { id: "switch:core", type: "switch", label: "Core", unifiState: 1 },
        { id: "ap:attic", type: "device", label: "Attic AP", unifiState: 0 },
        { id: "router:placeholder", type: "router", label: "Router" }, // no snapshot: nothing known
        { id: "device:printer", type: "device", label: "Printer", online: true }, // a hand-kept device without UniFi state
      ] as Graph["nodes"]),
      [],
      [],
    );
    expect(status["router:udm"]).toEqual({ health: "ok", detail: "connected" });
    expect(status["ap:attic"]).toEqual({ health: "down", detail: "offline" });
    expect(status["router:placeholder"]).toBeUndefined();
    expect(status["device:printer"]).toBeUndefined();
  });
});

const ran = (state: string, status: string) => ({ service: "cli", container: { state, status } });

describe("jobStatus", () => {
  it("is ok when the last run exited with 0, and says when", () => {
    expect(jobStatus([ran("exited", "Exited (0) 16 hours ago")])).toEqual({ health: "ok", detail: "last run ok, 16 hours ago" });
  });

  it("is down with the exit code when a run failed, whichever service it was", () => {
    expect(jobStatus([ran("exited", "Exited (1) 2 hours ago")])).toEqual({ health: "down", detail: "last run failed (exit 1), 2 hours ago" });
    expect(jobStatus([ran("exited", "Exited (0) 3 hours ago"), ran("exited", "Exited (137) 3 hours ago")])?.health).toBe("down");
  });

  it("is ok while it runs, unknown before the first run", () => {
    expect(jobStatus([ran("running", "Up 3 seconds")])).toEqual({ health: "ok", detail: "running now" });
    expect(jobStatus([])).toEqual({ health: "unknown", detail: "no run yet" });
    expect(jobStatus([{ service: "cli", container: null }])?.health).toBe("unknown");
  });

  it("leaves it to the stack's own state when the containers say nothing clear", () => {
    expect(jobStatus([ran("restarting", "Restarting (1) 5 seconds ago")])).toBeUndefined();
    expect(jobStatus([ran("created", "Created")])).toBeUndefined();
    expect(jobStatus([ran("exited", "Exited")])).toBeUndefined();
    expect(jobStatus([ran("exited", "Exited (0) 1 hour ago"), ran("paused", "Up (Paused)")])).toBeUndefined();
  });
});

describe("statusFromKomodo and one-shot jobs", () => {
  const nodes = [
    { id: "stack:backup", type: "stack", label: "backup", job: true },
    { id: "stack:app", type: "stack", label: "app" },
    { id: "container:backup/cli", type: "container", label: "cli", stack: "backup" },
  ] as Graph["nodes"];
  const stacks = [
    { name: "backup", info: { state: "stopped", status: "exited(1)" } },
    { name: "app", info: { state: "stopped", status: "exited(1)" } },
  ];

  it("shows the last run instead of the stopped stack, and its containers follow", () => {
    const status = statusFromKomodo(graph(nodes), stacks, [], { backup: { health: "ok", detail: "last run ok, 16 hours ago" } });
    expect(status["stack:backup"]).toEqual({ health: "ok", detail: "last run ok, 16 hours ago", updateAvailable: undefined });
    expect(status["container:backup/cli"]).toEqual({ health: "ok" });
  });

  it("keeps a stopped stack that is not a job red, and a job without a known run as the stack says", () => {
    const status = statusFromKomodo(graph(nodes), stacks, [], { app: { health: "ok" } });
    expect(status["stack:app"].health).toBe("down");
    expect(status["stack:backup"]).toMatchObject({ health: "down", detail: "exited(1)" });
  });
});

describe("statusPoller and one-shot jobs", () => {
  const cfg = loadConfig({ KOMODO_URL: "http://komodo:9120", KOMODO_API_KEY: "k", KOMODO_API_SECRET: "s" });
  const nodes = [
    { id: "stack:backup", type: "stack", label: "backup", job: true },
    { id: "stack:app", type: "stack", label: "app" },
  ] as Graph["nodes"];
  const answers = (services: Response | Error) => {
    const asked: { type: string; body: string }[] = [];
    const fetchFn = (async (url: string, init: RequestInit) => {
      const type = String(url).split("/read/")[1];
      asked.push({ type, body: String(init.body) });
      if (type === "ListStacks")
        return new Response(
          JSON.stringify([
            { name: "backup", info: { state: "stopped", status: "exited(1)" } },
            { name: "app", info: { state: "running" } },
          ]),
        );
      if (type === "ListServers") return new Response("[]");
      if (services instanceof Error) throw services;
      return services;
    }) as unknown as typeof fetch;
    return { asked, fetchFn };
  };

  it("asks for the containers of the job stacks only, and shows the exit code of the last run", async () => {
    const { asked, fetchFn } = answers(new Response(JSON.stringify([ran("exited", "Exited (0) 16 hours ago")])));
    const poller = statusPoller(cfg, async () => graph(nodes), fetchFn);
    await poller.refresh();
    expect(asked.filter((a) => a.type === "ListStackServices")).toEqual([{ type: "ListStackServices", body: JSON.stringify({ stack: "backup" }) }]);
    expect(poller.get().nodes["stack:backup"]).toMatchObject({ health: "ok", detail: "last run ok, 16 hours ago" });
    expect(poller.get().nodes["stack:app"].health).toBe("ok");
  });

  it("falls back to the stack's own state when the containers cannot be read", async () => {
    for (const failing of [new Error("offline"), new Response("no", { status: 500 })]) {
      const poller = statusPoller(cfg, async () => graph(nodes), answers(failing).fetchFn);
      await poller.refresh();
      expect(poller.get()).toMatchObject({ source: "komodo", nodes: { "stack:backup": { health: "down", detail: "exited(1)" } } });
    }
  });
});
