"""Session-scoped Chroma vector indexing and search."""
from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from structlog import get_logger

from app.config.config import Settings
from app.utils.custom_logger import LOGGER_NAME
from app.ai_module.document_ingestion import (
    Embedder,
    HuggingFaceCreditsUnavailable,
    LOCAL_EMBEDDING_MODEL,
    LocalHuggingFaceEmbeddings,
    ParsedDocument,
    chunk_pages,
)
from app.services.session_storage import SessionStorage

logger = get_logger(LOGGER_NAME)
LOCAL_EMBEDDING_PROFILE = f"huggingface-local-fastembed:{LOCAL_EMBEDDING_MODEL}"

class IndexService:
    def __init__(self, warehouse: SessionStorage, embedder: Embedder, settings: Settings):
        self.warehouse = warehouse
        self.embedder = embedder
        self.local_embedder = LocalHuggingFaceEmbeddings()
        self.settings = settings
        self._chroma = None

    def _check_index(self, manifest: dict) -> str:
        provider = self.settings.embedding_provider
        profile = (
            f"huggingface-hosted:{self.settings.huggingface_embedding_model}"
            if provider == "huggingface"
            else f"google:{self.settings.google_embedding_model}"
        )
        if manifest["documents"] and manifest.get("vector_backend") != "chroma":
            raise HTTPException(409, "This session uses an old index. Load the documents in a new session.")
        stored_profile = manifest.get("embedding_profile")
        if stored_profile == LOCAL_EMBEDDING_PROFILE and provider == "huggingface":
            return stored_profile
        if stored_profile not in (None, profile):
            raise HTTPException(409, "Embedding model changed. Load the documents in a new session.")
        return profile

    def _embedder_for(self, profile: str) -> Embedder:
        return self.local_embedder if profile == LOCAL_EMBEDDING_PROFILE else self.embedder

    def _save_parsed_document(
        self, manifest: dict, document: ParsedDocument, digest: str, chunks: list[dict]
    ) -> None:
        timestamp = datetime.fromisoformat(manifest["created_at"]).astimezone()
        session_folder = self.settings.output_dir / timestamp.strftime("%Y-%m-%d_%H-%M-%S_%f")
        name = f"{Path(document.filename).stem[:48]}_{digest[:16]}.json"
        self.warehouse.write_json(session_folder / name, {
            "session_id": manifest["session_id"],
            "filename": document.filename,
            "sha256": digest,
            "chunk_size_chars": self.settings.chunk_size,
            "chunk_overlap_chars": self.settings.chunk_overlap,
            "page_count": len(document.pages),
            "chunk_count": len(chunks),
            "pages": [asdict(page) for page in document.pages],
            "chunks": chunks,
        })

    def _client(self):
        if self._chroma is None:
            import chromadb

            self._chroma = chromadb.PersistentClient(
                path=str(self.settings.storage_root / "chroma")
            )
        return self._chroma

    @staticmethod
    def _collection_name(sid: str) -> str:
        return f"session-{sid}"

    def has_saved_index(self, sid: str, manifest: dict) -> bool:
        from chromadb.errors import NotFoundError

        if not manifest["documents"]:
            return False
        try:
            self._check_index(manifest)
            collection = self._client().get_collection(
                self._collection_name(sid), embedding_function=None
            )
            return collection.count() >= sum(doc["chunks"] for doc in manifest["documents"])
        except (HTTPException, NotFoundError):
            return False

    def delete_session(self, sid: str, owner: str) -> None:
        with self.warehouse.lock:
            from chromadb.errors import NotFoundError

            self.warehouse.owned(sid, owner)
            try:
                self._client().delete_collection(self._collection_name(sid))
            except NotFoundError:
                pass
            self.warehouse.delete(sid, owner)

    def add(self, sid: str, owner: str, documents: list[ParsedDocument]) -> dict:
        with self.warehouse.lock:
            manifest = self.warehouse.owned(sid, owner)
            folder = self.warehouse.path(sid)
            embedding_profile = self._check_index(manifest)
            existing_hashes = {d["sha256"] for d in manifest["documents"]}
            new_documents: list[dict] = []
            new_chunks: list[dict] = []
            new_files: list[tuple[str, bytes]] = []
            for document in documents:
                digest = hashlib.sha256(document.content).hexdigest()
                if digest in existing_hashes:
                    continue
                existing_hashes.add(digest)
                file_key = f"{digest}{Path(document.filename).suffix.lower()}"
                chunks = chunk_pages(document, self.settings.chunk_size, self.settings.chunk_overlap)
                for offset, chunk in enumerate(chunks):
                    # Stable identity includes content digest AND page/chunk offset.
                    chunk["chunk_id"] = f"{digest}:{chunk['page']}:{offset}"
                self._save_parsed_document(manifest, document, digest, chunks)
                new_chunks.extend(chunks)
                new_documents.append({"name": document.filename, "sha256": digest, "pages": len(document.pages), "chunks": len(chunks)})
                new_files.append((file_key, document.content))
            if not new_documents:
                return {"added_documents": 0, "added_chunks": 0, "skipped_duplicates": len(documents)}

            texts = [chunk["text"] for chunk in new_chunks]
            logger.info("document_embedding_started", documents=len(new_documents), chunks=len(texts))
            try:
                vectors = self._embedder_for(embedding_profile).embed_documents(texts)
            except HuggingFaceCreditsUnavailable:
                if manifest["documents"]:
                    raise
                logger.info("embedding_fallback", embedding_profile=LOCAL_EMBEDDING_PROFILE, chunks=len(texts))
                embedding_profile = LOCAL_EMBEDDING_PROFILE
                vectors = self.local_embedder.embed_documents(texts)
            if len(vectors) != len(new_chunks):
                raise ValueError("Embedding count does not match chunks")
            for filename, content in new_files:
                fpath = folder / filename
                if not fpath.exists():
                    fpath.write_bytes(content)
            client = self._client()
            collection = client.get_or_create_collection(
                self._collection_name(sid),
                embedding_function=None,
                configuration={"hnsw": {"space": "cosine"}},
            )
            batch_size = client.get_max_batch_size()
            for start in range(0, len(new_chunks), batch_size):
                batch = new_chunks[start:start + batch_size]
                collection.upsert(
                    ids=[chunk["chunk_id"] for chunk in batch],
                    embeddings=vectors[start:start + len(batch)],
                    documents=[chunk["text"] for chunk in batch],
                    metadatas=[
                        {"document": chunk["document"], "page": chunk["page"],
                         "sha256": chunk["chunk_id"].split(":", 1)[0]}
                        for chunk in batch
                    ],
                )
            manifest["vector_backend"] = "chroma"
            manifest["embedding_profile"] = embedding_profile
            manifest["documents"].extend(new_documents)
            self.warehouse.write_json(folder / "manifest.json", manifest)
            return {"added_documents": len(new_documents), "added_chunks": len(new_chunks), "skipped_duplicates": len(documents) - len(new_documents)}

    def search(self, sid: str, owner: str, question: str, k: int = 4) -> list[dict]:
        with self.warehouse.lock:
            manifest = self.warehouse.owned(sid, owner)
            profile = self._check_index(manifest)
            return self._search_chroma(sid, manifest, question, k, self._embedder_for(profile))

    def _search_chroma(
        self, sid: str, manifest: dict, question: str, k: int, embedder: Embedder
    ) -> list[dict]:
        from chromadb.errors import NotFoundError

        hashes = [document["sha256"] for document in manifest["documents"]]
        if not hashes:
            raise HTTPException(status_code=409, detail="Upload documents before chatting")
        try:
            collection = self._client().get_collection(
                self._collection_name(sid), embedding_function=None
            )
            result = collection.query(
                query_embeddings=[embedder.embed_query(question)],
                n_results=k,
                where={"sha256": {"$in": hashes}},
                include=["documents", "metadatas", "distances"],
            )
        except NotFoundError:
            raise HTTPException(
                409, "The document index is missing. Use Load Dataset or Upload Documents to rebuild it.",
            ) from None
        return [
            {"chunk_id": chunk_id, "document": metadata["document"], "page": metadata["page"],
             "text": document, "score": round(1.0 - distance, 4)}
            for chunk_id, metadata, document, distance in zip(
                result["ids"][0], result["metadatas"][0],
                result["documents"][0], result["distances"][0],
            )
        ]
