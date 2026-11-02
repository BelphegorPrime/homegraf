import {
  Background, BackgroundVariant, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useEffect, useMemo, useState } from "react";
import type { GraphNode, StatusSnapshot } from "../shared/types";
import { layout, NODE_H, NODE_W } from "./layout";
import type { Sub } from "./views";

interface CardData extends Record<string, unknown> {
  node: GraphNode;
  health: string;
  dimmed: boolean;
}

function GraphCard({ data, selected }: NodeProps<Node<CardData>>) {
  const { node, health, dimmed } = data;
  return (
    <div className={`gnode t-${node.type} ${selected ? "selected" : ""} ${dimmed ? "dimmed" : ""}`} style={{ width: NODE_W, height: NODE_H }}>
      <Handle type="target" position={Position.Left} />
      <span className={`dot h-${health}`} title={health} />
      <span className="gnode-text">
        <strong>{node.label}</strong>
        <small>{node.kind ?? node.type}{node.placeholder ? " · placeholder" : ""}{node.manual ? " · manual" : ""}{node.disabled ? " · disabled" : ""}</small>
      </span>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { card: GraphCard };
const isDisabled = (n: GraphNode) => n.disabled === true;

interface Props {
  sub: Sub;
  status: StatusSnapshot;
  query: string;
  selected: string | null;
  onSelect: (id: string | null) => void;
}

export function GraphView({ sub, status, query, selected, onSelect }: Props) {
  const [laid, setLaid] = useState<{ key: string; positions: Record<string, { x: number; y: number }> } | null>(null);
  const structure = useMemo(() => sub.nodes.map((n) => n.id).join("|") + "#" + sub.edges.map((e) => e.id).join("|"), [sub]);

  useEffect(() => {
    let live = true;
    layout(sub).then((positions) => live && setLaid({ key: structure, positions }));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [structure]);

  // fitView measures once on mount, so the graph is only mounted after the layout for this structure exists.
  const positions = laid?.key === structure ? laid.positions : null;
  const q = query.trim().toLowerCase();
  const nodes: Node<CardData>[] = sub.nodes.map((n) => ({
    id: n.id,
    type: "card",
    position: positions?.[n.id] ?? { x: 0, y: 0 },
    selected: n.id === selected,
    data: {
      node: n,
      health: status.nodes[n.id]?.health ?? "unknown",
      dimmed: isDisabled(n) || (q !== "" && !`${n.label} ${n.id} ${(n.urls ?? []).join(" ")}`.toLowerCase().includes(q)),
    },
  }));
  const edges: Edge[] = sub.edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    label: e.type === "link" ? e.label : e.type === "proxies" ? `${e.domains?.length ?? 0} domain(s)` : undefined,
    className: `e-${e.type}`,
    style: e.inferred ? { strokeDasharray: "5 4" } : undefined,
    markerEnd: { type: MarkerType.ArrowClosed },
  }));

  if (sub.nodes.length === 0) return <p className="empty">Nothing to show in this view yet.</p>;
  if (!positions) return <p className="empty">Laying out…</p>;
  return (
    <ReactFlow
      key={structure}
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      fitView
      minZoom={0.1}
      nodesConnectable={false}
      onNodeClick={(_, n) => onSelect(n.id)}
      onPaneClick={() => onSelect(null)}
      proOptions={{ hideAttribution: true }}
    >
      <Background variant={BackgroundVariant.Dots} gap={20} color="var(--border)" />
      <Controls showInteractive={false} />
      <MiniMap pannable zoomable maskColor="color-mix(in oklch, var(--background) 70%, transparent)" />
    </ReactFlow>
  );
}
