import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { AddonInfo, TemplateInfo } from "../shared/types";
import { firstRunNote, templateGroups } from "./templates";

type Props = {
  templates: TemplateInfo[];
  /** The first template: it is the app and fills the form. */
  value: string;
  /** The others: each is a service of its own next to it (<id>-<template>). */
  extras: string[];
  /** Start from this one alone ("" = my own image). */
  onPick: (id: string) => void;
  onAdd: (id: string) => void;
  onRemove: (id: string) => void;
  blocks: AddonInfo[];
  appId: string;
  /** Where the app is reached; the note of a further template comes from the tool, with its own address. */
  url: string;
  notes: { template: string; text: string }[];
};

/**
 * The templates by category as cards. One click starts from that template; "Add another template" puts a second
 * (third, ...) into the same compose file. Once something is chosen the list folds to the choice and its notes, so they are in view.
 */
export function TemplatePicker({ templates, value, extras, onPick, onAdd, onRemove, blocks, appId, url, notes }: Props) {
  const [mode, setMode] = useState<"closed" | "replace" | "add">("closed");
  const chosenIds = value ? [value, ...extras] : [];
  const chosen = chosenIds.map((id) => templates.find((t) => t.id === id)).filter((t): t is TemplateInfo => !!t);
  const folded = chosen.length > 0 && mode === "closed";
  const labelOf = (id: string) => blocks.find((b) => b.id === id)?.label ?? id;
  const pick = (id: string) => {
    if (mode === "add") onAdd(id);
    else onPick(id);
    setMode("closed");
  };
  const card = (id: string, title: string, text: string) => (
    <button
      key={id}
      type="button"
      className={`template-card${chosenIds.includes(id) || (!id && !value) ? " on" : ""}`}
      onClick={() => pick(id)}
      aria-pressed={chosenIds.includes(id) || (!id && !value)}
    >
      <strong>{title}</strong>
      <small>{text}</small>
    </button>
  );
  const hasSecrets = (t: TemplateInfo) => t.defaults.secretVars.length > 0 || (t.addons?.length ?? 0) > 0 || (t.sidecars?.length ?? 0) > 0;
  const own = (t: TemplateInfo) => [...(t.addons ?? []).map(labelOf), ...(t.sidecars ?? [])];
  const badges = (t: TemplateInfo) =>
    own(t).map((x) => (
      <Badge key={x} variant="outline" className="mr-1">
        {x}
      </Badge>
    ));
  const note = (t: TemplateInfo, first: boolean) => (first ? firstRunNote(t, appId, url) : (notes.find((n) => n.template === t.id)?.text ?? ""));

  return (
    <div className="template-picker">
      <h2>Start from</h2>
      {folded ? (
        <>
          {chosen.map((t, i) => (
            <div key={t.id} className="template-note">
              <div className="template-note-head">
                <strong>{t.label}</strong>
                <Badge variant="outline">{i === 0 ? "the app" : `service ${appId || "<id>"}-${t.id}`}</Badge>
                <Button type="button" variant="ghost" size="xs" className="ml-auto" aria-label={`Remove ${t.label}`} onClick={() => onRemove(t.id)}>
                  Remove
                </Button>
              </div>
              {i === 0 ? <>fills in image, port, volumes and environment</> : <>runs next to it on a port of its own</>}
              {own(t).length > 0 && <> with its own {badges(t)}</>}
              {hasSecrets(t) && (
                <>
                  . Passwords and keys are generated
                  {i === 0 ? (
                    ""
                  ) : (
                    <>
                      , named with the prefix <code>{t.id.toUpperCase().replace(/\W/g, "_")}_</code>
                    </>
                  )}
                </>
              )}
              .{t.firstRun && <p className="muted">After it runs: {note(t, i === 0) || "open its address once the app ID is typed"}</p>}
            </div>
          ))}
          <div className="template-actions">
            <Button type="button" variant="outline" size="sm" onClick={() => setMode("add")}>
              Add another template
            </Button>
            <Button type="button" variant="outline" size="sm" onClick={() => setMode("replace")}>
              Change template
            </Button>
          </div>
        </>
      ) : (
        <>
          {mode === "add" && (
            <p className="muted">
              It is put into the same compose file as <code>{appId || "<id>"}-&lt;template&gt;</code>, with building blocks, folders and secrets of its own. The
              routing (one domain) goes to the web interface you pick.
            </p>
          )}
          {mode !== "add" && <div className="template-grid">{card("", "My own image", "Type the image and fill in the rest yourself.")}</div>}
          {templateGroups(templates.filter((t) => mode !== "add" || !chosenIds.includes(t.id))).map((g) => (
            <section key={g.category}>
              <h3>{g.title}</h3>
              <div className="template-grid">{g.items.map((t) => card(t.id, t.label, t.description))}</div>
            </section>
          ))}
          {chosen.length > 0 && (
            <div className="template-actions">
              <Button type="button" variant="ghost" size="sm" onClick={() => setMode("closed")}>
                Cancel
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
