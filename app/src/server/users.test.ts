import { existsSync, mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { hashPassword, userStore, UserError, verifyPassword } from "./users.js";

const dir = () => mkdtempSync(join(tmpdir(), "homegraf-"));

describe("passwords", () => {
  it("hash with a fresh salt and verify only the right password", async () => {
    const a = await hashPassword("correct horse battery");
    const b = await hashPassword("correct horse battery");
    expect(a).not.toBe(b);
    expect(a).toMatch(/^scrypt\$32768\$/);
    expect(await verifyPassword("correct horse battery", a)).toBe(true);
    expect(await verifyPassword("correct horse batterY", a)).toBe(false);
    expect(await verifyPassword("x", "garbage")).toBe(false);
  });
});

describe("userStore", () => {
  it("keeps users in a private file that a new store reads back, without the password", async () => {
    const file = join(dir(), "data", "users.json");
    const store = userStore(file, () => new Date("2026-01-02T03:04:05Z"));
    await store.add("alice", "correct horse battery", ["apps:create"]);
    expect(statSync(file).mode & 0o777).toBe(0o600);
    expect(readFileSync(file, "utf8")).not.toContain("correct horse");
    const again = userStore(file);
    expect(again.list()).toMatchObject([{ name: "alice", permissions: ["apps:create"], created: "2026-01-02T03:04:05.000Z" }]);
    expect(await verifyPassword("correct horse battery", again.get("alice")!.hash)).toBe(true);
    expect(existsSync(`${file}.tmp`)).toBe(false);
  });

  it("reserves the configured administrator's name instead of admin", async () => {
    const store = userStore(join(dir(), "users.json"), undefined, "boss");
    expect(((await store.add("boss", "correct horse battery", []).catch((e: unknown) => e)) as UserError).message).toMatch(/"boss" is reserved/);
    expect(((await store.addSso("boss", []).catch((e: unknown) => e)) as UserError).message).toMatch(/"boss" is reserved/);
    expect((await store.add("admin", "correct horse battery", [])).name).toBe("admin");
  });

  it("validates names, the reserved admin, duplicates, passwords and permissions", async () => {
    const store = userStore(join(dir(), "users.json"));
    const refused = async (p: Promise<unknown>) => (await p.catch((e: unknown) => e)) as UserError;
    expect((await refused(store.add("admin", "correct horse battery", []))).message).toMatch(/reserved/);
    expect((await refused(store.add("Alice", "correct horse battery", []))).message).toMatch(/lower case/);
    expect((await refused(store.add("a b", "correct horse battery", []))).message).toMatch(/lower case/);
    expect((await refused(store.add("alice", "short", []))).message).toMatch(/at least 12/);
    expect((await refused(store.add("alice", "correct horse battery", ["root" as never]))).message).toMatch(/permissions must be/);
    await store.add("alice", "correct horse battery", ["apps:create", "apps:create"]);
    expect(store.get("alice")!.permissions).toEqual(["apps:create"]);
    expect(await refused(store.add("alice", "correct horse battery", []))).toMatchObject({ status: 409 });
    expect(store.list()).toHaveLength(1);
  });

  it("changes a password or permissions and removes a user", async () => {
    const store = userStore(join(dir(), "users.json"));
    await store.add("alice", "correct horse battery", []);
    const before = store.get("alice")!.hash;
    await store.update("alice", { permissions: ["users:manage"] });
    expect(store.get("alice")).toMatchObject({ hash: before, permissions: ["users:manage"] });
    await store.update("alice", { password: "a different password" });
    expect(await verifyPassword("a different password", store.get("alice")!.hash)).toBe(true);
    store.remove("alice");
    expect(store.list()).toEqual([]);
    expect(() => store.remove("alice")).toThrow(/no user alice/);
    await expect(store.update("alice", {})).rejects.toMatchObject({ status: 404 });
  });

  it("does not touch a file it cannot read", async () => {
    const file = join(dir(), "users.json");
    writeFileSync(file, "{ not json");
    const store = userStore(file);
    expect(store.problem).toMatch(/unreadable/);
    expect(store.list()).toEqual([]);
    await expect(store.add("alice", "correct horse battery", [])).rejects.toMatchObject({ status: 503 });
    expect(readFileSync(file, "utf8")).toBe("{ not json");
  });
});
