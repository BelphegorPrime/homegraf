export type NodeType =
  | "isp" | "router" | "switch" | "device" | "machine" | "stack" | "container" | "service" | "domain" | "infra";

export interface GraphNode {
  id: string;
  type: NodeType;
  label: string;
  server?: string;
  stack?: string;
  address?: string;
  image?: string;
  images?: string[];
  urls?: string[];
  /** Subset of urls that come from hand-made NPM hosts (inventory/npm-hosts.yaml). */
  manualUrls?: string[];
  manual?: boolean;
  disabled?: boolean;
  url?: string;
  category?: string;
  kind?: string;
  secrets?: string;
  placeholder?: boolean;
  /** Machine that is a Komodo server (a valid target for new apps). */
  komodo?: boolean;
  external?: boolean;
  target?: string;
  [extra: string]: unknown;
}

export type EdgeType =
  | "link" | "hosts" | "contains" | "proxies" | "terminated-by" | "depends-on" | "deployed-by" | "uses-secrets"
  | "runs" | "certificates-from";

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type: EdgeType;
  label?: string;
  domains?: string[];
  inferred?: boolean;
  via?: string[];
}

export interface Graph {
  generated: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  warnings: string[];
  /** Live sources behind the network layer (UniFi); without them the placeholders of inventory/network.yaml show. */
  sources?: { unifi?: { fetched?: string } };
}

export type Health = "ok" | "warn" | "down" | "unknown";

export interface NodeStatus {
  health: Health;
  detail?: string;
  updateAvailable?: boolean;
}

export interface StatusSnapshot {
  updated: string | null;
  source: "komodo" | "disabled" | "error";
  error?: string;
  nodes: Record<string, NodeStatus>;
}

export interface PlanResult {
  ok: boolean;
  exitCode: number;
  output: string;
  /** The same invocation, with --yes, to run by hand. */
  command: string;
}

/** What the UI may offer: creating apps needs a password and credentials in the container's environment. */
export interface Capabilities {
  create: { enabled: boolean; reason?: string };
  /** Signing in and managing users; off without HOMEGRAF_PASSWORD (the built-in admin). */
  users: { enabled: boolean; reason?: string };
  /** The UniFi controller behind the network layer of the map. */
  unifi: { enabled: boolean; fetched?: string; error?: string };
}

/** The certificate a domain serves, as a client sees it (read over TLS, not from the proxy's database). */
export interface CertificateInfo {
  domain: string;
  /** Set when the connection or the handshake failed; the fields below are then missing. */
  error?: string;
  issuer?: string;
  /** ISO time the certificate stops being valid. */
  notAfter?: string;
  /** Whole days until then, negative once expired. */
  daysLeft?: number;
  /** Names the certificate covers. */
  names?: string[];
  /** The certificate covers the domain that was asked for. */
  matches?: boolean;
  fingerprint?: string;
}

export interface CertificatesSnapshot {
  /** When the domains were last probed; null before the first run. */
  updated: string | null;
  certificates: CertificateInfo[];
}

/** What a person may do. Each write action of the app names the permission it needs. */
export const PERMISSIONS = {
  "apps:create": "Create apps (opens a pull request)",
  "apps:remove": "Remove apps (destroys the stack, opens a pull request, then removes routing and secrets)",
  "hosts:edit": "Change a host's routing options, e.g. redirect HTTP to HTTPS (opens a pull request)",
  "stacks:deploy": "Redeploy a stack (pull and up through Komodo)",
  "secrets:manage": "See the names of a stack's secrets, set, generate and delete them",
  "users:manage": "Manage users and their permissions",
} as const;
export type Permission = keyof typeof PERMISSIONS;

export interface UserInfo {
  name: string;
  permissions: Permission[];
  created?: string;
  /** Signs in through the identity provider, has no password. */
  sso?: boolean;
  /** The administrator whose password is in the environment: always there, not editable. */
  builtin?: boolean;
}

/** create = new app (pull request), remove = first half of a removal (stack gone, pull request), finish = second half, deploy = redeploy of a stack, host = a change to a host's routing options (pull request). */
export type JobKind = "create" | "remove" | "finish" | "deploy" | "host";

export interface JobSnapshot {
  id: string;
  kind: JobKind;
  appId: string;
  /** Who started it. */
  requestedBy?: string;
  state: "running" | "done" | "failed";
  exitCode?: number;
  /** URL of the Forgejo pull request, once the tool printed it. */
  pullRequestUrl?: string;
  log: string;
}

export interface AddonInfo {
  id: string;
  label: string;
  description: string;
  secrets: string[];
  image: string;
  /** A tool for people (database UI): the app does not wait for it. */
  ui: boolean;
  /** Needs at least one of these blocks next to it. */
  requiresAny: string[];
  /** Publishes a port on the server (a free one is picked). */
  publishesPort: boolean;
  /** Where its web page starts. */
  urlPath: string;
  /** Cannot be combined with other blocks (the VPN). */
  exclusive: boolean;
  /** The app runs in this block's network (the VPN). */
  vpn: boolean;
}

/** What `new-app --print-compose` finds: the compose file the app would get, and the secrets and ports in it. */
export interface PreviewResult {
  ok: boolean;
  error?: string;
  compose?: string;
  /** The compose file already exists in the repo: it is used as it is and cannot be edited here. */
  existing?: boolean;
  secrets?: string[];
  /** The template's files this compose file mounts from the repo (path -> text), with the edits applied. */
  files?: Record<string, string>;
  /** Folders of the templates (also the further ones) that still have the template's stand-in as source. */
  standIn?: { service: string; path: string }[];
  /** What to do once each further template runs, filled in for its own service and address. */
  notes?: { template: string; text: string }[];
  ports?: { service: string; host: number; container: number }[];
  /** Ports the building blocks publish (UIs, S3 console); not candidates for the app's domain. */
  blockPorts?: { service: string; host: number; container: number; path: string }[];
  /** The form's values as far as they can be read back from the compose file (also an edited one). */
  form?: {
    image: string | null;
    containerPort: number | null;
    hostPort: number | null;
    /** Every port the app's own service publishes as [host, container]. */
    ports: [number, number][];
    mount: string | null;
    secretVars: string[];
    addons: string[];
    volumes: VolumeRow[];
    networks: NetworkRow[];
    cpus: string;
    memory: string;
    gpu: "" | "nvidia" | "dri";
    /** Plain environment variables (not the secrets, not the ones a building block adds). */
    env: Record<string, string>;
  };
}

export interface VolumeRow {
  kind: "data" | "bind" | "named" | "tmpfs" | "repo";
  /** data: folder below /srv/docker/<id> (empty = the folder itself); bind: host path; named: volume name; repo: ./path. */
  source: string;
  target: string;
  readOnly: boolean;
  /** tmpfs only. */
  size: string;
}

export interface NetworkRow {
  name: string;
  /** An existing Docker network, created elsewhere. */
  external: boolean;
}

/** A template for a new app (a web server, or a ready-made app such as Mealie): defaults for the form and what comes with it.
 * The fields after `healthPath` come from newer versions of the tool; an older one does not send them. */
export interface TemplateInfo {
  id: string;
  label: string;
  description: string;
  healthPath: string;
  category?: string;
  /** Building blocks that come with it (postgres, redis, ...). */
  addons?: string[];
  websockets?: boolean;
  /** HTTP status codes that count as up without login. */
  statuses?: number[];
  /** Container paths whose host folder has to be chosen: the volume's source is a stand-in. */
  hostPaths?: string[];
  /** What to do once it runs. */
  firstRun?: string;
  /** Extra services of its own (a converter, a database with a special image). */
  sidecars?: string[];
  defaults: {
    image: string;
    containerPort: number | null;
    volumes: VolumeRow[];
    env: Record<string, string>;
    networks: NetworkRow[];
    cpus: string;
    memory: string;
    gpu: "" | "nvidia" | "dri";
    secretVars: string[];
  };
  files: string[];
}

/** An app the tool may remove (never the platform stacks), with what removing it touches. */
export interface RemovableApp {
  id: string;
  server: string;
  komodoStack: string;
  /** The Infisical folder is deleted with it. */
  secrets: boolean;
  /** Domains of its routing: the NPM host and certificate are removed. */
  domains: string[];
  /** Host paths it mounts: left alone, listed for the operator. */
  dataPaths: string[];
}

/** First half done (stack gone, pull request opened); routing and secrets wait for the merge. */
export interface PendingRemoval {
  appId: string;
  server: string;
  pullRequestUrl: string;
  requestedBy?: string;
  at: string;
  /** Of the pull request, read from Forgejo; unknown when Forgejo cannot be reached. */
  pr: "open" | "merged" | "closed" | "unknown";
}

/** An app whose pull request is open (or merged and on its way): the page follows it until the app answers over HTTPS. */
export interface PendingCreation {
  appId: string;
  server: string;
  pullRequestUrl: string;
  /** The app's domain; none without routing. */
  domain?: string;
  requestedBy?: string;
  at: string;
  /** Of the pull request, read from Forgejo; unknown when Forgejo cannot be reached. */
  pr: "open" | "merged" | "closed" | "unknown";
  /** Komodo's view of the stack, asked once the pull request is merged. */
  stack?: { health: Health; detail?: string };
  /** Whether the domain serves a certificate for itself, asked once the stack runs. */
  https?: { ok: boolean; detail?: string };
  /** Every step is through: merged, running and, with a domain, answering over HTTPS. */
  done: boolean;
}

/** A stack the UI may redeploy and manage the secrets of (never the platform stacks). */
export interface ManagedStack {
  id: string;
  server: string;
  komodoStack: string;
  /** Its secrets live in Infisical. */
  secrets: boolean;
}

/** A secret of a stack: the name only, values are never sent to the page. */
export interface SecretKey {
  key: string;
  /** A compose file of the stack uses ${KEY}. */
  used: boolean;
}

/** A host ingressctl manages (a service in external/ingress.yaml or stacks/<id>/ingress.yaml). */
export interface HostInfo {
  id: string;
  stack: string;
  /** The manifest that declares it, relative to the repository. */
  file: string;
  domains: string[];
  /** NPM redirects HTTP to HTTPS for it. */
  forceHttps: boolean;
}

/** The certificates of the homelab CA, for download and for checking what was downloaded. */
export interface RootCaInfo {
  certificates: { subject: string; issuer: string; notAfter: string; fingerprint: string; root: boolean }[];
}

/** Whether signing in through an identity provider is set up, and who is signed in through it right now (cookie session). */
export interface SsoSession {
  enabled: boolean;
  /** Name of the provider for the button. */
  label?: string;
  /** Why it is off although some of its settings are there. */
  problem?: string;
  user?: { name: string; permissions: Permission[]; /** Sent back as X-CSRF-Token with every write request. */ csrf: string };
}
