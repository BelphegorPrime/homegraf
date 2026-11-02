import { Badge } from "@/components/ui/badge";
import type { CertificatesSnapshot } from "../shared/types";
import { useCertificates } from "./api";
import { certHealth } from "./certificateHealth";
import { HostOptions } from "./HostOptions";
import { RootCa } from "./RootCa";

const date = (iso?: string) => (iso ? new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "");

/** The certificate each domain serves right now, read over TLS: what a browser would see. Worst first. */
export function Certificates({ onSelect }: { onSelect: (id: string) => void }) {
  const q = useCertificates();
  return (
    <div className="list">
      <RootCa />
      {q.isPending ? <p className="empty">Reading the certificates of all domains…</p> : q.isError ? <p className="warning">{q.error.message}</p> : <Domains snapshot={q.data} onSelect={onSelect} />}
      <HostOptions />
    </div>
  );
}

function Domains({ snapshot, onSelect }: { snapshot: CertificatesSnapshot; onSelect: (id: string) => void }) {
  const rows = snapshot.certificates.map((c) => ({ c, ...certHealth(c) }));
  const count = (h: string) => rows.filter((r) => r.health === h).length;
  return (
    <section>
      <h2>Domains</h2>
      <p className="muted">
        {rows.length} domains: {count("ok")} fine, {count("warn")} to renew, {count("down")} broken.
        Read over TLS from each domain at {snapshot.updated ? new Date(snapshot.updated).toLocaleTimeString() : "…"}, so this is what a browser gets;
        certificates from the homelab CA renew 30 days before the end.
      </p>
      <table>
        <thead><tr><th /><th>Domain</th><th>Ends</th><th>State</th><th>Issued by</th><th>Covers</th></tr></thead>
        <tbody>
          {rows.map(({ c, health, why }) => (
            <tr key={c.domain} onClick={() => onSelect(`domain:${c.domain}`)}>
              <td><span className={`dot h-${health}`} /></td>
              <td>{c.domain}</td>
              <td>{date(c.notAfter)}</td>
              <td>{why}</td>
              <td>{c.issuer ?? ""}</td>
              <td>{c.names && c.names.length > 1 ? <Badge variant="outline" title={c.names.join("\n")}>{c.names.length} names</Badge> : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
