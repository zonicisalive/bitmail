"""
Dynamic Server-Side Page Routes and Real-Time WebSocket Endpoint.
Renders Jinja2 HTML templates pre-hydrated with live database context
and manages real-time browser WebSocket streaming.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user_optional, get_user_by_token
from app.config import settings
from app.db import get_db, utc_now_iso
from app.websocket import ws_manager

logger = logging.getLogger("bitmail.pages")

router = APIRouter(tags=["Dynamic Web Pages & Realtime"])

# Initialize Jinja2 templates directory
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


async def get_initial_page_context(request: Request, active_tab: str = "dashboard") -> dict:
    """Fetch live data context from SQLite to pre-render dynamic Jinja2 view."""
    current_user = await get_current_user_optional(request)
    is_authenticated = bool(current_user)

    stats = {
        "total_sent": 0,
        "delivery_rate": 0.0,
        "open_rate": 0.0,
        "click_rate": 0.0,
        "active_subscribers": 0,
        "vault_stored": 0,
    }
    recent_vault_emails = []
    recent_campaigns = []
    smtp_configs = []
    subscriber_lists = []
    template_presets = []

    # SEC-LEAK-002: Do not pre-render sensitive records for unauthenticated visitors
    if not is_authenticated:
        return {
            "request": request,
            "active_tab": active_tab,
            "stats": stats,
            "recent_vault_emails": recent_vault_emails,
            "recent_campaigns": recent_campaigns,
            "smtp_configs": smtp_configs,
            "default_relay": None,
            "subscriber_lists": subscriber_lists,
            "template_presets": template_presets,
            "app_env": settings.APP_ENV,
            "current_user": None,
            "is_authenticated": False,
        }

    try:
        async with get_db() as db:
            # Stats
            async with db.execute("SELECT COUNT(*) FROM sent_emails") as cur:
                row = await cur.fetchone()
                stats["vault_stored"] = row[0] if row else 0

            async with db.execute("SELECT COUNT(*) FROM sent_emails WHERE status IN ('sent', 'delivered')") as cur:
                row = await cur.fetchone()
                stats["total_sent"] = row[0] if row else 0

            async with db.execute("SELECT COUNT(*) FROM sent_emails WHERE status IN ('sent', 'delivered', 'failed', 'bounced')") as cur:
                row = await cur.fetchone()
                attempted = row[0] if row else 0
                if attempted:
                    stats["delivery_rate"] = round((stats["total_sent"] / attempted) * 100, 1)

            async with db.execute("SELECT COUNT(*) FROM subscribers WHERE status = 'active'") as cur:
                row = await cur.fetchone()
                stats["active_subscribers"] = row[0] if row else 0

            async with db.execute("SELECT COUNT(*) FROM sent_emails WHERE open_count > 0") as cur:
                row = await cur.fetchone()
                opens = row[0] if row else 0
                if stats["total_sent"] > 0:
                    stats["open_rate"] = round((opens / stats["total_sent"]) * 100, 1)

            async with db.execute("SELECT COUNT(*) FROM sent_emails WHERE click_count > 0") as cur:
                row = await cur.fetchone()
                clicks = row[0] if row else 0
                if stats["total_sent"] > 0:
                    stats["click_rate"] = round((clicks / stats["total_sent"]) * 100, 1)

            # Recent Vault Emails
            async with db.execute("SELECT id, recipient_email, recipient_name, subject, status, sent_at, open_count, click_count FROM sent_emails ORDER BY created_at DESC LIMIT 15") as cur:
                recent_vault_emails = [dict(r) for r in await cur.fetchall()]

            # Campaigns
            async with db.execute("SELECT * FROM campaigns ORDER BY created_at DESC LIMIT 10") as cur:
                recent_campaigns = [dict(r) for r in await cur.fetchall()]

            # SMTP Configs
            async with db.execute("SELECT id, name, host, port, username, is_default, use_tls FROM smtp_configs ORDER BY is_default DESC") as cur:
                rows = await cur.fetchall()
                smtp_configs = []
                for r in rows:
                    item = dict(r)
                    item["is_sandbox"] = (str(item["host"] or "").strip().lower() == "sandbox")
                    smtp_configs.append(item)

            # Lists
            async with db.execute("SELECT id, name, description FROM subscriber_lists") as cur:
                subscriber_lists = [dict(r) for r in await cur.fetchall()]

            # Templates
            async with db.execute("SELECT id, name, subject FROM templates") as cur:
                template_presets = [dict(r) for r in await cur.fetchall()]

    except Exception as e:
        logger.warning(f"Error gathering initial page context: {e}")

    default_relay = next((s for s in smtp_configs if s.get("is_default")), (smtp_configs[0] if smtp_configs else None))

    return {
        "request": request,
        "active_tab": active_tab,
        "stats": stats,
        "recent_vault_emails": recent_vault_emails,
        "recent_campaigns": recent_campaigns,
        "smtp_configs": smtp_configs,
        "default_relay": default_relay,
        "subscriber_lists": subscriber_lists,
        "template_presets": template_presets,
        "app_env": settings.APP_ENV,
        "current_user": current_user,
        "is_authenticated": True,
    }


# ======================================================================
# Dynamic HTML Web Page Routes
# ======================================================================

@router.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    """Dynamic Dashboard & Analytics Page."""
    ctx = await get_initial_page_context(request, active_tab="dashboard")
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


@router.get("/broadcast", response_class=HTMLResponse)
async def page_broadcast(request: Request):
    """Dynamic Quick Mass Broadcast Page."""
    ctx = await get_initial_page_context(request, active_tab="broadcast")
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


@router.get("/vault", response_class=HTMLResponse)
async def page_vault(request: Request):
    """Dynamic Email Storage Vault Page."""
    ctx = await get_initial_page_context(request, active_tab="vault")
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


@router.get("/subscribers", response_class=HTMLResponse)
@router.get("/customers", response_class=HTMLResponse)
async def page_customers(request: Request):
    """Dynamic Customer Contacts & Lists Page."""
    ctx = await get_initial_page_context(request, active_tab="subscribers")
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


@router.get("/campaigns", response_class=HTMLResponse)
async def page_campaigns(request: Request):
    """Dynamic Mass Campaigns Page."""
    ctx = await get_initial_page_context(request, active_tab="campaigns")
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


@router.get("/templates-studio", response_class=HTMLResponse)
async def page_templates(request: Request):
    """Dynamic Template Studio Page."""
    ctx = await get_initial_page_context(request, active_tab="templates")
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


@router.get("/smtp", response_class=HTMLResponse)
async def page_smtp(request: Request):
    """Dynamic Mail Servers & SMTP Relays Page."""
    ctx = await get_initial_page_context(request, active_tab="smtp")
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


@router.get("/logs", response_class=HTMLResponse)
async def page_logs(request: Request):
    """Dynamic System & Dispatch Logs Page."""
    ctx = await get_initial_page_context(request, active_tab="logs")
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


@router.get("/deliverability", response_class=HTMLResponse)
async def page_deliverability(request: Request):
    """Dynamic Deliverability & DNS Diagnostics Page."""
    ctx = await get_initial_page_context(request, active_tab="deliverability")
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


@router.get("/warmup", response_class=HTMLResponse)
async def page_warmup(request: Request):
    """Dynamic Email Warmup & Multi-Relay Rotation Page."""
    ctx = await get_initial_page_context(request, active_tab="warmup")
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)



@router.get("/auth/scan-approve/{token}", response_class=HTMLResponse)
async def page_scan_approve(request: Request, token: str):
    """Mobile 1-Tap QR Scan Approval Page."""
    now_str = utc_now_iso()
    session_data = None
    is_expired = False
    is_already_approved = False

    async with get_db() as db:
        async with db.execute("SELECT * FROM scan_sessions WHERE token = ?", (token,)) as cur:
            row = await cur.fetchone()
            if row:
                session_data = dict(row)
                if session_data["status"] == "approved":
                    is_already_approved = True
                elif session_data["expires_at"] < now_str or session_data["status"] == "expired":
                    is_expired = True

    if not session_data:
        is_expired = True

    current_user = await get_current_user_optional(request)

    ctx = {
        "request": request,
        "token": token,
        "session": session_data,
        "is_expired": is_expired,
        "is_already_approved": is_already_approved,
        "current_user": current_user,
    }
    return templates.TemplateResponse(request=request, name="scan_approve.html", context=ctx)


# ======================================================================
# Real-Time WebSocket Streaming Endpoint
# ======================================================================

@router.websocket("/ws/live")
async def websocket_live_telemetry(websocket: WebSocket):
    """
    Persistent WebSocket endpoint for live broadcast streaming,
    open/click notifications, and storage vault updates.
    SEC-LEAK-001: Requires valid authentication token via cookie or query param.
    """
    token = (
        websocket.cookies.get("bitmail_token") or
        websocket.query_params.get("token") or
        websocket.query_params.get("auth_token")
    )
    user = await get_user_by_token(token) if token else None
    if not user:
        await websocket.close(code=1008)  # WS_1008_POLICY_VIOLATION
        return

    await ws_manager.connect(websocket)
    try:
        # Send initial live connection handshake
        await websocket.send_text(json.dumps({
            "type": "connection_established",
            "message": "Connected to Bitmail Live Telemetry Engine",
            "active_nodes": 1
        }))

        # Keep connection open and receive heartbeats / pings
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("action") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except Exception:
                pass

    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception as e:
        logger.debug(f"WebSocket session terminated: {e}")
        await ws_manager.disconnect(websocket)
