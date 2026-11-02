"""The optional profile renovate: the bot user, its repository with the workflow, the secret and the collaboration."""
import base64
import unittest

from hgboot.renovate import BOT, config_js, workflow

from .test_main import Stack


def text(stack, repo, path):
    return base64.b64decode(stack.forgejo.repos[repo]["files"][path]).decode()


class Setup(unittest.TestCase):
    def stack(self, profiles="renovate"):
        s = Stack()
        s.env["COMPOSE_PROFILES"] = profiles
        self.addCleanup(s.close)
        return s

    def test_the_bot_its_repository_the_secret_and_the_collaboration(self):
        s = self.stack()
        code, out, err = s.run()
        self.assertEqual(code, 0, err)
        f = s.forgejo
        self.assertIn(BOT, f.users)
        repo = f.repos[f"{BOT}/configure"]
        self.assertEqual(repo["secrets"]["RENOVATE_TOKEN"], f.user_tokens[BOT]["renovate"])        # the bot's own token, not the administrator's
        self.assertEqual(f.repos["owner/infrastructure"]["collaborators"], {BOT: "write"})
        self.assertEqual(sorted(repo["files"]), [".forgejo/workflows/renovate.yml", "config.js", "renovate.json"])
        self.assertIn("runs-on: docker", text(s, f"{BOT}/configure", ".forgejo/workflows/renovate.yml"))
        self.assertIn("autodiscover: true", text(s, f"{BOT}/configure", "config.js"))

    def test_the_endpoint_is_the_hosts_address_because_the_jobs_daemon_does_not_see_the_network(self):
        s = self.stack()
        s.run()
        self.assertIn("endpoint: 'http://192.168.1.10:3000'", text(s, f"{BOT}/configure", "config.js"))

    def test_the_secret_is_set_before_the_files_are_pushed(self):
        """Their push starts the first run, which needs the secret."""
        s = self.stack()
        s.run()
        self.assertEqual(s.forgejo.repos[f"{BOT}/configure"]["events"], ["secret", "commit"])

    def test_without_the_profile_nothing_of_it_exists(self):
        s = self.stack(profiles="backup")
        self.assertEqual(s.run()[0], 0)
        self.assertEqual((s.forgejo.users, [r for r in s.forgejo.repos if BOT in r]), ({}, []))

    def test_a_second_run_changes_nothing(self):
        s = self.stack()
        s.run()
        tokens, commits = dict(s.forgejo.user_tokens[BOT]), len(s.forgejo.commits)
        code, out, _ = s.run()
        self.assertEqual(code, 0)
        self.assertEqual((s.forgejo.user_tokens[BOT], len(s.forgejo.commits)), (tokens, commits))
        self.assertIn("workflow and configuration: up to date", out)

    def test_texts(self):
        self.assertIn("${{ secrets.RENOVATE_TOKEN }}", workflow())
        self.assertIn("gitUrl: 'endpoint'", config_js("http://h:3000", "bot@x"))


if __name__ == "__main__":
    unittest.main()
