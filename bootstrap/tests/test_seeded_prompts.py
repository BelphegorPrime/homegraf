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

SHARED_SENTENCE = "A missing check never blocks delivery."

ENVIRONMENT_LIMITS = (
    "ENVIRONMENT LIMITS. Your runner cannot do some things: building or starting containers "
    "(`unshare: operation not permitted`, no Docker daemon, cannot register a layer), reaching "
    "services outside the network, or hardware. A check that fails or cannot start for that "
    "reason alone is not a finding against the pull request. Report it as `not verified: runner "
    "limitation` with the command and its error line, run every static check you can instead "
    "(for example `docker compose config -q`, lint, typecheck, tests that do not need "
    "containers), and judge the rest. If everything else is met, the verdict is `## Verdict: OK` "
    "with a line `Not verified (runner limitation): <what>` so that the human or the CI can "
    "verify it; do not return NOT OK for it, and never claim it passed. A failure that is not "
    "caused by the environment (a real error message from the code, a wrong result) still counts. "
    "A requirement you could not check at all because of the runner is never turned into a "
    "requirement for the worker to change code."
)

FIX_PATH = (
    "FIX PATH FOR EVERY FINDING. Every problem you report (a NOT OK verdict, a scope or "
    "reality-check finding, a concern) comes with a concrete way to solve it, in the same line or "
    "directly below it, under `Fix:`. Name the file and the place (path and line or section), what "
    "to change, and the command that proves it afterwards. A finding without a fix path is "
    "incomplete: if you cannot say how to fix it, say so and name what you would need to know. If "
    "the cause is the ticket (a contradiction, a stale or impossible acceptance command, a file "
    "the steps need that the file list forbids), say that, and propose the corrected ticket text "
    "instead of asking the worker to guess. If the cause is the runner, name the check that could "
    "be run elsewhere. Keep each fix to what the pull request needs; no extra scope."
)

SEVERITY = (
    "SEVERITY: BLOCKING OR FOLLOW-UP. Report every finding in ONE review pass: look for all of "
    "them before you write the verdict, and never hold one back for a later round. Then sort "
    "them. A finding is BLOCKING (verdict `## Verdict: NOT OK`) only when it is one of: (1) an "
    "acceptance criterion of the ticket or a requirement of an operator comment that the pull "
    "request does not meet; (2) a regression, meaning something that worked on the default branch "
    "and now fails; (3) a security problem or a changed security default; (4) a repository check "
    "that passes on the default branch and fails on the pull request; (5) a file changed outside "
    "the ticket's file list that the ticket does not need. Everything else is a FOLLOW-UP: an "
    "edge case, a hardening idea or a style point that the acceptance criteria do not ask for, "
    "even if you can show that it fails with a constructed input. If there is no blocking finding "
    "the verdict is `## Verdict: OK`, and the follow-ups go below the evidence under a line "
    "`Follow-up (not blocking):` as a list, each with the input that shows it and a one-line "
    "`Fix:`. Do not turn a follow-up into NOT OK in a later round either: on the second and later "
    "reviews of the same pull request only a blocking finding of the kinds above, or a defect that "
    "the last round introduced, may produce NOT OK. Each blocking finding still carries its "
    "`Fix:` line."
)

EPIC_AND_RUNNER_LIMITS = (
    "EPIC AND RUNNER LIMITS. When you judge an epic, a criterion that could not be checked only "
    "because of the runner (no Docker daemon or socket, no container build, no network to an "
    "outside service, no special hardware) is NOT an unmet criterion. Record it on the epic as a "
    "line that starts with 'Not verified (runner limitation):' followed by the criterion, the "
    "exact command an operator runs elsewhere and the result to expect, and judge the epic on the "
    "criteria you could check. If every checkable criterion is met on the default branch, the epic "
    "is met: close it with that line in your closing comment, so that an operator can verify "
    "later; do not leave it open and do not ask the operator which runner to use. If a checkable "
    "criterion is unmet, the epic stays open as before."
)

ALL_PROMPTS = ("worker", "worker-strong", "planner", "reviewer", "sensitive-reviewer")


class SeededPromptTests(unittest.TestCase):
    def test_repository_check_guidance(self):
        for name in ALL_PROMPTS:
            with self.subTest(prompt=name):
                text = (PROMPTS / f"{name}.md").read_text()
                self.assertIn(SHARED_SENTENCE, text)
                self.assertNotRegex(text, r"(?i)\b(?:validate\.sh|check-seed\.sh|test_agents\.py)\b")

    def test_transient_retry_is_the_last_paragraph(self):
        for name in ("worker", "worker-strong", "planner", "reviewer"):
            with self.subTest(prompt=name):
                paragraphs = (PROMPTS / f"{name}.md").read_text().rstrip().split("\n\n")
                self.assertEqual(paragraphs.count(RETRY), 1)
                self.assertEqual(paragraphs[-1], RETRY)

    def test_reviewer_paragraphs_before_epic_close(self):
        text = (PROMPTS / "reviewer.md").read_text()
        epic_close = text.find("EPIC CLOSE")
        before = text[:epic_close]

        for heading, paragraph in (
            ("ENVIRONMENT LIMITS", ENVIRONMENT_LIMITS),
            ("FIX PATH FOR EVERY FINDING", FIX_PATH),
            ("SEVERITY: BLOCKING OR FOLLOW-UP", SEVERITY),
            ("EPIC AND RUNNER LIMITS", EPIC_AND_RUNNER_LIMITS),
        ):
            with self.subTest(paragraph=heading):
                self.assertIn(heading, before, f"{heading} missing before EPIC CLOSE")
                self.assertEqual(before.count(paragraph), 1, f"{paragraph} not exactly once before EPIC CLOSE")

    def test_environment_limits_has_runner_limitation_phrase(self):
        text = (PROMPTS / "reviewer.md").read_text()
        epic_close = text.find("EPIC CLOSE")
        before = text[:epic_close]
        start = before.find(ENVIRONMENT_LIMITS)
        paragraph = before[start:start + len(ENVIRONMENT_LIMITS)]
        self.assertIn("not verified: runner limitation", paragraph)

    def test_severity_has_follow_up_phrase(self):
        text = (PROMPTS / "reviewer.md").read_text()
        epic_close = text.find("EPIC CLOSE")
        before = text[:epic_close]
        start = before.find(SEVERITY)
        paragraph = before[start:start + len(SEVERITY)]
        self.assertIn("Follow-up (not blocking):", paragraph)

    def test_fix_path_paragraph_has_fix_once(self):
        text = (PROMPTS / "reviewer.md").read_text()
        epic_close = text.find("EPIC CLOSE")
        before = text[:epic_close]
        start = before.find(FIX_PATH)
        paragraph = before[start:start + len(FIX_PATH)]
        self.assertEqual(paragraph.count("Fix:"), 1)

    def test_review_step3_mentions_fix(self):
        text = (PROMPTS / "reviewer.md").read_text()
        self.assertIn("Fix:", text)

    def test_reviewer_ends_with_transient_errors(self):
        paragraphs = (PROMPTS / "reviewer.md").read_text().rstrip().split("\n\n")
        self.assertEqual(paragraphs[-1], RETRY)
        self.assertEqual(paragraphs.count(RETRY), 1)

    def test_sensitive_reviewer_has_fix_path_sentence(self):
        text = (PROMPTS / "sensitive-reviewer.md").read_text()
        self.assertIn("Fix:", text)
