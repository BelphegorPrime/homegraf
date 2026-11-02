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
