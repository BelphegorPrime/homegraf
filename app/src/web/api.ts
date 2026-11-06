import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { Capabilities, CertificatesSnapshot, Graph, HostInfo, ManagedStack, PendingCreation, PendingRemoval, RemovableApp, RootCaInfo, SsoSession, StatusSnapshot } from "../shared/types";

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error((body as { error?: string }).error ?? `HTTP ${res.status}`), { status: res.status });
  return body as T;
}

export const useGraph = () =>
  useQuery({ queryKey: ["graph"], queryFn: () => getJson<Graph>("/api/graph"), refetchInterval: 60_000 });

const EMPTY: StatusSnapshot = { updated: null, source: "disabled", nodes: {} };

/** Live status over server-sent events; EventSource reconnects by itself. */
export function useStatus(): StatusSnapshot {
  const [snapshot, setSnapshot] = useState<StatusSnapshot>(EMPTY);
  useEffect(() => {
    const es = new EventSource("/api/events");
    es.addEventListener("status", (e) => setSnapshot(JSON.parse((e as MessageEvent).data) as StatusSnapshot));
    return () => es.close();
  }, []);
  return snapshot;
}

/** What the server can do (creating apps) and the state of the UniFi source behind the network layer. */
export const useCapabilities = () =>
  useQuery({ queryKey: ["capabilities"], queryFn: () => getJson<Capabilities>("/api/capabilities"), refetchInterval: 60_000 });

/** `Authorization: Basic …` for a user name and password; the server reads the same header for every write action. */
export const basicAuth = (name: string, password: string) => `Basic ${btoa(String.fromCharCode(...new TextEncoder().encode(`${name}:${password}`)))}`;

/** Who acts: a name and password typed by the person, or a session of the identity provider (its cookie goes along by itself; `sso.csrf` goes in a header). */
export interface Session {
  name: string;
  password: string;
  sso?: { csrf: string };
}

/** The headers that sign a request in as `session`. */
export const authHeaders = (session: Session): Record<string, string> =>
  session.sso ? { "x-csrf-token": session.sso.csrf } : { authorization: basicAuth(session.name, session.password) };

/** A request signed in as `session`; throws the server's message when it refuses. The password is never stored by the page. */
export async function signedIn<T>(session: Session, method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: { ...authHeaders(session), ...(body === undefined ? {} : { "content-type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data as { error?: string }).error ?? `HTTP ${res.status}`);
  return data as T;
}

/** Single sign-on: whether it is set up and who is signed in through it. */
export const useSso = () =>
  useQuery({ queryKey: ["sso"], queryFn: () => getJson<SsoSession>("/api/auth/session"), refetchInterval: 5 * 60_000 });

/** The session of the person signed in (cookie; the token goes in a header on writes), in the form every request takes. */
export const ssoSession = (user: NonNullable<SsoSession["user"]>): Session => ({ name: user.name, password: "", sso: { csrf: user.csrf } });

/** The last user name typed, a convenience only (never the password). */
export const rememberedUser = (): string => {
  try { return localStorage.getItem("homegraf-user") ?? ""; } catch { return ""; }
};
export const rememberUser = (name: string) => {
  try { localStorage.setItem("homegraf-user", name); } catch { /* private window: fine */ }
};

/** The certificates the domains serve; the server probes them and keeps the result for a few minutes. */
export const useCertificates = () =>
  useQuery({ queryKey: ["certificates"], queryFn: () => getJson<CertificatesSnapshot>("/api/certificates"), refetchInterval: 5 * 60_000 });

/** The apps that may be removed (never the platform) and the removals whose second half waits for a merge. */
export const useRemovable = () =>
  useQuery({ queryKey: ["removable"], queryFn: () => getJson<RemovableApp[]>("/api/apps/removable"), refetchInterval: 60_000 });
export const useRemovals = () =>
  useQuery({ queryKey: ["removals"], queryFn: () => getJson<{ removals: PendingRemoval[]; problem?: string }>("/api/removals"), refetchInterval: 15_000 });

/** Apps whose pull request waits for the merge or whose deploy is on its way; polled quickly while any is listed. */
export const useCreations = () =>
  useQuery({
    queryKey: ["creations"],
    queryFn: () => getJson<{ creations: PendingCreation[]; problem?: string }>("/api/creations"),
    refetchInterval: (q) => (q.state.data?.creations.some((c) => !c.done) ? 10_000 : 60_000),
  });

/** The certificates of the homelab CA (the file itself is /api/ca.pem); an error while the repository has none. */
export const useRootCa = () =>
  useQuery({ queryKey: ["ca"], queryFn: () => getJson<RootCaInfo>("/api/ca"), retry: false, refetchInterval: 5 * 60_000 });

/** The hosts ingressctl manages, with whether each redirects HTTP to HTTPS. */
export const useHosts = () =>
  useQuery({ queryKey: ["hosts"], queryFn: () => getJson<HostInfo[]>("/api/hosts"), refetchInterval: 60_000 });

/** POST as `user`; resolves with the job to follow, or throws the server's message. */
export async function startJob(path: string, body: unknown, session: Session): Promise<string> {
  const { jobId } = await signedIn<{ jobId: string }>(session, "POST", path, body);
  return jobId;
}

/** The stacks the UI may redeploy and manage the secrets of (never the platform). */
export const useStacks = () =>
  useQuery({ queryKey: ["stacks"], queryFn: () => getJson<ManagedStack[]>("/api/stacks"), refetchInterval: 5 * 60_000 });

/**
 * Who is signed in, by a cookie session (the login page, or the identity provider): `session` is what every request takes, `null` while
 * nobody is. `signIn` opens a session with a name and password; `signOut` ends whichever it is and drops everything fetched for it.
 */
export function useAuth() {
  const sso = useSso();
  const client = useQueryClient();
  const session = sso.data?.user ? ssoSession(sso.data.user) : null;
  return {
    session,
    async signIn(name: string, password: string) {
      const res = await fetch("/api/auth/password", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name, password }) });
      if (!res.ok) throw new Error(((await res.json().catch(() => ({}))) as { error?: string }).error ?? `HTTP ${res.status}`);
      await client.invalidateQueries({ queryKey: ["sso"] });
    },
    async signOut() {
      await fetch("/api/auth/logout", { method: "POST" }).catch(() => undefined);
      client.removeQueries({ predicate: (q) => q.queryKey[0] !== "sso" });
      await client.invalidateQueries({ queryKey: ["sso"] });
    },
  };
}
