"""ASGI application. Launch: uvicorn app.main:app --host 0.0.0.0 --port 8080"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google.api_core.exceptions import GoogleAPICallError
from groq import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from langchain_core.exceptions import OutputParserException
from openai import APIConnectionError as OpenAIConnectionError
from openai import APIStatusError as OpenAIStatusError
from openai import APITimeoutError as OpenAITimeoutError
from openai import AuthenticationError as OpenAIAuthenticationError
from openai import RateLimitError as OpenAIRateLimitError
from starlette.exceptions import HTTPException as StarletteHTTPException
from structlog import get_logger
from structlog.contextvars import bind_contextvars, reset_contextvars

from app.api.auth_routes import router as auth_router
from app.api.routes import router
from app.services.accounts import AccountStore, LOGIN_COOKIE
from app.config.config import Settings
from app.utils.custom_exception import DocumentIntelligenceError
from app.utils.custom_logger import LOGGER_NAME, DocumentIntelligenceLogger
from app.services.document_workflows import PortalService

UI = Path(__file__).resolve().parent / "ui"
logger = get_logger(LOGGER_NAME)


def log_failure(event: str, message: str, error: Exception, status_code: int, **details) -> None:
    diagnostics = DocumentIntelligenceError(message, error)
    write = logger.error if status_code >= 500 else logger.warning
    write(event, message=message, status_code=status_code, **diagnostics.log_fields(), **details)


def log_unexpected_failure(error: Exception) -> None:
    upstream = error.__cause__ or error
    if isinstance(upstream, (APIStatusError, OpenAIStatusError, GoogleAPICallError)):
        if isinstance(upstream, APIStatusError):
            provider, upstream_status = "Groq", upstream.status_code
        elif isinstance(upstream, OpenAIStatusError):
            provider, upstream_status = "OpenAI", upstream.status_code
        else:
            provider, upstream_status = "Google", upstream.code
        try:
            reason = HTTPStatus(upstream_status).phrase
        except ValueError:
            reason = type(upstream).__name__
        log_failure(
            "model_provider_request_failed",
            f"{provider} could not complete the model request ({reason}).",
            error, 500, provider=provider, upstream_status=upstream_status,
        )
        return
    if isinstance(error, OSError):
        reason = os.strerror(error.errno) if error.errno is not None else "Check disk space and file permissions."
        log_failure(
            "storage_operation_failed",
            f"A system operation failed. {reason}",
            error, 500, errno=error.errno,
        )
        return
    log_failure(
        "unhandled_request_error",
        f"The request stopped because of an unexpected {type(error).__name__}. Check the logged source and traceback.",
        error, 500,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    try:
        conf = (settings or Settings.from_env()).validate()
        portal = PortalService(conf)
        accounts = AccountStore(conf.storage_root / "accounts.sqlite3") if conf.auth_mode == "sqlite" else None
    except Exception as error:
        with DocumentIntelligenceLogger() as logger_factory:
            logger_factory.get_logger()
            log_failure(
                "app_startup_failed",
                "The app could not start. Check the environment configuration and storage permissions.",
                error, 500,
            )
        raise

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        with DocumentIntelligenceLogger() as logger_factory:
            run_logger = logger_factory.get_logger()
            if accounts is not None:
                try:
                    accounts.initialize()
                except Exception as error:
                    log_failure("account_storage_startup_failed", "Could not initialize the account database.", error, 500)
                    raise
            run_logger.info(
                "app_started",
                message="The document portal is ready to accept requests.",
                chat_provider=conf.chat_provider,
                analysis_provider=conf.llm_provider,
                embedding_provider=conf.embedding_provider,
                vector_backend=conf.vector_backend,
            )
            try:
                yield
            finally:
                run_logger.info("app_stopped", message="The document portal shut down.")

    app = FastAPI(
        title="Document Intelligence Portal V2",
        version="2.0.0",
        lifespan=lifespan,
    )
    app.state.settings = conf
    app.state.portal = portal
    app.state.accounts = accounts
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(conf.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Review-ID"],
    )
    app.mount("/static", StaticFiles(directory=UI), name="static")
    app.include_router(router)
    app.include_router(auth_router)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(UI / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/workspace", include_in_schema=False, response_model=None)
    def workspace(request: Request) -> FileResponse | RedirectResponse:
        if accounts is not None and accounts.session_owner(request.cookies.get(LOGIN_COOKIE)) is None:
            return RedirectResponse("/login", headers={"Cache-Control": "no-store"})
        return FileResponse(UI / "workspace.html", headers={"Cache-Control": "no-store"})

    @app.get("/login", include_in_schema=False, response_model=None)
    @app.get("/signup", include_in_schema=False, response_model=None)
    def account_page(request: Request) -> FileResponse | RedirectResponse:
        if accounts is None or accounts.session_owner(request.cookies.get(LOGIN_COOKIE)):
            return RedirectResponse("/workspace", headers={"Cache-Control": "no-store"})
        return FileResponse(UI / "account.html", headers={"Cache-Control": "no-store"})

    @app.exception_handler(StarletteHTTPException)
    async def log_http_exception(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "The request could not be completed."
        log_failure("handled_http_error", detail, exc, exc.status_code)
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def log_validation_error(request: Request, exc: RequestValidationError):
        fields = [
            {"field": ".".join(map(str, error["loc"])), "issue": error["type"]}
            for error in exc.errors()
        ]
        logger.warning(
            "request_validation_failed",
            message="The request is missing required data or contains an invalid value.",
            status_code=422, fields=fields,
        )
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(RateLimitError)
    @app.exception_handler(OpenAIRateLimitError)
    async def handle_model_rate_limit(request: Request, exc: RateLimitError | OpenAIRateLimitError):
        provider = "OpenAI" if isinstance(exc, OpenAIRateLimitError) else "Groq"
        retry_after = exc.response.headers.get("retry-after", "").strip()
        headers = {}
        detail = f"{provider} rate limit reached. Please try again later."
        if isinstance(exc, OpenAIRateLimitError) and exc.code == "insufficient_quota":
            detail = "OpenAI API quota is exhausted. Check your API usage or billing."
        elif retry_after.isascii() and retry_after.isdecimal():
            headers["Retry-After"] = retry_after
            detail = f"{provider} rate limit reached. Try again in about {retry_after} seconds."
        log_failure(
            "model_provider_rate_limit", detail, exc, 429,
            provider=provider, retry_after=headers.get("Retry-After"),
        )
        return JSONResponse(status_code=429, content={"detail": detail}, headers=headers)

    @app.exception_handler(APIConnectionError)
    @app.exception_handler(OpenAIConnectionError)
    async def handle_model_connection_error(request: Request, exc: APIConnectionError | OpenAIConnectionError):
        provider = "OpenAI" if isinstance(exc, OpenAIConnectionError) else "Groq"
        timed_out = isinstance(exc, (APITimeoutError, OpenAITimeoutError))
        status_code = 504 if timed_out else 502
        detail = f"{provider} did not respond in time." if timed_out else f"EDIS could not reach {provider}."
        log_failure("model_provider_connection_error", detail, exc, status_code, provider=provider)
        return JSONResponse(
            status_code=status_code, content={"detail": f"{detail} Please try again."},
        )

    @app.exception_handler(OpenAIAuthenticationError)
    async def handle_openai_auth_error(request: Request, exc: OpenAIAuthenticationError):
        detail = "OpenAI rejected the API credentials. Check OPENAI_API_KEY in the server configuration."
        log_failure("openai_authentication_failed", detail, exc, 502, provider="OpenAI")
        return JSONResponse(status_code=502, content={"detail": detail})

    @app.exception_handler(OutputParserException)
    async def handle_invalid_model_output(request: Request, exc: OutputParserException):
        log_failure(
            "invalid_model_output", "The model response did not match the expected output format.",
            exc, 502,
        )
        return JSONResponse(status_code=502, content={"detail": "The AI returned an invalid response. Please try again."})

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        cid = uuid.uuid4().hex[:12]
        started_at = perf_counter()
        context = bind_contextvars(request_id=cid)
        try:
            try:
                origin = request.headers.get("origin")
                expected_origin = f"{request.url.scheme}://{request.url.netloc}"
                if accounts is not None and request.method in ("POST", "DELETE") and origin and origin != expected_origin:
                    raise StarletteHTTPException(status_code=403, detail="Use EDIS from the same origin.")
                else:
                    response = await call_next(request)
            except StarletteHTTPException as error:
                response = await log_http_exception(request, error)
            except Exception as error:
                log_unexpected_failure(error)
                response = JSONResponse(
                    status_code=500,
                    content={"detail": "Internal server error", "request_id": cid},
                )
            route = request.scope.get("route")
            log_request = logger.warning if response.status_code >= 400 else logger.info
            log_request(
                "http_request",
                message=(
                    "The HTTP request finished with an error."
                    if response.status_code >= 400 else "The HTTP request completed."
                ),
                method=request.method,
                route=getattr(route, "path", None),
                status_code=response.status_code,
                duration_ms=round((perf_counter() - started_at) * 1000, 1),
            )
            response.headers["X-Request-ID"] = cid
            response.headers["X-Content-Type-Options"] = "nosniff"
            if request.url.path.startswith("/api/auth/"):
                response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            reset_contextvars(**context)

    return app


app = create_app()
