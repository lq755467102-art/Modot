"""Check the default budget and per-model overrides in the wire request."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from loop import models
from loop.provider import Msg, OpenAICompatProvider


class OutputBudgetTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("HARNESS")}
        environment.update(HARNESS_MODELS=str(Path(scratch.name) / "models.json"),
                           HARNESS_CTX="131072", HARNESS_PROXY="")
        for override in (
            patch.dict(os.environ, environment, clear=True),
            patch.object(OpenAICompatProvider, "_read_creds", return_value={}),
            patch.object(models, "_creds", return_value={}),
        ):
            override.start()
            self.addCleanup(override.stop)

    def test_default_output_budget_applies_to_each_compatible_endpoint(self):
        for endpoint, model, field in (
            ("https://api.deepseek.com/v1", "deepseek-flash", "max_tokens"),
            ("https://gateway.example/v1", "fixture", "max_tokens"),
            ("https://api.openai.com/v1", "fixture", "max_completion_tokens"),
        ):
            with self.subTest(endpoint=endpoint):
                provider = OpenAICompatProvider(base_url=endpoint, model=model, api_key="fixture")
                self.assertEqual(provider.request_context_limit, 131072)
                self.assertEqual(provider._body([Msg("user", "hello")], [], stream=True)[field],
                                 128000)

    def test_explicit_limits_and_model_capability_still_apply(self):
        profile = models.upsert(name="fixture", base_url="https://gateway.example/v1",
                                api_key="fixture", model="fixture", max_output_tokens=64000)
        self.assertTrue(profile["ok"])
        models.select("main", profile["id"])
        provider = OpenAICompatProvider()
        self.assertEqual(provider._body([], [])["max_tokens"], 64000)
        self.assertEqual(provider._body([], [], max_tokens=24000)["max_tokens"], 24000)
        self.assertNotIn("max_tokens", provider._body([], [], max_tokens=0))
