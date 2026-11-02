import { XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Graph, StatusSnapshot } from "../shared/types";
import { neighbours } from "./views";

const HIDDEN = new Set(["id", "type", "label", "urls"]);

export function Detail({ graph, status, id, onSelect, onClose }: { graph: Graph; status: StatusSnapshot; id: string; onSelect: (id: string) => void; onClose: () => void }) {
  const node = graph.nodes.find((n) => n.id === id);
  if (!node) return null;
  const { incoming, outgoing } = neighbours(graph, id);
  const st = status.nodes[id];
  const label = (nid: string) => graph.nodes.find((n) => n.id === nid)?.label ?? nid;
  const groupOf = (edges: typeof incoming, end: "source" | "target") => {
    const by = new Map<string, string[]>();
    for (const e of edges) by.set(e.type, [...(by.get(e.type) ?? []), e[end]]);
    return [...by.entries()];
  };
  return (
    <aside className="detail">
      <Button variant="ghost" size="icon-sm" className="close" onClick={onClose} aria-label="Close"><XIcon /></Button>
      <h2>{node.label}</h2>
      <p className="muted">{node.type}{node.job ? " (one-shot job)" : ""} · <span className={`dot h-${st?.health ?? "unknown"}`} /> {st?.detail ?? st?.health ?? "unknown"}</p>
      {(node.urls ?? []).map((u) => <p key={u}><a href={u} target="_blank" rel="noreferrer">{u}</a></p>)}
      <dl>
        {Object.entries(node).filter(([k, v]) => !HIDDEN.has(k) && v != null && typeof v !== "object").map(([k, v]) => (
          <div key={k}><dt>{k}</dt><dd>{String(v)}</dd></div>
        ))}
        {node.images && node.images.length > 0 && <div><dt>images</dt><dd>{node.images.join(", ")}</dd></div>}
      </dl>
      {([["Outgoing", outgoing, "target"], ["Incoming", incoming, "source"]] as const).map(([title, edges, end]) => (
        edges.length > 0 && (
          <section key={title}>
            <h3>{title}</h3>
            {groupOf(edges, end).map(([type, ids]) => (
              <div key={type}>
                <small className="muted">{type}</small>
                <ul>{ids.map((n) => <li key={n}><button className="link" onClick={() => onSelect(n)}>{label(n)}</button></li>)}</ul>
              </div>
            ))}
          </section>
        )
      ))}
    </aside>
  );
}
