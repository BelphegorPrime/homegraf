import { describe, expect, it } from "vitest";
import { applyEnvFile, parseEnvFile } from "./envfile.js";

describe("parseEnvFile", () => {
  it("reads KEY=value lines, skips comments and junk, keeps = inside values, strips quotes", () => {
    expect(parseEnvFile(`# comment\n\nA=1\n B = two words \nURL=http://x/y?z=1\nQ="quoted"\nS='single'\nnoequals\n=novalue\n1BAD=x\nE=\n`)).toEqual({
      A: "1",
      B: "two words",
      URL: "http://x/y?z=1",
      Q: "quoted",
      S: "single",
      E: "",
    });
  });
  it("copes with Windows line endings", () => expect(parseEnvFile("A=1\r\nB=2\r\n")).toEqual({ A: "1", B: "2" }));
});

describe("applyEnvFile", () => {
  const file = "KOMODO_API_KEY=from-file\nFORGEJO_TOKEN=tok\nEMPTY=\nPASSWORD=file\n";

  it("does nothing without HOMEGRAF_ENV_FILE", () => {
    const env: NodeJS.ProcessEnv = {};
    expect(applyEnvFile(env, () => file)).toEqual([]);
    expect(env).toEqual({});
  });

  it("does nothing while the file does not exist yet", () => {
    const env: NodeJS.ProcessEnv = { HOMEGRAF_ENV_FILE: "/state/homegraf.env" };
    expect(applyEnvFile(env, () => undefined)).toEqual([]);
  });

  it("fills what is unset or empty, never overrides what the environment says, never sets an empty value", () => {
    const env: NodeJS.ProcessEnv = { HOMEGRAF_ENV_FILE: "/state/homegraf.env", KOMODO_API_KEY: "", PASSWORD: "explicit" };
    expect(applyEnvFile(env, () => file).sort()).toEqual(["FORGEJO_TOKEN", "KOMODO_API_KEY"]);
    expect(env).toMatchObject({ KOMODO_API_KEY: "from-file", FORGEJO_TOKEN: "tok", PASSWORD: "explicit" });
    expect("EMPTY" in env).toBe(false);
  });
});
