import { describe, expect, it } from "vitest";
import { loadConfig } from "./config.js";

describe("the administrator's name", () => {
  it("is admin unless HOMEGRAF_ADMIN says otherwise", () => {
    expect(loadConfig({}).admin).toBe("admin");
    expect(loadConfig({ HOMEGRAF_ADMIN: "  " }).admin).toBe("admin");
  });

  it("can be an e-mail address; sign-in compares names in lower case", () => {
    expect(loadConfig({ HOMEGRAF_ADMIN: " Me@Example.org " }).admin).toBe("me@example.org");
  });
});

describe("OIDC_ADMINS", () => {
  const oidc = { OIDC_ISSUER: "https://id.example", OIDC_CLIENT_ID: "a", OIDC_CLIENT_SECRET: "b", HOMEGRAF_URL: "https://map.example" };
  it("lists provider names in lower case, separated by commas or spaces", () => {
    expect(loadConfig({ ...oidc, OIDC_ADMINS: "Owner, alice  bob" }).oidc?.admins).toEqual(["owner", "alice", "bob"]);
    expect(loadConfig(oidc).oidc?.admins).toEqual([]);
  });
});
