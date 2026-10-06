"""Banned-origin model enforcement (README, Model policy).

This test fails CI if anyone tries to add a Chinese-origin slug to the allowed
list, or if a caller bypasses the check. Both violate the model-origin policy.
"""
from __future__ import annotations

import unittest

from coding_harness.models.ollama import (
    BANNED_MODEL_PREFIXES,
    BannedModelError,
    assert_model_allowed,
)


class BannedModelTests(unittest.TestCase):
    REQUIRED_PREFIXES = {
        "qwen", "deepseek", "yi:", "yi-", "baichuan", "chatglm", "glm-", "glm4",
        "internlm", "minimax", "kimi", "moonshot", "hunyuan", "bytedance",
        "doubao", "seed-", "ernie", "baidu", "zhipu", "stepfun", "skywork",
        "inclusionai", "ling-", "qwq", "yi", "internvl", "cogvlm", "cogagent",
        "marco-o1", "pangu", "telechat", "xverse", "aquila",
    }

    def test_required_banned_prefixes_present(self) -> None:
        missing = self.REQUIRED_PREFIXES - set(BANNED_MODEL_PREFIXES)
        self.assertFalse(
            missing,
            f"banned-prefix list is missing required entries: {sorted(missing)}. "
            "The model-origin policy forbids removing these.",
        )

    def test_known_banned_models_refused(self) -> None:
        cases = [
            "qwen2.5:32b",
            "Qwen2-VL-7B",
            "deepseek-coder:6.7b",
            "DeepSeek-V3",
            "yi:34b",
            "yi-34b-chat",
            "baichuan2:13b",
            "chatglm3-6b",
            "glm-4-9b",
            "glm4:9b",
            "internlm2:20b",
            "minimax-text-01",
            "kimi-k1.5",
            "hunyuan-large",
            "hf.co/Qwen/Qwen3-8B",
            "hf.co/deepseek-ai/DeepSeek-V3",
            "bytedance/seed-oss",
            "seed-coder:8b",
            "moonshotai/kimi-k2",
            "ernie4.5:21b",
            "zhipu/glm-4.5",
            "stepfun/step3",
            "skywork-r1v",
            "inclusionai/ling-lite",
            "codeqwen:7b",
            "hf.co/bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF",
            "qwq:32b",
            "QwQ-32B-Preview",
            "internvl2:8b",
            "cogvlm2-llama3",
            "cogagent-9b",
            "marco-o1:7b",
            "pangu-pro-moe",
            "telechat2:7b",
            "xverse-13b",
            "aquila2-7b",
            "yi",
            "Yi_1.5-9b",
            "yi.34b",
            "hf.co/01-ai/Yi-1.5-9B-Chat",
        ]
        for model in cases:
            with self.subTest(model=model):
                with self.assertRaises(BannedModelError):
                    assert_model_allowed(model)

    def test_allowed_models_pass(self) -> None:
        for model in [
            "hermes3:8b",
            "gemma4:26b",
            "mistral:7b",
            "llama3.1:8b",
            "phi4-mini",
            "claude-sonnet-4-20250514",  # not an Ollama model but the check is name-only
            "devstral-small-2:latest",
            "devstral-small-2:24b-instruct-2512-q4_K_M",
            "granite4.2:8b",
            "granite4.2:30b",
            "laguna-xs-2.1:q4_K_M",
            "nemotron-3-nano:30b",
            "gpt-oss:20b",
            "nomic-embed-text",
            "hf.co/mistralai/Devstral-Small-2",
            "hf.co/ibm-granite/granite-4.2-8b",
            "hf.co/nvidia/Nemotron-3-Nano",
        ]:
            with self.subTest(model=model):
                # Should not raise.
                assert_model_allowed(model)

    def test_short_slug_inside_a_word_is_not_banned(self) -> None:
        for model in ["sparkling-3b", "linseed-v1", "hoyi-1b", "kyi:7b", "hglm-1b",
                      "yield-7b", "yin:3b", "kiyi"]:
            with self.subTest(model=model):
                assert_model_allowed(model)


if __name__ == "__main__":
    unittest.main()
