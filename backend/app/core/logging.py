import logging
import sys
from uuid import uuid4

import structlog


def configure_logging(level: str) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def bind_request_context(value: str | None = None) -> str:
    request_id = value if value and len(value) <= 64 and value.isascii() else str(uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=request_id)
    return request_id


logger = structlog.get_logger("agentic-url")
