"""Grounded document-chat query rewriting and answer generation."""

from __future__ import annotations

import re
from typing import Any

from app.utils.model_provider import ModelProvider
from app.config.config import Settings
from app.ai_module.prompt_manager import PromptManager

SMALL_TALK = re.compile(
    r"(?:(?:hi|hello|hey|good morning|good afternoon|good evening|namaste)[.!?, ]+)?"
    r"(?:hi|hello|hey|good morning|good afternoon|good evening|namaste|"
    r"how are you(?: doing)?(?: today)?|how are u|how is it going|how's it going|what's up|"
    r"what are you doing(?: right now)?|what you are doing|who are you|what are you|"
    r"what(?: can| do) you do|what can you help (?:me |us )?with|"
    r"can you help me|tell me about yourself|what is edis|"
    r"how does edis work|kaise ho|kya kar rahe ho|"
    r"thank you(?: so much)?|thanks(?: a lot)?|bye|goodbye)"
)


class DocumentChatService:
    def __init__(self, settings: Settings, models: ModelProvider):
        self.settings = settings
        self.models = models

    @staticmethod
    def is_small_talk(question: str) -> bool:
        normalized = " ".join(question.casefold().replace("’", "'").split()).strip(" .!?,")
        return SMALL_TALK.fullmatch(normalized) is not None

    def answer_directly(self, question: str) -> str:
        from langchain_core.output_parsers import StrOutputParser

        chain = PromptManager.direct_chat() | self.models.model | StrOutputParser()
        return chain.invoke({"question": question})

    def answer(self, question: str, hits: list[dict], history: list[dict]) -> str:
        if not hits:
            return "I don't know based on the uploaded documents."

        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.output_parsers import StrOutputParser

        messages: list[Any] = []
        for item in history[-12:]:
            if item.get("role") == "user":
                messages.append(HumanMessage(content=item["content"]))
            elif item.get("role") == "assistant":
                messages.append(AIMessage(content=item["content"]))
        context = "\n\n".join(
            f"[Source: {hit['document']} page {hit['page']}]\n{hit['text']}"
            for hit in hits
        )[:20_000]
        prompt = PromptManager.document_chat()
        return (prompt | self.models.model | StrOutputParser()).invoke({
            "context": context,
            "history": messages,
            "question": question,
        })

    def rewrite_query(self, question: str, history: list[dict]) -> str:
        if not history:
            return question

        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.output_parsers import StrOutputParser

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
