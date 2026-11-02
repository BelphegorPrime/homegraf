"""The whole bootstrap against fake services, through run() the way the container calls it."""
import contextlib
import io
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from hgboot import main as hgmain
from hgboot.http import wait_for
from hgboot.state import State

from .fakes import FakeAdGuard, FakeForgejo, FakeInfisical, FakeKomodo, FakeNpm, Server
from .helpers import PASSWORD


INGRESS_YAML = """issuers:
  homelab-private-ca:
    type: infisical
    project_id: null           # Infisical project
    profile_id: null
    application_id: null
"""


EXTERNAL_YAML = """apiVersion: homelab.ingress/v1
namespace: infrastructure
services:
- id: git        # Forgejo
  stack: external
"""


class Stack:
    def __init__(self, dns_mode="external"):
        self.forgejo, self.komodo, self.npm, self.infisical, self.adguard = FakeForgejo(password=PASSWORD), FakeKomodo(password=PASSWORD), FakeNpm(), FakeInfisical(), FakeAdGuard(password=PASSWORD)
        self.servers = [Server(self.forgejo), Server(self.komodo), Server(self.npm), Server(self.npm.proxy), Server(self.infisical), Server(self.adguard)]
        self.state_dir = tempfile.mkdtemp()
        shutil.copytree(pathlib.Path(__file__).parent.parent / "seed-stub", self.state_dir + "/seed-stub")      # /seed-stub in the image
        seed = pathlib.Path(self.state_dir) / "seed"
        (seed / "inventory").mkdir(parents=True)
        (seed / "README.md").write_text("# seed\n")
        (seed / "inventory/ingress.yaml").write_text(INGRESS_YAML)
        (seed / "external").mkdir()
        (seed / "external/ingress.yaml").write_text(EXTERNAL_YAML)
        f, k, n, p, i, a = (s.url for s in self.servers)
        self.env = {"ADMIN_USER": "admin", "ADMIN_EMAIL": "admin@example.org", "ADMIN_PASSWORD": PASSWORD, "DOMAIN": "home.lab", "HOST_IP": "192.168.1.10",
                    "DNS_MODE": dns_mode, "DATA_DIR": "/srv/hg", "KOMODO_WEBHOOK_SECRET": "hook-secret", "STATE_DIR": self.state_dir, "SEED_DIR": self.state_dir + "/seed", "CREDENTIALS_DIR": self.state_dir + "/credentials",
                    "FORGEJO_URL": f, "KOMODO_URL": k, "NPM_URL": n, "NPM_PROXY_URL": p, "INFISICAL_URL": i, "ADGUARD_URL": a, "ADGUARD_SETUP_URL": a}

    def close(self):
        for s in self.servers:
            s.close()

    def run(self, step=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("hgboot.http.time.sleep", lambda s: None), mock.patch.dict(os.environ, {"HGBOOT_QUIET": ""}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = (step or hgmain.run)(self.env)
        return code, out.getvalue(), err.getvalue()

    def certificates(self):
        """The second service, `python -m hgboot certificates`, which runs once homegraf is up."""
        return self.run(hgmain.certificates)


class RunTests(unittest.TestCase):
    def stack(self, **kw):
        s = Stack(**kw)
        self.addCleanup(s.close)
        return s

    def test_everything_is_configured_and_homegraf_is_handed_what_it_needs(self):
        s = self.stack()
        code, out, err = s.run()
        self.assertEqual((code, err), (0, ""))
        with open(f"{s.state_dir}/homegraf.env") as f:
            env = dict(line.split("=", 1) for line in f.read().splitlines() if line and not line.startswith("#"))
        self.assertEqual(env["HOMEGRAF_CLONE_URL"], f"{s.env['FORGEJO_URL']}/owner/infrastructure.git")
        self.assertEqual((env["FORGEJO_TOKEN"], env["INFISICAL_BOOTSTRAP_CLIENT_ID"], env["INFISICAL_BOOTSTRAP_CLIENT_SECRET"]), ("tok-1", "client-1", "secret-1"))
        self.assertEqual((env["OIDC_ISSUER"], env["HOMEGRAF_URL"], env["OIDC_ADMINS"], env["OIDC_LABEL"]), ("http://git.home.lab", "http://homegraf.home.lab", "owner", "Forgejo"))
        self.assertEqual((env["OIDC_CLIENT_ID"], env["OIDC_CLIENT_SECRET"]), ("cid-1", "csec-1"))
        self.assertTrue(env["KOMODO_API_KEY"].startswith("K_") and env["KOMODO_API_SECRET"].startswith("S_"))
        self.assertEqual(oct(os.stat(f"{s.state_dir}/homegraf.env").st_mode & 0o777), "0o600")
        self.assertIn("update-all", s.komodo.procedures)
        self.assertEqual(len(s.npm.hosts), 5)
        self.assertEqual(s.adguard.rewrites, [])                     # DNS_MODE=external: AdGuard is not touched
        self.assertEqual(s.forgejo.commits[0][:1], ["README.md"])    # the stub seed

    def test_nothing_secret_is_printed(self):
        s = self.stack()
        _, out, err = s.run()
        secrets = [PASSWORD, "hook-secret", "tok-1", "secret-1", "csec-1"] + [k["secret"] for k in s.komodo.keys]
        for secret in secrets:
            self.assertNotIn(secret, out + err)

    def test_the_summary_says_where_to_go_and_what_is_left(self):
        s = self.stack()
        _, out, _ = s.run()
        for text in ("http://git.home.lab", "http://homegraf.home.lab", "http://192.168.1.10:8097", "admin@example.org or the user name owner",
                     "*.home.lab -> 192.168.1.10", "no token to type anywhere", "http://git.home.lab/owner/infrastructure", "http://192.168.1.10:3000/owner/infrastructure"):
            self.assertIn(text, out)

    def test_a_second_run_is_quiet_and_changes_nothing(self):
        s = self.stack()
        self.assertEqual(s.run()[0], 0)
        with open(f"{s.state_dir}/homegraf.env") as f:
            before = f.read()
        for srv in s.servers:
            srv.requests.clear()
        self.assertEqual(s.run()[0], 0)
        with open(f"{s.state_dir}/homegraf.env") as f:
            self.assertEqual(f.read(), before)
        writes = [(r.method, r.path) for srv in s.servers for r in srv.requests
                  if r.method in ("POST", "PUT", "DELETE") and not r.path.endswith(("/tokens", "/login", "LoginLocalUser", "/read/GetVersion", "GetUpdate")) and not r.path.startswith(("/read/", "/api/tokens"))]
        self.assertEqual(writes, [])

    def test_adguard_mode_sets_up_the_dns(self):
        s = self.stack(dns_mode="adguard")
        code, out, _ = s.run()
        self.assertEqual(code, 0)
        self.assertEqual(s.adguard.rewrites, [{"domain": "*.home.lab", "answer": "192.168.1.10"}])
        self.assertIn("point your router's DHCP", out)

    def test_a_failing_step_stops_with_the_cause_and_leaves_what_is_done(self):
        s = self.stack()
        s.npm.users = {"someone@else.org": "x"}
        code, _, err = s.run()
        self.assertEqual(code, 1)
        self.assertIn("set up before with other credentials", err)
        self.assertIn("run it again", err)
        self.assertTrue(State(f"{s.state_dir}/state.json").get("komodo", "api_key"))          # Forgejo and Komodo are done
        self.assertFalse(os.path.exists(f"{s.state_dir}/homegraf.env"))                        # homegraf is not handed half a job

    def test_an_image_without_any_seed_is_an_error_not_an_empty_repository(self):
        s = self.stack()
        shutil.rmtree(s.state_dir + "/seed-stub")
        shutil.rmtree(s.state_dir + "/seed")
        code, _, err = s.run()
        self.assertEqual(code, 1)
        self.assertIn("contains no seed", err)
        self.assertEqual(s.forgejo.repos, {})

    def test_missing_settings_are_a_clear_error(self):
        s = self.stack()
        del s.env["ADMIN_PASSWORD"]
        code, _, err = s.run()
        self.assertEqual(code, 2)
        self.assertIn("ADMIN_PASSWORD is not set", err)

    def test_show_prints_a_generated_infisical_password_or_says_there_is_none(self):
        s = self.stack()
        s.env["ADMIN_PASSWORD"] = "short-pass-12"
        for srv in (s.forgejo, s.komodo):
            srv.password = "short-pass-12"
        s.npm.users = {}
        s.run()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hgmain.show("infisical-password", {"STATE_DIR": s.state_dir})
        self.assertEqual(out.getvalue().strip(), s.infisical.admin_password)
        self.assertNotEqual(s.infisical.admin_password, "short-pass-12")


if __name__ == "__main__":
    unittest.main()
