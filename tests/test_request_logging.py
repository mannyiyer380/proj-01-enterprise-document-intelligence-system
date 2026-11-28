import importlib
import json

import httpx
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableLambda
from openai import RateLimitError

from app.config.config import Settings
from app.utils.custom_logger import DocumentIntelligenceLogger


def test_console_progress_and_structured_failure_details(tmp_path, monkeypatch, capsys):
    settings = Settings(
        auth_mode="local", storage_dir=tmp_path / "storage",
        groq_api_key="test-key", huggingface_token="test-token",
    )
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    main = importlib.import_module("app.main")
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(main, "DocumentIntelligenceLogger", lambda: DocumentIntelligenceLogger(log_dir))
    app = main.create_app(settings)
    sensitive_value = "private provider payload"

    def fail_with_internal_error(_document, _custom_instructions):
        raise RuntimeError(sensitive_value)

    def fail_with_quota_error(_document, _custom_instructions):
        response = httpx.Response(
            429, request=httpx.Request("POST", "https://example.test/"),
        )
        raise RateLimitError(sensitive_value, response=response, body={"code": "insufficient_quota"})

    file = {"file": ("test.txt", b"Sample text", "text/plain")}
    failing_file = {"file": ("failure.txt", b"Another document", "text/plain")}
    with TestClient(app) as client:
        model = RunnableLambda(lambda _: json.dumps({
            "title": "Test document", "summary": "Example summary", "keywords": [], "language": "en",
        }))
        monkeypatch.setattr(app.state.portal.document_analysis, "analysis_model", lambda: model)
        success = client.post("/api/analyze", files=file)
        invalid_pdf = client.post("/api/analyze", files={"file": ("test.pdf", b"broken", "application/pdf")})
        validation = client.post("/api/analyze")
        monkeypatch.setattr(app.state.portal, "analyze", fail_with_internal_error)
        cached = client.post("/api/analyze", files=file)
        unexpected = client.post("/api/analyze", files=failing_file)
        monkeypatch.setattr(app.state.portal, "analyze", fail_with_quota_error)
        limited = client.post("/api/analyze", files=failing_file)

    assert [invalid_pdf.status_code, validation.status_code, unexpected.status_code, limited.status_code] == [
        422, 422, 500, 429,
    ]
    assert success.status_code == 200
    assert cached.status_code == 200
    assert cached.json() == success.json()
    assert sensitive_value not in unexpected.text
    console = capsys.readouterr().err
    assert "Document analysis started." in console
    assert "The document fits in one request. Generating its summary." in console
    assert "Document analysis finished successfully." in console
    assert "The file does not have a valid PDF header." in console
    assert "OpenAI API quota is exhausted." in console
    assert sensitive_value not in console
    console_records = [json.loads(line) for line in console.splitlines()]

    log_file, = log_dir.glob("*.jsonl")
    raw_log = log_file.read_text(encoding="utf-8")
    assert sensitive_value not in raw_log
    records = [json.loads(line) for line in raw_log.splitlines()]
    assert console_records == records
    for event, response in (
        ("cache_hit", cached),
        ("handled_http_error", invalid_pdf),
        ("request_validation_failed", validation),
        ("unhandled_request_error", unexpected),
        ("model_provider_rate_limit", limited),
    ):
        entry = next(record for record in records if record["event"] == event)
        assert entry["request_id"] == response.headers["X-Request-ID"]
    internal = next(record for record in records if record["event"] == "unhandled_request_error")
    assert internal["source"].startswith("test_request_logging.py:")
    assert internal["traceback_frames"][-1]["function"] == "fail_with_internal_error"
