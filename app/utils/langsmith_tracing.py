"""Opt-in LangSmith traces with request correlation and private payloads."""

from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from langsmith import Client, trace, tracing_context
from langsmith.utils import get_env_var, tracing_is_enabled
from structlog.contextvars import get_contextvars

TRACE_METADATA = {"request_id", "ls_provider", "ls_model_name", "ls_model_type"}


def safe_trace_data(data: dict[str, Any]) -> dict[str, Any]:
    if "error" in data:
        return {"error": "The operation failed. See the correlated application logs."}
    return {key: value for key, value in data.items() if key in TRACE_METADATA}


@lru_cache(maxsize=1)
def trace_client() -> Client:
    return Client(hide_inputs=True, hide_outputs=True, anonymizer=safe_trace_data)


@contextmanager
def trace_operation(operation: str):
    project = get_env_var("PROJECT")
    if not tracing_is_enabled() or not get_env_var("API_KEY") or not project:
        with tracing_context(enabled=False):
            yield
        return

    request_id = get_contextvars().get("request_id")
    metadata = {"request_id": request_id} if request_id else {}
    with tracing_context(client=trace_client(), project_name=project):
        with trace(operation, run_type="chain", metadata=metadata):
            yield
