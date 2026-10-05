"""
Asynchronous mass campaign queuing engine, background dispatch worker,
token-bucket rate-limiting governor, pause/resume/cancel orchestration,
and live telemetry tracking.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from app.config import settings
from app.db import get_db, utc_now_iso
from app.models import (
    Campaign,
    CampaignStatus,
    EmailRecord,
    EmailStatus,
    EventType,
    RecipientStatus,
    SendResult,
    SMTPConfig,
    Subscriber,
    TransactionalEmailRequest,
    TransactionalSendResponse,
)
from app.sender import EmailSender, email_sender, send_single_email
from app.storage import EmailStorageVault, storage_vault
from app.template_engine import TemplateEngine, template_engine
from app.webhooks import WebhookDispatcher
from app.websocket import emit_event

logger = logging.getLogger("bitmail.queue")


class AsyncTokenBucketRateLimiter:
    """
    High-precision async token-bucket rate limiter.
    Regulates email transmission velocity to prevent SMTP server throttling and ISP rate blocks.
    """

    def __init__(self, rate_per_second: float = 25.0, capacity: Optional[float] = None) -> None:
        self.rate = max(0.1, float(rate_per_second))
        self.capacity = float(capacity) if capacity is not None else float(self.rate)
        self.tokens = self.capacity
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """Acquire tokens, sleeping if required to conform to the token bucket rate."""
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_update
                self.last_update = now
                self.tokens = min(self.capacity, self.tokens + (elapsed * self.rate))

                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return

                needed = tokens - self.tokens
                sleep_time = needed / self.rate
                await asyncio.sleep(sleep_time)


class CampaignWorker:
    """
    Dedicated worker instance for executing a single mass campaign batch.
    """

    def __init__(
        self,
        campaign: Any,
        rate_limit_per_sec: int = 25,
        concurrency_limit: int = 10,
        sender: Optional[EmailSender] = None,
        storage: Optional[EmailStorageVault] = None,
        templates: Optional[TemplateEngine] = None,
    ) -> None:
        self.campaign = campaign
        self.campaign_id = campaign.id if hasattr(campaign, "id") else campaign["id"]
        self.rate_limiter = AsyncTokenBucketRateLimiter(rate_per_second=rate_limit_per_sec)
        self.semaphore = asyncio.Semaphore(concurrency_limit)
        self.sender = sender or email_sender
        self.storage = storage or storage_vault
        self.templates = templates or template_engine
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.is_cancelled = False
        self.is_active = True
        self.task: Optional[asyncio.Task] = None
        self._sent_count = 0
        self._failed_count = 0
        self._total = 0

    async def run(self, explicit_recipients: Optional[List[Subscriber]] = None) -> None:
        """Execute the campaign send loop."""
        try:
            self.is_active = True

            recipients = await self.resolve_recipients(explicit_recipients)
            total = len(recipients)

            async with get_db() as db:
                await db.execute(
                    "UPDATE campaigns SET status = ?, started_at = ?, total_recipients = ?, updated_at = ? WHERE id = ?",
                    (CampaignStatus.SENDING.value, utc_now_iso(), total, utc_now_iso(), self.campaign_id)
                )
                await db.commit()

            if total == 0:
                await self._finish(CampaignStatus.COMPLETED.value, sent=0, total=0)
                return

            # Load campaign details
            subject_tmpl = getattr(self.campaign, "subject", "")
            html_tmpl = getattr(self.campaign, "template_html", "") or getattr(self.campaign, "body_html", "") or getattr(self.campaign, "custom_html", "") or ""
            sender_name = getattr(self.campaign, "sender_name", settings.DEFAULT_SENDER_NAME)
            sender_email = getattr(self.campaign, "sender_email", settings.DEFAULT_SENDER_EMAIL)
            reply_to = getattr(self.campaign, "reply_to", None)
            track_opens = bool(getattr(self.campaign, "track_opens", True))
            track_clicks = bool(getattr(self.campaign, "track_clicks", True))
            smtp_cfg = getattr(self.campaign, "smtp_config", None)

            self._sent_count = 0
            self._failed_count = 0
            self._total = total

            async def dispatch(sub: Subscriber) -> None:
                async with self.semaphore:
                    if self.is_cancelled:
                        return

                    storage_id = self.storage.generate_storage_id()
                    rendered = self.templates.render_email(
                        subject_template=subject_tmpl,
                        html_template=html_tmpl,
                        subscriber=sub,
                        email_storage_id=storage_id,
                        campaign_id=self.campaign_id,
                        sender_name=sender_name,
                        sender_email=sender_email,
                        track_opens=track_opens,
                        track_clicks=track_clicks,
                    )

                    res = await self.sender.send_email(
                        recipient=sub,
                        rendered=rendered,
                        storage_id=storage_id,
                        campaign_id=self.campaign_id,
                        sender_email=sender_email,
                        sender_name=sender_name,
                        reply_to=reply_to,
                        smtp_config=smtp_cfg,
                    )

                    if res.success:
                        self._sent_count += 1
                    elif getattr(res, "status", None) == "skipped" or (res.error and "Pre-send Safety Guard" in str(res.error)):
                        self._skipped_count = getattr(self, "_skipped_count", 0) + 1
                        logger.info("Recipient %s safely skipped by pre-send guard: %s", sub.email, res.error)
                    else:
                        self._failed_count += 1
                        logger.warning("Send to %s failed: %s", sub.email, res.error)

                    await self._publish_progress(sub, storage_id, rendered.subject, res.success, res.error)

            # The rate limiter governs throughput; the semaphore caps how many sends are
            # in flight at once, so a slow relay does not serialise the whole campaign.
            pending: Set[asyncio.Task] = set()
            for sub in recipients:
                if self.is_cancelled:
                    break

                await self.pause_event.wait()
                await self.rate_limiter.acquire()

                task = asyncio.create_task(dispatch(sub))
                pending.add(task)
                task.add_done_callback(pending.discard)

            if pending:
                await asyncio.gather(*list(pending), return_exceptions=True)

            if self.is_cancelled:
                final_status = CampaignStatus.CANCELLED.value
            elif self._sent_count == 0 and self._failed_count > 0:
                # Every recipient bounced off the relay - "completed" would hide that.
                final_status = CampaignStatus.FAILED.value
            else:
                final_status = CampaignStatus.COMPLETED.value
            await self._finish(final_status, sent=self._sent_count, total=total)

        except asyncio.CancelledError:
            await self._finish(CampaignStatus.CANCELLED.value, sent=self._sent_count, total=self._total)
            raise
        except Exception as e:
            logger.error("Campaign worker exception: %s", e, exc_info=True)
            async with get_db() as db:
                await db.execute(
                    "UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?",
                    (CampaignStatus.FAILED.value, utc_now_iso(), self.campaign_id)
                )
                await db.commit()
        finally:
            self.is_active = False

    async def resolve_recipients(self, explicit_recipients: Optional[List[Subscriber]] = None) -> List[Subscriber]:
        """
        Build the send list for this campaign, always dropping addresses that are
        unsubscribed or suppressed. Recipients handed in explicitly (a pasted lead
        paste, an uploaded list) are filtered too - a suppression is global.
        """
        async with get_db() as db:
            async with db.execute("SELECT email FROM suppressions") as cur:
                suppressed = {row["email"].strip().lower() for row in await cur.fetchall()}

            if explicit_recipients:
                return [
                    sub for sub in explicit_recipients
                    if sub.email and sub.email.strip().lower() not in suppressed
                ]

            async with db.execute("SELECT list_id FROM campaigns WHERE id = ?", (self.campaign_id,)) as cur:
                camp_row = await cur.fetchone()
                if not camp_row:
                    return []
                list_id = camp_row["list_id"]

            # Membership is recorded in two tables by different import paths; take both.
            if list_id:
                query = """
                    SELECT DISTINCT s.* FROM subscribers s
                    LEFT JOIN subscriber_list_memberships m ON s.id = m.subscriber_id
                    LEFT JOIN list_subscribers ls ON s.id = ls.subscriber_id
                    WHERE (m.list_id = ? OR ls.list_id = ?) AND s.status = 'active'
                """
                params = (list_id, list_id)
            else:
                query = "SELECT * FROM subscribers WHERE status = 'active'"
                params = ()

            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()

        recipients: List[Subscriber] = []
        for r in rows:
            if (r["email"] or "").strip().lower() in suppressed:
                continue
            try:
                cf = json.loads(r["custom_fields"] or "{}")
            except Exception:
                cf = {}
            recipients.append(Subscriber(
                id=r["id"],
                email=r["email"],
                first_name=r["first_name"],
                last_name=r["last_name"],
                custom_attributes=cf,
            ))
        return recipients

    async def _publish_progress(
        self, sub: Subscriber, storage_id: str, subject: str, ok: bool, error: Optional[str] = None
    ) -> None:
        """Persist running counters and push live telemetry to connected dashboards."""
        processed = self._sent_count + self._failed_count
        async with get_db() as db:
            await db.execute("""
                UPDATE campaigns
                SET sent_count = ?, failed_count = ?, delivered_count = ?, updated_at = ?
                WHERE id = ?
            """, (processed, self._failed_count, self._sent_count, utc_now_iso(), self.campaign_id))
            await db.commit()

        await emit_event("email_dispatched", {
            "campaign_id": self.campaign_id,
            "recipient": sub.email,
            "recipient_name": f"{sub.first_name or ''} {sub.last_name or ''}".strip(),
            "storage_id": storage_id,
            "subject": subject,
            "status": "delivered" if ok else "failed",
            "error": error,
            "sent_count": processed,
            "delivered_count": self._sent_count,
            "failed_count": self._failed_count,
            "total": self._total,
            "progress_percent": min(100, round((processed / self._total) * 100)) if self._total else 100,
        })

        # Outbound Webhook dispatch
        event_name = "email.delivered" if ok else "email.sent"
        await WebhookDispatcher.dispatch_event(event_name, {
            "campaign_id": self.campaign_id,
            "recipient": sub.email,
            "recipient_name": f"{sub.first_name or ''} {sub.last_name or ''}".strip(),
            "storage_id": storage_id,
            "subject": subject,
            "status": "delivered" if ok else "failed",
            "error": error
        })

    async def _finish(self, status: str, sent: int, total: int) -> None:
        """Mark the campaign finished and announce it."""
        now = utc_now_iso()
        async with get_db() as db:
            await db.execute(
                "UPDATE campaigns SET status = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                (status, now, now, self.campaign_id)
            )
            await db.commit()

        # Update warmup slice telemetry if campaign was a warmup slice
        try:
            async with get_db() as db:
                async with db.execute(
                    "SELECT id, schedule_id FROM warmup_slices WHERE campaign_id = ?",
                    (self.campaign_id,)
                ) as sc_cur:
                    s_row = await sc_cur.fetchone()
                if s_row:
                    slice_id = s_row["id"]
                    schedule_id = s_row["schedule_id"]
                    await db.execute(
                        "UPDATE warmup_slices SET dispatched_count = ?, failure_count = ?, status = ?, updated_at = ? WHERE id = ?",
                        (sent, self._failed_count, "completed" if status == "completed" else "failed", now, slice_id)
                    )
                    await db.execute(
                        "UPDATE warmup_schedules SET sent_today = sent_today + ?, updated_at = ? WHERE id = ?",
                        (sent, now, schedule_id)
                    )
                    await db.commit()

                    from app.warmup import WarmupCircuitBreaker
                    await WarmupCircuitBreaker.check_and_apply(schedule_id, slice_id)
        except Exception as warmup_ex:
            logger.warning("Warmup slice finish update exception: %s", warmup_ex)

        await emit_event("campaign_completed", {
            "campaign_id": self.campaign_id,
            "status": status,
            "sent_count": sent,
            "failed_count": self._failed_count,
            "total": total,
        })



class CampaignQueueManager:
    """
    Manages background execution tasks for mass email campaigns.
    Provides fine-grained control for starting, pausing, resuming, and cancelling campaigns.
    """

    def __init__(
        self,
        sender: Optional[EmailSender] = None,
        storage: Optional[EmailStorageVault] = None,
        templates: Optional[TemplateEngine] = None,
    ):
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._pause_events: Dict[str, asyncio.Event] = {}
        self._active_workers: Dict[str, CampaignWorker] = {}
        self._campaign_recipients: Dict[str, List[Subscriber]] = {}
        self._cancelled_campaigns: Set[str] = set()
        self._lock = asyncio.Lock()
        self.sender = sender or email_sender
        self.storage = storage or storage_vault
        self.templates = templates or template_engine

    def is_running(self, campaign_id: str) -> bool:
        """Check if a campaign task is currently active in memory."""
        worker = self._active_workers.get(campaign_id)
        if worker and worker.is_active:
            return True
        task = self._running_tasks.get(campaign_id)
        return task is not None and not task.done()

    def is_paused(self, campaign_id: str) -> bool:
        """Check if campaign is paused."""
        event = self._pause_events.get(campaign_id)
        return event is not None and not event.is_set()

    async def get_campaign(self, campaign_id: str) -> Optional[Any]:
        """Fetch campaign from DB, resolving its SMTP relay and template body."""
        async with get_db() as db:
            async with db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return None

                smtp_cfg = await self._resolve_smtp_config(db, row["smtp_config_json"], row["smtp_config_id"])

                html_body = row["template_html"] or row["custom_html"] or ""
                text_body = row["template_text"] or row["custom_text"] or ""
                subject = row["subject"]

                # Campaigns created through the REST API reference a template by id
                # rather than carrying the body inline.
                if row["template_id"] and not html_body:
                    async with db.execute(
                        "SELECT subject, body_html, body_text FROM templates WHERE id = ?",
                        (row["template_id"],),
                    ) as tcur:
                        tpl = await tcur.fetchone()
                        if tpl:
                            html_body = tpl["body_html"] or ""
                            text_body = text_body or (tpl["body_text"] or "")
                            subject = subject or tpl["subject"]

                return Campaign(
                    id=row["id"],
                    name=row["name"],
                    subject=subject,
                    template_id=row["template_id"],
                    list_id=row["list_id"],
                    template_html=html_body,
                    template_text=text_body,
                    sender_name=row["sender_name"],
                    sender_email=row["sender_email"],
                    reply_to=row["reply_to"],
                    smtp_config_id=row["smtp_config_id"],
                    smtp_config=smtp_cfg,
                    track_opens=bool(row["track_opens"]),
                    track_clicks=bool(row["track_clicks"]),
                    status=row["status"],
                    total_recipients=row["total_recipients"],
                    sent_count=row["sent_count"],
                    failed_count=row["failed_count"],
                    open_count=row["open_count"],
                    click_count=row["click_count"],
                    rate_limit_per_sec=row["rate_limit_per_sec"] or 25,
                    concurrency_limit=row["concurrency_limit"] or 10,
                )

    @staticmethod
    async def _resolve_smtp_config(db, smtp_config_json: Optional[str], smtp_config_id: Optional[str]) -> SMTPConfig:
        """
        Resolve the relay a campaign should dispatch through: the inline snapshot first,
        then the referenced profile, then the account default. Falls back to a relay with
        no host so the send fails loudly rather than silently pretending to deliver.
        """
        if smtp_config_json:
            try:
                return SMTPConfig(**json.loads(smtp_config_json))
            except Exception:
                logger.warning("Campaign has unreadable smtp_config_json; falling back to profile lookup.")

        row = None
        if smtp_config_id:
            async with db.execute("SELECT * FROM smtp_configs WHERE id = ?", (smtp_config_id,)) as cur:
                row = await cur.fetchone()
        if row is None:
            async with db.execute(
                "SELECT * FROM smtp_configs WHERE is_default = 1 AND is_active = 1 LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            async with db.execute("SELECT * FROM smtp_configs WHERE is_active = 1 LIMIT 1") as cur:
                row = await cur.fetchone()

        if row is None:
            return SMTPConfig(id=None, name="unconfigured", host="")

        cfg = dict(row)
        return SMTPConfig(
            id=cfg.get("id"),
            name=cfg.get("name") or "SMTP Relay",
            host=cfg.get("host") or "",
            port=cfg.get("port") or 587,
            username=cfg.get("username"),
            password=(__import__("app.auth", fromlist=["decrypt_credential"]).decrypt_credential(cfg.get("password") or "") if cfg.get("password") else None),
            use_tls=bool(cfg.get("use_tls", 1)),
            use_ssl=bool(cfg.get("use_ssl", 0)),
            rate_limit_per_second=cfg.get("rate_limit_per_second") or 25,
            daily_quota=cfg.get("daily_quota") or 50000,
            is_sandbox=str(cfg.get("host") or "").strip().lower() == "sandbox",
        )

    async def create_campaign(
        self,
        name: str,
        subject: str,
        template_html: str,
        template_text: Optional[str] = None,
        sender_name: Optional[str] = None,
        sender_email: Optional[str] = None,
        smtp_config: Optional[SMTPConfig] = None,
        rate_limit_per_sec: int = 25,
        concurrency_limit: int = 10,
        recipients: Optional[List[Subscriber]] = None,
    ) -> Campaign:
        """Create a new campaign object and persist to SQLite."""
        cid = f"cmp_{uuid.uuid4().hex[:12]}"
        now_iso = utc_now_iso()
        if smtp_config is None:
            async with get_db() as db:
                smtp_config = await self._resolve_smtp_config(db, None, None)
        smtp_cfg = smtp_config
        smtp_json = json.dumps(smtp_cfg.model_dump())
        total_recipients = len(recipients) if recipients else 0

        if recipients:
            self._campaign_recipients[cid] = recipients

        campaign = Campaign(
            id=cid,
            name=name,
            subject=subject,
            template_html=template_html,
            template_text=template_text,
            sender_name=sender_name or settings.DEFAULT_SENDER_NAME,
            sender_email=sender_email or settings.DEFAULT_SENDER_EMAIL,
            smtp_config=smtp_cfg,
            status=CampaignStatus.DRAFT.value,
            total_recipients=total_recipients,
            rate_limit_per_sec=rate_limit_per_sec,
            concurrency_limit=concurrency_limit,
            created_at=now_iso,
        )

        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO campaigns (
                    id, name, subject, template_html, template_text, sender_name, sender_email,
                    smtp_config_json, status, total_recipients, sent_count, failed_count,
                    open_count, click_count, unsubscribed_count, rate_limit_per_sec,
                    concurrency_limit, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign.id,
                    campaign.name,
                    campaign.subject,
                    campaign.template_html,
                    campaign.template_text,
                    campaign.sender_name,
                    campaign.sender_email,
                    smtp_json,
                    campaign.status,
                    total_recipients,
                    0, 0, 0, 0, 0,
                    rate_limit_per_sec,
                    concurrency_limit,
                    now_iso,
                    now_iso,
                ),
            )
            await db.commit()

        return campaign

    async def start_campaign(
        self,
        campaign_id: str,
        recipients: Optional[List[Subscriber]] = None,
    ) -> CampaignWorker:
        """Launch background worker for a mass campaign."""
        async with self._lock:
            if campaign_id in self._active_workers and self._active_workers[campaign_id].is_active:
                return self._active_workers[campaign_id]

            campaign = await self.get_campaign(campaign_id)
            if not campaign:
                raise ValueError(f"Campaign not found: {campaign_id}")

            target_recipients = recipients or self._campaign_recipients.get(campaign_id)

            worker = CampaignWorker(
                campaign=campaign,
                rate_limit_per_sec=getattr(campaign, "rate_limit_per_sec", settings.DEFAULT_RATE_LIMIT_PER_SEC),
                concurrency_limit=getattr(campaign, "concurrency_limit", settings.MAX_CONCURRENT_SENDS),
                sender=self.sender,
                storage=self.storage,
                templates=self.templates,
            )

            worker.task = asyncio.create_task(worker.run(target_recipients))
            self._active_workers[campaign_id] = worker
            self._running_tasks[campaign_id] = worker.task
            self._pause_events[campaign_id] = worker.pause_event
            return worker

    async def launch_campaign(self, campaign_id: str) -> Dict[str, Any]:
        """Alias method for route compatibility."""
        try:
            worker = await self.start_campaign(campaign_id)
            return {
                "success": True,
                "message": f"Campaign {campaign_id} launched successfully.",
                "status": "sending"
            }
        except Exception as e:
            return {
                "success": False,
                "message": str(e),
                "status": "failed"
            }

    async def pause_campaign(self, campaign_id: str) -> Dict[str, Any]:
        """Pause an active campaign."""
        async with self._lock:
            worker = self._active_workers.get(campaign_id)
            if worker:
                worker.pause_event.clear()
            event = self._pause_events.get(campaign_id)
            if event:
                event.clear()

            async with get_db() as db:
                await db.execute(
                    "UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?",
                    (CampaignStatus.PAUSED.value, utc_now_iso(), campaign_id)
                )
                await db.commit()

            return {"success": True, "message": f"Campaign {campaign_id} paused."}

    async def resume_campaign(self, campaign_id: str) -> Dict[str, Any]:
        """Resume a paused campaign."""
        async with self._lock:
            worker = self._active_workers.get(campaign_id)
            if worker:
                worker.pause_event.set()
            event = self._pause_events.get(campaign_id)
            if event:
                event.set()

            async with get_db() as db:
                await db.execute(
                    "UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?",
                    (CampaignStatus.SENDING.value, utc_now_iso(), campaign_id)
                )
                await db.commit()

            return {"success": True, "message": f"Campaign {campaign_id} resumed."}

    async def cancel_campaign(self, campaign_id: str) -> Dict[str, Any]:
        """Cancel a running or queued campaign."""
        async with self._lock:
            self._cancelled_campaigns.add(campaign_id)
            worker = self._active_workers.get(campaign_id)
            if worker:
                worker.is_cancelled = True
                worker.pause_event.set()
                if worker.task and not worker.task.done():
                    worker.task.cancel()

            async with get_db() as db:
                await db.execute(
                    "UPDATE campaigns SET status = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                    (CampaignStatus.CANCELLED.value, utc_now_iso(), utc_now_iso(), campaign_id)
                )
                await db.commit()

            return {"success": True, "message": f"Campaign {campaign_id} cancelled."}

    async def get_campaign_progress(self, campaign_id: str) -> Dict[str, Any]:
        """Get live metrics and progress for a campaign."""
        async with get_db() as db:
            async with db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return {"found": False}
                return {
                    "id": row["id"],
                    "status": row["status"],
                    "total_recipients": row["total_recipients"],
                    "sent_count": row["sent_count"],
                    "delivered_count": row["delivered_count"],
                    "failed_count": row["failed_count"],
                    "open_count": row["open_count"],
                    "click_count": row["click_count"],
                }

    async def send_transactional(self, request: TransactionalEmailRequest) -> TransactionalSendResponse:
        """Process and send an immediate high-priority transactional email."""
        storage_id = self.storage.generate_storage_id()
        sub = Subscriber(
            id=f"tx_sub_{uuid.uuid4().hex[:8]}",
            email=request.recipient_email,
            first_name=request.recipient_name,
            custom_attributes=request.template_context or {},
        )

        rendered = self.templates.render_email(
            subject_template=request.subject,
            html_template=request.html_content or request.text_content or "",
            subscriber=sub,
            email_storage_id=storage_id,
            track_opens=True,
            track_clicks=True,
        )

        smtp_cfg = request.smtp_config
        if smtp_cfg is None:
            async with get_db() as db:
                smtp_cfg = await self._resolve_smtp_config(db, None, None)
        result = await self.sender.send_email(
            recipient=sub,
            rendered=rendered,
            storage_id=storage_id,
            sender_email=request.sender_email or settings.DEFAULT_SENDER_EMAIL,
            sender_name=request.sender_name or settings.DEFAULT_SENDER_NAME,
            smtp_config=smtp_cfg,
        )

        final_status = result.status if hasattr(result, "status") and result.status else ("simulated" if getattr(smtp_cfg, "is_sandbox", False) else ("sent" if result.success else "failed"))
        return TransactionalSendResponse(
            success=result.success,
            sent_email_id=storage_id,
            storage_id=storage_id,
            message_id=result.message_id,
            status=EmailStatus.SIMULATED.value if getattr(smtp_cfg, "is_sandbox", False) else (EmailStatus.SENT.value if result.success else EmailStatus.FAILED.value),
            sent_at=utc_now_iso() if result.success else None,
            error=getattr(result, "error", getattr(result, "error_message", None)),
        )



# Global singleton
campaign_queue = CampaignQueueManager()
