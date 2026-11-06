import { isIP } from "node:net";
import tls from "node:tls";
import type { CertificateInfo, CertificatesSnapshot } from "../shared/types.js";

const DAY = 86_400_000;

/** Reads the certificate `host` serves for `domain`. Verification is off on purpose: the point is to see what is there. */
export type Probe = (domain: string) => Promise<CertificateInfo>;

/** What node's TLS socket knows about the peer's certificate (the part used here). */
export interface PeerCertificate {
  subject?: { CN?: string | string[] };
  issuer?: { CN?: string | string[]; O?: string | string[] };
  subjectaltname?: string;
  valid_to?: string;
  fingerprint256?: string;
}

const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

/** Turns a peer certificate into what the page shows. */
export function describeCertificate(domain: string, c: PeerCertificate, now: number): CertificateInfo {
  const notAfter = c.valid_to ? new Date(c.valid_to) : undefined;
  const names = (c.subjectaltname ?? "")
    .split(",")
    .map((n) => n.trim())
    .filter((n) => n.startsWith("DNS:"))
    .map((n) => n.slice(4));
  const subjectCn = first(c.subject?.CN);
  if (!names.length && subjectCn) names.push(subjectCn);
  if (!notAfter || Number.isNaN(notAfter.getTime())) return { domain, error: "the certificate has no end date" };
  return {
    domain,
    issuer: first(c.issuer?.CN) ?? first(c.issuer?.O),
    notAfter: notAfter.toISOString(),
    daysLeft: Math.floor((notAfter.getTime() - now) / DAY),
    names,
    matches: tls.checkServerIdentity(domain, c as tls.PeerCertificate) === undefined,
    fingerprint: c.fingerprint256,
  };
}

export function tlsProbe({ port = 443, timeoutMs = 8000, now = Date.now }: { port?: number; timeoutMs?: number; now?: () => number } = {}): Probe {
  return (domain) =>
    new Promise((resolve) => {
      const socket = tls.connect({ host: domain, port, servername: isIP(domain) ? undefined : domain, rejectUnauthorized: false, timeout: timeoutMs }, () => {
        resolve(describeCertificate(domain, socket.getPeerCertificate(), now()));
        socket.end();
      });
      socket.on("error", (e: NodeJS.ErrnoException) => resolve({ domain, error: e.code ?? e.message }));
      socket.on("timeout", () => {
        resolve({ domain, error: "timeout" });
        socket.destroy();
      });
    });
}

export interface CertificatesDeps {
  domains: () => Promise<string[]>;
  probe?: Probe;
  ttlMs?: number;
  /** Connections at a time: the proxy answers a burst of dozens only partly. */
  concurrency?: number;
  now?: () => number;
}

/**
 * The certificates of all domains, probed on demand and kept for `ttlMs`; one run at a time, however many pages ask.
 * Sorted by what needs attention first: failures, then the soonest to expire.
 */
export function certificateSource({ domains, probe = tlsProbe(), ttlMs = 15 * 60_000, concurrency = 6, now = Date.now }: CertificatesDeps) {
  let snapshot: CertificatesSnapshot = { updated: null, certificates: [] };
  let at = 0;
  let running: Promise<CertificatesSnapshot> | undefined;

  async function run(): Promise<CertificatesSnapshot> {
    const list = [...new Set(await domains())].sort();
    const found: CertificateInfo[] = [];
    let next = 0;
    await Promise.all(
      Array.from({ length: Math.min(concurrency, list.length) }, async () => {
        while (next < list.length) {
          const domain = list[next++];
          found.push(await probe(domain).catch((e: unknown) => ({ domain, error: e instanceof Error ? e.message : String(e) })));
        }
      }),
    );
    const rank = (c: CertificateInfo) => (c.error ? -1e9 : !c.matches ? -1e8 : (c.daysLeft ?? 0));
    found.sort((a, b) => rank(a) - rank(b) || a.domain.localeCompare(b.domain));
    at = now();
    return (snapshot = { updated: new Date(at).toISOString(), certificates: found });
  }

  return {
    async get(): Promise<CertificatesSnapshot> {
      if (snapshot.updated && now() - at < ttlMs) return snapshot;
      running ??= run().finally(() => (running = undefined));
      return running;
    },
  };
}
export type CertificateSource = ReturnType<typeof certificateSource>;
