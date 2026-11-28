"""LangGraph workflow for answering questions about session documents."""

from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from structlog import get_logger

from app.ai_module.document_chat import DocumentChatService
from app.ai_module.retrieval import IndexService
from app.utils.custom_logger import LOGGER_NAME

logger = get_logger(LOGGER_NAME)


class ChatInput(TypedDict):
    session_id: str
    user: str
    question: str
    history: list[dict]
    top_k: int


class ChatState(ChatInput):
    query: str
    hits: list[dict]
    answer: str
    cited_hits: list[dict]


def build_document_chat_graph(chat: DocumentChatService, index: IndexService):
    """Build a graph invoked while the portal holds the session storage lock."""

    def rewrite_query(state: ChatState) -> dict:
        if state["history"]:
            logger.info("document_chat_rewrite", message="Using chat history to prepare the document search.")
        return {"query": chat.rewrite_query(state["question"], state["history"])}

    def retrieve(state: ChatState) -> dict:
        logger.info("document_chat_search", message="Searching the document index.", top_k=state["top_k"])
        hits = index.search_locked(
            state["session_id"], state["user"], state["query"], k=state["top_k"]
        )
        logger.info("document_chat_search_finished", message="Document search finished.", matching_chunks=len(hits))
        return {"hits": hits}

    def next_step(state: ChatState) -> Literal["retrieve_original", "answer"]:
        if not state["hits"] and state["query"] != state["question"]:
            return "retrieve_original"
        return "answer"

    def retrieve_original(state: ChatState) -> dict:
        logger.info(
            "document_chat_search_retry",
            message="The rewritten query found no matches. Searching again with the original question.",
        )
        return retrieve({**state, "query": state["question"]})

    def answer(state: ChatState) -> dict:
        message = (
            "Generating an answer from the matching document chunks."
            if state["hits"] else "No matching chunks were found. Returning the no-evidence reply."
        )
        logger.info("document_chat_answer", message=message)
        reply, cited_hits = chat.answer(state["question"], state["hits"], state["history"])
        return {"answer": reply, "cited_hits": cited_hits}

    workflow = StateGraph(ChatState, input_schema=ChatInput)
    workflow.add_node("rewrite_query", rewrite_query)
    workflow.add_node("retrieve", retrieve)
    workflow.add_node("retrieve_original", retrieve_original)
    workflow.add_node("answer", answer)
    workflow.add_edge(START, "rewrite_query")
    workflow.add_edge("rewrite_query", "retrieve")
    workflow.add_conditional_edges("retrieve", next_step)
    workflow.add_edge("retrieve_original", "answer")
    workflow.add_edge("answer", END)
    return workflow.compile()
