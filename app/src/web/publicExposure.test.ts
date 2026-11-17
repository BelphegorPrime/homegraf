import { describe, expect, it } from "vitest";
import { parsePaths, pathProblem, toPublicInput } from "./publicExposure";

describe("pathProblem", () => {
  it("refuses '/' alone and in a list", () => {
    expect(pathProblem(["/"])).toBe("'/' would expose the whole host; list the paths to expose");
    expect(pathProblem(["/hook", "/"])).toBe("'/' would expose the whole host; list the paths to expose");
  });

  it("refuses '/*'", () => expect(pathProblem(["/*"])).toBe("'/' would expose the whole host; list the paths to expose"));

  it("accepts listed paths", () => {
    expect(pathProblem(["/hook/abc", "/files/*"])).toBeUndefined();
  });

  it("asks for a path when the list is empty", () => expect(pathProblem([])).toBe("add at least one path"));

  it("asks for a leading slash", () => expect(pathProblem(["/hook", "files"])).toMatch(/must start with "\/"/));
});

describe("parsePaths", () => {
  it("splits on commas and newlines, trims and drops repeats", () => {
    expect(parsePaths(" /hook/abc, /files/*\n/hook/abc , \n /other ")).toEqual(["/hook/abc", "/files/*", "/other"]);
  });
});

describe("toPublicInput", () => {
  const form = { paths: "/hook/abc, /files/*", methods: ["GET", "HEAD"] as const, cidrs: "", source: "any" as const };

  it("sends paths and methods for any address", () => {
    expect(toPublicInput(form)).toEqual({ paths: ["/hook/abc", "/files/*"], methods: ["GET", "HEAD"] });
  });

  it("sends the GitHub preset", () => {
    expect(toPublicInput({ ...form, methods: [], source: "github-hooks" })).toEqual({ paths: ["/hook/abc", "/files/*"], presets: ["github-hooks"] });
  });

  it("sends the custom networks", () => {
    expect(toPublicInput({ ...form, source: "custom", cidrs: "203.0.113.0/24\n2001:db8::/32" })).toEqual({
      paths: ["/hook/abc", "/files/*"],
      methods: ["GET", "HEAD"],
      cidrs: ["203.0.113.0/24", "2001:db8::/32"],
    });
  });

  it("sends neither preset nor CIDRs for custom without networks", () => {
    expect(toPublicInput({ ...form, source: "custom", cidrs: " " })).toEqual({ paths: ["/hook/abc", "/files/*"], methods: ["GET", "HEAD"] });
  });
});
