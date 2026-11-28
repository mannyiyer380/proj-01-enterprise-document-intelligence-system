"""Size-limited validated uploads and page-preserving PDF/DOCX/TXT parsing."""
from __future__ import annotations

import io
import hashlib
import json
import re
import tempfile
import zipfile
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import Protocol


from fastapi import HTTPException, UploadFile
from structlog import get_logger

from app.config.config import Settings
from app.utils.custom_logger import LOGGER_NAME

SUPPORTED = {".pdf", ".docx", ".txt"}
LOCAL_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
logger = get_logger(LOGGER_NAME)


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...




class HuggingFaceCreditsUnavailable(HTTPException):
    def __init__(self):
        super().__init__(503, "Hugging Face embedding credits are unavailable. Load the documents in a new session to use local embeddings.")


class LocalHuggingFaceEmbeddings:
    """Run the small BGE model locally and reuse its downloaded weights."""

    @cached_property
    def model(self):
        from fastembed import TextEmbedding

        logger.info(
            "loading_local_huggingface_embedding_model",
            message="Loading the Hugging Face embedding model for local use.",
            model=LOCAL_EMBEDDING_MODEL,
        )
        try:
            return TextEmbedding(model_name=LOCAL_EMBEDDING_MODEL, threads=4)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(
                503, "The local embedding model could not be loaded. Its first download needs internet access."
            ) from exc

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self.model.passage_embed(texts, batch_size=32)]

    def embed_query(self, text: str) -> list[float]:
        return next(self.model.query_embed(text)).tolist()


class HuggingFaceEmbeddings:
    """Request sentence embeddings from the Hugging Face Inference API."""

    def __init__(self, model_name: str, token: str):
        from huggingface_hub import InferenceClient

        self.client = InferenceClient(
            model=model_name, provider="hf-inference", token=token, timeout=60
        )

    def _embed(self, texts: list[str]) -> list[list[float]]:
        from huggingface_hub.errors import HfHubHTTPError, InferenceTimeoutError
        from httpx import RequestError

        try:
            vectors = self.client.feature_extraction(texts, truncate=True)
        except HfHubHTTPError as exc:
            logger.warning(
                "embedding_provider_error",
                message=(
                    "Hugging Face hosted embedding credits are unavailable."
                    if exc.response.status_code == 402 else "Hugging Face rejected the hosted embedding request."
                ),
                provider="huggingface",
                upstream_status=exc.response.status_code,
                batch_size=len(texts),
            )
            if exc.response.status_code in (401, 403):
                raise HTTPException(503, "Hugging Face rejected the embedding request. Check the token's Inference Providers permission.") from None
            if exc.response.status_code == 402:
                raise HuggingFaceCreditsUnavailable() from None
            if exc.response.status_code == 429:
                raise HTTPException(503, "Hugging Face embedding rate limit reached. Try again later.") from None
            raise HTTPException(502, "Hugging Face could not generate embeddings.") from None
        except InferenceTimeoutError:
            logger.warning("embedding_provider_error", message="Hugging Face did not respond before the embedding timeout.")
            raise HTTPException(504, "Hugging Face embedding request timed out.") from None
        except RequestError:
            logger.warning("embedding_provider_error", message="Could not connect to Hugging Face for hosted embeddings.")
            raise HTTPException(502, "Could not reach Hugging Face Inference.") from None
        if vectors.ndim == 1 and len(texts) == 1:
            vectors = vectors.reshape(1, -1)
        if vectors.ndim != 2 or vectors.shape[0] != len(texts) or vectors.shape[1] == 0:
            raise HTTPException(502, "Hugging Face returned invalid sentence embeddings.")
        return vectors.tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        batch_size = 32
        vectors = []
        for start in range(0, len(texts), batch_size):
            vectors.extend(self._embed(texts[start:start + batch_size]))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


def make_embedder(settings: Settings) -> Embedder:
    """Build the configured embedding provider."""
    if settings.embedding_provider == "huggingface":
        return HuggingFaceEmbeddings(settings.huggingface_embedding_model, settings.huggingface_token)
    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    return GoogleGenerativeAIEmbeddings(
        model=settings.google_embedding_model,
        google_api_key=settings.google_api_key,
    )


@dataclass(frozen=True)
class Page:
    number: int
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    filename: str
    content: bytes
    pages: list[Page]


def clean_filename(filename: str | None) -> str:
    if not filename:
        raise HTTPException(status_code=400, detail="Filename is required")
    # Windows path separators are also invalid even on Linux.
    if filename != Path(filename).name or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if len(filename) > 150 or not re.fullmatch(r"[\w .()\-]+", filename, re.UNICODE):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return filename


async def read_upload_bytes(upload: UploadFile, settings: Settings) -> tuple[str, bytes]:
    name = clean_filename(upload.filename)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    # Read at most max_bytes+1, rather than trusting Content-Length.
    raw = await upload.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail=f"File exceeds {settings.max_upload_mb} MB")
    validate_file_type(name, raw)
    return name, raw


async def read_upload(upload: UploadFile, settings: Settings) -> ParsedDocument:
    name, raw = await read_upload_bytes(upload, settings)
    return parse_bytes(name, raw, settings)


def validate_file_type(name: str, content: bytes) -> str:
    extension = Path(name).suffix.lower()
    if extension not in SUPPORTED:
        raise HTTPException(status_code=415, detail="Supported extensions: .pdf, .docx, .txt")
    if not content:
        raise HTTPException(status_code=400, detail="Empty document")
    return extension


def parse_bytes(filename: str, content: bytes, settings: Settings) -> ParsedDocument:
    name = clean_filename(filename)
    extension = validate_file_type(name, content)
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large")

    pages: list[Page]
    try:
        if extension == ".pdf":
            if not content.startswith(b"%PDF-"):
                raise HTTPException(422, "The file does not have a valid PDF header.")
            import fitz
            with fitz.open(stream=content, filetype="pdf") as pdf:
                if pdf.is_encrypted:
                    raise HTTPException(422, "Password-protected PDFs are not supported.")
                if len(pdf) > settings.max_pages:
                    raise HTTPException(status_code=413, detail="Too many PDF pages")
                pages = [Page(i + 1, (page.get_text("text") or "").strip()) for i, page in enumerate(pdf)]
        elif extension == ".docx":
            if not zipfile.is_zipfile(io.BytesIO(content)):
                raise HTTPException(422, "The file is not a valid DOCX archive.")
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                if "word/document.xml" not in z.namelist():
                    raise HTTPException(422, "The DOCX archive is missing its document content.")
                # Prevent archive bombs before allowing extraction through docx2txt.
                if sum(m.file_size for m in z.infolist()) > settings.max_upload_mb * 1024 * 1024 * 5:
                    raise HTTPException(422, "The DOCX archive exceeds the size limit after extraction.")
            import docx2txt
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "input.docx"
                path.write_bytes(content)
                pages = [Page(1, docx2txt.process(str(path)).strip())]
        else:
            text = content.decode("utf-8-sig")
            if "\x00" in text:
                raise HTTPException(422, "The TXT file contains binary data.")
            pages = [Page(1, text.strip())]
    except HTTPException:
        raise
    except (ValueError, RuntimeError, OSError, zipfile.BadZipFile) as exc:
        detail = f"Could not read the {extension} file. Check that it opens correctly."
        if extension == ".txt" and isinstance(exc, UnicodeError):
            detail = "The TXT file could not be decoded. Save it as UTF-8 and try again."
        raise HTTPException(422, detail) from exc
    if not any(page.text for page in pages):
        raise HTTPException(status_code=422, detail="No extractable text; scanned PDF needs OCR")
    if sum(len(page.text) for page in pages) > settings.max_extracted_chars:
        raise HTTPException(
            status_code=413,
            detail=f"Extracted text exceeds {settings.max_extracted_chars:,} characters",
        )
    return ParsedDocument(filename=name, content=content, pages=pages)


def chunk_pages(doc: ParsedDocument, chunk_size: int, overlap: int) -> list[dict]:
    chunks: list[dict] = []
    for page in doc.pages:
        text = page.text
        if not text:
            continue
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            # Prefer whitespace boundary for readable citations, without stalling progress.
            if end < len(text):
                boundary = text.rfind(" ", max(start + chunk_size // 2, end - 80), end)
                if boundary > start:
                    end = boundary
            piece = text[start:end].strip()
            if piece:
                chunks.append({"document": doc.filename, "page": page.number, "text": piece})
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
    return chunks


class DocumentIngestionService:
    """Parse and validate files from the configured server dataset."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def dataset_files(self) -> list[Path]:
        dataset_dir = self.settings.dataset_dir
        if not dataset_dir.is_dir():
            raise HTTPException(status_code=404, detail="Dataset folder is not available")

        files = sorted(
            path
            for path in dataset_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED
        )
        if not files:
            raise HTTPException(
                status_code=404,
                detail="No PDF, DOCX, or TXT files found in the dataset folder",
            )
        if len(files) > self.settings.max_dataset_files:
            raise HTTPException(
                status_code=400,
                detail=f"Dataset contains more than {self.settings.max_dataset_files} supported files",
            )
        return files

    def dataset_snapshot(self) -> tuple[str, set[tuple[str, str]]]:
        """Identify the current dataset without parsing or generating embeddings."""
        entries = []
        documents = set()
        limit = self.settings.max_dataset_file_mb * 1024 * 1024
        for path in self.dataset_files():
            relative_path = path.relative_to(self.settings.dataset_dir).as_posix()
            try:
                stat = path.stat()
                if stat.st_size > limit:
                    entries.append((relative_path, "oversized", stat.st_size, stat.st_mtime_ns))
                    continue
                with path.open("rb") as file:
                    digest = hashlib.file_digest(file, "sha256").hexdigest()
            except OSError:
                entries.append((relative_path, "unreadable"))
                continue
            entries.append((relative_path, digest))
            documents.add((path.name, digest))

        profile = (
            self.settings.chunk_size,
            self.settings.chunk_overlap,
            self.settings.max_dataset_file_mb,
            self.settings.max_dataset_pages,
            self.settings.max_dataset_extracted_chars,
        )
        key = hashlib.sha256(json.dumps([profile, entries]).encode()).hexdigest()
        return key, documents

    def load_dataset(self) -> tuple[list[ParsedDocument], list[dict[str, str]], int]:
        dataset_dir = self.settings.dataset_dir
        files = self.dataset_files()
        logger.info("dataset_files_found", message="Found files in the server dataset. Reading them for indexing.", files=len(files))

        dataset_settings = replace(
            self.settings,
            max_upload_mb=self.settings.max_dataset_file_mb,
            max_pages=self.settings.max_dataset_pages,
            max_extracted_chars=self.settings.max_dataset_extracted_chars,
        )
        documents: list[ParsedDocument] = []
        skipped: list[dict[str, str]] = []
        for path in files:
            relative_name = path.relative_to(dataset_dir).as_posix()
            try:
                if path.stat().st_size > dataset_settings.max_upload_mb * 1024 * 1024:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds {dataset_settings.max_upload_mb} MB",
                    )
                documents.append(
                    parse_bytes(path.name, path.read_bytes(), dataset_settings)
                )
            except (HTTPException, OSError) as exc:
                detail = exc.detail if isinstance(exc, HTTPException) else "Could not read the file from disk."
                skipped.append({"file": relative_name, "reason": str(detail)})
                logger.warning("dataset_file_skipped", message="Skipped a server dataset file.", file=relative_name, reason=detail)

        if not documents:
            raise HTTPException(
                status_code=422,
                detail="Dataset files were found, but none could be processed",
            )
        return documents, skipped, len(files)
