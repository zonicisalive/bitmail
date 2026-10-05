"""
Sent Email Storage Archive Vault API endpoints.
Provides full-text search, filtering, raw EML export, live HTML iframe rendering,
audit timeline inspection, and email resending.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.db import get_db, utc_now_iso
from app.models import EmailStatus, SentEmailResponse, SentEmailVaultSummary
from app.sender import send_single_email

router = APIRouter(prefix="/api/storage", tags=["Sent Email Storage Vault"])


class ResendRequest(BaseModel):
    recipient_email: Optional[str] = Field(default=None, description="Optional new recipient override")


@router.get("/summary", response_model=SentEmailVaultSummary)
async def get_storage_summary():
    """
    Get summary statistics and disk usage of the Sent Email Storage Vault.
    """
    async with get_db() as db:
        async with db.execute("""
            SELECT 
                COUNT(*) as total_archived,
                SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) as queued_count,
                SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) as sent_count,
                SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) as delivered_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                SUM(CASE WHEN status = 'bounced' THEN 1 ELSE 0 END) as bounced_count,
                SUM(open_count) as total_opens,
                SUM(click_count) as total_clicks
            FROM sent_emails
        """) as cursor:
            row = await cursor.fetchone()

    disk_bytes = 0
    try:
        if settings.EML_STORAGE_DIR.exists():
            for f in settings.EML_STORAGE_DIR.glob("*.eml"):
                disk_bytes += f.stat().st_size
    except Exception:
        pass

    return SentEmailVaultSummary(
        total_archived=row["total_archived"] or 0,
        queued_count=row["queued_count"] or 0,
        sent_count=row["sent_count"] or 0,
        delivered_count=row["delivered_count"] or 0,
        failed_count=row["failed_count"] or 0,
        bounced_count=row["bounced_count"] or 0,
        total_opens=row["total_opens"] or 0,
        total_clicks=row["total_clicks"] or 0,
        storage_disk_usage_bytes=disk_bytes
    )


@router.get("/emails")
async def list_stored_emails(
    search: Optional[str] = Query(default=None, alias="search_query"),
    q: Optional[str] = None,
    campaign_id: Optional[str] = None,
    recipient: Optional[str] = Query(default=None, alias="recipient_email"),
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    page: Optional[int] = None,
    per_page: Optional[int] = None
):
    """
    Search and filter stored emails in the Archive Vault.
    """
    if page and per_page:
        limit = per_page
        offset = (page - 1) * per_page

    query_term = search or q

    async with get_db() as db:
        sql = """
            SELECT 
                s.*,
                c.name as campaign_name
            FROM sent_emails s
            LEFT JOIN campaigns c ON s.campaign_id = c.id
            WHERE 1=1
        """
        params: List[Any] = []

        if query_term:
            sql += " AND (s.subject LIKE ? OR s.recipient_email LIKE ? OR s.recipient_name LIKE ? OR s.body_text LIKE ?)"
            term = f"%{query_term}%"
            params.extend([term, term, term, term])

        if campaign_id:
            sql += " AND s.campaign_id = ?"
            params.append(campaign_id)

        if recipient:
            sql += " AND s.recipient_email LIKE ?"
            params.append(f"%{recipient}%")

        if status:
            sql += " AND s.status = ?"
            params.append(status.lower())

        if date_from:
            sql += " AND s.created_at >= ?"
            params.append(date_from)

        if date_to:
            sql += " AND s.created_at <= ?"
            params.append(date_to)

        count_sql = f"SELECT COUNT(*) FROM ({sql})"
        async with db.execute(count_sql, params) as count_cur:
            c_row = await count_cur.fetchone()
            total_count = c_row[0] if c_row else 0

        sql += " ORDER BY s.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            results: List[Dict[str, Any]] = []

            for r in rows:
                headers = {}
                metadata = {}
                try:
                    if r["headers"]:
                        headers = json.loads(r["headers"])
                except Exception:
                    pass
                try:
                    if r["metadata"]:
                        metadata = json.loads(r["metadata"])
                except Exception:
                    pass

                html_len = len(r["body_html"] or "")
                text_len = len(r["body_text"] or "")
                size_kb = round((html_len + text_len + 1024) / 1024, 1)

                results.append({
                    "id": r["id"],
                    "campaign_id": r["campaign_id"],
                    "campaign_name": r["campaign_name"] or "Transactional",
                    "recipient_email": r["recipient_email"],
                    "recipient_name": r["recipient_name"],
                    "recipient": r["recipient_email"],
                    "recipientName": r["recipient_name"],
                    "sender_email": r["sender_email"],
                    "sender_name": r["sender_name"],
                    "subject": r["subject"],
                    "status": r["status"],
                    "error_message": r["error_message"],
                    "message_id": r["message_id"],
                    "messageId": r["message_id"],
                    "open_count": r["open_count"] or 0,
                    "click_count": r["click_count"] or 0,
                    "first_opened_at": r["first_opened_at"],
                    "last_opened_at": r["last_opened_at"],
                    "raw_eml_path": r["raw_eml_path"],
                    "metadata": metadata,
                    "variables": metadata,
                    "headers": headers,
                    "created_at": r["created_at"],
                    "sent_at": r["sent_at"],
                    "sentAt": r["sent_at"] or r["created_at"],
                    "sizeBytes": f"{size_kb} KB"
                })

            return {
                "total": total_count,
                "limit": limit,
                "offset": offset,
                "emails": results,
                "data": results
            }


@router.get("/emails/{email_id}")
async def get_stored_email(email_id: str):
    """
    Get stored email detail with full HTML, text, headers, and event audit timeline.
    """
    async with get_db() as db:
        async with db.execute("""
            SELECT s.*, c.name as campaign_name
            FROM sent_emails s
            LEFT JOIN campaigns c ON s.campaign_id = c.id
            WHERE s.id = ?
        """, (email_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Archived email record not found")
            email_dict = dict(row)

        events: List[Dict[str, Any]] = []
        async with db.execute("""
            SELECT * FROM email_events
            WHERE sent_email_id = ?
            ORDER BY created_at ASC
        """, (email_id,)) as ev_cursor:
            ev_rows = await ev_cursor.fetchall()
            for er in ev_rows:
                ev_payload = {}
                try:
                    if er["event_payload"]:
                        ev_payload = json.loads(er["event_payload"])
                except Exception:
                    pass

                e_type = er["event_type"]
                event_name = f"Email {e_type.title()}"
                st_badge = "success"

                if e_type == "open":
                    event_name = "Email Opened by Recipient"
                    st_badge = "opened"
                elif e_type == "click":
                    event_name = f"Link Clicked: {ev_payload.get('target_url', '')}"
                    st_badge = "clicked"
                elif e_type == "bounce":
                    event_name = f"Delivery Bounced: {ev_payload.get('reason', '')}"
                    st_badge = "warning"
                elif e_type == "failed":
                    event_name = f"Delivery Failed: {ev_payload.get('error', '')}"
                    st_badge = "error"
                elif e_type == "queued":
                    event_name = "Queued in Dispatch Engine"
                    st_badge = "info"
                elif e_type == "delivered":
                    event_name = "250 OK - Mail Delivered to Remote MX"
                    st_badge = "success"
                elif e_type == "sent":
                    event_name = "Dispatched via SMTP Handshake"
                    st_badge = "success"

                detail_str = f"IP: {er['ip_address']}" if er["ip_address"] else ""
                if er["user_agent"]:
                    detail_str += f" ({er['user_agent']})"
                if not detail_str and ev_payload:
                    detail_str = str(ev_payload)

                events.append({
                    "id": er["id"],
                    "event_type": e_type,
                    "event": event_name,
                    "timestamp": er["created_at"],
                    "ip_address": er["ip_address"],
                    "user_agent": er["user_agent"],
                    "details": detail_str,
                    "payload": ev_payload,
                    "status": st_badge
                })

        headers_obj = {}
        metadata_obj = {}
        try:
            if email_dict["headers"]:
                headers_obj = json.loads(email_dict["headers"])
        except Exception:
            pass
        try:
            if email_dict["metadata"]:
                metadata_obj = json.loads(email_dict["metadata"])
        except Exception:
            pass

        html_len = len(email_dict["body_html"] or "")
        text_len = len(email_dict["body_text"] or "")
        size_str = f"{round((html_len + text_len + 1024) / 1024, 1)} KB"

        return {
            "id": email_dict["id"],
            "campaign_id": email_dict["campaign_id"],
            "campaign_name": email_dict["campaign_name"] or "Transactional",
            "recipient_email": email_dict["recipient_email"],
            "recipient_name": email_dict["recipient_name"],
            "recipient": email_dict["recipient_email"],
            "recipientName": email_dict["recipient_name"],
            "sender_email": email_dict["sender_email"],
            "sender_name": email_dict["sender_name"],
            "subject": email_dict["subject"],
            "body_html": email_dict["body_html"],
            "body_text": email_dict["body_text"],
            "htmlBody": email_dict["body_html"],
            "status": email_dict["status"],
            "error_message": email_dict["error_message"],
            "message_id": email_dict["message_id"],
            "messageId": email_dict["message_id"],
            "open_count": email_dict["open_count"] or 0,
            "click_count": email_dict["click_count"] or 0,
            "first_opened_at": email_dict["first_opened_at"],
            "last_opened_at": email_dict["last_opened_at"],
            "raw_eml_path": email_dict["raw_eml_path"],
            "headers": headers_obj,
            "metadata": metadata_obj,
            "variables": metadata_obj,
            "sizeBytes": size_str,
            "created_at": email_dict["created_at"],
            "sent_at": email_dict["sent_at"],
            "sentAt": email_dict["sent_at"] or email_dict["created_at"],
            "audit_timeline": events,
            "auditTimeline": events
        }


@router.get("/emails/{email_id}/rendered")
async def render_stored_email_html(email_id: str):
    """
    Return live HTML body rendered with safe headers for iframe preview.
    """
    async with get_db() as db:
        async with db.execute("SELECT body_html, body_text FROM sent_emails WHERE id = ?", (email_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Email not found")

            html_content = row["body_html"]
            if not html_content:
                text_content = row["body_text"] or "No content available."
                html_content = f"<html><body style='font-family:monospace;white-space:pre-wrap;padding:20px;'>{text_content}</body></html>"

            return HTMLResponse(
                content=html_content,
                headers={
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src https: data: http:; font-src https: data:; frame-ancestors 'self'",
                    "X-Content-Type-Options": "nosniff"
                }
            )


@router.get("/emails/{email_id}/eml")
async def download_stored_eml(email_id: str):
    """
    Download raw RFC 2822 .eml file.
    """
    async with get_db() as db:
        async with db.execute("SELECT raw_eml_path, subject FROM sent_emails WHERE id = ?", (email_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Email not found")

            raw_path = row["raw_eml_path"]
            if raw_path and Path(raw_path).exists():
                return FileResponse(
                    path=raw_path,
                    media_type="message/rfc822",
                    filename=f"{email_id}.eml"
                )

    fallback = settings.EML_STORAGE_DIR / f"{email_id}.eml"
    if fallback.exists():
        return FileResponse(
            path=str(fallback),
            media_type="message/rfc822",
            filename=f"{email_id}.eml"
        )

    raise HTTPException(status_code=404, detail="Raw .eml file is not available on disk.")


@router.post("/emails/{email_id}/resend")
async def resend_stored_email(email_id: str, payload: Optional[ResendRequest] = None):
    """
    Resend an archived email to the original recipient or a new target address.
    """
    async with get_db() as db:
        async with db.execute("SELECT * FROM sent_emails WHERE id = ?", (email_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Email not found")
            email_dict = dict(row)

    target_recipient = (payload.recipient_email if payload and payload.recipient_email else email_dict["recipient_email"]).strip().lower()

    metadata = {}
    try:
        if email_dict["metadata"]:
            metadata = json.loads(email_dict["metadata"])
    except Exception:
        pass

    result = await send_single_email(
        recipient_email=target_recipient,
        recipient_name=email_dict["recipient_name"],
        subject=email_dict["subject"],
        body_html=email_dict["body_html"],
        body_text=email_dict["body_text"],
        sender_email=email_dict["sender_email"],
        sender_name=email_dict["sender_name"],
        campaign_id=email_dict["campaign_id"],
        merge_variables=metadata,
        track_opens=True,
        track_clicks=True
    )

    return {
        "success": result["success"],
        "message": f"Resent email to {target_recipient}",
        "new_email_id": result["sent_email_id"],
        "status": result["status"],
        "error": result.get("error")
    }


@router.delete("/emails/{email_id}")
async def delete_stored_email(email_id: str):
    """
    Delete email record and raw .eml file from disk.
    """
    async with get_db() as db:
        async with db.execute("SELECT raw_eml_path FROM sent_emails WHERE id = ?", (email_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Email not found")
            raw_path = row["raw_eml_path"]

        await db.execute("DELETE FROM email_events WHERE sent_email_id = ?", (email_id,))
        await db.execute("DELETE FROM sent_emails WHERE id = ?", (email_id,))
        await db.commit()

    if raw_path and Path(raw_path).exists():
        try:
            os.remove(raw_path)
        except Exception:
            pass

    return {"success": True, "message": f"Email {email_id} deleted."}
