"""
Transactional Email API endpoints for instant single-send delivery, merge variables,
and Email Storage Vault archiving.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status

from app.models import (
    BatchSendRequest,
    BatchSendResponse,
    EmailStatus,
    TransactionalSendRequest,
    TransactionalSendResponse,
)
from app.sender import send_single_email
from app.websocket import emit_event

router = APIRouter(tags=["Transactional API"])


@router.post("/api/v1/transactional/send", response_model=TransactionalSendResponse, status_code=status.HTTP_200_OK)
@router.post("/api/v1/send", response_model=TransactionalSendResponse, status_code=status.HTTP_200_OK)
async def send_transactional_email(payload: TransactionalSendRequest):
    """
    Send a high-priority transactional email immediately.
    Interpolates merge variables, injects open/click tracking if requested,
    archives raw MIME EML in the Storage Vault, and returns dispatch status.
    """
    result = await send_single_email(
        recipient_email=payload.recipient_email,
        recipient_name=payload.recipient_name,
        subject=payload.subject,
        body_html=payload.body_html,
        body_text=payload.body_text,
        sender_email=payload.sender_email,
        sender_name=payload.sender_name,
        reply_to=payload.reply_to,
        template_id=payload.template_id,
        smtp_config_id=payload.smtp_config_id,
        merge_variables=payload.merge_variables,
        custom_headers=payload.headers,
        track_opens=payload.track_opens,
        track_clicks=payload.track_clicks
    )

    await emit_event("email_dispatched", {
        "campaign_id": None,
        "recipient": payload.recipient_email,
        "recipient_name": payload.recipient_name or "Transactional Recipient",
        "storage_id": result["sent_email_id"],
        "subject": payload.subject,
        "status": result["status"],
        "error": result.get("error"),
        "sent_count": 1,
        "total": 1,
        "progress_percent": 100
    })

    return TransactionalSendResponse(
        success=result["success"],
        sent_email_id=result["sent_email_id"],
        message_id=result.get("message_id"),
        status=EmailStatus(result["status"]),
        sent_at=result.get("sent_at"),
        error=result.get("error")
    )


@router.post("/api/v1/transactional/batch", response_model=BatchSendResponse)
@router.post("/api/v1/batch", response_model=BatchSendResponse)
async def send_transactional_batch(payload: BatchSendRequest):
    """
    Send a batch of transactional messages to multiple recipients with individualized merge variables.
    """
    batch_ids: List[str] = []

    for item in payload.recipients:
        merged = item.merge_variables.copy()
        if item.name:
            merged.setdefault("name", item.name)
            merged.setdefault("first_name", item.name.split()[0])

        res = await send_single_email(
            recipient_email=item.email,
            recipient_name=item.name,
            subject=payload.subject,
            body_html=payload.body_html,
            body_text=payload.body_text,
            sender_email=payload.sender_email,
            sender_name=payload.sender_name,
            reply_to=payload.reply_to,
            template_id=payload.template_id,
            smtp_config_id=payload.smtp_config_id,
            merge_variables=merged,
            track_opens=payload.track_opens,
            track_clicks=payload.track_clicks
        )
        batch_ids.append(res["sent_email_id"])

    return BatchSendResponse(
        total_queued=len(batch_ids),
        batch_ids=batch_ids,
        campaign_id=None
    )
