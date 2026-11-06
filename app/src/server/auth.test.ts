import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { authenticator, basicCredentials } from "./auth.js";
import { userStore } from "./users.js";

const basic = (name: string, password: string) => `Basic ${Buffer.from(`${name}:${password}`).toString("base64")}`;
const freshStore = () => userStore(join(mkdtempSync(join(tmpdir(), "homegraf-")), "users.json"));

describe("basicCredentials", () => {
  it("reads name and password, the password may contain colons", () => {
    expect(basicCredentials(basic("alice", "pa:ss:word"))).toEqual({ name: "alice", password: "pa:ss:word" });
  });
  it("rejects anything else", () => {
    for (const h of [
      undefined,
      "",
      "Bearer abc",
      "Basic",
      "Basic !!!",
      `Basic ${Buffer.from("nocolon").toString("base64")}`,
      `Basic ${Buffer.from(":nopassword-name").toString("base64")}`,
    ])
      expect(basicCredentials(h)).toBeUndefined();
  });
});

describe("authenticator", () => {
  it("is disabled without the administrator's password", async () => {
    expect(await authenticator({ adminPassword: undefined, users: freshStore() })(basic("admin", "x"))).toEqual({ ok: false, reason: "disabled" });
  });

  it("asks for credentials when the request has none", async () => {
    expect(await authenticator({ adminPassword: "s3cret-admin", users: freshStore() })(undefined)).toEqual({ ok: false, reason: "missing" });
  });

  it("signs in the administrator with every permission", async () => {
    const auth = authenticator({ adminPassword: "s3cret-admin", users: freshStore() });
    const ok = await auth(basic("admin", "s3cret-admin"));
    expect(ok).toMatchObject({ ok: true, principal: { name: "admin", source: "builtin" } });
    expect(ok.ok && ok.principal.permissions).toEqual(["apps:create", "apps:remove", "hosts:edit", "stacks:deploy", "secrets:manage", "users:manage"]);
    expect(await auth(basic("admin", "s3cret-admiN"))).toEqual({ ok: false, reason: "wrong" });
  });

  it("names the administrator after HOMEGRAF_ADMIN, such as the e-mail address the stack was set up with", async () => {
    const auth = authenticator({ adminPassword: "s3cret-admin", admin: "me@example.org", users: freshStore() });
    const ok = await auth(basic("Me@Example.org", "s3cret-admin"));
    expect(ok).toMatchObject({ ok: true, principal: { name: "me@example.org", source: "builtin" } });
    expect(await auth(basic("admin", "s3cret-admin"))).toEqual({ ok: false, reason: "wrong" });
  });

  it("signs in a stored user with exactly that user's permissions", async () => {
    const users = freshStore();
    await users.add("alice", "correct horse battery", ["apps:create"]);
    const auth = authenticator({ adminPassword: "s3cret-admin", users });
    expect(await auth(basic("alice", "correct horse battery"))).toEqual({
      ok: true,
      principal: { name: "alice", permissions: ["apps:create"], source: "user" },
    });
    expect(await auth(basic("alice", "wrong horse battery"))).toEqual({ ok: false, reason: "wrong" });
    expect(await auth(basic("bob", "correct horse battery"))).toEqual({ ok: false, reason: "wrong" });
  });

  it("locks a name after repeated failures, even for the right password, until they age out; other names are not affected", async () => {
    const users = freshStore();
    await users.add("alice", "correct horse battery", []);
    await users.add("bob", "another long password", []);
    let now = 0;
    const auth = authenticator({ adminPassword: "s3cret-admin", users, now: () => now, maxFailures: 3, windowMs: 1000 });
    for (let i = 0; i < 3; i++) expect((await auth(basic("alice", "nope"))).ok).toBe(false);
    expect(await auth(basic("alice", "correct horse battery"))).toEqual({ ok: false, reason: "locked" });
    expect((await auth(basic("bob", "another long password"))).ok).toBe(true);
    now = 1001;
    expect((await auth(basic("alice", "correct horse battery"))).ok).toBe(true);
  });

  it("a success clears the failures", async () => {
    const auth = authenticator({ adminPassword: "s3cret-admin", users: freshStore(), now: () => 0, maxFailures: 3, windowMs: 1000 });
    await auth(basic("admin", "a"));
    await auth(basic("admin", "b"));
    expect((await auth(basic("admin", "s3cret-admin"))).ok).toBe(true);
    expect([(await auth(basic("admin", "a"))).ok, (await auth(basic("admin", "b"))).ok]).toEqual([false, false]);
    expect((await auth(basic("admin", "s3cret-admin"))).ok).toBe(true);
  });

  it("counts unknown names too, and names differing in case are the same name", async () => {
    const auth = authenticator({ adminPassword: "s3cret-admin", users: freshStore(), now: () => 0, maxFailures: 2, windowMs: 1000 });
    await auth(basic("ghost", "x"));
    await auth(basic("GHOST", "x"));
    expect(await auth(basic("ghost", "x"))).toEqual({ ok: false, reason: "locked" });
  });
});
