"""Document metadata and summary generation."""

from __future__ import annotations

import math
import time

from fastapi import HTTPException
from groq import APIStatusError, RateLimitError
from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import StrOutputParser
from pydantic import ValidationError
from structlog import get_logger

from app.ai_module.document_ingestion import ParsedDocument
from app.utils.model_provider import ModelProvider
from app.ai_module.prompt_manager import PromptManager
from app.ai_module.schemas import DocumentAnalysis
from app.config.config import Settings
from app.utils.custom_logger import LOGGER_NAME

ANALYSIS_BATCH_CHARS = 16_000
FINAL_SUMMARY_CHARS = 8_000
PART_SUMMARY_CHARS = 1_600
logger = get_logger(LOGGER_NAME)


class DocumentAnalysisService:
    def __init__(self, settings: Settings, models: ModelProvider):
        self.settings = settings
        self.models = models

    def analyze(self, document: ParsedDocument) -> DocumentAnalysis:
        text = "\n\n".join(page.text for page in document.pages if page.text)
        model = self.analysis_model()

        parser = PromptManager.analysis_parser(DocumentAnalysis)
        final_chain = PromptManager.document_analysis(DocumentAnalysis) | model | parser
        inputs = {
            "provider": self.settings.llm_provider,
            "filename": document.filename,
            "pages": len(document.pages),
        }
        try:
            raw = self._analyze_text(text, final_chain, model, inputs)
        except APIStatusError as exc:
            if exc.status_code != 413:
                raise
            raise HTTPException(
                413, "Groq rejected the analysis input at its current token limit. Try a shorter document."
            ) from None
        if not isinstance(raw, dict):
            raise OutputParserException("Expected an analysis JSON object.")
        try:
            return DocumentAnalysis.model_validate({
                **raw, "page_count": len(document.pages), "mode": self.settings.llm_provider,
            })
        except ValidationError:
            raise OutputParserException("The analysis output did not match its schema.") from None

    def _analyze_text(self, text, final_chain, model, inputs):
        batch_size = ANALYSIS_BATCH_CHARS
        if len(text) <= batch_size:
            try:
                return self.invoke_model(final_chain, {**inputs, "text": text})
            except APIStatusError as exc:
                if exc.status_code != 413:
                    raise
                batch_size = max(1, len(text) // 2)

        sections = list(self._sections(text, batch_size))
        logger.info("document_analysis_segmented", sections=len(sections))
        chain = PromptManager.summarize_analysis_section() | model | StrOutputParser()
        summaries = []
        for index, section in enumerate(sections, start=1):
            summaries.append(self._summarize(section, chain))
            logger.info("document_analysis_progress", completed_sections=index, total_sections=len(sections))
        logger.info("document_analysis_combining", sections=len(summaries))
        combined = self._combine_summaries(summaries, chain)
        try:
            return self.invoke_model(final_chain, {**inputs, "text": combined})
        except APIStatusError as exc:
            if exc.status_code != 413:
                raise
            shorter = self._summarize(combined, chain)
            return self.invoke_model(final_chain, {**inputs, "text": shorter})

    def analysis_model(self):
        model = self.models.model
        return model.bind(max_tokens=1024) if self.settings.llm_provider == "groq" else model

    def comparison_sections(self, text: str, model, side: str) -> tuple[list[str], str]:
        chain = PromptManager.summarize_analysis_section() | model | StrOutputParser()
        sections = list(self._sections(text, 12_000))
        summaries = []
        for index, section in enumerate(sections, start=1):
            summaries.append(self._summarize(section, chain))
            logger.info(
                "document_compare_progress", document=side,
                completed_sections=index, total_sections=len(sections),
            )
        return summaries, self._combine_summaries(summaries, chain, max_chars=4_000)

    def _combine_summaries(self, summaries: list[str], chain, max_chars: int = FINAL_SUMMARY_CHARS) -> str:
        combined = "\n\n".join(summaries)
        while len(combined) > max_chars:
            groups = [summaries[i:i + 4] for i in range(0, len(summaries), 4)]
            summaries = [
                self._summarize("\n\n".join(group), chain) if len(group) > 1 else group[0]
                for group in groups
            ]
            combined = "\n\n".join(summaries)
        return combined

    def _summarize(self, text: str, chain) -> str:
        try:
            summary = self.invoke_model(chain, {"text": text}).strip()
        except APIStatusError as exc:
            if exc.status_code != 413 or len(text) <= 512:
                raise
            summaries = [
                self._summarize(part, chain)
                for part in self._sections(text, (len(text) + 1) // 2)
            ]
            limit = (PART_SUMMARY_CHARS - 2 * (len(summaries) - 1)) // len(summaries)
            return "\n\n".join(summary[:limit] for summary in summaries)
        if not summary:
            raise HTTPException(502, "The analysis provider returned an empty section summary.")
        return summary[:PART_SUMMARY_CHARS]

    @staticmethod
    def _sections(text: str, limit: int):
        start = 0
        while start < len(text):
            end = min(start + limit, len(text))
            if end < len(text):
                boundary = text.rfind("\n", start + limit // 2, end)
                if boundary < 0:
                    boundary = text.rfind(" ", start + limit // 2, end)
                if boundary > start:
                    end = boundary + 1
            yield text[start:end]
            start = end

    @staticmethod
    def invoke_model(chain, payload):
        for attempt in range(3):
            try:
                return chain.invoke(payload)
            except RateLimitError as exc:
                retry_after = exc.response.headers.get("retry-after", "60")
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = 60
                if attempt == 2 or not math.isfinite(delay) or delay > 60:
                    raise HTTPException(429, "Groq rate limit reached. Try again after the quota resets.") from None
                delay = max(1, delay)
                logger.info("document_analysis_rate_retry", wait_seconds=delay, attempt=attempt + 1)
                time.sleep(delay)
