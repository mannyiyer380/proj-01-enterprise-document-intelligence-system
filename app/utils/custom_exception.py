"""Project-specific exception with consistent source and traceback metadata."""

from __future__ import annotations

import sys
import traceback
from types import ModuleType, TracebackType
from typing import TypeAlias


ExceptionInfo: TypeAlias = tuple[
    type[BaseException] | None,
    BaseException | None,
    TracebackType | None,
]


class DocumentIntelligenceError(Exception):
    """Wrap an application error while retaining its most useful diagnostics.

    ``error_details`` may be the original exception, the :mod:`sys` module for
    compatibility with older call sites, or ``None`` to inspect the currently
    handled exception.
    """

    def __init__(
        self,
        error_message: object,
        error_details: BaseException | ModuleType | None = None,
    ) -> None:
        self.error_message = str(error_message)
        exc_type, exc_value, exc_tb = self._resolve_exception_info(error_details)
        self.original_exception = exc_value

        last_tb = exc_tb
        while last_tb is not None and last_tb.tb_next is not None:
            last_tb = last_tb.tb_next

        self.file_name = (
            last_tb.tb_frame.f_code.co_filename if last_tb is not None else "<unknown>"
        )
        self.lineno = last_tb.tb_lineno if last_tb is not None else -1
        self.traceback_str = (
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)).rstrip()
            if exc_type is not None and exc_tb is not None
            else ""
        )

        super().__init__(self.error_message)

    @staticmethod
    def _resolve_exception_info(
        error_details: BaseException | ModuleType | None,
    ) -> ExceptionInfo:
        if isinstance(error_details, BaseException):
            return (
                type(error_details),
                error_details,
                error_details.__traceback__,
            )

        if error_details is not None:
            exc_info = getattr(error_details, "exc_info", None)
            if callable(exc_info):
                details = exc_info()
                if isinstance(details, tuple) and len(details) == 3:
                    return details

        return sys.exc_info()

    def __str__(self) -> str:
        base = (
            f"Error in [{self.file_name}] at line [{self.lineno}] | "
            f"Message: {self.error_message}"
        )
        return f"{base}\nTraceback:\n{self.traceback_str}" if self.traceback_str else base

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(file={self.file_name!r}, "
            f"line={self.lineno}, message={self.error_message!r})"
        )
