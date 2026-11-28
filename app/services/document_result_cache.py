"""Process-local cache for successful document workflow responses."""

from collections.abc import Callable
from threading import Condition
from typing import TypeVar

from cachetools import TTLCache, cachedmethod
from pydantic import BaseModel
from structlog import get_logger

from app.utils.custom_logger import LOGGER_NAME
from app.utils.langsmith_tracing import trace_operation

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)
logger = get_logger(LOGGER_NAME)


class DocumentResultCache:
    def __init__(self) -> None:
        self._results = TTLCache(maxsize=100, ttl=3600)
        self._condition = Condition()

    @cachedmethod(
        cache=lambda self: self._results,
        key=lambda self, key, compute: key,
        condition=lambda self: self._condition,
    )
    def _get_json(self, key: tuple, compute: Callable[[], str]) -> str:
        return compute()

    def resolve(
        self, operation: str, key: tuple, schema: type[ResponseModel], compute: Callable[[], ResponseModel]
    ) -> ResponseModel:
        cache_hit = True

        def compute_json() -> str:
            nonlocal cache_hit
            cache_hit = False
            with trace_operation("cache_miss"):
                logger.info(
                    "cache_miss", operation=operation,
                    message="No saved result is available. Running the document workflow.",
                )
            return schema.model_validate(compute()).model_dump_json()

        result = self._get_json((operation, key), compute_json)
        if cache_hit:
            with trace_operation("cache_hit"):
                logger.info(
                    "cache_hit", operation=operation,
                    message="Reusing the saved document result.",
                )
        return schema.model_validate_json(result)
