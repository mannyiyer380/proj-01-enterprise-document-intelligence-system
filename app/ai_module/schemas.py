"""Document Portal public response contracts."""
from typing import Literal

from pydantic import BaseModel, Field


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
