import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { AddonInfo, Capabilities, Graph, JobSnapshot, PlanResult, PreviewResult, TemplateInfo } from "../shared/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";
import { CredentialFields, useCredentials } from "./Credentials";
import { authHeaders, rememberUser } from "./api";
import { mergeFormChange } from "./composeMerge";
import { CreationProgress } from "./CreationProgress";
import { envComplete, EnvEditor, envObject, envRows, type EnvRow, networkComplete, NetworksEditor, sameNetworks, sameVolumes, volumeComplete, VolumesEditor } from "./ServiceOptions";
import type { NetworkRow, VolumeRow } from "../shared/types";

interface Issue { field: string; message: string }
type Preview = { state: "idle" | "loading" | "ok" | "error"; data?: PreviewResult; error?: string };

const APP_ID = /^[a-z0-9][a-z0-9-]{1,40}$/;
const num = (v: string) => (v.trim() === "" ? undefined : Number(v));
const text = (v: string) => (v.trim() === "" ? undefined : v.trim());
const json = (body: unknown) => ({ method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });

export function NewApp({ graph }: { graph: Graph }) {
  const servers = graph.nodes.filter((n) => n.type === "machine" && n.komodo).map((n) => n.label).sort();
  const [f, setF] = useState({
    appId: "", server: servers[0] ?? "", image: "", containerPort: "", hostPort: "", secretVars: "", cpus: "", memory: "", gpu: "",
    routing: true, domain: "", websockets: false, forceHttps: true, healthPath: "",
  });
  const [addons, setAddons] = useState<string[]>([]);
  const [volumes, setVolumes] = useState<VolumeRow[]>([]);
  const [networks, setNetworks] = useState<NetworkRow[]>([]);
  const [env, setEnv] = useState<EnvRow[]>([]);
  const [template, setTemplate] = useState("");                       // start from: nginx, httpd, haproxy
  const [fileEdits, setFileEdits] = useState<Record<string, string>>({});   // edited copies of the template's files
  const [tab, setTab] = useState("compose.yaml");
  // The compose file is the user's text. `base` is what the form generated last: a change of the form is merged into
  // the text as the difference between the old and the new generated file, so hand-made edits stay.
  const [compose, setComposeState] = useState("");
  const [base, setBaseState] = useState("");
  const composeRef = useRef("");
  const baseRef = useRef("");
  const baseId = useRef("");
  const setCompose = (v: string) => { composeRef.current = v; setComposeState(v); };
  const setBase = (v: string, id: string) => { baseRef.current = v; baseId.current = id; setBaseState(v); };
  const [mergeNote, setMergeNote] = useState<string | null>(null);
  const [settledTick, setSettledTick] = useState(0); // read the file again once the form's change has landed in it
  const mergeNoteRef = useRef<string | null>(null);
  mergeNoteRef.current = mergeNote;
  const [genError, setGenError] = useState<string | null>(null);
  const [pv, setPv] = useState<Preview>({ state: "idle" });
  const [secretValues, setSecretValues] = useState<Record<string, string>>({});
  const [webPort, setWebPort] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<PlanResult | null>(null);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [failure, setFailure] = useState<string | null>(null);
  const [copied, setCopied] = useState<"" | "command" | "compose">("");
  const caps = useQuery({ queryKey: ["capabilities"], queryFn: () => fetch("/api/capabilities").then((r) => r.json() as Promise<Capabilities>) });
  const templates = useQuery({ queryKey: ["templates"], queryFn: () => fetch("/api/templates").then((r) => (r.ok ? r.json() : []) as Promise<TemplateInfo[]>) });
  const blocks = useQuery({ queryKey: ["addons"], queryFn: () => fetch("/api/addons").then((r) => (r.ok ? r.json() : []) as Promise<AddonInfo[]>) });
  const client = useQueryClient();
  const cred = useCredentials();
  const [job, setJob] = useState<{ log: string; done?: JobSnapshot } | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const source = useRef<EventSource | null>(null);
  const genSeq = useRef(0);
  const anSeq = useRef(0);
  const formKeyRef = useRef("");
  const genPending = useRef(false); // a change of the form is not in the file yet
  const portIndex = useRef(0); // where the form's own port entry sits in the file's list
  useEffect(() => () => source.current?.close(), []);
  const set = <K extends keyof typeof f>(k: K, v: (typeof f)[K]) => setF((p) => ({ ...p, [k]: v }));
  const existing = graph.nodes.some((n) => n.type === "stack" && n.label === f.appId);
  const locked = existing; // a stack that exists brings its own compose file
  const edited = !existing && compose.trim() !== "" && compose !== base; // the user changed what the form generated
  const data = pv.state === "ok" ? pv.data : undefined;
  const secrets = data?.secrets ?? [];
  const ports = data?.ports ?? [];

  const composeText = (override: string | null | undefined) =>
    override === undefined ? (!existing && compose.trim() ? compose : undefined) : (override ?? undefined);
  // Only what is complete and valid is sent to the tool (and decides when the file is generated again); the rest is typing in progress.
  const cpusOk = /^\d+(\.\d+)?$/.test(f.cpus) ? f.cpus : "";
  const memoryOk = /^[1-9]\d*[kKmMgG]$/.test(f.memory) ? f.memory : "";
  const sentOptions = { volumes: volumes.filter(volumeComplete), networks: networks.filter(networkComplete), cpus: cpusOk, memory: memoryOk, gpu: f.gpu, env: envObject(env) };
  const webHost = ports.some((p) => String(p.host) === webPort) ? Number(webPort) : ports[0]?.host;
  // compose: undefined = the file as shown (what gets created), null = none (let the tool generate it from the form).
  const payload = (withSecrets = false, compose_: string | null | undefined = undefined) => ({
    appId: f.appId, server: f.server, routing: f.routing,
    // The container fields only matter while the tool generates the file from them.
    ...(existing ? {} : { image: text(f.image), containerPort: num(f.containerPort), hostPort: num(f.hostPort),
      secretVars: f.secretVars.split(/[\s,]+/).filter(Boolean), addons, ...sentOptions, template: template || undefined, files: fileEdits }),
    domain: text(f.domain), websockets: f.websockets, forceHttps: f.forceHttps, healthPath: text(f.healthPath),
    compose: composeText(compose_), webPort: f.routing && ports.length > 1 ? webHost : undefined,
    secretValues: withSecrets ? Object.fromEntries(secrets.filter((s) => secretValues[s]).map((s) => [s, secretValues[s]])) : undefined,
  });

  const formKey = JSON.stringify([f.appId, f.server, f.image, f.containerPort, f.hostPort, f.secretVars, addons, sentOptions]);
  formKeyRef.current = formKey;

  // Form -> file: generate the file for the form, and bring the difference to the previous one into the user's text.
  useEffect(() => {
    const n = ++genSeq.current;
    if (!APP_ID.test(f.appId) || !f.server || (!existing && !f.image.trim())) {
      // Nothing to generate from: an untouched file goes with its source, the user's own text stays.
      if (!existing && composeRef.current === baseRef.current) { setCompose(""); setBase("", ""); }
      setGenError(null);
      genPending.current = false;
      return;
    }
    genPending.current = true;
    const timer = setTimeout(async () => {
      try {
        const res = await fetch("/api/apps/preview", json(payload(false, null)));
        const body = await res.json();
        if (n !== genSeq.current) return;
        if (res.status === 400) return setGenError((body.issues ?? [])[0]?.message ?? body.error);
        if (!body.ok) return setGenError(body.error);
        setGenError(null);
        const generated = (body as PreviewResult).compose ?? "";
        const current = composeRef.current;
        if (existing || current.trim() === "" || current === baseRef.current) { // nothing of the user's to keep
          setCompose(generated); setBase(generated, f.appId); setMergeNote(null);
          return;
        }
        const merged = mergeFormChange(current, baseRef.current, generated, { oldId: baseId.current || f.appId, newId: f.appId });
        if (merged.ok) { setCompose(merged.text); setBase(generated, f.appId); setMergeNote(null); }
        else setMergeNote(merged.error); // base stays: the change is merged once the text parses again
      } catch (err) {
        if (n === genSeq.current) setGenError(err instanceof Error ? err.message : String(err));
      } finally {
        if (n === genSeq.current) { genPending.current = false; setSettledTick((x) => x + 1); }
      }
    }, 350);
    return () => clearTimeout(timer);
  }, [formKey, existing, mergeNote ? compose : ""]); // a held change is tried again whenever the text changes

  // File -> form: read what the text says (secrets, ports, and the fields it was generated from) and show it in the form.
  useEffect(() => {
    const n = ++anSeq.current;
    if ((!compose.trim() && !existing) || !APP_ID.test(f.appId) || !f.server) { setPv({ state: "idle" }); return; }
    const key = formKeyRef.current;
    const timer = setTimeout(async () => {
      setPv((p) => ({ ...p, state: "loading" }));
      try {
        const res = await fetch("/api/apps/preview", json(payload(false)));
        const body = await res.json();
        if (n !== anSeq.current) return;
        if (res.status === 400) return setPv({ state: "error", error: (body.issues ?? [])[0]?.message ?? body.error });
        if (!body.ok) return setPv({ state: "error", error: body.error });
        const ok = body as PreviewResult;
        setPv({ state: "ok", data: ok });
        // Only a file that differs from the form's own is a source; and not if the form moved on meanwhile.
        // ...and not while a change of the form is on its way into the file (or held back): it would be undone.
        const settled = !genPending.current && !mergeNoteRef.current;
        if (ok.form && settled && key === formKeyRef.current && (existing || composeRef.current !== baseRef.current)) {
          const v = ok.form;
          setF((p) => {
            // The form's port stays while the file still has it. If the user changed that entry in place, the entry
            // at the same position is the form's port now (they may have added ports of their own, in front).
            const at = v.ports.findIndex(([h, c]) => String(c) === p.containerPort && (p.hostPort === "" || String(h) === p.hostPort));
            if (at >= 0) portIndex.current = at;
            const port = at >= 0 ? v.ports[at] : (v.ports[portIndex.current] ?? v.ports[0]);
            const next = { image: v.image ?? "", secretVars: v.secretVars.join(", "), cpus: v.cpus, memory: v.memory, gpu: v.gpu,
              containerPort: port ? String(port[1]) : "", hostPort: port ? (at >= 0 ? p.hostPort : String(port[0])) : "" };
            return Object.entries(next).every(([k, val]) => p[k as keyof typeof next] === val) ? p : { ...p, ...next };
          });
          setAddons((a) => (a.length === v.addons.length && v.addons.every((x) => a.includes(x)) ? a : v.addons));
          // Rows still being typed (not complete yet) stay as they are; the rest follows the file.
          setVolumes((cur) => (sameVolumes(cur.filter(volumeComplete), v.volumes) ? cur : v.volumes));
          setNetworks((cur) => (sameNetworks(cur.filter(networkComplete), v.networks) ? cur : v.networks));
          setEnv((cur) => (JSON.stringify(Object.entries(envObject(cur)).sort()) === JSON.stringify(Object.entries(v.env).sort()) ? cur : envRows(v.env)));
        }
      } catch (err) {
        if (n === anSeq.current) setPv({ state: "error", error: err instanceof Error ? err.message : String(err) });
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [compose, existing, f.appId, f.server, settledTick, template, JSON.stringify(fileEdits)]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setCreateError(null); setIssues([]);
    const init = json(payload(true));
    const session = cred.session();
    const res = await fetch("/api/apps/create", { ...init, headers: { ...init.headers, ...authHeaders(session) } }).catch((err: unknown) => ({ ok: false, status: 0, json: async () => ({ error: String(err) }) }));
    const body = await res.json().catch(() => ({}));
    cred.spent(); // typed again for every action
    if (res.status !== 401 && !session.sso) rememberUser(session.name);
    setSecretValues({}); // typed values are not kept around
    if (res.status === 400) return setIssues(body.issues ?? []);
    if (!res.ok) return setCreateError(body.error ?? `HTTP ${res.status}`);
    setJob({ log: "" });
    source.current?.close();
    const es = new EventSource(`/api/apps/jobs/${body.jobId}/events`);
    source.current = es;
    es.addEventListener("log", (ev) => setJob((j) => ({ ...j, log: (j?.log ?? "") + JSON.parse((ev as MessageEvent).data) })));
    es.addEventListener("done", (ev) => { setJob((j) => ({ log: j?.log ?? "", done: JSON.parse((ev as MessageEvent).data) })); es.close(); void client.invalidateQueries({ queryKey: ["creations"] }); });
  }

  async function plan(e: React.FormEvent) {
    e.preventDefault();
    if (!locked && !compose.trim() && !f.image.trim()) {
      setResult(null); setJob(null);
      return setIssues([{ field: "image", message: "Required: this app has no compose file yet, so the tool writes one from the image" }]);
    }
    setBusy(true); setResult(null); setIssues([]); setFailure(null); setCopied(""); setJob(null); setCreateError(null);
    try {
      const res = await fetch("/api/apps/plan", json(payload()));
      const body = await res.json();
      if (res.status === 400) setIssues(body.issues ?? []);
      else if (!res.ok) setFailure(body.error ?? `HTTP ${res.status}`);
      else setResult(body as PlanResult);
    } catch (err) {
      setFailure(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const copy = (what: "command" | "compose", value: string) =>
    navigator.clipboard?.writeText(value).then(() => setCopied(what), () => undefined);
  const err = (field: string) => issues.find((i) => i.field === field)?.message;
  const field = (label: string, key: keyof typeof f, props: { placeholder?: string; hint?: string; locked?: boolean } = {}) => (
    <label className="field">
      <span>{label}</span>
      <Input value={String(f[key])} placeholder={props.placeholder} disabled={props.locked} onChange={(e) => set(key, e.target.value as never)} aria-invalid={!!err(key)} />
      {(err(key) ?? props.hint) && <small className={err(key) ? "error" : "muted"}>{err(key) ?? props.hint}</small>}
    </label>
  );
  const catalog = blocks.data ?? [];
  const labelOf = (id: string) => catalog.find((b) => b.id === id)?.label ?? id;
  const satisfied = (b: AddonInfo, chosen: string[]) => b.requiresAny.length === 0 || b.requiresAny.some((r) => chosen.includes(r));
  // The VPN stands alone: an app in its network cannot reach other services by name.
  const exclusiveIds = catalog.filter((b) => b.exclusive).map((b) => b.id);
  const clashes = (b: AddonInfo, chosen: string[]) =>
    b.exclusive ? chosen.some((x) => x !== b.id) : chosen.some((x) => exclusiveIds.includes(x));
  // A block that needs another one (a database UI needs a database) goes when the last one it needs is unticked.
  const toggle = (id: string) =>
    setAddons((a) => {
      const next = a.includes(id) ? a.filter((x) => x !== id) : [...a, id];
      return next.filter((x) => { const b = catalog.find((c) => c.id === x); return !b || satisfied(b, next); });
    });
  const files = data?.files ?? {};
  const fileNames = Object.keys(files);
  const activeTab = tab === "compose.yaml" || fileNames.includes(tab) ? tab : "compose.yaml";
  const fileText = (name: string) => fileEdits[name] ?? files[name] ?? "";
  // A template fills the form with its defaults (image, port, volumes); from there the form and the file work as usual.
  function applyTemplate(id: string) {
    setTemplate(id); setFileEdits({}); setTab("compose.yaml");
    const d = templates.data?.find((x) => x.id === id)?.defaults;
    if (!d) return;
    setF((p) => ({ ...p, image: d.image, containerPort: d.containerPort?.toString() ?? "", hostPort: "", cpus: d.cpus, memory: d.memory, gpu: d.gpu, secretVars: d.secretVars.join(", ") }));
    setVolumes(d.volumes); setNetworks(d.networks); setEnv(envRows(d.env));
  }
  const vpnOn = catalog.some((b) => b.vpn && addons.includes(b.id));
  const address = graph.nodes.find((n) => n.type === "machine" && n.komodo && n.label === f.server)?.address;

  return (
    <div className="list newapp">
      <CreationProgress />
      <div className="split">
      <form onSubmit={plan}>
        <h2>App</h2>
        <div className="grid">
          {field("App ID", "appId", { hint: "e.g. whoami-test. Names stacks/<id>, the Komodo stack and the inventory entry" })}
          <label className="field">
            <span>Server</span>
            <NativeSelect value={f.server} onChange={(e) => set("server", e.target.value)}>{servers.map((s) => <option key={s}>{s}</option>)}</NativeSelect>
          </label>
        </div>
        {!existing && (templates.data?.length ?? 0) > 0 && (
          <label className="field template-pick">
            <span>Start from</span>
            <NativeSelect value={template} onChange={(e) => applyTemplate(e.target.value)}>
              <option value="">my own image</option>
              {templates.data!.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
            </NativeSelect>
            <small className="muted">{templates.data!.find((x) => x.id === template)?.description ?? "Or pick a template: it fills in image, port and volumes, and brings the configuration files."}</small>
          </label>
        )}
        {existing && <p className="muted">A stack with this ID exists: the plan continues it, and the compose file in the repo is used (shown below, read-only).</p>}

        <h2>Container (only if stacks/&lt;id&gt;/compose.yaml does not exist yet)</h2>
        <p className="muted">The form and the compose file below follow each other: a change here updates only the matching lines of the file and keeps your own edits, and a change in the file shows up in these fields.</p>
        <div className="grid">
          {field(locked ? "Image" : "Image *", "image", { locked, hint: existing ? "not needed: the compose file in the repo is used" : "required, with a fixed tag, e.g. traefik/whoami:v1.11.0" })}
          {field("Container port", "containerPort", { locked, hint: "e.g. 80; empty: no port and no routing" })}
          {field("Host port", "hostPort", { locked, placeholder: "first free port", hint: "port on the server" })}
          {field("Secret variables", "secretVars", { locked, hint: "e.g. API_TOKEN, DB_PASSWORD; values are generated in Infisical; empty: none" })}
        </div>

        <h2>Volumes</h2>
        <VolumesEditor rows={volumes} onChange={setVolumes} disabled={locked} />
        <p className="muted">Data folder: <code>/srv/docker/&lt;id&gt;</code> on the server, the usual place for an app's data. Host path: a folder that already exists, e.g. a share. Docker volume: managed by Docker. Memory: a tmpfs, gone on restart. File from the repo: a path relative to <code>stacks/&lt;id&gt;</code>, delivered with every pull (and redeployed by hand when it changes).</p>

        <h2>Environment</h2>
        <EnvEditor rows={env} onChange={setEnv} disabled={locked} />
        <p className="muted">Plain values, written into the file. Passwords and keys belong in <em>Secret variables</em> above, they go to Infisical.</p>

        <h2>Networks</h2>
        <NetworksEditor rows={networks} onChange={setNetworks} disabled={locked || vpnOn}
          hint={vpnOn ? "Not with the VPN: the app is in the VPN's network." : "Besides the stack's own network (the app and its building blocks always share that one). An existing Docker network is one that another stack created, e.g. to reach its services by name."} />

        <h2>Resources</h2>
        <div className="grid">
          <label className="field"><span>CPU limit</span>
            <Input value={f.cpus} placeholder="no limit" disabled={locked} onChange={(e) => set("cpus", e.target.value)} aria-invalid={f.cpus !== "" && !cpusOk} />
            <small className={f.cpus !== "" && !cpusOk ? "error" : "muted"}>number of CPUs, e.g. 1.5</small></label>
          <label className="field"><span>Memory limit</span>
            <Input value={f.memory} placeholder="no limit" disabled={locked} onChange={(e) => set("memory", e.target.value)} aria-invalid={f.memory !== "" && !memoryOk} />
            <small className={f.memory !== "" && !memoryOk ? "error" : "muted"}>e.g. 512m or 2g</small></label>
          <label className="field"><span>GPU</span>
            <NativeSelect value={f.gpu} disabled={locked} onChange={(e) => set("gpu", e.target.value)}>
              <option value="">none</option>
              <option value="nvidia">NVIDIA (all GPUs)</option>
              <option value="dri">Intel / AMD (/dev/dri)</option>
            </NativeSelect>
            <small className="muted">NVIDIA needs the container toolkit on the server; /dev/dri passes the render device through</small></label>
        </div>

        <h2>Services next to the app</h2>
        <div className="addons">
          {catalog.map((b) => {
            const unmet = !satisfied(b, addons);
            const clash = clashes(b, addons) && !addons.includes(b.id);
            return (
              <label key={b.id} className={`addon${addons.includes(b.id) ? " on" : ""}${locked || unmet || clash ? " off" : ""}`}>
                <input type="checkbox" checked={addons.includes(b.id)} disabled={locked || ((unmet || clash) && !addons.includes(b.id))} onChange={() => toggle(b.id)} />
                <span>
                  <strong>{b.label}</strong> <code>{b.image}</code>
                  <small>{b.description}</small>
                  {unmet && <small className="muted">Needs {b.requiresAny.map(labelOf).join(" or ")} first.</small>}
                  {clash && <small className="muted">{b.exclusive ? "Cannot be combined with the other blocks: untick them first." : `Not together with ${exclusiveIds.map(labelOf).join(", ")}.`}</small>}
                </span>
              </label>
            );
          })}
          {blocks.data?.length === 0 && <span className="muted">No building blocks available.</span>}
        </div>
        {(data?.blockPorts ?? []).length > 0 && (
          <p className="muted">
            {(data?.blockPorts ?? []).map((p) => (
              <span key={p.service} className="portnote"><strong>{labelOf(p.service)}</strong>: {address ? <a href={`http://${address}:${p.host}${p.path}`} target="_blank" rel="noreferrer">http://{address}:{p.host}{p.path}</a> : `port ${p.host}`}</span>
            ))}
            {" "}open on the LAN once the app is deployed.
          </p>
        )}
        <p className="muted">Each one is added to the compose file with a healthcheck and a data directory; the app waits for it and gets a connection URL in its environment (rename it in the file to what the app expects). Tools such as Adminer are not waited for.</p>

        <h2>Routing</h2>
        <label className="check routing-toggle"><input type="checkbox" checked={f.routing} onChange={(e) => set("routing", e.target.checked)} /> HTTPS through NPM with a certificate from the homelab CA</label>
        {f.routing && (
          <div className="grid">
            {field("Domain", "domain", { placeholder: `${f.appId || "<id>"}.<your domain>` })}
            {field("Health path", "healthPath", { placeholder: "/" })}
            {ports.length > 1 && (
              <label className="field">
                <span>Web interface</span>
                <NativeSelect value={webHost} onChange={(e) => setWebPort(e.target.value)}>
                  {ports.map((p) => <option key={`${p.service}${p.host}`} value={p.host}>{p.service}: port {p.host} → {p.container}</option>)}
                </NativeSelect>
                <small className="muted">the compose file publishes several ports</small>
              </label>
            )}
          </div>
        )}
        {f.routing && (
          <div className="checks">
            <label className="check"><input type="checkbox" checked={f.websockets} onChange={(e) => set("websockets", e.target.checked)} /> WebSockets</label>
            <label className="check"><input type="checkbox" checked={f.forceHttps} onChange={(e) => set("forceHttps", e.target.checked)} /> Force HTTPS</label>
          </div>
        )}

        <div className="actions">
          <Button type="submit" disabled={busy || !f.appId || !f.server}>{busy ? "Planning…" : "Show plan"}</Button>
          <span className="muted">A dry run: nothing is created, changed or pushed.</span>
        </div>
      </form>
      <aside className="yaml-pane">
      <h2>
        compose.yaml {pv.state === "loading" && <span className="muted">updating…</span>}
        {edited && <Badge variant="outline" className="ml-2 border-warn text-warn">edited</Badge>}
      </h2>
      {pv.state === "idle" && !compose && <p className="muted">Enter an app ID{existing ? "" : " and an image"} to see the compose file.</p>}
      {fileNames.length > 0 && (
        <div className="tabs" role="tablist">
          {["compose.yaml", ...fileNames].map((n) => (
            <button key={n} type="button" role="tab" aria-selected={n === activeTab} className={`tab${n === activeTab ? " on" : ""}`} onClick={() => setTab(n)}>{n}{n !== "compose.yaml" && fileEdits[n] !== undefined ? " •" : ""}</button>
          ))}
        </div>
      )}
      {activeTab !== "compose.yaml" && (
        <>
          <Textarea className="editor" spellCheck={false} value={fileText(activeTab)} aria-label={activeTab}
            onChange={(e) => setFileEdits((x) => ({ ...x, [activeTab]: e.target.value }))} />
          <div className="actions tight">
            <span className="muted">Written to stacks/{f.appId || "<id>"}/{activeTab} with the app (template {template}); you can change it later in the repository.</span>
            {fileEdits[activeTab] !== undefined && <Button type="button" variant="outline" size="xs" onClick={() => setFileEdits((x) => Object.fromEntries(Object.entries(x).filter(([k]) => k !== activeTab)))}>Back to the template's</Button>}
          </div>
        </>
      )}
      {activeTab === "compose.yaml" && (compose || pv.state !== "idle") && (
        <>
          <Textarea
            className={`editor${(genError || pv.state === "error") && !edited ? " stale" : ""}`} spellCheck={false} value={compose} readOnly={existing}
            aria-label="compose.yaml" onChange={(e) => setCompose(e.target.value)}
          />
          <div className="actions tight">
            <span className={genError || mergeNote || pv.state === "error" ? "error" : "muted"}>
              {mergeNote
                ? `The file has a problem (${mergeNote}): changes of the form wait until it is fixed.`
                : genError ?? (pv.state === "error" ? pv.error : existing ? "From the repo, used as it is." : edited ? "Your edits are what gets created." : "Generated from the form; edit it to change anything.")}
            </span>
            {edited && <Button type="button" variant="outline" size="xs" onClick={() => { setCompose(base); setMergeNote(null); }}>Reset to generated</Button>}
          </div>
        </>
      )}

      {secrets.length > 0 && (
        <>
          <h2>Secrets in this file</h2>
          <p className="muted">Each <code>{"${VAR}"}</code> without a default is stored in Infisical. Leave a field empty to generate a random value{existing ? "; values that already exist in Infisical are kept" : ""}.</p>
          <div className="secrets">
            {secrets.map((s) => (
              <label key={s} className="field">
                <span><code>{s}</code></span>
                <Input type="password" autoComplete="off" placeholder="generated" value={secretValues[s] ?? ""} onChange={(e) => setSecretValues((v) => ({ ...v, [s]: e.target.value }))} />
              </label>
            ))}
          </div>
        </>
      )}
      </aside>
      </div>

      {failure && <p className="warning">{failure}</p>}
      {result && (
        <section className="plan">
          <h2>Plan <Badge variant="outline" className={result.ok ? "border-ok text-ok" : "border-down text-down"}>{result.ok ? "ready" : `failed (exit ${result.exitCode})`}</Badge></h2>
          <pre>{result.output}</pre>
          {result.ok && (
            <>
              <h2>Create</h2>
              {caps.data?.create.enabled ? (
                <form onSubmit={create} className="create">
                  <p className="muted">Creates the Komodo stack and the secrets in Infisical now and opens a pull request with the files; merging it deploys the app.</p>
                  <div className="actions">
                    <CredentialFields c={cred} className="contents" />
                    <Button type="submit" disabled={!cred.ready || (!!job && !job.done)}>Create pull request</Button>
                  </div>
                  {createError && <p className="warning">{createError}</p>}
                </form>
              ) : (
                <p className="muted">Creating from here is off{caps.data?.create.reason ? ` (${caps.data.create.reason})` : ""}. Run it yourself:</p>
              )}
              {job && (
                <div className="job">
                  <h2>Log {job.done ? <Badge variant="outline" className={job.done.state === "done" ? "border-ok text-ok" : "border-down text-down"}>{job.done.state === "done" ? "pull request open" : `failed (exit ${job.done.exitCode})`}</Badge> : <span className="muted">running…</span>}</h2>
                  <pre>{job.log}</pre>
                  {job.done?.pullRequestUrl && <p><a href={job.done.pullRequestUrl} target="_blank" rel="noreferrer">Open the pull request</a>, then merge it to deploy {job.done.appId}; the progress is listed at the top of this page.</p>}
                </div>
              )}
              <p className="muted">The same by hand{result.command.includes("--compose-file") ? " (save the compose file above as compose.yaml next to where you run it)" : ""}:</p>
              <div className="cmd">
                <code>{result.command}</code>
                <Button type="button" variant="outline" size="xs" onClick={() => copy("command", result.command)}>{copied === "command" ? "Copied" : "Copy"}</Button>
                {result.command.includes("--compose-file") && <Button type="button" variant="outline" size="xs" onClick={() => copy("compose", compose)}>{copied === "compose" ? "Copied" : "Copy compose"}</Button>}
              </div>
            </>
          )}
        </section>
      )}
    </div>
  );
}
