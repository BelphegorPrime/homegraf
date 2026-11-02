import { describe, expect, it } from "vitest";
import type { Graph } from "../shared/types";
import { buildView, catalogTiles, defaultOptions } from "./views";

const g: Graph = {
  generated: "now",
  warnings: [],
  nodes: [
    { id: "machine:nas", type: "machine", label: "nas" },
    { id: "machine:idle", type: "machine", label: "idle" },
    { id: "stack:app", type: "stack", label: "app", server: "nas", urls: ["https://app.lab.example"], category: "Media" },
    { id: "stack:db", type: "stack", label: "db", server: "nas" },
    { id: "infra:komodo", type: "infra", label: "Komodo" },
    { id: "infra:infisical", type: "infra", label: "Infisical" },
  ],
  edges: [
    { id: "h1", source: "machine:nas", target: "stack:app", type: "hosts" },
    { id: "h2", source: "machine:nas", target: "stack:db", type: "hosts" },
    { id: "d1", source: "stack:app", target: "stack:db", type: "depends-on" },
    { id: "k1", source: "stack:app", target: "infra:komodo", type: "deployed-by" },
    { id: "s1", source: "stack:db", target: "infra:infisical", type: "uses-secrets" },
  ],
};

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

  it("catalog hides stacks without URL unless asked, grouped by category", () => {
    expect(catalogTiles(g, "category", false).map((t) => [t.node.id, t.group])).toEqual([["stack:app", "Media"]]);
    expect(catalogTiles(g, "machine", true)).toHaveLength(2);
  });
});
