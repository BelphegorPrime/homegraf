import { X509Certificate } from "node:crypto";
import { readFileSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";
import type { RootCaInfo } from "../shared/types.js";

const PEM = /-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----/g;

/** The file of the CA certificates (no key): `ca_bundle` of the first issuer in inventory/ingress.yaml, always inside the repository. */
export function caBundlePath(repoDir: string): string | undefined {
  let text: string;
  try {
    text = readFileSync(`${repoDir}/inventory/ingress.yaml`, "utf8");
  } catch {
    return undefined;
  }
  const name = /^\s+ca_bundle:\s*["']?([^\s"'#]+)/m.exec(text)?.[1];
  if (!name) return undefined;
  const path = resolve(repoDir, name);
  const inside = relative(resolve(repoDir), path);
  return inside && !inside.startsWith("..") && !isAbsolute(inside) ? path : undefined;
}

const name = (dn: string) => /(?:^|\n)CN=([^\n]+)/.exec(dn)?.[1] ?? dn.replaceAll("\n", ", ");

/** What a person needs to know before trusting it: whose certificate it is, how long it lasts, its fingerprint. */
export function describeBundle(pem: string): RootCaInfo["certificates"] {
  return (pem.match(PEM) ?? []).map((one) => {
    const c = new X509Certificate(one);
    return { subject: name(c.subject), issuer: name(c.issuer), notAfter: new Date(c.validTo).toISOString(), fingerprint: c.fingerprint256, root: c.subject === c.issuer };
  });
}

/** The homelab root certificate for download. Public by nature (clients need it before they can trust anything); null while there is none. */
export function rootCaSource(repoDir: string, read: (path: string) => string = (p) => readFileSync(p, "utf8")) {
  return (): { pem: string; info: RootCaInfo } | null => {
    const path = caBundlePath(repoDir);
    if (!path) return null;
    try {
      const pem = read(path);
      const certificates = describeBundle(pem);
      return certificates.length ? { pem, info: { certificates } } : null;
    } catch {
      return null;
    }
  };
}
export type RootCaSource = ReturnType<typeof rootCaSource>;
