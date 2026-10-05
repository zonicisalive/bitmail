"""
Bulk delete endpoint.

Rather than duplicating each resource's cascade logic (campaign queue cancellation,
.eml file removal, list membership cleanup), this loops over the existing
single-resource delete handlers so behaviour can never drift between the two paths.
"""

from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.routes.campaigns import delete_campaign
from app.routes.smtp import delete_smtp_config
from app.routes.storage import delete_stored_email
from app.routes.subscribers import delete_subscriber, delete_subscriber_list
from app.routes.templates import delete_template

router = APIRouter(prefix="/api", tags=["Bulk Operations"])

DELETERS = {
    "subscribers": delete_subscriber,
    "lists": delete_subscriber_list,
    "campaigns": delete_campaign,
    "templates": delete_template,
    "smtp": delete_smtp_config,
    "emails": delete_stored_email,
}

MAX_BULK_IDS = 5000


class BulkDeleteRequest(BaseModel):
    resource: str = Field(..., description="One of: " + ", ".join(DELETERS))
    ids: List[str] = Field(..., min_length=1)


@router.post("/bulk-delete")
async def bulk_delete(payload: BulkDeleteRequest):
    """Delete many records of one resource type in a single request."""
    deleter = DELETERS.get(payload.resource)
    if not deleter:
        raise HTTPException(status_code=400, detail=f"Unknown resource '{payload.resource}'")
    if len(payload.ids) > MAX_BULK_IDS:
        raise HTTPException(status_code=400, detail=f"Too many ids (max {MAX_BULK_IDS})")

    deleted = 0
    failed = []
    for item_id in dict.fromkeys(payload.ids):  # de-duplicate, keep order
        try:
            await deleter(item_id)
            deleted += 1
        except HTTPException as exc:
            failed.append({"id": item_id, "error": exc.detail})
        except Exception as exc:  # noqa: BLE001 - one bad row must not abort the batch
            failed.append({"id": item_id, "error": str(exc)})

    return {
        "success": True,
        "resource": payload.resource,
        "deleted": deleted,
        "failed_count": len(failed),
        "failed": failed,
        "message": f"Deleted {deleted} of {len(payload.ids)} {payload.resource}.",
    }
