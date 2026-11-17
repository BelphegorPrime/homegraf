/** The methods the proxy allows for a public path; the tool takes the same list. */
export const PUBLIC_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"] as const;
export type PublicMethod = (typeof PUBLIC_METHODS)[number];

/** Where the requests may come from: everything, GitHub's webhook addresses, or networks the user lists. */
export const SOURCE_PRESETS = [
  { value: "any", label: "any address" },
  { value: "github-hooks", label: "GitHub webhooks" },
  { value: "custom", label: "custom networks" },
] as const;
export type SourcePreset = (typeof SOURCE_PRESETS)[number]["value"];

/** Comma or newline separated, trimmed, without repeats. */
export function parsePaths(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/[,\n]/)
        .map((p) => p.trim())
        .filter(Boolean),
    ),
  ];
}

/** What is wrong with the list of paths, or undefined when it can be sent. */
export function pathProblem(paths: string[]): string | undefined {
  if (paths.length === 0) return "add at least one path";
  if (paths.includes("/") || paths.includes("/*")) return "'/' would expose the whole host; list the paths to expose";
  const bad = paths.find((p) => !p.startsWith("/"));
  if (bad !== undefined) return `"${bad}" must start with "/", e.g. /hook/abc`;
  return undefined;
}

export interface PublicForm {
  paths: string;
  methods: readonly PublicMethod[];
  source: SourcePreset;
  cidrs: string;
}

/** The `public` object of the create/plan request; an empty source sends neither presets nor CIDRs. */
export function toPublicInput(form: PublicForm): { paths: string[]; methods?: PublicMethod[]; cidrs?: string[]; presets?: "github-hooks"[] } {
  const cidrs = parsePaths(form.cidrs);
  return {
    paths: parsePaths(form.paths),
    ...(form.methods.length > 0 ? { methods: [...form.methods] } : {}),
    ...(form.source === "github-hooks" ? { presets: ["github-hooks"] as "github-hooks"[] } : {}),
    ...(form.source === "custom" && cidrs.length > 0 ? { cidrs } : {}),
  };
}
