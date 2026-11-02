import { Button } from "@/components/ui/button";
import { useRootCa } from "./api";

const date = (iso: string) => new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });

const INSTALL: [string, string][] = [
  ["Windows", "Open the file, Install Certificate, Local Machine, then place it in “Trusted Root Certification Authorities”."],
  ["macOS", "Open the file in Keychain Access (System keychain), open the certificate and set “Always Trust”."],
  ["Linux", "Save it as a .crt file in /usr/local/share/ca-certificates/ and run sudo update-ca-certificates."],
  ["iOS", "Open the file, install the profile in Settings, then switch it on under General, About, Certificate Trust Settings."],
  ["Android", "Settings, Security, Encryption & credentials, Install a certificate, CA certificate."],
  ["Firefox", "Has its own store: Settings, Privacy & Security, View Certificates, Authorities, Import (or let it use the system's)."],
];

/** The homelab root certificate for download: every client has to trust it once before the homelab's HTTPS pages load without a warning. */
export function RootCa() {
  const q = useRootCa();
  if (q.isPending) return null;
  if (q.isError) return <p className="muted">Root certificate: {q.error.message}.</p>;
  return (
    <section>
      <h2>Root certificate</h2>
      <p className="muted max-w-3xl">
        Certificates of the homelab CA are only trusted by devices that have its certificate. Install it once on each device and the pages open without a warning;
        it holds no key.
      </p>
      <div className="actions">
        <Button nativeButton={false} render={<a href="/api/ca.pem" download="homelab-root-ca.pem" />}>Download the root certificate</Button>
      </div>
      <table className="mt-3 text-left text-sm">
        <thead className="muted"><tr><th className="pr-4 font-normal">Certificate</th><th className="pr-4 font-normal">Ends</th><th className="font-normal">SHA-256 fingerprint</th></tr></thead>
        <tbody>
          {q.data.certificates.map((c) => (
            <tr key={c.fingerprint} className="align-top">
              <td className="pr-4">{c.subject}{c.root ? " (root)" : ` (issued by ${c.issuer})`}</td>
              <td className="pr-4">{date(c.notAfter)}</td>
              <td><code className="break-all text-xs">{c.fingerprint}</code></td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted max-w-3xl">Compare the fingerprint with the one your device shows before you trust it.</p>
      <details className="mt-2 max-w-3xl">
        <summary className="cursor-pointer">How to install it</summary>
        <ul className="mt-2 list-disc pl-5 text-sm">
          {INSTALL.map(([os, how]) => <li key={os}><strong>{os}:</strong> {how}</li>)}
        </ul>
      </details>
    </section>
  );
}
