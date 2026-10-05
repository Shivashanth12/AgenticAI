from fastapi import APIRouter

from app.core.auth import PrincipalDependency

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.get("/me")
async def me(principal: PrincipalDependency) -> dict[str, str]:
    return {"actor": principal.actor, "role": principal.role}
