"""Local configuration reload and secret handling without real credentials."""
from dataclasses import replace
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from copilot.config import Settings, load_local_env


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ("config-test-" + uuid4().hex)
        self.root.mkdir()
        self.env_file = self.root / ".env"
        self.scope = patch("copilot.config.PROJECT_ROOT", self.root)
        self.scope.start()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.scope.stop()
        if self.root.resolve().parent == Path(__file__).resolve().parent and self.root.name.startswith("config-test-"):
            shutil.rmtree(self.root)

    def write(self, text):
        self.env_file.write_text(text, encoding="utf-8")

    def test_file_edits_and_deletion_take_effect_without_stale_environment(self):
        self.write("OPENROUTER_API_KEY=first-placeholder\nCOPILOT_MODEL=google/gemma-first:free\n")
        first = Settings.from_env()
        self.assertEqual(first.api_key, "first-placeholder")
        self.assertEqual(first.model, "google/gemma-first:free")
        self.assertNotIn("OPENROUTER_API_KEY", os.environ)
        self.write("OPENROUTER_API_KEY=second-placeholder\nCOPILOT_MODEL=google/gemma-second:free\n")
        second = Settings.from_env()
        self.assertEqual(second.api_key, "second-placeholder")
        self.assertEqual(second.model, "google/gemma-second:free")
        self.env_file.unlink()
        self.assertEqual(Settings.from_env().api_key, "")

    def test_real_environment_has_precedence_and_is_not_mutated(self):
        self.write("OPENROUTER_API_KEY=file-placeholder\nCOPILOT_MODEL=file-model\n")
        os.environ["OPENROUTER_API_KEY"] = "environment-placeholder"
        os.environ["COPILOT_MODEL"] = "environment-model"
        settings = Settings.from_env()
        self.assertEqual(settings.api_key, "environment-placeholder")
        self.assertEqual(settings.model, "environment-model")
        self.assertEqual(os.environ["OPENROUTER_API_KEY"], "environment-placeholder")

    def test_provider_is_fixed_and_legacy_provider_keys_cannot_be_used(self):
        self.write("COPILOT_PROVIDER=google\nGOOGLE_API_KEY=old-placeholder\nGEMINI_API_KEY=old-placeholder\nCOPILOT_GOOGLE_TPM_LIMIT=not-an-integer\n")
        settings = Settings.from_env()
        self.assertEqual(settings.provider, "openrouter")
        self.assertEqual(settings.api_key, "")
        self.assertEqual(settings.model, "google/gemma-4-31b-it:free")
        self.assertFalse(hasattr(settings, "google_tpm_limit"))
        with self.assertRaises((TypeError, ValueError)):
            replace(settings, provider="google")
        with self.assertRaises(TypeError):
            Settings(provider="google")

    def test_parser_keeps_shell_syntax_inert_and_handles_comments(self):
        self.write("# comment\nexport OPENROUTER_API_KEY='placeholder#literal' # comment\nCOPILOT_FIELD_TIMEZONE=UTC # explanation\nUNSAFE=$(echo should-not-run)\n9INVALID=value\nMALFORMED\nBAD='unfinished\n")
        values = load_local_env()
        self.assertEqual(values["OPENROUTER_API_KEY"], "placeholder#literal")
        self.assertEqual(values["COPILOT_FIELD_TIMEZONE"], "UTC")
        self.assertEqual(values["UNSAFE"], "$(echo should-not-run)")
        self.assertNotIn("9INVALID", values)
        self.assertNotIn("BAD", values)

    def test_api_key_never_appears_in_settings_repr(self):
        self.write("OPENROUTER_API_KEY=repr-placeholder-secret\n")
        self.assertNotIn("repr-placeholder-secret", repr(Settings.from_env()))

    def test_openrouter_is_default_without_provider_selection(self):
        self.write("OPENROUTER_API_KEY=router-placeholder\nCOPILOT_MAX_OUTPUT_TOKENS=1234\n")
        settings = Settings.from_env()
        self.assertEqual(settings.provider, "openrouter")
        self.assertEqual(settings.model, "google/gemma-4-31b-it:free")
        self.assertEqual(settings.api_key, "router-placeholder")
        self.assertEqual(settings.max_output_tokens, 1234)

    def test_model_round_limit_and_request_size_budget_remain_bounded(self):
        self.assertEqual(Settings.from_env().max_tool_rounds, 3)
        self.assertEqual(Settings.from_env().max_input_tokens, 12000)
        self.write("COPILOT_MAX_TOOL_ROUNDS=8\nCOPILOT_MAX_INPUT_TOKENS=10000\n")
        self.assertEqual(Settings.from_env().max_tool_rounds, 3)
        self.assertEqual(Settings.from_env().max_input_tokens, 10000)


if __name__ == "__main__":
    unittest.main()
