"""
Webhooks Management Router.
Provides CRUD endpoints for registering, testing, and monitoring outbound webhooks.
"""

import json
import secrets
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status

from app.db import get_db, utc_now_iso
from app.models import (
    WebhookCreate,
    WebhookDeliveryResponse,
    WebhookResponse,
    WebhookTestResponse,
    WebhookUpdate,
)
from app.webhooks import WebhookDispatcher

router = APIRouter(prefix="/api/webhooks", tags=["Webhooks"])


@router.get("", response_model=List[WebhookResponse])
async def list_webhooks() -> Any:
    """List all configured outbound webhooks."""
    async with get_db() as db:
        async with db.execute("""
            SELECT id, name, url, secret, events_json, is_active, created_at, updated_at
            FROM webhooks
            ORDER BY created_at DESC
        """) as cur:
            rows = await cur.fetchall()
            result = []
            for r in rows:
                events = []
                try:
                    events = json.loads(r["events_json"]) if r["events_json"] else []
                except Exception:
                    events = []
                result.append(
                    WebhookResponse(
                        id=r["id"],
                        name=r["name"],
                        url=r["url"],
                        secret=r["secret"],
                        events=events,
                        is_active=bool(r["is_active"]),
                        created_at=r["created_at"],
                        updated_at=r["updated_at"]
                    )
                )
            return result


@router.post("", response_model=WebhookResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook(payload: WebhookCreate) -> Any:
    """Register a new outbound webhook with auto-generated secret if not provided."""
    name = payload.name.strip()
    url = payload.url.strip()
    if not name or not url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Webhook name and URL are required."
        )

    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook URL must begin with http:// or https://"
        )

    webhook_id = f"whk_{uuid.uuid4().hex[:12]}"
    secret = payload.secret.strip() if payload.secret and payload.secret.strip() else f"whsec_{secrets.token_hex(24)}"
    events_json = json.dumps(payload.events or ["*"])
    now = utc_now_iso()

    async with get_db() as db:
        await db.execute("""
            INSERT INTO webhooks (id, name, url, secret, events_json, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            webhook_id,
            name,
            url,
            secret,
            events_json,
            1 if payload.is_active else 0,
            now,
            now
        ))
        await db.commit()

    return WebhookResponse(
        id=webhook_id,
        name=name,
        url=url,
        secret=secret,
        events=payload.events or ["*"],
        is_active=payload.is_active,
        created_at=now,
        updated_at=now
    )


@router.get("/{webhook_id}", response_model=Dict[str, Any])
async def get_webhook_detail(webhook_id: str) -> Any:
    """Get webhook details and its recent 25 delivery history records."""
    async with get_db() as db:
        async with db.execute("""
            SELECT id, name, url, secret, events_json, is_active, created_at, updated_at
            FROM webhooks
            WHERE id = ?
        """, (webhook_id,)) as cur:
            wh_row = await cur.fetchone()

        if not wh_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Webhook '{webhook_id}' not found."
            )

        events = []
        try:
            events = json.loads(wh_row["events_json"]) if wh_row["events_json"] else []
        except Exception:
            events = []

        async with db.execute("""
            SELECT id, webhook_id, event_type, status_code, response_body, success, created_at
            FROM webhook_deliveries
            WHERE webhook_id = ?
            ORDER BY created_at DESC
            LIMIT 25
        """, (webhook_id,)) as d_cur:
            d_rows = await d_cur.fetchall()
            deliveries = [
                {
                    "id": d["id"],
                    "event_type": d["event_type"],
                    "status_code": d["status_code"],
                    "response_body": d["response_body"],
                    "success": bool(d["success"]),
                    "created_at": d["created_at"]
                }
                for d in d_rows
            ]

        return {
            "webhook": {
                "id": wh_row["id"],
                "name": wh_row["name"],
                "url": wh_row["url"],
                "secret": wh_row["secret"],
                "events": events,
                "is_active": bool(wh_row["is_active"]),
                "created_at": wh_row["created_at"],
                "updated_at": wh_row["updated_at"]
            },
            "recent_deliveries": deliveries
        }


@router.put("/{webhook_id}", response_model=WebhookResponse)
async def update_webhook(webhook_id: str, payload: WebhookUpdate) -> Any:
    """Update webhook properties."""
    async with get_db() as db:
        async with db.execute("""
            SELECT id, name, url, secret, events_json, is_active, created_at, updated_at
            FROM webhooks
            WHERE id = ?
        """, (webhook_id,)) as cur:
            existing = await cur.fetchone()

        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Webhook '{webhook_id}' not found."
            )

        new_name = payload.name.strip() if payload.name is not None else existing["name"]
        new_url = payload.url.strip() if payload.url is not None else existing["url"]
        new_secret = payload.secret.strip() if payload.secret is not None else existing["secret"]
        new_events = payload.events if payload.events is not None else (
            json.loads(existing["events_json"]) if existing["events_json"] else []
        )
        new_is_active = payload.is_active if payload.is_active is not None else bool(existing["is_active"])
        now = utc_now_iso()

        await db.execute("""
            UPDATE webhooks
            SET name = ?, url = ?, secret = ?, events_json = ?, is_active = ?, updated_at = ?
            WHERE id = ?
        """, (
            new_name,
            new_url,
            new_secret,
            json.dumps(new_events),
            1 if new_is_active else 0,
            now,
            webhook_id
        ))
        await db.commit()

        return WebhookResponse(
            id=webhook_id,
            name=new_name,
            url=new_url,
            secret=new_secret,
            events=new_events,
            is_active=new_is_active,
            created_at=existing["created_at"],
            updated_at=now
        )


@router.delete("/{webhook_id}")
async def delete_webhook(webhook_id: str) -> Dict[str, Any]:
    """Delete a webhook and its associated delivery history."""
    async with get_db() as db:
        async with db.execute("SELECT id FROM webhooks WHERE id = ?", (webhook_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Webhook '{webhook_id}' not found."
            )

        await db.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
        await db.commit()

    return {"status": "success", "deleted": True, "webhook_id": webhook_id}


@router.post("/{webhook_id}/test", response_model=WebhookTestResponse)
async def test_webhook_endpoint(webhook_id: str) -> Any:
    """Send an immediate signed ping test event to the specified webhook endpoint."""
    try:
        res = await WebhookDispatcher.test_webhook(webhook_id)
        return WebhookTestResponse(
            success=res["success"],
            status_code=res.get("status_code"),
            response_body=res.get("response_body"),
            delivery_id=res["delivery_id"],
            latency_ms=res.get("latency_ms", 0.0)
        )
    except ValueError as val_err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(val_err)
        )
    except Exception as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Webhook delivery failed: {err}"
        )
