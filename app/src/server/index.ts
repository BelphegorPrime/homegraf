import { serve } from "@hono/node-server";
import { addonCatalog, hostCatalog, planner, previewer, removableCatalog, secretTool, stackCatalog, templateCatalog } from "./apps.js";
import { createOidc } from "./oidc.js";
import { rootCaSource } from "./rootCa.js";
import { certificateSource, tlsProbe } from "./certificates.js";
import { applier } from "./apply.js";
import { createApp } from "./app.js";
import { authenticator } from "./auth.js";
import { createProblem, loadConfig } from "./config.js";
import { applyEnvFile } from "./envfile.js";
import { graphLoader } from "./graph.js";
import { unifiSource } from "./unifi.js";
import { pullStateReader, removalStore } from "./removals.js";
import { creationStore, creationTracker, stackStateReader } from "./creations.js";
import { statusPoller } from "./status.js";
import { userStore } from "./users.js";

// The all-in-one stack's bootstrap hands over what homegraf needs in a file (HOMEGRAF_ENV_FILE); the environment wins.
const fromFile = applyEnvFile();
if (fromFile.length) console.log(`environment from ${process.env.HOMEGRAF_ENV_FILE}: ${fromFile.join(", ")}`);
const cfg = loadConfig();
const unifi = unifiSource({ cfg: cfg.unifi, repoDir: cfg.repoDir });
const loadGraph = graphLoader(cfg.repoDir, cfg.graphTtlMs, Date.now, unifi.args);
const status = statusPoller(cfg, loadGraph);
status.start();

const certificates = certificateSource({ domains: async () => (await loadGraph()).nodes.filter((n) => n.type === "domain").map((n) => n.label) });
const create = { problem: createProblem(cfg.create, cfg.komodo), applier: applier({ cfg }) };
const store = userStore(`${cfg.dataDir}/users.json`);
if (store.problem) console.error(`users: ${store.problem}`);
const users = { enabled: !!cfg.create.password, authenticate: authenticator({ adminPassword: cfg.create.password, users: store }), store };
const pullState = pullStateReader(cfg);
const sso = { oidc: cfg.oidc ? createOidc({ cfg: cfg.oidc, users: store }) : undefined, problem: cfg.oidcProblem };
if (sso.problem) console.error(`sso: ${sso.problem}`);
const removal = { apps: removableCatalog(cfg.repoDir), store: removalStore(`${cfg.dataDir}/removals.json`), pullState };
if (removal.store.problem) console.error(`removals: ${removal.store.problem}`);
const creationsStore = creationStore(`${cfg.dataDir}/creations.json`);
if (creationsStore.problem) console.error(`creations: ${creationsStore.problem}`);
const creations = { store: creationsStore, tracker: creationTracker({ store: creationsStore, pullState, stackState: stackStateReader(cfg), probe: tlsProbe({ timeoutMs: 4000 }) }) };
const hosts = { list: hostCatalog(cfg.repoDir), rootCa: rootCaSource(cfg.repoDir) };
const stacks = {
  list: stackCatalog(cfg.repoDir),
  secretTool: secretTool(cfg.repoDir, {
    INFISICAL_URL: cfg.create.infisical?.url ?? "", INFISICAL_BOOTSTRAP_CLIENT_ID: cfg.create.infisical?.clientId ?? "",
    INFISICAL_BOOTSTRAP_CLIENT_SECRET: cfg.create.infisical?.clientSecret ?? "",
  }),
};

serve({ fetch: createApp({ loadGraph, status, webDir: cfg.webDir, plan: planner(cfg.repoDir), preview: previewer(cfg.repoDir), addons: addonCatalog(cfg.repoDir), templates: templateCatalog(cfg.repoDir), unifi: unifi.status, certificates, create, users, removal, creations, hosts, stacks, sso }).fetch, port: cfg.port }, (info) => {
  console.log(`homegraf listening on :${info.port} (repo ${cfg.repoDir}, status ${cfg.komodo ? "komodo" : "disabled"}, create ${createProblem(cfg.create, cfg.komodo) ?? "enabled"}, users ${users.enabled ? `${store.list().length} + admin` : "off"}, sso ${cfg.oidc ? cfg.oidc.label : "off"}, unifi ${cfg.unifi ? "on" : "off"})`);
});
