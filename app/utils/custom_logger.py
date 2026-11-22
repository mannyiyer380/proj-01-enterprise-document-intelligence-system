"""Structured JSON logging for the document-intelligence application."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import structlog
from structlog.stdlib import BoundLogger


class DocumentIntelligenceLogger:
    """Create named JSON loggers that write to both a file and the console."""

    _configuration_lock = threading.Lock()
    _structlog_configured = False

    def __init__(
        self,
        log_dir: str | Path = "runtime_data/logs",
        level: int = logging.INFO,
        console: bool = True,
    ) -> None:
        self.logs_dir = Path(log_dir).expanduser().resolve()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        self.log_file_path = self.logs_dir / f"edis_{timestamp}.jsonl"
        self.level = level
        self.console = console
        self._managed_handlers: list[tuple[logging.Logger, logging.Handler]] = []

    @classmethod
    def _configure_structlog(cls) -> None:
        with cls._configuration_lock:
            if cls._structlog_configured:
                return

            structlog.configure(
                processors=[
                    structlog.contextvars.merge_contextvars,
                    structlog.stdlib.add_logger_name,
                    structlog.processors.add_log_level,
                    structlog.processors.TimeStamper(
                        fmt="iso", utc=True, key="timestamp"
                    ),
                    structlog.processors.StackInfoRenderer(),
                    structlog.processors.format_exc_info,
                    structlog.processors.EventRenamer(to="event"),
                    structlog.processors.JSONRenderer(),
                ],
                logger_factory=structlog.stdlib.LoggerFactory(),
                wrapper_class=structlog.stdlib.BoundLogger,
                cache_logger_on_first_use=True,
            )
            cls._structlog_configured = True

    def get_logger(self, name: str = "document_intelligence") -> BoundLogger:
        """Return a structured logger without adding duplicate handlers."""
        self._configure_structlog()
        logger_name = Path(name).name if name else "document_intelligence"
        stdlib_logger = logging.getLogger(logger_name)
        stdlib_logger.setLevel(self.level)
        stdlib_logger.propagate = False

        file_path = str(self.log_file_path)
        has_file_handler = any(
            getattr(handler, "_edis_file_path", None) == file_path
            for handler in stdlib_logger.handlers
        )
        if not has_file_handler:
            file_handler = logging.FileHandler(
                self.log_file_path, encoding="utf-8"
            )
            file_handler.setLevel(self.level)
            file_handler.setFormatter(logging.Formatter("%(message)s"))
            file_handler._edis_file_path = file_path  # type: ignore[attr-defined]
            stdlib_logger.addHandler(file_handler)
            self._managed_handlers.append((stdlib_logger, file_handler))

        has_console_handler = any(
            getattr(handler, "_edis_console", False)
            for handler in stdlib_logger.handlers
        )
        if self.console and not has_console_handler:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(self.level)
            console_handler.setFormatter(logging.Formatter("%(message)s"))
            console_handler._edis_console = True  # type: ignore[attr-defined]
            stdlib_logger.addHandler(console_handler)
            self._managed_handlers.append((stdlib_logger, console_handler))

        return structlog.get_logger(logger_name)

    def close(self) -> None:
        """Flush, detach, and close handlers created by this instance."""
        while self._managed_handlers:
            logger, handler = self._managed_handlers.pop()
            logger.removeHandler(handler)
            handler.flush()
            handler.close()

    def __enter__(self) -> "DocumentIntelligenceLogger":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
