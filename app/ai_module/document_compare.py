"""Compare matching PDF pages or review unpaginated documents by topic."""

from __future__ import annotations


import operator
from difflib import SequenceMatcher
from itertools import zip_longest
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser, StrOutputParser
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from structlog import get_logger

from app.ai_module.document_analysis import DOCUMENT_WORKFLOW_CONCURRENCY, DocumentAnalysisService
from app.ai_module.document_ingestion import Page, ParsedDocument
from app.ai_module.schemas import ChangeItem, CompareResponse, ComparisonOverview
from app.config.config import Settings
from app.ai_module.prompt_manager import PromptManager
from app.utils.custom_logger import LOGGER_NAME

logger = get_logger(LOGGER_NAME)


class PageComparison(TypedDict):
    index: int
    reference: Page | None
    actual: Page | None
    custom_instructions: str


class ComparisonState(TypedDict):
    reference: ParsedDocument
    actual: ParsedDocument
    custom_instructions: str
    page_pairs: list[tuple[Page | None, Page | None]]
    changes: Annotated[list[tuple[int, ChangeItem]], operator.add]
    reference_text: str
    actual_text: str
    reference_summary: tuple[list[str], str]
    actual_summary: tuple[list[str], str]
    response: CompareResponse


class DocumentCompareService:
    def __init__(self, settings: Settings, analysis: DocumentAnalysisService):
        self.settings = settings
        self.analysis = analysis
        self.graph = self._build_graph()

    def compare(
        self, reference: ParsedDocument, actual: ParsedDocument, custom_instructions: str = ""
    ) -> CompareResponse:
        state = self.graph.invoke(
            {"reference": reference, "actual": actual, "custom_instructions": custom_instructions},
            config={"max_concurrency": DOCUMENT_WORKFLOW_CONCURRENCY},
        )
        return state["response"]

    def _build_graph(self):
        workflow = StateGraph(ComparisonState)
        workflow.add_node("align_pages", self._align_pages)
        workflow.add_node("compare_page", self._compare_page)
        workflow.add_node("collect_pages", self._collect_pages)
        workflow.add_node("prepare_documents", self._prepare_documents)
        workflow.add_node("compare_document", self._compare_document)
        workflow.add_node("summarize_reference", self._summarize_reference)
        workflow.add_node("summarize_actual", self._summarize_actual)
        workflow.add_node("compare_sections", self._compare_sections)
        workflow.add_conditional_edges(START, self._comparison_route)
        workflow.add_conditional_edges("align_pages", self._dispatch_pages, ["compare_page", "collect_pages"])
        workflow.add_conditional_edges("prepare_documents", self._document_route)
        workflow.add_edge("compare_page", "collect_pages")
        workflow.add_edge(["summarize_reference", "summarize_actual"], "compare_sections")
        workflow.add_edge("collect_pages", END)
        workflow.add_edge("compare_document", END)
        workflow.add_edge("compare_sections", END)
        return workflow.compile()

    @staticmethod
    def _comparison_route(state: ComparisonState) -> Literal["align_pages", "prepare_documents"]:
        both_pdf = all(
            Path(document.filename).suffix.lower() == ".pdf"
            for document in (state["reference"], state["actual"])
        )
        return "align_pages" if both_pdf else "prepare_documents"

    @staticmethod
    def _align_pages(state: ComparisonState) -> dict:
        reference = state["reference"].pages
        actual = state["actual"].pages
        matcher = SequenceMatcher(
            None,
            [" ".join(page.text.split()) for page in reference],
            [" ".join(page.text.split()) for page in actual],
            autojunk=False,
        )
        # Unchanged text anchors shifted pages. Pair edited pages in order between anchors.
        pairs = [
            pair
            for _, left_start, left_end, right_start, right_end in matcher.get_opcodes()
            for pair in zip_longest(reference[left_start:left_end], actual[right_start:right_end])
        ]
        logger.info(
            "pdf_pages_aligned", message="Aligned the PDF pages. Checking the aligned pairs for changes.",
            reference_pages=len(reference), second_document_pages=len(actual), page_pairs=len(pairs),
        )
        return {"page_pairs": pairs}

    @staticmethod
    def _dispatch_pages(state: ComparisonState) -> list[Send] | Literal["collect_pages"]:
        return [
            Send("compare_page", {
                "index": index, "reference": reference, "actual": actual,
                "custom_instructions": state["custom_instructions"],
            })
            for index, (reference, actual) in enumerate(state["page_pairs"])
        ] or "collect_pages"

    def _compare_page(self, pair: PageComparison) -> dict:
        reference = pair["reference"]
        actual = pair["actual"]
        reference_page = reference.number if reference else None
        actual_page = actual.number if actual else None
        left = reference.text if reference else ""
        right = actual.text if actual else ""
        change = self._compare_text(left, right, actual_page or reference_page, pair["custom_instructions"])
        change.reference_page = reference_page
        change.actual_page = actual_page
        if reference is None or actual is None:
            label = "Page added." if reference is None else "Page removed."
            change.summary = f"{label} {change.summary}" if change.changed else label
            change.changed = True
        return {"changes": [(pair["index"], change)]}

    def _collect_pages(self, state: ComparisonState) -> dict:
        changes = [change for _, change in sorted(state["changes"])]
        logger.info(
            "pdf_pages_compared", message="Finished checking the PDF pages for changes.",
            compared_pages=len(changes), changed_pages=sum(change.changed for change in changes),
        )
        return {"response": CompareResponse(
            changes=changes,
            reference_pages=len(state["reference"].pages),
            actual_pages=len(state["actual"].pages),
            mode=self.settings.llm_provider,
        )}

    @staticmethod
    def _prepare_documents(state: ComparisonState) -> dict:
        return {
            "reference_text": "\n\n".join(page.text for page in state["reference"].pages),
            "actual_text": "\n\n".join(page.text for page in state["actual"].pages),
        }

    @staticmethod
    def _document_route(
        state: ComparisonState,
    ) -> list[Literal["compare_document", "summarize_reference", "summarize_actual"]]:
        left = state["reference_text"]
        right = state["actual_text"]
        if left != right and max(len(left), len(right)) > 6_000:
            logger.info(
                "document_compare_by_section",
                message="The documents are too long for one comparison request. Summarizing each document first.",
            )
            return ["summarize_reference", "summarize_actual"]
        return ["compare_document"]

    def _compare_document(self, state: ComparisonState) -> dict:
        logger.info("document_compare_direct", message="Comparing the two documents as a whole.")
        return {"response": CompareResponse(
            changes=[self._compare_text(state["reference_text"], state["actual_text"], 1, state["custom_instructions"])],
            reference_pages=len(state["reference"].pages),
            actual_pages=len(state["actual"].pages),
            mode=self.settings.llm_provider,
            comparison_kind="document",
        )}

    def _compare_text(self, left: str, right: str, page: int, custom_instructions: str) -> ChangeItem:
        if left == right:
            return ChangeItem(page=page, changed=False, summary="NO CHANGE")
        model = self.analysis.analysis_model()
        if len(left) > 6_000:
            _, left = self.analysis.comparison_sections(left, model, "reference", custom_instructions)
        if len(right) > 6_000:
            _, right = self.analysis.comparison_sections(right, model, "second", custom_instructions)
        chain = PromptManager.document_compare(custom_instructions) | model | StrOutputParser()
        summary = self.analysis.invoke_model(chain, {"left": left, "right": right}).strip()
        if not summary:
            raise OutputParserException("The comparison output was empty.")
        return ChangeItem(page=page, changed=True, summary=summary)

    def _summarize_reference(self, state: ComparisonState) -> dict:
        return {"reference_summary": self.analysis.comparison_sections(
            state["reference_text"], self.analysis.analysis_model(), "reference", state["custom_instructions"],
        )}

    def _summarize_actual(self, state: ComparisonState) -> dict:
        return {"actual_summary": self.analysis.comparison_sections(
            state["actual_text"], self.analysis.analysis_model(), "second", state["custom_instructions"],
        )}

    def _compare_sections(self, state: ComparisonState) -> dict:
        left_sections, left_summary = state["reference_summary"]
        right_sections, right_summary = state["actual_summary"]
        model = self.analysis.analysis_model()
        chain = (
            PromptManager.compare_document_sections(ComparisonOverview, state["custom_instructions"])
            | model
            | PydanticOutputParser(pydantic_object=ComparisonOverview)
        )
        logger.info("document_compare_summaries", message="Comparing the section summaries from both documents.")
        overview = self.analysis.invoke_model(chain, {"left": left_summary, "right": right_summary})
        return {"response": CompareResponse(
            changes=[],
            reference_pages=len(state["reference"].pages),
            actual_pages=len(state["actual"].pages),
            mode=self.settings.llm_provider,
            comparison_kind="sections",
            overview=overview,
            reference_sections=left_sections,
            actual_sections=right_sections,
        )}
