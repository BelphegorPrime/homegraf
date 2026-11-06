import { isMap, isScalar, isSeq, parse, parseDocument, type Document, type YAMLMap, type YAMLSeq } from "yaml";

type Path = (string | number)[];
type Json = unknown;

/** One change between two generated compose files, applied to the user's text where it belongs and nowhere else. */
export type Op =
  | { kind: "set"; path: Path; value: Json }
  | { kind: "delete"; path: Path }
  | { kind: "listAdd"; path: Path; item: Json }
  | { kind: "listRemove"; path: Path; item: Json }
  | { kind: "rename"; path: Path; to: string };

const isObject = (v: Json): v is Record<string, Json> => typeof v === "object" && v !== null && !Array.isArray(v);
const isScalarValue = (v: Json) => v === null || ["string", "number", "boolean"].includes(typeof v);
const same = (a: Json, b: Json) => JSON.stringify(a) === JSON.stringify(b);

/**
 * What the form takes away when it drops a key. Inside a service only its own entries go (a generated `environment:`
 * may hold the user's variables too); a whole service goes as one.
 */
function removals(value: Json, path: Path): Op[] {
  // The top-level networks: and volumes: sections are shared with the user's own entries, like a service's own sections.
  const section = path.length === 1 && (path[0] === "networks" || path[0] === "volumes");
  if ((path.length >= 3 || section) && isObject(value)) return Object.entries(value).flatMap(([k, v]) => removals(v, [...path, k]));
  if (path.length >= 3 && Array.isArray(value) && value.every(isScalarValue)) return value.map((item): Op => ({ kind: "listRemove", path, item }));
  return [{ kind: "delete", path }];
}

/** Differences from `before` to `after`: map keys are compared one by one, lists of scalars item by item. */
export function diff(before: Json, after: Json, path: Path = []): Op[] {
  if (isObject(before) && isObject(after)) {
    const ops: Op[] = [];
    for (const key of Object.keys(before)) if (!(key in after)) ops.push(...removals(before[key], [...path, key]));
    for (const key of Object.keys(after)) {
      if (!(key in before)) ops.push({ kind: "set", path: [...path, key], value: after[key] });
      else ops.push(...diff(before[key], after[key], [...path, key]));
    }
    return ops;
  }
  if (Array.isArray(before) && Array.isArray(after) && before.every(isScalarValue) && after.every(isScalarValue)) {
    const had = new Set(before.map(String)),
      has = new Set(after.map(String));
    return [
      ...before.filter((x) => !has.has(String(x))).map((item): Op => ({ kind: "listRemove", path, item })),
      ...after.filter((x) => !had.has(String(x))).map((item): Op => ({ kind: "listAdd", path, item })),
    ];
  }
  return same(before, after) ? [] : [{ kind: "set", path, value: after }];
}

/** The changes of the form: the app's own service may change its name, everything else is compared as it is. */
export function formChanges(before: Json, after: Json, ids: { oldId: string; newId: string }): Op[] {
  const old = isObject(before) ? before : {};
  const services = isObject(old.services) ? old.services : {};
  const renamed = ids.oldId !== ids.newId && ids.oldId in services && !(ids.newId in services);
  if (!renamed) return diff(before, after);
  const moved = Object.fromEntries(Object.entries(services).map(([k, v]) => [k === ids.oldId ? ids.newId : k, v]));
  return [{ kind: "rename", path: ["services", ids.oldId], to: ids.newId }, ...diff({ ...old, services: moved }, after)];
}

const nodeAt = (doc: Document, path: Path) => doc.getIn(path, true);

/** Removes a collection that a delete left empty (an `environment:` without entries), up to the top level. */
function pruneEmpty(doc: Document, path: Path) {
  for (let p = path.slice(0, -1); p.length > 0 && !(p.length === 1 && p[0] === "services"); p = p.slice(0, -1)) {
    const node = nodeAt(doc, p);
    if ((isMap(node) || isSeq(node)) && node.items.length === 0) doc.deleteIn(p);
    else break;
  }
}

/**
 * Puts a value the form added. Where the user's text already has a map or a list of scalars at that place (their own
 * `environment:`, their own `ports:`), the new entries are added to it instead of replacing it.
 */
function put(doc: Document, path: Path, value: Json) {
  const node = nodeAt(doc, path);
  if (isMap(node) && isObject(value)) {
    for (const [key, v] of Object.entries(value)) put(doc, [...path, key], v);
  } else if (isSeq(node) && Array.isArray(value) && value.every(isScalarValue)) {
    for (const item of value) apply(doc, { kind: "listAdd", path, item });
  } else {
    doc.setIn(path, doc.createNode(value));
  }
}

function apply(doc: Document, op: Op) {
  switch (op.kind) {
    case "set":
      put(doc, op.path, op.value);
      break;
    case "delete":
      if (doc.hasIn(op.path)) {
        doc.deleteIn(op.path);
        pruneEmpty(doc, op.path);
      }
      break;
    case "rename": {
      const parent = nodeAt(doc, op.path.slice(0, -1)) as YAMLMap<unknown, unknown> | undefined;
      const pair = parent?.items.find((p) => isScalar(p.key) && p.key.value === op.path[op.path.length - 1]);
      if (pair && isScalar(pair.key)) pair.key.value = op.to; // keeps the comments around the key
      break;
    }
    case "listAdd": {
      const seq = nodeAt(doc, op.path);
      if (!isSeq(seq)) doc.setIn(op.path, doc.createNode([op.item]));
      else if (!(seq as YAMLSeq<unknown>).items.some((i) => String(isScalar(i) ? i.value : i) === String(op.item)))
        (seq as YAMLSeq<unknown>).add(doc.createNode(op.item));
      break;
    }
    case "listRemove": {
      const seq = nodeAt(doc, op.path);
      if (!isSeq(seq)) break;
      const at = (seq as YAMLSeq<unknown>).items.findIndex((i) => String(isScalar(i) ? i.value : i) === String(op.item));
      if (at >= 0) (seq as YAMLSeq<unknown>).delete(at);
      if ((seq as YAMLSeq<unknown>).items.length === 0) doc.deleteIn(op.path);
      break;
    }
  }
}

export type Merge = { ok: true; text: string; changed: boolean } | { ok: false; error: string };

/**
 * Brings a change of the form into the user's compose text: `before` and `after` are the files the form generated
 * for its previous and its new state, `text` is what the user has now (the generated file plus their own edits).
 * Only the places the form changed are touched; comments, extra services, extra keys and layout stay as they are.
 */
export function mergeFormChange(text: string, before: string, after: string, ids: { oldId: string; newId: string }): Merge {
  let ops: Op[];
  try {
    ops = formChanges(parse(before), parse(after), ids);
  } catch (err) {
    return { ok: false, error: `generated file not readable: ${err instanceof Error ? err.message : err}` };
  }
  if (ops.length === 0) return { ok: true, text, changed: false };
  const doc = parseDocument(text);
  if (doc.errors.length > 0) return { ok: false, error: doc.errors[0].message.split("\n")[0] };
  if (!isMap(doc.contents) || !isMap(doc.get("services", true))) return { ok: false, error: "the file has no `services:` mapping" };
  const before_ = JSON.stringify(doc.toJS());
  for (const op of [...ops].sort((a, b) => Number(b.kind === "rename") - Number(a.kind === "rename"))) apply(doc, op);
  // The user already had it their way: their text stays byte for byte (writing it again would normalise its layout).
  if (JSON.stringify(doc.toJS()) === before_) return { ok: true, text, changed: false };
  return { ok: true, text: doc.toString({ lineWidth: 0 }), changed: true };
}
