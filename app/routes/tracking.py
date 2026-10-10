"""
Tracking Endpoints for Open Pixel Beacon, Click Link Redirector, and 1-Click Unsubscribe.
"""

import base64
import html
import json
import urllib.parse
import uuid
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.config import settings
from app.db import get_db, utc_now_iso
from app.models import EventType, SubscriberStatus
from app.sender import verify_unsubscribe_token
from app.template_engine import template_engine
from app.webhooks import WebhookDispatcher
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

            await WebhookDispatcher.dispatch_event("email.opened", {
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

        await WebhookDispatcher.dispatch_event("email.clicked", {
            "email_id": email_id,
            "campaign_id": campaign_id,
            "target_url": target_url,
            "ip": client_ip,
            "timestamp": now
        })

    return RedirectResponse(url=target_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


# ---------------------------------------------------------------------------
# Unsubscribe / resubscribe
#
# Clicking the unsubscribe link in an email unsubscribes in one click (GET), and
# mail clients' List-Unsubscribe button does the same with a POST (RFC 8058).
# The page then rewrites its own URL with view=1, so a reload or a restored tab
# only shows the current status instead of unsubscribing again (which used to
# undo "Subscribe again"). Resubscribing always needs a button press (POST), so
# link scanners can't re-add someone who opted out.
# Only signed links or unguessable record ids identify the recipient: a bare
# email address in the URL is not proof of anything.
# ---------------------------------------------------------------------------

_LINK_PARAMS = ("token", "email", "campaign_id")


async def _resolve_recipient(token: str, request: Request, db) -> tuple[Optional[str], Optional[str]]:
    """Return (email, subscriber_id) for this link, or (None, None)."""
    # Signed path token. Tokens without a "." are bare emails/ids, which
    # verify_unsubscribe_token accepts unsigned, so they are not trusted here.
    if "." in token:
        verified = verify_unsubscribe_token(token)
        if verified and verified[0]:
            return verified[0].strip().lower(), verified[1]

    # Campaign footer links: storage id + signed token + email in the query.
    query_email = (request.query_params.get("email") or "").strip().lower()
    query_token = request.query_params.get("token") or ""
    if query_email and query_token:
        if template_engine.verify_unsubscribe_token(token, query_email, query_token):
            return query_email, None
        return None, None  # a bad signature never falls through to the id lookups

    async with db.execute("SELECT email, id FROM subscribers WHERE id = ?", (token,)) as cursor:
        row = await cursor.fetchone()
        if row:
            return row["email"], row["id"]
    async with db.execute("SELECT recipient_email FROM sent_emails WHERE id = ?", (token,)) as cursor:
        row = await cursor.fetchone()
        if row and row["recipient_email"]:
            return row["recipient_email"].strip().lower(), None
    return None, None


async def _is_suppressed(db, email: str) -> bool:
    async with db.execute("SELECT 1 FROM suppressions WHERE email = ? LIMIT 1", (email,)) as cursor:
        return await cursor.fetchone() is not None


async def _record_event(db, email: str, event_type: str, client_ip: str, user_agent: Optional[str], now: str) -> Optional[str]:
    """Log the event against the recipient's latest email; returns its campaign id."""
    async with db.execute(
        "SELECT id, campaign_id FROM sent_emails WHERE recipient_email = ? ORDER BY created_at DESC LIMIT 1",
        (email,)
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return None
    payload = {"email": email, "ip": client_ip}
    if event_type == "resubscribe":
        payload["action"] = "resubscribe"
    await db.execute("""
        INSERT INTO email_events (id, sent_email_id, campaign_id, event_type, ip_address, user_agent, event_payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (f"evt_{uuid.uuid4().hex[:12]}", row["id"], row["campaign_id"], event_type, client_ip, user_agent, json.dumps(payload), now))
    return row["campaign_id"]


def _link_query(request: Request, **extra: str) -> str:
    params = {k: request.query_params[k] for k in _LINK_PARAMS if request.query_params.get(k)}
    params.update(extra)
    return urllib.parse.urlencode(params)


def _status_page(state: str, email: Optional[str], token: str, request: Request) -> HTMLResponse:
    """state: unsubscribed | subscribed | confirm_resubscribe | invalid"""
    company = html.escape(settings.COMPANY_NAME or "Bitnade")
    who = html.escape(email) if email else "your email address"
    path_token = urllib.parse.quote(token, safe="")
    view_url = f"/unsubscribe/{path_token}?{_link_query(request, view='1')}"
    resub_url = f"/resubscribe/{path_token}?{_link_query(request)}"
    unsub_url = f"/unsubscribe/{path_token}?{_link_query(request)}"

    if state == "unsubscribed":
        icon, title = "✓", "You're unsubscribed"
        text = f"<strong>{who}</strong> won't get any more emails from {company}."
        prompt, action, button = "Unsubscribed by mistake?", resub_url, "Subscribe again"
    elif state == "subscribed":
        icon, title = "🎉", "Welcome back!"
        text = f"<strong>{who}</strong> is subscribed to updates from {company}."
        prompt, action, button = "Changed your mind?", unsub_url, "Unsubscribe"
    elif state == "confirm_resubscribe":
        icon, title = "↩", "Subscribe again?"
        text = f"Press the button to start getting emails from {company} at <strong>{who}</strong> again."
        prompt, action, button = "", resub_url, "Subscribe again"
    else:
        icon, title = "!", "Link not recognised"
        text = "This link is incomplete or has expired. Use the unsubscribe link in your most recent email, or reply to it and ask to be removed."
        prompt, action, button = "", "", ""

    form = ""
    if action:
        form = f"""
    <div class="pt-5 border-t border-slate-800 space-y-3">
      {f'<p class="text-xs text-slate-400">{prompt}</p>' if prompt else ''}
      <form method="POST" action="{html.escape(action)}" id="status-form">
        <button type="submit" id="status-btn" class="w-full py-2.5 px-4 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-semibold text-sm transition-colors cursor-pointer">{button}</button>
      </form>
    </div>"""

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="robots" content="noindex">
  <title>{title} | {company}</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 flex items-center justify-center min-h-screen p-4">
  <div class="max-w-md w-full bg-slate-900 border border-slate-800 rounded-2xl p-8 text-center shadow-2xl">
    <div class="w-16 h-16 bg-emerald-500/10 text-emerald-400 rounded-full flex items-center justify-center mx-auto mb-5 text-2xl font-bold">{icon}</div>
    <h1 class="text-2xl font-bold text-white mb-2">{title}</h1>
    <p class="text-slate-400 text-sm mb-6 leading-relaxed">{text}</p>{form}
  </div>
  <script>
    const viewUrl = {json.dumps(view_url)};
    // Reloading this page must not repeat the action that brought us here.
    {"history.replaceState(null, '', viewUrl);" if state in ("unsubscribed", "subscribed") else ""}
    const form = document.getElementById('status-form');
    if (form) form.addEventListener('submit', async (e) => {{
      e.preventDefault();
      const btn = document.getElementById('status-btn');
      btn.disabled = true; btn.textContent = 'Working…';
      try {{
        const sep = form.action.includes('?') ? '&' : '?';
        const res = await fetch(form.action + sep + 'format=json', {{ method: 'POST', headers: {{ 'Accept': 'application/json' }} }});
        if (!res.ok) throw new Error();
        location.replace(viewUrl);
      }} catch {{
        btn.disabled = false; btn.textContent = 'Something went wrong. Try again';
      }}
    }});
  </script>
</body>
</html>"""
    return HTMLResponse(content=page, status_code=status.HTTP_200_OK)


def _wants_json(request: Request) -> bool:
    return request.query_params.get("format") == "json" or request.headers.get("accept", "").startswith("application/json")


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
    1-Click Unsubscribe: clicking the email link (GET) or the mail client's
    List-Unsubscribe button (POST) unsubscribes. GET with view=1 only shows status.
    """
    client_ip = request.client.host if request.client else "127.0.0.1"
    now = utc_now_iso()
    view_only = request.method == "GET" and request.query_params.get("view") == "1"
    changed = False
    campaign_id = None

    async with get_db() as db:
        email_target, sub_id = await _resolve_recipient(token, request, db)
        if email_target and not view_only:
            changed = not await _is_suppressed(db, email_target)
            await db.execute("""
                UPDATE subscribers SET status = 'unsubscribed', updated_at = ?
                WHERE email = ? OR id = ?
            """, (now, email_target, sub_id))
            await db.execute("""
                INSERT OR IGNORE INTO suppressions (id, email, campaign_id, reason, created_at)
                VALUES (?, ?, NULL, 'user_unsubscribed', ?)
            """, (f"sup_{uuid.uuid4().hex[:10]}", email_target, now))
            # Only a real change counts; repeat clicks don't inflate campaign stats.
            if changed:
                campaign_id = await _record_event(db, email_target, "unsubscribe", client_ip, user_agent, now)
                if campaign_id:
                    await db.execute(
                        "UPDATE campaigns SET unsubscribe_count = unsubscribe_count + 1, updated_at = ? WHERE id = ?",
                        (now, campaign_id)
                    )
            await db.commit()
        suppressed = bool(email_target) and await _is_suppressed(db, email_target)

    if changed:
        await WebhookDispatcher.dispatch_event("subscriber.unsubscribed", {
            "recipient_email": email_target,
            "reason": "user_unsubscribed",
            "campaign_id": campaign_id,
            "timestamp": now
        })

    # RFC 8058 One-Click POST (mail clients) and the page's own button get JSON.
    if request.method == "POST" and "text/html" not in request.headers.get("accept", ""):
        return JSONResponse(
            status_code=status.HTTP_200_OK if email_target else status.HTTP_404_NOT_FOUND,
            content={
                "status": "success" if email_target else "error",
                "unsubscribed": bool(email_target),
                "email": email_target,
                "timestamp": now,
                "message": "Recipient has been unsubscribed per RFC 8058 One-Click standard." if email_target else "Unsubscribe link not recognised."
            }
        )

    if not email_target:
        return _status_page("invalid", None, token, request)
    return _status_page("unsubscribed" if suppressed else "subscribed", email_target, token, request)


@router.post("/resubscribe/{token}", response_class=HTMLResponse)
@router.get("/resubscribe/{token}", response_class=HTMLResponse)
@router.post("/api/tracking/resubscribe/{token}", response_class=HTMLResponse)
@router.get("/api/tracking/resubscribe/{token}", response_class=HTMLResponse)
@router.post("/track/resubscribe/{token}", response_class=HTMLResponse)
@router.get("/track/resubscribe/{token}", response_class=HTMLResponse)
async def handle_resubscribe(
    token: str,
    request: Request,
    user_agent: Optional[str] = Header(default=None)
):
    """
    Resubscribe: POST removes the suppression and restores the subscriber.
    GET only shows a confirm button, so prefetchers can't resubscribe anyone.
    """
    client_ip = request.client.host if request.client else "127.0.0.1"
    now = utc_now_iso()

    async with get_db() as db:
        email_target, sub_id = await _resolve_recipient(token, request, db)
        if email_target and request.method == "POST":
            await db.execute("DELETE FROM suppressions WHERE email = ?", (email_target,))
            await db.execute("""
                UPDATE subscribers SET status = 'active', updated_at = ?
                WHERE email = ? OR id = ?
            """, (now, email_target, sub_id))
            await _record_event(db, email_target, "resubscribe", client_ip, user_agent, now)
            await db.commit()

    if email_target and request.method == "POST":
        await WebhookDispatcher.dispatch_event("subscriber.resubscribed", {
            "recipient_email": email_target,
            "reason": "user_resubscribed",
            "timestamp": now
        })

    if _wants_json(request):
        ok = bool(email_target) and request.method == "POST"
        return JSONResponse(
            status_code=status.HTTP_200_OK if email_target else status.HTTP_404_NOT_FOUND,
            content={
                "status": "success" if ok else "error",
                "resubscribed": ok,
                "email": email_target,
                "timestamp": now,
                "message": f"Successfully resubscribed {email_target}." if ok else "Resubscribe link not recognised or not confirmed."
            }
        )

    if not email_target:
        return _status_page("invalid", None, token, request)
    if request.method == "GET":
        return _status_page("confirm_resubscribe", email_target, token, request)
    return _status_page("subscribed", email_target, token, request)
