import { describe, expect, it } from "vitest";
import type { PendingCreation } from "../shared/types";
import { creationSteps } from "./creationSteps";

const base: PendingCreation = { appId: "demo", server: "s", pullRequestUrl: "u", domain: "demo.lab.example", at: "t", pr: "open", done: false };
const states = (c: Partial<PendingCreation>) => creationSteps({ ...base, ...c }).map((s) => s.state);

describe("creationSteps", () => {
  it("starts at the merge", () => {
    expect(states({})).toEqual(["now", "waiting", "waiting"]);
    expect(creationSteps(base)[0].detail).toMatch(/merge it/);
  });

  it("moves on to the stack once merged, and to HTTPS once the stack runs", () => {
    expect(states({ pr: "merged", stack: { health: "down", detail: "Down" } })).toEqual(["done", "now", "waiting"]);
    expect(states({ pr: "merged", stack: { health: "ok" } })).toEqual(["done", "done", "now"]);
    expect(creationSteps({ ...base, pr: "merged", stack: { health: "ok" }, https: { ok: false, detail: "ENOTFOUND" } })[2].detail).toBe("ENOTFOUND");
  });

  it("is through when HTTPS answers", () => {
    expect(states({ pr: "merged", stack: { health: "ok" }, https: { ok: true }, done: true })).toEqual(["done", "done", "done"]);
  });

  it("has no HTTPS step without a domain", () => {
    expect(states({ domain: undefined })).toEqual(["now", "waiting"]);
  });

  it("marks a pull request closed without merge as failed", () => {
    expect(states({ pr: "closed" })[0]).toBe("failed");
  });
});
