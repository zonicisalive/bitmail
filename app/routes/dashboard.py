"""
Dashboard routes for KPI telemetry, activity feed, and deliverability analytics chart data.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import json

from fastapi import APIRouter, Query

from app.db import get_db

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard Analytics"])


@router.get("/stats")
async def get_dashboard_stats() -> Dict[str, Any]:
    """
    Retrieve real-time KPI metrics: Total Sent, Delivered %, Open %, Click %,
    Active Subscribers, Bounced, Unsubscribed, Failed, Total Stored Emails.
    """
    async with get_db() as db:
        async with db.execute("""
            SELECT 
                COUNT(*) as total_stored,
                SUM(CASE WHEN status IN ('sent', 'delivered', 'failed', 'bounced') THEN 1 ELSE 0 END) as total_sent,
                SUM(CASE WHEN status IN ('sent', 'delivered') THEN 1 ELSE 0 END) as delivered_count,
                SUM(CASE WHEN status = 'simulated' THEN 1 ELSE 0 END) as simulated_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                SUM(CASE WHEN status = 'bounced' THEN 1 ELSE 0 END) as bounced_count,
                SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) as queued_count,
                SUM(CASE WHEN open_count > 0 THEN 1 ELSE 0 END) as opened_count,
                SUM(CASE WHEN click_count > 0 THEN 1 ELSE 0 END) as clicked_count,
                SUM(open_count) as total_opens,
                SUM(click_count) as total_clicks
            FROM sent_emails
        """) as cursor:
            row = await cursor.fetchone()
            total_stored = row["total_stored"] or 0
            total_sent = row["total_sent"] or 0
            delivered_count = row["delivered_count"] or 0
            failed_count = row["failed_count"] or 0
            bounced_count = row["bounced_count"] or 0
            queued_count = row["queued_count"] or 0
            simulated_count = row["simulated_count"] or 0
            opened_count = row["opened_count"] or 0
            clicked_count = row["clicked_count"] or 0

        async with db.execute("""
            SELECT 
                COUNT(*) as total_subs,
                SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active_subs,
                SUM(CASE WHEN status = 'unsubscribed' THEN 1 ELSE 0 END) as unsub_subs,
                SUM(CASE WHEN status = 'bounced' THEN 1 ELSE 0 END) as bounced_subs
            FROM subscribers
        """) as cursor:
            sub_row = await cursor.fetchone()
            total_subs = sub_row["total_subs"] or 0
            active_subs = sub_row["active_subs"] or 0
            unsub_subs = sub_row["unsub_subs"] or 0
            bounced_subs = sub_row["bounced_subs"] or 0

        async with db.execute("SELECT COUNT(*) FROM suppressions") as cursor:
            sup_row = await cursor.fetchone()
            suppressions_count = sup_row[0] if sup_row else 0

        # Which relay a campaign launched right now would actually go out through.
        async with db.execute("""
            SELECT name, host, port FROM smtp_configs
            WHERE is_active = 1 ORDER BY is_default DESC, updated_at DESC LIMIT 1
        """) as cursor:
            relay_row = await cursor.fetchone()
        relay = {"configured": False, "name": None, "host": None, "port": None, "is_sandbox": False}
        if relay_row:
            host = (relay_row["host"] or "").strip()
            relay = {
                "configured": bool(host),
                "name": relay_row["name"],
                "host": host,
                "port": relay_row["port"],
                "is_sandbox": host.lower() == "sandbox",
            }

        denominator = total_sent or 1
        delivery_rate = round((delivered_count / denominator) * 100, 1)
        open_rate = round((opened_count / denominator) * 100, 1)
        click_rate = round((clicked_count / denominator) * 100, 1)
        bounce_rate = round((bounced_count / denominator) * 100, 1)

        # Recent failure details so dashboard can explain rejections directly
        recent_failures = []
        async with db.execute("""
            SELECT id, recipient_email, recipient_name, subject, error_message, created_at, status
            FROM sent_emails
            WHERE status IN ('failed', 'bounced')
            ORDER BY created_at DESC LIMIT 5
        """) as cursor:
            fail_rows = await cursor.fetchall()
            for fr in fail_rows:
                recent_failures.append({
                    "id": fr["id"],
                    "recipient": fr["recipient_email"],
                    "recipient_name": fr["recipient_name"],
                    "subject": fr["subject"],
                    "error": fr["error_message"] or "Relay rejected delivery",
                    "created_at": fr["created_at"],
                    "status": fr["status"]
                })

        return {
            "total_sent": total_sent,
            "delivered_count": delivered_count,
            "delivery_rate": delivery_rate,
            "open_rate": open_rate,
            "click_rate": click_rate,
            "bounce_rate": bounce_rate,
            "active_subscribers": active_subs,
            "bounced_count": bounced_count + bounced_subs,
            "unsubscribed_count": unsub_subs + suppressions_count,
            "failed_count": failed_count,
            "queued_count": queued_count,
            "simulated_count": simulated_count,
            "attempted_count": total_sent,
            "suppressed_count": suppressions_count,
            "relay": relay,
            "total_stored_emails": total_stored,
            "total_subscribers": total_subs,
            "latest_failure": recent_failures[0] if recent_failures else None,
            "recent_failures": recent_failures,
            "totalSent": total_sent,
            "deliveryRate": delivery_rate,
            "openRate": open_rate,
            "clickRate": click_rate,
            "activeSubscribers": active_subs,
            "bouncedCount": bounced_count + bounced_subs,
            "unsubscribedCount": unsub_subs + suppressions_count,
            "failedCount": failed_count,
            "queuedCount": queued_count,
            "simulatedCount": simulated_count,
            "totalStoredEmails": total_stored
        }


@router.get("/activity")
async def get_dashboard_activity(limit: int = Query(default=50, ge=1, le=200)) -> List[Dict[str, Any]]:
    """
    Retrieve real-time event activity stream from Sent Email archive and tracking logs.
    """
    activity_items: List[Dict[str, Any]] = []

    async with get_db() as db:
        query = """
            SELECT 
                e.id as event_id,
                e.sent_email_id,
                e.campaign_id,
                e.event_type,
                e.ip_address,
                e.user_agent,
                e.event_payload,
                e.created_at as event_time,
                s.recipient_email,
                s.recipient_name,
                s.subject,
                s.status as email_status,
                c.name as campaign_name
            FROM email_events e
            LEFT JOIN sent_emails s ON e.sent_email_id = s.id
            LEFT JOIN campaigns c ON e.campaign_id = c.id
            ORDER BY e.created_at DESC
            LIMIT ?
        """
        async with db.execute(query, (limit,)) as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                payload = {}
                try:
                    if r["event_payload"]:
                        payload = json.loads(r["event_payload"])
                except Exception:
                    pass

                ev_type = r["event_type"]
                desc = f"Email {ev_type}"
                status_badge = "success"

                if ev_type == "open":
                    desc = f"Email opened by {r['recipient_email'] or 'recipient'}"
                    status_badge = "opened"
                elif ev_type == "click":
                    target = payload.get("target_url") or "Link"
                    desc = f"Clicked link: {target}"
                    status_badge = "clicked"
                elif ev_type == "delivered":
                    desc = f"Delivered to {r['recipient_email'] or 'recipient'}"
                    status_badge = "success"
                elif ev_type == "sent":
                    desc = f"Dispatched via SMTP to {r['recipient_email'] or 'recipient'}"
                    status_badge = "success"
                elif ev_type == "queued":
                    desc = f"Queued for dispatch: {r['recipient_email'] or 'recipient'}"
                    status_badge = "info"
                elif ev_type == "bounce" or ev_type == "bounced":
                    reason = payload.get("reason") or "Mailbox unavailable"
                    desc = f"Bounced: {reason}"
                    status_badge = "warning"
                elif ev_type == "failed":
                    err = payload.get("error") or "Transmission failed"
                    desc = f"Failed: {err}"
                    status_badge = "error"
                elif ev_type == "unsubscribe":
                    desc = f"Subscriber unsubscribed: {r['recipient_email']}"
                    status_badge = "warning"

                activity_items.append({
                    "id": r["event_id"],
                    "sent_email_id": r["sent_email_id"],
                    "campaign_id": r["campaign_id"],
                    "campaign_name": r["campaign_name"] or "Transactional Send",
                    "event_type": ev_type,
                    "event": desc,
                    "recipient": r["recipient_email"],
                    "recipient_name": r["recipient_name"],
                    "subject": r["subject"],
                    "ip_address": r["ip_address"],
                    "user_agent": r["user_agent"],
                    "timestamp": r["event_time"],
                    "status": status_badge,
                    "details": payload
                })

    return activity_items


@router.get("/chart")
async def get_dashboard_chart(days: int = Query(default=14, ge=1, le=90)) -> Dict[str, Any]:
    """
    Retrieve deliverability & timeline chart data grouped by day for the last N days.
    """
    today = datetime.now(timezone.utc).date()
    dates_list = [(today - timedelta(days=i)) for i in range(days - 1, -1, -1)]
    date_labels = [d.strftime("%b %d") for d in dates_list]
    date_keys = [d.strftime("%Y-%m-%d") for d in dates_list]

    sent_data = [0] * len(dates_list)
    delivered_data = [0] * len(dates_list)
    opened_data = [0] * len(dates_list)
    clicked_data = [0] * len(dates_list)
    bounced_data = [0] * len(dates_list)
    failed_data = [0] * len(dates_list)

    date_idx_map = {date_keys[i]: i for i in range(len(date_keys))}
    cutoff_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")

    async with get_db() as db:
        async with db.execute("""
            SELECT 
                SUBSTR(created_at, 1, 10) as day_str,
                COUNT(*) as total_sent,
                SUM(CASE WHEN status IN ('sent', 'delivered') THEN 1 ELSE 0 END) as delivered,
                SUM(CASE WHEN open_count > 0 THEN 1 ELSE 0 END) as opened,
                SUM(CASE WHEN click_count > 0 THEN 1 ELSE 0 END) as clicked,
                SUM(CASE WHEN status = 'bounced' THEN 1 ELSE 0 END) as bounced,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
            FROM sent_emails
            WHERE created_at >= ?
            GROUP BY SUBSTR(created_at, 1, 10)
        """, (cutoff_date,)) as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                d = r["day_str"]
                if d in date_idx_map:
                    idx = date_idx_map[d]
                    sent_data[idx] = r["total_sent"] or 0
                    delivered_data[idx] = r["delivered"] or 0
                    opened_data[idx] = r["opened"] or 0
                    clicked_data[idx] = r["clicked"] or 0
                    bounced_data[idx] = r["bounced"] or 0
                    failed_data[idx] = r["failed"] or 0

    return {
        "labels": date_labels,
        "has_data": sum(sent_data) > 0,
        "date_keys": date_keys,
        "datasets": {
            "sent": sent_data,
            "delivered": delivered_data,
            "opened": opened_data,
            "clicked": clicked_data,
            "bounced": bounced_data,
            "failed": failed_data
        }
    }
