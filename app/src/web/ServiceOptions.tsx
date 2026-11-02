import { PlusIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import type { NetworkRow, VolumeRow } from "../shared/types";

export interface EnvRow { key: string; value: string }

const KINDS: { kind: VolumeRow["kind"]; label: string; source: string }[] = [
  { kind: "data", label: "Data folder", source: "subfolder, empty = /srv/docker/<id>" },
  { kind: "bind", label: "Host path", source: "/mnt/tank/media" },
  { kind: "named", label: "Docker volume", source: "volume name" },
  { kind: "tmpfs", label: "Memory (tmpfs)", source: "" },
  { kind: "repo", label: "Repo file", source: "./config/app.ini" },
];

/** A row is sent to the tool once it says everything it needs; until then it is only in the form. */
export const volumeComplete = (v: VolumeRow) =>
  /^\/[\w./@+-]*$/.test(v.target) &&
  (v.kind === "data" || v.kind === "tmpfs" ||
    (v.kind === "bind" && /^\/[\w./@+-]+$/.test(v.source)) ||
    (v.kind === "named" && /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(v.source)) ||
    (v.kind === "repo" && /^\.{1,2}\/[\w./@+-]+$/.test(v.source)));
export const networkComplete = (n: NetworkRow) => /^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/.test(n.name) && n.name !== "default";
export const envComplete = (e: EnvRow) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(e.key);

export const envObject = (rows: EnvRow[]): Record<string, string> => Object.fromEntries(rows.filter(envComplete).map((e) => [e.key, e.value]));
export const envRows = (o: Record<string, string>): EnvRow[] => Object.entries(o).map(([key, value]) => ({ key, value }));

const rowKey = (v: VolumeRow) => JSON.stringify([v.kind, v.kind === "tmpfs" ? "" : v.source, v.target, v.kind === "tmpfs" ? false : v.readOnly, v.kind === "tmpfs" ? v.size : ""]);
/** The same set of volumes, whatever the order (the file lists tmpfs apart from the others). */
export const sameVolumes = (a: VolumeRow[], b: VolumeRow[]) => JSON.stringify(a.map(rowKey).sort()) === JSON.stringify(b.map(rowKey).sort());
export const sameNetworks = (a: NetworkRow[], b: NetworkRow[]) => JSON.stringify(a.map((n) => [n.name, n.external])) === JSON.stringify(b.map((n) => [n.name, n.external]));

interface Common { disabled?: boolean }

export function VolumesEditor({ rows, onChange, disabled }: Common & { rows: VolumeRow[]; onChange: (r: VolumeRow[]) => void }) {
  const set = (i: number, patch: Partial<VolumeRow>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div className="rows">
      {rows.map((v, i) => {
        const kind = KINDS.find((k) => k.kind === v.kind)!;
        const bad = v.target !== "" && !volumeComplete(v);
        return (
          <div className="row" key={i}>
            <NativeSelect aria-label="Kind of volume" value={v.kind} disabled={disabled} onChange={(e) => set(i, { kind: e.target.value as VolumeRow["kind"], source: "", size: "" })}>
              {KINDS.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}
            </NativeSelect>
            {v.kind === "tmpfs"
              ? <Input aria-label="Size" placeholder="size, e.g. 512m (optional)" value={v.size} disabled={disabled} onChange={(e) => set(i, { size: e.target.value })} />
              : <Input aria-label="Source" placeholder={kind.source} value={v.source} disabled={disabled} onChange={(e) => set(i, { source: e.target.value })} aria-invalid={bad} />}
            <Input aria-label="Path in the container" placeholder="/path/in/container" value={v.target} disabled={disabled} onChange={(e) => set(i, { target: e.target.value })} aria-invalid={bad} />
            <label className="check"><input type="checkbox" checked={v.readOnly && v.kind !== "tmpfs"} disabled={disabled || v.kind === "tmpfs"} onChange={(e) => set(i, { readOnly: e.target.checked })} /> read-only</label>
            <Button type="button" variant="ghost" size="icon-sm" aria-label="Remove volume" disabled={disabled} onClick={() => onChange(rows.filter((_, j) => j !== i))}><XIcon /></Button>
            {bad && <small className="error span">{v.kind === "repo" ? "a path like ./config" : v.kind === "bind" ? "an absolute host path" : v.kind === "named" ? "a volume name" : "an absolute path inside the container"}</small>}
          </div>
        );
      })}
      <div><Button type="button" variant="outline" size="xs" disabled={disabled} onClick={() => onChange([...rows, { kind: "data", source: "", target: "", readOnly: false, size: "" }])}><PlusIcon />Add volume</Button></div>
    </div>
  );
}

export function NetworksEditor({ rows, onChange, disabled, hint }: Common & { rows: NetworkRow[]; onChange: (r: NetworkRow[]) => void; hint?: string }) {
  const set = (i: number, patch: Partial<NetworkRow>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div className="rows">
      {rows.map((n, i) => (
        <div className="row net" key={i}>
          <Input aria-label="Network name" placeholder="network name" value={n.name} disabled={disabled} onChange={(e) => set(i, { name: e.target.value })} aria-invalid={n.name !== "" && !networkComplete(n)} />
          <label className="check"><input type="checkbox" checked={n.external} disabled={disabled} onChange={(e) => set(i, { external: e.target.checked })} /> existing Docker network</label>
          <Button type="button" variant="ghost" size="icon-sm" aria-label="Remove network" disabled={disabled} onClick={() => onChange(rows.filter((_, j) => j !== i))}><XIcon /></Button>
        </div>
      ))}
      <div><Button type="button" variant="outline" size="xs" disabled={disabled} onClick={() => onChange([...rows, { name: "", external: false }])}><PlusIcon />Add network</Button></div>
      {hint && <small className="muted">{hint}</small>}
    </div>
  );
}

export function EnvEditor({ rows, onChange, disabled }: Common & { rows: EnvRow[]; onChange: (r: EnvRow[]) => void }) {
  const set = (i: number, patch: Partial<EnvRow>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div className="rows">
      {rows.map((e, i) => (
        <div className="row env" key={i}>
          <Input aria-label="Variable name" placeholder="NAME" value={e.key} disabled={disabled} onChange={(ev) => set(i, { key: ev.target.value })} aria-invalid={e.key !== "" && !envComplete(e)} />
          <Input aria-label="Value" placeholder="value" value={e.value} disabled={disabled} onChange={(ev) => set(i, { value: ev.target.value })} />
          <Button type="button" variant="ghost" size="icon-sm" aria-label="Remove variable" disabled={disabled} onClick={() => onChange(rows.filter((_, j) => j !== i))}><XIcon /></Button>
        </div>
      ))}
      <div><Button type="button" variant="outline" size="xs" disabled={disabled} onClick={() => onChange([...rows, { key: "", value: "" }])}><PlusIcon />Add variable</Button></div>
    </div>
  );
}
