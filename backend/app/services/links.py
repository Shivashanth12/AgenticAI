import hashlib
import json
import secrets
import string
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.contextvars import get_contextvars

from app.core.cache import allow_request, cache_delete, cache_get, cache_set
from app.core.config import get_settings
from app.core.errors import (
    ConflictError,
    DomainValidationError,
    GoneError,
    NotFoundError,
    RateLimitError,
)
from app.core.logging import logger
from app.core.observability import observed
from app.db.models import AuditEvent, ClickEvent, LinkIdempotency, LinkStatus, ShortLink
from app.schemas import LinkCreate, LinkUpdate

ALPHABET = string.ascii_letters + string.digits
RESERVED = {"api", "docs", "health", "metrics", "redoc", "openapi.json"}


class LinkService:
    def __init__(self, session: AsyncSession, actor: str = "visitor") -> None:
        self.session = session
        self.actor = actor

    @observed("link.create")
    async def create(self, payload: LinkCreate, idempotency_key: str | None = None) -> ShortLink:
        request_hash = hashlib.sha256(
            json.dumps(payload.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        if idempotency_key:
            prior = await self.session.get(LinkIdempotency, idempotency_key)
            if prior:
                if prior.request_hash != request_hash:
                    raise ConflictError("This idempotency key was already used for another request")
                existing_link = await self.session.get(ShortLink, prior.link_id)
                if existing_link:
                    return existing_link
        alias = payload.custom_alias or "".join(secrets.choice(ALPHABET) for _ in range(8))
        if alias.lower() in RESERVED:
            raise DomainValidationError("The requested alias is reserved")
        existing = await self.session.scalar(
            select(ShortLink).where(func.lower(ShortLink.alias) == alias.lower())
        )
        if existing:
            raise ConflictError("Alias is already in use")
        target = str(payload.target_url)
        if payload.expires_at and payload.expires_at <= datetime.now(UTC):
            raise DomainValidationError("Expiration must be in the future")
        link = ShortLink(
            alias=alias,
            target_url=target,
            title=payload.title,
            expires_at=payload.expires_at,
            redirect_type=payload.redirect_type,
        )
        try:
            self.session.add(link)
            await self.session.flush()
            if idempotency_key:
                self.session.add(
                    LinkIdempotency(
                        key=idempotency_key,
                        request_hash=request_hash,
                        link_id=link.id,
                    )
                )
            self._audit("link.created", link, "success")
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            if idempotency_key:
                prior = await self.session.get(LinkIdempotency, idempotency_key)
                if prior and prior.request_hash == request_hash:
                    return await self.session.get(ShortLink, prior.link_id)
            raise ConflictError("Alias or idempotency key is already in use") from None
        await self.session.refresh(link)
        await cache_delete(f"link:{link.alias.lower()}")
        logger.info("link.created", link_id=str(link.id), alias=alias)
        return link

    @observed("link.list")
    async def list(self, limit: int = 50, offset: int = 0) -> list[ShortLink]:
        result = await self.session.scalars(
            select(ShortLink)
            .where(ShortLink.status != LinkStatus.DELETED)
            .order_by(ShortLink.created_at.desc())
            .limit(min(limit, 100))
            .offset(offset)
        )
        return list(result)

    @observed("link.get")
    async def get(self, link_id: UUID) -> ShortLink:
        link = await self.session.get(ShortLink, link_id)
        if not link or link.status == LinkStatus.DELETED:
            raise NotFoundError("Link")
        return link

    @observed("link.update")
    async def update(self, link_id: UUID, payload: LinkUpdate) -> ShortLink:
        link = await self.get(link_id)
        changes = payload.model_dump(exclude_unset=True, exclude={"version"})
        if "status" in changes:
            changes["status"] = LinkStatus(changes["status"])
        result = await self.session.execute(
            update(ShortLink)
            .where(
                ShortLink.id == link_id,
                ShortLink.version == payload.version,
                ShortLink.status != LinkStatus.DELETED,
            )
            .values(**changes, version=ShortLink.version + 1)
        )
        if result.rowcount != 1:
            await self.session.rollback()
            raise ConflictError("Link was changed by another request; refresh and retry")
        await self.session.refresh(link)
        self._audit("link.updated", link, "success", {"fields": sorted(changes)})
        await self.session.commit()
        await self.session.refresh(link)
        await cache_delete(f"link:{link.alias.lower()}")
        logger.info("link.updated", link_id=str(link.id), fields=list(changes))
        return link

    @observed("link.delete")
    async def delete(self, link_id: UUID) -> None:
        link = await self.get(link_id)
        await self.session.execute(
            update(ShortLink)
            .where(
                ShortLink.id == link_id,
            )
            .values(status=LinkStatus.DELETED, version=ShortLink.version + 1)
        )
        await self.session.refresh(link)
        self._audit("link.deleted", link, "success")
        await self.session.commit()
        await cache_delete(f"link:{link.alias.lower()}")
        logger.info("link.deleted", link_id=str(link.id))

    @observed("link.resolve")
    async def resolve(
        self,
        alias: str,
        request_id: str,
        client_ip: str | None,
        referrer: str | None,
        user_agent: str | None,
    ) -> ShortLink:
        settings = get_settings()
        rate_key = hashlib.sha256(f"{client_ip or 'unknown'}:{alias.lower()}".encode()).hexdigest()
        if not await allow_request(f"redirect-rate:{rate_key}", settings.redirect_rate_limit):
            raise RateLimitError()
        cached = await cache_get(f"link:{alias.lower()}")
        link = (
            await self.session.get(ShortLink, UUID(cached["id"]))
            if cached and cached.get("id")
            else None
        )
        if not link:
            link = await self.session.scalar(
                select(ShortLink).where(func.lower(ShortLink.alias) == alias.lower())
            )
        now = datetime.now(UTC)
        if not link or link.status != LinkStatus.ACTIVE:
            raise NotFoundError("Link")
        if link.expires_at and link.expires_at.replace(tzinfo=UTC) <= now:
            raise GoneError("This link has expired")
        await cache_set(
            f"link:{alias.lower()}",
            {"id": str(link.id)},
            settings.redirect_cache_seconds,
        )
        event = ClickEvent(
            link_id=link.id,
            visitor_hash=hashlib.sha256((client_ip or "").encode()).hexdigest()
            if client_ip
            else None,
            referrer_domain=urlparse(referrer).hostname if referrer else None,
            user_agent_class=hashlib.sha256((user_agent or "unknown").encode()).hexdigest()[:16],
            request_id=request_id,
        )
        await self.session.execute(
            update(ShortLink)
            .where(
                ShortLink.id == link.id,
            )
            .values(click_count=ShortLink.click_count + 1)
        )
        self.session.add(event)
        self._audit("link.redirected", link, "success")
        await self.session.commit()
        logger.info("link.redirected", link_id=str(link.id), alias=alias)
        return link

    @observed("link.analytics")
    async def analytics(self, link_id: UUID) -> dict:
        link = await self.get(link_id)
        rows = await self.session.execute(
            select(func.date(ClickEvent.occurred_at), func.count(ClickEvent.id))
            .where(ClickEvent.link_id == link_id)
            .group_by(func.date(ClickEvent.occurred_at))
            .order_by(func.date(ClickEvent.occurred_at))
        )
        return {
            "link_id": str(link.id),
            "total_clicks": link.click_count,
            "daily": [{"date": str(day), "clicks": count} for day, count in rows],
        }

    def _audit(
        self,
        action: str,
        link: ShortLink,
        outcome: str,
        detail: dict | None = None,
    ) -> None:
        self.session.add(
            AuditEvent(
                actor=self.actor,
                action=action,
                resource_type="link",
                resource_id=str(link.id),
                outcome=outcome,
                detail={
                    "alias": link.alias,
                    "title": link.title,
                    "status": (
                        link.status.value
                        if isinstance(link.status, LinkStatus)
                        else str(link.status)
                    ),
                    **(detail or {}),
                },
                correlation_id=str(get_contextvars().get("correlation_id", "request")),
            )
        )
