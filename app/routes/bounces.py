"""
Inbound Bounce & Feedback Loop (FBL) Router.
Receives bounce webhook reports from MTAs/ISPs or parses raw DSN reports,
automatically suppressing invalid recipients and updating campaign stats.
"""

import hmac
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth import current_user_id, get_current_user, get_current_user_optional
from app.bounce import BounceClassifier
from app.config import settings
from app.db import get_db
from app.models import BounceType, InboundBouncePayload, InboundBounceResponse

logger = logging.getLogger("bitmail.bounces")

router = APIRouter(prefix="/api/bounces", tags=["Bounces & Deliverability"])


async def authorize_bounce_report(request: Request) -> Optional[str]:
    """
    Who may report a bounce. A mail server presenting BOUNCE_WEBHOOK_SECRET may report
    for any account (returns None). A signed-in user may report only for mail their own
    account sent (returns their id). Anyone else gets 401, so strangers cannot get
    addresses suppressed by posting fake bounces.
    """
    secret = settings.BOUNCE_WEBHOOK_SECRET
    presented = request.headers.get("X-Bounce-Secret") or request.query_params.get("secret") or ""
    if secret and presented and hmac.compare_digest(presented.encode(), secret.encode()):
        return None
    user = await get_current_user_optional(request)
    if user:
        return user["id"]
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Bounce reports need the X-Bounce-Secret header or a signed-in session.",
    )


@router.post("/inbound", response_model=InboundBounceResponse)
async def handle_inbound_bounce(
    payload: InboundBouncePayload, only_user_id: Optional[str] = Depends(authorize_bounce_report)
) -> Any:
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
        message_id=payload.message_id,
        only_user_id=only_user_id
    )


@router.post("/inbound-raw", response_model=InboundBounceResponse)
async def handle_inbound_raw_dsn(
    request: Request, only_user_id: Optional[str] = Depends(authorize_bounce_report)
) -> Any:
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
        diagnostic_code=parsed.get("diagnostic_code"),
        only_user_id=only_user_id
    )


# Brevo transactional webhook events -> bounce type. Anything else (delivered,
# opened, clicked, deferred-then-delivered...) is acknowledged and ignored.
BREVO_EVENTS = {
    # keys are lowercase letters only, so "hard_bounce", "hardBounce" and "Hard Bounced" all match
    "hardbounce": BounceType.HARD, "hardbounced": BounceType.HARD,
    "invalidemail": BounceType.HARD, "invalid": BounceType.HARD,
    "blocked": BounceType.HARD,  # Brevo refuses the address (it bounced or complained before)
    "spam": BounceType.COMPLAINT, "complaint": BounceType.COMPLAINT,
    "softbounce": BounceType.SOFT, "softbounced": BounceType.SOFT,
}


def _letters(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalpha())


def _parse_brevo(event: Dict[str, Any]):
    # Classic webhooks say "hard_bounce"; the newer builder may say "hardBounce" or "Hard Bounced".
    bounce_type = BREVO_EVENTS.get(_letters(event.get("event")))
    reason = event.get("reason") or f"Brevo: {event.get('event')}"
    return event.get("email"), bounce_type, reason, event.get("message-id")


def _parse_mailjet(event: Dict[str, Any]):
    # https://dev.mailjet.com/email/guides/webhooks/ - "bounce" carries hard_bounce true/false;
    # "blocked" is only about the address when error_related_to is "recipient".
    kind = _letters(event.get("event"))
    reason = f"Mailjet: {event.get('event')} {event.get('error_related_to') or ''} {event.get('error') or ''}".strip()
    if kind == "bounce":
        bounce_type = BounceType.HARD if event.get("hard_bounce") in (True, "true", 1) else BounceType.SOFT
    elif kind == "blocked" and event.get("error_related_to") == "recipient":
        bounce_type = BounceType.HARD
    elif kind == "spam":
        bounce_type = BounceType.COMPLAINT
    else:
        bounce_type = None
    return event.get("email"), bounce_type, reason, None


def _parse_resend(event: Dict[str, Any]):
    # https://resend.com/docs/dashboard/webhooks/event-types - data.to is a list of addresses.
    data = event.get("data") or {}
    to = data.get("to") or []
    email = to[0] if isinstance(to, list) and to else to
    kind = event.get("type")
    bounce = data.get("bounce") or {}
    if kind == "email.bounced":
        bounce_type = BounceType.HARD if bounce.get("type") == "Permanent" else BounceType.SOFT
    elif kind == "email.complained":
        bounce_type = BounceType.COMPLAINT
    else:
        bounce_type = None
    reason = bounce.get("message") or f"Resend: {kind}"
    return email, bounce_type, reason, None


async def _process_provider_events(request: Request, provider: str, parse, only_user_id: Optional[str]) -> Dict[str, Any]:
    """
    Relay providers post one event or a list. Bounces and spam complaints suppress the
    address for the account that sent the email; soft bounces are only logged.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Expected a JSON body.")
    events = [e for e in (body if isinstance(body, list) else [body]) if isinstance(e, dict)]
    # Keys only (no addresses): lets us see the payload shape the provider actually sends.
    logger.info("%s webhook: %d event(s): %s", provider, len(events),
                [(e.get("event") or e.get("type"), sorted(e)) for e in events][:5])

    results = []
    for event in events:
        email, bounce_type, reason, message_id = parse(event)
        email = str(email or "").strip()
        if not bounce_type or not email:
            continue
        common = dict(recipient_email=email, bounce_type=bounce_type, reason=reason, only_user_id=only_user_id)
        res = await BounceClassifier.process_bounce(message_id=message_id or None, **common)
        if not res.suppressed and message_id and bounce_type != BounceType.SOFT:
            # The provider may report its own Message-ID; fall back to the latest email to that address.
            res = await BounceClassifier.process_bounce(**common)
        results.append({"email": email, "event": event.get("event") or event.get("type"), "suppressed": res.suppressed})

    return {"success": True, "processed": len(results), "results": results}


@router.post("/brevo")
async def handle_brevo_webhook(
    request: Request, only_user_id: Optional[str] = Depends(authorize_bounce_report)
) -> Dict[str, Any]:
    """Brevo transactional webhook (single event, or a list when "batched" is on)."""
    return await _process_provider_events(request, "Brevo", _parse_brevo, only_user_id)


@router.post("/mailjet")
async def handle_mailjet_webhook(
    request: Request, only_user_id: Optional[str] = Depends(authorize_bounce_report)
) -> Dict[str, Any]:
    """Mailjet event webhook (single event, or a list when grouping is on)."""
    return await _process_provider_events(request, "Mailjet", _parse_mailjet, only_user_id)


@router.post("/resend")
async def handle_resend_webhook(
    request: Request, only_user_id: Optional[str] = Depends(authorize_bounce_report)
) -> Dict[str, Any]:
    """Resend webhook (email.bounced / email.complained)."""
    return await _process_provider_events(request, "Resend", _parse_resend, only_user_id)


@router.get("/stats", dependencies=[Depends(get_current_user)])
async def get_bounce_stats() -> Dict[str, Any]:
    """Summary of bounces and suppressions across campaigns."""
    uid = current_user_id()
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM suppressions WHERE user_id = ?", (uid,)) as cur:
            row = await cur.fetchone()
            total_suppressions = row[0] if row else 0

        async with db.execute("SELECT COUNT(*) FROM sent_emails WHERE status = 'bounced' AND user_id = ?", (uid,)) as cur:
            row = await cur.fetchone()
            total_bounced_sends = row[0] if row else 0

        async with db.execute("""
            SELECT reason, COUNT(*) as cnt
            FROM suppressions
            WHERE user_id = ?
            GROUP BY reason
            ORDER BY cnt DESC
            LIMIT 10
        """, (uid,)) as cur:
            breakdown = [dict(r) for r in await cur.fetchall()]

    return {
        "total_suppressed": total_suppressions,
        "total_bounced_sends": total_bounced_sends,
        "reasons_breakdown": breakdown
    }


@router.get("/suppressions", dependencies=[Depends(get_current_user)])
async def list_suppressions() -> Dict[str, Any]:
    """Retrieve all suppressed email addresses and reasons."""
    async with get_db() as db:
        async with db.execute("SELECT id, email, campaign_id, reason, created_at FROM suppressions WHERE user_id = ? ORDER BY created_at DESC LIMIT 200",
            (current_user_id(),)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return {"suppressions": rows, "count": len(rows)}


@router.delete("/suppressions/{email}", dependencies=[Depends(get_current_user)])
async def unsuppress_email(email: str) -> Dict[str, Any]:
    """Remove an email address from the suppressions table."""
    clean = email.strip().lower()
    async with get_db() as db:
        await db.execute("DELETE FROM suppressions WHERE email = ? COLLATE NOCASE AND user_id = ?", (clean, current_user_id()))
        await db.commit()
    return {"success": True, "message": f"Address '{clean}' unsuppressed successfully."}


@router.post("/suppressions/sync", dependencies=[Depends(get_current_user)])
async def sync_active_subscribers_suppressions() -> Dict[str, Any]:
    """Remove active subscribers from suppressions so their sends are never blocked."""
    async with get_db() as db:
        await db.execute("""
            DELETE FROM suppressions
            WHERE user_id = ?1
              AND email IN (SELECT email FROM subscribers WHERE status = 'active' AND user_id = ?1)
        """, (current_user_id(),))
        await db.commit()
    return {"success": True, "message": "Synchronized suppressions with active subscribers."}
