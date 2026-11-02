import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { createServer } from "node:tls";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { certificateSource, describeCertificate, tlsProbe } from "./certificates.js";

const NOW = Date.parse("2026-10-30T12:00:00Z");
const peer = (over: object = {}) => ({
  subject: { CN: "a.lab.example" }, issuer: { CN: "Example CA" }, subjectaltname: "DNS:a.lab.example, DNS:b.lab.example",
  valid_to: "Jan 22 16:42:57 2027 GMT", fingerprint256: "AA:BB", ...over,
});

describe("describeCertificate", () => {
  it("reads issuer, end date, days left and names", () => {
    expect(describeCertificate("a.lab.example", peer(), NOW)).toEqual({
      domain: "a.lab.example", issuer: "Example CA", notAfter: "2027-01-22T16:42:57.000Z", daysLeft: 84,
      names: ["a.lab.example", "b.lab.example"], matches: true, fingerprint: "AA:BB",
    });
  });

  it("counts negative days once expired (exactly two days ago is -2)", () => {
    expect(describeCertificate("a.lab.example", peer({ valid_to: "Oct 28 12:00:00 2026 GMT" }), NOW).daysLeft).toBe(-2);
  });

  it("notices a certificate for other names, and takes wildcards", () => {
    expect(describeCertificate("c.lab.example", peer(), NOW).matches).toBe(false);
    expect(describeCertificate("c.lab.example", peer({ subjectaltname: "DNS:*.lab.example" }), NOW).matches).toBe(true);
  });

  it("falls back to the common name and reports a missing end date", () => {
    expect(describeCertificate("a.lab.example", peer({ subjectaltname: undefined }), NOW).names).toEqual(["a.lab.example"]);
    expect(describeCertificate("a.lab.example", peer({ valid_to: undefined }), NOW)).toEqual({ domain: "a.lab.example", error: "the certificate has no end date" });
  });
});

describe("certificateSource", () => {
  const info = (domain: string, daysLeft: number, matches = true) => ({ domain, daysLeft, matches, issuer: "x", names: [domain], notAfter: "2027-01-01T00:00:00.000Z" });

  it("lists failures first, then the soonest to expire, and de-duplicates the domains", async () => {
    const source = certificateSource({
      domains: async () => ["c.x", "a.x", "b.x", "d.x", "e.x", "a.x"],
      probe: async (d) => ({ c: info(d, 80), a: info(d, 10), b: info(d, 5, false), d: { domain: d, error: "ECONNREFUSED" }, e: info(d, 10) })[d[0] as "a"],
      now: () => NOW,
    });
    const snap = await source.get();
    expect(snap.updated).toBe(new Date(NOW).toISOString());
    expect(snap.certificates.map((c) => c.domain)).toEqual(["d.x", "b.x", "a.x", "e.x", "c.x"]);
  });

  it("keeps the result for the ttl and runs one probe round at a time", async () => {
    let now = NOW; let rounds = 0;
    const source = certificateSource({ domains: async () => { rounds++; return ["a.x"]; }, probe: async (d) => info(d, 1), ttlMs: 1000, now: () => now });
    await Promise.all([source.get(), source.get(), source.get()]);
    expect(rounds).toBe(1);
    now += 999; await source.get();
    expect(rounds).toBe(1);
    now += 2; await source.get();
    expect(rounds).toBe(2);
  });

  it("limits the connections at a time and survives a probe that throws", async () => {
    let active = 0; let most = 0;
    const source = certificateSource({
      domains: async () => Array.from({ length: 20 }, (_, i) => `d${i}.x`), concurrency: 4,
      probe: async (d) => {
        active++; most = Math.max(most, active);
        await new Promise((r) => setTimeout(r, 2));
        active--;
        if (d === "d3.x") throw new Error("boom");
        return info(d, 30);
      },
    });
    const snap = await source.get();
    expect(most).toBe(4);
    expect(snap.certificates).toHaveLength(20);
    expect(snap.certificates[0]).toEqual({ domain: "d3.x", error: "boom" });
  });
});

describe("tlsProbe", () => {
  const openssl = (() => { try { execFileSync("openssl", ["version"]); return true; } catch { return false; } })();

  it.skipIf(!openssl)("reads the certificate a real TLS server presents, and reports a closed port", async () => {
    const dir = mkdtempSync(join(tmpdir(), "homegraf-tls-"));
    execFileSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", join(dir, "k.pem"), "-out", join(dir, "c.pem"),
      "-days", "10", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"], { stdio: "ignore" });
    const server = createServer({ key: readFileSync(join(dir, "k.pem")), cert: readFileSync(join(dir, "c.pem")) }, (s) => s.end());
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    const port = (server.address() as { port: number }).port;
    try {
      const found = await tlsProbe({ port })("localhost");
      expect(found).toMatchObject({ domain: "localhost", issuer: "localhost", names: ["localhost"], matches: true });
      expect(found.daysLeft).toBeGreaterThanOrEqual(9);
      expect((await tlsProbe({ port })("127.0.0.1")).matches).toBe(false); // SAN is the name, not the address
    } finally {
      server.close();
    }
    expect(await tlsProbe({ port })("localhost")).toEqual({ domain: "localhost", error: "ECONNREFUSED" });
  });
});
