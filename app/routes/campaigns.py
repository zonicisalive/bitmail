"""
Campaign Management API endpoints for scheduling, dispatching, pausing, resuming,
and monitoring high-volume mass email marketing campaigns.
"""

import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.config import settings
from app.db import get_db, utc_now_iso
from app.models import (
    CampaignCreate,
    CampaignResponse,
    CampaignStatsResponse,
    CampaignStatus,
    CampaignUpdate,
)
from app.queue import campaign_queue
from app.scheduler import parse_and_normalize_schedule_time
from app.sender import send_single_email

router = APIRouter(prefix="/api/campaigns", tags=["Campaigns"])


class CampaignCreatePayload(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    subject: str
    template_id: Optional[str] = None
    list_id: Optional[str] = None
    list_ids: Optional[List[str]] = None
    smtp_config_id: Optional[str] = None
    sender_name: Optional[str] = None
    sender_email: Optional[str] = None
    reply_to: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)
    track_opens: bool = True
    track_clicks: bool = True
    custom_html: Optional[str] = None
    custom_text: Optional[str] = None
    scheduled_at: Optional[str] = None
    rate_limit: Optional[int] = None


class TestSendRequest(BaseModel):
    test_email: str = Field(..., description="Target email to receive test verification send")
    sample_variables: Dict[str, Any] = Field(default_factory=dict)


@router.get("", response_model=List[CampaignResponse])
async def list_campaigns(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    search: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0)
):
    """
    List campaigns with live status and aggregated telemetry counters.
    """
    async with get_db() as db:
        query = "SELECT * FROM campaigns WHERE 1=1"
        params: List[Any] = []

        if status_filter:
            query += " AND status = ?"
            params.append(status_filter.lower())

        if search:
            query += " AND (name LIKE ? OR subject LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            results: List[CampaignResponse] = []
            for r in rows:
                headers = {}
                try:
                    raw_h = r["headers"] if "headers" in r.keys() else (r["headers_json"] if "headers_json" in r.keys() else None)
                    if raw_h:
                        headers = json.loads(raw_h)
                except Exception:
                    pass

                results.append(
                    CampaignResponse(
                        id=r["id"],
                        name=r["name"],
                        subject=r["subject"],
                        template_id=r["template_id"],
                        list_id=r["list_id"],
                        smtp_config_id=r["smtp_config_id"],
                        sender_name=r["sender_name"],
                        sender_email=r["sender_email"],
                        reply_to=r["reply_to"],
                        headers=headers,
                        track_opens=bool(r["track_opens"]),
                        track_clicks=bool(r["track_clicks"]),
                        custom_html=r["custom_html"],
                        custom_text=r["custom_text"],
                        status=CampaignStatus(r["status"]),
                        scheduled_at=r["scheduled_at"],
                        started_at=r["started_at"],
                        completed_at=r["completed_at"],
                        total_recipients=r["total_recipients"] or 0,
                        sent_count=r["sent_count"] or 0,
                        delivered_count=r["delivered_count"] or 0,
                        failed_count=r["failed_count"] or 0,
                        open_count=r["open_count"] or 0,
                        click_count=r["click_count"] or 0,
                        unsubscribe_count=r["unsubscribe_count"] or 0,
                        bounce_count=r["bounce_count"] or 0,
                        created_at=r["created_at"],
                        updated_at=r["updated_at"]
                    )
                )

            return results


@router.post("", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_campaign(payload: CampaignCreatePayload):
    """
    Create a new campaign. Supports title/name aliases and list_id/list_ids.
    """
    camp_id = f"cmp_{uuid.uuid4().hex[:10]}"
    now = utc_now_iso()

    campaign_name = (payload.name or payload.title or "Untitled Campaign").strip()
    target_list_id = payload.list_id or (payload.list_ids[0] if payload.list_ids else None)
    sender_name = payload.sender_name or settings.DEFAULT_SENDER_NAME
    sender_email = payload.sender_email or settings.DEFAULT_SENDER_EMAIL

    initial_recipients = 0
    async with get_db() as db:
        if target_list_id:
            async with db.execute("""
                SELECT COUNT(DISTINCT s.id) 
                FROM subscribers s
                LEFT JOIN subscriber_list_memberships m ON s.id = m.subscriber_id
                LEFT JOIN list_subscribers ls ON s.id = ls.subscriber_id
                WHERE (m.list_id = ? OR ls.list_id = ?) AND s.status = 'active'
                  AND s.email NOT IN (SELECT email FROM suppressions)
            """, (target_list_id, target_list_id)) as cursor:
                c_row = await cursor.fetchone()
                initial_recipients = c_row[0] if c_row else 0
        else:
            async with db.execute("""
                SELECT COUNT(*) FROM subscribers 
                WHERE status = 'active' AND email NOT IN (SELECT email FROM suppressions)
            """) as cursor:
                c_row = await cursor.fetchone()
                initial_recipients = c_row[0] if c_row else 0

        normalized_sched = None
        if payload.scheduled_at:
            try:
                normalized_sched = parse_and_normalize_schedule_time(payload.scheduled_at)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

        initial_status = CampaignStatus.SCHEDULED.value if normalized_sched else CampaignStatus.DRAFT.value

        await db.execute("""
            INSERT INTO campaigns (
                id, name, subject, template_id, list_id, smtp_config_id,
                sender_name, sender_email, reply_to, headers,
                track_opens, track_clicks, custom_html, custom_text,
                status, scheduled_at, started_at, completed_at,
                total_recipients, sent_count, delivered_count, failed_count,
                open_count, click_count, unsubscribe_count, bounce_count,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 0, 0, 0, 0, 0, 0, 0, ?, ?)
        """, (
            camp_id,
            campaign_name,
            payload.subject,
            payload.template_id,
            target_list_id,
            payload.smtp_config_id,
            sender_name,
            sender_email,
            payload.reply_to,
            json.dumps(payload.headers),
            1 if payload.track_opens else 0,
            1 if payload.track_clicks else 0,
            payload.custom_html,
            payload.custom_text,
            initial_status,
            normalized_sched,
            initial_recipients,
            now,
            now
        ))
        await db.commit()

    return CampaignResponse(
        id=camp_id,
        name=campaign_name,
        subject=payload.subject,
        template_id=payload.template_id,
        list_id=target_list_id,
        smtp_config_id=payload.smtp_config_id,
        sender_name=sender_name,
        sender_email=sender_email,
        reply_to=payload.reply_to,
        headers=payload.headers,
        track_opens=payload.track_opens,
        track_clicks=payload.track_clicks,
        custom_html=payload.custom_html,
        custom_text=payload.custom_text,
        status=CampaignStatus(initial_status),
        scheduled_at=normalized_sched,
        started_at=None,
        completed_at=None,
        total_recipients=initial_recipients,
        sent_count=0,
        delivered_count=0,
        failed_count=0,
        open_count=0,
        click_count=0,
        unsubscribe_count=0,
        bounce_count=0,
        created_at=now,
        updated_at=now
    )


@router.get("/scheduled", response_model=List[CampaignResponse])
async def list_scheduled_campaigns():
    """
    List all upcoming scheduled campaigns ordered by scheduled_at ascending.
    """
    async with get_db() as db:
        async with db.execute("""
            SELECT * FROM campaigns
            WHERE status = 'scheduled'
            ORDER BY scheduled_at ASC
        """) as cur:
            rows = await cur.fetchall()

    results = []
    for r in rows:
        headers = {}
        try:
            headers = json.loads(r["headers"] or "{}")
        except Exception:
            pass

        results.append(CampaignResponse(
            id=r["id"],
            name=r["name"],
            subject=r["subject"],
            template_id=r["template_id"],
            list_id=r["list_id"],
            smtp_config_id=r["smtp_config_id"],
            sender_name=r["sender_name"],
            sender_email=r["sender_email"],
            reply_to=r["reply_to"],
            headers=headers,
            track_opens=bool(r["track_opens"]),
            track_clicks=bool(r["track_clicks"]),
            custom_html=r["custom_html"],
            custom_text=r["custom_text"],
            status=CampaignStatus(r["status"]) if r["status"] in [s.value for s in CampaignStatus] else CampaignStatus.SCHEDULED,
            scheduled_at=r["scheduled_at"],
            started_at=r["started_at"],
            completed_at=r["completed_at"],
            total_recipients=r["total_recipients"],
            sent_count=r["sent_count"],
            delivered_count=r["delivered_count"],
            failed_count=r["failed_count"],
            open_count=r["open_count"],
            click_count=r["click_count"],
            unsubscribe_count=r["unsubscribe_count"],
            bounce_count=r["bounce_count"],
            created_at=r["created_at"],
            updated_at=r["updated_at"]
        ))
    return results


@router.get("/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(campaign_id: str):
    """
    Get campaign details and live progress metrics.
    """
    async with get_db() as db:
        async with db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Campaign not found")

            headers = {}
            try:
                raw_h = row["headers"] if "headers" in row.keys() else (row["headers_json"] if "headers_json" in row.keys() else None)
                if raw_h:
                    headers = json.loads(raw_h)
            except Exception:
                pass

            return CampaignResponse(
                id=row["id"],
                name=row["name"],
                subject=row["subject"],
                template_id=row["template_id"],
                list_id=row["list_id"],
                smtp_config_id=row["smtp_config_id"],
                sender_name=row["sender_name"],
                sender_email=row["sender_email"],
                reply_to=row["reply_to"],
                headers=headers,
                track_opens=bool(row["track_opens"]),
                track_clicks=bool(row["track_clicks"]),
                custom_html=row["custom_html"],
                custom_text=row["custom_text"],
                status=CampaignStatus(row["status"]),
                scheduled_at=row["scheduled_at"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                total_recipients=row["total_recipients"] or 0,
                sent_count=row["sent_count"] or 0,
                delivered_count=row["delivered_count"] or 0,
                failed_count=row["failed_count"] or 0,
                open_count=row["open_count"] or 0,
                click_count=row["click_count"] or 0,
                unsubscribe_count=row["unsubscribe_count"] or 0,
                bounce_count=row["bounce_count"] or 0,
                created_at=row["created_at"],
                updated_at=row["updated_at"]
            )


@router.get("/{campaign_id}/stats", response_model=CampaignStatsResponse)
async def get_campaign_stats(campaign_id: str):
    """
    Get full analytics report for a specific campaign.
    """
    async with get_db() as db:
        async with db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)) as cursor:
            camp = await cursor.fetchone()
            if not camp:
                raise HTTPException(status_code=404, detail="Campaign not found")
            camp_dict = dict(camp)

        async with db.execute("""
            SELECT 
                COUNT(DISTINCT sent_email_id) as unique_opens
            FROM email_events
            WHERE campaign_id = ? AND event_type = 'open'
        """, (campaign_id,)) as cursor:
            u_open_row = await cursor.fetchone()
            unique_opens = u_open_row[0] if u_open_row else 0

        async with db.execute("""
            SELECT 
                COUNT(DISTINCT sent_email_id) as unique_clicks
            FROM email_events
            WHERE campaign_id = ? AND event_type = 'click'
        """, (campaign_id,)) as cursor:
            u_click_row = await cursor.fetchone()
            unique_clicks = u_click_row[0] if u_click_row else 0

    total_rec = camp_dict["total_recipients"] or 0
    sent_cnt = camp_dict["sent_count"] or 0
    deliv_cnt = camp_dict["delivered_count"] or 0
    failed_cnt = camp_dict["failed_count"] or 0
    open_cnt = camp_dict["open_count"] or 0
    click_cnt = camp_dict["click_count"] or 0
    unsub_cnt = camp_dict["unsubscribe_count"] or 0
    bounce_cnt = camp_dict["bounce_count"] or 0

    denom = sent_cnt if sent_cnt > 0 else (total_rec if total_rec > 0 else 1)
    deliv_rate = round((deliv_cnt / denom) * 100, 2) if sent_cnt > 0 else 0.0
    open_rate = round((open_cnt / denom) * 100, 2) if sent_cnt > 0 else 0.0
    click_rate = round((click_cnt / denom) * 100, 2) if sent_cnt > 0 else 0.0
    bounce_rate = round((bounce_cnt / denom) * 100, 2) if sent_cnt > 0 else 0.0

    return CampaignStatsResponse(
        campaign_id=campaign_id,
        campaign_name=camp_dict["name"],
        status=CampaignStatus(camp_dict["status"]),
        total_recipients=total_rec,
        sent_count=sent_cnt,
        delivered_count=deliv_cnt,
        failed_count=failed_cnt,
        open_count=open_cnt,
        unique_opens=unique_opens or open_cnt,
        click_count=click_cnt,
        unique_clicks=unique_clicks or click_cnt,
        unsubscribe_count=unsub_cnt,
        bounce_count=bounce_cnt,
        delivery_rate_percent=deliv_rate,
        open_rate_percent=open_rate,
        click_through_rate_percent=click_rate,
        bounce_rate_percent=bounce_rate,
        started_at=camp_dict.get("started_at"),
        completed_at=camp_dict.get("completed_at")
    )


@router.put("/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(campaign_id: str, payload: CampaignUpdate):
    """
    Update editable fields of a campaign.
    """
    now = utc_now_iso()
    async with get_db() as db:
        async with db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Campaign not found")
            camp = dict(row)

        new_name = payload.name if payload.name is not None else camp["name"]
        new_subject = payload.subject if payload.subject is not None else camp["subject"]
        new_tpl = payload.template_id if payload.template_id is not None else camp["template_id"]
        new_list = payload.list_id if payload.list_id is not None else camp["list_id"]
        new_smtp = payload.smtp_config_id if payload.smtp_config_id is not None else camp["smtp_config_id"]
        new_sname = payload.sender_name if payload.sender_name is not None else camp["sender_name"]
        new_semail = payload.sender_email if payload.sender_email is not None else camp["sender_email"]
        new_reply = payload.reply_to if payload.reply_to is not None else camp["reply_to"]
        new_html = payload.custom_html if payload.custom_html is not None else camp["custom_html"]
        new_text = payload.custom_text if payload.custom_text is not None else camp["custom_text"]
        new_status = payload.status.value if payload.status is not None else camp["status"]
        new_sched = payload.scheduled_at if payload.scheduled_at is not None else camp["scheduled_at"]

        headers_str = camp["headers"] if "headers" in camp else "{}"
        if payload.headers is not None:
            headers_str = json.dumps(payload.headers)

        track_o = camp["track_opens"] if payload.track_opens is None else (1 if payload.track_opens else 0)
        track_c = camp["track_clicks"] if payload.track_clicks is None else (1 if payload.track_clicks else 0)

        await db.execute("""
            UPDATE campaigns
            SET name = ?, subject = ?, template_id = ?, list_id = ?, smtp_config_id = ?,
                sender_name = ?, sender_email = ?, reply_to = ?, headers = ?,
                track_opens = ?, track_clicks = ?, custom_html = ?, custom_text = ?,
                status = ?, scheduled_at = ?, updated_at = ?
            WHERE id = ?
        """, (
            new_name, new_subject, new_tpl, new_list, new_smtp,
            new_sname, new_semail, new_reply, headers_str,
            track_o, track_c, new_html, new_text,
            new_status, new_sched, now, campaign_id
        ))
        await db.commit()

        return await get_campaign(campaign_id)


@router.delete("/{campaign_id}")
async def delete_campaign(campaign_id: str):
    """
    Delete a campaign and dissociate sent email records.
    """
    if campaign_queue.is_running(campaign_id):
        await campaign_queue.cancel_campaign(campaign_id)

    async with get_db() as db:
        async with db.execute("SELECT id FROM campaigns WHERE id = ?", (campaign_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Campaign not found")

        await db.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
        await db.commit()

    return {"success": True, "message": f"Campaign {campaign_id} deleted."}


# ======================================================================
# Campaign Execution Controls (Test Send, Launch, Pause, Resume, Cancel)
# ======================================================================

@router.post("/{campaign_id}/test-send")
async def test_send_campaign(campaign_id: str, payload: TestSendRequest):
    """
    Send a single test preview to a specific test email.
    """
    async with get_db() as db:
        async with db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)) as cursor:
            camp = await cursor.fetchone()
            if not camp:
                raise HTTPException(status_code=404, detail="Campaign not found")
            camp_dict = dict(camp)

    sample_vars = {
        "first_name": "Test",
        "last_name": "User",
        "name": "Test User",
        "email": payload.test_email,
        "company": "NexusMail Preview Desk",
        "plan": "Enterprise Pro",
        **payload.sample_variables
    }

    result = await send_single_email(
        recipient_email=payload.test_email,
        recipient_name="Test Recipient",
        subject=f"[PREVIEW TEST] {camp_dict['subject']}",
        body_html=camp_dict["custom_html"],
        body_text=camp_dict["custom_text"],
        sender_email=camp_dict["sender_email"],
        sender_name=camp_dict["sender_name"],
        reply_to=camp_dict["reply_to"],
        template_id=camp_dict["template_id"],
        campaign_id=campaign_id,
        smtp_config_id=camp_dict["smtp_config_id"],
        merge_variables=sample_vars,
        track_opens=False,
        track_clicks=False
    )

    return {
        "success": result["success"],
        "message": f"Test preview dispatched to {payload.test_email}",
        "sent_email_id": result["sent_email_id"],
        "status": result["status"],
        "error": result.get("error")
    }


@router.post("/{campaign_id}/launch")
async def launch_campaign(campaign_id: str):
    """
    Queue and begin asynchronous execution of mass email campaign.
    """
    async with get_db() as db:
        async with db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)) as cursor:
            camp = await cursor.fetchone()
            if not camp:
                raise HTTPException(status_code=404, detail="Campaign not found")

    result = await campaign_queue.launch_campaign(campaign_id)
    return result


@router.post("/{campaign_id}/pause")
async def pause_campaign(campaign_id: str):
    """
    Pause a running mass campaign.
    """
    return await campaign_queue.pause_campaign(campaign_id)


@router.post("/{campaign_id}/resume")
async def resume_campaign(campaign_id: str):
    """
    Resume a paused mass campaign.
    """
    return await campaign_queue.resume_campaign(campaign_id)


@router.post("/{campaign_id}/cancel")
async def cancel_campaign(campaign_id: str):
    """
    Cancel an ongoing or scheduled mass campaign.
    """
    return await campaign_queue.cancel_campaign(campaign_id)


class ScheduleCampaignRequest(BaseModel):
    scheduled_at: str = Field(..., description="Target ISO or formatted timestamp for scheduled dispatch")


@router.post("/{campaign_id}/schedule")
async def schedule_campaign(campaign_id: str, payload: ScheduleCampaignRequest):
    """
    Schedule or reschedule an existing campaign.
    """
    try:
        normalized = parse_and_normalize_schedule_time(payload.scheduled_at)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    now = utc_now_iso()
    async with get_db() as db:
        async with db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Campaign not found.")
            camp = dict(row)

        if camp["status"] in ("sending", "completed"):
            raise HTTPException(status_code=400, detail=f"Cannot schedule campaign with status '{camp['status']}'.")

        await db.execute("""
            UPDATE campaigns
            SET status = 'scheduled',
                scheduled_at = ?,
                updated_at = ?
            WHERE id = ?
        """, (normalized, now, campaign_id))
        await db.commit()

    return {
        "success": True,
        "campaign_id": campaign_id,
        "status": "scheduled",
        "scheduled_at": normalized,
        "message": f"Campaign successfully scheduled for {normalized} UTC."
    }


@router.post("/{campaign_id}/unschedule")
async def unschedule_campaign(campaign_id: str):
    """
    Cancel scheduling and revert campaign status to draft.
    """
    now = utc_now_iso()
    async with get_db() as db:
        async with db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Campaign not found.")
            camp = dict(row)

        if camp["status"] != "scheduled":
            raise HTTPException(status_code=400, detail=f"Campaign is not scheduled (current status: '{camp['status']}').")

        await db.execute("""
            UPDATE campaigns
            SET status = 'draft',
                scheduled_at = NULL,
                updated_at = ?
            WHERE id = ?
        """, (now, campaign_id))
        await db.commit()

    return {
        "success": True,
        "campaign_id": campaign_id,
        "status": "draft",
        "scheduled_at": None,
        "message": "Campaign schedule cancelled. Status reverted to draft."
    }


# ======================================================================
# Quick Customer Mass Broadcast Endpoint
# ======================================================================

import re
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
NAME_EMAIL_REGEX = re.compile(r'^(?:"?([^"<]+)"?\s*)?<([^>]+)>$')

def parse_customer_emails(raw_text: str) -> List[Dict[str, str]]:
    """Extract email & display name from arbitrary delimiter-separated text."""
    if not raw_text:
        return []
    normalized = re.sub(r'[\r\n;]+', ',', raw_text)
    raw_tokens = [t.strip() for t in normalized.split(',') if t.strip()]
    results = []
    seen = set()
    for token in raw_tokens:
        m = NAME_EMAIL_REGEX.match(token)
        if m:
            name_part = (m.group(1) or "").strip()
            email_part = m.group(2).strip().lower()
        else:
            if "<" in token and ">" in token:
                parts = token.split("<")
                name_part = parts[0].strip().strip('"')
                email_part = parts[1].split(">")[0].strip().lower()
            else:
                name_part = ""
                email_part = token.strip().strip('"').lower()
        
        if email_part and EMAIL_REGEX.match(email_part):
            if email_part not in seen:
                seen.add(email_part)
                first_name = ""
                last_name = ""
                if name_part:
                    words = name_part.split()
                    first_name = words[0]
                    last_name = " ".join(words[1:]) if len(words) > 1 else ""
                else:
                    first_name = email_part.split("@")[0].capitalize()
                
                results.append({
                    "email": email_part,
                    "first_name": first_name,
                    "last_name": last_name,
                    "name": name_part or first_name
                })
    return results


class QuickBroadcastPayload(BaseModel):
    subject: str = Field(..., description="Email Subject")
    body_html: str = Field(..., description="HTML / Rich message content")
    body_text: Optional[str] = Field(default=None, description="Plain text fallback")
    name: Optional[str] = Field(default=None, description="Campaign title")
    sender_name: Optional[str] = Field(default=None, description="Sender Display Name")
    sender_email: Optional[str] = Field(default=None, description="Sender Email Address")
    reply_to: Optional[str] = Field(default=None, description="Reply-to Email Address")
    smtp_config_id: Optional[str] = Field(default=None, description="Specific SMTP Relay ID")
    recipients_text: Optional[str] = Field(default=None, description="Raw pasted customer emails")
    list_id: Optional[str] = Field(default=None, description="Target list ID or 'all'")
    rate_limit_per_second: int = Field(default=25, description="Emails dispatched per second")
    track_opens: bool = Field(default=True, description="Inject open tracking beacon")
    track_clicks: bool = Field(default=True, description="Rewrite hyperlinks for click tracking")
    scheduled_at: Optional[str] = Field(default=None, description="Target ISO timestamp for scheduled dispatch")


@router.post("/quick-broadcast")
async def quick_broadcast_send(payload: QuickBroadcastPayload):
    """
    Instantly dispatch a message to multiple customer emails (either pasted directly or from audience lists).
    Archives all sent emails to the Email Storage Vault and executes with rate limiting.
    """
    from app.models import SMTPConfig, Subscriber

    target_subscribers: List[Subscriber] = []
    now = utc_now_iso()

    # 1. Parse pasted customer emails if provided
    if payload.recipients_text and payload.recipients_text.strip():
        parsed_entries = parse_customer_emails(payload.recipients_text)
        if not parsed_entries:
            raise HTTPException(status_code=400, detail="No valid customer email addresses found in the provided recipients text.")
        
        async with get_db() as db:
            for item in parsed_entries:
                email_str = item["email"]
                first_name = item["first_name"]
                last_name = item["last_name"]

                async with db.execute("SELECT * FROM subscribers WHERE email = ?", (email_str,)) as cur:
                    row = await cur.fetchone()

                if row:
                    sub_id = row["id"]
                    cf = {}
                    try:
                        cf = json.loads(row["custom_fields"] or "{}")
                    except Exception:
                        pass
                    target_subscribers.append(Subscriber(
                        id=sub_id,
                        email=email_str,
                        first_name=row["first_name"] or first_name,
                        last_name=row["last_name"] or last_name,
                        custom_attributes=cf
                    ))
                else:
                    sub_id = f"sub_{uuid.uuid4().hex[:10]}"
                    await db.execute("""
                        INSERT INTO subscribers (
                            id, email, first_name, last_name, status,
                            tags, custom_fields, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'active', '["quick-broadcast"]', '{}', ?, ?)
                    """, (sub_id, email_str, first_name, last_name, now, now))
                    
                    target_subscribers.append(Subscriber(
                        id=sub_id,
                        email=email_str,
                        first_name=first_name,
                        last_name=last_name,
                        custom_attributes={}
                    ))
            await db.commit()

    # 2. Or pull from list_id / all customers
    elif payload.list_id and payload.list_id.lower() != "all":
        async with get_db() as db:
            async with db.execute("""
                SELECT s.* FROM subscribers s
                JOIN subscriber_list_memberships m ON s.id = m.subscriber_id
                WHERE m.list_id = ? AND s.status = 'active'
            """, (payload.list_id,)) as cur:
                rows = await cur.fetchall()
                for r in rows:
                    cf = {}
                    try:
                        cf = json.loads(r["custom_fields"] or "{}")
                    except Exception:
                        pass
                    target_subscribers.append(Subscriber(
                        id=r["id"],
                        email=r["email"],
                        first_name=r["first_name"],
                        last_name=r["last_name"],
                        custom_attributes=cf
                    ))
    else:
        # Pull all active customers
        async with get_db() as db:
            async with db.execute("SELECT * FROM subscribers WHERE status = 'active'") as cur:
                rows = await cur.fetchall()
                for r in rows:
                    cf = {}
                    try:
                        cf = json.loads(r["custom_fields"] or "{}")
                    except Exception:
                        pass
                    target_subscribers.append(Subscriber(
                        id=r["id"],
                        email=r["email"],
                        first_name=r["first_name"],
                        last_name=r["last_name"],
                        custom_attributes=cf
                    ))

    if not target_subscribers:
        raise HTTPException(
            status_code=400,
            detail="No active customer recipients found. Please add or paste customer email addresses."
        )

    # 3. Load SMTP configuration
    smtp_cfg_obj = None
    async with get_db() as db:
        if payload.smtp_config_id:
            async with db.execute("SELECT * FROM smtp_configs WHERE id = ?", (payload.smtp_config_id,)) as cur:
                smtp_row = await cur.fetchone()
        else:
            async with db.execute("SELECT * FROM smtp_configs WHERE is_default = 1 ORDER BY updated_at DESC LIMIT 1") as cur:
                smtp_row = await cur.fetchone()
            if not smtp_row:
                async with db.execute("SELECT * FROM smtp_configs ORDER BY updated_at DESC LIMIT 1") as cur:
                    smtp_row = await cur.fetchone()

        if smtp_row:
            smtp_dict = dict(smtp_row)
            host = smtp_dict.get("host") or ""
            # Only the reserved 'sandbox' host is a dry run; a loopback relay is a real one.
            is_sand = host.strip().lower() == "sandbox"
            smtp_cfg_obj = SMTPConfig(
                id=smtp_dict.get("id"),
                name=smtp_dict.get("name", "SMTP Profile"),
                host=host,
                port=smtp_dict.get("port", 587),
                username=smtp_dict.get("username"),
                password=(__import__("app.auth", fromlist=["decrypt_credential"]).decrypt_credential(smtp_dict.get("password") or "") if smtp_dict.get("password") else None),
                use_tls=bool(smtp_dict.get("use_tls", 1)),
                use_ssl=bool(smtp_dict.get("use_ssl", 0)),
                is_sandbox=is_sand,
                simulated_delay_sec=0.02
            )

    campaign_name = payload.name or f"Broadcast - {payload.subject[:35]} ({len(target_subscribers)} customers)"
    sender_name = payload.sender_name or settings.DEFAULT_SENDER_NAME
    sender_email = payload.sender_email or settings.DEFAULT_SENDER_EMAIL

    normalized_sched = None
    if payload.scheduled_at:
        try:
            normalized_sched = parse_and_normalize_schedule_time(payload.scheduled_at)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # 4. Create and persist campaign
    campaign = await campaign_queue.create_campaign(
        name=campaign_name,
        subject=payload.subject,
        template_html=payload.body_html,
        template_text=payload.body_text,
        sender_name=sender_name,
        sender_email=sender_email,
        smtp_config=smtp_cfg_obj,
        rate_limit_per_sec=payload.rate_limit_per_second,
        concurrency_limit=10,
        recipients=target_subscribers
    )

    if normalized_sched:
        # Schedule for automated future dispatch
        now_ts = utc_now_iso()
        async with get_db() as db:
            await db.execute("""
                UPDATE campaigns
                SET status = 'scheduled',
                    scheduled_at = ?,
                    updated_at = ?
                WHERE id = ?
            """, (normalized_sched, now_ts, campaign.id))
            await db.commit()

        return {
            "success": True,
            "campaign_id": campaign.id,
            "name": campaign_name,
            "total_recipients": len(target_subscribers),
            "status": "scheduled",
            "scheduled_at": normalized_sched,
            "sender": f"{sender_name} <{sender_email}>",
            "message": f"Broadcast successfully scheduled for {normalized_sched} UTC."
        }

    # 5. Launch background execution worker immediately
    worker = await campaign_queue.start_campaign(campaign.id, recipients=target_subscribers)

    return {
        "success": True,
        "campaign_id": campaign.id,
        "name": campaign_name,
        "total_recipients": len(target_subscribers),
        "status": "sending",
        "sender": f"{sender_name} <{sender_email}>",
        "message": f"Broadcast successfully launched to {len(target_subscribers)} customer email addresses."
    }

