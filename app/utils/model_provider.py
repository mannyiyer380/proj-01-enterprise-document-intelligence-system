"""Lazy construction of the configured hosted chat model."""

from __future__ import annotations

from app.config.config import Settings


class ModelProvider:
    """Build and cache the selected provider model only when it is needed."""

    def __init__(self, settings: Settings, provider: str | None = None):
        self.settings = settings
        self.provider = provider or settings.llm_provider
        self._model = None

    @property
    def model(self):
        if self._model is None:
            if self.provider == "openai":
                from langchain_openai import ChatOpenAI

                self._model = ChatOpenAI(
                    model=self.settings.openai_chat_model,
                    api_key=self.settings.openai_api_key,
                    timeout=30,
                    max_retries=0,
                )
            elif self.provider == "google":
                from langchain_google_genai import ChatGoogleGenerativeAI

                self._model = ChatGoogleGenerativeAI(
                    model=self.settings.google_chat_model,
                    google_api_key=self.settings.google_api_key,
                    temperature=0,
                )
            elif self.provider == "groq":
                from groq import RateLimitError
                from langchain_groq import ChatGroq

                model_ids = dict.fromkeys((self.settings.groq_chat_model, *self.settings.groq_fallback_models))
                primary, *fallbacks = [
                    ChatGroq(
                        model=model_id,
                        api_key=self.settings.groq_api_key,
                        temperature=0,
                        max_retries=0,
                    )
                    for model_id in model_ids
                ]
                self._model = primary.with_fallbacks(fallbacks, exceptions_to_handle=(RateLimitError,))
        return self._model
