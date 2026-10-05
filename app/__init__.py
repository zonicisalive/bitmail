"""
Enterprise Mass Email & Storage Archive System.
Core engine components for delivery, storage vault, template processing, and queue management.
"""

from app.config import settings
from app.db import get_db, init_db
from app.models import (
    Campaign,
    CampaignStatus,
    EmailRecord,
    EmailStatus,
    EventType,
    RecipientStatus,
    RenderedEmailContent,
    SendResult,
    SMTPConfig,
    Subscriber,
    TimelineEvent,
    TransactionalEmailRequest,
)
from app.queue import (
    AsyncTokenBucketRateLimiter,
    CampaignQueueManager,
    CampaignWorker,
    campaign_queue,
)
from app.sender import EmailSender, email_sender
from app.storage import EmailStorageVault, storage_vault
from app.template_engine import TemplateEngine, template_engine

__all__ = [
    "settings",
    "init_db",
    "get_db",
    "TemplateEngine",
    "template_engine",
    "EmailStorageVault",
    "storage_vault",
    "EmailSender",
    "email_sender",
    "CampaignQueueManager",
    "campaign_queue",
    "CampaignWorker",
    "AsyncTokenBucketRateLimiter",
    "EmailRecord",
    "EmailStatus",
    "Campaign",
    "CampaignStatus",
    "RecipientStatus",
    "EventType",
    "SMTPConfig",
    "Subscriber",
    "TimelineEvent",
    "SendResult",
    "RenderedEmailContent",
    "TransactionalEmailRequest",
]
