import { describe, expect, it } from "vitest";
import type { Exposure, Graph } from "../shared/types";
import { buildView, catalogTiles, defaultOptions, exposures, isFullHost, isPublic } from "./views";

const publicHost: Exposure = { domain: "app.lab.example", reason: "proxy-public", paths: ["/api"], methods: ["GET", "HEAD"] };

const g: Graph = {
  generated: "now",
  warnings: [],
  nodes: [
    { id: "machine:nas", type: "machine", label: "nas" },
    { id: "machine:idle", type: "machine", label: "idle" },
    { id: "stack:app", type: "stack", label: "app", server: "nas", urls: ["https://app.lab.example"], category: "Media", exposure: [publicHost] },
    { id: "stack:db", type: "stack", label: "db", server: "nas" },
    { id: "infra:komodo", type: "infra", label: "Komodo" },
    { id: "infra:infisical", type: "infra", label: "Infisical" },
    { id: "infra:npm", type: "infra", label: "NPM" },
    { id: "domain:app.lab.example", type: "domain", label: "app.lab.example", exposure: publicHost },
    { id: "domain:db.example.org", type: "domain", label: "db.example.org" },
  ],
  edges: [
    { id: "h1", source: "machine:nas", target: "stack:app", type: "hosts" },
    { id: "h2", source: "machine:nas", target: "stack:db", type: "hosts" },
    { id: "d1", source: "stack:app", target: "stack:db", type: "depends-on" },
    { id: "k1", source: "stack:app", target: "infra:komodo", type: "deployed-by" },
    { id: "s1", source: "stack:db", target: "infra:infisical", type: "uses-secrets" },
    { id: "t1", source: "domain:app.lab.example", target: "infra:npm", type: "terminated-by" },
    { id: "t2", source: "domain:db.example.org", target: "infra:npm", type: "terminated-by" },
    { id: "p1", source: "infra:npm", target: "stack:app", type: "proxies", domains: ["app.lab.example"] },
    { id: "p2", source: "infra:npm", target: "stack:db", type: "proxies", domains: ["db.example.org"] },
  ],
};

const node = (id: string) => g.nodes.find((n) => n.id === id)!;

describe("exposure helpers", () => {
  it("wraps the single entry of a domain and keeps the array of a stack", () => {
    expect(exposures(node("domain:app.lab.example"))).toEqual([publicHost]);
    expect(exposures(node("stack:app"))).toEqual([publicHost]);
    expect(exposures(node("stack:db"))).toEqual([]);
  });

  it("isPublic is true only where the builder wrote an exposure", () => {
    expect(isPublic(node("domain:app.lab.example"))).toBe(true);
    expect(isPublic(node("stack:app"))).toBe(true);
    expect(isPublic(node("stack:db"))).toBe(false);
  });

  it("isFullHost treats a missing or empty path list as the whole host", () => {
    expect(isFullHost(publicHost)).toBe(false);
    expect(isFullHost({ domain: "nas.example.org", reason: "public-name" })).toBe(true);
    expect(isFullHost({ ...publicHost, paths: [] })).toBe(true);
  });
});

describe("views", () => {
  it("hosting keeps machines without stacks", () => {
    const v = buildView("hosting", g);
    expect(v.nodes.map((n) => n.id)).toContain("machine:idle");
    expect(v.edges).toHaveLength(2);
  });

  it("dependencies shows only connected stacks", () => {
    const v = buildView("dependencies", g);
    expect(v.nodes.map((n) => n.id).sort()).toEqual(["stack:app", "stack:db"]);
  });

  it("platform draws only the focused infra service's dependents", () => {
    const v = buildView("platform", g, { ...defaultOptions, focus: "infra:komodo" });
    expect(v.edges.map((e) => e.id)).toEqual(["k1"]);
    expect(v.nodes.map((n) => n.id)).toContain("infra:infisical");
    expect(v.nodes.map((n) => n.id)).not.toContain("stack:db");
  });

  it("request draws both domains by default", () => {
    const v = buildView("request", g);
    expect(v.nodes.map((n) => n.id).sort()).toEqual(["domain:app.lab.example", "domain:db.example.org", "infra:npm", "stack:app", "stack:db"]);
  });

  it("request with publicOnly drops the private domain and its target", () => {
    const v = buildView("request", g, { ...defaultOptions, publicOnly: true });
    expect(v.nodes.map((n) => n.id).sort()).toEqual(["domain:app.lab.example", "infra:npm", "stack:app"]);
    expect(v.edges.map((e) => e.id).sort()).toEqual(["p1", "t1"]);
  });

  it("catalog hides stacks without URL unless asked, grouped by category", () => {
    expect(catalogTiles(g, "category", false).map((t) => [t.node.id, t.group])).toEqual([["stack:app", "Media"]]);
    expect(catalogTiles(g, "machine", true)).toHaveLength(2);
  });

  it("catalog with publicOnly returns only the public stack", () => {
    expect(catalogTiles(g, "machine", true, true).map((t) => t.node.id)).toEqual(["stack:app"]);
  });

  it("default options leave the public filter off", () => {
    expect(defaultOptions.publicOnly).toBe(false);
  });
});
