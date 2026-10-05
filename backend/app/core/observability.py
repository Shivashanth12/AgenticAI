import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from app.core.errors import AppError
from app.core.logging import logger

P = ParamSpec("P")
R = TypeVar("R")


def observed(operation: str) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """AOP-style boundary for consistent service logging and timing."""

    def decorator(function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(function)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            started = time.perf_counter()
            logger.info(f"{operation}.started")
            try:
                result = await function(*args, **kwargs)
            except Exception as exc:
                logger.error(
                    f"{operation}.failed",
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    error_type=type(exc).__name__,
                    error_code=exc.code if isinstance(exc, AppError) else "UNEXPECTED_ERROR",
                    retryable=exc.retryable if isinstance(exc, AppError) else False,
                )
                raise
            logger.info(
                f"{operation}.completed",
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            return result

        return wrapper

    return decorator
