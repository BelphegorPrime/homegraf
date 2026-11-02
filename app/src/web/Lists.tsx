import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/native-select";
import type { Graph, GraphNode, StatusSnapshot } from "../shared/types";
import { catalogTiles } from "./views";

const match = (n: GraphNode, q: string) =>
  q === "" || `${n.label} ${n.id} ${(n.urls ?? []).join(" ")} ${(n.images ?? []).join(" ")}`.toLowerCase().includes(q.toLowerCase());

export function Catalog({ graph, status, query, selected, onSelect }: { graph: Graph; status: StatusSnapshot; query: string; selected: string | null; onSelect: (id: string) => void }) {
  const [groupBy, setGroupBy] = useState<"machine" | "category">("machine");
  const [withoutUrl, setWithoutUrl] = useState(false);
  const groups = useMemo(() => {
    const out = new Map<string, GraphNode[]>();
    for (const t of catalogTiles(graph, groupBy, withoutUrl)) {
      if (!match(t.node, query)) continue;
      out.set(t.group, [...(out.get(t.group) ?? []), t.node]);
    }
    return [...out.entries()];
  }, [graph, groupBy, withoutUrl, query]);

  return (
    <div className="list">
      <div className="toolbar">
        <label className="check">Group by
          <NativeSelect size="sm" value={groupBy} onChange={(e) => setGroupBy(e.target.value as "machine" | "category")}>
            <option value="machine">machine</option>
            <option value="category">category</option>
          </NativeSelect>
        </label>
        <span className="legend"><span className="url-pill">managed</span> <span className="url-pill manual">manual NPM host</span></span>
        <label className="check"><input type="checkbox" checked={withoutUrl} onChange={(e) => setWithoutUrl(e.target.checked)} /> include stacks without URL</label>
      </div>
      {groups.map(([group, nodes]) => (
        <section key={group}>
          <h2>{group}</h2>
          <div className="tiles">
            {nodes.map((n) => {
              const st = status.nodes[n.id];
              return (
                <Card key={n.id} className={`tile gap-2 py-3 ${n.id === selected ? "selected" : ""}`}>
                 <div className="flex flex-col gap-2 px-3">
                  <div className="tile-head">
                    <span className={`dot h-${st?.health ?? "unknown"}`} title={st?.detail ?? st?.health ?? "unknown"} />
                    <strong>{n.label}</strong>
                    {st?.updateAvailable && <Badge variant="outline" className="border-warn text-warn">update</Badge>}
                    <Button variant="outline" size="xs" onClick={() => onSelect(n.id)} aria-label={`Details of ${n.label}`}>Details</Button>
                  </div>
                  {(n.urls ?? []).length > 0 && (
                    <div className="url-pills">
                      {(n.urls ?? []).map((u) => (
                        <a key={u} className={`url-pill ${n.manualUrls?.includes(u) ? "manual" : ""}`} href={u} target="_blank" rel="noreferrer"
                           title={n.manualUrls?.includes(u) ? `${u} (manual NPM host)` : u}>{u.replace("https://", "")}</a>
                      ))}
                    </div>
                  )}
                 </div>
                </Card>
              );
            })}
          </div>
        </section>
      ))}
      {groups.length === 0 && <p className="empty">No matches.</p>}
    </div>
  );
}

export function StatusList({ graph, status, onSelect }: { graph: Graph; status: StatusSnapshot; onSelect: (id: string) => void }) {
  if (status.source === "disabled") {
    return <p className="empty">Live status is off: set KOMODO_URL, KOMODO_API_KEY and KOMODO_API_SECRET for the container.</p>;
  }
  const rows = graph.nodes
    .filter((n) => ["machine", "stack", "infra", "router", "switch", "device"].includes(n.type))
    .map((n) => ({ n, st: status.nodes[n.id] }))
    .filter(({ st }) => st && (st.health !== "ok" || st.updateAvailable));
  return (
    <div className="list">
      {status.error && <p className="warning">Komodo unreachable ({status.error}); showing the last known states.</p>}
      {rows.length === 0 ? <p className="empty">Everything is running.</p> : (
        <table>
          <thead><tr><th /><th>Name</th><th>Type</th><th>State</th></tr></thead>
          <tbody>
            {rows.map(({ n, st }) => (
              <tr key={n.id} onClick={() => onSelect(n.id)}>
                <td><span className={`dot h-${st.health}`} /></td>
                <td>{n.label}</td>
                <td>{n.type}</td>
                <td>{st.detail ?? st.health}{st.updateAvailable ? " · image update available" : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
