import { existsSync, readFileSync } from "node:fs";

/**
 * Reads `KEY=value` lines (comments and empty lines skipped, no expansion, no shell): the file the all-in-one stack's bootstrap
 * writes with what homegraf needs (API keys, tokens). Surrounding quotes are removed.
 */
export function parseEnvFile(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq < 1) continue;
    const key = line.slice(0, eq).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue;
    let value = line.slice(eq + 1).trim();
    if (value.length >= 2 && ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'")))) value = value.slice(1, -1);
    out[key] = value;
  }
  return out;
}

/**
 * Fills the environment from the file named by HOMEGRAF_ENV_FILE, for variables that are not set or empty (what the
 * environment says explicitly wins). Without the variable or without the file nothing changes. Returns the names it set.
 */
export function applyEnvFile(env: NodeJS.ProcessEnv = process.env, read: (path: string) => string | undefined = (p) => (existsSync(p) ? readFileSync(p, "utf8") : undefined)): string[] {
  const path = env.HOMEGRAF_ENV_FILE;
  if (!path) return [];
  const text = read(path);
  if (text === undefined) return [];
  const applied: string[] = [];
  for (const [key, value] of Object.entries(parseEnvFile(text))) {
    if (value !== "" && (env[key] === undefined || env[key] === "")) {
      env[key] = value;
      applied.push(key);
    }
  }
  return applied;
}
