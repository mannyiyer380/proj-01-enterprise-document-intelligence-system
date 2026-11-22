"""ASGI application. Launch: uvicorn app.main:app --host 0.0.0.0 --port 8080"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.utils.config import Settings
from app.utils.custom_logger import DocumentIntelligenceLogger
from app.services.portal import PortalService

UI = Path(__file__).resolve().parent / "ui"


def create_app(settings: Settings | None = None) -> FastAPI:
    conf = (settings or Settings.from_env()).validate()
    portal = PortalService(conf)
    logger_factory = DocumentIntelligenceLogger(conf.storage_dir / "logs")
    logger = logger_factory.get_logger("document_portal")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        logger_factory.close()

    app = FastAPI(
        title="Document Intelligence Portal V2",
        version="2.0.0",
        lifespan=lifespan,
    )
    app.state.settings = conf
    app.state.portal = portal
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(conf.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.mount("/static", StaticFiles(directory=UI), name="static")
    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(UI / "index.html", headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        cid = uuid.uuid4().hex[:12]
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "unhandled_request_error",
                request_id=cid,
                path=request.url.path,
            )
            return JSONResponse(status_code=500, content={"detail": "Internal server error", "request_id": cid})
        response.headers["X-Request-ID"] = cid
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return app


app = create_app()
