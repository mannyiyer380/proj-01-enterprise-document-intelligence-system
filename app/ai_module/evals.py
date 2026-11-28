"""Offline document-chat evaluation against a synthetic handbook."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from unittest.mock import patch
from uuid import uuid4

import chromadb
import fitz
import structlog
from langsmith import tracing_context

from app.ai_module.document_ingestion import parse_bytes
from app.ai_module.schemas import AskRequest, ChatResponse
from app.config.config import Settings
from app.services.document_workflows import PortalService

HANDBOOK = "eval-travel-handbook.pdf"
EVAL_USER = "offline-evaluation"
ABSTENTIONS = {
    "i don't know based on the documents",
    "i don't know based on the uploaded documents",
}


@dataclass(frozen=True)
class Turn:
    question: str
    expected: Literal["grounded", "unsupported", "general"]
    answer_pattern: str = ""
    source_page: int | None = None


CASES = {
    "expense_deadline_citation": (
        Turn(
            "According to the travel handbook, what is the deadline for submitting expense claims?",
            "grounded", r"\b(?:45|forty[- ]five)\b", 2,
        ),
    ),
    "greeting_with_document_question": (
        Turn(
            "Hello bro, what cabin class does the travel handbook require for domestic flights?",
            "grounded", r"\beconomy\b", 1,
        ),
    ),
    "missing_evidence": (
        Turn("What mileage reimbursement rate does the travel handbook specify?", "unsupported"),
    ),
    "greeting_with_address": (Turn("hello sister", "general"),),
    "greeting_with_typo": (Turn("hgello brother", "general"),),
    "small_talk_followup": (
        Turn("how are you bro", "general"),
        Turn("i am doing good what about you bro", "general"),
    ),
    "unrelated_weather_question": (Turn("hello bro what is tday weather", "general"),),
}


def handbook_bytes() -> bytes:
    with fitz.open() as pdf:
        for text in (
            "Travel Handbook - Flight Bookings\nDomestic flights must be booked in economy class.",
            "Travel Handbook - Expense Claims\nSubmit expense claims within 45 calendar days after the trip ends.",
        ):
            pdf.new_page().insert_text((60, 72), text)
        return pdf.tobytes()


def score_turn(turn: Turn, response: ChatResponse, searches: int) -> dict[str, bool]:
    answer = response.answer.strip()
    normalized = answer.casefold().replace("\u2019", "'").rstrip(".! ")
    checks = {
        "route_correct": response.answer_kind == ("general" if turn.expected == "general" else "document"),
        "retrieval_correct": (searches > 0) == (turn.expected != "general"),
    }
    if turn.expected == "grounded":
        checks["answer_contains_expected_fact"] = bool(re.search(turn.answer_pattern, answer, re.IGNORECASE))
        checks["expected_citation"] = (HANDBOOK, turn.source_page) in {
            (source.document, source.page) for source in response.sources
        }
    else:
        checks["no_citations"] = not response.sources
        if turn.expected == "unsupported":
            checks["abstained"] = normalized in ABSTENTIONS
        else:
            checks["conversational_reply"] = bool(answer) and normalized not in ABSTENTIONS
    return checks


def evaluate_chat(portal: PortalService, session_id: str) -> dict:
    results = []
    with patch.object(portal.index, "search_locked", wraps=portal.index.search_locked) as search:
        for name, turns in CASES.items():
            conversation_id = uuid4().hex
            turn_results = []
            for turn in turns:
                call_count = search.call_count
                try:
                    response = portal.chat(session_id, EVAL_USER, AskRequest(
                        question=turn.question, top_k=1, conversation_id=conversation_id,
                    ))
                except Exception as error:
                    turn_results.append({
                        "question": turn.question, "passed": False, "error_type": type(error).__name__,
                        "retrieval_calls": search.call_count - call_count,
                    })
                    break
                searches = search.call_count - call_count
                checks = score_turn(turn, response, searches)
                turn_results.append({
                    "question": turn.question, "passed": all(checks.values()), "checks": checks,
                    "answer": response.answer, "answer_kind": response.answer_kind, "retrieval_calls": searches,
                    "sources": [{"document": source.document, "page": source.page} for source in response.sources],
                })
            results.append({"case": name, "passed": all(turn["passed"] for turn in turn_results), "turns": turn_results})
    return {"passed": sum(case["passed"] for case in results), "total": len(results), "cases": results}


def main() -> int:
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    try:
        configured = Settings.from_env()
    except ValueError as error:
        print(json.dumps({"setup_error": str(error)}))
        return 2

    try:
        with TemporaryDirectory(prefix="edis-eval-") as directory, tracing_context(enabled=False):
            root = Path(directory)
            settings = replace(configured, storage_dir=root / "storage", output_dir=root / "output")
            portal = PortalService(settings)
            session_id = portal.create_session(EVAL_USER)["session_id"]
            index = chromadb.EphemeralClient(chromadb.Settings(anonymized_telemetry=False))
            with patch.object(portal.index, "_client", return_value=index):
                try:
                    portal.ingest(session_id, EVAL_USER, [parse_bytes(HANDBOOK, handbook_bytes(), settings)])
                    report = evaluate_chat(portal, session_id)
                    report["chat_provider"] = settings.chat_provider
                    report["embedding_profile"] = portal.warehouse.owned(session_id, EVAL_USER)["embedding_profile"]
                finally:
                    portal.index.delete_session(session_id, EVAL_USER)
    except Exception as error:
        print(json.dumps({"setup_error": type(error).__name__, "detail": getattr(error, "detail", None)}))
        return 2

    print(json.dumps(report, indent=2))
    return int(report["passed"] != report["total"])


if __name__ == "__main__":
    raise SystemExit(main())
