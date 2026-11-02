"""The seed folder of this repository (written by hand and synced from the author's tools) must pass through the bootstrap's own
placeholder handling: an unknown placeholder, or a literal one in a document, would make the real bootstrap stop."""
import pathlib
import unittest

from hgboot import seed

from .helpers import config

SEED = pathlib.Path(__file__).resolve().parents[2] / "seed"


class RealSeedTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config(domain="lab.example", host_ip="10.0.0.5", admin_user="owner", data_dir="/srv/hg",
                          forgejo_url="http://forgejo:3000", infisical_url="http://infisical:8080")
        self.files = dict(seed.collect(SEED, seed.substitutions(self.cfg, "main")))

    def test_every_placeholder_is_known_and_none_is_left(self):
        self.assertGreater(len(self.files), 50)
        for path, content in self.files.items():
            self.assertNotIn(b"@@", content, path)

    def test_the_inventory_carries_what_the_bootstrap_knows(self):
        # Text checks on purpose: the bootstrap and its tests use only the standard library, no YAML parser.
        homegraf = self.files["inventory/homegraf.yaml"].decode()
        self.assertIn('domain: "lab.example"', homegraf)
        self.assertIn(f'repository: "{self.cfg.repository}"', homegraf)
        self.assertIn('repos_dir: "/srv/hg/periphery/repos"', homegraf)
        self.assertIn('"10.0.0.5"', self.files["inventory/servers.yaml"].decode())
        ingress = self.files["inventory/ingress.yaml"].decode()
        self.assertIn('management_repo_dir: "/srv/hg/periphery/repos/infrastructure"', ingress)
        self.assertIn('allowed_domains: ["*.lab.example"]', ingress)

    def test_the_tools_and_the_skeleton_are_all_there(self):
        for needed in ("homelab.py", "infra/graph/build.py", "infra/homelab/settings.py", "infra/komodo/komodo_api.py",
                       "infra/infisical/secret.py", "inventory/stacks.yaml", "stacks/README.md"):
            self.assertIn(needed, self.files)


if __name__ == "__main__":
    unittest.main()
