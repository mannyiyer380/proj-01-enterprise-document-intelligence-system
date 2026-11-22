"""Application orchestration, avoiding stateful LLM chains stored in global variables."""
from __future__ import annotations

from fastapi import HTTPException

from app.config.config import Settings
from app.ai_module.schemas import AskRequest, ChatResponse, SourceHit
from app.ai_module.document_chat import DocumentChatService
from app.ai_module.document_compare import DocumentCompareService
from app.ai_module.document_analysis import DocumentAnalysisService
from app.ai_module.document_ingestion import DocumentIngestionService, ParsedDocument
from app.ai_module.document_ingestion import make_embedder
from app.utils.model_provider import ModelProvider
from app.ai_module.retrieval import IndexService
from app.services.session_storage import SessionStorage


class PortalService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.warehouse = SessionStorage(settings.storage_root / "sessions")
        self.embedder = make_embedder(settings)
        self.index = IndexService(self.warehouse, self.embedder, settings)
        self.models = ModelProvider(settings)
        chat_models = self.models
        if settings.chat_provider != settings.llm_provider:
            chat_models = ModelProvider(settings, provider=settings.chat_provider)
        self.document_chat = DocumentChatService(settings, chat_models)
        self.document_ingestion = DocumentIngestionService(settings)
        self.document_analysis = DocumentAnalysisService(settings, self.models)
        self.document_compare = DocumentCompareService(settings, self.document_analysis)

    def create_session(self, user: str) -> dict:
        return self.warehouse.create(user)

    def session_detail(self, sid: str, user: str) -> dict:
        manifest = self.warehouse.owned(sid, user)
        history = self.warehouse.read_json(self.warehouse.path(sid) / "history.json")
        return dict(self.warehouse.public_manifest(manifest), history=history)

    def ingest(self, sid: str, user: str, documents: list[ParsedDocument]) -> dict:
        return self.index.add(sid, user, documents)

    def ingest_dataset(self, sid: str, user: str) -> dict:
        """Load supported documents from the configured, server-owned dataset folder."""
        self.warehouse.owned(sid, user)
        dataset_key, _ = self.document_ingestion.dataset_snapshot()
        documents, skipped, discovered = self.document_ingestion.load_dataset()
        with self.warehouse.lock:
            result = self.ingest(sid, user, documents)
            manifest = self.warehouse.owned(sid, user)
            manifest["dataset_key"] = dataset_key
            self.warehouse.write_json(self.warehouse.path(sid) / "manifest.json", manifest)
        return dict(
            result,
            discovered_files=discovered,
            processed_files=len(documents),
            skipped_files=skipped,
        )

    def saved_dataset_session(self, user: str) -> dict | None:
        try:
            dataset_key, dataset_documents = self.document_ingestion.dataset_snapshot()
        except HTTPException as error:
            if error.status_code in (400, 404):
                return None
            raise

        matches = []
        with self.warehouse.lock:
            for folder in self.warehouse.root.iterdir():
                if not folder.is_dir():
                    continue
                try:
                    manifest = self.warehouse.owned(folder.name, user)
                except HTTPException as error:
                    if error.status_code in (400, 404):
                        continue
                    raise
                except OSError:
                    continue
                indexed = {(doc["name"], doc["sha256"]) for doc in manifest["documents"]}
                if "dataset_key" in manifest:
                    if manifest["dataset_key"] != dataset_key or not indexed <= dataset_documents:
                        continue
                else:
                    if not indexed or indexed != dataset_documents:
                        continue
                matches.append(manifest)

            for manifest in sorted(matches, key=lambda item: item["created_at"], reverse=True):
                sid = manifest["session_id"]
                if self.index.has_saved_index(sid, manifest):
                    return self.session_detail(sid, user)
        return None

    def direct_chat(self, data: AskRequest) -> ChatResponse:
        if not self.document_chat.is_small_talk(data.question):
            raise HTTPException(409, "Use Load Dataset or Upload Documents before asking about files.")
        return ChatResponse(
            answer=self.document_chat.answer_directly(data.question),
            sources=[], session_id=None, mode=self.settings.chat_provider, answer_kind="general",
        )

    def chat(self, sid: str, user: str, data: AskRequest) -> ChatResponse:
        with self.warehouse.lock:
            self.warehouse.owned(sid, user)
            path = self.warehouse.path(sid) / "history.json"
            history = self.warehouse.read_json(path)
            if self.document_chat.is_small_talk(data.question):
                result = self.direct_chat(data).model_copy(update={"session_id": sid})
            else:
                document_history = [
                    turn for turn in history
                    if turn.get("conversation_id") == data.conversation_id
                    and turn.get("answer_kind") != "general"
                ]
                standalone = self.document_chat.rewrite_query(data.question, document_history)
                hits = self.index.search(sid, user, standalone, k=data.top_k)
                result = ChatResponse(
                    answer=self.document_chat.answer(data.question, hits, document_history),
                    sources=[SourceHit(document=hit["document"], page=hit["page"],
                                       score=hit["score"], excerpt=hit["text"][:350]) for hit in hits],
                    session_id=sid,
                    mode=self.settings.chat_provider,
                )
            turns = [
                {"role": "user", "content": data.question, "answer_kind": result.answer_kind},
                {
                    "role": "assistant", "content": result.answer, "answer_kind": result.answer_kind,
                    "sources": [source.model_dump() for source in result.sources],
                },
            ]
            if data.conversation_id is not None:
                for turn in turns:
                    turn["conversation_id"] = data.conversation_id
            self.warehouse.write_json(path, (history + turns)[-40:])
        return result

    def analyze(self, doc: ParsedDocument):
        return self.document_analysis.analyze(doc)

    def compare(self, ref: ParsedDocument, updated: ParsedDocument):
        return self.document_compare.compare(ref, updated)
