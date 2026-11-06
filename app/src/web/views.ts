import type { EdgeType, Graph, GraphEdge, GraphNode, NodeType } from "../shared/types";

export type ViewId = "catalog" | "hosting" | "network" | "request" | "dependencies" | "platform" | "status" | "certificates" | "new" | "remove" | "stacks" | "users";

export interface ViewDef {
  id: ViewId;
  label: string;
  hint: string;
  /** Graph views are drawn with React Flow; the others are lists or a form. */
  kind: "graph" | "list" | "form";
  /** The sidebar icon: the name of a lucide icon (see ICONS in App.tsx). */
  icon: string;
  /** The sidebar group it is listed under. */
  group: ViewGroup;
}

/** The sidebar's groups, in order: what is there, how it hangs together, what you can change. */
export type ViewGroup = "Overview" | "Maps" | "Manage";
export const VIEW_GROUPS: ViewGroup[] = ["Overview", "Maps", "Manage"];

export const VIEWS: ViewDef[] = [
  { id: "catalog", label: "Catalog", hint: "Every service with its links", kind: "list", icon: "LayoutGrid", group: "Overview" },
  { id: "hosting", label: "Hosting", hint: "Which machine runs which stack", kind: "graph", icon: "Server", group: "Maps" },
  { id: "network", label: "Network", hint: "ISP, router, switches and machines", kind: "graph", icon: "Network", group: "Maps" },
  { id: "request", label: "Request path", hint: "Domain, proxy and the container that answers", kind: "graph", icon: "Route", group: "Maps" },
  { id: "dependencies", label: "Dependencies", hint: "What depends on what", kind: "graph", icon: "Workflow", group: "Maps" },
  { id: "platform", label: "Platform", hint: "Komodo, Infisical, proxy and CA and who uses them", kind: "graph", icon: "Layers", group: "Maps" },
  { id: "status", label: "Status", hint: "Everything that is not healthy", kind: "list", icon: "Activity", group: "Overview" },
  { id: "certificates", label: "Certificates", hint: "When each domain's certificate ends", kind: "list", icon: "ShieldCheck", group: "Overview" },
  { id: "new", label: "New app", hint: "Plan a new app: compose, Komodo stack, domain and secrets", kind: "form", icon: "PlusCircle", group: "Manage" },
  { id: "remove", label: "Remove app", hint: "Destroy an app's stack, remove its files, routing and secrets", kind: "form", icon: "Trash2", group: "Manage" },
  { id: "stacks", label: "Stacks", hint: "Redeploy a stack and manage its secrets", kind: "form", icon: "Boxes", group: "Manage" },
  { id: "users", label: "Users", hint: "Who may create apps and manage users", kind: "form", icon: "Users", group: "Manage" },
];

export interface Sub {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface ViewOptions {
  containers: boolean;       // hosting and dependencies: include container nodes
  focus: string;             // platform: infra node whose dependents are shown ("" = none)
}

export const defaultOptions: ViewOptions = { containers: false, focus: "infra:komodo" };

function subgraph(g: Graph, nodeTypes: NodeType[], edgeTypes: EdgeType[], keep: (n: GraphNode) => boolean = () => true,
                  alwaysShow: (n: GraphNode) => boolean = () => false): Sub {
  const types = new Set(nodeTypes);
  const allowed = new Map(g.nodes.filter((n) => types.has(n.type) && keep(n)).map((n) => [n.id, n]));
  const edges = g.edges.filter((e) => edgeTypes.includes(e.type) && allowed.has(e.source) && allowed.has(e.target));
  const used = new Set(edges.flatMap((e) => [e.source, e.target]));
  return { nodes: [...allowed.values()].filter((n) => used.has(n.id) || alwaysShow(n)), edges };
}

export function buildView(id: ViewId, g: Graph, opts: ViewOptions = defaultOptions): Sub {
  switch (id) {
    case "hosting":
      return opts.containers
        ? subgraph(g, ["machine", "stack", "container", "service"], ["hosts", "contains"], (n) => !n.external, (n) => n.type === "machine")
        : subgraph(g, ["machine", "stack", "service"], ["hosts"], (n) => !n.external, (n) => n.type === "machine");
    case "network":
      return subgraph(g, ["isp", "router", "switch", "device", "machine"], ["link"], (n) => !n.external);
    case "request":
      return subgraph(g, ["domain", "infra", "container", "stack", "service"], ["terminated-by", "proxies"]);
    case "dependencies":
      return subgraph(g, opts.containers ? ["stack", "container"] : ["stack"], ["depends-on"]);
    case "platform": {
      const edgeTypes: EdgeType[] = ["runs", "certificates-from", "deployed-by", "uses-secrets"];
      const infra = g.nodes.filter((n) => n.type === "infra");
      const dependents = new Set(
        g.edges.filter((e) => opts.focus && e.target === opts.focus && ["deployed-by", "uses-secrets"].includes(e.type)).map((e) => e.source),
      );
      const keepStack = (n: GraphNode) => n.type !== "stack" || dependents.has(n.id) || g.edges.some((e) => e.type === "runs" && e.target === n.id);
      const sub = subgraph(g, ["infra", "stack"], edgeTypes, keepStack, (n) => infra.includes(n));
      // Edges of the other infra services (e.g. every stack -> Komodo) would bury the picture; only the focused one is drawn.
      const edges = sub.edges.filter((e) => !["deployed-by", "uses-secrets"].includes(e.type) || e.target === opts.focus);
      const used = new Set(edges.flatMap((e) => [e.source, e.target]));
      return { nodes: sub.nodes.filter((n) => n.type === "infra" || used.has(n.id)), edges };
    }
    default:
      return { nodes: [], edges: [] };
  }
}

/** Direct neighbours of a node in the full graph, for the detail panel. */
export function neighbours(g: Graph, id: string): { incoming: GraphEdge[]; outgoing: GraphEdge[] } {
  return { incoming: g.edges.filter((e) => e.target === id), outgoing: g.edges.filter((e) => e.source === id) };
}

export interface Tile {
  node: GraphNode;
  group: string;
}

/** Catalog tiles: stacks and appliances that have a URL (or all of them), grouped by machine or category. */
export function catalogTiles(g: Graph, groupBy: "machine" | "category", withoutUrl: boolean): Tile[] {
  return g.nodes
    .filter((n) => (n.type === "stack" || n.type === "service") && (withoutUrl || (n.urls ?? []).length > 0))
    .map((node) => ({
      node,
      group: groupBy === "machine" ? (node.server ?? "unassigned") : (node.category ?? "Other"),
    }))
    .sort((a, b) => a.group.localeCompare(b.group) || a.node.label.localeCompare(b.node.label));
}
