from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import PrincipalDependency, current_principal, require_role
from app.db.base import get_session
from app.schemas import LinkCreate, LinkUpdate, LinkView
from app.services.links import LinkService

router = APIRouter(
    prefix="/api/v1/links", tags=["links"], dependencies=[Depends(current_principal)]
)
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("", response_model=LinkView, status_code=201)
async def create_link(
    payload: LinkCreate,
    session: Session,
    principal: PrincipalDependency,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)
    ] = None,
) -> LinkView:
    require_role(principal, "requester")
    return LinkView.model_validate(
        await LinkService(session, principal.actor).create(payload, idempotency_key)
    )


@router.get("", response_model=list[LinkView])
async def list_links(
    session: Session, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)
) -> list[LinkView]:
    return [
        LinkView.model_validate(item) for item in await LinkService(session).list(limit, offset)
    ]


@router.get("/{link_id}", response_model=LinkView)
async def get_link(link_id: UUID, session: Session) -> LinkView:
    return LinkView.model_validate(await LinkService(session).get(link_id))


@router.patch("/{link_id}", response_model=LinkView)
async def update_link(
    link_id: UUID, payload: LinkUpdate, session: Session, principal: PrincipalDependency
) -> LinkView:
    require_role(principal, "requester")
    return LinkView.model_validate(
        await LinkService(session, principal.actor).update(link_id, payload)
    )


@router.delete("/{link_id}", status_code=204)
async def delete_link(link_id: UUID, session: Session, principal: PrincipalDependency) -> Response:
    require_role(principal, "requester")
    await LinkService(session, principal.actor).delete(link_id)
    return Response(status_code=204)


@router.get("/{link_id}/analytics")
async def link_analytics(link_id: UUID, session: Session) -> dict:
    return await LinkService(session).analytics(link_id)


redirect_router = APIRouter(tags=["redirect"])


@redirect_router.get("/{alias}", include_in_schema=False)
async def redirect(
    alias: str,
    request: Request,
    session: Session,
    user_agent: Annotated[str | None, Header()] = None,
    referer: Annotated[str | None, Header()] = None,
) -> RedirectResponse:
    link = await LinkService(session).resolve(
        alias,
        request.state.correlation_id,
        request.client.host if request.client else None,
        referer,
        user_agent,
    )
    return RedirectResponse(link.target_url, status_code=link.redirect_type)
