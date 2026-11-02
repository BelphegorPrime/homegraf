import base64
import pathlib
import unittest

from hgboot.config import ConfigError
from hgboot.forgejo import Forgejo
from hgboot.hosts import AGENT_IMAGE, Hosts, agent_compose, check_address, check_name
from hgboot.komodo import Komodo, update_all_stages
from hgboot.main import add_host_cmd, agent_compose_cmd
from hgboot.state import State
from hgboot.steps import StepError

from .fakes import FakeForgejo, FakeKomodo, Server
from .helpers import PASSWORD, config, no_wait

SERVERS = '# hosts\nservers:\n  "Local": { address: "192.168.1.10", status: verified }\n'


class HostsTests(unittest.TestCase):
    def setUp(self):
        self.komodo_fake = FakeKomodo(password=PASSWORD)
        self.komodo_fake.procedures["update-all"] = {"stages": update_all_stages(), "webhook_enabled": True}
        self.forgejo_fake = FakeForgejo(password=PASSWORD)
        self.forgejo_fake.repos["owner/infrastructure"] = {"empty": False, "private": True,
                                                           "files": {"inventory/servers.yaml": base64.b64encode(SERVERS.encode()).decode()}}
        self.ksrv, self.fsrv = Server(self.komodo_fake), Server(self.forgejo_fake)
        self.addCleanup(self.ksrv.close)
        self.addCleanup(self.fsrv.close)
        self.cfg = config(komodo_url=self.ksrv.url, forgejo_url=self.fsrv.url, komodo_passkey="pass-1")
        self.state = State(f"{self.cfg.state_dir}/state.json")
        self.state.set("forgejo", "token", "forgejo-token")

    def hosts(self):
        return Hosts(self.cfg, self.state, Komodo(self.cfg, self.state, wait=no_wait, sleep=lambda s: None),
                     Forgejo(self.cfg, self.state, wait=no_wait), wait=no_wait)

    def servers_file(self):
        return base64.b64decode(self.forgejo_fake.repos["owner/infrastructure"]["files"]["inventory/servers.yaml"]).decode()

    def test_registers_the_agent_clones_the_repository_and_lists_the_host(self):
        self.hosts().add("worker-1", "10.0.0.12")
        self.assertEqual(self.komodo_fake.servers["worker-1"]["config"], {"address": "https://10.0.0.12:8120", "enabled": True})
        # the agent there cannot resolve forgejo:3000: it clones from the address of this host
        self.assertEqual([(a["domain"], a["username"], a["token"]) for a in self.komodo_fake.accounts], [("192.168.1.10:3000", "owner", "forgejo-token")])
        repo = self.komodo_fake.repos["infrastructure-worker-1"]
        self.assertEqual(repo["config"], {"server_id": "s2", "git_provider": "192.168.1.10:3000", "git_https": False, "git_account": "owner",
                                          "repo": "owner/infrastructure", "branch": "main"})
        self.assertEqual(repo["info"]["latest_hash"], "25de55c")
        pull = self.komodo_fake.procedures["update-all"]["stages"][0]
        self.assertEqual([e["execution"]["params"]["repo"] for e in pull["executions"]], ["infrastructure", "infrastructure-worker-1"])
        self.assertEqual(self.servers_file(), SERVERS + '  "worker-1": { address: "10.0.0.12", status: verified }\n')
        self.assertEqual(self.forgejo_fake.puts, [("inventory/servers.yaml", "Add the host worker-1", "main")])

    def test_a_second_run_changes_nothing(self):
        self.hosts().add("worker-1", "10.0.0.12")
        before = (self.servers_file(), len(self.komodo_fake.procedures["update-all"]["stages"][0]["executions"]), len(self.komodo_fake.accounts))
        self.ksrv.requests.clear()
        self.fsrv.requests.clear()
        self.hosts().add("worker-1", "10.0.0.12")
        self.assertEqual(before, (self.servers_file(), len(self.komodo_fake.procedures["update-all"]["stages"][0]["executions"]), len(self.komodo_fake.accounts)))
        self.assertEqual([r.path for r in self.ksrv.requests if r.path.startswith(("/write/", "/execute/"))], [])
        self.assertEqual([r for r in self.fsrv.requests if r.method in ("PUT", "POST")], [])

    def test_a_new_address_updates_the_server_but_not_a_hand_edited_entry(self):
        self.hosts().add("worker-1", "10.0.0.12")
        with self.assertRaisesRegex(StepError, "another address"):
            self.hosts().add("worker-1", "10.0.0.13")
        self.assertEqual(self.komodo_fake.servers["worker-1"]["config"]["address"], "https://10.0.0.13:8120")   # Komodo follows, the repository is the user's

    def test_an_agent_that_does_not_answer_says_what_to_check(self):
        self.komodo_fake.new_server_state = "NotOk"
        with self.assertRaisesRegex(StepError, "port 8120"):
            self.hosts().add("worker-1", "10.0.0.12")
        self.assertNotIn("infrastructure-worker-1", self.komodo_fake.repos)      # nothing is cloned before the agent is there
        self.assertEqual(self.servers_file(), SERVERS)

    def test_a_changed_update_all_or_servers_file_is_reported_not_guessed(self):
        self.komodo_fake.procedures["update-all"] = {"stages": [{"name": "Mine", "enabled": True, "executions": []}]}
        with self.assertRaisesRegex(StepError, "no stage 'Pull'"):
            self.hosts().add("worker-1", "10.0.0.12")
        self.komodo_fake.procedures["update-all"] = {"stages": update_all_stages()}
        self.forgejo_fake.repos["owner/infrastructure"]["files"]["inventory/servers.yaml"] = base64.b64encode(b"hosts: []\n").decode()
        with self.assertRaisesRegex(StepError, "no 'servers:'"):
            self.hosts().add("worker-1", "10.0.0.12")

    def test_names_and_addresses(self):
        for bad in ("Worker", "a", "-x", "x y", "local", "x" * 40):
            with self.assertRaises(StepError, msg=bad):
                check_name(bad)
        self.assertEqual(check_name("worker-1"), "worker-1")
        for bad in ("", "10.0.0.1:80", "a b", "http://x", "-x"):
            with self.assertRaises(StepError, msg=bad):
                check_address(bad)
        self.assertEqual((check_address("10.0.0.12"), check_address("nas.lan")), ("10.0.0.12", "nas.lan"))


class AgentComposeTests(unittest.TestCase):
    def test_is_the_agent_of_this_stack_reachable_from_it(self):
        text = agent_compose(config(komodo_passkey="pass-1"), "worker-1")
        for want in ("name: homegraf-agent", "PERIPHERY_PASSKEYS: pass-1", "PERIPHERY_ROOT_DIRECTORY: /srv/homegraf/data/periphery",
                     "- /srv/homegraf/data/periphery:/srv/homegraf/data/periphery", '- "8120:8120"', f"image: {AGENT_IMAGE}"):
            self.assertIn(want, text)

    def test_uses_the_agent_image_of_compose_yaml(self):
        compose = (pathlib.Path(__file__).resolve().parents[2] / "compose.yaml").read_text()
        self.assertIn(f"image: {AGENT_IMAGE}\n", compose)

    def test_needs_the_passkey(self):
        with self.assertRaisesRegex(StepError, "KOMODO_PASSKEY"):
            agent_compose(config(), "worker-1")

    def test_the_commands_refuse_bad_input_before_they_touch_anything(self):
        env = {"ADMIN_EMAIL": "a@b.c", "ADMIN_PASSWORD": PASSWORD, "DOMAIN": "home.lab", "HOST_IP": "10.0.0.1", "DATA_DIR": "/srv/hg",
               "KOMODO_WEBHOOK_SECRET": "x", "KOMODO_PASSKEY": "pk"}
        self.assertEqual(agent_compose_cmd([], env), 2)
        self.assertEqual(agent_compose_cmd(["Bad Name"], env), 2)
        self.assertEqual(agent_compose_cmd(["worker-1"], {k: v for k, v in env.items() if k != "KOMODO_PASSKEY"}), 2)
        self.assertEqual(add_host_cmd(["worker-1"], env), 2)
        self.assertEqual(add_host_cmd(["worker-1", "no good"], env), 2)


if __name__ == "__main__":
    unittest.main()
