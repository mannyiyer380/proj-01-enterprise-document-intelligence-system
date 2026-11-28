"""Small, documented FastAPI interface; no path traversal or exception detail leakage."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, Response
from starlette.concurrency import run_in_threadpool
from structlog import get_logger

from app.config.config import Settings
from app.utils.custom_logger import LOGGER_NAME
from app.utils.langsmith_tracing import trace_operation
from app.ai_module.schemas import (
    MAX_CUSTOM_INSTRUCTIONS, AskRequest, ChatResponse, CompareResponse, DocumentAnalysis, DocumentWorkflow,
    InstructionDefaults, InstructionPreference, ResultReview, ReviewDecision,
)
from app.security import current_user, get_settings, require_session_id
from app.ai_module.document_ingestion import parse_bytes, read_upload, read_upload_bytes
from app.services.document_workflows import PortalService

router = APIRouter(prefix="/api", tags=["Document Portal"])
logger = get_logger(LOGGER_NAME)


@contextmanager
def track_operation(operation: str, label: str, **details):
    started = perf_counter()
    logger.info("operation_started", message=f"{label} started.", operation=operation, **details)
    try:
        with trace_operation(operation):
            yield
    except Exception as error:
        logger.warning(
            "operation_failed",
            message=f"{label} stopped before it could finish. Look for the error with the same request ID.",
            operation=operation,
            duration_ms=round((perf_counter() - started) * 1000, 1),
            status_code=getattr(error, "status_code", None),
            error_type=type(error).__name__,
        )
        raise
    else:
        logger.info(
            "operation_completed",
            message=f"{label} finished successfully.",
            operation=operation,
            duration_ms=round((perf_counter() - started) * 1000, 1),
        )


def get_portal(request: Request) -> PortalService:
    return request.app.state.portal


User = Annotated[str, Depends(current_user)]
Portal = Annotated[PortalService, Depends(get_portal)]
Conf = Annotated[Settings, Depends(get_settings)]
CustomInstructions = Annotated[str, Form(max_length=MAX_CUSTOM_INSTRUCTIONS)]


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "document-portal-v2"}


@router.get("/dataset/session")
def saved_dataset_session(user: User, portal: Portal, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"session": portal.saved_dataset_session(user)}


@router.post("/sessions", status_code=201)
def create_session(user: User, portal: Portal) -> dict:
    return portal.create_session(user)


@router.get("/sessions/{session_id}")
def session_detail(session_id: str, user: User, portal: Portal, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return portal.session_detail(require_session_id(session_id), user)



@router.delete(
    "/sessions/{session_id}",
    status_code=204,
    response_class=Response,
    response_model=None,
)
def delete_session(session_id: str, user: User, portal: Portal):
    portal.index.delete_session(
        require_session_id(session_id), user
    )
    return Response(status_code=204)



@router.post("/sessions/{session_id}/documents")
async def upload_documents(
    session_id: str,
    user: User,
    portal: Portal,
    settings: Conf,
    files: list[UploadFile] = File(...),
) -> dict:
    require_session_id(session_id)
    portal.warehouse.owned(session_id, user)  # Authorization before processing uploaded bytes.
    if not files or len(files) > settings.max_files_per_request:
        raise HTTPException(status_code=400, detail=f"Upload 1–{settings.max_files_per_request} files")
    with track_operation("upload_documents", "Document upload", file_count=len(files)):
        documents = [await read_upload(file, settings) for file in files]
        result = await run_in_threadpool(portal.ingest, session_id, user, documents)
    return dict(result, session_id=session_id)


@router.post("/sessions/{session_id}/dataset")
async def load_dataset(session_id: str, user: User, portal: Portal) -> dict:
    """Index files from the server's configured dataset directory."""
    session_id = require_session_id(session_id)
    portal.warehouse.owned(session_id, user)
    with track_operation("load_dataset", "Server dataset indexing"):
        result = await run_in_threadpool(portal.ingest_dataset, session_id, user)
    return dict(result, session_id=session_id)


@router.post("/sessions/{session_id}/chat", response_model=ChatResponse)
def chat(session_id: str, data: AskRequest, user: User, portal: Portal) -> ChatResponse:
    with track_operation("document_chat", "Document chat"):
        return portal.chat(require_session_id(session_id), user, data)

@router.post("/chat", response_model=ChatResponse)
def direct_chat(data: AskRequest, user: User, portal: Portal) -> ChatResponse:
    with track_operation("document_chat", "General chat"):
        return portal.direct_chat(data, user)


@router.get("/chat/history/{conversation_id}")
def general_chat_history(conversation_id: str, user: User, portal: Portal, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"history": portal.general_chat_history(user, require_session_id(conversation_id))}


def document_result_key(
    settings: Settings, user: str, version: int, *documents: tuple[str, bytes], custom_instructions: str = ""
) -> tuple:
    models = {
        "google": (settings.google_chat_model,),
        "groq": (settings.groq_chat_model, *settings.groq_fallback_models),
        "openai": (settings.openai_chat_model,),
    }
    return (
        user, version, settings.llm_provider, models[settings.llm_provider],
        settings.max_upload_mb, settings.max_pages, settings.max_extracted_chars,
        tuple((name, sha256(content).hexdigest()) for name, content in documents),
        sha256(custom_instructions.encode("utf-8")).hexdigest(),
    )


@router.post("/analyze", response_model=DocumentAnalysis)
async def analyze(
    file: UploadFile, user: User, portal: Portal, settings: Conf, response: Response,
    custom_instructions: CustomInstructions = "",
) -> DocumentAnalysis:
    analysis_settings = replace(
        settings,
        max_upload_mb=settings.max_analysis_file_mb,
        max_pages=settings.max_analysis_pages,
        max_extracted_chars=settings.max_analysis_extracted_chars,
    )
    instructions = custom_instructions.strip()
    with track_operation("document_analysis", "Document analysis"):
        name, content = await read_upload_bytes(file, analysis_settings)

        def analyze_document() -> DocumentAnalysis:
            key = document_result_key(analysis_settings, user, 1, (name, content), custom_instructions=instructions)
            return portal.document_results.resolve(
                "document_analysis", key, DocumentAnalysis,
                lambda: portal.analyze(parse_bytes(name, content, analysis_settings), instructions),
            )

        result = await run_in_threadpool(analyze_document)
        response.headers["X-Review-ID"] = await run_in_threadpool(portal.result_reviews.create, user, "analysis")
        response.headers["Cache-Control"] = "no-store"
        return result


@router.post("/compare", response_model=CompareResponse)
async def compare(
    reference: UploadFile, actual: UploadFile, user: User, portal: Portal, settings: Conf, response: Response,
    custom_instructions: CustomInstructions = "",
) -> CompareResponse:
    instructions = custom_instructions.strip()
    with track_operation("document_compare", "Document comparison"):
        reference_name, reference_content = await read_upload_bytes(reference, settings)
        actual_name, actual_content = await read_upload_bytes(actual, settings)

        def compare_documents() -> CompareResponse:
            key = document_result_key(
                settings, user, 1,
                (Path(reference_name).suffix.lower(), reference_content),
                (Path(actual_name).suffix.lower(), actual_content),
                custom_instructions=instructions,
            )
            return portal.document_results.resolve(
                "document_compare", key, CompareResponse,
                lambda: portal.compare(
                    parse_bytes(reference_name, reference_content, settings),
                    parse_bytes(actual_name, actual_content, settings),
                    instructions,
                ),
            )

        result = await run_in_threadpool(compare_documents)
        response.headers["X-Review-ID"] = await run_in_threadpool(portal.result_reviews.create, user, "compare")
        response.headers["Cache-Control"] = "no-store"
        return result


@router.get("/instructions", response_model=InstructionDefaults)
def instruction_defaults(user: User, portal: Portal, response: Response) -> InstructionDefaults:
    response.headers["Cache-Control"] = "no-store"
    return portal.warehouse.instruction_defaults(user)


@router.put("/instructions/{workflow}", status_code=204)
def save_instruction_default(
    workflow: DocumentWorkflow, preference: InstructionPreference, user: User, portal: Portal
) -> Response:
    portal.warehouse.save_instruction_default(user, workflow, preference.instructions)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/reviews/{review_id}", response_model=ResultReview)
def result_review(review_id: str, user: User, portal: Portal, response: Response) -> ResultReview:
    response.headers["Cache-Control"] = "no-store"
    return portal.result_reviews.get(review_id, user)


@router.put("/reviews/{review_id}", response_model=ResultReview)
def submit_result_review(
    review_id: str, decision: ReviewDecision, user: User, portal: Portal, response: Response
) -> ResultReview:
    review = portal.result_reviews.decide(review_id, user, decision)
    response.headers["Cache-Control"] = "no-store"
    logger.info(
        "result_review_saved", workflow=review.workflow, status=review.status,
        message="Saved the user's review decision.",
    )
    return review
