"""Production defaults and explicit legacy configuration, isolated from local keys."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


def inspect_config(overrides):
    with tempfile.TemporaryDirectory() as directory:
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT),
               "REGULAR_HOME": directory, "QQ_APP_ID": "fake", "QQ_APP_SECRET": "fake"}
        env.update(overrides)
        script = """import config,json
print(json.dumps({"provider": config.AI_PROVIDER,"vision_enabled":config.VISION_ENABLED,
 "vision_provider":config.VISION_PROVIDER,"vision_model":config.VISION_MODEL,
 "errors":config.validate()}))"""
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                                text=True, check=True)
        return json.loads(result.stdout)


class ProviderConfigurationTest(unittest.TestCase):
    def test_no_configuration_never_selects_a_vendor_or_implicit_vision(self):
        cfg = inspect_config({})
        self.assertEqual(cfg["provider"], "openai")
        self.assertFalse(cfg["vision_enabled"])
        self.assertEqual(cfg["vision_provider"], "openai")
        self.assertEqual(cfg["vision_model"], "")
        self.assertTrue(any("OPENAI_BASE_URL" in error for error in cfg["errors"]))
        self.assertTrue(any("OPENAI_MODELS" in error for error in cfg["errors"]))

    def test_explicit_legacy_preset_remains_valid_without_implicit_vision(self):
        cfg = inspect_config({"AI_PROVIDER": "zhipu", "ZHIPU_API_KEY": "fake"})
        self.assertEqual(cfg["provider"], "zhipu")
        self.assertEqual(cfg["vision_provider"], "zhipu")
        self.assertFalse(cfg["vision_enabled"])
        self.assertEqual(cfg["vision_model"], "")
        self.assertEqual(cfg["errors"], [])

    def test_enabled_vision_requires_an_explicit_model(self):
        cfg = inspect_config({"AI_PROVIDER": "zhipu", "ZHIPU_API_KEY": "fake", "VISION_ENABLED": "true"})
        self.assertTrue(any("VISION_MODEL" in error for error in cfg["errors"]))
        configured = inspect_config({"AI_PROVIDER": "zhipu", "ZHIPU_API_KEY": "fake",
                                     "VISION_ENABLED": "true", "VISION_MODEL": "explicit-image-model"})
        self.assertEqual(configured["errors"], [])

    def test_explicit_generic_endpoint_is_valid(self):
        cfg = inspect_config({"OPENAI_BASE_URL": "https://model.invalid/v1",
                              "OPENAI_API_KEY": "fake", "OPENAI_MODELS": "explicit-chat-model"})
        self.assertEqual(cfg["provider"], "openai")
        self.assertEqual(cfg["errors"], [])
