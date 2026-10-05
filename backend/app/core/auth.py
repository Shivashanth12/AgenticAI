import hashlib
import hmac
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Header

from app.core.config import get_settings
from app.core.errors import AppError

Role = Literal["requester", "reviewer"]


@dataclass(frozen=True)
class Principal:
    actor: str
    role: Role


def _match(candidate: str, expected: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode()).digest(), hashlib.sha256(expected.encode()).digest()
    )


async def current_principal(
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise AppError(401, "UNAUTHENTICATED", "A bearer token is required")
    token = authorization.removeprefix("Bearer ").strip()
    settings = get_settings()
    if token and _match(token, settings.requester_token):
        return Principal("local-requester", "requester")
    if token and _match(token, settings.reviewer_token):
        return Principal("local-reviewer", "reviewer")
    raise AppError(401, "UNAUTHENTICATED", "The bearer token is invalid")


PrincipalDependency = Annotated[Principal, Depends(current_principal)]


def require_role(principal: Principal, role: Role) -> None:
    if principal.role != role:
        raise AppError(403, "FORBIDDEN", f"The {role} role is required")
