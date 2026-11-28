import os
import unittest
from unittest.mock import patch

from app.config.config import Settings
from app.utils.model_provider import ModelProvider


class ModelProviderConfigTests(unittest.TestCase):
    def test_defaults_chat_and_analysis_to_groq_even_with_other_keys(self):
        environment = {
            "OPENAI_API_KEY": "test-openai-key",
            "GROQ_API_KEY": "test-groq-key",
            "GOOGLE_API_KEY": "test-google-key",
            "HF_TOKEN": "test-huggingface-token",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.llm_provider, "groq")
        self.assertEqual(settings.chat_provider, "groq")
        self.assertEqual(settings.embedding_provider, "huggingface")
        self.assertEqual(ModelProvider(settings).model.runnable.model_name, settings.groq_chat_model)

    def test_requires_openai_key_instead_of_falling_back_to_another_llm_provider(self):
        environment = {
            "LLM_PROVIDER": "openai",
            "GROQ_API_KEY": "test-groq-key",
            "GOOGLE_API_KEY": "test-google-key",
            "HF_TOKEN": "test-huggingface-token",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY required"):
                Settings.from_env()

    def test_global_provider_uses_selected_model_and_only_its_key(self):
        for provider, prefix, model_id in (
            ("openai", "OPENAI", "gpt-4.1"),
            ("google", "GOOGLE", "gemini-3.6-flash"),
        ):
            environment = {
                "LLM_PROVIDER": provider,
                f"{prefix}_API_KEY": "test-selected-key",
                f"{prefix}_CHAT_MODEL": model_id,
                "HF_TOKEN": "test-huggingface-token",
            }
            with self.subTest(provider=provider), patch.dict(os.environ, environment, clear=True):
                settings = Settings.from_env()
                self.assertEqual(settings.llm_provider, provider)
                self.assertEqual(settings.chat_provider, provider)
                model = ModelProvider(settings).model
                configured_model = model.model_name if provider == "openai" else model.model.removeprefix("models/")
                self.assertEqual(configured_model, model_id)

    def test_chat_override_can_use_a_different_provider(self):
        environment = {
            "CHAT_LLM_PROVIDER": "openai",
            "GROQ_API_KEY": "test-groq-key",
            "OPENAI_API_KEY": "test-openai-key",
            "HF_TOKEN": "test-huggingface-token",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.llm_provider, "groq")
        self.assertEqual(settings.chat_provider, "openai")
