"""
Inbound Bounce & Feedback Loop (FBL) Router.
Receives bounce webhook reports from MTAs/ISPs or parses raw DSN reports,
automatically suppressing invalid recipients and updating campaign stats.
"""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, status

from app.bounce import BounceClassifier
from app.db import get_db
from app.models import InboundBouncePayload, InboundBounceResponse

router = APIRouter(prefix="/api/bounces", tags=["Bounces & Deliverability"])


@router.post("/inbound", response_model=InboundBounceResponse)
async def handle_inbound_bounce(payload: InboundBouncePayload) -> Any:
    """
    Ingest a structured bounce report (e.g. from Postfix, Sendgrid, SES, or custom MTA).
    Automatically suppresses hard bounces and complaints.
    """
    recipient = payload.recipient_email.strip()
    if not recipient:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Recipient email is required."
        )

    # Classify bounce type if hint or status code provided
    bounce_type, default_reason = BounceClassifier.classify(
        status_code=payload.status_code,
        diagnostic=payload.diagnostic_code,
        raw_text=payload.raw_dsn,
        type_hint=payload.bounce_type.value if payload.bounce_type else None
    )

    reason = payload.reason or payload.diagnostic_code or default_reason

    return await BounceClassifier.process_bounce(
        recipient_email=recipient,
        bounce_type=bounce_type,
        reason=reason,
        status_code=payload.status_code,
        diagnostic_code=payload.diagnostic_code,
        campaign_id=payload.campaign_id,
        message_id=payload.message_id
    )


@router.post("/inbound-raw", response_model=InboundBounceResponse)
async def handle_inbound_raw_dsn(request: Request) -> Any:
    """
    Ingest a raw RFC 3464 DSN or RFC 5965 ARF complaint MIME email.
    Parses headers and multipart attachments to identify recipient and failure code.
    """
    body_bytes = await request.body()
    raw_str = body_bytes.decode("utf-8", errors="ignore")
    if not raw_str.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty DSN message body."
        )

    parsed = BounceClassifier.parse_raw_dsn(raw_str)
    recipient = parsed.get("recipient_email")
    if not recipient:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Could not extract recipient address from DSN report."
        )

    bounce_type, reason = BounceClassifier.classify(
        status_code=parsed.get("status_code"),
        diagnostic=parsed.get("diagnostic_code"),
        raw_text=raw_str
    )

    return await BounceClassifier.process_bounce(
        recipient_email=recipient,
        bounce_type=bounce_type,
        reason=reason,
        status_code=parsed.get("status_code"),
        diagnostic_code=parsed.get("diagnostic_code")
    )


@router.get("/stats")
async def get_bounce_stats() -> Dict[str, Any]:
    """Summary of bounces and suppressions across campaigns."""
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM suppressions") as cur:
            row = await cur.fetchone()
            total_suppressions = row[0] if row else 0

        async with db.execute("SELECT COUNT(*) FROM sent_emails WHERE status = 'bounced'") as cur:
            row = await cur.fetchone()
            total_bounced_sends = row[0] if row else 0

        async with db.execute("""
            SELECT reason, COUNT(*) as cnt
            FROM suppressions
            GROUP BY reason
            ORDER BY cnt DESC
            LIMIT 10
        """) as cur:
            breakdown = [dict(r) for r in await cur.fetchall()]

    return {
        "total_suppressed": total_suppressions,
        "total_bounced_sends": total_bounced_sends,
        "reasons_breakdown": breakdown
    }
