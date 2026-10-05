from typing import Any


class AppError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: list[dict[str, Any]] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.retryable = retryable


class NotFoundError(AppError):
    def __init__(self, resource: str) -> None:
        super().__init__(404, "NOT_FOUND", f"{resource} was not found")


class ConflictError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(409, "CONFLICT", message)


class DomainValidationError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(400, "VALIDATION_ERROR", message)


class ProviderError(AppError):
    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(503, "AI_PROVIDER_ERROR", message, retryable=retryable)


class PolicyViolationError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(422, "POLICY_VIOLATION", message, retryable=False)


class RateLimitError(AppError):
    def __init__(self, message: str = "Too many requests; retry shortly") -> None:
        super().__init__(429, "RATE_LIMITED", message, retryable=True)


class GoneError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(410, "GONE", message)
