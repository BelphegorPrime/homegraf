import { createHash, timingSafeEqual } from "node:crypto";
import { PERMISSIONS, type Permission } from "../shared/types.js";
import { ADMIN, DUMMY_HASH, verifyPassword, type UserStore } from "./users.js";

/** Who is acting. They come from a password (one request at a time) or from a session of the identity provider (src/server/oidc.ts). */
export interface Principal {
  name: string;
  permissions: Permission[];
  source: "builtin" | "user" | "sso";
}

export type Denied = "missing" | "wrong" | "locked" | "disabled";
export type AuthResult = { ok: true; principal: Principal } | { ok: false; reason: Denied };

/** Reads the credentials of a request: `Authorization: Basic base64(name:password)`. A `Bearer` token for OAuth goes here later. */
export type Authenticator = (authorization: string | undefined) => Promise<AuthResult>;

const digest = (s: string) => createHash("sha256").update(s).digest();

export function basicCredentials(header: string | undefined): { name: string; password: string } | undefined {
  const m = /^Basic ([A-Za-z0-9+/=]+)$/.exec(header ?? "");
  if (!m) return undefined;
  const decoded = Buffer.from(m[1], "base64").toString("utf8");
  const i = decoded.indexOf(":");
  return i < 1 ? undefined : { name: decoded.slice(0, i), password: decoded.slice(i + 1) };
}

export interface AuthOptions {
  /** The administrator's password (HOMEGRAF_PASSWORD); without it nobody can sign in. */
  adminPassword: string | undefined;
  /** The built-in administrator's name (HOMEGRAF_ADMIN), lower case. */
  admin?: string;
  users: UserStore;
  now?: () => number;
  maxFailures?: number;
  windowMs?: number;
}

/**
 * Checks name and password. Wrong attempts are counted per name (a proxy hides the client's address): after
 * `maxFailures` within `windowMs` that name is refused, even with the right password, until the oldest failure aged out.
 * An unknown name costs as much time as a wrong password and is counted like one.
 */
export function authenticator({ adminPassword, admin = ADMIN, users, now = Date.now, maxFailures = 5, windowMs = 10 * 60_000 }: AuthOptions): Authenticator {
  const failures = new Map<string, number[]>();
  const recent = (name: string) => (failures.get(name) ?? []).filter((t) => now() - t < windowMs);

  return async (authorization) => {
    if (!adminPassword) return { ok: false, reason: "disabled" };
    const given = basicCredentials(authorization);
    if (!given) return { ok: false, reason: "missing" };
    const key = given.name.toLowerCase();
    const attempts = recent(key);
    if (attempts.length >= maxFailures) return { ok: false, reason: "locked" };

    let principal: Principal | undefined;
    if (key === admin) {
      if (timingSafeEqual(digest(given.password), digest(adminPassword)))
        principal = { name: admin, permissions: Object.keys(PERMISSIONS) as Permission[], source: "builtin" };
    } else {
      const user = users.get(key);
      if ((await verifyPassword(given.password, user?.hash || (await DUMMY_HASH))) && user && !user.sso)
        principal = { name: user.name, permissions: user.permissions, source: "user" };
    }
    if (principal) {
      failures.delete(key);
      return { ok: true, principal };
    }
    failures.set(key, [...attempts, now()]);
    // Names are typed by whoever asks; do not let unknown ones grow the table without bound.
    if (failures.size > 1000) failures.delete(failures.keys().next().value as string);
    return { ok: false, reason: "wrong" };
  };
}
