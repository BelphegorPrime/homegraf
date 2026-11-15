// Behavior of scripts/forgehands-entrypoint.mjs against the real script: the original command is a
// harmless Node child that records its argv and environment, and the runtime file lives in a
// temporary directory.  Nothing here needs Docker or ForgeHands itself; the only socket is the
// loopback fixture for Forgejo's GET /api/v1/user.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import fsp from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const WRAPPER = fileURLToPath(new URL('./forgehands-entrypoint.mjs', import.meta.url));

const PLANNER = '11111111-1111-4111-8111-111111111111';
const WORKER = '22222222-2222-4222-8222-222222222222';
const WORKER_STRONG = '33333333-3333-4333-8333-333333333333';
const REVIEWER = '44444444-4444-4444-8444-444444444444';
const TOKEN = 'forgejo-agent-token-not-for-logs';
const BACKEND_KEY = 'backend-key-not-for-logs';
const WEBHOOK_SECRET = '0123456789abcdef';

// The Forgejo API boundary, loopback only: GET /api/v1/user answers for the token in the request.
const USERS = new Map([
  [TOKEN, { login: 'openhands', is_admin: false }],
  ['operator-token', { login: 'openhands', is_admin: false }],
  ['admin-token', { login: 'root', is_admin: true }],
  ['human-token', { login: 'marcel', is_admin: false }],
  ['stranger-token', { login: 'stranger', is_admin: false }],
]);
const forgejo = createServer((request, response) => {
  if (request.url !== '/api/v1/user') {
    response.writeHead(404).end();
    return;
  }
  const token = (request.headers.authorization || '').replace(/^token /, '');
  const user = USERS.get(token);
  if (!user) {
    response.writeHead(401, { 'content-type': 'application/json' }).end('{"message":"unauthorized"}');
    return;
  }
  response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(user));
});
await new Promise((resolve) => forgejo.listen(0, '127.0.0.1', resolve));
const FORGEJO = `http://127.0.0.1:${forgejo.address().port}`;
test.after(() => new Promise((resolve) => forgejo.close(resolve)));

const CHILD = `
import { writeFileSync } from 'node:fs';
writeFileSync(process.env.CHILD_OUT, JSON.stringify({ argv: process.argv.slice(2), env: process.env }));
process.exit(Number(process.env.CHILD_EXIT || 0));
`;

// Records its argv and environment, then exits with CHILD_EXIT.
async function childScript(dir) {
  const file = path.join(dir, 'child.mjs');
  await fsp.writeFile(file, CHILD);
  return file;
}

async function waitScript(dir) {
  const file = path.join(dir, 'wait.mjs');
  await fsp.writeFile(file, `
import { writeFileSync } from 'node:fs';
writeFileSync(process.env.CHILD_OUT, JSON.stringify({ argv: process.argv.slice(2) }));
setInterval(() => {}, 1000);
`);
  return file;
}

function routes(ids = {}) {
  const id = { planner: PLANNER, worker: WORKER, 'worker-strong': WORKER_STRONG, reviewer: REVIEWER, ...ids };
  return JSON.stringify({
    'ai:plan': { profile: id.planner, tier: 'planner', max_iterations: 80 },
    'ai:ready': { profile: id.worker, tier: 'worker', max_iterations: 120 },
    'ai:escalate': { profile: id['worker-strong'], tier: 'worker-strong', max_iterations: 120 },
    'ai:review': { profile: id.reviewer, tier: 'reviewer', max_iterations: 60 },
    'ai:changes': { profile: id.worker, tier: 'worker', max_iterations: 120, on: 'pull_request' },
    'ci:failure': { profile: id.worker, tier: 'worker', max_iterations: 120, on: 'ci_failure' },
    'ci:success': { profile: id.reviewer, tier: 'reviewer', max_iterations: 60, on: 'ci_success' },
    'epic:done': { profile: id.reviewer, tier: 'reviewer', max_iterations: 60, on: 'epic_done' },
  });
}

function runtime(overrides = {}) {
  return {
    ready: true,
    env: {
      FORGEJO_URL: FORGEJO,
      FORGEJO_TOKEN: TOKEN,
      FORGEJO_WEBHOOK_SECRET: WEBHOOK_SECRET,
      OPENHANDS_API_URL: 'http://openhands:8000',
      OPENHANDS_API_KEY: BACKEND_KEY,
      AGENT_SECRETS: 'FORGEJO_TOKEN',
      LABEL_ROUTES: routes(),
      ALLOWED_REPOS: 'marcel/homegraf',
      ALLOWED_SENDERS: 'marcel,openhands',
      HUMAN_LOGIN: 'marcel',
      MERGE_POLICY: JSON.stringify({ 'marcel/homegraf': { deny: ['.forgejo/**', '.github/**', 'scripts/**'] } }),
      MERGE_METHOD: 'rebase-merge',
      MAX_CONCURRENT: '4',
      MAX_CONCURRENT_PER_TIER: '2',
      MAX_RUNS_PER_HOUR: '30',
      MAX_RUN_MINUTES: '120',
      MAX_ROUNDS: '3',
      WORKSPACE_DIR: '/workspace',
      JOBS_FILE: '/app/data/jobs.json',
      ...overrides.env,
    },
    ...overrides.document,
  };
}

async function writeRuntime(dir, document, mode = 0o600) {
  const file = path.join(dir, 'forgehands.json');
  const temporary = `${file}.${process.pid}.tmp`;
  await fsp.writeFile(temporary, JSON.stringify(document), { mode });
  await fsp.chmod(temporary, mode);
  await fsp.rename(temporary, file);
  return file;
}

async function tempDir() {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'forgehands-entrypoint-'));
  test.after(() => fsp.rm(dir, { recursive: true, force: true }));
  return dir;
}

// Runs the real launcher with the original command as argv, exactly as the compose entrypoint does.
function launch(argv, env, cwd) {
  const child = spawn(process.execPath, [WRAPPER, ...argv], { env, cwd, stdio: ['ignore', 'pipe', 'pipe'] });
  let stdout = '';
  let stderr = '';
  child.stdout.on('data', (chunk) => { stdout += chunk; });
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  const done = new Promise((resolve) => {
    child.on('exit', (code, signal) => resolve({ code, signal, stdout, stderr }));
  });
  return { child, done };
}

async function record(dir, name = 'child.json') {
  const file = path.join(dir, name);
  try {
    return JSON.parse(await fsp.readFile(file, 'utf8'));
  } catch {
    return null;
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

test('passes the original command and the unchanged environment through when the flag is unset', async () => {
  const dir = await tempDir();
  const script = await childScript(dir);
  const out = path.join(dir, 'child.json');
  const argv = [process.execPath, script, 'src/index.mjs', 'a b; echo pwned', '$(touch nope)'];
  const env = {
    CHILD_OUT: out,
    CHILD_EXIT: '0',
    FORGEHANDS_RUNTIME_FILE: path.join(dir, 'absent.json'),
    FORGEHANDS_RUNTIME_WAIT_MS: '100',
  };
  const { done } = launch(argv, env);
  const result = await done;
  assert.equal(result.code, 0);
  const recorded = await record(dir);
  assert.deepEqual(recorded.argv, argv.slice(2));
  assert.deepEqual(recorded.env, env);
});

test('treats an explicit 0 as not opted in', async () => {
  const dir = await tempDir();
  const script = await childScript(dir);
  const env = {
    CHILD_OUT: path.join(dir, 'child.json'),
    FORGEHANDS_BOOTSTRAP: '0',
    FORGEHANDS_RUNTIME_FILE: path.join(dir, 'absent.json'),
    FORGEHANDS_RUNTIME_WAIT_MS: '50',
  };
  const { done } = launch([process.execPath, script], env);
  assert.equal((await done).code, 0);
  assert.ok(await record(dir));
});

test('fails closed on any other value of the opt-in flag', async () => {
  const dir = await tempDir();
  const script = await childScript(dir);
  const { done } = launch([process.execPath, script], {
    CHILD_OUT: path.join(dir, 'child.json'),
    FORGEHANDS_BOOTSTRAP: 'true',
  });
  const result = await done;
  assert.equal(result.code, 1);
  assert.match(result.stderr, /FORGEHANDS_BOOTSTRAP must be 1/);
  assert.equal(await record(dir), null);
});

test('fails closed without a command to start', async () => {
  const { done } = launch([], { FORGEHANDS_BOOTSTRAP: '1' });
  const result = await done;
  assert.equal(result.code, 1);
  assert.match(result.stderr, /no ForgeHands command/);
});

test('waits for the runtime file, then starts the command with its settings and forwards the exit code', async () => {
  const dir = await tempDir();
  const script = await childScript(dir);
  const out = path.join(dir, 'child.json');
  const file = path.join(dir, 'forgehands.json');
  const env = {
    CHILD_OUT: out,
    CHILD_EXIT: '7',
    FORGEHANDS_BOOTSTRAP: '1',
    FORGEHANDS_RUNTIME_FILE: file,
    FORGEHANDS_RUNTIME_WAIT_MS: '5000',
    OPENHANDS_API_URL: '',
  };
  const { done } = launch([process.execPath, script, 'src/index.mjs'], env);
  await sleep(150);
  await writeRuntime(dir, runtime());
  const result = await done;
  assert.equal(result.code, 7);
  const recorded = await record(dir);
  assert.deepEqual(recorded.argv, ['src/index.mjs']);
  assert.equal(recorded.env.FORGEJO_TOKEN, TOKEN);
  assert.equal(recorded.env.OPENHANDS_API_KEY, BACKEND_KEY);
  assert.equal(recorded.env.OPENHANDS_API_URL, 'http://openhands:8000');
  assert.equal(recorded.env.LABEL_ROUTES, routes());
  assert.equal(recorded.env.MERGE_METHOD, 'rebase-merge');
  assert.equal(recorded.env.WORKSPACE_DIR, '/workspace');
  assert.equal(recorded.env.FORGEHANDS_BOOTSTRAP, '1');
});

test('keeps the operator value when the runtime file names the same setting', async () => {
  const dir = await tempDir();
  const script = await childScript(dir);
  const out = path.join(dir, 'child.json');
  const operatorRoutes = routes({ worker: '99999999-9999-4999-8999-999999999999' });
  await writeRuntime(dir, runtime());
  const { done } = launch([process.execPath, script], {
    CHILD_OUT: out,
    FORGEHANDS_BOOTSTRAP: '1',
    FORGEHANDS_RUNTIME_FILE: path.join(dir, 'forgehands.json'),
    FORGEHANDS_RUNTIME_WAIT_MS: '1000',
    FORGEJO_TOKEN: 'operator-token',
    LABEL_ROUTES: operatorRoutes,
  });
  assert.equal((await done).code, 0);
  const recorded = await record(dir);
  assert.equal(recorded.env.FORGEJO_TOKEN, 'operator-token');
  assert.equal(recorded.env.LABEL_ROUTES, operatorRoutes);
  assert.equal(recorded.env.ALLOWED_REPOS, 'marcel/homegraf');
});

test('times out without starting the command when no file appears', async () => {
  const dir = await tempDir();
  const script = await childScript(dir);
  const { done } = launch([process.execPath, script], {
    CHILD_OUT: path.join(dir, 'child.json'),
    FORGEHANDS_BOOTSTRAP: '1',
    FORGEHANDS_RUNTIME_FILE: path.join(dir, 'forgehands.json'),
    FORGEHANDS_RUNTIME_WAIT_MS: '150',
  });
  const result = await done;
  assert.equal(result.code, 1);
  assert.match(result.stderr, /no usable runtime configuration/);
  assert.match(result.stderr, /within 150 ms/);
  assert.equal(await record(dir), null);
});

test('rejects a partial file and never starts the command with it', async () => {
  const dir = await tempDir();
  const script = await childScript(dir);
  const file = path.join(dir, 'forgehands.json');
  await fsp.writeFile(file, '{"ready": true, "env": {"FORGEJO_URL":', { mode: 0o600 });
  const { done } = launch([process.execPath, script], {
    CHILD_OUT: path.join(dir, 'child.json'),
    FORGEHANDS_BOOTSTRAP: '1',
    FORGEHANDS_RUNTIME_FILE: file,
    FORGEHANDS_RUNTIME_WAIT_MS: '300',
  });
  const result = await done;
  assert.equal(result.code, 1);
  assert.match(result.stderr, /not valid JSON/);
  assert.equal(await record(dir), null);
});

test('accepts a file that appears only after a partial one', async () => {
  const dir = await tempDir();
  const script = await childScript(dir);
  const file = path.join(dir, 'forgehands.json');
  await fsp.writeFile(file, '{"ready": true,', { mode: 0o600 });
  const { done } = launch([process.execPath, script], {
    CHILD_OUT: path.join(dir, 'child.json'),
    FORGEHANDS_BOOTSTRAP: '1',
    FORGEHANDS_RUNTIME_FILE: file,
    FORGEHANDS_RUNTIME_WAIT_MS: '5000',
  });
  await sleep(120);
  await writeRuntime(dir, runtime());
  assert.equal((await done).code, 0);
  assert.ok(await record(dir));
});

test('refuses a file that is not ready, not 0600, a symlink or too large', async () => {
  const cases = [
    { name: 'ready false', document: runtime({ document: { ready: false } }), mode: 0o600, message: /"ready" must be the boolean true/ },
    { name: 'ready as a string', document: runtime({ document: { ready: 'true' } }), mode: 0o600, message: /"ready" must be the boolean true/ },
    { name: 'mode 0640', document: runtime(), mode: 0o640, message: /mode 0600/ },
    { name: 'mode 0644', document: runtime(), mode: 0o644, message: /mode 0600/ },
  ];
  for (const item of cases) {
    const dir = await tempDir();
    const script = await childScript(dir);
    const file = await writeRuntime(dir, item.document, item.mode);
    const { done } = launch([process.execPath, script], {
      CHILD_OUT: path.join(dir, 'child.json'),
      FORGEHANDS_BOOTSTRAP: '1',
      FORGEHANDS_RUNTIME_FILE: file,
      FORGEHANDS_RUNTIME_WAIT_MS: '200',
    });
    const result = await done;
    assert.equal(result.code, 1, item.name);
    assert.match(result.stderr, item.message, item.name);
    assert.equal(await record(dir), null, item.name);
  }

  const dir = await tempDir();
  const script = await childScript(dir);
  const target = await writeRuntime(dir, runtime());
  const link = path.join(dir, 'link.json');
  await fsp.symlink(target, link);
  const linked = await launch([process.execPath, script], {
    CHILD_OUT: path.join(dir, 'child.json'),
    FORGEHANDS_BOOTSTRAP: '1',
    FORGEHANDS_RUNTIME_FILE: link,
    FORGEHANDS_RUNTIME_WAIT_MS: '200',
  });
  assert.equal((await linked.done).code, 1);
  assert.equal(await record(dir), null);

  const directory = await tempDir();
  const directoryScript = await childScript(directory);
  await fsp.mkdir(path.join(directory, 'nested'));
  const notAFile = await launch([process.execPath, directoryScript], {
    CHILD_OUT: path.join(directory, 'child.json'),
    FORGEHANDS_BOOTSTRAP: '1',
    FORGEHANDS_RUNTIME_FILE: path.join(directory, 'nested'),
    FORGEHANDS_RUNTIME_WAIT_MS: '200',
  });
  assert.equal((await notAFile.done).code, 1);
  assert.match((await notAFile.done).stderr, /not a regular file/);
  assert.equal(await record(directory), null);

  const large = await tempDir();
  const largeScript = await childScript(large);
  const oversized = path.join(large, 'forgehands.json');
  await fsp.writeFile(oversized, JSON.stringify(runtime({ env: { MERGE_POLICY: 'x'.repeat(70 * 1024) } })), { mode: 0o600 });
  const tooLarge = await launch([process.execPath, largeScript], {
    CHILD_OUT: path.join(large, 'child.json'),
    FORGEHANDS_BOOTSTRAP: '1',
    FORGEHANDS_RUNTIME_FILE: oversized,
    FORGEHANDS_RUNTIME_WAIT_MS: '200',
  });
  assert.equal((await tooLarge.done).code, 1);
  assert.match((await tooLarge.done).stderr, /larger than 64 KiB/);
  assert.equal(await record(large), null);
});

test('rejects unexpected keys, missing settings and non-string values', async () => {
  const cases = [
    { name: 'unexpected setting', doc: runtime({ env: { GITHUB_TOKEN: TOKEN } }), message: /unexpected setting GITHUB_TOKEN/ },
    { name: 'unexpected top-level key', doc: { ...runtime(), policy: {} }, message: /unexpected key policy/ },
    { name: 'missing token', doc: runtime({ env: { FORGEJO_TOKEN: '' } }), message: /missing FORGEJO_TOKEN/ },
    { name: 'missing routes', doc: runtime({ env: { LABEL_ROUTES: undefined } }), message: /missing LABEL_ROUTES/ },
    { name: 'numeric value', doc: runtime({ env: { MAX_CONCURRENT: 4 } }), message: /MAX_CONCURRENT must be a string/ },
    { name: 'newline in value', doc: runtime({ env: { ALLOWED_SENDERS: 'marcel,openhands\n' } }), message: /one line/ },
    { name: 'reserved agent secret', doc: runtime({ env: { AGENT_SECRETS: 'PATH' } }), message: /must not name the process setting PATH/ },
    { name: 'agent secret not a name', doc: runtime({ env: { AGENT_SECRETS: 'forgejo token' } }), message: /not an environment name/ },
    { name: 'empty agent secrets', doc: runtime({ env: { AGENT_SECRETS: '' } }), message: /missing AGENT_SECRETS/ },
    { name: 'unresolved agent secret', doc: runtime({ env: { AGENT_SECRETS: 'MIRROR_TOKEN' } }), message: /AGENT_SECRETS names MIRROR_TOKEN/ },
    { name: 'opt-in flag in the runtime file', doc: runtime({ env: { FORGEHANDS_BOOTSTRAP: '1' } }), message: /unexpected setting FORGEHANDS_BOOTSTRAP/ },
    { name: 'runtime file path in the runtime file', doc: runtime({ env: { FORGEHANDS_RUNTIME_FILE: '/tmp/x.json' } }), message: /unexpected setting FORGEHANDS_RUNTIME_FILE/ },
    { name: 'wait time in the runtime file', doc: runtime({ env: { FORGEHANDS_RUNTIME_WAIT_MS: '1' } }), message: /unexpected setting FORGEHANDS_RUNTIME_WAIT_MS/ },
  ];
  for (const item of cases) {
    const dir = await tempDir();
    const script = await childScript(dir);
    await writeRuntime(dir, item.doc);
    const { done } = launch([process.execPath, script], {
      CHILD_OUT: path.join(dir, 'child.json'),
      FORGEHANDS_BOOTSTRAP: '1',
      FORGEHANDS_RUNTIME_FILE: path.join(dir, 'forgehands.json'),
      FORGEHANDS_RUNTIME_WAIT_MS: '200',
    });
    const result = await done;
    assert.equal(result.code, 1, item.name);
    assert.match(result.stderr, item.message, item.name);
    assert.equal(await record(dir), null, item.name);
  }
});

test('rejects malformed limits, URLs and paths', async () => {
  const cases = [
    { name: 'limit too large', env: { MAX_CONCURRENT: '101' }, message: /MAX_CONCURRENT must be an integer between 1 and 100/ },
    { name: 'limit zero', env: { MAX_RUN_MINUTES: '0' }, message: /MAX_RUN_MINUTES must be an integer/ },
    { name: 'url with credentials', env: { FORGEJO_URL: 'http://user:pass@forgejo:3000' }, message: /must not embed credentials/ },
    { name: 'url with query', env: { OPENHANDS_API_URL: 'http://openhands:8000?x=1' }, message: /base URL without query/ },
    { name: 'short webhook secret', env: { FORGEJO_WEBHOOK_SECRET: 'short' }, message: /at least 16 characters/ },
    { name: 'repository with upper case', env: { ALLOWED_REPOS: 'Marcel/homegraf' }, message: /not owner\/repository/ },
    { name: 'sender with white space', env: { ALLOWED_SENDERS: 'mar cel' }, message: /not a Forgejo login/ },
    { name: 'relative workspace', env: { WORKSPACE_DIR: 'workspace' }, message: /WORKSPACE_DIR must be an absolute path/ },
    { name: 'unknown merge method', env: { MERGE_METHOD: 'octopus' }, message: /MERGE_METHOD must be/ },
    { name: 'budget not positive', env: { TIER_WEEKLY_BUDGETS: '{"worker":0}' }, message: /worker must be a number > 0/ },
  ];
  for (const item of cases) {
    const dir = await tempDir();
    const script = await childScript(dir);
    await writeRuntime(dir, runtime({ env: item.env }));
    const { done } = launch([process.execPath, script], {
      CHILD_OUT: path.join(dir, 'child.json'),
      FORGEHANDS_BOOTSTRAP: '1',
      FORGEHANDS_RUNTIME_FILE: path.join(dir, 'forgehands.json'),
      FORGEHANDS_RUNTIME_WAIT_MS: '200',
    });
    const result = await done;
    assert.equal(result.code, 1, item.name);
    assert.match(result.stderr, item.message, item.name);
  }
});

test('rejects an invalid merge policy', async () => {
  const cases = [
    { policy: 'not json', message: /MERGE_POLICY must be valid JSON/ },
    { policy: '[]', message: /must be a JSON object/ },
    { policy: JSON.stringify({ 'Not a repo': { deny: ['a'] } }), message: /not owner\/repository/ },
    { policy: JSON.stringify({ 'marcel/homegraf': { deny: ['/etc/passwd'] } }), message: /relative paths/ },
    { policy: JSON.stringify({ 'marcel/homegraf': { deny: ['../outside'] } }), message: /relative paths/ },
    { policy: JSON.stringify({ 'marcel/homegraf': { deny: [''] } }), message: /non-empty paths/ },
    { policy: JSON.stringify({ 'marcel/homegraf': { allow: ['x'] } }), message: /exactly \{deny: \[globs\]\}/ },
  ];
  for (const item of cases) {
    const dir = await tempDir();
    const script = await childScript(dir);
    await writeRuntime(dir, runtime({ env: { MERGE_POLICY: item.policy } }));
    const { done } = launch([process.execPath, script], {
      CHILD_OUT: path.join(dir, 'child.json'),
      FORGEHANDS_BOOTSTRAP: '1',
      FORGEHANDS_RUNTIME_FILE: path.join(dir, 'forgehands.json'),
      FORGEHANDS_RUNTIME_WAIT_MS: '200',
    });
    const result = await done;
    assert.equal(result.code, 1, item.policy);
    assert.match(result.stderr, item.message, item.policy);
  }
});

test('rejects role names, shared ids, missing routes and foreign keys in LABEL_ROUTES', async () => {
  const cases = [
    { name: 'role name as id', routes: routes({ worker: 'worker' }), message: /real agent profile id/ },
    { name: 'truncated uuid', routes: routes({ planner: '1111' }), message: /real agent profile id/ },
    { name: 'shared id', routes: routes({ reviewer: PLANNER }), message: /four distinct agent profile ids/ },
    { name: 'one id per worker route', routes: routes().replace(WORKER, WORKER_STRONG), message: /two ids for the worker role/ },
    { name: 'seven routes', routes: JSON.stringify({ ...JSON.parse(routes()), 'epic:done': undefined }), message: /eight routes/ },
    { name: 'wrong tier', routes: routes().replace('"tier":"worker"', '"tier":"strong"'), message: /must use the tier worker/ },
    { name: 'wrong event', routes: routes().replace('"on":"ci_failure"', '"on":"pull_request"'), message: /must use the event ci_failure/ },
    { name: 'unexpected route key', routes: routes().replace('"max_iterations":80', '"max_iterations":80,"max_budget":1.5'), message: /unexpected key max_budget/ },
    { name: 'not an object', routes: '[]', message: /must be a JSON object/ },
  ];
  for (const item of cases) {
    const dir = await tempDir();
    const script = await childScript(dir);
    await writeRuntime(dir, runtime({ env: { LABEL_ROUTES: item.routes } }));
    const { done } = launch([process.execPath, script], {
      CHILD_OUT: path.join(dir, 'child.json'),
      FORGEHANDS_BOOTSTRAP: '1',
      FORGEHANDS_RUNTIME_FILE: path.join(dir, 'forgehands.json'),
      FORGEHANDS_RUNTIME_WAIT_MS: '200',
    });
    const result = await done;
    assert.equal(result.code, 1, item.name);
    assert.match(result.stderr, item.message, item.name);
  }
});

test('refuses a token that belongs to an administrator or to the human', async () => {
  const cases = [
    { name: 'administrator', token: 'admin-token', message: /belongs to root, which is an administrator/ },
    { name: 'the human', token: 'human-token', message: /belongs to marcel, which is the human/ },
    { name: 'a sender outside the allowlist', token: 'stranger-token', message: /ALLOWED_SENDERS must contain the agent user stranger/ },
    { name: 'a refused token', token: 'unknown-token', message: /refused at .* \(HTTP 401\)/ },
  ];
  for (const item of cases) {
    const dir = await tempDir();
    const script = await childScript(dir);
    await writeRuntime(dir, runtime({ env: { FORGEJO_TOKEN: item.token } }));
    const { done } = launch([process.execPath, script], {
      CHILD_OUT: path.join(dir, 'child.json'),
      FORGEHANDS_BOOTSTRAP: '1',
      FORGEHANDS_RUNTIME_FILE: path.join(dir, 'forgehands.json'),
      FORGEHANDS_RUNTIME_WAIT_MS: '1000',
    });
    const result = await done;
    assert.equal(result.code, 1, item.name);
    assert.match(result.stderr, item.message, item.name);
    assert.equal(await record(dir), null, item.name);
    assert.ok(!result.stderr.includes(item.token), `${item.name}: the token must not be logged`);
  }
});

test('refuses to start when Forgejo cannot confirm the token', async () => {
  const dir = await tempDir();
  const script = await childScript(dir);
  await writeRuntime(dir, runtime({ env: { FORGEJO_URL: 'http://127.0.0.1:1' } }));
  const { done } = launch([process.execPath, script], {
    CHILD_OUT: path.join(dir, 'child.json'),
    FORGEHANDS_BOOTSTRAP: '1',
    FORGEHANDS_RUNTIME_FILE: path.join(dir, 'forgehands.json'),
    FORGEHANDS_RUNTIME_WAIT_MS: '1000',
  });
  const result = await done;
  assert.equal(result.code, 1);
  assert.match(result.stderr, /cannot verify the Forgejo token/);
  assert.ok(!result.stderr.includes(TOKEN));
  assert.equal(await record(dir), null);
});

test('does not follow a redirect that would take the token elsewhere', async () => {
  const redirecting = createServer((request, response) => {
    response.writeHead(302, { location: 'http://127.0.0.1:1/api/v1/user' }).end();
  });
  await new Promise((resolve) => redirecting.listen(0, '127.0.0.1', resolve));
  try {
    const dir = await tempDir();
    const script = await childScript(dir);
    const url = `http://127.0.0.1:${redirecting.address().port}`;
    await writeRuntime(dir, runtime({ env: { FORGEJO_URL: url } }));
    const { done } = launch([process.execPath, script], {
      CHILD_OUT: path.join(dir, 'child.json'),
      FORGEHANDS_BOOTSTRAP: '1',
      FORGEHANDS_RUNTIME_FILE: path.join(dir, 'forgehands.json'),
      FORGEHANDS_RUNTIME_WAIT_MS: '1000',
    });
    const result = await done;
    assert.equal(result.code, 1);
    assert.match(result.stderr, /cannot verify the Forgejo token/);
    assert.ok(!result.stderr.includes(TOKEN));
    assert.equal(await record(dir), null);
  } finally {
    await new Promise((resolve) => redirecting.close(resolve));
  }
});

test('requires HUMAN_LOGIN in the runtime file and in ALLOWED_SENDERS', async () => {
  const cases = [
    { name: 'missing human', env: { HUMAN_LOGIN: '' }, message: /missing HUMAN_LOGIN/ },
    { name: 'not a login', env: { HUMAN_LOGIN: 'mar cel' }, message: /HUMAN_LOGIN must be a Forgejo login/ },
    { name: 'not a sender', env: { HUMAN_LOGIN: 'someone' }, message: /ALLOWED_SENDERS must contain HUMAN_LOGIN/ },
  ];
  for (const item of cases) {
    const dir = await tempDir();
    const script = await childScript(dir);
    await writeRuntime(dir, runtime({ env: item.env }));
    const { done } = launch([process.execPath, script], {
      CHILD_OUT: path.join(dir, 'child.json'),
      FORGEHANDS_BOOTSTRAP: '1',
      FORGEHANDS_RUNTIME_FILE: path.join(dir, 'forgehands.json'),
      FORGEHANDS_RUNTIME_WAIT_MS: '200',
    });
    const result = await done;
    assert.equal(result.code, 1, item.name);
    assert.match(result.stderr, item.message, item.name);
  }
});

test('never writes a secret value to its output', async () => {
  const dir = await tempDir();
  const script = await childScript(dir);
  await writeRuntime(dir, runtime({ env: { GITHUB_TOKEN: TOKEN, MIRROR_TOKEN: BACKEND_KEY } }));
  const { done } = launch([process.execPath, script], {
    CHILD_OUT: path.join(dir, 'child.json'),
    FORGEHANDS_BOOTSTRAP: '1',
    FORGEHANDS_RUNTIME_FILE: path.join(dir, 'forgehands.json'),
    FORGEHANDS_RUNTIME_WAIT_MS: '200',
  });
  const result = await done;
  assert.equal(result.code, 1);
  assert.match(result.stderr, /unexpected setting GITHUB_TOKEN/);
  for (const secret of [TOKEN, BACKEND_KEY, WEBHOOK_SECRET]) {
    assert.ok(!result.stderr.includes(secret), `stderr must not contain ${secret}`);
    assert.ok(!result.stdout.includes(secret), `stdout must not contain ${secret}`);
  }
});

test('requires an absolute runtime file path and a bounded wait', async () => {
  const dir = await tempDir();
  const script = await childScript(dir);
  for (const [name, env, message] of [
    ['relative file', { FORGEHANDS_RUNTIME_FILE: 'forgehands.json' }, /FORGEHANDS_RUNTIME_FILE must be an absolute path/],
    ['negative wait', { FORGEHANDS_RUNTIME_WAIT_MS: '-1' }, /FORGEHANDS_RUNTIME_WAIT_MS must be an integer/],
    ['huge wait', { FORGEHANDS_RUNTIME_WAIT_MS: '3600001' }, /FORGEHANDS_RUNTIME_WAIT_MS must be an integer/],
  ]) {
    const { done } = launch([process.execPath, script], {
      CHILD_OUT: path.join(dir, 'child.json'),
      FORGEHANDS_BOOTSTRAP: '1',
      ...env,
    });
    const result = await done;
    assert.equal(result.code, 1, name);
    assert.match(result.stderr, message, name);
  }
  assert.equal(await record(dir), null);
});

test('forwards a SIGTERM to the command and exits with 128 + the signal', async () => {
  const dir = await tempDir();
  const script = await waitScript(dir);
  const out = path.join(dir, 'child.json');
  await writeRuntime(dir, runtime());
  const { child, done } = launch([process.execPath, script], {
    CHILD_OUT: out,
    FORGEHANDS_BOOTSTRAP: '1',
    FORGEHANDS_RUNTIME_FILE: path.join(dir, 'forgehands.json'),
    FORGEHANDS_RUNTIME_WAIT_MS: '1000',
  });
  for (let attempt = 0; attempt < 100 && !(await record(dir)); attempt += 1) await sleep(20);
  assert.ok(await record(dir), 'the child must have started');
  child.kill('SIGTERM');
  const result = await done;
  assert.equal(result.code, 143);
  assert.equal(result.signal, null);
});

test('reads the runtime file from the configured path, never from the working directory', async () => {
  const dir = await tempDir();
  const clone = path.join(dir, 'clone');
  await fsp.mkdir(clone);
  await fsp.writeFile(path.join(clone, 'forgehands.json'), JSON.stringify(runtime({ env: {
    MERGE_POLICY: JSON.stringify({ 'marcel/homegraf': { deny: [] } }),
  } })));
  const script = await childScript(dir);
  const out = path.join(dir, 'child.json');
  await writeRuntime(dir, runtime());
  const { done } = launch([process.execPath, script], {
    CHILD_OUT: out,
    FORGEHANDS_BOOTSTRAP: '1',
    FORGEHANDS_RUNTIME_FILE: path.join(dir, 'forgehands.json'),
    FORGEHANDS_RUNTIME_WAIT_MS: '1000',
  }, clone);
  assert.equal((await done).code, 0);
  const recorded = await record(dir);
  assert.deepEqual(JSON.parse(recorded.env.MERGE_POLICY), { 'marcel/homegraf': { deny: ['.forgejo/**', '.github/**', 'scripts/**'] } });
});

test('passes the command as argv and never through a shell', async () => {
  const dir = await tempDir();
  const script = await childScript(dir);
  const out = path.join(dir, 'child.json');
  await writeRuntime(dir, runtime());
  const { done } = launch([process.execPath, script, 'literal; touch pwned', '$(id)'], {
    CHILD_OUT: out,
    FORGEHANDS_BOOTSTRAP: '1',
    FORGEHANDS_RUNTIME_FILE: path.join(dir, 'forgehands.json'),
    FORGEHANDS_RUNTIME_WAIT_MS: '1000',
  });
  assert.equal((await done).code, 0);
  const recorded = await record(dir);
  assert.deepEqual(recorded.argv, ['literal; touch pwned', '$(id)']);
  await assert.rejects(fsp.stat(path.join(process.cwd(), 'pwned')));
});
