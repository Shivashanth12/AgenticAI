from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_principal
from app.db.base import get_session
from app.db.models import AuditEvent, ShortLink, WorkflowRun

router = APIRouter(
    prefix="/api/v1/activity", tags=["activity"], dependencies=[Depends(current_principal)]
)
Session = Annotated[AsyncSession, Depends(get_session)]

ACTION_LABELS = {
    "link.created": "Link created",
    "link.updated": "Link updated",
    "link.deleted": "Link deleted",
    "link.redirected": "Short URL opened",
    "workflow.created": "Workflow started",
    "workflow.safe_stopped": "Workflow stopped safely",
    "task.completed": "Workflow step completed",
    "approval.requested": "Review requested",
    "approval.decided": "Review decision recorded",
}


def parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except (ValueError, TypeError):
        return None


def link_description(action: str, label: str, alias: str, fields: list[str]) -> str:
    short_name = f"/{alias}" if alias else "the short URL"
    if action == "link.created":
        return f"Created {label} with short URL {short_name}."
    if action == "link.updated":
        changes = ", ".join(field.replace("_", " ") for field in fields)
        return f"Updated {label} ({short_name}){f': {changes}' if changes else ''}."
    if action == "link.deleted":
        return f"Deleted {label} ({short_name}); the short URL no longer works."
    if action == "link.redirected":
        return f"A visitor opened {label} ({short_name}) and was redirected."
    return f"Recorded an operation for {label} ({short_name})."


@router.get("")
async def list_activity(
    session: Session,
    limit: int = Query(100, ge=1, le=250),
) -> list[dict]:
    events = list(
        await session.scalars(
            select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)
        )
    )
    link_ids = {
        value
        for event in events
        if event.resource_type == "link"
        if (value := parse_uuid(event.resource_id)) is not None
    }
    run_ids = {event.run_id for event in events if event.run_id is not None}
    links = (
        {
            link.id: link
            for link in await session.scalars(select(ShortLink).where(ShortLink.id.in_(link_ids)))
        }
        if link_ids
        else {}
    )
    runs = (
        {
            run.id: run
            for run in await session.scalars(select(WorkflowRun).where(WorkflowRun.id.in_(run_ids)))
        }
        if run_ids
        else {}
    )

    response = []
    for event in events:
        detail = event.detail or {}
        resource_label = event.resource_type.replace("_", " ").title()
        description = f"{ACTION_LABELS.get(event.action, event.action.replace('.', ' ').title())}."
        destination_url = None
        alias = ""

        resource_uuid = parse_uuid(event.resource_id)
        link = links.get(resource_uuid) if resource_uuid else None
        if event.resource_type == "link":
            alias = str(detail.get("alias") or (link.alias if link else ""))
            title = detail.get("title") or (link.title if link else None)
            resource_label = str(title or alias or "Deleted link")
            destination_url = detail.get("target_url") or (link.target_url if link else None)
            description = link_description(
                event.action,
                resource_label,
                alias,
                list(detail.get("fields") or []),
            )
        elif event.run_id and (run := runs.get(event.run_id)):
            resource_label = f"{run.scenario.title()} workflow"
            requirement = " ".join(run.requirement.split())
            if len(requirement) > 140:
                requirement = f"{requirement[:137]}..."
            description = (
                f"{ACTION_LABELS.get(event.action, event.action.replace('.', ' ').title())}: "
                f"{requirement}"
            )

        response.append(
            {
                "id": str(event.id),
                "action": event.action,
                "action_label": ACTION_LABELS.get(
                    event.action, event.action.replace(".", " ").title()
                ),
                "resource_type": event.resource_type,
                "resource_id": event.resource_id,
                "resource_label": resource_label,
                "description": description,
                "alias": alias or None,
                "destination_url": destination_url,
                "actor": event.actor,
                "outcome": event.outcome,
                "detail": detail,
                "correlation_id": event.correlation_id,
                "created_at": event.created_at,
            }
        )
    return response
