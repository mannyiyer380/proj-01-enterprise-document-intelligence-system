"""Document Portal public response contracts."""
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    top_k: int = Field(default=4, ge=1, le=12)
    conversation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class SourceHit(BaseModel):
    document: str
    page: int
    score: float
    excerpt: str


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceHit]
    session_id: str | None
    mode: Literal["google", "groq", "openai"]
    answer_kind: Literal["document", "general"] = "document"


class DocumentAnalysis(BaseModel):
    title: str
    summary: str
    keywords: list[str]
    language: str
    page_count: int
    mode: str


class ChangeItem(BaseModel):
    page: int
    changed: bool
    summary: str
    reference_page: int | None = None
    actual_page: int | None = None


class ComparisonOverview(BaseModel):
    overview: str
    differences: list[str]
    similarities: list[str]


class CompareResponse(BaseModel):
    changes: list[ChangeItem]
    reference_pages: int
    actual_pages: int
    mode: str
    comparison_kind: Literal["pages", "document", "sections"] = "pages"
    overview: ComparisonOverview | None = None
    reference_sections: list[str] = Field(default_factory=list)
    actual_sections: list[str] = Field(default_factory=list)


DocumentWorkflow = Literal["analysis", "compare"]
ReviewStatus = Literal["approved", "changes_requested"]
MAX_CUSTOM_INSTRUCTIONS = 1200


class InstructionPreference(BaseModel):
    instructions: Annotated[str, StringConstraints(max_length=MAX_CUSTOM_INSTRUCTIONS, strip_whitespace=True)]


class InstructionDefaults(BaseModel):
    analysis: str = ""
    compare: str = ""


class ReviewDecision(BaseModel):
    status: ReviewStatus
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ""

    @model_validator(mode="after")
    def require_change_note(self):
        if self.status == "changes_requested" and not self.note:
            raise ValueError("Describe the changes you want.")
        return self


class ResultReview(BaseModel):
    review_id: str
    workflow: DocumentWorkflow
    status: ReviewStatus | Literal["pending"] = "pending"
    note: str = ""
    created_at: str
    reviewed_at: str | None = None
