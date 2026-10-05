"""
Tracking Endpoints for Open Pixel Beacon, Click Link Redirector, and 1-Click Unsubscribe.
"""

import base64
import json
import urllib.parse
import uuid
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.config import settings
from app.db import get_db, utc_now_iso
from app.models import EventType, SubscriberStatus
from app.sender import verify_unsubscribe_token
from app.template_engine import template_engine
from app.websocket import emit_event

router = APIRouter(tags=["Tracking & Analytics"])

# 1x1 Transparent GIF bytes (43 bytes standard)
TRANSPARENT_GIF_BYTES = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

# 1x1 Transparent PNG bytes
TRANSPARENT_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@router.get("/track/open/{email_id}")
async def track_email_open(
    email_id: str,
    request: Request,
    user_agent: Optional[str] = Header(default=None)
):
    """
    Tracking beacon: returns 1x1 transparent image, records OPEN event,
    and updates email and campaign analytics.
    """
    client_ip = request.client.host if request.client else "127.0.0.1"
    now = utc_now_iso()

    async with get_db() as db:
        async with db.execute("SELECT id, campaign_id, open_count, first_opened_at FROM sent_emails WHERE id = ?", (email_id,)) as cursor:
            email_row = await cursor.fetchone()

        if email_row:
            campaign_id = email_row["campaign_id"]
            current_open_cnt = email_row["open_count"] or 0
            is_first_open = (current_open_cnt == 0)

            event_id = f"evt_{uuid.uuid4().hex[:12]}"
            await db.execute("""
                INSERT INTO email_events (id, sent_email_id, campaign_id, event_type, ip_address, user_agent, event_payload, created_at)
                VALUES (?, ?, ?, 'open', ?, ?, ?, ?)
            """, (
                event_id,
                email_id,
                campaign_id,
                client_ip,
                user_agent,
                json.dumps({"ip": client_ip, "user_agent": user_agent}),
                now
            ))

            first_opened = email_row["first_opened_at"] or now
            await db.execute("""
                UPDATE sent_emails
                SET open_count = open_count + 1,
                    first_opened_at = ?,
                    last_opened_at = ?
                WHERE id = ?
            """, (first_opened, now, email_id))

            if campaign_id and is_first_open:
                await db.execute("""
                    UPDATE campaigns
                    SET open_count = open_count + 1,
                        updated_at = ?
                    WHERE id = ?
                """, (now, campaign_id))

            await db.commit()

            # Broadcast live dynamic open notification
            recipient_email = email_row["recipient_email"] if "recipient_email" in email_row.keys() else ""
            await emit_event("email_opened", {
                "email_id": email_id,
                "campaign_id": campaign_id,
                "recipient": recipient_email,
                "ip": client_ip,
                "timestamp": now
            })

    return Response(
        content=TRANSPARENT_PNG_BYTES,
        media_type="image/png",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


@router.get("/track/click/{email_id}")
async def track_email_click(
    email_id: str,
    request: Request,
    url: str = Query(..., description="Destination URL to redirect to"),
    user_agent: Optional[str] = Header(default=None)
):
    """
    Click Tracker: records CLICK event with target destination URL,
    increments click metrics, and redirects user via HTTP 307.
    SEC-REDIR-001: Validates destination URL to prevent unvalidated open redirection.
    """
    target_url = urllib.parse.unquote(url).strip()
    if not (target_url.startswith("http://") or target_url.startswith("https://")):
        target_url = "https://" + target_url.lstrip("/")

    parsed_target = urllib.parse.urlparse(target_url)
    if parsed_target.scheme not in ("http", "https") or not parsed_target.netloc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid destination URL scheme. Only HTTP and HTTPS are permitted."
        )

    client_ip = request.client.host if request.client else "127.0.0.1"
    now = utc_now_iso()

    async with get_db() as db:
        async with db.execute(
            "SELECT id, campaign_id, click_count, rendered_html, body_html, body_text FROM sent_emails WHERE id = ?",
            (email_id,)
        ) as cursor:
            email_row = await cursor.fetchone()

        if not email_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Email tracking record not found."
            )

        # Validate that the destination URL was part of the dispatched email or matches tracking host
        tracking_host = urllib.parse.urlparse(settings.TRACKING_BASE_URL).hostname or "localhost"
        is_same_host = (parsed_target.hostname == tracking_host or parsed_target.hostname in ("127.0.0.1", "localhost"))
        email_content = (email_row["rendered_html"] or "") + (email_row["body_html"] or "") + (email_row["body_text"] or "")
        url_in_email = (target_url in email_content or url in email_content or urllib.parse.quote(target_url, safe="") in email_content)

        if not (is_same_host or url_in_email):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Untrusted or unverified destination redirect URL."
            )

        campaign_id = email_row["campaign_id"]
        current_click_cnt = email_row["click_count"] or 0
        is_first_click = (current_click_cnt == 0)

        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        await db.execute("""
            INSERT INTO email_events (id, sent_email_id, campaign_id, event_type, ip_address, user_agent, event_payload, created_at)
            VALUES (?, ?, ?, 'click', ?, ?, ?, ?)
        """, (
            event_id,
            email_id,
            campaign_id,
            client_ip,
            user_agent,
            json.dumps({"target_url": target_url, "ip": client_ip, "user_agent": user_agent}),
            now
        ))

        await db.execute("""
            UPDATE sent_emails
            SET click_count = click_count + 1
            WHERE id = ?
        """, (email_id,))

        if campaign_id and is_first_click:
            await db.execute("""
                UPDATE campaigns
                SET click_count = click_count + 1,
                    updated_at = ?
                WHERE id = ?
            """, (now, campaign_id))

        await db.commit()

        # Broadcast live dynamic click notification
        await emit_event("email_clicked", {
            "email_id": email_id,
            "campaign_id": campaign_id,
            "target_url": target_url,
            "ip": client_ip,
            "timestamp": now
        })

    return RedirectResponse(url=target_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
@router.post("/unsubscribe/{token}", response_class=HTMLResponse)
@router.get("/track/unsubscribe/{token}", response_class=HTMLResponse)
@router.post("/track/unsubscribe/{token}", response_class=HTMLResponse)
async def handle_unsubscribe(
    token: str,
    request: Request,
    user_agent: Optional[str] = Header(default=None)
):
    """
    1-Click Unsubscribe Handler: updates subscriber status to 'unsubscribed',
    adds to global suppressions table, records event, and renders confirmation page.
    """
    client_ip = request.client.host if request.client else "127.0.0.1"
    now = utc_now_iso()

    verified = verify_unsubscribe_token(token)
    email_target: Optional[str] = None
    sub_id: Optional[str] = None

    if verified:
        email_target, sub_id = verified

    # Campaign footers built by the template engine address the email by its storage id
    # and carry the signed token plus the address in the query string.
    if not email_target:
        query_email = (request.query_params.get("email") or "").strip().lower()
        query_token = request.query_params.get("token") or ""
        if query_email and query_token and template_engine.verify_unsubscribe_token(token, query_email, query_token):
            email_target = query_email

    async with get_db() as db:
        if not email_target:
            async with db.execute("SELECT email, id FROM subscribers WHERE id = ?", (token,)) as cursor:
                sub_row = await cursor.fetchone()
                if sub_row:
                    email_target = sub_row["email"]
                    sub_id = sub_row["id"]

        if not email_target:
            async with db.execute("SELECT recipient_email FROM sent_emails WHERE id = ?", (token,)) as cursor:
                mail_row = await cursor.fetchone()
                if mail_row:
                    email_target = (mail_row["recipient_email"] or "").strip().lower()

        if not email_target:
            if "@" in token:
                email_target = token.strip().lower()

        if email_target:
            await db.execute("""
                UPDATE subscribers
                SET status = 'unsubscribed', updated_at = ?
                WHERE email = ? OR id = ?
            """, (now, email_target, sub_id))

            await db.execute("""
                INSERT OR IGNORE INTO suppressions (id, email, campaign_id, reason, created_at)
                VALUES (?, ?, NULL, 'user_unsubscribed', ?)
            """, (f"sup_{uuid.uuid4().hex[:10]}", email_target, now))

            async with db.execute(
                "SELECT id, campaign_id FROM sent_emails WHERE recipient_email = ? ORDER BY created_at DESC LIMIT 1",
                (email_target,)
            ) as s_cur:
                s_row = await s_cur.fetchone()
                if s_row:
                    email_id = s_row["id"]
                    camp_id = s_row["campaign_id"]
                    event_id = f"evt_{uuid.uuid4().hex[:12]}"

                    await db.execute("""
                        INSERT INTO email_events (id, sent_email_id, campaign_id, event_type, ip_address, user_agent, event_payload, created_at)
                        VALUES (?, ?, ?, 'unsubscribe', ?, ?, ?, ?)
                    """, (
                        event_id,
                        email_id,
                        camp_id,
                        client_ip,
                        user_agent,
                        json.dumps({"email": email_target, "ip": client_ip}),
                        now
                    ))

                    if camp_id:
                        await db.execute("""
                            UPDATE campaigns
                            SET unsubscribe_count = unsubscribe_count + 1, updated_at = ?
                            WHERE id = ?
                        """, (now, camp_id))

            await db.commit()

    display_email = email_target or "your email address"

    html_page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Unsubscribed Successfully | NexusMail</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 flex items-center justify-center min-h-screen p-4">
  <div class="max-w-md w-full bg-slate-900 border border-slate-800 rounded-2xl p-8 text-center shadow-2xl">
    <div class="w-16 h-16 bg-emerald-500/10 text-emerald-400 rounded-full flex items-center justify-center mx-auto mb-5 text-2xl font-bold">
      ✓
    </div>
    <h1 class="text-2xl font-bold text-white mb-2">Unsubscribed Successfully</h1>
    <p class="text-slate-400 text-sm mb-6 leading-relaxed">
      <strong>{display_email}</strong> has been removed from this mailing list and added to the global suppression register.
    </p>
    <div class="bg-slate-800/50 rounded-xl p-4 border border-slate-700/50 text-xs text-slate-400 text-left mb-6">
      <div class="flex items-center justify-between mb-1">
        <span class="text-slate-500">Status:</span>
        <span class="text-emerald-400 font-mono">SUPPRESSED</span>
      </div>
      <div class="flex items-center justify-between mb-1">
        <span class="text-slate-500">Effective:</span>
        <span class="text-slate-300 font-mono">{now} UTC</span>
      </div>
      <div class="flex items-center justify-between">
        <span class="text-slate-500">Relay:</span>
        <span class="text-slate-300">NexusMail Compliance Vault</span>
      </div>
    </div>
    <p class="text-xs text-slate-500">
      Did you do this by mistake? Contact support to reactivate your subscription.
    </p>
  </div>
</body>
</html>"""

    return HTMLResponse(content=html_page, status_code=status.HTTP_200_OK)
