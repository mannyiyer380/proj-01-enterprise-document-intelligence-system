"""Small, documented FastAPI interface; no path traversal or exception detail leakage."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from time import perf_counter
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, Response
from starlette.concurrency import run_in_threadpool
from structlog import get_logger

from app.config.config import Settings
from app.utils.custom_logger import LOGGER_NAME
from app.ai_module.schemas import AskRequest, ChatResponse, CompareResponse, DocumentAnalysis
from app.security import current_user, get_settings, require_session_id
from app.ai_module.document_ingestion import read_upload
from app.services.document_workflows import PortalService

router = APIRouter(prefix="/api", tags=["Document Portal"])
logger = get_logger(LOGGER_NAME)


@contextmanager
def track_operation(operation: str):
    started = perf_counter()
    logger.info("operation_started", operation=operation)
    try:
        yield
    except Exception as error:
        logger.warning(
            "operation_failed",
            operation=operation,
            duration_ms=round((perf_counter() - started) * 1000, 1),
            status_code=getattr(error, "status_code", None),
            error_type=type(error).__name__,
        )
        raise
    else:
        logger.info(
            "operation_completed",
            operation=operation,
            duration_ms=round((perf_counter() - started) * 1000, 1),
        )


def get_portal(request: Request) -> PortalService:
    return request.app.state.portal


User = Annotated[str, Depends(current_user)]
Portal = Annotated[PortalService, Depends(get_portal)]
Conf = Annotated[Settings, Depends(get_settings)]


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "document-portal-v2"}


@router.get("/dataset/session")
def saved_dataset_session(user: User, portal: Portal) -> dict:
    return {"session": portal.saved_dataset_session(user)}


@router.post("/sessions", status_code=201)
def create_session(user: User, portal: Portal) -> dict:
    return portal.create_session(user)


@router.get("/sessions/{session_id}")
def session_detail(session_id: str, user: User, portal: Portal) -> dict:
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
    with track_operation("upload_documents"):
        documents = [await read_upload(file, settings) for file in files]
        result = await run_in_threadpool(portal.ingest, session_id, user, documents)
    return dict(result, session_id=session_id)


@router.post("/sessions/{session_id}/dataset")
async def load_dataset(session_id: str, user: User, portal: Portal) -> dict:
    """Index files from the server's configured dataset directory."""
    session_id = require_session_id(session_id)
    portal.warehouse.owned(session_id, user)
    with track_operation("load_dataset"):
        result = await run_in_threadpool(portal.ingest_dataset, session_id, user)
    return dict(result, session_id=session_id)


@router.post("/sessions/{session_id}/chat", response_model=ChatResponse)
def chat(session_id: str, data: AskRequest, user: User, portal: Portal) -> ChatResponse:
    with track_operation("document_chat"):
        return portal.chat(require_session_id(session_id), user, data)

@router.post("/chat", response_model=ChatResponse)
def direct_chat(data: AskRequest, user: User, portal: Portal) -> ChatResponse:
    with track_operation("document_chat"):
        return portal.direct_chat(data)


@router.post("/analyze", response_model=DocumentAnalysis)
async def analyze(
    file: UploadFile, user: User, portal: Portal, settings: Conf
) -> DocumentAnalysis:
    _ = user
    analysis_settings = replace(
        settings,
        max_upload_mb=settings.max_analysis_file_mb,
        max_pages=settings.max_analysis_pages,
        max_extracted_chars=settings.max_analysis_extracted_chars,
    )
    with track_operation("document_analysis"):
        doc = await read_upload(file, analysis_settings)
        return await run_in_threadpool(portal.analyze, doc)


@router.post("/compare", response_model=CompareResponse)
async def compare(
    reference: UploadFile, actual: UploadFile, user: User, portal: Portal, settings: Conf
) -> CompareResponse:
    _ = user
    with track_operation("document_compare"):
        ref = await read_upload(reference, settings)
        updated = await read_upload(actual, settings)
        return await run_in_threadpool(portal.compare, ref, updated)
