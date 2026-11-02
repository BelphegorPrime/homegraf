import { describe, expect, it } from "vitest";
import type { TemplateInfo, VolumeRow } from "../shared/types";
import { firstRunNote, templateGroups, unchosenPaths } from "./templates";

const defaults = (volumes: VolumeRow[] = []): TemplateInfo["defaults"] =>
  ({ image: "x:1", containerPort: 80, volumes, env: {}, networks: [], cpus: "", memory: "", gpu: "", secretVars: [] });
const t = (id: string, category?: string, extra: Partial<TemplateInfo> = {}): TemplateInfo =>
  ({ id, label: id, description: "d", healthPath: "/", category, defaults: defaults(), files: [], ...extra });
const bind = (source: string, target: string): VolumeRow => ({ kind: "bind", source, target, readOnly: false, size: "" });

describe("templateGroups", () => {
  it("orders the categories like the picker and the templates by name; an unknown or missing category goes last or to tools", () => {
    const groups = templateGroups([t("paperless", "documents"), t("nginx", "web server"), t("kuma"), t("odd", "zzz"), t("jellyfin", "media"), t("audiobookshelf", "media")]);
    expect(groups.map((g) => g.category)).toEqual(["web server", "media", "documents", "tools", "zzz"]);
    expect(groups[1].items.map((x) => x.id)).toEqual(["audiobookshelf", "jellyfin"]);
    expect(groups[0].title).toBe("Web servers");
  });
  it("is empty without templates", () => expect(templateGroups([])).toEqual([]));
});

describe("unchosenPaths", () => {
  const tpl = t("navidrome", "media", { hostPaths: ["/music"], defaults: defaults([bind("/srv/media/music", "/music")]) });
  it("reports a path whose volume still has the stand-in", () => expect(unchosenPaths(tpl, [bind("/srv/media/music", "/music")])).toEqual(["/music"]));
  it("is quiet once the folder was changed", () => expect(unchosenPaths(tpl, [bind("/tank/songs", "/music")])).toEqual([]));
  it("reports a path whose volume was removed", () => expect(unchosenPaths(tpl, [])).toEqual(["/music"]));
  it("is empty for a template without host paths, or no template", () => {
    expect(unchosenPaths(t("kuma"), [])).toEqual([]);
    expect(unchosenPaths(undefined, [])).toEqual([]);
  });
});

describe("firstRunNote", () => {
  it("fills in the address and the ID", () =>
    expect(firstRunNote(t("p", "documents", { firstRun: "Open __URL__; secrets get __APP__ KEY" }), "docs", "https://docs.lab.example")).toBe("Open https://docs.lab.example; secrets get docs KEY"));
  it("is empty without a note", () => expect(firstRunNote(t("p"), "docs", "u")).toBe(""));
});
