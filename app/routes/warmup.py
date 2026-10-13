"""
Automated Email Warmup & Multi-Relay Rotation Router.
Provides endpoints for previewing ramp-up curves, managing warmup schedules,
monitoring daily slices, and controlling the active SMTP relay rotation pool.
"""

from datetime import datetime, timedelta, timezone
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.auth import current_user_id
from app.db import get_db, utc_now_iso
from app.models import (
    Campaign,
    CampaignStatus,
    RelayPoolStatusResponse,
    WarmupPreviewRequest,
    WarmupPreviewResponse,
    WarmupPreviewSlice,
    WarmupScheduleCreate,
    WarmupScheduleResponse,
    WarmupSliceResponse,
)
from app.warmup import (
    RelayPoolManager,
    WarmupScheduleCurves,
    WarmupSlicer,
)

router = APIRouter(prefix="/api/warmup", tags=["Email Warmup & Relay Rotation"])


# ==============================================================================
# 1. Warmup Preview Simulator Endpoint
# ==============================================================================
# Used only when there are no contacts to measure: a rough consumer-list average.
TYPICAL_PROVIDER_MIX = {"gmail": 0.45, "microsoft": 0.30, "yahoo": 0.15, "corporate": 0.10}


async def contacts_provider_mix(list_id: Optional[str]) -> Tuple[Dict[str, float], int]:
    """Share of each mailbox provider among the user's active contacts (optionally one list)."""
    sql = "SELECT s.email FROM subscribers s WHERE s.status = 'active' AND s.user_id = ?"
    params: List[Any] = [current_user_id()]
    if list_id and list_id != "all":
        sql += " AND EXISTS (SELECT 1 FROM subscriber_list_memberships m WHERE m.subscriber_id = s.id AND m.list_id = ?)"
        params.append(list_id)
    async with get_db() as db:
        async with db.execute(sql, params) as cur:
            emails = [r[0] for r in await cur.fetchall()]
    if not emails:
        return {}, 0
    counts: Dict[str, int] = {}
    for e in emails:
        prov = WarmupSlicer.identify_provider(e)
        # The table has four columns; Apple mail is grouped with corporate/other.
        prov = "corporate" if prov == "apple" else prov
        counts[prov] = counts.get(prov, 0) + 1
    return {k: v / len(emails) for k, v in counts.items()}, len(emails)


def split_by_mix(cap: int, mix: Dict[str, float]) -> Dict[str, int]:
    """Split a day's cap by provider share; rounding remainder goes to the largest shares."""
    keys = ["gmail", "microsoft", "yahoo", "corporate"]
    exact = {k: cap * mix.get(k, 0.0) for k in keys}
    out = {k: int(exact[k]) for k in keys}
    leftover = cap - sum(out.values())
    for k in sorted(keys, key=lambda k: exact[k] - out[k], reverse=True)[:leftover]:
        out[k] += 1
    return out


@router.post("/preview", response_model=WarmupPreviewResponse)
async def preview_warmup_curve(payload: WarmupPreviewRequest):
    """
    Simulate a warmup ramp-up schedule for a given audience size and curve strategy.

    The per-provider columns use the real provider mix of your active contacts (or of
    payload.list_id) when there are any; the old fixed 45/30/15/10 split made a list
    of 50 Gmail addresses look like only 22 would go to Gmail on day 1.
    """
    daily_caps = WarmupScheduleCurves.get_curve_caps(
        strategy=payload.strategy,
        total_recipients=payload.total_recipients,
        custom_days=payload.custom_days or 14,
        custom_start_cap=payload.custom_start_cap or 50,
        custom_growth_pct=payload.custom_growth_pct,
        custom_max_daily=payload.custom_max_daily,
    )

    mix, sample = await contacts_provider_mix(payload.list_id)
    source = "contacts" if sample else "typical"
    if not sample:
        mix = dict(TYPICAL_PROVIDER_MIX)

    today = datetime.now(timezone.utc)
    cumulative = 0
    slices: List[WarmupPreviewSlice] = []
    for day_idx, cap in enumerate(daily_caps, start=1):
        cumulative += cap
        slices.append(WarmupPreviewSlice(
            day=day_idx,
            date=(today + timedelta(days=day_idx - 1)).strftime("%Y-%m-%d"),
            daily_cap=cap,
            cumulative_volume=cumulative,
            recommended_providers=split_by_mix(cap, mix),
        ))

    return WarmupPreviewResponse(
        strategy=payload.strategy,
        total_days=len(daily_caps),
        total_recipients=payload.total_recipients,
        slices=slices,
        provider_mix={k: round(v, 4) for k, v in mix.items()},
        provider_mix_source=source,
        provider_mix_sample=sample,
    )


def _live_progress(s, slices: List[WarmupSliceResponse]) -> Dict[str, Any]:
    """Day, cap, sent count and status as the slices stand now; the stored columns are never advanced."""
    if not slices:
        return {"current_day": s["current_day"], "daily_cap": s["daily_cap"], "sent_today": s["sent_today"] or 0, "status": s["status"]}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    due = [sl for sl in slices if sl.scheduled_for <= now] or slices[:1]
    today = due[-1]
    finished = all(sl.status in ("completed", "failed") for sl in slices)
    return {
        "current_day": today.day_number,
        "daily_cap": today.target_count,
        "sent_today": today.dispatched_count,
        "status": "completed" if finished and s["status"] == "active" else s["status"],
    }


# ==============================================================================
# 2. Schedules CRUD Endpoints
# ==============================================================================
@router.get("/schedules", response_model=List[WarmupScheduleResponse])
async def list_warmup_schedules(
    status_filter: Optional[str] = Query(default=None, alias="status")
):
    """
    List all automated warmup schedules with current day progression and daily caps.
    """
    async with get_db() as db:
        query = "SELECT * FROM warmup_schedules WHERE user_id = ?"
        params: List[Any] = [current_user_id()]
        if status_filter:
            query += " AND status = ?"
            params.append(status_filter.lower())
        query += " ORDER BY created_at DESC"

        async with db.execute(query, params) as cur:
            schedule_rows = await cur.fetchall()

        results: List[WarmupScheduleResponse] = []
        for s in schedule_rows:
            # Fetch slices for this schedule
            async with db.execute(
                "SELECT * FROM warmup_slices WHERE schedule_id = ? ORDER BY day_number ASC",
                (s["id"],)
            ) as slice_cur:
                slice_rows = await slice_cur.fetchall()

            slices = [
                WarmupSliceResponse(
                    id=sr["id"],
                    schedule_id=sr["schedule_id"],
                    day_number=sr["day_number"],
                    scheduled_for=sr["scheduled_for"],
                    target_count=sr["target_count"],
                    dispatched_count=sr["dispatched_count"] or 0,
                    bounce_count=sr["bounce_count"] or 0,
                    failure_count=sr["failure_count"] or 0,
                    campaign_id=sr["campaign_id"],
                    status=sr["status"],
                    created_at=sr["created_at"]
                )
                for sr in slice_rows
            ]

            pool_ids = []
            try:
                pool_ids = json.loads(s["relay_pool_json"] or "[]")
            except Exception:
                pass

            results.append(WarmupScheduleResponse(
                id=s["id"],
                name=s["name"],
                campaign_id=s["campaign_id"],
                strategy=s["strategy"],
                total_recipients=s["total_recipients"],
                total_days=s["total_days"],
                **_live_progress(s, slices),
                relay_pool=pool_ids,
                rotation_mode=s["rotation_mode"],
                max_bounce_rate=s["max_bounce_rate"] or 0.02,
                slices=slices,
                created_at=s["created_at"],
                updated_at=s["updated_at"]
            ))

        return results


@router.post("/schedules", response_model=WarmupScheduleResponse, status_code=status.HTTP_201_CREATED)
async def create_warmup_schedule(payload: WarmupScheduleCreate):
    """
    Create a new warmup schedule:
    - Resolves target recipients (from direct list, list_id, or count)
    - Partitions audience into daily slices with provider round-robin balancing
    - Automatically creates scheduled campaign records for each slice
    """
    schedule_id = f"wup_{uuid.uuid4().hex[:10]}"
    now = utc_now_iso()
    uid = current_user_id()

    async with get_db() as db:
        if payload.campaign_id:
            async with db.execute("SELECT 1 FROM campaigns WHERE id = ? AND user_id = ?", (payload.campaign_id, uid)) as cur:
                if not await cur.fetchone():
                    raise HTTPException(status_code=404, detail="Campaign not found")
        if payload.list_id and payload.list_id != "all":
            async with db.execute("SELECT 1 FROM subscriber_lists WHERE id = ? AND user_id = ?", (payload.list_id, uid)) as cur:
                if not await cur.fetchone():
                    raise HTTPException(status_code=404, detail="List not found")
        for relay_id in payload.relay_ids or []:
            async with db.execute("SELECT 1 FROM smtp_configs WHERE id = ? AND user_id = ?", (relay_id, uid)) as cur:
                if not await cur.fetchone():
                    raise HTTPException(status_code=404, detail="SMTP relay not found")

    # 1. Resolve recipients
    recipients_list: List[Dict[str, Any]] = []
    if payload.recipient_emails:
        recipients_list = [{"email": e.strip()} for e in payload.recipient_emails if "@" in e]
    elif payload.list_id or payload.total_recipients is None:
        # No list means "all active contacts". It used to fall through to made-up
        # warmup_N@example.com placeholders.
        sql = "SELECT s.id, s.email, s.first_name, s.last_name, s.custom_fields FROM subscribers s WHERE s.status = 'active' AND s.user_id = ?"
        params: List[Any] = [uid]
        if payload.list_id and payload.list_id != "all":
            sql += " AND EXISTS (SELECT 1 FROM subscriber_list_memberships m WHERE m.subscriber_id = s.id AND m.list_id = ?)"
            params.append(payload.list_id)
        async with get_db() as db:
            async with db.execute(sql, params) as cur:
                recipients_list = [dict(r) for r in await cur.fetchall()]
        if not recipients_list:
            raise HTTPException(status_code=400, detail="No active contacts to warm up. Add contacts or pick another list.")

    total_recipients = len(recipients_list) if recipients_list else (payload.total_recipients or 100)

    # 2. Generate curve
    daily_caps = WarmupScheduleCurves.get_curve_caps(
        strategy=payload.strategy,
        total_recipients=total_recipients,
        custom_start_cap=payload.custom_start_cap or 50,
        custom_growth_pct=payload.custom_growth_pct,
        custom_max_daily=payload.custom_max_daily,
    )
    total_days = len(daily_caps)
    day_1_cap = daily_caps[0] if daily_caps else 50

    # 3. Partition recipients with provider balancing
    start_dt = None
    if payload.start_date:
        try:
            start_dt = datetime.fromisoformat(payload.start_date.replace("Z", "+00:00"))
        except Exception:
            pass

    slices_data = WarmupSlicer.balance_and_slice(
        recipients=recipients_list if recipients_list else [{"email": f"warmup_{i}@example.com"} for i in range(total_recipients)],
        daily_caps=daily_caps,
        start_datetime=start_dt
    )

    relay_pool = payload.relay_ids or []
    async with get_db() as db:
        # Create Schedule record
        await db.execute("""
            INSERT INTO warmup_schedules (
                id, name, campaign_id, strategy, total_recipients, current_day,
                total_days, daily_cap, sent_today, status, relay_pool_json,
                rotation_mode, max_bounce_rate, created_at, updated_at, user_id
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, 0, 'active', ?, ?, 0.02, ?, ?, ?)
        """, (
            schedule_id,
            payload.name.strip(),
            payload.campaign_id,
            payload.strategy,
            total_recipients,
            total_days,
            day_1_cap,
            json.dumps(relay_pool),
            payload.rotation_mode,
            now,
            now,
            uid
        ))

        # Create Slice records and corresponding scheduled campaign entries if base campaign exists
        slice_responses: List[WarmupSliceResponse] = []
        for s in slices_data:
            slice_id = f"wslice_{uuid.uuid4().hex[:10]}"
            child_camp_id = None

            # If a parent campaign exists, clone it as a scheduled child campaign for this slice
            if payload.campaign_id:
                async with db.execute("SELECT * FROM campaigns WHERE id = ? AND user_id = ?", (payload.campaign_id, uid)) as c_cur:
                    parent_camp = await c_cur.fetchone()
                if parent_camp:
                    child_camp_id = f"camp_{uuid.uuid4().hex[:10]}"
                    await db.execute("""
                        INSERT INTO campaigns (
                            id, name, subject, template_id, list_id, smtp_config_id,
                            smtp_config_json, template_html, template_text, sender_name,
                            sender_email, reply_to, headers, track_opens, track_clicks,
                            custom_html, custom_text, status, scheduled_at, total_recipients,
                            rate_limit_per_sec, concurrency_limit, is_warmup, warmup_schedule_id,
                            created_at, updated_at, user_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """, (
                        child_camp_id,
                        f"{parent_camp['name']} (Warmup Day {s['day_number']}/{total_days})",
                        parent_camp["subject"],
                        parent_camp["template_id"],
                        parent_camp["list_id"],
                        parent_camp["smtp_config_id"],
                        parent_camp["smtp_config_json"],
                        parent_camp["template_html"],
                        parent_camp["template_text"],
                        parent_camp["sender_name"],
                        parent_camp["sender_email"],
                        parent_camp["reply_to"],
                        parent_camp["headers"],
                        parent_camp["track_opens"],
                        parent_camp["track_clicks"],
                        parent_camp["custom_html"],
                        parent_camp["custom_text"],
                        s["scheduled_for"],
                        s["target_count"],
                        parent_camp["rate_limit_per_sec"] or 25,
                        parent_camp["concurrency_limit"] or 10,
                        schedule_id,
                        now,
                        now,
                        uid
                    ))

            await db.execute("""
                INSERT INTO warmup_slices (
                    id, schedule_id, day_number, scheduled_for, target_count,
                    dispatched_count, bounce_count, failure_count, campaign_id,
                    status, recipients_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, 'pending', ?, ?, ?)
            """, (
                slice_id,
                schedule_id,
                s["day_number"],
                s["scheduled_for"],
                s["target_count"],
                child_camp_id,
                json.dumps(s["recipients"]),
                now,
                now
            ))

            slice_responses.append(WarmupSliceResponse(
                id=slice_id,
                schedule_id=schedule_id,
                day_number=s["day_number"],
                scheduled_for=s["scheduled_for"],
                target_count=s["target_count"],
                dispatched_count=0,
                bounce_count=0,
                failure_count=0,
                campaign_id=child_camp_id,
                status="pending",
                created_at=now
            ))

        await db.commit()

    return WarmupScheduleResponse(
        id=schedule_id,
        name=payload.name.strip(),
        campaign_id=payload.campaign_id,
        strategy=payload.strategy,
        total_recipients=total_recipients,
        current_day=1,
        total_days=total_days,
        daily_cap=day_1_cap,
        sent_today=0,
        status="active",
        relay_pool=relay_pool,
        rotation_mode=payload.rotation_mode,
        max_bounce_rate=0.02,
        slices=slice_responses,
        created_at=now,
        updated_at=now
    )


@router.get("/schedules/{schedule_id}", response_model=WarmupScheduleResponse)
async def get_warmup_schedule(schedule_id: str):
    """
    Get full details of a specific warmup schedule including all daily slices.
    """
    async with get_db() as db:
        async with db.execute("SELECT * FROM warmup_schedules WHERE id = ? AND user_id = ?", (schedule_id, current_user_id())) as cur:
            s = await cur.fetchone()
        if not s:
            raise HTTPException(status_code=404, detail="Warmup schedule not found")

        async with db.execute(
            "SELECT * FROM warmup_slices WHERE schedule_id = ? ORDER BY day_number ASC",
            (schedule_id,)
        ) as scur:
            slice_rows = await scur.fetchall()

    slices = [
        WarmupSliceResponse(
            id=sr["id"],
            schedule_id=sr["schedule_id"],
            day_number=sr["day_number"],
            scheduled_for=sr["scheduled_for"],
            target_count=sr["target_count"],
            dispatched_count=sr["dispatched_count"] or 0,
            bounce_count=sr["bounce_count"] or 0,
            failure_count=sr["failure_count"] or 0,
            campaign_id=sr["campaign_id"],
            status=sr["status"],
            created_at=sr["created_at"]
        )
        for sr in slice_rows
    ]

    pool_ids = []
    try:
        pool_ids = json.loads(s["relay_pool_json"] or "[]")
    except Exception:
        pass

    return WarmupScheduleResponse(
        id=s["id"],
        name=s["name"],
        campaign_id=s["campaign_id"],
        strategy=s["strategy"],
        total_recipients=s["total_recipients"],
        total_days=s["total_days"],
        **_live_progress(s, slices),
        relay_pool=pool_ids,
        rotation_mode=s["rotation_mode"],
        max_bounce_rate=s["max_bounce_rate"] or 0.02,
        slices=slices,
        created_at=s["created_at"],
        updated_at=s["updated_at"]
    )


@router.post("/schedules/{schedule_id}/pause")
async def pause_warmup_schedule(schedule_id: str):
    """Pause an active warmup schedule and halt scheduled child campaigns."""
    now = utc_now_iso()
    async with get_db() as db:
        async with db.execute("SELECT id FROM warmup_schedules WHERE id = ? AND user_id = ?", (schedule_id, current_user_id())) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Warmup schedule not found")

        await db.execute("UPDATE warmup_schedules SET status = 'paused', updated_at = ? WHERE id = ?", (now, schedule_id))
        await db.commit()
    return {"status": "success", "message": f"Warmup schedule '{schedule_id}' paused."}


@router.post("/schedules/{schedule_id}/resume")
async def resume_warmup_schedule(schedule_id: str):
    """Resume a paused warmup schedule."""
    now = utc_now_iso()
    async with get_db() as db:
        async with db.execute("SELECT id FROM warmup_schedules WHERE id = ? AND user_id = ?", (schedule_id, current_user_id())) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Warmup schedule not found")

        await db.execute("UPDATE warmup_schedules SET status = 'active', updated_at = ? WHERE id = ?", (now, schedule_id))
        await db.commit()
    return {"status": "success", "message": f"Warmup schedule '{schedule_id}' resumed."}


@router.delete("/schedules/{schedule_id}")
async def delete_warmup_schedule(schedule_id: str):
    """Delete a warmup schedule and all associated slices."""
    async with get_db() as db:
        async with db.execute("SELECT id FROM warmup_schedules WHERE id = ? AND user_id = ?", (schedule_id, current_user_id())) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Warmup schedule not found")

        await db.execute("DELETE FROM warmup_slices WHERE schedule_id = ?", (schedule_id,))
        await db.execute("DELETE FROM warmup_schedules WHERE id = ?", (schedule_id,))
        await db.commit()
    return {"status": "success", "message": f"Warmup schedule '{schedule_id}' deleted."}


# ==============================================================================
# 3. Relay Pool Rotation Management Endpoints
# ==============================================================================
@router.get("/relays", response_model=List[RelayPoolStatusResponse])
async def list_relay_pool():
    """
    List all configured SMTP mail servers with their warmup pool status,
    daily send counters, failure rates, and active cooldown states.
    """
    relays = await RelayPoolManager.get_pool_relays()
    return [
        RelayPoolStatusResponse(
            id=f"relay_pool_{r['id']}",
            smtp_config_id=r["id"],
            name=r["name"],
            host=r["host"],
            port=r["port"],
            in_pool=r["in_pool"],
            current_day=r["current_day"],
            daily_sends=r["daily_sends"],
            daily_failures=r["daily_failures"],
            is_cooling_down=r["is_cooling_down"],
            cooldown_until=r["cooldown_until"],
            last_error=r["last_error"]
        )
        for r in relays
    ]


class TogglePoolRequest(BaseModel):
    in_pool: bool


@router.post("/relays/{smtp_config_id}/toggle-pool")
async def toggle_relay_in_pool(smtp_config_id: str, payload: TogglePoolRequest):
    """
    Include or exclude an SMTP mail server from the active warmup rotation pool.
    """
    async with get_db() as db:
        async with db.execute("SELECT id FROM smtp_configs WHERE id = ? AND user_id = ?", (smtp_config_id, current_user_id())) as cur:
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="SMTP mail server profile not found")

    await RelayPoolManager.toggle_relay_pool(smtp_config_id, payload.in_pool)
    status_str = "added to" if payload.in_pool else "removed from"
    return {
        "status": "success",
        "message": f"Relay '{smtp_config_id}' {status_str} warmup rotation pool.",
        "in_pool": payload.in_pool
    }
