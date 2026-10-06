"""env_scrub: credential-shaped variable names are dropped, everything else survives."""
from __future__ import annotations

import unittest

from coding_harness.security import env_scrub

SECRETS = {
    "OLLAMA_API_KEY": "k",
    "SOME_TOKEN": "t",
    "CLIENT_SECRET": "s",
    "DB_PASSWORD": "p",
    "PASSWORD": "p",
    "MyPasswordFile": "p",
    "AWS_ACCESS_KEY_ID": "a",
    "AWS_REGION": "us-west-2",
    "ANTHROPIC_API_KEY": "a",
    "ANTHROPIC_BASE_URL": "u",
    "OPENAI_API_KEY": "o",
    "OPENAI_ORG": "o",
    "GH_TOKEN": "g",
    "GH_HOST": "h",
    "GITHUB_TOKEN": "g",
    "npm_config_token": "n",
}
KEPT = {
    "PATH": "/usr/bin",
    "HOME": "/Users/x",
    "VIRTUAL_ENV": "/Users/x/.venv",
    "LANG": "en_US.UTF-8",
    "TERM": "xterm",
    "TMPDIR": "/tmp",
    "OLLAMA_URL": "http://localhost:11434",
    "HARNESS_REVIEW": "0",
    "KEYBOARD": "us",
    "TOKENIZERS_PARALLELISM": "false",
    "GITHUB_ACTIONS": "true",
    "SECRET_SAUCE": "x",
}


class ScrubTests(unittest.TestCase):
    def test_secret_shaped_names_are_dropped(self) -> None:
        out = env_scrub.scrub({**SECRETS, **KEPT})
        self.assertEqual(out, KEPT)

    def test_each_secret_name_is_flagged(self) -> None:
        for name in SECRETS:
            with self.subTest(name=name):
                self.assertTrue(env_scrub.is_secret_name(name))
        for name in KEPT:
            with self.subTest(name=name):
                self.assertFalse(env_scrub.is_secret_name(name))

    def test_returns_a_fresh_dict_and_leaves_the_input_alone(self) -> None:
        env = dict(SECRETS)
        out = env_scrub.scrub(env)
        self.assertEqual(out, {})
        self.assertEqual(env, SECRETS)

    def test_empty_env(self) -> None:
        self.assertEqual(env_scrub.scrub({}), {})


if __name__ == "__main__":
    unittest.main()
