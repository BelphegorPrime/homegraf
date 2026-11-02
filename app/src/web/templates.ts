import type { TemplateInfo, VolumeRow } from "../shared/types";

/** The order of the picker; a category it does not know comes last. */
const ORDER = ["web server", "media", "documents", "productivity", "ai", "tools"];
export const CATEGORY_TITLES: Record<string, string> = {
  "web server": "Web servers", media: "Media", documents: "Documents", productivity: "Productivity", ai: "AI", tools: "Tools",
};

/** The templates by category, in the order of the picker and by name inside a category. */
export function templateGroups(all: TemplateInfo[]): { category: string; title: string; items: TemplateInfo[] }[] {
  const by = new Map<string, TemplateInfo[]>();
  for (const t of all) by.set(t.category ?? "tools", [...(by.get(t.category ?? "tools") ?? []), t]);
  const rank = (c: string) => (ORDER.includes(c) ? ORDER.indexOf(c) : ORDER.length);
  return [...by.entries()]
    .sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
    .map(([category, items]) => ({ category, title: CATEGORY_TITLES[category] ?? category, items: [...items].sort((a, b) => a.label.localeCompare(b.label)) }));
}

/** The container paths of the template's hostPaths whose volume still points at the template's stand-in folder. */
export function unchosenPaths(template: TemplateInfo | undefined, rows: VolumeRow[]): string[] {
  const standIn = new Map((template?.defaults.volumes ?? []).map((v) => [v.target, v.source]));
  return (template?.hostPaths ?? []).filter((target) => {
    const row = rows.find((r) => r.target === target);
    return !row || row.source === standIn.get(target);
  });
}

/** The text of a first-run note with its placeholders filled in (the app's address and ID). */
export function firstRunNote(template: TemplateInfo | undefined, appId: string, url: string): string {
  return (template?.firstRun ?? "").replaceAll("__URL__", url).replaceAll("__APP__", appId || "<id>");
}
