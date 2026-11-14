import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PERMISSIONS, type Permission, type UserInfo } from "../shared/types";
import { signedIn, useCapabilities, useSso, type Session } from "./api";
import { PageHeader, Section } from "./Page";

const PERMISSION_IDS = Object.keys(PERMISSIONS) as Permission[];

export interface Me {
  name: string;
  permissions: Permission[];
  source: "builtin" | "user" | "sso";
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

function PermissionChecks({
  value,
  onChange,
  disabled,
  describe,
}: {
  value: Permission[];
  onChange: (p: Permission[]) => void;
  disabled?: boolean;
  describe?: boolean;
}) {
  return (
    <div className={describe ? "flex flex-col gap-1.5" : "flex flex-wrap gap-x-4 gap-y-1"}>
      {PERMISSION_IDS.map((p) => (
        <label key={p} className="check" title={PERMISSIONS[p]}>
          <input
            type="checkbox"
            disabled={disabled}
            checked={value.includes(p)}
            onChange={(e) => onChange(e.target.checked ? [...value, p] : value.filter((x) => x !== p))}
          />{" "}
          {p}
          {describe && <span className="muted"> — {PERMISSIONS[p]}</span>}
        </label>
      ))}
    </div>
  );
}

function ChangePassword({ session }: { session: Session }) {
  const [password, setPassword] = useState("");
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const change = useMutation({
    mutationFn: () => signedIn(session, "PUT", "/api/me/password", { password }),
    onSuccess: () => {
      setPassword("");
      setNote({ ok: true, text: "Password changed." });
    },
    onError: (e) => setNote({ ok: false, text: message(e) }),
  });
  return (
    <form
      className="mt-4 max-w-sm border-t pt-4"
      onSubmit={(e) => {
        e.preventDefault();
        setNote(null);
        change.mutate();
      }}
    >
      <p className="m-0 mb-1 text-sm font-medium">Change your password</p>
      <p className="muted m-0 mb-2 text-xs">12 characters or more.</p>
      <div className="flex gap-2">
        <Input
          type="password"
          autoComplete="new-password"
          placeholder="New password"
          aria-label="New password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <Button type="submit" variant="outline" disabled={!password || change.isPending}>
          Change
        </Button>
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
    onSuccess: () => {
      setPassword("");
      setError(null);
      refresh();
    },
    onError: (e) => setError(message(e)),
  });
  const url = `/api/users/${encodeURIComponent(user.name)}`;
  return (
    <tr className="border-b align-top">
      <td className="py-2 pr-4">
        {user.name}{" "}
        {user.builtin && (
          <Badge variant="outline" title="Its password is HOMEGRAF_PASSWORD in the environment">
            built in
          </Badge>
        )}
        {user.sso && (
          <Badge variant="outline" title="Signs in through the identity provider; has no password here">
            single sign-on
          </Badge>
        )}
        {user.created && <div className="muted text-xs">since {new Date(user.created).toLocaleDateString()}</div>}
      </td>
      <td className="py-2 pr-4">
        <PermissionChecks
          value={user.permissions}
          disabled={user.builtin || run.isPending}
          onChange={(permissions) => run.mutate(() => signedIn(session, "PATCH", url, { permissions }))}
        />
      </td>
      <td className="py-2">
        {!user.builtin && (
          <div className="flex flex-wrap items-center gap-2">
            {!user.sso && (
              <Input
                type="password"
                autoComplete="new-password"
                className="w-48"
                placeholder="New password"
                aria-label={`New password for ${user.name}`}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            )}
            {!user.sso && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={!password || run.isPending}
                onClick={() => run.mutate(() => signedIn(session, "PATCH", url, { password }))}
              >
                Set password
              </Button>
            )}
            <Button
              type="button"
              variant="destructive"
              size="sm"
              disabled={run.isPending}
              onClick={() => confirm(`Remove ${user.name}?`) && run.mutate(() => signedIn(session, "DELETE", url))}
            >
              Remove
            </Button>
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
    mutationFn: () =>
      signedIn(session, "POST", "/api/users", viaSso ? { name: name.trim(), sso: true, permissions } : { name: name.trim(), password, permissions }),
    onSuccess: () => {
      setName("");
      setPassword("");
      setError(null);
      refresh();
    },
    onError: (e) => setError(message(e)),
  });
  return (
    <form
      className="max-w-xl"
      onSubmit={(e) => {
        e.preventDefault();
        add.mutate();
      }}
    >
      <div className="grid gap-2">
        <Input
          autoComplete="off"
          placeholder={viaSso ? "User name at the identity provider" : "Name (lower case letters, digits, . _ -)"}
          aria-label="New user name"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        {sso?.enabled && (
          <label className="check">
            <input type="checkbox" checked={viaSso} onChange={(e) => setViaSso(e.target.checked)} /> signs in through {sso.label ?? "single sign-on"} (no
            password)
          </label>
        )}
        {!viaSso && (
          <Input
            type="password"
            autoComplete="new-password"
            placeholder="Password (12 characters or more)"
            aria-label="Password for the new user"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        )}
        <p className="muted m-0 mt-1 text-xs">What this user may do:</p>
        <PermissionChecks value={permissions} onChange={setPermissions} describe />
      </div>
      <div className="actions !mb-0">
        <Button type="submit" disabled={!name.trim() || (!viaSso && !password) || add.isPending}>
          Add user
        </Button>
      </div>
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
      <Section title="Users" description="Everyone who can sign in here. Tick a permission to grant it; the change applies at once.">
        {users.isPending && <p className="muted m-0">Loading…</p>}
        {users.isError && <p className="warning !px-0">{message(users.error)}</p>}
        {users.data && (
          <table className="w-full text-left text-sm">
            <thead className="muted">
              <tr>
                <th className="pb-1 pr-4 font-normal">Name</th>
                <th className="pb-1 pr-4 font-normal">May</th>
                <th className="pb-1 font-normal">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.data.users.map((u) => (
                <UserRow key={u.name} user={u} session={session} refresh={refresh} />
              ))}
            </tbody>
          </table>
        )}
      </Section>
      <Section title="Add a user" description="Creates an account. Pick only the permissions the person needs.">
        <AddUser session={session} refresh={refresh} />
      </Section>
    </>
  );
}

export function Users({ session }: { session: Session }) {
  const caps = useCapabilities().data?.users;
  const me = useQuery({ queryKey: ["me", session.name], retry: false, queryFn: () => signedIn<Me>(session, "GET", "/api/me") });
  if (caps && !caps.enabled)
    return (
      <div className="list">
        <PageHeader title="Account and users">Users are off{caps.reason ? ` (${caps.reason})` : ""}.</PageHeader>
      </div>
    );
  return (
    <div className="list">
      <PageHeader title="Account and users">
        Your own account, and, if you may manage users, everyone else's. Permissions decide what a user can change in this app; without any, they can only look.
      </PageHeader>
      {caps?.reason && <p className="warning !px-0">{caps.reason}</p>}
      {me.isError && <p className="warning !px-0">{message(me.error)}</p>}
      {me.isPending && <p className="muted">Loading…</p>}
      {me.data && (
        <>
          <Section
            title={
              <>
                {me.data.name}{" "}
                <Badge variant="outline" className="align-middle">
                  you{me.data.source === "sso" ? ", single sign-on" : ""}
                </Badge>
              </>
            }
            description="Your account and what you may do."
          >
            {me.data.permissions.length ? (
              <ul className="m-0 list-none p-0 text-sm">
                {me.data.permissions.map((p) => (
                  <li key={p} className="py-0.5">
                    <Badge variant="outline" className="mr-2">
                      {p}
                    </Badge>
                    <span className="muted">{PERMISSIONS[p]}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted m-0">You may only look: no permissions are granted to you.</p>
            )}
            {me.data.source === "user" && <ChangePassword session={session} />}
          </Section>
          {me.data.permissions.includes("users:manage") ? (
            <Manage session={session} />
          ) : (
            <Section
              title="Users and permissions"
              description="Adding users, removing them and changing what they may do is reserved to accounts with the users:manage permission."
            >
              <p className="muted m-0 text-sm">
                Your account does not have it, so the user list and the add form are hidden. Sign in as the built-in administrator (the password is{" "}
                <code>HOMEGRAF_PASSWORD</code> in the environment), or ask an administrator to grant you <code>users:manage</code>. There are no groups: every
                user gets individual permissions.
              </p>
              <p className="m-0 mt-3 text-sm font-medium">The permissions that exist</p>
              <ul className="m-0 mt-1 list-none p-0 text-sm">
                {PERMISSION_IDS.map((p) => (
                  <li key={p} className="py-0.5">
                    <Badge variant="outline" className="mr-2">
                      {p}
                    </Badge>
                    <span className="muted">{PERMISSIONS[p]}</span>
                  </li>
                ))}
              </ul>
            </Section>
          )}
        </>
      )}
    </div>
  );
}
