"""Grounded document-chat query rewriting and answer generation."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from langchain_core.output_parsers import StrOutputParser
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator
from structlog import get_logger

from app.utils.model_provider import ModelProvider
from app.config.config import Settings
from app.ai_module.prompt_manager import PromptManager
from app.utils.custom_logger import LOGGER_NAME

logger = get_logger(LOGGER_NAME)
NO_DOCUMENT_EVIDENCE = "I don't know based on the uploaded documents."
NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

GREETING = r"(?:hi+|hello+|hey+|hiya|yo|good (?:morning|afternoon|evening|night)|namaste|namaskar)"
ADDRESS = r"(?:bro|brother|sis|sister|bhai|bhaiya|dost|yaar|buddy|mate|sir|ma'am|edis|there)"
SMALL_TALK_SEPARATOR = r"[.!?, ]+"
SMALL_TALK = re.compile(
    rf"(?:{ADDRESS}{SMALL_TALK_SEPARATOR})?"
    rf"(?:{GREETING}(?:{SMALL_TALK_SEPARATOR}{ADDRESS})?{SMALL_TALK_SEPARATOR})*"
    rf"(?:{GREETING}|"
    r"how (?:are|r) (?:you|u)(?: doing)?(?: today)?|how(?: is|'s) it going|"
    r"how(?: is|'s) your day(?: going)?|what(?: is|'s) up|whats up|"
    r"what are you doing(?: right now)?|what you are doing|who are you|what are you|what is your name|"
    r"what(?: can| do) you do|what can you help (?:me |us )?with|"
    r"can you help me|tell me about yourself|what is edis|"
    r"how does edis work|(?:aap )?kaise (?:ho|hain)|kya haal hai|kya kar rahe ho|"
    r"thank you(?: so much)?|thanks(?: a lot)?|bye|goodbye)"
    rf"(?:{SMALL_TALK_SEPARATOR}{ADDRESS})?"
)


class ChatRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    intent: Literal["general", "document"]
    answer: NonBlankText | None

    @model_validator(mode="after")
    def answer_matches_intent(self):
        if (self.intent == "general") != (self.answer is not None):
            raise ValueError("Only general chat must include an answer.")
        return self


class GroundedReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answer: NonBlankText | None
    source_ids: list[int] = Field(max_length=12)


def json_payload(text: str) -> str:
    text = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.IGNORECASE | re.DOTALL)
    return fence.group(1) if fence else text


class DocumentChatService:
    def __init__(self, settings: Settings, models: ModelProvider):
        self.settings = settings
        self.models = models

    @staticmethod
    def is_small_talk(question: str) -> bool:
        normalized = " ".join(question.casefold().replace("\u2019", "'").split()).strip(" .!?,")
        return SMALL_TALK.fullmatch(normalized) is not None

    @staticmethod
    def is_unsupported_answer(answer: str) -> bool:
        normalized = answer.strip().casefold().replace("\u2019", "'").rstrip(".")
        return normalized in {
            "i don't know based on the documents",
            "i don't know based on the uploaded documents",
        }

    def general_reply(self, question: str, history: list[dict], documents: list[str]) -> str | None:
        if self.is_small_talk(question):
            return self.answer_directly(question)

        recent = "\n".join(
            f"{turn['role']}: {turn['content'][:1_000]}"
            for turn in history[-6:]
        )
        for attempt in (1, 2):
            chain = PromptManager.route_chat(retry=attempt == 2) | self.models.model | StrOutputParser()
            raw = chain.invoke({
                "question": question, "history": recent, "documents": "\n".join(documents),
            })
            try:
                return ChatRoute.model_validate_json(json_payload(raw)).answer
            except ValidationError:
                logger.warning(
                    "chat_guardrail", reason="invalid_route", attempt=attempt,
                    message="The model returned an invalid chat route.",
                )
        raise HTTPException(422, "I couldn't tell whether your question was about the documents. Please rephrase it.")

    def answer_directly(self, question: str) -> str:
        chain = PromptManager.direct_chat() | self.models.model | StrOutputParser()
        return chain.invoke({"question": question})

    def answer(self, question: str, hits: list[dict], history: list[dict]) -> tuple[str, list[dict]]:
        if not hits:
            return NO_DOCUMENT_EVIDENCE, []

        from langchain_core.messages import AIMessage, HumanMessage
        messages: list[Any] = []
        for item in history[-12:]:
            if item.get("role") == "user":
                messages.append(HumanMessage(content=item["content"]))
            elif item.get("role") == "assistant":
                messages.append(AIMessage(content=item["content"]))
        context = []
        remaining = 20_000
        for hit in hits:
            text = hit["text"][:remaining]
            if not text.strip():
                continue
            context.append({**hit, "text": text})
            remaining -= len(text)
            if remaining == 0:
                break
        if not context:
            return NO_DOCUMENT_EVIDENCE, []

        raw = (PromptManager.document_chat() | self.models.model | StrOutputParser()).invoke({
            "context": json.dumps([
                {"id": source_id, "document": hit["document"], "page": hit["page"], "text": hit["text"]}
                for source_id, hit in enumerate(context, start=1)
            ], ensure_ascii=False),
            "history": messages,
            "question": question,
        })
        try:
            result = GroundedReply.model_validate_json(json_payload(raw))
        except ValidationError:
            logger.warning(
                "chat_guardrail", reason="invalid_document_answer",
                message="The model did not return a valid document answer.",
            )
            return NO_DOCUMENT_EVIDENCE, []

        if result.answer is None and not result.source_ids:
            return NO_DOCUMENT_EVIDENCE, []
        if result.answer is None or not result.source_ids or any(
            not 1 <= source_id <= len(context) for source_id in result.source_ids
        ):
            logger.warning(
                "chat_guardrail", reason="invalid_citation",
                message="The document answer did not cite valid sources from this request.",
            )
            return NO_DOCUMENT_EVIDENCE, []
        if self.is_unsupported_answer(result.answer):
            return NO_DOCUMENT_EVIDENCE, []
        cited_pages = {}
        for source_id in result.source_ids:
            hit = context[source_id - 1]
            cited_pages.setdefault((hit["document"], hit["page"]), hit)
        return result.answer, list(cited_pages.values())

    def rewrite_query(self, question: str, history: list[dict]) -> str:
        if not history:
            return question

        from langchain_core.messages import AIMessage, HumanMessage
        messages = [
            HumanMessage(content=item["content"])
            if item["role"] == "user"
            else AIMessage(content=item["content"])
            for item in history[-12:]
            if item["role"] in ("user", "assistant")
        ]
        prompt = PromptManager.rewrite_query()
        return (prompt | self.models.model | StrOutputParser()).invoke({
            "history": messages,
            "question": question,
        })
