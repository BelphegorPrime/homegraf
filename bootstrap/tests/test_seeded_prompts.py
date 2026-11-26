import unittest
from pathlib import Path

PROMPTS = Path(__file__).resolve().parents[1] / "hgboot" / "prompts"
RETRY = (
    "TRANSIENT GIT ERRORS. A git clone, fetch or push (or an API call to the forge) that fails "
    "with an HTTP error (for example 'The requested URL returned error: 400', any 5xx, a timeout, "
    "or an authentication prompt although a token is configured) is retried up to three times "
    "with the same command, waiting 15 seconds between the tries, with GIT_TERMINAL_PROMPT=0 and "
    "the same http.extraHeader token header. Only after three failures in a row do you stop, and "
    "then you say in a comment which command failed and the exact error line, and you do not "
    "claim that the work was delivered."
)


class SeededPromptTests(unittest.TestCase):
    def test_repository_check_guidance(self):
        for name in ("worker", "worker-strong", "planner"):
            with self.subTest(prompt=name):
                text = (PROMPTS / f"{name}.md").read_text()
                self.assertIn("A missing check never blocks delivery.", text)
                self.assertNotRegex(text, r"(?i)\b(?:validate\.sh|check-seed\.sh|test_agents\.py)\b")

    def test_transient_retry_is_the_last_paragraph(self):
        for name in ("worker", "worker-strong", "planner"):
            with self.subTest(prompt=name):
                paragraphs = (PROMPTS / f"{name}.md").read_text().rstrip().split("\n\n")
                self.assertEqual(paragraphs.count(RETRY), 1)
                self.assertEqual(paragraphs[-1], RETRY)
