import os
import stat
import tempfile
import unittest

from hgboot import seed
from hgboot.config import Config, ConfigError
from hgboot.http import Unreachable, request, wait_for
from hgboot.state import State, write_env_file

from .fakes import Server
from .helpers import config


class HttpTests(unittest.TestCase):
    def test_json_basic_auth_headers_and_error_statuses_are_returned_not_raised(self):
        srv = Server(lambda req: (200, {"user": req.basic()[0]}) if req.basic() else (401, {"error": "no"}))
        self.addCleanup(srv.close)
        self.assertEqual(request("GET", srv.url + "/x", basic=("a", "b")).json(), {"user": "a"})
        r = request("GET", srv.url + "/x")
        self.assertEqual((r.status, r.ok, r.json()), (401, False, {"error": "no"}))
        srv.requests.clear()
        request("POST", srv.url + "/y", body={"k": 1})
        self.assertEqual((srv.requests[0].headers["content-type"], srv.requests[0].json), ("application/json", {"k": 1}))

    def test_an_unreachable_service_is_one_error(self):
        with self.assertRaises(Unreachable):
            request("GET", "http://127.0.0.1:9/", timeout=2)

    def test_wait_for_retries_until_true_and_counts_unreachable_as_not_yet(self):
        calls = iter([Unreachable("x"), False, True])
        sleeps = []

        def check():
            v = next(calls)
            if isinstance(v, Exception):
                raise v
            return v
        wait_for(check, "x", sleep=sleeps.append, clock=lambda: 0)
        self.assertEqual(len(sleeps), 2)

    def test_wait_for_gives_up_with_what_it_waited_for(self):
        t = iter(range(0, 100, 10))
        with self.assertRaisesRegex(TimeoutError, "Forgejo did not become ready within 25 s"):
            wait_for(lambda: False, "Forgejo", timeout=25, sleep=lambda s: None, clock=lambda: next(t))


class StateTests(unittest.TestCase):
    def test_state_survives_a_restart_and_is_private(self):
        d = tempfile.mkdtemp()
        s = State(f"{d}/sub/state.json")
        s.set("forgejo", "token", "t1")
        self.assertEqual(State(f"{d}/sub/state.json").get("forgejo", "token"), "t1")
        self.assertEqual(State(f"{d}/sub/state.json").get("komodo", "x", "dflt"), "dflt")
        self.assertEqual(stat.S_IMODE(os.stat(f"{d}/sub/state.json").st_mode), 0o600)

    def test_env_file_is_private_replaced_atomically_and_refuses_line_breaks(self):
        d = tempfile.mkdtemp()
        write_env_file(f"{d}/homegraf.env", {"A": "1", "B": "two=2"})
        with open(f"{d}/homegraf.env") as f:
            self.assertEqual(f.read().splitlines()[1:], ["A=1", "B=two=2"])
        self.assertEqual(stat.S_IMODE(os.stat(f"{d}/homegraf.env").st_mode), 0o600)
        with self.assertRaises(ValueError):
            write_env_file(f"{d}/x.env", {"A": "a\nB=b"})
        self.assertEqual(sorted(os.listdir(d)), ["homegraf.env"])


class ConfigTests(unittest.TestCase):
    ENV = {"ADMIN_EMAIL": "a@b.c", "ADMIN_PASSWORD": "pw", "DOMAIN": "home.lab", "HOST_IP": "1.2.3.4", "DATA_DIR": "/d", "KOMODO_WEBHOOK_SECRET": "s"}

    def test_defaults_and_overrides(self):
        c = Config.from_env({**self.ENV, "FORGEJO_URL": "http://x:1/", "PORT_KOMODO": "19120"})
        self.assertEqual((c.admin_user, c.forgejo_url, c.ports["KOMODO"], c.ports["HTTP"], c.dns_mode), ("admin", "http://x:1", "19120", "80", "external"))
        self.assertEqual((c.repos_dir, c.webhook_url), ("/d/periphery/repos", "http://komodo-core:9120/listener/github/procedure/update-all/main"))

    def test_forgejo_does_not_allow_the_name_admin(self):
        self.assertEqual(Config.from_env(self.ENV).forgejo_user, "owner")
        self.assertEqual(Config.from_env(self.ENV).repository, "owner/infrastructure")
        self.assertEqual(Config.from_env({**self.ENV, "ADMIN_USER": "owner"}).repository, "owner/infrastructure")
        self.assertEqual(Config.from_env({**self.ENV, "ADMIN_USER": "owner"}).clone_url, "http://forgejo:3000/owner/infrastructure.git")

    def test_the_forgejo_administrator_can_be_named_explicitly(self):
        c = Config.from_env({**self.ENV, "FORGEJO_ADMIN_USER": "homelab"})
        self.assertEqual((c.forgejo_user, c.repository), ("homelab", "homelab/infrastructure"))

    def test_missing_values_and_a_bad_dns_mode_are_named(self):
        for name in ("ADMIN_PASSWORD", "DOMAIN", "KOMODO_WEBHOOK_SECRET"):
            with self.assertRaisesRegex(ConfigError, name):
                Config.from_env({k: v for k, v in self.ENV.items() if k != name})
        with self.assertRaisesRegex(ConfigError, "DNS_MODE"):
            Config.from_env({**self.ENV, "DNS_MODE": "nope"})


class SeedTests(unittest.TestCase):
    def make(self, files):
        d = tempfile.mkdtemp()
        for name, data in files.items():
            p = os.path.join(d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
        return d

    def test_placeholders_are_filled_in_text_files_only_and_junk_is_skipped(self):
        d = self.make({"inventory/a.yaml": b"domain: @@DOMAIN@@\nrepo: @@REPOSITORY@@\n", "bin.dat": b"\xff\xfe@@DOMAIN@@", ".git/config": b"x",
                       "pkg/__pycache__/m.pyc": b"x", "README.md": "Grüße @@HOST_IP@@".encode()})
        import pathlib
        subs = seed.substitutions(config(), "Local")
        got = dict(seed.collect(pathlib.Path(d), subs))
        self.assertEqual(sorted(got), ["README.md", "bin.dat", "inventory/a.yaml"])
        self.assertEqual(got["inventory/a.yaml"], b"domain: home.lab\nrepo: owner/infrastructure\n")
        self.assertEqual(got["bin.dat"], b"\xff\xfe@@DOMAIN@@")
        self.assertEqual(got["README.md"].decode(), "Grüße 192.168.1.10")

    def test_an_unknown_placeholder_is_an_error(self):
        import pathlib
        d = self.make({"a.txt": b"@@NOPE@@"})
        with self.assertRaisesRegex(ValueError, "unknown placeholder"):
            seed.collect(pathlib.Path(d), seed.substitutions(config(), "Local"))

    def test_the_real_seed_wins_over_the_stub_and_the_stub_is_valid(self):
        import pathlib
        cfg = config()
        self.assertEqual(str(seed.seed_dir(cfg)), cfg.seed_dir + "-stub")
        os.makedirs(cfg.seed_dir)
        self.assertEqual(str(seed.seed_dir(cfg)), cfg.seed_dir + "-stub")               # empty folder: still the stub
        with open(f"{cfg.seed_dir}/x", "w") as f:
            f.write("x")
        self.assertEqual(str(seed.seed_dir(cfg)), cfg.seed_dir)
        stub = pathlib.Path(__file__).parent.parent / "seed-stub"
        files = dict(seed.collect(stub, seed.substitutions(cfg, "Local")))
        self.assertIn("inventory/homegraf.yaml", files)
        self.assertNotIn(b"@@", b"".join(files.values()))
        self.assertIn(b'domain: "home.lab"', files["inventory/homegraf.yaml"])


if __name__ == "__main__":
    unittest.main()
