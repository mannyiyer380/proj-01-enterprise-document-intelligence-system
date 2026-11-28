"""Application orchestration, avoiding stateful LLM chains stored in global variables."""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from app.config.config import Settings
from app.ai_module.schemas import AskRequest, ChatResponse, SourceHit
from app.ai_module.document_chat import DocumentChatService
from app.ai_module.document_chat_graph import build_document_chat_graph
from app.ai_module.document_compare import DocumentCompareService
from app.ai_module.document_analysis import DocumentAnalysisService
from app.ai_module.document_ingestion import DocumentIngestionService, ParsedDocument
from app.ai_module.document_ingestion import make_embedder
from app.utils.model_provider import ModelProvider
from app.ai_module.retrieval import IndexService
from app.services.document_result_cache import DocumentResultCache
from app.services.result_reviews import ResultReviewStore
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
        self.document_chat_graph = build_document_chat_graph(self.document_chat, self.index)
        self.document_ingestion = DocumentIngestionService(settings)
        self.document_analysis = DocumentAnalysisService(settings, self.models)
        self.document_compare = DocumentCompareService(settings, self.document_analysis)
        self.document_results = DocumentResultCache()
        self.result_reviews = ResultReviewStore(settings.storage_root / "result_reviews")

    def create_session(self, user: str) -> dict:
        return self.warehouse.create(user)

    def session_detail(self, sid: str, user: str) -> dict:
        with self.warehouse.lock:
            manifest = self.warehouse.owned(sid, user)
            history = self._read_chat_history(self.warehouse.path(sid) / "history.json")
        return dict(self.warehouse.public_manifest(manifest), history=history)

    def _read_chat_history(self, path: Path) -> list[dict]:
        history = self.warehouse.read_json(path)
        for index, turn in enumerate(history):
            if turn.get("role") == "assistant" and self.document_chat.is_unsupported_answer(turn.get("content", "")):
                turn["sources"] = []
            if turn.get("role") != "user" or turn.get("answer_kind") == "general":
                continue
            if not self.document_chat.is_small_talk(turn.get("content", "")):
                continue
            turn["answer_kind"] = "general"
            if index + 1 < len(history):
                reply = history[index + 1]
                if reply.get("role") == "assistant" and reply.get("conversation_id") == turn.get("conversation_id"):
                    reply.update(answer_kind="general", sources=[])
        return history

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

    def general_chat_history(self, user: str, conversation_id: str) -> list[dict]:
        with self.warehouse.general_chat_lock:
            history = self.warehouse.read_json(self.warehouse.general_history_path(user))
        return self._conversation(history, conversation_id)

    def direct_chat(self, data: AskRequest, user: str | None = None) -> ChatResponse:
        if user is None or data.conversation_id is None:
            return self._answer_general_chat(data.question, [])

        with self.warehouse.general_chat_lock:
            path = self.warehouse.general_history_path(user)
            history = self.warehouse.read_json(path)
            result = self._answer_general_chat(data.question, self._conversation(history, data.conversation_id))
            self._save_chat_turns(path, history, data, result)
        return result

    def _answer_general_chat(self, question: str, history: list[dict]) -> ChatResponse:
        answer = self.document_chat.general_reply(question, history=history, documents=[])
        if answer is None:
            raise HTTPException(409, "Use Load Dataset or Upload Documents before asking about files.")
        return self._general_chat_response(answer)

    def _general_chat_response(self, answer: str, sid: str | None = None) -> ChatResponse:
        return ChatResponse(
            answer=answer, sources=[], session_id=sid, mode=self.settings.chat_provider, answer_kind="general",
        )

    @staticmethod
    def _conversation(history: list[dict], conversation_id: str | None) -> list[dict]:
        return [turn for turn in history if turn.get("conversation_id") == conversation_id]

    def _save_chat_turns(self, path: Path, history: list[dict], data: AskRequest, result: ChatResponse) -> None:
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

    def chat(self, sid: str, user: str, data: AskRequest) -> ChatResponse:
        with self.warehouse.lock:
            manifest = self.warehouse.owned(sid, user)
            path = self.warehouse.path(sid) / "history.json"
            history = self._read_chat_history(path)
            conversation = self._conversation(history, data.conversation_id)
            answer = self.document_chat.general_reply(
                data.question, conversation, [doc["name"] for doc in manifest["documents"]],
            )
            if answer is not None:
                result = self._general_chat_response(answer, sid)
            else:
                document_history = [turn for turn in conversation if turn.get("answer_kind") != "general"]
                state = self.document_chat_graph.invoke({
                    "session_id": sid,
                    "user": user,
                    "question": data.question,
                    "history": document_history,
                    "top_k": data.top_k,
                })
                result = ChatResponse(
                    answer=state["answer"],
                    sources=[
                        SourceHit(
                            document=hit["document"], page=hit["page"],
                            score=hit["score"], excerpt=hit["text"][:350],
                        )
                        for hit in state["cited_hits"]
                    ],
                    session_id=sid,
                    mode=self.settings.chat_provider,
                )
            self._save_chat_turns(path, history, data, result)
        return result

    def analyze(self, doc: ParsedDocument, custom_instructions: str = ""):
        return self.document_analysis.analyze(doc, custom_instructions)

    def compare(self, ref: ParsedDocument, updated: ParsedDocument, custom_instructions: str = ""):
        return self.document_compare.compare(ref, updated, custom_instructions)
