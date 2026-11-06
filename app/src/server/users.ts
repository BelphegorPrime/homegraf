import { randomBytes, scrypt, timingSafeEqual } from "node:crypto";
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { PERMISSIONS, type Permission } from "../shared/types.js";

/** The built-in administrator: its password is HOMEGRAF_PASSWORD, so nobody can be locked out of the user list. */
export const ADMIN = "admin";
export const NAME_RE = /^[a-z0-9][a-z0-9._-]{1,31}$/;
export const MIN_PASSWORD = 12;
const N = 2 ** 15;

export interface StoredUser {
  name: string;
  /** `scrypt$N$salt$key`, base64: the cost is stored with the hash so it can be raised later. */
  hash: string;
  permissions: Permission[];
  created: string;
  /** Signs in through the identity provider: no password (the hash is empty), the name is the provider's user name. */
  sso?: boolean;
  /** The provider's stable id of that account, bound at the first sign-in: a renamed or re-registered name cannot take over the entry. */
  sub?: string;
}

const derive = (password: string, salt: Buffer, n: number) =>
  new Promise<Buffer>((resolve, reject) => scrypt(password, salt, 32, { N: n, r: 8, p: 1, maxmem: 128 * n * 8 * 2 }, (e, k) => (e ? reject(e) : resolve(k))));

export async function hashPassword(password: string): Promise<string> {
  const salt = randomBytes(16);
  return `scrypt$${N}$${salt.toString("base64")}$${(await derive(password, salt, N)).toString("base64")}`;
}

export async function verifyPassword(password: string, stored: string): Promise<boolean> {
  const [kind, n, salt, key] = stored.split("$");
  if (kind !== "scrypt" || !n || !salt || !key) return false;
  const want = Buffer.from(key, "base64");
  const got = await derive(password, Buffer.from(salt, "base64"), Number(n));
  return got.length === want.length && timingSafeEqual(got, want);
}

/** Compared against when the name is unknown, so that a wrong name takes as long as a wrong password. */
export const DUMMY_HASH = hashPassword("no such user");

export const isPermission = (p: unknown): p is Permission => typeof p === "string" && p in PERMISSIONS;

export interface UserStore {
  /** Why the users cannot be changed (unreadable file), or undefined. */
  readonly problem?: string;
  list(): StoredUser[];
  get(name: string): StoredUser | undefined;
  /** Throws a message fit for the user: name taken, reserved, malformed. */
  add(name: string, password: string, permissions: Permission[]): Promise<StoredUser>;
  /** An entry for someone who signs in through the identity provider. */
  addSso(name: string, permissions: Permission[]): Promise<StoredUser>;
  /** Remembers which account of the provider this entry belongs to. */
  bind(name: string, sub: string): void;
  update(name: string, change: { password?: string; permissions?: Permission[] }): Promise<StoredUser>;
  remove(name: string): void;
}

export class UserError extends Error {
  constructor(
    message: string,
    readonly status: 400 | 404 | 409 | 503 = 400,
  ) {
    super(message);
  }
}

export function checkPassword(password: unknown): string {
  if (typeof password !== "string" || password.length < MIN_PASSWORD) throw new UserError(`the password needs at least ${MIN_PASSWORD} characters`);
  if (password.length > 256) throw new UserError("the password is too long");
  return password;
}

function checkPermissions(permissions: unknown): Permission[] {
  if (!Array.isArray(permissions) || !permissions.every(isPermission))
    throw new UserError(`permissions must be a list of: ${Object.keys(PERMISSIONS).join(", ")}`);
  return [...new Set(permissions)];
}

/**
 * The users as a JSON file on a volume (mode 0600, replaced atomically on every change). A small list kept in memory is
 * enough for a homelab; the store is the one place that would move to a database or an identity provider.
 */
export function userStore(file: string, now: () => Date = () => new Date(), admin: string = ADMIN): UserStore {
  let users: StoredUser[] = [];
  let problem: string | undefined;
  try {
    if (existsSync(file)) {
      const parsed = JSON.parse(readFileSync(file, "utf8")) as { users?: StoredUser[] };
      if (!Array.isArray(parsed.users)) throw new Error("no users list");
      users = parsed.users;
    }
  } catch (err) {
    // Keep the file as it is: overwriting it would destroy what an operator may still be able to repair.
    problem = `${file} is unreadable (${err instanceof Error ? err.message : err}); users cannot be changed until it is fixed`;
  }

  const save = () => {
    if (problem) throw new UserError(problem, 503);
    mkdirSync(dirname(file), { recursive: true });
    const tmp = `${file}.tmp`;
    writeFileSync(tmp, JSON.stringify({ users }, null, 2) + "\n", { mode: 0o600 });
    chmodSync(tmp, 0o600);
    renameSync(tmp, file);
  };
  const find = (name: string) => users.find((u) => u.name === name);

  return {
    get problem() {
      return problem;
    },
    list: () => users.map((u) => ({ ...u, permissions: [...u.permissions] })),
    get: (name) => find(name),
    async add(name, password, permissions) {
      if (problem) throw new UserError(problem, 503);
      if (name === admin) throw new UserError(`"${admin}" is reserved for the administrator from the environment`);
      if (!NAME_RE.test(name)) throw new UserError("the name is 2 to 32 characters: lower case letters, digits, . _ -");
      if (find(name)) throw new UserError(`user ${name} exists`, 409);
      const user: StoredUser = {
        name,
        hash: await hashPassword(checkPassword(password)),
        permissions: checkPermissions(permissions),
        created: now().toISOString(),
      };
      users.push(user);
      save();
      return user;
    },
    async addSso(name, permissions) {
      if (problem) throw new UserError(problem, 503);
      if (name === admin) throw new UserError(`"${admin}" is reserved for the administrator from the environment`);
      if (!NAME_RE.test(name)) throw new UserError("the name is 2 to 32 characters: lower case letters, digits, . _ -");
      if (find(name)) throw new UserError(`user ${name} exists`, 409);
      const user: StoredUser = { name, hash: "", sso: true, permissions: checkPermissions(permissions), created: now().toISOString() };
      users.push(user);
      save();
      return user;
    },
    bind(name, sub) {
      if (problem) throw new UserError(problem, 503);
      const user = find(name);
      if (!user?.sso) throw new UserError(`no single-sign-on user ${name}`, 404);
      users = users.map((u) => (u === user ? { ...user, sub } : u));
      save();
    },
    async update(name, change) {
      if (problem) throw new UserError(problem, 503);
      const user = find(name);
      if (!user) throw new UserError(`no user ${name}`, 404);
      if (user.sso && change.password !== undefined) throw new UserError(`${name} signs in through the identity provider and has no password`);
      const next = { ...user };
      if (change.password !== undefined) next.hash = await hashPassword(checkPassword(change.password));
      if (change.permissions !== undefined) next.permissions = checkPermissions(change.permissions);
      users = users.map((u) => (u === user ? next : u));
      save();
      return next;
    },
    remove(name) {
      if (problem) throw new UserError(problem, 503);
      if (!find(name)) throw new UserError(`no user ${name}`, 404);
      users = users.filter((u) => u.name !== name);
      save();
    },
  };
}
