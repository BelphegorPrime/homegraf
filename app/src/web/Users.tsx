import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PERMISSIONS, type Permission, type UserInfo } from "../shared/types";
import { rememberedUser, rememberUser, signedIn, useCapabilities, useSso, type Session } from "./api";

const PERMISSION_IDS = Object.keys(PERMISSIONS) as Permission[];

export interface Me {
  name: string;
  permissions: Permission[];
  source: "builtin" | "user" | "sso";
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

function PermissionChecks({ value, onChange, disabled }: { value: Permission[]; onChange: (p: Permission[]) => void; disabled?: boolean }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1">
      {PERMISSION_IDS.map((p) => (
        <label key={p} className="check" title={PERMISSIONS[p]}>
          <input type="checkbox" disabled={disabled} checked={value.includes(p)}
                 onChange={(e) => onChange(e.target.checked ? [...value, p] : value.filter((x) => x !== p))} /> {p}
        </label>
      ))}
    </div>
  );
}

/** Why the last single sign-on did not work, as the server put it in the address; shown once and removed from the address. */
function ssoError(): string | null {
  const q = new URLSearchParams(location.search);
  const error = q.get("sso_error");
  if (error) { q.delete("sso_error"); history.replaceState(null, "", `${location.pathname}${q.size ? `?${q}` : ""}${location.hash}`); }
  return error;
}

export function SignIn({ onSignedIn }: { onSignedIn: (s: Session, me: Me) => void }) {
  const sso = useSso().data;
  const [ssoFailure] = useState(ssoError);
  const [name, setName] = useState(rememberedUser);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const login = useMutation({
    mutationFn: async () => ({ session: { name: name.trim(), password }, me: await signedIn<Me>({ name: name.trim(), password }, "GET", "/api/me") }),
    onSuccess: ({ session, me }) => { rememberUser(session.name); setPassword(""); onSignedIn(session, me); },
    onError: (e) => { setPassword(""); setError(message(e)); },
  });
  return (
    <form className="max-w-sm" onSubmit={(e) => { e.preventDefault(); setError(null); login.mutate(); }}>
      <p className="muted">Sign in to manage users or to see what you may do. The map itself needs no sign-in.</p>
      {(ssoFailure || sso?.problem) && <p className="warning !px-0">{ssoFailure ?? sso?.problem}</p>}
      {sso?.enabled && (
        <div className="actions">
          <Button nativeButton={false} render={<a href="/api/auth/login" />}>Sign in with {sso.label ?? "single sign-on"}</Button>
          <span className="muted">or with a name and password:</span>
        </div>
      )}
      <div className="grid gap-2">
        <Input autoComplete="username" placeholder="User name" aria-label="User name" value={name} onChange={(e) => setName(e.target.value)} />
        <Input type="password" autoComplete="current-password" placeholder="Password" aria-label="Password" value={password} onChange={(e) => setPassword(e.target.value)} />
      </div>
      <div className="actions"><Button type="submit" disabled={!name.trim() || !password || login.isPending}>Sign in</Button></div>
      {error && <p className="warning !px-0">{error}</p>}
    </form>
  );
}

function ChangePassword({ session, onChanged }: { session: Session; onChanged: (password: string) => void }) {
  const [password, setPassword] = useState("");
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const change = useMutation({
    mutationFn: () => signedIn(session, "PUT", "/api/me/password", { password }),
    onSuccess: () => { onChanged(password); setPassword(""); setNote({ ok: true, text: "Password changed." }); },
    onError: (e) => setNote({ ok: false, text: message(e) }),
  });
  return (
    <form className="max-w-sm" onSubmit={(e) => { e.preventDefault(); setNote(null); change.mutate(); }}>
      <h2>Your password</h2>
      <p className="muted">12 characters or more.</p>
      <div className="flex gap-2">
        <Input type="password" autoComplete="new-password" placeholder="New password" aria-label="New password" value={password} onChange={(e) => setPassword(e.target.value)} />
        <Button type="submit" variant="outline" disabled={!password || change.isPending}>Change</Button>
      </div>
      {note && <p className={note.ok ? "muted" : "warning !px-0"}>{note.text}</p>}
    </form>
  );
}

function UserRow({ user, session, refresh }: { user: UserInfo; session: Session; refresh: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: (job: () => Promise<unknown>) => job(),
    onSuccess: () => { setPassword(""); setError(null); refresh(); },
    onError: (e) => setError(message(e)),
  });
  const url = `/api/users/${encodeURIComponent(user.name)}`;
  return (
    <tr className="border-b align-top">
      <td className="py-2 pr-4">
        {user.name} {user.builtin && <Badge variant="outline" title="Its password is HOMEGRAF_PASSWORD in the environment">built in</Badge>}
        {user.sso && <Badge variant="outline" title="Signs in through the identity provider; has no password here">single sign-on</Badge>}
        {user.created && <div className="muted text-xs">since {new Date(user.created).toLocaleDateString()}</div>}
      </td>
      <td className="py-2 pr-4">
        <PermissionChecks value={user.permissions} disabled={user.builtin || run.isPending}
                          onChange={(permissions) => run.mutate(() => signedIn(session, "PATCH", url, { permissions }))} />
      </td>
      <td className="py-2">
        {!user.builtin && (
          <div className="flex flex-wrap items-center gap-2">
            {!user.sso && <Input type="password" autoComplete="new-password" className="w-48" placeholder="New password" aria-label={`New password for ${user.name}`} value={password} onChange={(e) => setPassword(e.target.value)} />}
            {!user.sso && <Button type="button" variant="outline" size="sm" disabled={!password || run.isPending} onClick={() => run.mutate(() => signedIn(session, "PATCH", url, { password }))}>Set password</Button>}
            <Button type="button" variant="destructive" size="sm" disabled={run.isPending}
                    onClick={() => confirm(`Remove ${user.name}?`) && run.mutate(() => signedIn(session, "DELETE", url))}>Remove</Button>
          </div>
        )}
        {error && <div className="warning !px-0">{error}</div>}
      </td>
    </tr>
  );
}

function AddUser({ session, refresh }: { session: Session; refresh: () => void }) {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [permissions, setPermissions] = useState<Permission[]>(["apps:create"]);
  const [error, setError] = useState<string | null>(null);
  const sso = useSso().data;
  const [viaSso, setViaSso] = useState(false);
  const add = useMutation({
    mutationFn: () => signedIn(session, "POST", "/api/users", viaSso ? { name: name.trim(), sso: true, permissions } : { name: name.trim(), password, permissions }),
    onSuccess: () => { setName(""); setPassword(""); setError(null); refresh(); },
    onError: (e) => setError(message(e)),
  });
  return (
    <form className="max-w-xl" onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
      <h2>Add a user</h2>
      <p className="muted">Name: lower case letters, digits, . _ - (2 to 32 characters){viaSso ? ", the user name at the identity provider" : ". Password: 12 characters or more"}.</p>
      <div className="grid gap-2">
        <Input autoComplete="off" placeholder="Name" aria-label="New user name" value={name} onChange={(e) => setName(e.target.value)} />
        {sso?.enabled && <label className="check"><input type="checkbox" checked={viaSso} onChange={(e) => setViaSso(e.target.checked)} /> signs in through {sso.label ?? "single sign-on"} (no password)</label>}
        {!viaSso && <Input type="password" autoComplete="new-password" placeholder="Password" aria-label="Password for the new user" value={password} onChange={(e) => setPassword(e.target.value)} />}
        <PermissionChecks value={permissions} onChange={setPermissions} />
      </div>
      <div className="actions"><Button type="submit" disabled={!name.trim() || (!viaSso && !password) || add.isPending}>Add user</Button></div>
      {error && <p className="warning !px-0">{error}</p>}
    </form>
  );
}

function Manage({ session }: { session: Session }) {
  const client = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => signedIn<{ users: UserInfo[] }>(session, "GET", "/api/users") });
  const refresh = () => void client.invalidateQueries({ queryKey: ["users"] });
  return (
    <>
      <h2>Users</h2>
      {users.isError && <p className="warning !px-0">{message(users.error)}</p>}
      {users.data && (
        <table className="w-full max-w-4xl text-left text-sm">
          <thead className="muted"><tr><th className="pb-1 pr-4 font-normal">Name</th><th className="pb-1 pr-4 font-normal">May</th><th className="pb-1 font-normal" /></tr></thead>
          <tbody>{users.data.users.map((u) => <UserRow key={u.name} user={u} session={session} refresh={refresh} />)}</tbody>
        </table>
      )}
      <AddUser session={session} refresh={refresh} />
    </>
  );
}

export function Users({ session, onSession }: { session: Session | null; onSession: (s: Session | null) => void }) {
  const caps = useCapabilities().data?.users;
  const me = useQuery({ queryKey: ["me", session?.name, !!session?.sso], enabled: !!session, retry: false, queryFn: () => signedIn<Me>(session!, "GET", "/api/me") });
  if (caps && !caps.enabled) return <div className="list"><p className="muted">Users are off{caps.reason ? ` (${caps.reason})` : ""}.</p></div>;
  return (
    <div className="list">
      {caps?.reason && <p className="warning !px-0">{caps.reason}</p>}
      {!session || !me.data ? (
        <>
          {me.isError && <p className="warning !px-0">{message(me.error)}</p>}
          <SignIn onSignedIn={(s) => onSession(s)} />
        </>
      ) : (
        <>
          <p>
            Signed in as <strong>{me.data.name}</strong>{me.data.source === "sso" ? " through single sign-on" : ""}. May: {me.data.permissions.length ? me.data.permissions.map((p) => <Badge key={p} variant="outline" className="mr-1" title={PERMISSIONS[p]}>{p}</Badge>) : <span className="muted">look only</span>}{" "}
            <Button type="button" variant="ghost" size="sm" onClick={() => onSession(null)}>Sign out</Button>
          </p>
          {me.data.source === "user" && <ChangePassword session={session} onChanged={(password) => onSession({ ...session, password })} />}
          {me.data.permissions.includes("users:manage") && <Manage session={session} />}
        </>
      )}
    </div>
  );
}
