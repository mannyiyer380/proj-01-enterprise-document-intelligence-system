import json
from unittest.mock import call, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableLambda

from app.ai_module.schemas import AskRequest
from app.api.routes import router
from app.config.config import Settings
from app.security import current_user
from app.services.document_workflows import PortalService


@pytest.fixture
def session(tmp_path):
    portal = PortalService(Settings(storage_dir=tmp_path))
    portal.document_chat.models._model = RunnableLambda(lambda _: '{"intent":"document","answer":null}')
    session_id = portal.create_session("owner")["session_id"]
    return portal, session_id


def test_document_chat_retries_empty_rewrite_and_saves_sources(session):
    portal, session_id = session
    conversation_id = "a" * 32
    history = [{"role": "user", "content": "Tell me about KYC.", "conversation_id": conversation_id}]
    portal.warehouse.write_json(portal.warehouse.path(session_id) / "history.json", [
        *history,
        {"role": "assistant", "content": "Hello", "answer_kind": "general", "conversation_id": conversation_id},
        {"role": "user", "content": "Other topic", "conversation_id": "b" * 32},
    ])
    question = "What about renewals?"
    rewritten = "What are the KYC renewal rules?"
    hits = [{"document": "kyc.pdf", "page": 7, "score": 0.9, "text": "Renewal terms."}]

    with (
        patch.object(portal.document_chat, "rewrite_query", return_value=rewritten) as rewrite,
        patch.object(portal.index, "_search_chroma", side_effect=[[], hits]) as search,
        patch.object(portal.document_chat, "answer", return_value=("See page 7.", hits)) as answer,
    ):
        response = portal.chat(
            session_id, "owner",
            AskRequest(question=question, conversation_id=conversation_id, top_k=2),
        )

    rewrite.assert_called_once_with(question, history)
    assert [entry.args[2:4] for entry in search.call_args_list] == [(rewritten, 2), (question, 2)]
    answer.assert_called_once_with(question, hits, history)
    assert response.answer == "See page 7."
    assert response.sources[0].page == 7
    saved = portal.session_detail(session_id, "owner")["history"][-2:]
    assert [turn["conversation_id"] for turn in saved] == [conversation_id, conversation_id]
    assert saved[-1]["sources"] == [source.model_dump() for source in response.sources]


def test_empty_results_and_small_talk_use_existing_answers(session):
    portal, session_id = session
    question = "What is the renewal period?"
    with (
        patch.object(portal.index, "search_locked", return_value=[]) as search,
        patch.object(portal.document_chat, "answer_directly", return_value="Hello!") as direct,
    ):
        result = portal.chat(session_id, "owner", AskRequest(question=question))
        greeting = portal.chat(session_id, "owner", AskRequest(question="Hello"))

    assert result.answer == "I don't know based on the uploaded documents."
    assert result.sources == []
    assert search.call_args_list == [call(session_id, "owner", question, k=4)]
    direct.assert_called_once_with("Hello")
    assert greeting.answer_kind == "general"


def test_missing_documents_error_leaves_history_empty(session):
    portal, session_id = session
    with pytest.raises(HTTPException) as error:
        portal.chat(session_id, "owner", AskRequest(question="What is the renewal period?"))
    assert error.value.status_code == 409
    assert portal.session_detail(session_id, "owner")["history"] == []


def test_combined_greeting_calls_model_without_document_search(session):
    portal, session_id = session
    questions = ["hello bro", "hello sister", "hi hello how are you", "Hey bro, how are you?", "Bhai, kya haal hai?"]
    received = []

    def reply(prompt):
        system, human = (message.content for message in prompt.to_messages())
        if "Decide whether" in system:
            return '{"intent":"document","answer":null}'
        received.append(human)
        return "Hi! I'm doing well."

    portal.document_chat.models._model = RunnableLambda(reply)
    with patch.object(portal.index, "search_locked", return_value=[]) as search:
        responses = [
            portal.direct_chat(AskRequest(question=questions[0])),
            *(portal.chat(session_id, "owner", AskRequest(question=question)) for question in questions),
        ]
        search.assert_not_called()
        document_question = "Hello bro, what is KYC?"
        portal.chat(session_id, "owner", AskRequest(question=document_question))
        search.assert_called_once_with(session_id, "owner", document_question, k=4)

    assert received == [questions[0], *questions]
    assert all(response.answer == "Hi! I'm doing well." for response in responses)
    assert all(response.answer_kind == "general" and response.sources == [] for response in responses)


def test_saved_greeting_loses_old_sources_without_affecting_document_answers(session):
    portal, session_id = session
    source = {"document": "report.pdf", "page": 1, "score": 0.9, "excerpt": "Policy terms"}
    document_turns = [
        {"role": "user", "content": "What are the policy terms?", "answer_kind": "document"},
        {"role": "assistant", "content": "See the report.", "answer_kind": "document", "sources": [source]},
    ]
    portal.warehouse.write_json(portal.warehouse.path(session_id) / "history.json", [
        {"role": "user", "content": "hello bro", "answer_kind": "document"},
        {"role": "assistant", "content": "Hello!", "answer_kind": "document", "sources": [source]},
        *document_turns,
    ])

    history = portal.session_detail(session_id, "owner")["history"]
    assert [turn["answer_kind"] for turn in history[:2]] == ["general", "general"]
    assert history[1]["sources"] == []
    assert history[2:] == document_turns

    followup = "What about renewals?"
    with (
        patch.object(portal.document_chat, "rewrite_query", return_value=followup) as rewrite,
        patch.object(portal.index, "search_locked", return_value=[]),
    ):
        portal.chat(session_id, "owner", AskRequest(question=followup))
    rewrite.assert_called_once_with(followup, document_turns)


def test_intent_router_handles_casual_followups_typos_and_weather_without_rag(session):
    portal, session_id = session
    replies = {
        "i am doing good what about you bro": "I'm doing well too!",
        "hgello brother": "Hey! How's it going?",
        "hello bro what is tday weather": "I can't check live weather here.",
    }
    routed_messages = []

    def model_response(prompt):
        system, human = (message.content for message in prompt.to_messages())
        if "Decide whether" not in system:
            return "I'm doing well, thanks! How are you?"
        assert "do not guess" in system
        routed_messages.append(human)
        question = human.split("Latest message:\n")[-1]
        return json.dumps({"intent": "general", "answer": replies[question]})

    portal.document_chat.models._model = RunnableLambda(model_response)
    with patch.object(portal.document_chat_graph, "invoke") as rag:
        portal.chat(session_id, "owner", AskRequest(question="how are you bro"))
        for question, expected in replies.items():
            result = portal.chat(session_id, "owner", AskRequest(question=question))
            assert result.answer == expected
            assert result.answer_kind == "general" and result.sources == []
        assert portal.direct_chat(AskRequest(question="hgello brother")).answer == replies["hgello brother"]
        rag.assert_not_called()

    assert "assistant: I'm doing well, thanks! How are you?" in routed_messages[0]


def test_unsupported_document_answer_omits_retrieved_sources(session):
    portal, session_id = session
    hits = [{"document": "report.pdf", "page": 1, "text": "Unrelated excerpt", "score": 0.9}]
    replies = iter(['{"intent":"document","answer":null}', '{"answer":null,"source_ids":[]}'])
    portal.document_chat.models._model = RunnableLambda(lambda _: next(replies))
    with patch.object(portal.index, "search_locked", return_value=hits) as search:
        response = portal.chat(session_id, "owner", AskRequest(question="What is the policy's renewal date?"))
    search.assert_called_once()
    assert response.answer_kind == "document"
    assert response.sources == []
    assert portal.session_detail(session_id, "owner")["history"][-1]["sources"] == []


def test_invalid_intent_retries_once_and_never_searches(session):
    portal, session_id = session
    question = AskRequest(question="Can you tell me today's weather?")
    replies = iter(['{"intent":"general","answer":"   "}', '{"intent":"general","answer":"I cannot check live weather."}'])
    portal.document_chat.models._model = RunnableLambda(lambda _: next(replies))
    with patch.object(portal.index, "search_locked") as search:
        assert portal.chat(session_id, "owner", question).answer == "I cannot check live weather."
        with patch.object(portal.document_chat.models, "_model", RunnableLambda(lambda _: '{"intent":"unknown"}')):
            with pytest.raises(HTTPException, match="rephrase") as error:
                portal.chat(session_id, "owner", question)
        assert error.value.status_code == 422
        search.assert_not_called()
    assert len(portal.session_detail(session_id, "owner")["history"]) == 2


@pytest.mark.parametrize("source_ids, expected_page", [([2, 2], 8), ([99], None), ([1, 99], None), ([], None)])
def test_only_validated_source_ids_are_returned(session, source_ids, expected_page):
    portal, session_id = session
    hits = [
        {"document": "report.pdf", "page": 2, "text": "Other notes", "score": 0.9},
        {"document": "report.pdf", "page": 8, "text": "Renew after 30 days", "score": 0.8},
    ]

    def reply(prompt):
        messages = prompt.to_messages()
        if "Decide whether" in messages[0].content:
            return '{"intent":"document","answer":null}'
        assert "Renew after 30 days" not in messages[0].content
        context = json.loads(messages[-2].content.split("\n", 1)[1])
        assert [item["id"] for item in context] == [1, 2]
        return json.dumps({"answer": "30 days", "source_ids": source_ids})

    portal.document_chat.models._model = RunnableLambda(reply)
    with patch.object(portal.index, "search_locked", return_value=hits):
        response = portal.chat(session_id, "owner", AskRequest(question="When does the report say to renew?"))
    if expected_page is None:
        assert response.answer == "I don't know based on the uploaded documents."
        assert response.sources == []
    else:
        assert response.answer == "30 days"
        assert [(source.document, source.page) for source in response.sources] == [("report.pdf", expected_page)]


def test_general_chat_memory_survives_restart_and_is_isolated(tmp_path):
    settings = Settings(storage_dir=tmp_path)
    app = FastAPI()
    app.include_router(router)

    def test_user(request: Request) -> str:
        return request.headers["x-test-user"]

    app.dependency_overrides[current_user] = test_user
    owner = {"x-test-user": "../alice"}
    other = {"x-test-user": "bob"}
    conversation = "a" * 32
    fresh_conversation = "b" * 32
    history_url = f"/api/chat/history/{conversation}"
    app.state.portal = PortalService(settings)

    with TestClient(app) as client, patch.object(app.state.portal.document_chat, "general_reply", return_value="OK"):
        response = client.post("/api/chat", headers=owner, json={
            "question": "Remember the name Nila", "conversation_id": conversation,
        })
        assert response.status_code == 200
        assert response.json()["session_id"] is None

    app.state.portal = PortalService(settings)
    with TestClient(app) as client, patch.object(app.state.portal.document_chat, "general_reply", return_value="Nila") as reply:
        history = client.get(history_url, headers=owner)
        assert history.headers["Cache-Control"] == "no-store"
        assert [turn["content"] for turn in history.json()["history"]] == ["Remember the name Nila", "OK"]
        assert client.get(history_url, headers=other).json() == {"history": []}
        assert client.get("/api/chat/history/invalid", headers=owner).status_code == 400
        for conversation_id in (conversation, fresh_conversation):
            result = client.post("/api/chat", headers=owner, json={
                "question": "What name did I tell you?", "conversation_id": conversation_id,
            })
            assert result.status_code == 200
        assert [turn["content"] for turn in reply.call_args_list[0].kwargs["history"]] == ["Remember the name Nila", "OK"]
        assert reply.call_args_list[1].kwargs["history"] == []
        assert len(client.get(history_url, headers=owner).json()["history"]) == 4


def test_document_conversations_stay_separate_from_general_memory(session):
    portal, session_id = session
    first = "a" * 32
    second = "b" * 32
    with patch.object(portal.document_chat, "general_reply", return_value="OK") as reply:
        portal.direct_chat(AskRequest(question="General context", conversation_id=first), "owner")
        portal.chat(session_id, "owner", AskRequest(question="Document context", conversation_id=first))
        portal.chat(session_id, "owner", AskRequest(question="Resume document chat", conversation_id=first))
        portal.chat(session_id, "owner", AskRequest(question="Start a new chat", conversation_id=second))
    assert reply.call_args_list[1].args[1] == []
    assert [turn["content"] for turn in reply.call_args_list[2].args[1]] == ["Document context", "OK"]
    assert reply.call_args_list[3].args[1] == []
    assert [turn["content"] for turn in portal.general_chat_history("owner", first)] == ["General context", "OK"]
    with pytest.raises(HTTPException) as error:
        portal.session_detail(session_id, "another-user")
    assert error.value.status_code == 404
