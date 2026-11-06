import { createHash, randomBytes } from "node:crypto";
import { PERMISSIONS, type Permission } from "../shared/types.js";
import type { OidcConfig } from "./config.js";
import type { Principal } from "./auth.js";
import { ADMIN, NAME_RE, type UserStore } from "./users.js";

export const SESSION_COOKIE = "hg_session";
export const STATE_COOKIE = "hg_oidc";

interface Discovery {
  issuer: string;
  authorization_endpoint: string;
  token_endpoint: string;
  userinfo_endpoint?: string;
}
interface Pending {
  verifier: string;
  nonce: string;
  exp: number;
}
interface Session {
  name: string;
  csrf: string;
  exp: number;
}

export interface OidcDeps {
  cfg: OidcConfig;
  users: UserStore;
  /** The built-in administrator's name: reserved, no account of the provider may take it. */
  admin?: string;
  fetchFn?: typeof fetch;
  now?: () => number;
}

const b64url = (b: Buffer) => b.toString("base64url");
const random = (bytes = 32) => b64url(randomBytes(bytes));
const TEN_MINUTES = 10 * 60_000;
const MAX_ENTRIES = 1000;

/** The value of one cookie in a `Cookie` header. */
export function cookieValue(header: string | undefined, name: string): string | undefined {
  for (const part of (header ?? "").split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return v.join("=");
  }
  return undefined;
}

/** What happened to a sign-in: a session, or a message fit for the page. */
export type SignIn = { ok: true; sessionId: string; maxAgeSeconds: number } | { ok: false; error: string };

/**
 * Signing in through an OpenID Connect provider: authorization code with PKCE, then a cookie session kept in memory (a restart
 * signs everybody out). The id token comes straight from the provider's token endpoint over TLS, which the specification accepts
 * in place of checking its signature; issuer, audience, expiry and nonce are checked. Who may do what is decided here, by name,
 * in the Users tab: a name the provider confirms but the list does not know may look and nothing else.
 */
export function createOidc({ cfg, users, admin = ADMIN, fetchFn = fetch, now = Date.now }: OidcDeps) {
  const pending = new Map<string, Pending>();
  const sessions = new Map<string, Session>();
  let discovered: { at: number; value: Discovery } | undefined;
  const redirectUri = `${cfg.publicUrl}/api/auth/callback`;
  const secure = cfg.publicUrl.startsWith("https:");

  async function discovery(): Promise<Discovery> {
    if (discovered && now() - discovered.at < 3_600_000) return discovered.value;
    const res = await fetchFn(`${cfg.issuer}/.well-known/openid-configuration`, { signal: AbortSignal.timeout(8000) });
    if (!res.ok) throw new Error(`the provider's configuration at ${cfg.issuer} answered HTTP ${res.status}`);
    const value = (await res.json()) as Discovery;
    if (value.issuer?.replace(/\/$/, "") !== cfg.issuer) throw new Error(`the provider says it is ${value.issuer}, not ${cfg.issuer}`);
    if (!value.authorization_endpoint || !value.token_endpoint) throw new Error("the provider's configuration has no authorization or token endpoint");
    discovered = { at: now(), value };
    return value;
  }

  const trim = <V extends { exp: number }>(map: Map<string, V>) => {
    for (const [k, v] of map) if (v.exp <= now()) map.delete(k);
    while (map.size > MAX_ENTRIES) map.delete(map.keys().next().value as string);
  };

  const cookie = (name: string, value: string, maxAge: number, path = "/") =>
    `${name}=${value}; Path=${path}; Max-Age=${maxAge}; HttpOnly; SameSite=Lax${secure ? "; Secure" : ""}`;

  return {
    cfg,
    /** The address to send the browser to, and the cookie that ties the answer to this browser. */
    async login(): Promise<{ location: string; stateCookie: string }> {
      const meta = await discovery();
      trim(pending);
      const state = random();
      const verifier = random(48);
      const nonce = random(16);
      pending.set(state, { verifier, nonce, exp: now() + TEN_MINUTES });
      const url = new URL(meta.authorization_endpoint);
      for (const [k, v] of Object.entries({
        response_type: "code",
        client_id: cfg.clientId,
        redirect_uri: redirectUri,
        scope: cfg.scopes,
        state,
        nonce,
        code_challenge: b64url(createHash("sha256").update(verifier).digest()),
        code_challenge_method: "S256",
      }))
        url.searchParams.set(k, v);
      return { location: url.toString(), stateCookie: cookie(STATE_COOKIE, state, 600, "/api/auth") };
    },

    /** The provider's answer: trades the code for the id token, checks it and opens the session. */
    async callback(q: { code?: string; state?: string; error?: string }, cookieState: string | undefined): Promise<SignIn> {
      if (q.error) return { ok: false, error: `the provider refused the sign-in (${q.error})` };
      const wait = q.state ? pending.get(q.state) : undefined;
      if (q.state) pending.delete(q.state); // one try per start
      if (!q.code || !q.state || !wait || wait.exp <= now() || cookieState !== q.state) {
        return { ok: false, error: "the sign-in did not start in this browser, or took too long: start it again" };
      }
      try {
        const meta = await discovery();
        const res = await fetchFn(meta.token_endpoint, {
          method: "POST",
          headers: { "content-type": "application/x-www-form-urlencoded", accept: "application/json" },
          body: new URLSearchParams({
            grant_type: "authorization_code",
            code: q.code,
            redirect_uri: redirectUri,
            client_id: cfg.clientId,
            client_secret: cfg.clientSecret,
            code_verifier: wait.verifier,
          }),
          signal: AbortSignal.timeout(8000),
        });
        if (!res.ok) return { ok: false, error: `the provider's token endpoint answered HTTP ${res.status}` };
        const token = (await res.json()) as { id_token?: string; access_token?: string };
        if (!token.id_token) return { ok: false, error: "the provider sent no id token (is the scope openid on?)" };
        const claims = JSON.parse(Buffer.from(token.id_token.split(".")[1] ?? "", "base64url").toString("utf8")) as Record<string, unknown>;
        const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
        if (typeof claims.iss !== "string" || claims.iss.replace(/\/$/, "") !== cfg.issuer) return { ok: false, error: "the id token is from another issuer" };
        if (!aud.includes(cfg.clientId)) return { ok: false, error: "the id token is for another client" };
        if (typeof claims.exp !== "number" || claims.exp * 1000 < now() - 60_000) return { ok: false, error: "the id token has expired" };
        if (claims.nonce !== wait.nonce) return { ok: false, error: "the id token does not belong to this sign-in" };
        if (typeof claims.sub !== "string" || !claims.sub) return { ok: false, error: "the id token has no subject" };

        let raw = claims[cfg.usernameClaim];
        if (typeof raw !== "string" && token.access_token && meta.userinfo_endpoint) {
          const info = await fetchFn(meta.userinfo_endpoint, { headers: { authorization: `Bearer ${token.access_token}` }, signal: AbortSignal.timeout(8000) });
          if (info.ok) raw = ((await info.json()) as Record<string, unknown>)[cfg.usernameClaim];
        }
        if (typeof raw !== "string" || !raw) return { ok: false, error: `the provider's answer has no "${cfg.usernameClaim}"` };
        const name = raw.toLowerCase();
        if (name === admin) return { ok: false, error: `"${admin}" is reserved for the built-in administrator` };
        if (!NAME_RE.test(name)) return { ok: false, error: `"${raw}" is not a name this app can use (2 to 32 characters: letters, digits, . _ -)` };

        let known = users.get(name);
        if (!known && cfg.admins?.includes(name)) known = await users.addSso(name, Object.keys(PERMISSIONS) as Permission[]);
        if (known?.sso) {
          if (known.sub && known.sub !== claims.sub)
            return { ok: false, error: `${name} is bound to another account at the provider: remove the entry in the Users tab and add it again` };
          if (!known.sub) {
            try {
              users.bind(name, claims.sub);
            } catch {
              /* the users file is unreadable: the sign-in still works, unbound */
            }
          }
        }
        trim(sessions);
        const id = random();
        sessions.set(id, { name, csrf: random(24), exp: now() + cfg.sessionHours * 3_600_000 });
        return { ok: true, sessionId: id, maxAgeSeconds: Math.round(cfg.sessionHours * 3600) };
      } catch (err) {
        return { ok: false, error: err instanceof Error ? err.message : String(err) };
      }
    },

    /** Who the session is, with what they may do right now (read from the user list on every request, so a change applies at once). */
    principal(sessionId: string | undefined): { principal: Principal; csrf: string } | undefined {
      const s = sessionId ? sessions.get(sessionId) : undefined;
      if (!s) return undefined;
      if (s.exp <= now()) {
        sessions.delete(sessionId!);
        return undefined;
      }
      const user = users.get(s.name);
      const permissions: Permission[] = user?.sso ? user.permissions : [];
      return { principal: { name: s.name, permissions, source: "sso" }, csrf: s.csrf };
    },

    logout(sessionId: string | undefined) {
      if (sessionId) sessions.delete(sessionId);
    },

    sessionCookie: (id: string, maxAgeSeconds: number) => cookie(SESSION_COOKIE, id, maxAgeSeconds),
    clearSessionCookie: () => cookie(SESSION_COOKIE, "", 0),
    clearStateCookie: () => cookie(STATE_COOKIE, "", 0, "/api/auth"),
  };
}
export type Oidc = ReturnType<typeof createOidc>;
