"""The opt-in factory in a real setup.sh run and the Compose handoff that carries it to ForgeHands."""
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from hgboot.agent_user import LOGIN as AGENT_LOGIN
from hgboot.agent_user import TOKEN as AGENT_TOKEN, ensure_agent_user
from hgboot.config import Config
from hgboot.factory_config import DEFAULT_MERGE_DENY, FactoryConfig

from .test_agent_user import FakeAgentForgejo, Server, Store

ROOT = pathlib.Path(__file__).resolve().parents[2]
SETUP = ROOT / "setup.sh"
COMPOSE = ROOT / "compose.yaml"

FACTORY_INPUTS = {
    "FORGEHANDS_STRONG_MODEL": "vendor/strong-model",
    "FORGEHANDS_CHEAP_MODEL": "vendor/cheap-model",
    "FORGEHANDS_GATEWAY_URL": "https://llm.example/v1",
    "FORGEHANDS_GATEWAY_API_KEY": "gateway-key-123",
}
BASE_ENV = {
    "HOST_IP": "10.0.0.10",
    "DNS_MODE": "external",
    "ADMIN_EMAIL": "owner@lab.example",
    "ADMIN_PASSWORD": "correct-horse-1",
}


def run_setup(tmp: pathlib.Path, optional: str = "agents", overrides: dict | None = None, check: bool = True):
    shutil.copy(SETUP, tmp / "setup.sh")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("FORGEHANDS_") and k not in ("COMPOSE_PROFILES", "OPTIONAL_SERVICES")}
    env.update(BASE_ENV)
    env["OPTIONAL_SERVICES"] = optional
    if "agents" in optional.split(","):
        env.update(FACTORY_INPUTS)
    env.update(overrides or {})
    result = subprocess.run(["bash", "setup.sh", "--yes"], cwd=tmp, env=env, check=check,
                            capture_output=True, text=True)
    return result


def read_env(tmp: pathlib.Path) -> dict:
    text = (tmp / ".env").read_text()
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))


def block(text: str, service: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(f"  {service}:")
    out = []
    for line in lines[start + 1:]:
        if line and not line.startswith("    "):
            break
        out.append(line)
    return out


def environment(lines: list[str]) -> dict:
    out = {}
    try:
        start = lines.index("    environment:")
    except ValueError:
        return out
    for line in lines[start + 1:]:
        if not line.startswith("      "):
            break
        match = re.match(r" {6}([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if match:
            out[match.group(1)] = match.group(2).strip()
    return out


def volumes(lines: list[str]) -> list[str]:
    out = []
    try:
        start = lines.index("    volumes:")
    except ValueError:
        return out
    for line in lines[start + 1:]:
        if not line.startswith("      "):
            break
        stripped = line.strip()
        if stripped.startswith("- "):
            out.append(stripped[2:])
    return out


class FreshOptInTests(unittest.TestCase):
    def test_agents_is_the_opt_in_and_writes_the_factory_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = pathlib.Path(temporary)
            result = run_setup(tmp)
            env = read_env(tmp)
            mode = stat.S_IMODE((tmp / ".env").stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(env["FORGEHANDS_BOOTSTRAP"], "1")
        for name, value in FACTORY_INPUTS.items():
            self.assertEqual(env[name], value)
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertEqual(env["FORGEHANDS_AGENT_LOGIN"], "homegraf-agent")
        self.assertEqual(env["FORGEHANDS_HUMAN_LOGIN"], "owner")
        self.assertEqual(env["FORGEHANDS_ALLOWED_SENDERS"], "owner,homegraf-agent")
        self.assertEqual(env["FORGEHANDS_AGENT_SECRETS"], "FORGEJO_TOKEN,FORGEJO_URL")
        self.assertEqual((env["FORGEHANDS_MAX_CONCURRENT_PER_TIER"], env["FORGEHANDS_MAX_CONCURRENT"],
                          env["FORGEHANDS_MAX_RUNS_PER_HOUR"], env["FORGEHANDS_MAX_RUN_MINUTES"],
                          env["FORGEHANDS_MAX_ROUNDS"]), ("2", "4", "30", "120", "3"))

    def test_direct_mode_stays_off_until_the_four_ids_are_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = pathlib.Path(temporary)
            run_setup(tmp)
            text = (tmp / ".env").read_text()
            env = read_env(tmp)
        self.assertEqual(env.get("FORGEHANDS_OPENHANDS_API_URL", ""), "")
        self.assertEqual(env.get("FORGEHANDS_LABEL_ROUTES", ""), "")
        self.assertIn("# FORGEHANDS_OPENHANDS_API_URL=http://openhands:8000", text)
        self.assertNotRegex(text, r'"profile"\s*:\s*"(planner|worker|worker-strong|reviewer)"')
        self.assertIn("<planner-id>", text)

    def test_a_fresh_own_repository_policy_protects_the_deploy_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = pathlib.Path(temporary)
            run_setup(tmp)
            policy = json.loads(read_env(tmp)["FORGEHANDS_MERGE_POLICY"])
        self.assertEqual(list(policy), ["owner/infrastructure"])
        denied = policy["owner/infrastructure"]["deny"]
        for path in (".forgejo/workflows/ci.yaml", ".github/workflows/ci.yml", "AGENTS.md", "CLAUDE.md",
                     "CONTRIBUTING.md", ".agents/skills/qa-changes/SKILL.md", ".claude/settings.json",
                     "compose.yaml", "stacks/demo/compose.yaml", "setup.sh", "scripts/deploy.sh",
                     "infra/ci/validate.sh", "stacks/demo/stack.yaml"):
            self.assertTrue(any(_matches(pattern, path) for pattern in denied), path)
        self.assertEqual(denied, list(DEFAULT_MERGE_DENY))

    def test_an_explicit_empty_policy_stays_manual(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = pathlib.Path(temporary)
            run_setup(tmp, overrides={"FORGEHANDS_MERGE_POLICY": "{}"})
            self.assertEqual(read_env(tmp)["FORGEHANDS_MERGE_POLICY"], "{}")

    def test_an_explicit_policy_is_never_replaced_by_the_default(self):
        policy = '{"owner/infrastructure":{"deny":["custom/**"]}}'
        with tempfile.TemporaryDirectory() as temporary:
            tmp = pathlib.Path(temporary)
            run_setup(tmp, overrides={"FORGEHANDS_MERGE_POLICY": policy})
            self.assertEqual(read_env(tmp)["FORGEHANDS_MERGE_POLICY"], policy)

    def test_a_second_allowed_repository_stays_manual(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = pathlib.Path(temporary)
            run_setup(tmp, overrides={"FORGEHANDS_ALLOWED_REPOS": "owner/infrastructure,owner/other"})
            policy = json.loads(read_env(tmp)["FORGEHANDS_MERGE_POLICY"])
        self.assertEqual(list(policy), ["owner/infrastructure"])

    def test_the_agents_profile_without_factory_inputs_fails_before_writing_env(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = pathlib.Path(temporary)
            result = run_setup(tmp, overrides={name: "" for name in FACTORY_INPUTS}, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("FORGEHANDS_STRONG_MODEL", result.stderr)
            self.assertFalse((tmp / ".env").exists())

    def test_the_operator_token_is_not_the_factory_agent_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = pathlib.Path(temporary)
            result = run_setup(tmp, overrides={"FORGEJO_TOKEN": "operator-token-123"})
            env = read_env(tmp)
        self.assertEqual(env["FORGEJO_TOKEN"], "operator-token-123")
        self.assertNotIn("FORGEHANDS_FORGEJO_AGENT_TOKEN", env)
        self.assertNotIn("operator-token-123", result.stdout + result.stderr)

    def test_a_legacy_setup_does_not_opt_in_and_keeps_the_old_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = pathlib.Path(temporary)
            run_setup(tmp, optional="")
            env = read_env(tmp)
        self.assertNotIn("FORGEHANDS_BOOTSTRAP", env)
        self.assertNotIn("FORGEHANDS_MERGE_POLICY", env)
        self.assertNotIn("FORGEHANDS_STRONG_MODEL", env)
        self.assertEqual(env["FORGEHANDS_ALLOWED_REPOS"], "owner/infrastructure")
        self.assertIn("FORGEHANDS_FORGEJO_WEBHOOK_SECRET", env)
        self.assertIn("FORGEHANDS_OPENHANDS_WEBHOOK_SECRET", env)

    def test_an_existing_env_is_never_rewritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = pathlib.Path(temporary)
            (tmp / ".env").write_text("ADMIN_EMAIL=owner@lab.example\nKEEP=me\n")
            before = (tmp / ".env").read_bytes()
            result = run_setup(tmp, check=False)
            after = (tmp / ".env").read_bytes()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(after, before)


class FreshDeploymentIntegrationTests(unittest.TestCase):
    def test_the_fresh_rendered_bootstrap_environment_parses_and_the_agent_identity_supplies_the_token(self):
        docker = shutil.which("docker")
        if docker is None:
            self.skipTest("docker is not installed")
        try:
            version = subprocess.run([docker, "compose", "version"], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest("docker compose is not available")
        if version.returncode != 0:
            self.skipTest("docker compose is not available")
        with tempfile.TemporaryDirectory() as temporary:
            tmp = pathlib.Path(temporary)
            run_setup(tmp, overrides={"FORGEJO_TOKEN": "operator-token-123"})
            fresh = read_env(tmp)
            rendered = subprocess.run([docker, "compose", "--env-file", str(tmp / ".env"), "--profile", "agents",
                                       "-f", str(COMPOSE), "config", "--format", "json"],
                                      cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            document = json.loads(rendered.stdout)
        env = {name: "" if value is None else str(value)
               for name, value in document["services"]["bootstrap"]["environment"].items()}
        self.assertNotIn("FORGEJO_TOKEN", env)
        self.assertEqual(env["FORGEHANDS_FORGEJO_AGENT_TOKEN"], "")
        cfg = FactoryConfig.from_env(env, "owner/infrastructure")
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.forgejo_agent_token, "")
        self.assertEqual(cfg.allowed_senders, ("owner", "homegraf-agent"))

        stack = Config.from_env(fresh)
        forgejo = FakeAgentForgejo()
        forgejo.admin, forgejo.password = stack.forgejo_user, stack.admin_password
        server = Server(forgejo)
        self.addCleanup(server.close)
        stack.forgejo_url = server.url
        forgejo.repos["owner/infrastructure"] = {"empty": True, "files": {}, "private": True, "collaborators": {}}
        store = Store()
        settings = ensure_agent_user(stack, store, repositories=("owner/infrastructure",))
        token = store.get(AGENT_TOKEN)
        self.assertEqual(settings.login, AGENT_LOGIN)
        self.assertIn(settings.login, forgejo.users)
        self.assertIn(token, forgejo.user_tokens[AGENT_LOGIN].values())
        self.assertNotEqual(token, "operator-token-123")
        supplied = FactoryConfig.from_env({**env, "FORGEHANDS_FORGEJO_AGENT_TOKEN": token}, "owner/infrastructure")
        self.assertEqual(supplied.forgejo_agent_token, token)


class ComposeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = COMPOSE.read_text()
        cls.forgehands = block(cls.text, "forgehands")
        cls.bootstrap = block(cls.text, "bootstrap")
        cls.env = environment(cls.forgehands)

    def test_forgehands_is_pinned_to_a_supported_release(self):
        image = next(line.strip() for line in self.forgehands if line.strip().startswith("image:"))
        self.assertIn("${FORGEHANDS_IMAGE:-ghcr.io/belphegorprime/forgehands:v", image)
        version = re.search(r"forgehands:(v[0-9.]+)\}", image).group(1)
        self.assertGreaterEqual(tuple(int(part) for part in version[1:].split(".")), (0, 4, 2))

    def test_forgehands_release_knows_the_human_login(self):
        image = next(line.strip() for line in self.forgehands if line.strip().startswith("image:"))
        match = re.search(r"forgehands:v(\d+)\.(\d+)\.(\d+)\}", image)
        self.assertIsNotNone(match, image)
        self.assertGreaterEqual(tuple(int(part) for part in match.groups()), (0, 2, 19))

    def test_the_launcher_starts_the_original_image_command(self):
        joined = "\n".join(self.forgehands)
        self.assertIn('entrypoint: ["node", "/factory/forgehands-entrypoint.mjs"]', joined)
        self.assertIn('command: ["node", "src/index.ts"]', joined)
        self.assertNotIn("src/index.mjs", joined)
        mounts = volumes(self.forgehands)
        self.assertIn("./scripts/forgehands-entrypoint.mjs:/factory/forgehands-entrypoint.mjs:ro", mounts)

    def test_forgehands_reads_the_handoff_read_only_and_never_bootstrap_state(self):
        mounts = volumes(self.forgehands)
        self.assertIn("factory-runtime:/factory-runtime:ro", mounts)
        self.assertNotIn("bootstrap-state:/state:ro", mounts)
        self.assertIn("forgehands-data:/app/data", mounts)
        self.assertIn("forgehands-workspace:/workspace", mounts)

    def test_the_bootstrap_alone_writes_the_separate_runtime_volume(self):
        self.assertIn("<<: [*bootstrap-env, *factory-env]", "\n".join(self.bootstrap))
        mounts = volumes(self.bootstrap)
        self.assertIn("factory-runtime:/factory-runtime", mounts)
        self.assertNotIn("factory-runtime:/factory-runtime:ro", mounts)
        self.assertIn("bootstrap-state:/state", mounts)
        self.assertIn("  factory-runtime:", self.text.splitlines())

    def test_forgehands_gets_the_allowlists_limits_and_policy(self):
        expected = {
            "FORGEHANDS_BOOTSTRAP": "${FORGEHANDS_BOOTSTRAP:-}",
            "FORGEJO_URL": "${FORGEHANDS_FORGEJO_URL:-http://forgejo:3000}",
            "OPENHANDS_API_URL": "${FORGEHANDS_OPENHANDS_API_URL:-}",
            "OPENHANDS_API_KEY": "${OPENHANDS_LOCAL_BACKEND_API_KEY:-}",
            "AGENT_SECRETS": "${FORGEHANDS_AGENT_SECRETS:-}",
            "ALLOWED_REPOS": "${FORGEHANDS_ALLOWED_REPOS:-}",
            "ALLOWED_SENDERS": "${FORGEHANDS_ALLOWED_SENDERS:-}",
            "HUMAN_LOGIN": "${FORGEHANDS_HUMAN_LOGIN:-}",
            "MAX_CONCURRENT_PER_TIER": "${FORGEHANDS_MAX_CONCURRENT_PER_TIER:-2}",
            "MAX_CONCURRENT": "${FORGEHANDS_MAX_CONCURRENT:-4}",
            "MAX_RUNS_PER_HOUR": "${FORGEHANDS_MAX_RUNS_PER_HOUR:-30}",
            "MAX_RUN_MINUTES": "${FORGEHANDS_MAX_RUN_MINUTES:-120}",
            "MAX_ROUNDS": "${FORGEHANDS_MAX_ROUNDS:-3}",
            "MERGE_POLICY": "${FORGEHANDS_MERGE_POLICY:-}",
            "SPLIT_LABEL": "${FORGEHANDS_SPLIT_LABEL:-ai:plan}",
            "REMIND_AFTER_DAYS": "${FORGEHANDS_REMIND_AFTER_DAYS:-2}",
            "ERROR_RETRIES": "${FORGEHANDS_ERROR_RETRIES:-1}",
            "QUOTA_HOLD_MAX_HOURS": "${FORGEHANDS_QUOTA_HOLD_MAX_HOURS:-8}",
            "QUOTA_GIVE_UP_HOURS": "${FORGEHANDS_QUOTA_GIVE_UP_HOURS:-24}",
            "APPROVAL_GRACE_SECONDS": "${FORGEHANDS_APPROVAL_GRACE_SECONDS:-90}",
            "REPO_WEIGHTS": "${FORGEHANDS_REPO_WEIGHTS:-}",
            "STATUS_TOKEN": "${FORGEHANDS_STATUS_TOKEN:-}",
        }
        for name, value in expected.items():
            self.assertEqual(self.env.get(name), value, name)
        self.assertNotIn("TIER_WEEKLY_BUDGETS", self.text)

    def test_forgehands_gets_only_the_agent_token_not_the_operator_token(self):
        self.assertEqual(self.env["FORGEJO_TOKEN"], "${FORGEHANDS_FORGEJO_AGENT_TOKEN:-}")
        self.assertNotIn("${FORGEJO_TOKEN", self.env["FORGEJO_TOKEN"])
        self.assertNotIn("FORGEJO_TOKEN: ${FORGEJO_TOKEN", "\n".join(self.forgehands))

    def test_the_runtime_file_path_and_wait_are_operator_switches(self):
        self.assertEqual(self.env["FORGEHANDS_RUNTIME_FILE"], "${FORGEHANDS_RUNTIME_FILE:-/factory-runtime/forgehands.json}")
        self.assertEqual(self.env["FORGEHANDS_RUNTIME_WAIT_MS"], "${FORGEHANDS_RUNTIME_WAIT_MS:-120000}")

    def test_the_bootstrap_gets_the_settings_that_produce_the_handoff(self):
        start = self.text.index("x-factory-env: &factory-env")
        anchor = self.text[start:self.text.index("\n\n", start)]
        for name in ("FORGEHANDS_BOOTSTRAP", "FORGEHANDS_OPENHANDS_API_URL", "OPENHANDS_LOCAL_BACKEND_API_KEY",
                     "FORGEHANDS_FORGEJO_AGENT_TOKEN", "FORGEHANDS_FORGEJO_WEBHOOK_SECRET",
                     "FORGEHANDS_AGENT_LOGIN", "FORGEHANDS_HUMAN_LOGIN", "FORGEHANDS_ALLOWED_REPOS",
                     "FORGEHANDS_ALLOWED_SENDERS", "FORGEHANDS_AGENT_SECRETS", "FORGEHANDS_MERGE_POLICY",
                     "FORGEHANDS_STRONG_MODEL", "FORGEHANDS_CHEAP_MODEL", "FORGEHANDS_GATEWAY_URL",
                     "FORGEHANDS_GATEWAY_API_KEY", "FORGEHANDS_RUNTIME_DIR", "FORGEHANDS_MAX_ROUNDS"):
            self.assertIn(f"{name}:", anchor, name)

    def test_forgehands_keeps_its_protections_and_healthcheck(self):
        joined = "\n".join(self.forgehands)
        for line in ("read_only: true", "cap_drop: [ALL]", "no-new-privileges:true", "/healthz",
                     "start_period: 15s", "init: true"):
            self.assertIn(line, joined)
        self.assertIn("depends_on:", joined)
        self.assertIn("condition: service_healthy", joined)

    def test_the_startup_order_is_openhands_then_bootstrap_then_forgehands(self):
        joined = "\n".join(self.forgehands)
        self.assertIn("openhands:\n        condition: service_healthy", joined)
        self.assertIn("bootstrap:\n        condition: service_completed_successfully", joined)
        bootstrap = "\n".join(self.bootstrap)
        self.assertIn("openhands:\n        condition: service_healthy\n        required: false", bootstrap)
        self.assertIn("factory-runtime-init:\n        condition: service_completed_successfully\n        required: false",
                      bootstrap)
        self.assertNotIn("forgehands:", bootstrap)

    def test_the_runtime_volume_is_writable_by_the_bootstrap_uid(self):
        init = "\n".join(block(self.text, "factory-runtime-init"))
        self.assertIn("profiles: [agents]", init)
        self.assertIn('user: "0:0"', init)
        self.assertIn("read_only: true", init)
        self.assertIn("cap_drop: [ALL]", init)
        self.assertIn("cap_add: [CHOWN, FOWNER]", init)
        self.assertIn("no-new-privileges:true", init)
        self.assertIn("factory-runtime:/factory-runtime", volumes(block(self.text, "factory-runtime-init")))
        self.assertIn("os.chown('/factory-runtime', 1000, 1000)", init)
        self.assertIn("0o700", init)
        self.assertNotIn("bootstrap-state", init)

    def test_legacy_webhook_mode_defaults_are_unchanged(self):
        self.assertEqual(self.env["OPENHANDS_WEBHOOK_SECRET"], "${FORGEHANDS_OPENHANDS_WEBHOOK_SECRET:-}")
        self.assertEqual(self.env["OPENHANDS_WEBHOOK_URL"], "${FORGEHANDS_OPENHANDS_WEBHOOK_URL:-}")
        self.assertEqual(self.env["FORGEJO_WEBHOOK_SECRET"], "${FORGEHANDS_FORGEJO_WEBHOOK_SECRET:-}")
        self.assertEqual(self.env["LABEL_ROUTES"], "${FORGEHANDS_LABEL_ROUTES:-}")
        self.assertEqual(self.env["WORKSPACE_DIR"], "/workspace")
        self.assertEqual(self.env["JOBS_FILE"], "/app/data/jobs.json")

    def test_the_rendered_compose_config_is_valid(self):
        docker = shutil.which("docker")
        if docker is None:
            self.skipTest("docker is not installed")
        try:
            version = subprocess.run([docker, "compose", "version"], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest("docker compose is not available")
        if version.returncode != 0:
            self.skipTest("docker compose is not available")
        env = {**os.environ,
               "ADMIN_EMAIL": "owner@lab.example", "ADMIN_PASSWORD": "correct-horse-1",
               "HOST_IP": "10.0.0.10", "DATA_DIR": "/tmp/homegraf-data",
               "KOMODO_WEBHOOK_SECRET": "webhook", "KOMODO_PASSKEY": "passkey",
               "FORGEJO_SECRET_KEY": "secret", "FORGEJO_DB_PASSWORD": "db",
               "KOMODO_DB_PASSWORD": "db", "KOMODO_JWT_SECRET": "jwt",
               "INFISICAL_DB_PASSWORD": "db", "INFISICAL_ENCRYPTION_KEY": "key",
               "INFISICAL_AUTH_SECRET": "auth"}
        agents = subprocess.run([docker, "compose", "--profile", "agents", "config"], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(agents.returncode, 0, agents.stderr)
        legacy = subprocess.run([docker, "compose", "config"], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertNotIn("\n  forgehands:", legacy.stdout)
        self.assertNotIn("\n  factory-runtime-init:", legacy.stdout)


class RuntimePermissionTests(unittest.TestCase):
    def test_the_handoff_is_private_to_the_bootstrap_user(self):
        setpriv = shutil.which("setpriv")
        if os.geteuid() != 0 or setpriv is None:
            self.skipTest("needs root to switch to the bootstrap uid")
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            os.chown(directory, 1000, 1000)
            os.chmod(directory, 0o700)
            handoff = directory / "forgehands.json"
            writer = ("import os; fd = os.open(%r, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); "
                      "os.write(fd, b'{\"ready\": true, \"env\": {}}'); os.close(fd)") % str(handoff)
            subprocess.run([setpriv, "--reuid=1000", "--regid=1000", "--clear-groups", sys.executable, "-c", writer],
                           check=True)
            self.assertEqual(stat.S_IMODE(handoff.stat().st_mode), 0o600)
            self.assertEqual(handoff.stat().st_uid, 1000)
            reader = f"open({str(handoff)!r}).read()"
            allowed = subprocess.run([setpriv, "--reuid=1000", "--regid=1000", "--clear-groups",
                                      sys.executable, "-c", reader], capture_output=True)
            self.assertEqual(allowed.returncode, 0)
            denied = subprocess.run([setpriv, "--reuid=65534", "--regid=65534", "--clear-groups",
                                     sys.executable, "-c", reader], capture_output=True)
            self.assertNotEqual(denied.returncode, 0)


def _matches(pattern: str, path: str) -> bool:
    source = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            source.append(".*")
            i += 2
        elif pattern[i] == "*":
            source.append("[^/]*")
            i += 1
        else:
            source.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(source), path) is not None


if __name__ == "__main__":
    unittest.main()
