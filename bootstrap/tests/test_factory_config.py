"""The factory's opt-in settings, their validation and the repository merge policy."""
import json
import re
import unittest

from hgboot.factory_config import (DEFAULT_AGENT_LOGIN, DEFAULT_MERGE_DENY, DEFAULT_RUNTIME_DIR, FactoryConfig,
                                   FactoryConfigError)

REPOSITORY = "owner/infrastructure"
SECRETS = {
    "OPENHANDS_LOCAL_BACKEND_API_KEY": "backend-key-123",
    "FORGEHANDS_GATEWAY_API_KEY": "gateway-key-123",
    "FORGEHANDS_FORGEJO_AGENT_TOKEN": "agent-token-123",
    "FORGEHANDS_FORGEJO_WEBHOOK_SECRET": "webhook-secret-123",
}
ENV = {
    "FORGEHANDS_BOOTSTRAP": "1",
    "FORGEHANDS_OPENHANDS_API_URL": "http://openhands:8000/",
    "FORGEHANDS_STRONG_MODEL": "openai/gpt-5",
    "FORGEHANDS_CHEAP_MODEL": "openai/gpt-5-mini",
    "FORGEHANDS_GATEWAY_URL": "https://llm.example/v1/",
    "FORGEHANDS_ALLOWED_REPOS": "owner/infrastructure, owner/other",
    "FORGEHANDS_ALLOWED_SENDERS": "marcel,owner,homegraf-agent",
    "FORGEHANDS_HUMAN_LOGIN": "marcel",
    **SECRETS,
}
REQUIRED = ("FORGEHANDS_OPENHANDS_API_URL", "OPENHANDS_LOCAL_BACKEND_API_KEY", "FORGEHANDS_STRONG_MODEL",
            "FORGEHANDS_CHEAP_MODEL", "FORGEHANDS_GATEWAY_URL", "FORGEHANDS_GATEWAY_API_KEY",
            "FORGEHANDS_ALLOWED_REPOS", "FORGEHANDS_ALLOWED_SENDERS", "FORGEHANDS_HUMAN_LOGIN",
            "FORGEHANDS_FORGEJO_WEBHOOK_SECRET")


class OptInTests(unittest.TestCase):
    def test_a_missing_flag_leaves_the_factory_off_without_reading_the_new_inputs(self):
        legacy = {"ADMIN_EMAIL": "a@b.c", "COMPOSE_PROFILES": "agents", "FORGEHANDS_OPENHANDS_API_URL": "not a url",
                  "FORGEHANDS_GATEWAY_URL": "ftp://user:pass@gw", "FORGEHANDS_MERGE_POLICY": "not json"}
        cfg = FactoryConfig.from_env(legacy, "not/a/repo/name")
        self.assertFalse(cfg.enabled)
        self.assertEqual((cfg.allowed_repos, cfg.allowed_senders, cfg.merge_policy), ((), (), {}))
        self.assertEqual((cfg.agent_login, cfg.human_login), ("", ""))
        self.assertIsNone(cfg.denied_paths(REPOSITORY))
        for name in SECRETS:
            self.assertEqual(getattr(cfg, _attribute(name)), "")

    def test_zero_and_padding_are_the_same_off_switch(self):
        self.assertFalse(FactoryConfig.from_env({"FORGEHANDS_BOOTSTRAP": " 0 "}, REPOSITORY).enabled)
        self.assertTrue(FactoryConfig.from_env({**ENV, "FORGEHANDS_BOOTSTRAP": " 1 "}, REPOSITORY).enabled)

    def test_an_unknown_flag_value_is_an_error_not_a_silent_switch(self):
        for value in ("true", "yes", "2"):
            with self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_BOOTSTRAP"):
                FactoryConfig.from_env({"FORGEHANDS_BOOTSTRAP": value}, REPOSITORY)

    def test_the_flag_is_what_opts_in_not_the_agents_profile(self):
        self.assertTrue(FactoryConfig.from_env({**ENV, "COMPOSE_PROFILES": ""}, REPOSITORY).enabled)
        self.assertFalse(FactoryConfig.from_env({**ENV, "FORGEHANDS_BOOTSTRAP": ""}, REPOSITORY).enabled)


class FieldContractTests(unittest.TestCase):
    def test_the_documented_fields_and_defaults(self):
        cfg = FactoryConfig.from_env(ENV, REPOSITORY)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.repository, REPOSITORY)
        self.assertEqual(cfg.openhands_url, "http://openhands:8000")
        self.assertEqual(cfg.gateway_url, "https://llm.example/v1")
        self.assertEqual((cfg.strong_model, cfg.cheap_model), ("openai/gpt-5", "openai/gpt-5-mini"))
        self.assertEqual(cfg.free_model, "")
        self.assertEqual(cfg.allowed_repos, ("owner/infrastructure", "owner/other"))
        self.assertEqual(cfg.allowed_senders, ("marcel", "owner", "homegraf-agent"))
        self.assertEqual((cfg.agent_login, cfg.human_login), (DEFAULT_AGENT_LOGIN, "marcel"))
        self.assertEqual((cfg.security_reviewers, cfg.security_token), ((), ""))
        self.assertEqual(cfg.forgejo_agent_token, SECRETS["FORGEHANDS_FORGEJO_AGENT_TOKEN"])
        self.assertEqual(cfg.forgejo_webhook_secret, SECRETS["FORGEHANDS_FORGEJO_WEBHOOK_SECRET"])
        self.assertEqual((cfg.mirror_url, cfg.mirror_token), ("", ""))
        self.assertEqual(cfg.runtime_dir, DEFAULT_RUNTIME_DIR)
        self.assertEqual((cfg.max_concurrent_per_tier, cfg.max_concurrent, cfg.max_runs_per_hour, cfg.max_run_minutes),
                         (2, 4, 30, 120))

    def test_the_free_model_is_optional_and_blank_means_no_free_tier(self):
        cfg = FactoryConfig.from_env({**ENV, "FORGEHANDS_FREE_MODEL": " openai/gpt-5-free "}, REPOSITORY)
        self.assertEqual(cfg.free_model, "openai/gpt-5-free")
        self.assertEqual(FactoryConfig.from_env({**ENV, "FORGEHANDS_FREE_MODEL": "  "}, REPOSITORY).free_model, "")

    def test_the_free_model_must_be_an_identifier_like_the_other_models(self):
        for value in ("openai/free model", "openai/free\nmodel", "x" * 201):
            with self.subTest(value=value), self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_FREE_MODEL"):
                FactoryConfig.from_env({**ENV, "FORGEHANDS_FREE_MODEL": value}, REPOSITORY)

    def test_runtime_limits_and_the_optional_mirror_can_be_overridden(self):
        cfg = FactoryConfig.from_env({**ENV, "FORGEHANDS_RUNTIME_DIR": "/run/factory/", "FORGEHANDS_MAX_CONCURRENT_PER_TIER": "3",
                                      "FORGEHANDS_MAX_CONCURRENT": "5", "FORGEHANDS_MAX_RUNS_PER_HOUR": "7",
                                      "FORGEHANDS_MAX_RUN_MINUTES": "60", "FORGEHANDS_MIRROR_URL": "https://github.com/owner/infrastructure.git/",
                                      "FORGEHANDS_MIRROR_TOKEN": "mirror-token-123"}, REPOSITORY)
        self.assertEqual(cfg.runtime_dir, "/run/factory")
        self.assertEqual((cfg.max_concurrent_per_tier, cfg.max_concurrent, cfg.max_runs_per_hour, cfg.max_run_minutes),
                         (3, 5, 7, 60))
        self.assertEqual(cfg.mirror_url, "https://github.com/owner/infrastructure.git")
        self.assertEqual(cfg.mirror_token, "mirror-token-123")

    def test_a_fresh_opt_in_leaves_the_agent_token_to_the_agent_user_step(self):
        env = {k: v for k, v in ENV.items() if k != "FORGEHANDS_FORGEJO_AGENT_TOKEN"}
        cfg = FactoryConfig.from_env({**env, "FORGEJO_TOKEN": "operator-token-123"}, REPOSITORY)
        self.assertEqual(cfg.forgejo_agent_token, "")

    def test_every_required_input_is_named_when_it_is_missing(self):
        for name in REQUIRED:
            with self.subTest(name=name), self.assertRaisesRegex(FactoryConfigError, name):
                FactoryConfig.from_env({k: v for k, v in ENV.items() if k != name}, REPOSITORY)

    def test_secrets_are_not_in_repr_or_in_errors(self):
        cfg = FactoryConfig.from_env({**ENV, "FORGEHANDS_MIRROR_URL": "https://github.com/owner/infrastructure.git",
                                      "FORGEHANDS_MIRROR_TOKEN": "mirror-token-123"}, REPOSITORY)
        shown = repr(cfg) + str(cfg)
        for secret in [*SECRETS.values(), "mirror-token-123"]:
            self.assertNotIn(secret, shown)
        try:
            FactoryConfig.from_env({**ENV, "FORGEHANDS_MAX_CONCURRENT": "0"}, REPOSITORY)
        except FactoryConfigError as e:
            for secret in SECRETS.values():
                self.assertNotIn(secret, str(e))
        else:
            self.fail("invalid limit was accepted")


class IdentityTests(unittest.TestCase):
    def test_the_agent_login_has_a_default_and_the_human_login_is_required(self):
        cfg = FactoryConfig.from_env({k: v for k, v in ENV.items() if k != "FORGEHANDS_AGENT_LOGIN"}, REPOSITORY)
        self.assertEqual(cfg.agent_login, DEFAULT_AGENT_LOGIN)
        with self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_HUMAN_LOGIN"):
            FactoryConfig.from_env({k: v for k, v in ENV.items() if k != "FORGEHANDS_HUMAN_LOGIN"}, REPOSITORY)

    def test_the_human_and_the_agent_must_not_be_the_same_login(self):
        with self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_HUMAN_LOGIN"):
            FactoryConfig.from_env({**ENV, "FORGEHANDS_HUMAN_LOGIN": DEFAULT_AGENT_LOGIN}, REPOSITORY)

    def test_allowed_senders_must_contain_the_agent_and_the_human(self):
        for senders, missing in (("marcel,owner", "FORGEHANDS_AGENT_LOGIN"),
                                 ("owner,homegraf-agent", "FORGEHANDS_HUMAN_LOGIN")):
            with self.subTest(senders=senders), self.assertRaisesRegex(FactoryConfigError, missing):
                FactoryConfig.from_env({**ENV, "FORGEHANDS_ALLOWED_SENDERS": senders}, REPOSITORY)

    def test_a_sender_list_with_both_logins_is_accepted(self):
        cfg = FactoryConfig.from_env({**ENV, "FORGEHANDS_AGENT_LOGIN": "worker", "FORGEHANDS_HUMAN_LOGIN": "owner",
                                      "FORGEHANDS_ALLOWED_SENDERS": "owner,worker"}, REPOSITORY)
        self.assertEqual((cfg.agent_login, cfg.human_login, cfg.allowed_senders),
                         ("worker", "owner", ("owner", "worker")))

    def test_login_names_must_be_forgejo_user_names(self):
        for name in ("FORGEHANDS_AGENT_LOGIN", "FORGEHANDS_HUMAN_LOGIN"):
            for value in ("two words", "team/name", "-bad", "x" * 40):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(FactoryConfigError, name):
                    FactoryConfig.from_env({**ENV, name: value}, REPOSITORY)


class ValidationTests(unittest.TestCase):
    def test_urls_must_be_http_or_https_without_credentials_or_queries(self):
        for name in ("FORGEHANDS_OPENHANDS_API_URL", "FORGEHANDS_GATEWAY_URL", "FORGEHANDS_MIRROR_URL"):
            for value in ("ftp://llm.example", "not a url", "https://user:pass@llm.example", "https://llm.example?token=x",
                          "https://llm.example/#frag", "https://llm.example:notaport"):
                with self.subTest(name=name, value=value), self.assertRaises(FactoryConfigError) as caught:
                    FactoryConfig.from_env({**ENV, name: value}, REPOSITORY)
                self.assertNotIn("user:pass", str(caught.exception))

    def test_repository_names_are_owner_slash_name(self):
        for value in ("owner", "owner/", "/repo", "owner/two/parts", "own er/repo", "owner/../repo", "owner/.", ""):
            with self.subTest(value=value), self.assertRaisesRegex(FactoryConfigError, "repository"):
                FactoryConfig.from_env(ENV, value)
        for value in ("owner/a, owner", "owner/a, own er/b", "owner/a,,owner/b", "owner/a, "):
            with self.subTest(value=value), self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_ALLOWED_REPOS"):
                FactoryConfig.from_env({**ENV, "FORGEHANDS_ALLOWED_REPOS": value}, REPOSITORY)

    def test_senders_must_be_forgejo_user_names(self):
        for value in ("", "marcel,", "marcel user", "team/name", "-bad"):
            with self.subTest(value=value), self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_ALLOWED_SENDERS"):
                FactoryConfig.from_env({**ENV, "FORGEHANDS_ALLOWED_SENDERS": value}, REPOSITORY)

    def test_limits_are_positive_bounded_integers_and_per_tier_fits_the_total(self):
        for name, values in {
            "FORGEHANDS_MAX_CONCURRENT_PER_TIER": ("0", "-1", "2.5", "1_0", "1001", "two"),
            "FORGEHANDS_MAX_CONCURRENT": ("0", "1001"),
            "FORGEHANDS_MAX_RUNS_PER_HOUR": ("0", "100001"),
            "FORGEHANDS_MAX_RUN_MINUTES": ("0", "10081"),
        }.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaisesRegex(FactoryConfigError, name):
                    FactoryConfig.from_env({**ENV, name: value}, REPOSITORY)
        with self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_MAX_CONCURRENT_PER_TIER"):
            FactoryConfig.from_env({**ENV, "FORGEHANDS_MAX_CONCURRENT_PER_TIER": "5", "FORGEHANDS_MAX_CONCURRENT": "4"},
                                   REPOSITORY)

    def test_runtime_directory_is_an_absolute_path_below_the_root(self):
        for value in ("factory-runtime", "/", "/run/../factory", "/run//factory", "~/.factory", "/run/\nfactory"):
            with self.subTest(value=value), self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_RUNTIME_DIR"):
                FactoryConfig.from_env({**ENV, "FORGEHANDS_RUNTIME_DIR": value}, REPOSITORY)

    def test_a_mirror_token_requires_an_https_target(self):
        with self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_MIRROR_URL"):
            FactoryConfig.from_env({**ENV, "FORGEHANDS_MIRROR_TOKEN": "token"}, REPOSITORY)
        with self.assertRaisesRegex(FactoryConfigError, "https"):
            FactoryConfig.from_env({**ENV, "FORGEHANDS_MIRROR_URL": "http://github.com/owner/repo.git",
                                    "FORGEHANDS_MIRROR_TOKEN": "token"}, REPOSITORY)
        cfg = FactoryConfig.from_env({**ENV, "FORGEHANDS_MIRROR_URL": "https://github.com/owner/repo.git"}, REPOSITORY)
        self.assertEqual(cfg.mirror_token, "")

    def test_secrets_with_line_breaks_are_rejected_without_echoing_them(self):
        for name in SECRETS:
            with self.subTest(name=name), self.assertRaisesRegex(FactoryConfigError, name) as caught:
                FactoryConfig.from_env({**ENV, name: "good\nleak-value"}, REPOSITORY)
            self.assertNotIn("leak-value", str(caught.exception))


class MergePolicyTests(unittest.TestCase):
    def test_a_fresh_opt_in_protects_the_own_repository_and_leaves_others_manual(self):
        cfg = FactoryConfig.from_env(ENV, REPOSITORY)
        self.assertEqual(cfg.denied_paths(REPOSITORY), DEFAULT_MERGE_DENY)
        self.assertEqual(set(DEFAULT_MERGE_DENY), {".forgejo/**", ".github/**", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md",
                                                   ".agents/**", ".claude/**", "compose.yaml", "**/compose.yaml", "setup.sh",
                                                   "scripts/**", "infra/**", "stacks/**"})
        self.assertEqual(cfg.repository_merge_policy(REPOSITORY), {"deny": DEFAULT_MERGE_DENY})
        self.assertIsNone(cfg.repository_merge_policy("owner/other"))
        self.assertIsNone(cfg.denied_paths("owner/other"))

    def test_the_default_policy_matches_every_path_it_has_to_protect(self):
        protected = (".forgejo/workflows/ci.yaml", ".github/workflows/ci.yml", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md",
                     ".agents/skills/code-review.md", ".claude/settings.json", "compose.yaml", "stacks/app/compose.yaml",
                     "setup.sh", "scripts/deploy.sh", "infra/ci/validate.sh", "stacks/app/stack.yaml")
        for path in protected:
            with self.subTest(path=path):
                self.assertTrue(any(_glob_matches(glob, path) for glob in DEFAULT_MERGE_DENY),
                                f"{path} is not protected by the default policy")
        for path in ("app/src/web/main.tsx", "README.md", "docs/CONTRIBUTING.md", "app/AGENTS.md", "stacks.md"):
            with self.subTest(path=path):
                self.assertFalse(any(_glob_matches(glob, path) for glob in DEFAULT_MERGE_DENY),
                                 f"{path} must stay mergeable by default")
        self.assertTrue(_glob_matches("scripts/**", "scripts/nested/run.sh"))
        self.assertFalse(_glob_matches("scripts/*", "scripts/nested/run.sh"))
        self.assertTrue(_glob_matches("scripts/*", "scripts/run.sh"))

    def test_an_explicit_empty_policy_stays_empty(self):
        cfg = FactoryConfig.from_env({**ENV, "FORGEHANDS_MERGE_POLICY": "{}"}, REPOSITORY)
        self.assertEqual(cfg.merge_policy, {})
        self.assertIsNone(cfg.denied_paths(REPOSITORY))

    def test_operator_policy_is_kept_for_every_repository_and_is_not_replaced_by_defaults(self):
        policy = {"owner/infrastructure": {"deny": ["docs/**", "docs/**"]}, "owner/other": {"deny": []}}
        cfg = FactoryConfig.from_env({**ENV, "FORGEHANDS_MERGE_POLICY": json.dumps(policy)}, REPOSITORY)
        self.assertEqual(cfg.denied_paths(REPOSITORY), ("docs/**",))
        self.assertEqual(cfg.denied_paths("owner/other"), ())
        self.assertIsNone(cfg.denied_paths("owner/third"))

    def test_a_policy_without_the_own_repository_stays_manual_there(self):
        cfg = FactoryConfig.from_env({**ENV, "FORGEHANDS_MERGE_POLICY": '{"owner/other": {"deny": ["secrets/**"]}}'}, REPOSITORY)
        self.assertIsNone(cfg.denied_paths(REPOSITORY))
        self.assertEqual(cfg.denied_paths("owner/other"), ("secrets/**",))

    def test_a_blank_value_is_unset_and_gets_the_fresh_default(self):
        for value in ("", "   "):
            with self.subTest(value=value):
                self.assertEqual(FactoryConfig.from_env({**ENV, "FORGEHANDS_MERGE_POLICY": value}, REPOSITORY)
                                 .denied_paths(REPOSITORY), DEFAULT_MERGE_DENY)

    def test_a_disabled_factory_does_not_parse_the_policy(self):
        cfg = FactoryConfig.from_env({**ENV, "FORGEHANDS_BOOTSTRAP": "", "FORGEHANDS_MERGE_POLICY": "{broken"}, REPOSITORY)
        self.assertEqual(cfg.merge_policy, {})

    def test_bad_json_and_bad_entries_are_rejected(self):
        for value in ("not json", "[]", "null", "42", '"owner/repo"'):
            with self.subTest(value=value), self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_MERGE_POLICY"):
                FactoryConfig.from_env({**ENV, "FORGEHANDS_MERGE_POLICY": value}, REPOSITORY)
        bad_entries = (
            {"not-a-repository": {"deny": []}},
            {"owner/repo": []},
            {"owner/repo": {}},
            {"owner/repo": {"deny": "docs/**"}},
            {"owner/repo": {"denny": []}},
            {"owner/repo": {"deny": [""]}},
            {"owner/repo": {"deny": [" /abs"]}},
            {"owner/repo": {"deny": ["/abs"]}},
            {"owner/repo": {"deny": ["../up"]}},
            {"owner/repo": {"deny": ["back\\slash"]}},
            {"owner/repo": {"deny": [1]}},
        )
        for policy in bad_entries:
            with self.subTest(policy=policy), self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_MERGE_POLICY|repository|deny"):
                FactoryConfig.from_env({**ENV, "FORGEHANDS_MERGE_POLICY": json.dumps(policy)}, REPOSITORY)
        with self.assertRaisesRegex(FactoryConfigError, "repeats"):
            FactoryConfig.from_env({**ENV, "FORGEHANDS_MERGE_POLICY":
                                    '{"owner/repo": {"deny": []}, "owner/repo": {"deny": ["x"]}}'}, REPOSITORY)


TWO_KEYS_ENV = {"FORGEHANDS_SECURITY_REVIEWERS": "openhands-security",
                "FORGEHANDS_SECURITY_TOKEN": "security-token-123"}


class TwoKeysTests(unittest.TestCase):
    """The protected paths that merge with a reviewer and an independent security review instead of a human."""

    def test_a_two_keys_entry_parses_with_reasons_and_an_independent_reviewer(self):
        policy = {"owner/infrastructure": {"deny": ["infra/**", "setup.sh"],
                                           "reasons": {"infra/**": "a merge here deploys production"},
                                           "two_keys": ["infra/**"]}}
        cfg = FactoryConfig.from_env({**ENV, **TWO_KEYS_ENV, "FORGEHANDS_MERGE_POLICY": json.dumps(policy)},
                                     REPOSITORY)
        self.assertEqual(cfg.repository_merge_policy(REPOSITORY),
                         {"deny": ("infra/**", "setup.sh"),
                          "reasons": {"infra/**": "a merge here deploys production"},
                          "two_keys": ("infra/**",)})
        self.assertEqual(cfg.security_reviewers, ("openhands-security",))
        self.assertEqual(cfg.security_token, TWO_KEYS_ENV["FORGEHANDS_SECURITY_TOKEN"])
        self.assertNotIn(TWO_KEYS_ENV["FORGEHANDS_SECURITY_TOKEN"], repr(cfg))

    def test_a_two_keys_entry_without_reviewers_or_token_names_what_is_missing(self):
        policy = json.dumps({"owner/infrastructure": {"deny": ["infra/**"], "two_keys": ["infra/**"]}})
        cases = (({}, "FORGEHANDS_SECURITY_REVIEWERS and FORGEHANDS_SECURITY_TOKEN"),
                 ({"FORGEHANDS_SECURITY_REVIEWERS": "openhands-security"}, "FORGEHANDS_SECURITY_TOKEN"),
                 ({"FORGEHANDS_SECURITY_TOKEN": "security-token-123"}, "FORGEHANDS_SECURITY_REVIEWERS"))
        for extra, message in cases:
            with self.subTest(extra=extra), self.assertRaisesRegex(FactoryConfigError, message):
                FactoryConfig.from_env({**ENV, **extra, "FORGEHANDS_MERGE_POLICY": policy}, REPOSITORY)

    def test_two_keys_need_a_reviewer_that_is_not_the_agent(self):
        policy = json.dumps({"owner/infrastructure": {"deny": ["infra/**"], "two_keys": ["infra/**"]}})
        with self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_SECURITY_REVIEWERS.*FORGEHANDS_AGENT_LOGIN"):
            FactoryConfig.from_env({**ENV, **TWO_KEYS_ENV, "FORGEHANDS_SECURITY_REVIEWERS": "homegraf-agent",
                                    "FORGEHANDS_MERGE_POLICY": policy}, REPOSITORY)

    def test_a_two_keys_pattern_must_be_covered_by_a_deny_pattern(self):
        for deny, two_keys in ((["infra/**"], ["AGENTS.md"]),
                               (["infra/*"], ["infra/ci/validate.sh"]),
                               ([], ["infra/**"])):
            policy = json.dumps({"owner/infrastructure": {"deny": deny, "two_keys": two_keys}})
            with self.subTest(deny=deny, two_keys=two_keys), \
                    self.assertRaisesRegex(FactoryConfigError, "two_keys|not covered"):
                FactoryConfig.from_env({**ENV, **TWO_KEYS_ENV, "FORGEHANDS_MERGE_POLICY": policy}, REPOSITORY)
        covered = json.dumps({"owner/infrastructure": {"deny": ["**/compose.yaml"],
                                                       "two_keys": ["stacks/app/compose.yaml"]}})
        cfg = FactoryConfig.from_env({**ENV, **TWO_KEYS_ENV, "FORGEHANDS_MERGE_POLICY": covered}, REPOSITORY)
        self.assertEqual(cfg.repository_merge_policy(REPOSITORY)["two_keys"], ("stacks/app/compose.yaml",))

    def test_two_keys_without_deny_is_an_error(self):
        for rule in ({"deny": [], "two_keys": ["infra/**"]}, {"two_keys": ["infra/**"]}):
            policy = json.dumps({"owner/infrastructure": rule})
            with self.subTest(rule=rule), self.assertRaisesRegex(FactoryConfigError, "two_keys.*needs a deny list"):
                FactoryConfig.from_env({**ENV, **TWO_KEYS_ENV, "FORGEHANDS_MERGE_POLICY": policy}, REPOSITORY)

    def test_reasons_must_name_a_deny_pattern_and_carry_text(self):
        bad_rules = ({"deny": ["infra/**"], "reasons": {"AGENTS.md": "not denied"}},
                     {"deny": ["infra/**"], "reasons": {"infra/**": ""}},
                     {"deny": ["infra/**"], "reasons": {"infra/**": "  "}},
                     {"deny": ["infra/**"], "reasons": {"infra/**": 1}},
                     {"deny": ["infra/**"], "reasons": ["infra/**"]},
                     {"reasons": {"infra/**": "no deny list"}})
        for rule in bad_rules:
            policy = json.dumps({"owner/infrastructure": rule})
            with self.subTest(rule=rule), self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_MERGE_POLICY"):
                FactoryConfig.from_env({**ENV, **TWO_KEYS_ENV, "FORGEHANDS_MERGE_POLICY": policy}, REPOSITORY)

    def test_bad_two_keys_values_and_unknown_keys_are_rejected(self):
        bad_rules = ({"deny": ["infra/**"], "two_keys": "infra/**"},
                     {"deny": ["infra/**"], "two_keys": [""]},
                     {"deny": ["infra/**"], "two_keys": ["/abs"]},
                     {"deny": ["infra/**"], "two_keys": [1]},
                     {"deny": ["infra/**"], "two_key": ["infra/**"]},
                     {"deny": ["infra/**"], "reason": {}})
        for rule in bad_rules:
            policy = json.dumps({"owner/infrastructure": rule})
            with self.subTest(rule=rule), self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_MERGE_POLICY"):
                FactoryConfig.from_env({**ENV, **TWO_KEYS_ENV, "FORGEHANDS_MERGE_POLICY": policy}, REPOSITORY)

    def test_a_bad_security_reviewer_login_or_an_empty_entry_is_rejected(self):
        for value in ("openhands security", "openhands-security,", "openhands-security,,someone"):
            with self.subTest(value=value), self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_SECURITY_REVIEWERS"):
                FactoryConfig.from_env({**ENV, **TWO_KEYS_ENV, "FORGEHANDS_SECURITY_REVIEWERS": value}, REPOSITORY)
        with self.assertRaisesRegex(FactoryConfigError, "FORGEHANDS_SECURITY_TOKEN") as caught:
            FactoryConfig.from_env({**ENV, **TWO_KEYS_ENV, "FORGEHANDS_SECURITY_TOKEN": "good\nleak-value",
                                    "FORGEHANDS_MERGE_POLICY":
                                        '{"owner/infrastructure": {"deny": ["infra/**"], "two_keys": ["infra/**"]}}'},
                                   REPOSITORY)
        self.assertNotIn("leak-value", str(caught.exception))

    def test_an_old_policy_shape_without_the_new_fields_is_unchanged(self):
        cfg = FactoryConfig.from_env({**ENV, "FORGEHANDS_MERGE_POLICY":
                                      '{"owner/infrastructure": {"deny": ["docs/**"]}}'}, REPOSITORY)
        self.assertEqual(cfg.repository_merge_policy(REPOSITORY), {"deny": ("docs/**",)})
        self.assertEqual((cfg.security_reviewers, cfg.security_token), ((), ""))

    def test_a_policy_without_two_keys_does_not_need_the_security_reviewer(self):
        cfg = FactoryConfig.from_env({**ENV, "FORGEHANDS_SECURITY_REVIEWERS": "openhands-security",
                                      "FORGEHANDS_MERGE_POLICY":
                                          '{"owner/infrastructure": {"deny": ["docs/**"], "reasons": {"docs/**": "docs"}}}'},
                                     REPOSITORY)
        self.assertEqual(cfg.security_token, "")
        self.assertEqual(cfg.repository_merge_policy(REPOSITORY),
                         {"deny": ("docs/**",), "reasons": {"docs/**": "docs"}})


def _glob_matches(pattern: str, path: str) -> bool:
    """``**`` crosses folders, ``*`` does not; enough to check the default deny list."""
    parts = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(parts), path) is not None


def _attribute(name: str) -> str:
    return {
        "OPENHANDS_LOCAL_BACKEND_API_KEY": "openhands_backend_key",
        "FORGEHANDS_GATEWAY_API_KEY": "gateway_api_key",
        "FORGEHANDS_FORGEJO_AGENT_TOKEN": "forgejo_agent_token",
        "FORGEHANDS_FORGEJO_WEBHOOK_SECRET": "forgejo_webhook_secret",
    }[name]


if __name__ == "__main__":
    unittest.main()
