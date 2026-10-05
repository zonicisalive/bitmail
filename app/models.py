"""
Pydantic data models and schemas for the Enterprise Mass Email System.
Defines schemas for Subscribers, Subscriber Lists, Custom Merge Fields, Templates,
Campaigns, SMTP Server Configurations, Tracking Events, Sent Email Archive Vault,
and Transactional Send Requests.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field, ConfigDict, field_validator, AliasChoices


# ----------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------

class SubscriberStatus(str, Enum):
    ACTIVE = "active"
    UNSUBSCRIBED = "unsubscribed"
    BOUNCED = "bounced"
    COMPLAINED = "complained"


class CampaignStatus(str, Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    SENDING = "sending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EmailStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    BOUNCED = "bounced"
    SIMULATED = "simulated"



class EventType(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    SIMULATED = "simulated"
    DELIVERED = "delivered"
    OPEN = "open"
    OPENED = "opened"
    CLICK = "click"
    CLICKED = "clicked"
    UNSUBSCRIBE = "unsubscribe"
    UNSUBSCRIBED = "unsubscribed"
    BOUNCE = "bounce"
    BOUNCED = "bounced"
    COMPLAINT = "complaint"
    FAILED = "failed"



class RecipientStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class SecurityType(str, Enum):
    PLAIN = "plain"
    STARTTLS = "starttls"
    SSL_TLS = "ssl_tls"


# ----------------------------------------------------------------------
# Subscriber & Subscriber List Models
# ----------------------------------------------------------------------

class SubscriberBase(BaseModel):
    email: str = Field(..., description="Recipient email address")
    first_name: Optional[str] = Field(default=None, description="Subscriber first name")
    last_name: Optional[str] = Field(default=None, description="Subscriber last name")
    custom_fields: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary custom merge fields, e.g. {'company': 'Acme', 'plan': 'Enterprise'}"
    )
    status: SubscriberStatus = Field(
        default=SubscriberStatus.ACTIVE,
        description="Subscriber status"
    )

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Invalid email format")
        return v


class SubscriberCreate(SubscriberBase):
    list_ids: Optional[List[str]] = Field(
        default=None,
        description="List of subscriber list IDs to attach to upon creation"
    )


class SubscriberUpdate(BaseModel):
    email: Optional[str] = Field(default=None)
    first_name: Optional[str] = Field(default=None)
    last_name: Optional[str] = Field(default=None)
    custom_fields: Optional[Dict[str, Any]] = Field(default=None)
    status: Optional[SubscriberStatus] = Field(default=None)
    list_ids: Optional[List[str]] = Field(default=None)


    @field_validator("email")
    @classmethod
    def validate_email_format(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip().lower()
            if "@" not in v or "." not in v.split("@")[-1]:
                raise ValueError("Invalid email format")
        return v


class SubscriberResponse(SubscriberBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: str
    updated_at: str
    lists: List[str] = Field(default_factory=list, description="IDs of lists subscriber belongs to")


class Subscriber(BaseModel):
    """General subscriber representation compatible with campaign dispatches."""
    id: Optional[str] = Field(default=None, description="Unique subscriber ID")
    email: str = Field(..., description="Subscriber email address")
    first_name: Optional[str] = Field(default="", description="Subscriber first name")
    last_name: Optional[str] = Field(default="", description="Subscriber last name")
    custom_fields: Dict[str, Any] = Field(default_factory=dict, description="Custom merge attributes")
    custom_attributes: Dict[str, Any] = Field(default_factory=dict, description="Alias for custom_fields")
    status: str = Field(default=SubscriberStatus.ACTIVE.value)


class SubscriberBulkImportItem(BaseModel):
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    custom_fields: Dict[str, Any] = Field(default_factory=dict)


class SubscriberBulkImportRequest(BaseModel):
    list_id: str
    subscribers: List[SubscriberBulkImportItem]


class SubscriberBulkImportResponse(BaseModel):
    total_received: int
    added_count: int
    updated_count: int
    failed_count: int
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    custom_fields_detected: List[str] = Field(default_factory=list)
    list_id: Optional[str] = None
    list_name: Optional[str] = None


class SubscriberListBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="List name")
    description: Optional[str] = Field(default=None, description="Optional description of the list")
    schema_fields: List[str] = Field(
        default_factory=list,
        description="Custom placeholder column names defined for this table (e.g. ['order_id', 'amount'])"
    )


class SubscriberListCreate(SubscriberListBase):
    pass


class SubscriberListUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None)
    schema_fields: Optional[List[str]] = Field(default=None)


class SubscriberListResponse(SubscriberListBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    subscriber_count: int = Field(default=0, description="Active subscriber count")
    created_at: str
    updated_at: str


class SubscriberListDetail(SubscriberListResponse):
    subscribers: List[SubscriberResponse] = Field(default_factory=list)


# ----------------------------------------------------------------------
# Template Models
# ----------------------------------------------------------------------

class TemplateBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Template name")
    description: Optional[str] = Field(default=None, description="Template description or purpose")
    subject: str = Field(..., description="Default subject line (supports merge tags)")
    body_html: str = Field(..., description="Full HTML body template with merge tags")
    body_text: Optional[str] = Field(
        default=None,
        description="Plain text fallback template with merge tags"
    )


class TemplateCreate(TemplateBase):
    pass


class TemplateUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None)
    subject: Optional[str] = Field(default=None)
    body_html: Optional[str] = Field(default=None)
    body_text: Optional[str] = Field(default=None)


class TemplateResponse(TemplateBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: str
    updated_at: str


class TemplatePreviewRequest(BaseModel):
    template_id: Optional[str] = None
    subject: Optional[str] = Field(default=None, validation_alias=AliasChoices("subject", "subject_template"))
    body_html: Optional[str] = Field(default=None, validation_alias=AliasChoices("body_html", "body_template", "html"))
    body_text: Optional[str] = Field(default=None, validation_alias=AliasChoices("body_text", "text"))
    merge_variables: Dict[str, Any] = Field(default_factory=dict, validation_alias=AliasChoices("merge_variables", "sample_variables", "context"))


class TemplatePreviewResponse(BaseModel):
    rendered_subject: str
    rendered_body_html: str
    rendered_body_text: str
    rendered_body: Optional[str] = None
    detected_tags: List[str] = Field(default_factory=list)


class RenderedEmailContent(BaseModel):
    """Processed template payload ready for MIME construction and dispatch."""
    subject: str
    body_text: str
    body_html: str
    rendered_html: str
    unsubscribe_url: str


# ----------------------------------------------------------------------
# SMTP Configuration Models
# ----------------------------------------------------------------------

class SMTPConfigBase(BaseModel):
    name: str = Field(..., description="Descriptive profile name, e.g. 'Production SES' or 'Local Sandbox'")
    host: str = Field(..., description="SMTP server hostname or IP")
    port: int = Field(default=587, ge=1, le=65535, description="SMTP server port (usually 25, 465, 587, or 1025)")
    username: Optional[str] = Field(default=None, description="SMTP username / auth ID")
    use_tls: bool = Field(default=True, description="Enable STARTTLS encryption")
    use_ssl: bool = Field(default=False, description="Enable direct SSL/TLS encryption (port 465)")
    rate_limit_per_second: int = Field(default=25, ge=1, le=1000, description="Max emails dispatched per second")
    daily_quota: int = Field(default=50000, ge=0, description="Max allowed sends per 24-hour cycle")
    is_default: bool = Field(default=False, description="Whether this is the primary default SMTP config")
    is_active: bool = Field(default=True, description="Whether this configuration profile is enabled")


class SMTPConfigCreate(SMTPConfigBase):
    password: Optional[str] = Field(default=None, description="SMTP password / auth secret")


class SMTPConfigUpdate(BaseModel):
    name: Optional[str] = Field(default=None)
    host: Optional[str] = Field(default=None)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    username: Optional[str] = Field(default=None)
    password: Optional[str] = Field(default=None)
    use_tls: Optional[bool] = Field(default=None)
    use_ssl: Optional[bool] = Field(default=None)
    rate_limit_per_second: Optional[int] = Field(default=None, ge=1, le=1000)
    daily_quota: Optional[int] = Field(default=None, ge=0)
    is_default: Optional[bool] = Field(default=None)
    is_active: Optional[bool] = Field(default=None)


class SMTPConfigResponse(SMTPConfigBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    has_password: bool = Field(default=False, description="Indicates if a password is configured")
    created_at: str
    updated_at: str


class SMTPConfig(BaseModel):
    """SMTP configuration model for transport execution."""
    id: Optional[str] = None
    name: Optional[str] = "Default SMTP"
    host: str = Field(default="127.0.0.1", description="SMTP server hostname or 'sandbox'")
    port: int = Field(default=587, description="SMTP server port")
    username: Optional[str] = Field(default=None, description="SMTP username")
    password: Optional[str] = Field(default=None, description="SMTP password")
    use_tls: bool = Field(default=True, description="STARTTLS")
    use_ssl: bool = Field(default=False, description="Implicit SSL/TLS")
    timeout: int = Field(default=30, description="Connection timeout in seconds")
    rate_limit_per_second: int = Field(default=25)
    daily_quota: int = Field(default=50000)
    is_sandbox: bool = Field(default=False, description="Explicit sandbox override")
    simulated_delay_sec: float = Field(default=0.03, description="Delay in sandbox mode")


class SMTPTestRequest(BaseModel):
    smtp_config_id: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    use_tls: Optional[bool] = None
    use_ssl: Optional[bool] = None
    test_recipient: Optional[str] = Field(default="test@example.com", description="Email address to receive the test verification probe")


class SMTPTestResponse(BaseModel):
    success: bool
    message: str
    latency_ms: float
    details: Dict[str, Any] = Field(default_factory=dict)


# ----------------------------------------------------------------------
# Campaign Models
# ----------------------------------------------------------------------

class CampaignBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Campaign identifier name")
    subject: str = Field(..., description="Email subject line (supports merge tags)")
    template_id: Optional[str] = Field(default=None, description="Associated Template ID")
    list_id: Optional[str] = Field(default=None, description="Target Subscriber List ID")
    smtp_config_id: Optional[str] = Field(default=None, description="SMTP configuration profile to dispatch with")
    sender_name: str = Field(..., description="Display From Name")
    sender_email: str = Field(..., description="From Email Address")
    reply_to: Optional[str] = Field(default=None, description="Reply-To address")
    headers: Dict[str, str] = Field(default_factory=dict, description="Custom SMTP headers")
    track_opens: bool = Field(default=True, description="Inject open tracking beacon")
    track_clicks: bool = Field(default=True, description="Wrap links for click tracking")
    custom_html: Optional[str] = Field(default=None, description="Direct HTML override if template is not used")
    custom_text: Optional[str] = Field(default=None, description="Direct Plain Text override")


class CampaignCreate(CampaignBase):
    scheduled_at: Optional[str] = Field(
        default=None,
        description="ISO timestamp string to schedule campaign sending"
    )


class CampaignUpdate(BaseModel):
    name: Optional[str] = Field(default=None)
    subject: Optional[str] = Field(default=None)
    template_id: Optional[str] = Field(default=None)
    list_id: Optional[str] = Field(default=None)
    smtp_config_id: Optional[str] = Field(default=None)
    sender_name: Optional[str] = Field(default=None)
    sender_email: Optional[str] = Field(default=None)
    reply_to: Optional[str] = Field(default=None)
    headers: Optional[Dict[str, str]] = Field(default=None)
    track_opens: Optional[bool] = Field(default=None)
    track_clicks: Optional[bool] = Field(default=None)
    custom_html: Optional[str] = Field(default=None)
    custom_text: Optional[str] = Field(default=None)
    status: Optional[CampaignStatus] = Field(default=None)
    scheduled_at: Optional[str] = Field(default=None)


class CampaignResponse(CampaignBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: CampaignStatus
    scheduled_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    total_recipients: int = 0
    sent_count: int = 0
    delivered_count: int = 0
    failed_count: int = 0
    open_count: int = 0
    unsubscribe_count: int = 0
    unsubscribed_count: int = 0
    bounce_count: int = 0

    created_at: str
    updated_at: str


class Campaign(BaseModel):
    """Mass email broadcast campaign specification and live metrics."""
    id: str = Field(..., description="Unique campaign ID")
    name: str = Field(..., description="Human-readable campaign title")
    subject: str = Field(..., description="Email subject template")
    template_id: Optional[str] = None
    list_id: Optional[str] = None
    template_html: Optional[str] = Field(default="", description="Email HTML body template")
    template_text: Optional[str] = Field(default="", description="Email plain text template")
    sender_name: Optional[str] = Field(default=None, description="Sender display name")
    sender_email: Optional[str] = Field(default=None, description="Sender email address")
    reply_to: Optional[str] = None
    smtp_config_id: Optional[str] = None
    smtp_config: Optional[SMTPConfig] = None
    track_opens: bool = Field(default=True, description="Inject open tracking beacon")
    track_clicks: bool = Field(default=True, description="Wrap links for click tracking")
    status: str = Field(default=CampaignStatus.DRAFT.value, description="Current campaign status")
    total_recipients: int = Field(default=0, description="Total queued subscribers")
    sent_count: int = Field(default=0, description="Successfully sent subscriber count")
    delivered_count: int = Field(default=0)
    failed_count: int = Field(default=0, description="Failed subscriber count")
    open_count: int = Field(default=0, description="Unique opens count")
    click_count: int = Field(default=0, description="Unique clicks count")
    unsubscribe_count: int = Field(default=0)
    bounce_count: int = Field(default=0)
    rate_limit_per_sec: int = Field(default=25, description="Rate limit (emails per second)")
    concurrency_limit: int = Field(default=10, description="Max concurrent async send tasks")
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Campaign creation timestamp"
    )
    updated_at: Optional[str] = None
    started_at: Optional[str] = Field(default=None, description="Campaign start timestamp")
    completed_at: Optional[str] = Field(default=None, description="Campaign completion timestamp")


class CampaignStatsResponse(BaseModel):
    campaign_id: str
    campaign_name: str
    status: CampaignStatus
    total_recipients: int
    sent_count: int
    delivered_count: int
    failed_count: int
    open_count: int
    unique_opens: int
    click_count: int
    unique_clicks: int
    unsubscribe_count: int
    bounce_count: int
    delivery_rate_percent: float = 0.0
    open_rate_percent: float = 0.0
    click_through_rate_percent: float = 0.0
    bounce_rate_percent: float = 0.0
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None


# ----------------------------------------------------------------------
# Sent Email Storage Vault & Archiving Models
# ----------------------------------------------------------------------

class SentEmailBase(BaseModel):
    id: str = Field(..., description="Unique UUID identifier for the sent email record")
    campaign_id: Optional[str] = Field(
        default=None,
        description="Associated campaign ID, or None for standalone transactional emails"
    )
    recipient_email: str = Field(..., description="Target recipient email address")
    recipient_name: Optional[str] = Field(default=None, description="Target recipient display name")
    sender_email: str = Field(..., description="From sender email address")
    sender_name: Optional[str] = Field(default=None, description="From sender display name")
    subject: str = Field(..., description="Sent email subject line")
    body_html: Optional[str] = Field(default=None, description="Full rendered HTML body archive")
    body_text: Optional[str] = Field(default=None, description="Full rendered plain text archive")
    headers_json: Optional[str] = Field(default="{}", description="JSON string of RFC headers")
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description="Serialized headers dictionary"
    )
    status: EmailStatus = Field(
        default=EmailStatus.QUEUED,
        description="Delivery lifecycle status ('queued', 'sending', 'sent', 'delivered', 'failed', 'bounced')"
    )
    error_message: Optional[str] = Field(default=None, description="SMTP error diagnostic message if delivery failed")
    message_id: Optional[str] = Field(default=None, description="RFC 2822 Message-ID or provider response ID")
    open_count: int = Field(default=0, description="Total tracking beacon views recorded")
    click_count: int = Field(default=0, description="Total tracked link clicks recorded")
    first_opened_at: Optional[str] = Field(default=None, description="Timestamp of first recorded open")
    last_opened_at: Optional[str] = Field(default=None, description="Timestamp of most recent recorded open")
    raw_eml_path: Optional[str] = Field(default=None, description="File path to raw persisted .eml message file")
    metadata_json: Optional[str] = Field(default="{}", description="JSON string of metadata")
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Merge variables, custom tags, and tracking tokens used for this recipient"
    )
    created_at: str = Field(..., description="Record creation timestamp")
    sent_at: Optional[str] = Field(default=None, description="Timestamp when SMTP delivery was acknowledged")


class SentEmailCreate(BaseModel):
    id: Optional[str] = Field(default=None, description="Optional custom UUID; generated if omitted")
    campaign_id: Optional[str] = None
    recipient_email: str
    recipient_name: Optional[str] = None
    sender_email: str
    sender_name: Optional[str] = None
    subject: str
    body_html: Optional[str] = None
    body_text: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)
    headers_json: Optional[str] = None
    status: EmailStatus = EmailStatus.QUEUED
    error_message: Optional[str] = None
    message_id: Optional[str] = None
    raw_eml_path: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    metadata_json: Optional[str] = None
    created_at: Optional[str] = None
    sent_at: Optional[str] = None


class SentEmailUpdate(BaseModel):
    status: Optional[EmailStatus] = None
    error_message: Optional[str] = None
    message_id: Optional[str] = None
    open_count: Optional[int] = None
    click_count: Optional[int] = None
    first_opened_at: Optional[str] = None
    last_opened_at: Optional[str] = None
    raw_eml_path: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    metadata_json: Optional[str] = None
    sent_at: Optional[str] = None


class SentEmailResponse(SentEmailBase):
    model_config = ConfigDict(from_attributes=True)


class EmailRecord(BaseModel):
    """Complete sent email representation stored in database and storage vault."""
    id: str = Field(..., description="Unique email storage ID (e.g. eml_...)")
    campaign_id: Optional[str] = Field(default=None, description="Associated campaign ID if part of a broadcast")
    subscriber_id: Optional[str] = Field(default=None, description="Associated subscriber ID")
    recipient_email: str = Field(..., description="Recipient email address")
    recipient_name: Optional[str] = Field(default="", description="Recipient display name")
    sender_email: str = Field(..., description="Sender from email address")
    sender_name: Optional[str] = Field(default="", description="Sender display name")
    subject: str = Field(..., description="Interpolated email subject line")
    body_text: Optional[str] = Field(default="", description="Plain text alternative version")
    body_html: Optional[str] = Field(default="", description="Raw HTML before tracking injection")
    rendered_html: Optional[str] = Field(default="", description="Rendered HTML with tracking pixel and rewritten links")
    raw_headers_json: Optional[str] = Field(default="{}", description="JSON string of RFC 5322 headers")
    headers_json: Optional[str] = Field(default="{}", description="JSON string of RFC headers")
    eml_file_path: Optional[str] = Field(default=None, description="Path to raw .eml file on disk")
    raw_eml_path: Optional[str] = Field(default=None, description="Path to raw .eml file on disk")
    eml_size_bytes: int = Field(default=0, description="Size of raw .eml file in bytes")
    message_id: Optional[str] = Field(default=None, description="RFC 5322 Message-ID header value")
    status: str = Field(default=EmailStatus.QUEUED.value, description="Current email delivery status")
    smtp_host: Optional[str] = Field(default=None, description="SMTP server hostname used")
    smtp_port: Optional[int] = Field(default=None, description="SMTP port used")
    is_sandbox: bool = Field(default=False, description="Whether email was sent in simulation mode")
    error_message: Optional[str] = Field(default=None, description="Failure diagnostic message if error occurred")
    delivery_latency_ms: Optional[float] = Field(default=None, description="Time taken to dispatch email in ms")
    open_count: int = Field(default=0, description="Total number of times tracking pixel was loaded")
    first_opened_at: Optional[str] = Field(default=None, description="ISO timestamp of first open")
    opened_at: Optional[str] = Field(default=None, description="ISO timestamp of first open")
    last_opened_at: Optional[str] = Field(default=None, description="ISO timestamp of last open")
    click_count: int = Field(default=0, description="Total number of tracking links clicked")
    clicked_at: Optional[str] = Field(default=None, description="ISO timestamp of first link click")
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO timestamp when record was initialized"
    )
    sent_at: Optional[str] = Field(default=None, description="ISO timestamp when email was transmitted")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary custom tags and metadata")
    metadata_json: Optional[str] = Field(default="{}", description="JSON string of metadata")


class SendResult(BaseModel):
    """Result of an SMTP transmission attempt."""
    success: bool = Field(..., description="Whether email was successfully accepted by SMTP server")
    storage_id: str = Field(..., description="Unique email storage vault ID")
    message_id: Optional[str] = Field(default=None, description="Generated RFC 5322 Message-ID")
    status: str = Field(..., description="Final status (sent, simulated, failed, bounced)")
    error: Optional[str] = Field(default=None, description="Error message if send failed")
    latency_ms: float = Field(default=0.0, description="Dispatch duration in milliseconds")
    smtp_response: Optional[str] = Field(default=None, description="Raw SMTP response message from server")
    eml_path: Optional[str] = Field(default=None, description="Path where raw .eml is archived")


class SentEmailFilter(BaseModel):
    campaign_id: Optional[str] = None
    recipient_email: Optional[str] = None
    status: Optional[EmailStatus] = None
    search_query: Optional[str] = Field(default=None, description="Keyword search across subject and recipient")
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class SentEmailVaultSummary(BaseModel):
    total_archived: int
    queued_count: int
    sent_count: int
    delivered_count: int
    failed_count: int
    bounced_count: int
    total_opens: int
    total_clicks: int
    storage_disk_usage_bytes: int = 0


# ----------------------------------------------------------------------
# Tracking & Email Events
# ----------------------------------------------------------------------

class EmailEventBase(BaseModel):
    sent_email_id: str = Field(..., description="Foreign key reference to sent_emails.id")
    campaign_id: Optional[str] = Field(default=None, description="Associated campaign ID if applicable")
    event_type: Union[EventType, str] = Field(..., description="Type of event: open, click, unsubscribe, bounce, etc.")
    ip_address: Optional[str] = Field(default=None, description="Client IP address recording the event")
    user_agent: Optional[str] = Field(default=None, description="Client User-Agent browser / email client")
    event_payload: Dict[str, Any] = Field(
        default_factory=dict,
        description="Detailed event context (clicked URL, bounce reason, provider response code, etc.)"
    )


class EmailEventCreate(EmailEventBase):
    pass


class EmailEventResponse(EmailEventBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: str


class TimelineEvent(BaseModel):
    """Audit timeline record for lifecycle milestones of an email."""
    id: Optional[Union[int, str]] = Field(default=None, description="Autoincrement or string event ID")
    email_storage_id: str = Field(..., description="Storage ID of the target email")
    event_type: str = Field(..., description="Type of event (created, queued, sent, opened, clicked, etc.)")
    event_timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 UTC timestamp"
    )
    event_data: Dict[str, Any] = Field(default_factory=dict, description="Contextual payload (IP, User-Agent, URL, error)")


# ----------------------------------------------------------------------
# Transactional Send Requests
# ----------------------------------------------------------------------

class TransactionalSendRequest(BaseModel):
    recipient_email: str = Field(..., description="Target recipient email address")
    recipient_name: Optional[str] = Field(default=None, description="Optional recipient name")
    sender_email: Optional[str] = Field(default=None, description="Sender email (uses system default if omitted)")
    sender_name: Optional[str] = Field(default=None, description="Sender display name (uses system default if omitted)")
    reply_to: Optional[str] = Field(default=None, description="Optional Reply-To header")
    subject: str = Field(..., description="Email subject line (supports merge variables)")
    template_id: Optional[str] = Field(default=None, description="Optional template ID to render body from")
    body_html: Optional[str] = Field(default=None, description="HTML message body if not using template")
    body_text: Optional[str] = Field(default=None, description="Plain text fallback body")
    merge_variables: Dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("merge_variables", "template_context"),
        description="Key-value pairs to interpolate into subject and body (e.g. {{name}}, {{reset_link}})"
    )
    headers: Dict[str, str] = Field(default_factory=dict, description="Custom SMTP headers")
    smtp_config_id: Optional[str] = Field(default=None, description="Specific SMTP config ID to use")
    track_opens: bool = Field(default=True, description="Enable open tracking beacon")
    track_clicks: bool = Field(default=True, description="Enable click link tracking")
    tags: List[str] = Field(default_factory=list, description="Categorization tags for storage vault indexing")


class TransactionalEmailRequest(BaseModel):
    """Parameters for on-demand single transactional email dispatch."""
    recipient_email: str = Field(..., description="Recipient email address")
    recipient_name: Optional[str] = Field(default="", description="Recipient display name")
    sender_email: Optional[str] = Field(default=None, description="Custom sender email")
    sender_name: Optional[str] = Field(default=None, description="Custom sender name")
    subject: str = Field(..., description="Email subject template or string")
    html_content: Optional[str] = Field(default="", description="HTML body template")
    text_content: Optional[str] = Field(default=None, description="Plain text body")
    template_context: Dict[str, Any] = Field(default_factory=dict, description="Variables for merge tags")
    smtp_config: Optional[SMTPConfig] = Field(default=None, description="Custom SMTP configuration")
    headers: Dict[str, str] = Field(default_factory=dict, description="Custom RFC headers")
    track_opens: bool = Field(default=True, description="Inject open tracking pixel")
    track_clicks: bool = Field(default=True, description="Rewrite hyperlinks for click tracking")
    inject_footer: bool = Field(default=True, description="Inject compliance footer")
    attachments: List[Dict[str, Any]] = Field(default_factory=list, description="Attachments list")


class TransactionalSendResponse(BaseModel):
    success: bool
    sent_email_id: str
    storage_id: Optional[str] = None
    message_id: Optional[str] = None
    status: Union[EmailStatus, str]
    sent_at: Optional[str] = None
    error: Optional[str] = None



class BatchSendRecipient(BaseModel):
    email: str
    name: Optional[str] = None
    merge_variables: Dict[str, Any] = Field(default_factory=dict)


class BatchSendRequest(BaseModel):
    recipients: List[BatchSendRecipient]
    subject: str
    template_id: Optional[str] = None
    body_html: Optional[str] = None
    body_text: Optional[str] = None
    sender_name: Optional[str] = None
    sender_email: Optional[str] = None
    reply_to: Optional[str] = None
    smtp_config_id: Optional[str] = None
    track_opens: bool = True
    track_clicks: bool = True
    tags: List[str] = Field(default_factory=list)


class BatchSendResponse(BaseModel):
    total_queued: int
    batch_ids: List[str]
    campaign_id: Optional[str] = None


# ----------------------------------------------------------------------
# Suppression & Unsubscribe Models
# ----------------------------------------------------------------------

class SuppressionBase(BaseModel):
    email: str
    campaign_id: Optional[str] = None
    reason: Optional[str] = Field(default="user_unsubscribed", description="Reason for suppression: unsubscribe, hard_bounce, spam_complaint")


class SuppressionCreate(SuppressionBase):
    pass


class SuppressionResponse(SuppressionBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: str


# ----------------------------------------------------------------------
# Automated Email Warmup & Relay Rotation Models
# ----------------------------------------------------------------------

class WarmupCurveStrategy(str, Enum):
    CONSERVATIVE_30 = "conservative_30"
    STANDARD_14 = "standard_14"
    AGGRESSIVE_7 = "aggressive_7"
    CUSTOM = "custom"


class WarmupPreviewRequest(BaseModel):
    total_recipients: int = Field(default=1000, ge=1, le=1000000, description="Total list size to simulate")
    strategy: str = Field(default="conservative_30", description="conservative_30, standard_14, aggressive_7, custom")
    custom_start_cap: Optional[int] = Field(default=50, ge=1, description="Day 1 starting cap for custom curve")
    custom_days: Optional[int] = Field(default=14, ge=2, le=90, description="Duration in days for custom curve")


class WarmupPreviewSlice(BaseModel):
    day: int
    date: str
    daily_cap: int
    cumulative_volume: int
    recommended_providers: Dict[str, int] = Field(default_factory=dict)


class WarmupPreviewResponse(BaseModel):
    strategy: str
    total_days: int
    total_recipients: int
    slices: List[WarmupPreviewSlice]


class WarmupScheduleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Warmup schedule name")
    strategy: str = Field(default="conservative_30", description="Warmup curve profile")
    total_recipients: Optional[int] = Field(default=None, description="Total count if provided directly")
    recipient_emails: Optional[List[str]] = Field(default=None, description="Direct recipient emails to partition")
    list_id: Optional[str] = Field(default=None, description="Subscriber list ID to pull audience from")
    campaign_id: Optional[str] = Field(default=None, description="Associated base campaign template")
    relay_ids: Optional[List[str]] = Field(default=None, description="SMTP relay profile IDs in rotation pool")
    rotation_mode: str = Field(default="round_robin", description="round_robin, failover, weighted")
    start_date: Optional[str] = Field(default=None, description="Target launch timestamp (defaults to now)")


class WarmupSliceResponse(BaseModel):
    id: str
    schedule_id: str
    day_number: int
    scheduled_for: str
    target_count: int
    dispatched_count: int = 0
    bounce_count: int = 0
    failure_count: int = 0
    campaign_id: Optional[str] = None
    status: str = "pending"
    created_at: str


class WarmupScheduleResponse(BaseModel):
    id: str
    name: str
    campaign_id: Optional[str] = None
    strategy: str
    total_recipients: int
    current_day: int
    total_days: int
    daily_cap: int
    sent_today: int
    status: str
    relay_pool: List[str] = Field(default_factory=list)
    rotation_mode: str
    max_bounce_rate: float = 0.02
    slices: List[WarmupSliceResponse] = Field(default_factory=list)
    created_at: str
    updated_at: str


class RelayPoolStatusResponse(BaseModel):
    id: str
    smtp_config_id: str
    name: str
    host: str
    port: int
    in_pool: bool
    current_day: int
    daily_sends: int
    daily_failures: int
    is_cooling_down: bool
    cooldown_until: Optional[str] = None
    last_error: Optional[str] = None

