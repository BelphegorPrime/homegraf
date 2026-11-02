export interface Config {
  port: number;
  repoDir: string;
  webDir: string;
  graphTtlMs: number;
  statusIntervalMs: number;
  komodo?: { url: string; key: string; secret: string };
  unifi?: UnifiConfig;
  create: CreateConfig;
  /** Where the users file lives (a volume). */
  dataDir: string;
  /** Signing in through an OpenID Connect provider (Forgejo, Keycloak, Authentik, …); off unless all four settings are there. */
  oidc?: OidcConfig;
  /** Some OIDC settings are there but not all. */
  oidcProblem?: string;
}

export interface OidcConfig {
  issuer: string;
  clientId: string;
  clientSecret: string;
  /** The address the browser reaches this app at (the callback is `<publicUrl>/api/auth/callback`). */
  publicUrl: string;
  scopes: string;
  /** The claim that holds the user name; it names the entry in the Users tab. */
  usernameClaim: string;
  label: string;
  sessionHours: number;
}

/** The local UniFi controller, read with a read-only account (infra/graph/unifi.py). */
export interface UnifiConfig {
  url: string;
  user: string;
  password: string;
  /** Verify the controller's certificate (it is self-signed, so off unless asked for). */
  verify?: boolean;
  /** SHA-256 of the controller's certificate: pins it instead. */
  fingerprint?: string;
}

/** Creating apps from the UI: each field comes from the environment; without all of them the feature is off. */
export interface CreateConfig {
  password?: string;
  workDir: string;
  cloneUrl?: string;
  forgejoUrl?: string;
  forgejoToken?: string;
  infisical?: { url: string; clientId: string; clientSecret: string };
}

/** Why creating is unavailable, or undefined when everything is set. */
export function createProblem(c: CreateConfig, komodo: Config["komodo"]): string | undefined {
  const missing = [
    ["HOMEGRAF_PASSWORD", c.password],
    ["HOMEGRAF_CLONE_URL", c.cloneUrl],
    ["FORGEJO_TOKEN", c.forgejoToken],
    ["INFISICAL_URL / INFISICAL_BOOTSTRAP_CLIENT_ID / INFISICAL_BOOTSTRAP_CLIENT_SECRET", c.infisical],
    ["KOMODO_URL / KOMODO_API_KEY / KOMODO_API_SECRET", komodo],
  ].filter(([, v]) => !v).map(([k]) => k);
  return missing.length ? `not configured: ${missing.join(", ")}` : undefined;
}

function oidcFrom(env: NodeJS.ProcessEnv): Pick<Config, "oidc" | "oidcProblem"> {
  const need = [["OIDC_ISSUER", env.OIDC_ISSUER], ["OIDC_CLIENT_ID", env.OIDC_CLIENT_ID], ["OIDC_CLIENT_SECRET", env.OIDC_CLIENT_SECRET], ["HOMEGRAF_URL", env.HOMEGRAF_URL]] as const;
  const missing = need.filter(([, v]) => !v).map(([k]) => k);
  if (missing.length === need.length) return {};
  if (missing.length) return { oidcProblem: `single sign-on needs ${missing.join(", ")} as well` };
  const issuer = env.OIDC_ISSUER!.replace(/\/$/, "");
  const hours = Number(env.OIDC_SESSION_HOURS ?? 8);
  return {
    oidc: {
      issuer, clientId: env.OIDC_CLIENT_ID!, clientSecret: env.OIDC_CLIENT_SECRET!, publicUrl: env.HOMEGRAF_URL!.replace(/\/$/, ""),
      scopes: env.OIDC_SCOPES || "openid profile", usernameClaim: env.OIDC_USERNAME_CLAIM || "preferred_username",
      label: env.OIDC_LABEL || (URL.canParse(issuer) ? new URL(issuer).hostname : "single sign-on"),
      sessionHours: Number.isFinite(hours) && hours > 0 ? hours : 8,
    },
  };
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const komodo = env.KOMODO_URL && env.KOMODO_API_KEY && env.KOMODO_API_SECRET
    ? { url: env.KOMODO_URL.replace(/\/$/, ""), key: env.KOMODO_API_KEY, secret: env.KOMODO_API_SECRET }
    : undefined;
  return {
    port: Number(env.PORT ?? 8080),
    repoDir: env.REPO_DIR ?? "/repo",
    webDir: env.WEB_DIR ?? "dist/web",
    graphTtlMs: Number(env.GRAPH_TTL_SECONDS ?? 30) * 1000,
    statusIntervalMs: Number(env.STATUS_INTERVAL_SECONDS ?? 15) * 1000,
    komodo,
    unifi: env.UNIFI_URL && env.UNIFI_USER && env.UNIFI_PASSWORD
      ? { url: env.UNIFI_URL.replace(/\/$/, ""), user: env.UNIFI_USER, password: env.UNIFI_PASSWORD,
          verify: env.UNIFI_VERIFY === "true", fingerprint: env.UNIFI_FINGERPRINT || undefined }
      : undefined,
    dataDir: env.DATA_DIR ?? "/data",
    ...oidcFrom(env),
    create: {
      password: env.HOMEGRAF_PASSWORD || undefined,
      workDir: env.WORK_DIR ?? "/work",
      cloneUrl: env.HOMEGRAF_CLONE_URL || undefined,
      forgejoUrl: env.FORGEJO_URL || undefined,
      forgejoToken: env.FORGEJO_TOKEN || undefined,
      infisical: env.INFISICAL_URL && env.INFISICAL_BOOTSTRAP_CLIENT_ID && env.INFISICAL_BOOTSTRAP_CLIENT_SECRET
        ? { url: env.INFISICAL_URL, clientId: env.INFISICAL_BOOTSTRAP_CLIENT_ID, clientSecret: env.INFISICAL_BOOTSTRAP_CLIENT_SECRET }
        : undefined,
    },
  };
}
