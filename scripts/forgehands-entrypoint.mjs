#!/usr/bin/env node
// Launcher for the pinned ForgeHands image: it starts the image's own command, and only with an
// explicit opt-in does it read the factory's runtime configuration first.
//
// The bootstrap writes that configuration to a file outside the cloned repository, and ForgeHands
// mounts the directory read-only.  Contract: an atomic (write + rename), mode 0600, at most 64 KiB
// JSON document with exactly two keys:
//
//   {"ready": true, "env": {"FORGEJO_URL": "...", "FORGEJO_TOKEN": "...", ...}}
//
// "ready" must be the boolean true.  "env" maps only the settings below and the credentials named
// by AGENT_SECRETS; every other key is rejected.  It holds the agent user's token, ALLOWED_SENDERS
// with the human and the agent user, and HUMAN_LOGIN.  Before the command starts, the launcher asks
// Forgejo (GET /api/v1/user) who the token belongs to and refuses to start when that identity is an
// administrator, is the human, or is not in ALLOWED_SENDERS.
//
// The launcher never evaluates the data: it parses it with JSON.parse, validates every value and
// copies the strings into the child environment.  It reads no file from the cloned repository, so a
// repository cannot change the merge policy or the allowlists.
//
// Modes (FORGEHANDS_BOOTSTRAP, the same flag the bootstrap uses):
//   unset or 0   the original command runs with the environment unchanged; the runtime file is not
//                read at all, so a legacy stack boots exactly as before.
//   1            bounded wait for a usable file, then the original command runs with the runtime
//                settings merged into the environment.  A missing, partial, malformed or
//                incomplete configuration never starts ForgeHands: the launcher exits non-zero.
//   anything else fails closed.
//
// A variable the operator set in the environment keeps its value; the runtime file only fills the
// variables the operator left empty or unset.  That keeps an operator's policy and credentials in
// charge without letting a partial handoff start a half-configured factory.
//
// The original command is the image's entrypoint plus command as argv (for the pinned release
// "docker-entrypoint.sh node src/index.mjs"); it is spawned without a shell, signals are forwarded
// and the child's exit code (or 128 + the signal number) becomes the launcher's exit code.
//
// Operator-only switches of the launcher itself (never read from the runtime file):
//   FORGEHANDS_RUNTIME_FILE      default /factory-runtime/forgehands.json
//   FORGEHANDS_RUNTIME_WAIT_MS   default 120000; bounded wait for the file

import { spawn } from 'node:child_process';
import fsp from 'node:fs/promises';
import { constants as fsConstants } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const RUNTIME_FILE = '/factory-runtime/forgehands.json';
const WAIT_MS = 120_000;
const POLL_MS = 50;
const MAX_FILE_BYTES = 64 * 1024;
const MAX_VALUE = 4096;
const IDENTITY_TIMEOUT_MS = 10_000;
const PREFIX = 'forgehands-entrypoint';

// Names the pinned release reads (HUMAN_LOGIN is the launcher's own), the only ones the runtime
// file may set.
const SETTINGS = new Set([
  'FORGEJO_URL',
  'FORGEJO_TOKEN',
  'FORGEJO_WEBHOOK_SECRET',
  'OPENHANDS_API_URL',
  'OPENHANDS_API_KEY',
  'AGENT_SECRETS',
  'LABEL_ROUTES',
  'ALLOWED_REPOS',
  'ALLOWED_SENDERS',
  'MERGE_POLICY',
  'MERGE_METHOD',
  'APPROVED_LABEL',
  'BLOCKED_LABEL',
  'MAX_CONCURRENT',
  'MAX_CONCURRENT_PER_TIER',
  'MAX_RUNS_PER_HOUR',
  'MAX_RUN_MINUTES',
  'MAX_ROUNDS',
  'HUMAN_LOGIN',
  'TIER_WEEKLY_BUDGETS',
  'WORKSPACE_DIR',
  'JOBS_FILE',
  'DATA_FILE',
]);

// Without these the factory either does not start or runs without the limits and allowlists.
const REQUIRED = [
  'FORGEJO_URL',
  'FORGEJO_TOKEN',
  'FORGEJO_WEBHOOK_SECRET',
  'OPENHANDS_API_URL',
  'OPENHANDS_API_KEY',
  'AGENT_SECRETS',
  'LABEL_ROUTES',
  'ALLOWED_REPOS',
  'ALLOWED_SENDERS',
  'HUMAN_LOGIN',
];

// label -> (profile role, tier, max_iterations, event of synthetic routes); bootstrap/hgboot/agents.py
const ROUTES = new Map([
  ['ai:plan', { role: 'planner', tier: 'planner', iterations: 80 }],
  ['ai:ready', { role: 'worker', tier: 'worker', iterations: 120 }],
  ['ai:escalate', { role: 'worker-strong', tier: 'worker-strong', iterations: 120 }],
  ['ai:review', { role: 'reviewer', tier: 'reviewer', iterations: 60 }],
  ['ai:changes', { role: 'worker', tier: 'worker', iterations: 120, on: 'pull_request' }],
  ['ci:failure', { role: 'worker', tier: 'worker', iterations: 120, on: 'ci_failure' }],
  ['ci:success', { role: 'reviewer', tier: 'reviewer', iterations: 60, on: 'ci_success' }],
  ['epic:done', { role: 'reviewer', tier: 'reviewer', iterations: 60, on: 'epic_done' }],
]);

// The pinned release accepts an agent profile id only as a UUID (src/config.mjs) and lower-case
// owner/name repositories; a profile name or a mixed-case repository would fail there anyway.
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const REPO = /^[a-z0-9_.-]+\/[a-z0-9_.-]+$/;
const ENV_NAME = /^[A-Z][A-Z0-9_]*$/;
const MERGE_METHODS = new Set(['merge', 'squash', 'rebase', 'rebase-merge', 'fast-forward-only']);

class RuntimeError extends Error {}

function fail(message) {
  process.stderr.write(`${PREFIX}: ${message}\n`);
  process.exit(1);
}

function setting(env, name) {
  const value = env[name];
  return typeof value === 'string' ? value.trim() : '';
}

class Launcher {
  constructor(argv, env) {
    this.argv = argv;
    this.env = env;
  }

  async run() {
    if (this.argv.length === 0) {
      fail('no ForgeHands command given; pass the image entrypoint and command as argv');
    }
    const flag = setting(this.env, 'FORGEHANDS_BOOTSTRAP').toLowerCase();
    if (flag === '' || flag === '0') {
      this.spawnChild(this.env);
      return;
    }
    if (flag !== '1') {
      fail('FORGEHANDS_BOOTSTRAP must be 1 to opt in, or unset');
    }
    const file = this.runtimeFile();
    const runtime = await this.waitForRuntime(file, this.waitMs());
    const childEnv = this.merge(this.env, runtime);
    this.checkAgentSecrets(childEnv);
    await this.checkForgejoIdentity(childEnv);
    this.spawnChild(childEnv);
  }

  runtimeFile() {
    const value = setting(this.env, 'FORGEHANDS_RUNTIME_FILE') || RUNTIME_FILE;
    if (!path.isAbsolute(value) || value.includes('\0') || path.normalize(value) !== path.resolve(value)) {
      fail('FORGEHANDS_RUNTIME_FILE must be an absolute path without "." or ".." segments');
    }
    return value;
  }

  waitMs() {
    const raw = setting(this.env, 'FORGEHANDS_RUNTIME_WAIT_MS');
    if (raw === '') return WAIT_MS;
    const value = Number(raw);
    if (!Number.isInteger(value) || value < 0 || value > 3_600_000) {
      fail('FORGEHANDS_RUNTIME_WAIT_MS must be an integer between 0 and 3600000');
    }
    return value;
  }

  // The writer renames a complete file into place, so a partial file is a transient state: retry
  // until the deadline instead of starting with half a configuration or failing on a race.
  async waitForRuntime(file, waitMs) {
    const deadline = Date.now() + waitMs;
    let reason = 'the file is missing';
    for (;;) {
      try {
        return await readRuntime(file);
      } catch (error) {
        if (!(error instanceof RuntimeError) && !error?.code) throw error;
        reason = error instanceof RuntimeError ? error.message : 'the file cannot be read';
      }
      if (Date.now() >= deadline) {
        fail(`no usable runtime configuration at ${file} within ${waitMs} ms (${reason})`);
      }
      await new Promise((resolve) => setTimeout(resolve, Math.min(POLL_MS, deadline - Date.now())));
    }
  }

  merge(operatorEnv, runtimeEnv) {
    const childEnv = { ...operatorEnv };
    for (const [name, value] of Object.entries(runtimeEnv)) {
      if (setting(operatorEnv, name) !== '') continue;
      childEnv[name] = value;
    }
    return childEnv;
  }

  checkAgentSecrets(env) {
    const names = splitList(setting(env, 'AGENT_SECRETS'), 'AGENT_SECRETS');
    for (const name of names) {
      if (setting(env, name) === '') {
        fail(`AGENT_SECRETS names ${name}, but the runtime configuration and the environment do not set it`);
      }
    }
  }

  // Fail closed on the identity behind the token: the factory runs as the agent user, so an
  // administrator's or the human's token never reaches ForgeHands.  The token is sent only to
  // FORGEJO_URL and its value never appears in a message.
  async checkForgejoIdentity(env) {
    const base = setting(env, 'FORGEJO_URL').replace(/\/+$/, '');
    const endpoint = `${base}/api/v1/user`;
    let response;
    try {
      response = await fetch(endpoint, {
        headers: { accept: 'application/json', authorization: `token ${setting(env, 'FORGEJO_TOKEN')}` },
        // A redirect could point somewhere else and take the token with it; fail instead.
        redirect: 'error',
        signal: AbortSignal.timeout(IDENTITY_TIMEOUT_MS),
      });
    } catch (error) {
      fail(`cannot verify the Forgejo token at ${endpoint}: ${error?.cause?.code || error?.name || 'request failed'}`);
    }
    if (!response.ok) {
      fail(`the Forgejo token was refused at ${endpoint} (HTTP ${response.status})`);
    }
    let user;
    try {
      user = await response.json();
    } catch {
      fail(`${endpoint} did not answer JSON`);
    }
    if (user === null || typeof user !== 'object' || typeof user.login !== 'string' || user.login === '') {
      fail(`${endpoint} did not answer a login`);
    }
    const human = setting(env, 'HUMAN_LOGIN');
    if (user.is_admin !== false) {
      fail(`the Forgejo token belongs to ${user.login}, which is an administrator; the factory needs the agent user`);
    }
    if (user.login.toLowerCase() === human.toLowerCase()) {
      fail(`the Forgejo token belongs to ${user.login}, which is the human; the factory needs the agent user's token`);
    }
    const senders = splitList(setting(env, 'ALLOWED_SENDERS'), 'ALLOWED_SENDERS').map((name) => name.toLowerCase());
    if (!senders.includes(user.login.toLowerCase())) {
      fail(`ALLOWED_SENDERS must contain the agent user ${user.login}`);
    }
  }

  spawnChild(env) {
    const [command, ...args] = this.argv;
    const child = spawn(command, args, { env, stdio: 'inherit', shell: false });
    const forward = (signal) => {
      try {
        child.kill(signal);
      } catch {
        // The child is already gone; its exit decides the status below.
      }
    };
    for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP', 'SIGQUIT']) {
      process.on(signal, () => forward(signal));
    }
    child.on('error', (error) => {
      fail(`cannot start ${command}: ${error.code || 'spawn failed'}`);
    });
    child.on('exit', (code, signal) => {
      if (signal) {
        const number = os.constants.signals[signal];
        process.exit(Number.isInteger(number) ? 128 + number : 1);
      }
      process.exit(code ?? 1);
    });
  }
}

async function readRuntime(file) {
  // O_NOFOLLOW: a symlink in the read-only mount must not redirect the configuration elsewhere.
  const handle = await fsp.open(file, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
  try {
    const stats = await handle.stat();
    if (!stats.isFile()) throw new RuntimeError('it is not a regular file');
    if ((stats.mode & 0o077) !== 0) throw new RuntimeError('it must not be readable by group or others (mode 0600)');
    if (stats.size > MAX_FILE_BYTES) throw new RuntimeError('it is larger than 64 KiB');
    const bytes = await handle.readFile();
    if (bytes.length > MAX_FILE_BYTES) throw new RuntimeError('it is larger than 64 KiB');
    const text = bytes.toString('utf8');
    let document;
    try {
      document = JSON.parse(text);
    } catch {
      throw new RuntimeError('it is not valid JSON');
    }
    return validateDocument(document);
  } finally {
    await handle.close();
  }
}

function validateDocument(document) {
  if (document === null || typeof document !== 'object' || Array.isArray(document)) {
    throw new RuntimeError('it must be a JSON object with "ready" and "env"');
  }
  const unknown = Object.keys(document).filter((key) => key !== 'ready' && key !== 'env');
  if (unknown.length > 0) {
    throw new RuntimeError(`it has an unexpected key ${unknown.join(', ')}`);
  }
  if (document.ready !== true) {
    throw new RuntimeError('"ready" must be the boolean true');
  }
  const env = document.env;
  if (env === null || typeof env !== 'object' || Array.isArray(env)) {
    throw new RuntimeError('"env" must be a JSON object of environment names and string values');
  }

  const agentSecrets = Object.hasOwn(env, 'AGENT_SECRETS') ? splitList(env.AGENT_SECRETS, 'AGENT_SECRETS') : [];
  const named = new Set(agentSecrets);
  for (const name of Object.keys(env)) {
    if (!SETTINGS.has(name) && !named.has(name)) {
      throw new RuntimeError(`"env" has an unexpected setting ${name}`);
    }
    if (typeof env[name] !== 'string') {
      throw new RuntimeError(`"env" value of ${name} must be a string`);
    }
  }
  for (const name of REQUIRED) {
    if (!Object.hasOwn(env, name) || env[name].trim() === '') {
      throw new RuntimeError(`"env" is missing ${name}`);
    }
  }
  for (const [name, value] of Object.entries(env)) {
    checkValue(name, value);
  }
  validateRoutes(env.LABEL_ROUTES);
  if (Object.hasOwn(env, 'MERGE_POLICY')) validatePolicy(env.MERGE_POLICY);
  if (Object.hasOwn(env, 'MERGE_METHOD') && !MERGE_METHODS.has(env.MERGE_METHOD)) {
    throw new RuntimeError('MERGE_METHOD must be merge, squash, rebase, rebase-merge or fast-forward-only');
  }
  if (Object.hasOwn(env, 'TIER_WEEKLY_BUDGETS')) validateBudgets(env.TIER_WEEKLY_BUDGETS);
  if (Object.hasOwn(env, 'ALLOWED_REPOS')) checkRepos(env.ALLOWED_REPOS);
  if (Object.hasOwn(env, 'ALLOWED_SENDERS')) checkSenders(env.ALLOWED_SENDERS);
  for (const name of ['WORKSPACE_DIR', 'JOBS_FILE', 'DATA_FILE']) {
    if (Object.hasOwn(env, name) && (!path.isAbsolute(env[name]) || env[name].includes('\0'))) {
      throw new RuntimeError(`${name} must be an absolute path`);
    }
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,38}$/.test(env.HUMAN_LOGIN)) {
    throw new RuntimeError('HUMAN_LOGIN must be a Forgejo login');
  }
  if (!splitList(env.ALLOWED_SENDERS, 'ALLOWED_SENDERS').some((name) => name.toLowerCase() === env.HUMAN_LOGIN.toLowerCase())) {
    throw new RuntimeError('ALLOWED_SENDERS must contain HUMAN_LOGIN');
  }
  for (const name of ['APPROVED_LABEL', 'BLOCKED_LABEL']) {
    if (Object.hasOwn(env, name) && (!env[name].trim() || /\s/.test(env[name]))) {
      throw new RuntimeError(`${name} must be a label without white space`);
    }
  }
  return env;
}

function checkValue(name, value) {
  if (value.length > MAX_VALUE) throw new RuntimeError(`${name} is longer than ${MAX_VALUE} characters`);
  if (/[\r\n\0]/.test(value)) throw new RuntimeError(`${name} must be one line without control characters`);
  if (name === 'FORGEJO_URL' || name === 'OPENHANDS_API_URL') checkUrl(name, value);
  if (name === 'FORGEJO_WEBHOOK_SECRET' && value.length < 16) {
    throw new RuntimeError('FORGEJO_WEBHOOK_SECRET must be at least 16 characters');
  }
  if (name === 'FORGEJO_TOKEN' && (/\s/.test(value) || value.trim() !== value)) {
    throw new RuntimeError('FORGEJO_TOKEN must not contain white space');
  }
  const limit = LIMITS.get(name);
  if (limit) {
    const number = Number(value);
    if (!Number.isInteger(number) || number < 1 || number > limit) {
      throw new RuntimeError(`${name} must be an integer between 1 and ${limit}`);
    }
  }
  if (name === 'AGENT_SECRETS') {
    const names = splitList(value, name);
    if (names.length === 0) throw new RuntimeError('AGENT_SECRETS must name at least one credential');
    for (const secret of names) {
      if (!ENV_NAME.test(secret)) throw new RuntimeError(`AGENT_SECRETS entry is not an environment name: ${secret}`);
      if (RESERVED.has(secret)) throw new RuntimeError(`AGENT_SECRETS must not name the process setting ${secret}`);
    }
  }
  if (name === 'FORGEJO_TOKEN' || name === 'OPENHANDS_API_KEY') {
    if (value.trim() === '') throw new RuntimeError(`${name} must not be empty`);
  }
}

// Handing these to the child would change how the launcher's own command starts, not what
// ForgeHands does with the credential.
const RESERVED = new Set([
  'PATH',
  'HOME',
  'PWD',
  'SHELL',
  'TMPDIR',
  'USER',
  'HOSTNAME',
  'NODE_OPTIONS',
  'NODE_PATH',
  'LD_PRELOAD',
  'LD_LIBRARY_PATH',
  'DYLD_INSERT_LIBRARIES',
]);

const LIMITS = new Map([
  ['MAX_CONCURRENT', 100],
  ['MAX_CONCURRENT_PER_TIER', 100],
  ['MAX_RUNS_PER_HOUR', 10_000],
  ['MAX_RUN_MINUTES', 1_440],
  ['MAX_ROUNDS', 100],
]);

function checkUrl(name, value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new RuntimeError(`${name} must be a URL`);
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new RuntimeError(`${name} must use http or https`);
  }
  if (url.username || url.password || value.includes('@')) {
    throw new RuntimeError(`${name} must not embed credentials`);
  }
  if (url.search || url.hash) throw new RuntimeError(`${name} must be a base URL without query or fragment`);
}

function splitList(value, name) {
  if (typeof value !== 'string') throw new RuntimeError(`${name} must be a string`);
  const items = value.split(',').map((item) => item.trim());
  if (items.length === 1 && items[0] === '') return [];
  if (items.some((item) => item === '')) {
    throw new RuntimeError(`${name} must be comma separated without empty entries`);
  }
  return items;
}

function checkRepos(value) {
  const repos = splitList(value, 'ALLOWED_REPOS');
  if (repos.length === 0) throw new RuntimeError('ALLOWED_REPOS must name at least one owner/repository');
  for (const repo of repos) {
    if (!REPO.test(repo)) throw new RuntimeError(`ALLOWED_REPOS entry is not owner/repository: ${repo}`);
  }
}

function checkSenders(value) {
  const senders = splitList(value, 'ALLOWED_SENDERS');
  if (senders.length === 0) throw new RuntimeError('ALLOWED_SENDERS must name at least one login');
  for (const sender of senders) {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,38}$/.test(sender)) {
      throw new RuntimeError(`ALLOWED_SENDERS entry is not a Forgejo login: ${sender}`);
    }
  }
}

// Four real profile ids, one per role, and the eight routes the bootstrap renders from them.  A
// role name in "profile" (which the pinned release would reject with HTTP 422) or one id shared by
// two roles means the handoff is not the result of the profile read-back and must not start.
function validateRoutes(text) {
  let routes;
  try {
    routes = JSON.parse(text);
  } catch {
    throw new RuntimeError('LABEL_ROUTES must be valid JSON');
  }
  if (routes === null || typeof routes !== 'object' || Array.isArray(routes)) {
    throw new RuntimeError('LABEL_ROUTES must be a JSON object');
  }
  const keys = Object.keys(routes).sort();
  const expected = [...ROUTES.keys()].sort();
  if (keys.join(',') !== expected.join(',')) {
    throw new RuntimeError(`LABEL_ROUTES must hold exactly the eight routes: ${expected.join(', ')}`);
  }
  const ids = new Map();
  for (const [label, route] of Object.entries(routes)) {
    const wanted = ROUTES.get(label);
    if (route === null || typeof route !== 'object' || Array.isArray(route)) {
      throw new RuntimeError(`LABEL_ROUTES entry ${label} must be an object`);
    }
    const allowed = wanted.on ? ['profile', 'tier', 'max_iterations', 'on'] : ['profile', 'tier', 'max_iterations'];
    const extra = Object.keys(route).filter((key) => !allowed.includes(key));
    if (extra.length > 0) throw new RuntimeError(`LABEL_ROUTES entry ${label} has an unexpected key ${extra.join(', ')}`);
    if (typeof route.profile !== 'string' || !UUID.test(route.profile)) {
      throw new RuntimeError(`LABEL_ROUTES entry ${label} needs a real agent profile id (a UUID), not a name`);
    }
    if (route.tier !== wanted.tier) {
      throw new RuntimeError(`LABEL_ROUTES entry ${label} must use the tier ${wanted.tier}`);
    }
    if (route.max_iterations !== wanted.iterations) {
      throw new RuntimeError(`LABEL_ROUTES entry ${label} must use max_iterations ${wanted.iterations}`);
    }
    if (wanted.on ? route.on !== wanted.on : Object.hasOwn(route, 'on')) {
      throw new RuntimeError(`LABEL_ROUTES entry ${label} must use the event ${wanted.on || '(a label)'}`);
    }
    const previous = ids.get(wanted.role);
    if (previous && previous !== route.profile) {
      throw new RuntimeError(`LABEL_ROUTES uses two ids for the ${wanted.role} role`);
    }
    ids.set(wanted.role, route.profile);
  }
  if (ids.size !== 4 || new Set(ids.values()).size !== 4) {
    throw new RuntimeError('LABEL_ROUTES needs four distinct agent profile ids, one per role');
  }
}

function validatePolicy(text) {
  let policy;
  try {
    policy = JSON.parse(text);
  } catch {
    throw new RuntimeError('MERGE_POLICY must be valid JSON');
  }
  if (policy === null || typeof policy !== 'object' || Array.isArray(policy)) {
    throw new RuntimeError('MERGE_POLICY must be a JSON object of repository -> {deny: [globs]}');
  }
  for (const [name, rule] of Object.entries(policy)) {
    if (!REPO.test(name)) throw new RuntimeError(`MERGE_POLICY key is not owner/repository: ${name}`);
    if (rule === null || typeof rule !== 'object' || Array.isArray(rule) || Object.keys(rule).join(',') !== 'deny') {
      throw new RuntimeError(`MERGE_POLICY entry for ${name} must be exactly {deny: [globs]}`);
    }
    if (!Array.isArray(rule.deny)) throw new RuntimeError(`MERGE_POLICY deny of ${name} must be a list`);
    for (const glob of rule.deny) {
      if (typeof glob !== 'string' || glob === '' || glob.length > 500 || glob !== glob.trim()) {
        throw new RuntimeError(`MERGE_POLICY deny of ${name} must hold non-empty paths`);
      }
      if (glob.startsWith('/') || glob.includes('\\') || /[\r\n\0]/.test(glob)
        || glob.split('/').some((part) => part === '.' || part === '..')) {
        throw new RuntimeError(`MERGE_POLICY deny of ${name} must hold relative paths inside the repository`);
      }
    }
  }
}

function validateBudgets(text) {
  let budgets;
  try {
    budgets = JSON.parse(text);
  } catch {
    throw new RuntimeError('TIER_WEEKLY_BUDGETS must be valid JSON');
  }
  if (budgets === null || typeof budgets !== 'object' || Array.isArray(budgets)) {
    throw new RuntimeError('TIER_WEEKLY_BUDGETS must be a JSON object of tier -> amount');
  }
  for (const [tier, amount] of Object.entries(budgets)) {
    if (typeof amount !== 'number' || !(amount > 0)) {
      throw new RuntimeError(`TIER_WEEKLY_BUDGETS: ${tier} must be a number > 0`);
    }
  }
}

const entry = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : '';
if (entry === import.meta.url) {
  new Launcher(process.argv.slice(2), process.env).run().catch((error) => {
    fail(error instanceof RuntimeError ? error.message : `unexpected failure: ${error?.code || 'error'}`);
  });
}
