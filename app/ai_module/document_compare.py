"""Compare matching PDF pages or review unpaginated documents by topic."""

from __future__ import annotations


from pathlib import Path

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser, StrOutputParser

from app.ai_module.document_analysis import DocumentAnalysisService
from app.ai_module.document_ingestion import ParsedDocument
from app.ai_module.schemas import ChangeItem, CompareResponse, ComparisonOverview
from app.config.config import Settings
from app.ai_module.prompt_manager import PromptManager


class DocumentCompareService:
    def __init__(self, settings: Settings, analysis: DocumentAnalysisService):
        self.settings = settings
        self.analysis = analysis

    def compare(
        self, reference: ParsedDocument, actual: ParsedDocument
    ) -> CompareResponse:
        both_pdf = all(
            Path(document.filename).suffix.lower() == ".pdf"
            for document in (reference, actual)
        )
        if not both_pdf:
            left = "\n\n".join(page.text for page in reference.pages)
            right = "\n\n".join(page.text for page in actual.pages)
            if left != right and max(len(left), len(right)) > 6_000:
                return self._compare_sections(reference, actual, left, right)
            return CompareResponse(
                changes=[self._compare_text(left, right, 1)],
                reference_pages=len(reference.pages),
                actual_pages=len(actual.pages),
                mode=self.settings.llm_provider,
                comparison_kind="document",
            )

        changes: list[ChangeItem] = []
        page_count = max(len(reference.pages), len(actual.pages))
        for index in range(page_count):
            left = reference.pages[index].text if index < len(reference.pages) else ""
            right = actual.pages[index].text if index < len(actual.pages) else ""
            changes.append(self._compare_text(left, right, index + 1))

        return CompareResponse(
            changes=changes,
            reference_pages=len(reference.pages),
            actual_pages=len(actual.pages),
            mode=self.settings.llm_provider,
        )

    def _compare_text(self, left: str, right: str, page: int) -> ChangeItem:
        if left == right:
            return ChangeItem(page=page, changed=False, summary="NO CHANGE")
        model = self.analysis.analysis_model()
        if len(left) > 6_000:
            _, left = self.analysis.comparison_sections(left, model, "reference")
        if len(right) > 6_000:
            _, right = self.analysis.comparison_sections(right, model, "second")
        chain = PromptManager.document_compare() | model | StrOutputParser()
        summary = self.analysis.invoke_model(chain, {"left": left, "right": right}).strip()
        if not summary:
            raise OutputParserException("The comparison output was empty.")
        return ChangeItem(page=page, changed=True, summary=summary)

    def _compare_sections(
        self, reference: ParsedDocument, actual: ParsedDocument, left: str, right: str
    ) -> CompareResponse:
        model = self.analysis.analysis_model()
        left_sections, left_summary = self.analysis.comparison_sections(left, model, "reference")
        right_sections, right_summary = self.analysis.comparison_sections(right, model, "second")
        chain = (
            PromptManager.compare_document_sections(ComparisonOverview)
            | model
            | PydanticOutputParser(pydantic_object=ComparisonOverview)
        )
        overview = self.analysis.invoke_model(chain, {"left": left_summary, "right": right_summary})
        return CompareResponse(
            changes=[],
            reference_pages=len(reference.pages),
            actual_pages=len(actual.pages),
            mode=self.settings.llm_provider,
            comparison_kind="sections",
            overview=overview,
            reference_sections=left_sections,
            actual_sections=right_sections,
        )
