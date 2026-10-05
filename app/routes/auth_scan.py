"""
Direct QR Scan-to-Login and Mobile Device Authorization Engine.
Generates dynamic SVG QR codes, manages temporary authentication sessions,
provides mobile 1-tap approval interface, and broadcasts instant WebSocket approvals.
"""

import base64
import io
import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.db import get_db, utc_now_iso
from app.websocket import emit_event

logger = logging.getLogger("nexusmail.auth_scan")

router = APIRouter(prefix="/api/auth/scan", tags=["Direct Scan-to-Login"])


def generate_qr_svg(url: str) -> str:
    """Generate clean vector SVG QR code string."""
    factory = qrcode.image.svg.SvgPathImage
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
        image_factory=factory
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image()
    
    stream = io.BytesIO()
    img.save(stream)
    return stream.getvalue().decode("utf-8")


class CreateScanSessionRequest(BaseModel):
    device_info: Optional[str] = Field(default="Desktop Browser (NexusMail)", description="Client device description")


class ApproveScanSessionRequest(BaseModel):
    token: str = Field(..., description="Scan session token")
    email: str = Field(..., description="User / Sender email address to authenticate")
    name: Optional[str] = Field(default=None, description="User full name")


# ======================================================================
# API Endpoints
# ======================================================================

@router.post("/session")
async def create_scan_session(request: Request, payload: Optional[CreateScanSessionRequest] = None):
    """
    Create a new Direct Scan-to-Login session with a 5-minute TTL.
    Generates a unique authentication token and SVG QR Code.
    """
    session_id = f"scan_{uuid.uuid4().hex[:12]}"
    token = secrets.token_urlsafe(24)
    now = utc_now_iso()
    expires_dt = datetime.now(timezone.utc) + timedelta(minutes=5)
    expires_at = expires_dt.strftime("%Y-%m-%d %H:%M:%S")

    client_ip = request.client.host if request.client else "127.0.0.1"
    user_agent = request.headers.get("user-agent", "Web Browser")
    device_desc = (payload.device_info if payload and payload.device_info else "Desktop Browser")

    # Construct mobile authorization URL
    # If request is from localhost or IP, use the actual host header
    host = request.headers.get("host", f"127.0.0.1:{settings.PORT}")
    proto = "https" if request.url.scheme == "https" else "http"
    scan_url = f"{proto}://{host}/auth/scan-approve/{token}"

    # Generate QR Code SVG
    svg_code = generate_qr_svg(scan_url)

    async with get_db() as db:
        await db.execute("""
            INSERT INTO scan_sessions (
                id, token, status, device_info, ip_address, expires_at, created_at, updated_at
            ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
        """, (session_id, token, f"{device_desc} ({user_agent[:40]})", client_ip, expires_at, now, now))
        await db.commit()

    return {
        "success": True,
        "session_id": session_id,
        "token": token,
        "scan_url": scan_url,
        "qr_svg": svg_code,
        "expires_at": expires_at,
        "expires_in_seconds": 300,
        "message": "Scan session created. Scan QR code on your phone to authenticate."
    }


@router.get("/session/{session_id}/status")
async def get_scan_session_status(session_id: str):
    """
    Check the current status of a scan session (pending, approved, rejected, expired).
    """
    now_str = utc_now_iso()
    async with get_db() as db:
        async with db.execute("SELECT * FROM scan_sessions WHERE id = ?", (session_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Scan session not found")
            sess = dict(row)

    # Check expiration
    if sess["status"] == "pending" and sess["expires_at"] < now_str:
        async with get_db() as db:
            await db.execute("UPDATE scan_sessions SET status = 'expired', updated_at = ? WHERE id = ?", (now_str, session_id))
            await db.commit()
        sess["status"] = "expired"

    return {
        "session_id": sess["id"],
        "status": sess["status"],
        "user_email": sess["user_email"],
        "user_name": sess["user_name"],
        "auth_token": sess["auth_token"] if sess["status"] == "approved" else None,
        "is_approved": (sess["status"] == "approved"),
        "expires_at": sess["expires_at"]
    }


@router.post("/approve")
async def approve_scan_session(payload: ApproveScanSessionRequest):
    """
    Mobile endpoint: Approve a scan session and bind user email / auth token.
    Broadcasts live WebSocket notification to immediately unlock desktop browser.
    """
    clean_email = payload.email.strip().lower()
    if not clean_email or "@" not in clean_email:
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")

    user_name = payload.name or clean_email.split("@")[0].capitalize()
    auth_token = f"auth_tok_{secrets.token_hex(20)}"
    now = utc_now_iso()

    async with get_db() as db:
        async with db.execute("SELECT * FROM scan_sessions WHERE token = ?", (payload.token,)) as cur:
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Invalid or expired scan token.")
            sess = dict(row)

        if sess["status"] != "pending":
            raise HTTPException(status_code=400, detail=f"Session is already {sess['status']}.")

        if sess["expires_at"] < now:
            await db.execute("UPDATE scan_sessions SET status = 'expired', updated_at = ? WHERE id = ?", (now, sess["id"]))
            await db.commit()
            raise HTTPException(status_code=400, detail="This scan QR code has expired. Please refresh the QR code.")

        # Check if user exists in users table, or auto-provision
        user_id = None
        async with db.execute("SELECT id FROM users WHERE LOWER(email) = ?", (clean_email,)) as u_cur:
            u_row = await u_cur.fetchone()
            if u_row:
                user_id = u_row["id"]

        if not user_id:
            from app.auth import hash_password
            user_id = f"usr_{uuid.uuid4().hex[:12]}"
            random_hash = hash_password(secrets.token_urlsafe(20))
            await db.execute("""
                INSERT INTO users (id, email, username, password_hash, name, role, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'admin', 'active', ?, ?)
            """, (user_id, clean_email, clean_email.split("@")[0], random_hash, user_name, now, now))

        # Create persistent session for auth_token
        sess_expires_dt = datetime.now(timezone.utc) + timedelta(days=settings.SESSION_EXPIRE_DAYS)
        sess_expires_str = sess_expires_dt.strftime("%Y-%m-%d %H:%M:%S")
        await db.execute("""
            INSERT OR REPLACE INTO user_sessions (
                token, user_id, expires_at, created_at, last_seen_at, user_agent, ip_address
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (auth_token, user_id, sess_expires_str, now, now, sess.get("device_info", "Mobile QR Scan"), sess.get("ip_address", "127.0.0.1")))

        # Mark session approved
        await db.execute("""
            UPDATE scan_sessions
            SET status = 'approved',
                user_email = ?,
                user_name = ?,
                auth_token = ?,
                updated_at = ?
            WHERE id = ?
        """, (clean_email, user_name, auth_token, now, sess["id"]))

        # Also ensure this user exists in subscribers directory as an admin/sender
        await db.execute("""
            INSERT OR IGNORE INTO subscribers (
                id, email, first_name, last_name, status, tags, custom_fields, created_at, updated_at
            ) VALUES (?, ?, ?, '', 'active', '["authenticated-user", "admin"]', '{}', ?, ?)
        """, (f"sub_{uuid.uuid4().hex[:10]}", clean_email, user_name, now, now))

        await db.commit()

    # Broadcast instant WebSocket unlock to desktop dashboard
    await emit_event("scan_auth_approved", {
        "session_id": sess["id"],
        "email": clean_email,
        "name": user_name,
        "auth_token": auth_token,
        "timestamp": now
    })

    return {
        "success": True,
        "session_id": sess["id"],
        "message": f"Successfully authorized login for {clean_email}!",
        "email": clean_email,
        "name": user_name,
        "auth_token": auth_token
    }


@router.post("/simulate-approval/{session_id}")
async def simulate_scan_approval(session_id: str, email: Optional[str] = None):
    """
    1-Click test simulation: approves the scan session immediately on the server
    without needing a physical mobile phone camera.
    """
    user_email = (email or "admin@bitmail.com").strip().lower()
    user_name = "Bitmail Admin" if "@bitmail" in user_email or "@bitnade" in user_email else user_email.split("@")[0].capitalize()
    auth_token = f"auth_tok_sim_{secrets.token_hex(16)}"
    now = utc_now_iso()

    async with get_db() as db:
        async with db.execute("SELECT * FROM scan_sessions WHERE id = ?", (session_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Scan session not found")
            sess = dict(row)

        user_id = None
        async with db.execute("SELECT id FROM users WHERE LOWER(email) = ?", (user_email,)) as u_cur:
            u_row = await u_cur.fetchone()
            if u_row:
                user_id = u_row["id"]

        if not user_id:
            from app.auth import hash_password
            user_id = f"usr_{uuid.uuid4().hex[:12]}"
            random_hash = hash_password(secrets.token_urlsafe(20))
            await db.execute("""
                INSERT INTO users (id, email, username, password_hash, name, role, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'admin', 'active', ?, ?)
            """, (user_id, user_email, user_email.split("@")[0], random_hash, user_name, now, now))

        sess_expires_dt = datetime.now(timezone.utc) + timedelta(days=settings.SESSION_EXPIRE_DAYS)
        sess_expires_str = sess_expires_dt.strftime("%Y-%m-%d %H:%M:%S")
        await db.execute("""
            INSERT OR REPLACE INTO user_sessions (
                token, user_id, expires_at, created_at, last_seen_at, user_agent, ip_address
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (auth_token, user_id, sess_expires_str, now, now, "Simulated QR Scan", "127.0.0.1"))

        await db.execute("""
            UPDATE scan_sessions
            SET status = 'approved',
                user_email = ?,
                user_name = ?,
                auth_token = ?,
                updated_at = ?
            WHERE id = ?
        """, (user_email, user_name, auth_token, now, session_id))
        await db.commit()

    # Broadcast instant WebSocket unlock
    await emit_event("scan_auth_approved", {
        "session_id": session_id,
        "email": user_email,
        "name": user_name,
        "auth_token": auth_token,
        "timestamp": now
    })

    return {
        "success": True,
        "session_id": session_id,
        "email": user_email,
        "name": user_name,
        "auth_token": auth_token,
        "message": f"Simulated instant mobile scan approval for {user_email}"
    }
