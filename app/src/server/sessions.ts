import { randomBytes } from "node:crypto";
import { PERMISSIONS, type Permission } from "../shared/types.js";
import type { Principal } from "./auth.js";
import { ADMIN, type UserStore } from "./users.js";

export const LOGIN_COOKIE = "hg_login";

interface Session {
  name: string;
  csrf: string;
  exp: number;
}

const MAX_ENTRIES = 1000;
const random = (bytes = 32) => randomBytes(bytes).toString("base64url");

/**
 * Cookie sessions for the name-and-password sign-in of the login page, kept in memory (a restart signs everybody out). What a session
 * may do is read from the user list on every request, so a changed or removed user applies at once. The sign-in through an identity
 * provider keeps its own sessions (src/server/oidc.ts); the two share nothing.
 */
export function createSessions({ users, admin = ADMIN, hours = 12, now = Date.now }: { users: UserStore; admin?: string; hours?: number; now?: () => number }) {
  const sessions = new Map<string, Session>();
  const trim = () => {
    for (const [k, v] of sessions) if (v.exp <= now()) sessions.delete(k);
    while (sessions.size > MAX_ENTRIES) sessions.delete(sessions.keys().next().value as string);
  };
  const all = Object.keys(PERMISSIONS) as Permission[];

  return {
    maxAgeSeconds: Math.round(hours * 3600),
    /** Opens a session for a principal the authenticator has just accepted. */
    open(principal: Principal): string {
      trim();
      const id = random();
      sessions.set(id, { name: principal.name, csrf: random(24), exp: now() + hours * 3_600_000 });
      return id;
    },
    principal(id: string | undefined): { principal: Principal; csrf: string } | undefined {
      const s = id ? sessions.get(id) : undefined;
      if (!s) return undefined;
      if (s.exp <= now()) {
        sessions.delete(id!);
        return undefined;
      }
      if (s.name === admin) return { principal: { name: admin, permissions: all, source: "builtin" }, csrf: s.csrf };
      const user = users.get(s.name);
      if (!user || user.sso) {
        sessions.delete(id!);
        return undefined;
      }
      return { principal: { name: user.name, permissions: user.permissions, source: "user" }, csrf: s.csrf };
    },
    close(id: string | undefined) {
      if (id) sessions.delete(id);
    },
    cookie: (id: string, secure: boolean) =>
      `${LOGIN_COOKIE}=${id}; Path=/; Max-Age=${Math.round(hours * 3600)}; HttpOnly; SameSite=Lax${secure ? "; Secure" : ""}`,
    clearCookie: (secure: boolean) => `${LOGIN_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax${secure ? "; Secure" : ""}`,
  };
}
export type Sessions = ReturnType<typeof createSessions>;
