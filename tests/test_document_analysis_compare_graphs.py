import json
from threading import Barrier

import fitz
import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from groq import APIStatusError
from langchain_core.runnables import RunnableLambda

from app.api.routes import router
from app.config.config import Settings
from app.security import current_user
from app.services.document_workflows import PortalService


@pytest.fixture
def document_api(tmp_path):
    settings = Settings(auth_mode="local", storage_dir=tmp_path)
    app = FastAPI()
    app.state.settings = settings
    app.state.portal = PortalService(settings)
    app.include_router(router)
    with TestClient(app) as client:
        yield client, app.state.portal.document_analysis


def pdf_upload(name, pages):
    with fitz.open() as document:
        for text in pages:
            document.new_page().insert_text((50, 50), text)
        return name, document.tobytes(), "application/pdf"


@pytest.mark.parametrize("workflow", ["analysis", "compare_text", "compare_pdf"])
def test_custom_instructions_reach_model_and_have_separate_cache_entries(document_api, monkeypatch, workflow):
    client, analysis = document_api
    seen = []

    def respond(prompt):
        system, preferences, document_text = (message.content for message in prompt.to_messages())
        assert "only when compatible" in system
        assert preferences.startswith("User preferences for this request:\n")
        assert "Source fee" in document_text
        seen.append(preferences.split("\n", 1)[1])
        if workflow == "analysis":
            return json.dumps({"title": "Report", "summary": seen[-1], "keywords": [], "language": "en"})
        return seen[-1]

    monkeypatch.setattr(analysis, "analysis_model", lambda: RunnableLambda(respond))
    if workflow == "analysis":
        url = "/api/analyze"
        files = {"file": ("report.txt", "Source fee is 10.", "text/plain")}
    else:
        url = "/api/compare"
        if workflow == "compare_pdf":
            files = {
                "reference": pdf_upload("old.pdf", ["Source fee is 10."]),
                "actual": pdf_upload("new.pdf", ["Source fee is 20."]),
            }
        else:
            files = {"reference": ("old.txt", "Source fee is 10."), "actual": ("new.txt", "Source fee is 20.")}

    instructions = "Summarize fees using {amount} as a label."
    first = client.post(url, data={"custom_instructions": instructions}, files=files)
    cached = client.post(url, data={"custom_instructions": f"  {instructions}  "}, files=files)
    updated = client.post(url, data={"custom_instructions": "Focus on dates."}, files=files)
    assert [response.status_code for response in (first, cached, updated)] == [200, 200, 200]
    assert first.json() == cached.json()
    assert first.json() != updated.json()
    assert seen == [instructions, "Focus on dates."]
    assert client.post(url, data={"custom_instructions": "x" * 1201}, files=files).status_code == 422


def test_instruction_defaults_are_per_owner_and_workflow(document_api, monkeypatch):
    client, _ = document_api
    app = client.app

    def owner(request: Request) -> str:
        return request.headers.get("X-Test-Owner", "owner")

    monkeypatch.setitem(app.dependency_overrides, current_user, owner)
    assert client.get("/api/instructions").json() == {"analysis": "", "compare": ""}
    assert client.put("/api/instructions/analysis", json={"instructions": "  Reply in Hindi.  "}).status_code == 204
    assert client.put("/api/instructions/compare", json={"instructions": "Focus on fees."}).status_code == 204
    assert client.put("/api/instructions/compare", json={"instructions": "x" * 1201}).status_code == 422
    app.state.portal = PortalService(app.state.settings)
    restored = client.get("/api/instructions")
    assert restored.headers["Cache-Control"] == "no-store"
    assert restored.json() == {"analysis": "Reply in Hindi.", "compare": "Focus on fees."}
    assert client.get("/api/instructions", headers={"X-Test-Owner": "other"}).json() == {"analysis": "", "compare": ""}
    assert client.put("/api/instructions/analysis", json={"instructions": ""}).status_code == 204
    assert client.get("/api/instructions").json() == {"analysis": "", "compare": "Focus on fees."}


@pytest.mark.parametrize("workflow", ["analysis", "compare"])
def test_result_review_is_owned_and_independent_of_cached_results(document_api, monkeypatch, workflow):
    client, analysis = document_api
    app = client.app

    def owner(request: Request) -> str:
        return request.headers.get("X-Test-Owner", "owner")

    monkeypatch.setitem(app.dependency_overrides, current_user, owner)
    if workflow == "analysis":
        reply = json.dumps({"title": "Report", "summary": "Summary", "keywords": [], "language": "en"})
        url = "/api/analyze"
        files = {"file": ("report.txt", b"First report")}
    else:
        reply = "Text changed."
        url = "/api/compare"
        files = {"reference": ("report.txt", b"First report"), "actual": ("updated.txt", b"Second report")}
    monkeypatch.setattr(analysis, "analysis_model", lambda: RunnableLambda(lambda _: reply))

    generated = client.post(url, files=files)
    assert generated.status_code == 200, generated.text
    review_url = f"/api/reviews/{generated.headers['X-Review-ID']}"
    pending = client.get(review_url)
    assert pending.headers["Cache-Control"] == "no-store"
    assert pending.json()["workflow"] == workflow
    assert pending.json()["status"] == "pending"
    assert pending.json()["reviewed_at"] is None

    note = {"status": "changes_requested", "note": "  Verify page references.  "}
    assert client.put(review_url, json={"status": "changes_requested", "note": " "}).status_code == 422
    outside = {"X-Test-Owner": "another-user"}
    assert client.get(review_url, headers=outside).status_code == 404
    assert client.put(review_url, headers=outside, json=note).status_code == 404
    requested = client.put(review_url, json=note)
    assert requested.status_code == 200, requested.text
    assert requested.json()["note"] == "Verify page references."
    assert requested.json()["reviewed_at"]

    regenerated = client.post(url, files=files)
    assert regenerated.json() == generated.json()
    new_review_id = regenerated.headers["X-Review-ID"]
    assert new_review_id != generated.headers["X-Review-ID"]
    assert client.get(f"/api/reviews/{new_review_id}").json()["status"] == "pending"

    app.state.portal = PortalService(app.state.settings)
    assert client.get(review_url).json() == requested.json()
    accepted = client.put(review_url, json={"status": "approved"})
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "approved"
    assert client.put(review_url, json={"status": "approved"}).json() == accepted.json()


def test_compare_aligns_shifted_pdf_pages_and_reports_additions_and_removals(document_api, monkeypatch):
    client, analysis = document_api
    monkeypatch.setattr(analysis, "analysis_model", lambda: RunnableLambda(lambda _: "Text changed."))

    response = client.post("/api/compare", files={
        "reference": pdf_upload("old.pdf", ["Intro", "Terms", "Old contact", "Obsolete"]),
        "actual": pdf_upload("new.pdf", ["Intro", "New material", "Terms", "New contact"]),
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["comparison_kind"] == "pages"
    # The final unmatched span pairs New contact with Old contact in document order.
    changes = result["changes"]
    assert [(item["reference_page"], item["actual_page"], item["changed"]) for item in changes] == [
        (1, 1, False), (None, 2, True), (2, 3, False), (3, 4, True), (4, None, True),
    ]
    assert changes[1]["summary"].startswith("Page added.")
    assert changes[-1]["summary"].startswith("Page removed.")


@pytest.mark.parametrize("instructions", ["", "Prioritize figures."])
def test_long_analysis_splits_rejected_sections_and_merges_in_document_order(document_api, monkeypatch, instructions):
    client, analysis = document_api
    oversize_payloads = []
    final_payloads = []

    def respond(prompt):
        system, *preferences, human = (message.content for message in prompt.to_messages())
        assert preferences == ([f"User preferences for this request:\n{instructions}"] if instructions else [])
        if system.startswith("Summarize this part"):
            if len(human) > 9_000:
                oversize_payloads.append(len(human))
                request = httpx.Request("POST", "https://example.test")
                response = httpx.Response(413, request=request)
                raise APIStatusError("Request too large", response=response, body=None)
            return human[0]
        final_payloads.append(human)
        return json.dumps({
            "title": "Test", "summary": "Combined summary", "keywords": [], "language": "en",
        })

    monkeypatch.setattr(analysis, "analysis_model", lambda: RunnableLambda(respond))
    text = "A" * 16_000 + "B" * 16_000 + "C" * 4_000
    response = client.post("/api/analyze", data={"custom_instructions": instructions}, files={"file": ("report.txt", text, "text/plain")})

    assert response.status_code == 200, response.text
    assert response.json()["summary"] == "Combined summary"
    assert response.json()["page_count"] == 1
    assert oversize_payloads == [16_000, 16_000]
    assert final_payloads[0].endswith("A\n\nA\n\nB\n\nB\n\nC")


def test_compare_short_text_documents_keeps_document_mode(document_api, monkeypatch):
    client, analysis = document_api
    monkeypatch.setattr(analysis, "analysis_model", lambda: RunnableLambda(lambda _: "Text changed."))
    response = client.post("/api/compare", files={
        "reference": ("old.txt", "New York", "text/plain"),
        "actual": ("new.txt", "New Delhi", "text/plain"),
    })
    assert response.status_code == 200, response.text
    assert response.json()["comparison_kind"] == "document"
    assert response.json()["changes"][0]["summary"] == "Text changed."


@pytest.mark.parametrize("instructions", ["", "Prioritize fees."])
def test_compare_long_text_documents_combines_both_section_summaries(document_api, monkeypatch, instructions):
    client, analysis = document_api
    both_summaries_started = Barrier(2, timeout=5)

    def respond(prompt):
        system, *preferences, human = (message.content for message in prompt.to_messages())
        assert preferences == ([f"User preferences for this request:\n{instructions}"] if instructions else [])
        if system.startswith("Summarize this part"):
            both_summaries_started.wait()
            return f"{human[0]} summary"
        assert human == "Reference document:\nA summary\n\nSecond document:\nB summary"
        return json.dumps({"overview": "Updated terms.", "differences": ["Terms changed."], "similarities": []})

    monkeypatch.setattr(analysis, "analysis_model", lambda: RunnableLambda(respond))
    response = client.post("/api/compare", data={"custom_instructions": instructions}, files={
        "reference": ("old.txt", "A" * 8_000, "text/plain"),
        "actual": ("new.txt", "B" * 8_000, "text/plain"),
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["comparison_kind"] == "sections"
    assert result["overview"]["differences"] == ["Terms changed."]
    assert result["reference_sections"] == ["A summary"]
    assert result["actual_sections"] == ["B summary"]
