import type { Sub } from "./views";

export const NODE_W = 200;
export const NODE_H = 46;

/** elkjs is large; load it when the first graph view opens. */
const elk = import("elkjs/lib/elk.bundled.js").then(({ default: ELK }) => new ELK());

/** Layered left-to-right layout; returns the top-left position of every node. */
export async function layout(sub: Sub): Promise<Record<string, { x: number; y: number }>> {
  const result = await (
    await elk
  ).layout({
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.layered.spacing.nodeNodeBetweenLayers": "90",
      "elk.spacing.nodeNode": "18",
    },
    children: sub.nodes.map((n) => ({ id: n.id, width: NODE_W, height: NODE_H })),
    edges: sub.edges.map((e) => ({ id: e.id, sources: [e.source], targets: [e.target] })),
  });
  return Object.fromEntries((result.children ?? []).map((c) => [c.id, { x: c.x ?? 0, y: c.y ?? 0 }]));
}
