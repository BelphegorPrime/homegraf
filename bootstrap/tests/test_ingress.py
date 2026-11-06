"""The certificate authority, ingressctl and what they leave behind, through run() against fake services."""
import base64
import pathlib
import unittest

from hgboot.ingress import credentials_env, patch_ingress_yaml, with_optional_services
from hgboot.steps import StepError

from .helpers import config
from .test_main import EXTERNAL_YAML, INGRESS_YAML, Stack


def file_of(stack, path):
    return base64.b64decode(stack.forgejo.repos["owner/infrastructure"]["files"][path]).decode()


class PatchTests(unittest.TestCase):
    ids = {"project_id": "p-1", "profile_id": "pf-1", "application_id": "a-1"}

    def test_the_three_ids_replace_null_and_comments_stay(self):
        out = patch_ingress_yaml(INGRESS_YAML, self.ids)
        self.assertIn("project_id: p-1           # Infisical project", out)
        self.assertIn("profile_id: pf-1", out)
        self.assertIn("application_id: a-1", out)
        self.assertNotIn("null", out)

    def test_a_file_without_the_keys_is_refused(self):
        with self.assertRaises(StepError):
            patch_ingress_yaml("issuers: {}\n", self.ids)

    def test_credentials_are_quoted_for_a_shell(self):
        out = credentials_env("http://proxy:81/api", "a@b", "it's", "cid", "sec")
        self.assertIn("export NPM_SECRET='it'\\''s'\n", out)


class OptionalServiceTests(unittest.TestCase):
    def test_a_service_of_a_profile_is_added_once(self):
        cfg = config(profiles=("status",))
        out = with_optional_services(EXTERNAL_YAML, cfg)
        self.assertIn("- id: status ", out)
        self.assertIn("domains: [status.home.lab]", out)
        self.assertIn("upstream: {scheme: http, host: kuma, port: 3001}", out)
        self.assertEqual(out.count("id: git "), 1)                       # the seed's own entry is left alone
        self.assertEqual(with_optional_services(out, cfg), out)          # and a second pass adds nothing

    def test_without_the_profile_the_file_is_unchanged(self):
        self.assertEqual(with_optional_services(EXTERNAL_YAML, config()), EXTERNAL_YAML)


class StackTests(unittest.TestCase):
    def stack(self):
        s = Stack()
        self.addCleanup(s.close)
        return s

    def test_the_ca_ingressctl_and_its_credentials_are_set_up(self):
        s = self.stack()
        self.assertEqual(s.run()[0], 0)
        code, out, err = s.certificates()
        self.assertEqual(code, 0, err)
        i = s.infisical
        self.assertEqual([p["type"] for p in i.projects], ["cert-manager", "secret-manager"])   # the CA, and stacks-<server> for the secrets
        self.assertEqual(i.active_project, i.projects[0]["id"])        # applications refuse to work before the project is active
        self.assertEqual([c["name"] for c in i.cas], ["homegraf-root-ca"])
        self.assertEqual([p["name"] for p in i.policies], ["ingressctl"])
        self.assertEqual(i.policies[0]["sans"][0]["allowed"], ["*.home.lab"])
        self.assertEqual(len(i.profiles), 1)
        self.assertEqual(len(i.apps), 1)
        self.assertEqual(len(i.app_members), 1)
        self.assertEqual(i.identities["id2"], {"name": "ingressctl", "organizationId": "org1", "role": "no-access"})   # id1 is homegraf's
        ingress = file_of(s, "inventory/ingress.yaml")
        self.assertIn(f"project_id: {i.projects[0]['id']}", ingress)
        self.assertIn(f"profile_id: {i.profiles[0]['id']}", ingress)
        self.assertIn("BEGIN CERTIFICATE", file_of(s, "inventory/homegraf-root-ca.pem"))
        creds = pathlib.Path(s.state_dir, "credentials/ingressctl.env").read_text()
        self.assertIn("export INFISICAL_CLIENT_ID='client-2'", creds)
        self.assertIn("export NPM_IDENTITY='ingressctl@home.lab'", creds)
        self.assertIn("export NPM_API_URL='" + s.servers[2].url + "/api'", creds)
        self.assertIn("every proxy host of the stack has one from your own CA", out)
        self.assertIn("inventory/homegraf-root-ca.pem", out)

    def test_the_main_run_leaves_the_certificates_to_the_second_service(self):
        s = self.stack()
        self.assertEqual(s.run()[0], 0)
        self.assertEqual((s.infisical.projects, list(s.komodo.stacks)), ([], []))   # homegraf, which ingressctl checks, is not up yet

    def test_the_status_profile_gets_a_proxy_host_an_ingress_entry_and_a_certificate(self):
        s = self.stack()
        s.env["COMPOSE_PROFILES"] = "status"
        self.assertEqual(s.run()[0], 0)
        self.assertIn("status.home.lab", [d for h in s.npm.hosts for d in h["domain_names"]])
        self.assertEqual(s.certificates()[0], 0)
        self.assertIn("- id: status ", file_of(s, "external/ingress.yaml"))
        self.assertIn("infrastructure/status", [c[2] for c in s.komodo.ingress_calls if c[0] == "adopt"])

    def test_the_technical_user_sees_everything_and_is_no_administrator(self):
        s = self.stack()
        s.run()
        s.certificates()
        email = "ingressctl@home.lab"
        self.assertIn(email, s.npm.users)
        perms = s.npm.permissions[s.npm.user_ids[email]]
        self.assertEqual(perms["visibility"], "all")

    def test_komodo_gets_the_stack_the_procedure_and_a_last_stage_in_update_all(self):
        s = self.stack()
        s.run()
        s.certificates()
        stack = s.komodo.stacks["ingressctl"]
        self.assertEqual(stack["run_directory"], "/srv/hg/periphery/repos/infrastructure/infra/ingress-controller")
        self.assertEqual(stack["file_paths"], ["compose.yaml", "../../inventory/ingressctl.compose.yaml"])
        self.assertTrue(stack["run_build"])
        self.assertIn("ingress-apply", s.komodo.procedures)
        last = s.komodo.procedures["update-all"]["stages"][-1]
        self.assertEqual(last["executions"][0]["execution"], {"type": "RunProcedure", "params": {"procedure": "ingress-apply"}})

    def test_every_service_of_the_stack_is_adopted_before_the_first_apply(self):
        s = self.stack()
        s.run()
        s.certificates()
        calls = s.komodo.ingress_calls
        adopted = [c[2] for c in calls if c[0] == "adopt"]
        self.assertEqual(adopted, [f"infrastructure/{n}" for n in ("git", "komodo", "secrets", "proxy", "homegraf")])
        self.assertEqual(calls[-1], ["apply", "--root", "/repo", "--all"])
        self.assertTrue(all(c[0] == "adopt" for c in calls[:-1]))

    def test_a_second_run_changes_nothing(self):
        s = self.stack()
        s.run()
        s.certificates()
        calls, stages = len(s.komodo.ingress_calls), len(s.komodo.procedures["update-all"]["stages"])
        code, out, _ = s.certificates()
        self.assertEqual(code, 0)
        self.assertEqual((len(s.komodo.ingress_calls), len(s.komodo.procedures["update-all"]["stages"])), (calls, stages))
        self.assertIn("already set up", out)

    def test_a_failing_ingressctl_does_not_stop_the_stack_and_is_said(self):
        s = self.stack()
        s.komodo.ingress_ok = False
        s.run()
        code, out, err = s.certificates()
        self.assertEqual(code, 0)                      # the proxy hosts work over plain HTTP; homegraf must still start
        self.assertIn("certificates are not set up", err)
        self.assertIn("certificates: not set up", out)
        self.assertIn("docker compose up -d certificates", err)
        self.assertNotIn("ingress-apply", str(s.komodo.procedures["update-all"]))   # no stage that would fail on every push

    def test_a_rerun_after_a_failure_finishes_the_job(self):
        s = self.stack()
        s.komodo.ingress_ok = False
        s.run()
        s.certificates()
        s.komodo.ingress_ok = True
        code, out, _ = s.certificates()
        self.assertEqual(code, 0)
        self.assertIn("every proxy host of the stack has one from your own CA", out)
        self.assertEqual(len(s.infisical.cas), 1)       # nothing is created twice
        self.assertEqual(len(s.infisical.identities), 4)   # homegraf, ingressctl, stacks-local (viewer), stack-provisioner: each once
        self.assertEqual(len(s.komodo.stacks), 1)

    def test_a_second_certificate_authority_that_is_not_ours_is_not_guessed(self):
        s = self.stack()
        s.run()
        s.certificates()                                 # creates the project and the CA
        s.infisical.cas[0]["name"] = "someone-elses"
        import json
        state_file = pathlib.Path(s.state_dir, "state.json")
        data = json.loads(state_file.read_text())
        data["ingress"]["done"] = False
        state_file.write_text(json.dumps(data))
        code, _, err = s.certificates()
        self.assertIn("not 'homegraf-root-ca'", err)


if __name__ == "__main__":
    unittest.main()


class ProvisionerTests(unittest.TestCase):
    def stack(self):
        s = Stack()
        self.addCleanup(s.close)
        return s

    def test_the_identity_its_key_and_the_credentials_file_are_made_and_update_all_gets_a_reconcile_stage(self):
        s = self.stack()
        self.assertEqual(s.run()[0], 0)
        code, out, err = s.certificates()
        self.assertEqual(code, 0, err)
        names = [i["name"] for i in s.infisical.identities.values()]
        self.assertIn("stack-provisioner", names)
        self.assertEqual(next(i for i in s.infisical.identities.values() if i["name"] == "stack-provisioner")["role"], "no-access")
        self.assertIn("stack-provisioner", [k["name"] for k in s.komodo.keys])
        env = file_of(s, "infra/secrets/provisioner.env")
        for var in ("INFISICAL_URL", "INFISICAL_PROVISIONER_CLIENT_ID", "INFISICAL_PROVISIONER_CLIENT_SECRET", "KOMODO_URL", "KOMODO_API_KEY", "KOMODO_API_SECRET"):
            self.assertIn(f"export {var}='", env)
        key = next(k for k in s.komodo.keys if k["name"] == "stack-provisioner")
        self.assertIn(f"export KOMODO_API_KEY='{key['key']}'", env)          # its own key, not homegraf's
        stages = s.komodo.procedures["update-all"]["stages"]
        names = [st["name"] for st in stages]
        self.assertEqual(names.index("Reconcile"), names.index("Pull") + 1)   # right after the pull, before the deploy
        self.assertLess(names.index("Reconcile"), names.index("Deploy"))
        step = stages[names.index("Reconcile")]["executions"][0]["execution"]["params"]
        self.assertEqual((step["stack"], step["service"], step["command"]), ("ingressctl", "reconcile", ["--apply"]))

    def test_a_second_run_changes_nothing(self):
        s = self.stack()
        s.run()
        s.certificates()
        before = (len(s.infisical.identities), len(s.komodo.keys), s.komodo.procedures["update-all"]["stages"][:])
        code, out, err = s.certificates()
        self.assertEqual(code, 0, err)
        self.assertEqual((len(s.infisical.identities), len(s.komodo.keys), s.komodo.procedures["update-all"]["stages"]), before)



class ServerSecretsTests(unittest.TestCase):
    def test_the_server_can_keep_secrets_before_the_first_stack_is_pushed(self):
        s = Stack()
        self.addCleanup(s.close)
        s.run()
        code, out, err = s.certificates()
        self.assertEqual(code, 0, err)
        self.assertEqual([p["slug"] for p in s.infisical.projects if p["type"] == "secret-manager"], ["stacks-local"])
        self.assertEqual({"INFISICAL_API_URL", "INFISICAL_PROJECT_ID_LOCAL", "INFISICAL_CLIENT_ID_LOCAL", "INFISICAL_CLIENT_SECRET_LOCAL"},
                         set(s.komodo.variables))
        self.assertTrue(s.komodo.variables["INFISICAL_CLIENT_SECRET_LOCAL"]["is_secret"])
        viewer = next(i for i in s.infisical.identities.values() if i["name"] == "stacks-local")
        self.assertEqual(viewer["role"], "no-access")
        # the provisioner is a member of that project too, so the first push needs nothing by hand
        project_id = next(p["id"] for p in s.infisical.projects if p["slug"] == "stacks-local")
        self.assertGreaterEqual(len(s.infisical.project_members), 3)   # ingressctl (CA project), the viewer, the provisioner
        self.assertTrue(project_id)

    def test_a_second_run_creates_nothing_again(self):
        s = Stack()
        self.addCleanup(s.close)
        s.run()
        s.certificates()
        before = (len(s.infisical.projects), len(s.infisical.identities), dict(s.komodo.variables))
        s.certificates()
        self.assertEqual((len(s.infisical.projects), len(s.infisical.identities), dict(s.komodo.variables)), before)

    def test_a_host_added_later_gets_its_own_project_and_the_provisioner_joins_it(self):
        from hgboot.config import Config
        from hgboot.komodo import Komodo
        from hgboot.serversecrets import ServerSecrets
        from hgboot.state import State
        from .helpers import no_wait
        s = Stack()
        self.addCleanup(s.close)
        s.run()
        s.certificates()
        cfg = Config.from_env(s.env)
        state = State(f"{cfg.state_dir}/state.json")
        komodo = Komodo(cfg, state, wait=no_wait, sleep=lambda x: None)
        komodo.login()
        members_before = len(s.infisical.project_members)
        ServerSecrets(cfg, state, komodo).run("worker-1", remote=True)
        self.assertEqual(sorted(p["slug"] for p in s.infisical.projects if p["type"] == "secret-manager"), ["stacks-local", "stacks-worker-1"])
        self.assertTrue({"INFISICAL_PROJECT_ID_WORKER_1", "INFISICAL_CLIENT_ID_WORKER_1", "INFISICAL_CLIENT_SECRET_WORKER_1"} <= set(s.komodo.variables))
        self.assertEqual(len(s.infisical.project_members) - members_before, 2)     # the viewer and the provisioner
        # the wrapper runs on the other host, where the name `infisical` does not resolve: the address of this host instead
        self.assertEqual(s.komodo.variables["INFISICAL_API_URL"]["value"], f"http://{cfg.host_ip}:{cfg.ports['INFISICAL']}")
        ServerSecrets(cfg, state, komodo).run("worker-1", remote=True)             # again: nothing more
        self.assertEqual(len(s.infisical.project_members) - members_before, 2)

    def test_an_app_that_answers_nothing_does_not_keep_the_provisioner_from_being_set_up(self):
        s = Stack()
        self.addCleanup(s.close)
        s.run()
        s.komodo.ingress_ok = False          # ingressctl apply reports an error (e.g. an app of the user is down)
        code, out, err = s.certificates()
        self.assertEqual(code, 0)
        self.assertIn("certificates are not set up", err)                   # still said
        self.assertIn("stack-provisioner", [i["name"] for i in s.infisical.identities.values()])
        self.assertIn("infra/secrets/provisioner.env", s.forgejo.repos["owner/infrastructure"]["files"])
        self.assertIn("Reconcile", [st["name"] for st in s.komodo.procedures["update-all"]["stages"]])

    def test_ingress_sync_applies_the_manifests_on_a_schedule_without_alerts(self):
        s = Stack()
        self.addCleanup(s.close)
        s.run()
        s.certificates()
        sync = s.komodo.procedures["ingress-sync"]
        self.assertEqual((sync["schedule"], sync["schedule_enabled"], sync["failure_alert"], sync["webhook_enabled"]),
                         ("0 7,22,37,52 * * * *", True, False, False))
        params = sync["stages"][0]["executions"][0]["execution"]["params"]
        self.assertEqual((params["stack"], params["service"], params["command"]), ("ingressctl", "ingressctl", ["apply", "--root", "/repo", "--all"]))
        s.certificates()                                              # again: not created twice
        self.assertEqual(sum(1 for n in s.komodo.procedures if n == "ingress-sync"), 1)
