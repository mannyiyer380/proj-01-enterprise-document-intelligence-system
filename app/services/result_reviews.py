"""Store user decisions for individual generated results."""

from pathlib import Path
from threading import Lock
from uuid import uuid4

from fastapi import HTTPException

from app.ai_module.schemas import DocumentWorkflow, ResultReview, ReviewDecision
from app.security import SESSION_RE
from app.services.session_storage import SessionStorage, utc_now


class ResultReviewStore:
    def __init__(self, root: Path):
        self.root = root
        self.lock = Lock()

    def create(self, owner: str, workflow: DocumentWorkflow) -> str:
        review = ResultReview(review_id=uuid4().hex, workflow=workflow, created_at=utc_now())
        self._save(review, owner)
        return review.review_id

    def _save(self, review: ResultReview, owner: str) -> None:
        SessionStorage.write_json(self.root / f"{review.review_id}.json", {**review.model_dump(), "owner": owner})

    def _owned(self, review_id: str, owner: str) -> ResultReview:
        if not SESSION_RE.fullmatch(review_id):
            raise HTTPException(404, "Review not found")
        record = SessionStorage.read_json(self.root / f"{review_id}.json", fallback={})
        if record.get("owner") != owner:
            raise HTTPException(404, "Review not found")
        return ResultReview.model_validate(record)

    def get(self, review_id: str, owner: str) -> ResultReview:
        with self.lock:
            return self._owned(review_id, owner)

    def decide(self, review_id: str, owner: str, decision: ReviewDecision) -> ResultReview:
        with self.lock:
            review = self._owned(review_id, owner)
            if (review.status, review.note) != (decision.status, decision.note):
                review.status = decision.status
                review.note = decision.note
                review.reviewed_at = utc_now()
                self._save(review, owner)
            return review
