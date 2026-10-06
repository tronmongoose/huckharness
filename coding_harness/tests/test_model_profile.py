"""Per-model sampling profiles (P1-1): family resolution, env overrides, hash, session wiring."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core.session import Session
from coding_harness.models.profile import (
    GENERIC,
    ModelProfile,
    profile_hash,
    resolve_profile,
    sampling_kwargs,
)
from coding_harness.modes.print_mode import SYSTEM_PROMPT, build_registry
from coding_harness.security import audit

_ENV_KEYS = ("HARNESS_NUM_CTX", "HARNESS_NUM_PREDICT", "HARNESS_TEMPERATURE",
             "HARNESS_THINK", "HARNESS_KEEP_ALIVE")


def _clean_env(**values: str) -> mock._patch:
    """Patch os.environ so only the given HARNESS_* overrides are present."""
    import os
    env = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS}
    env.update(values)
    return mock.patch.dict("os.environ", env, clear=True)


class TestFamilyResolution(unittest.TestCase):
    def test_tags_map_to_families(self) -> None:
        cases = {
            "devstral-small-2:latest": "devstral",
            "mistral-small3.2:latest": "mistral-small",
            "hf.co/mistralai/Devstral-Small-2": "devstral",
            "granite4.2:8b": "granite4.2",
            "granite4.1:3b": "granite4.1",
            "laguna-xs-2.1:q4_K_M": "laguna",
            "nemotron-3-nano:30b": "nemotron-3-nano",
            "gpt-oss:20b": "gpt-oss",
            "gemma4:26b": "gemma",
        }
        with _clean_env():
            for tag, family in cases.items():
                self.assertEqual(resolve_profile(tag).family, family, tag)

    def test_unknown_tag_is_generic_with_todays_constants(self) -> None:
        with _clean_env():
            p = resolve_profile("llama3.1:8b")
        self.assertEqual(p, GENERIC)
        self.assertEqual((p.temperature, p.num_predict, p.num_ctx), (0.2, 2048, 32768))
        self.assertIsNone(p.top_p)

    def test_family_defaults(self) -> None:
        with _clean_env():
            mistral = resolve_profile("mistral-small3.2:latest")
            laguna = resolve_profile("laguna-xs-2.1:q4_K_M")
            granite = resolve_profile("granite4.2:8b")
        self.assertEqual((mistral.temperature, mistral.top_p, mistral.top_k), (0.15, 0.95, 40))
        self.assertEqual((mistral.num_ctx, mistral.num_predict, mistral.think), (32768, 4096, False))
        self.assertIn("slice $.Messages $index", mistral.template_lint or "")
        self.assertEqual((laguna.temperature, laguna.top_p, laguna.top_k, laguna.think),
                         (1.0, 1.0, 20, True))
        self.assertEqual((granite.temperature, granite.supports_format), (0.1, True))
        self.assertFalse(mistral.supports_format)
        self.assertEqual(mistral.tool_aliases, ())


class TestEnvOverrides(unittest.TestCase):
    def test_valid_overrides_apply(self) -> None:
        with _clean_env(HARNESS_NUM_CTX="8192", HARNESS_NUM_PREDICT="512",
                        HARNESS_TEMPERATURE="0.7", HARNESS_THINK="1", HARNESS_KEEP_ALIVE="30m"):
            p = resolve_profile("mistral-small3.2:latest")
        self.assertEqual((p.num_ctx, p.num_predict, p.temperature), (8192, 512, 0.7))
        self.assertTrue(p.think)
        self.assertEqual(p.keep_alive, "30m")
        self.assertEqual(p.family, "mistral-small")

    def test_invalid_overrides_are_ignored(self) -> None:
        with _clean_env(HARNESS_NUM_CTX="lots", HARNESS_NUM_PREDICT="-1",
                        HARNESS_TEMPERATURE="warm", HARNESS_THINK="yes", HARNESS_KEEP_ALIVE=""):
            p = resolve_profile("mistral-small3.2:latest")
        with _clean_env():
            base = resolve_profile("mistral-small3.2:latest")
        self.assertEqual(p, base)


class TestHashAndKwargs(unittest.TestCase):
    def test_hash_is_stable_and_tracks_temperature(self) -> None:
        a = ModelProfile("x", temperature=0.2)
        b = ModelProfile("x", temperature=0.2)
        c = ModelProfile("x", temperature=0.3)
        self.assertEqual(profile_hash(a), profile_hash(b))
        self.assertNotEqual(profile_hash(a), profile_hash(c))
        self.assertEqual(len(profile_hash(a)), 12)
        int(profile_hash(a), 16)

    def test_sampling_kwargs_shape(self) -> None:
        p = ModelProfile("x", temperature=0.15, top_p=0.95, num_predict=4096, num_ctx=65536)
        self.assertEqual(sampling_kwargs(p), {"temperature": 0.15, "max_tokens": 4096, "top_p": 0.95})
        self.assertEqual(sampling_kwargs(GENERIC),
                         {"temperature": 0.2, "max_tokens": 2048, "top_p": None})


class TestSessionWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", tmp_path),
            mock.patch("coding_harness.core.session.SESSIONS_DIR", tmp_path / "s"),
            _clean_env(),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _run(self, model: str, model_override: str | None = None) -> tuple[dict, Session]:
        seen: dict = {}

        def _chat(**kw):
            seen.update(kw)
            return {"role": "assistant", "content": "ok", "tool_calls": [],
                    "_usage": {"tokens_in": 1, "tokens_out": 1, "thinking_tokens": None}}

        registry = build_registry(event_sink=None, enable_mcp=False)
        session = Session(model=model, registry=registry, system_prompt=SYSTEM_PROMPT,
                          force_local=True)
        with mock.patch("coding_harness.core.session.ollama.chat", side_effect=_chat):
            session.run_turn("go", model_override=model_override)
        return seen, session

    def test_session_start_records_profile_and_hash(self) -> None:
        _seen, session = self._run("mistral-small3.2:latest")
        records = [json.loads(ln) for ln in session.session_log_path.read_text().splitlines()]
        start = next(r for r in records if r["kind"] == "session_start")
        self.assertEqual(start["profile"]["family"], "mistral-small")
        self.assertEqual(start["profile"]["temperature"], 0.15)
        self.assertEqual(start["profile_hash"], profile_hash(session.profile))

    def test_chat_receives_family_sampling(self) -> None:
        seen, _session = self._run("mistral-small3.2:latest")
        self.assertEqual(seen["temperature"], 0.15)
        self.assertEqual(seen["max_tokens"], 4096)
        self.assertEqual(seen["top_p"], 0.95)

    def test_override_turn_resolves_its_own_profile(self) -> None:
        seen, _session = self._run("mistral-small3.2:latest", model_override="granite4.2:8b")
        self.assertEqual(seen["model"], "granite4.2:8b")
        self.assertEqual(seen["temperature"], 0.1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
