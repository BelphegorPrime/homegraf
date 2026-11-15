import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ValidateScriptTests(unittest.TestCase):
    def test_a_syntax_error_after_the_first_file_fails_the_gate(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "infra" / "ci").mkdir(parents=True)
            (root / "infra" / "ci" / "validate.sh").write_bytes((ROOT / "infra" / "ci" / "validate.sh").read_bytes())
            (root / "bootstrap" / "tests").mkdir(parents=True)
            (root / "bootstrap" / "tests" / "__init__.py").write_text("")
            # Discovery fails when it finds no tests, so the temporary repository carries one.
            (root / "bootstrap" / "tests" / "test_placeholder.py").write_text(
                "import unittest\n\n\nclass PlaceholderTest(unittest.TestCase):\n    def test_placeholder(self):\n        pass\n"
            )
            (root / "scripts").mkdir()
            (root / "setup.sh").write_text("true\n")
            (root / "scripts" / "good.sh").write_text("true\n")
            (root / "scripts" / "bad.sh").write_text("if true; then\n")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)

            result = subprocess.run(["bash", "infra/ci/validate.sh"], cwd=root, capture_output=True, text=True)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("scripts/bad.sh", result.stderr)
