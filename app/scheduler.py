"""
Automated Campaign Scheduler Service.
Monitors SQLite for scheduled campaigns whose target dispatch timestamp has arrived,
atomically transitions their status, and launches background queue dispatch.
"""

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Union

from app.db import get_db, utc_now_iso
from app.queue import campaign_queue
from app.websocket import emit_event

logger = logging.getLogger("bitmail.scheduler")


def parse_and_normalize_schedule_time(dt_input: Union[str, datetime]) -> str:
    """
    Parse arbitrary datetime inputs (ISO 8601, HTML5 datetime-local, epoch)
    and normalize to UTC standard format 'YYYY-MM-DD HH:MM:SS'.
    """
    if isinstance(dt_input, datetime):
        if dt_input.tzinfo is None:
            dt = dt_input.replace(tzinfo=timezone.utc)
        else:
            dt = dt_input.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    raw = str(dt_input).strip()
    if not raw:
        raise ValueError("Schedule timestamp cannot be empty.")

    # 1. Try standard ISO parsing
    try:
        # Handle trailing Z
        clean_iso = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass

    # 2. Try common datetime format patterns
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(raw, fmt)
            dt = dt.replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

    raise ValueError(f"Unable to parse schedule timestamp: '{raw}'. Expected ISO format like 'YYYY-MM-DD HH:MM:SS'.")


class CampaignScheduler:
    """
    Background asynchronous scheduler for scheduled campaigns.
    Periodically queries SQLite for due campaigns and initiates dispatch.
    """

    def __init__(self, poll_interval_seconds: float = 5.0):
        self.poll_interval = poll_interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._is_running = False

    @property
    def is_running(self) -> bool:
        return self._is_running and self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the background scheduler polling loop."""
        if self.is_running:
            return

        self._stop_event.clear()
        self._is_running = True
        self._task = asyncio.create_task(self._scheduler_loop(), name="campaign-scheduler-loop")
        logger.info("[Scheduler] Campaign Scheduler service started (poll interval: %.1fs).", self.poll_interval)

    async def stop(self) -> None:
        """Stop the background scheduler loop gracefully."""
        self._is_running = False
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("[Scheduler] Campaign Scheduler service stopped.")

    async def check_and_trigger_due_campaigns(self) -> List[str]:
        """
        Poll database for scheduled campaigns where scheduled_at <= current UTC time.
        Atomically claims each campaign and launches execution.
        Returns list of triggered campaign IDs.
        """
        now = utc_now_iso()
        due_campaigns: List[Dict[str, Any]] = []

        try:
            async with get_db() as db:
                async with db.execute("""
                    SELECT id, name, subject, scheduled_at, status
                    FROM campaigns
                    WHERE status = 'scheduled'
                      AND scheduled_at IS NOT NULL
                      AND scheduled_at <= ?
                    ORDER BY scheduled_at ASC
                """, (now,)) as cur:
                    rows = await cur.fetchall()
                    due_campaigns = [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"[Scheduler] Database error during scheduled campaign check: {e}")
            return []

        if not due_campaigns:
            return []

        triggered_ids = []
        for camp in due_campaigns:
            camp_id = camp["id"]
            camp_name = camp.get("name") or camp.get("subject") or camp_id

            # Atomically update status to queued to claim this campaign and prevent duplicate execution
            claimed = False
            async with get_db() as db:
                async with db.execute("""
                    UPDATE campaigns
                    SET status = 'queued',
                        updated_at = ?
                    WHERE id = ? AND status = 'scheduled'
                """, (now, camp_id)) as cur:
                    if cur.rowcount > 0:
                        claimed = True
                await db.commit()

            if not claimed:
                continue

            logger.info(
                f"[Scheduler] Triggering scheduled campaign '{camp_name}' ({camp_id}) "
                f"scheduled for {camp['scheduled_at']}."
            )

            try:
                launch_res = await campaign_queue.launch_campaign(camp_id)
                if launch_res.get("success"):
                    triggered_ids.append(camp_id)
                    await emit_event("campaign_scheduled_triggered", {
                        "campaign_id": camp_id,
                        "name": camp_name,
                        "scheduled_at": camp["scheduled_at"],
                        "triggered_at": now
                    })
                else:
                    logger.warning(f"[Scheduler] Failed to launch campaign {camp_id}: {launch_res.get('message')}")
            except Exception as e:
                logger.error(f"[Scheduler] Error executing scheduled campaign {camp_id}: {e}")
                async with get_db() as db:
                    await db.execute(
                        "UPDATE campaigns SET status = 'failed', updated_at = ? WHERE id = ?",
                        (now, camp_id)
                    )
                    await db.commit()

        return triggered_ids

    async def _scheduler_loop(self) -> None:
        """Periodic background polling loop."""
        while not self._stop_event.is_set():
            try:
                await self.check_and_trigger_due_campaigns()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Scheduler] Unexpected error in scheduler loop: {e}", exc_info=True)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass


# Global singleton instance
campaign_scheduler = CampaignScheduler()
