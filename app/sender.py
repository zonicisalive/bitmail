"""
Email sending engine, MIME builder, template interpolation, tracking beacon injection,
EML archive vault persistence, and aiosmtplib dispatcher.
"""

import email.utils
import hashlib
import hmac
import json
import os
import re
import socket
import ssl
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosmtplib
import jinja2

from app.config import settings
from app.db import get_db, utc_now_iso
from app.models import EmailStatus, EventType


# ----------------------------------------------------------------------
# Token & Tracking Helpers
# ----------------------------------------------------------------------

def generate_unsubscribe_token(email_str: str, subscriber_id: Optional[str] = None) -> str:
    """Generate a tamper-evident unsubscribe token for a subscriber email."""
    data = f"{email_str}:{subscriber_id or ''}"
    sig = hmac.new(
        settings.SECRET_KEY.encode(),
        data.encode(),
        hashlib.sha256
    ).hexdigest()[:16]
    encoded_data = urllib.parse.quote_plus(data)
    return f"{encoded_data}.{sig}"


def verify_unsubscribe_token(token: str) -> Optional[Tuple[str, Optional[str]]]:
    """
    Verify and decode an unsubscribe token. Returns (email, subscriber_id) or None.
    A token without a signature is treated as a bare email or bare subscriber id;
    a token *with* a signature must verify, otherwise it is rejected.
    """
    if not token:
        return None

    if "." not in token:
        if "@" in token:
            return token.strip().lower(), None
        return None, token.strip()

    try:
        encoded_data, sig = token.rsplit(".", 1)
        data = urllib.parse.unquote_plus(encoded_data)
        expected_sig = hmac.new(
            settings.SECRET_KEY.encode(),
            data.encode(),
            hashlib.sha256
        ).hexdigest()[:16]

        if not hmac.compare_digest(sig, expected_sig):
            return None

        parts = data.split(":", 1)
        email_val = parts[0].strip().lower()
        sub_id = parts[1].strip() if len(parts) > 1 and parts[1] else None
        return (email_val or None), sub_id
    except Exception:
        return None


def interpolate_template(template_str: str, variables: Dict[str, Any]) -> str:
    """Render template using Jinja2 with fallback to regex key replacement."""
    if not template_str:
        return ""

    try:
        from jinja2.sandbox import SandboxedEnvironment
        jinja_env = SandboxedEnvironment(
            autoescape=False,
            undefined=jinja2.Undefined
        )
        t = jinja_env.from_string(template_str)
        return t.render(**variables)
    except Exception:
        result = template_str
        for key, val in variables.items():
            if val is not None:
                pattern = re.compile(r"\{\{\s*" + re.escape(str(key)) + r"\s*\}\}", re.IGNORECASE)
                result = pattern.sub(str(val), result)
        result = re.sub(r"\{\{[^}]+\}\}", "", result)
        return result


def inject_tracking(
    html_content: str,
    email_id: str,
    track_opens: bool = True,
    track_clicks: bool = True,
    base_url: Optional[str] = None
) -> str:
    """
    Inject open tracking beacon and wrap HTML links with redirect tracking endpoints.
    """
    if not html_content:
        return ""

    root_url = (base_url or settings.TRACKING_BASE_URL).rstrip("/")

    # 1. Click link tracking replacement
    if track_clicks:
        def replace_link(match):
            original_url = match.group(2)
            if (
                not original_url
                or original_url.startswith("#")
                or original_url.startswith("javascript:")
                or original_url.startswith("mailto:")
                or original_url.startswith("tel:")
                or "/track/" in original_url
                or "/unsubscribe" in original_url
            ):
                return match.group(0)

            encoded_target = urllib.parse.quote(original_url, safe="")
            tracked_url = f"{root_url}/track/click/{email_id}?url={encoded_target}"
            return f'{match.group(1)}{tracked_url}{match.group(3)}'

        html_content = re.sub(
            r'(<a\s+[^>]*?href=[\'"])([^\'"]+)([\'"][^>]*?>)',
            replace_link,
            html_content,
            flags=re.IGNORECASE
        )

    # 2. Open tracking pixel beacon injection
    if track_opens:
        pixel_url = f"{root_url}/track/open/{email_id}"
        pixel_tag = (
            f'<img src="{pixel_url}" alt="" width="1" height="1" border="0" '
            f'style="height:1px!important;width:1px!important;border-width:0!important;'
            f'margin:0!important;padding:0!important;display:none;" />'
        )
        if "</body>" in html_content.lower():
            pattern = re.compile(r"</body>", re.IGNORECASE)
            html_content = pattern.sub(f"{pixel_tag}\n</body>", html_content, count=1)
        else:
            html_content += f"\n{pixel_tag}"

    return html_content


# ----------------------------------------------------------------------
# MIME Builder & Storage Vault Archiver
# ----------------------------------------------------------------------

def build_mime_message(
    sender_name: Optional[str],
    sender_email: str,
    recipient_name: Optional[str],
    recipient_email: str,
    subject: str,
    body_html: Optional[str],
    body_text: Optional[str],
    headers_dict: Optional[Dict[str, str]] = None,
    email_id: Optional[str] = None,
    unsubscribe_url: Optional[str] = None
) -> Tuple[MIMEMultipart, str]:
    """
    Construct a standards-compliant MIME Multipart email message.
    Returns (msg_object, raw_eml_string).
    """
    msg = MIMEMultipart("alternative")

    from_header = email.utils.formataddr((sender_name or "", sender_email)) if sender_name else sender_email
    to_header = email.utils.formataddr((recipient_name or "", recipient_email)) if recipient_name else recipient_email

    msg["From"] = from_header
    msg["To"] = to_header
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)

    domain = sender_email.split("@")[-1] if "@" in sender_email else "nexusmail.local"
    unique_tag = email_id or uuid.uuid4().hex[:12]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    msg_id = f"<{unique_tag}.{timestamp}@{domain}>"
    msg["Message-ID"] = msg_id

    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    if headers_dict:
        for k, v in headers_dict.items():
            if k.lower() not in ["from", "to", "subject", "date", "message-id"]:
                msg[k] = str(v)

    if body_text:
        part_text = MIMEText(body_text, "plain", "utf-8")
        msg.attach(part_text)

    if body_html:
        part_html = MIMEText(body_html, "html", "utf-8")
        msg.attach(part_html)

    raw_eml_str = msg.as_string()
    return msg, raw_eml_str


def persist_raw_eml(email_id: str, raw_eml_content: str) -> str:
    """Save raw .eml file to sent email archive vault directories."""
    settings.ensure_directories()
    file_name = f"{email_id}.eml"
    primary_path = settings.EML_STORAGE_DIR / file_name

    with open(primary_path, "w", encoding="utf-8") as f:
        f.write(raw_eml_content)

    archive_path = settings.EML_ARCHIVE_DIR / file_name
    try:
        with open(archive_path, "w", encoding="utf-8") as f:
            f.write(raw_eml_content)
    except Exception:
        pass

    return str(primary_path)


# ----------------------------------------------------------------------
# SMTP Transmission Engine
# ----------------------------------------------------------------------

def is_sandbox_config(smtp_config: Optional[Dict[str, Any]]) -> bool:
    """
    A send is a dry run only when the relay explicitly says so: the is_sandbox flag,
    or the reserved host name 'sandbox'. Loopback and .local hosts are real relays
    (Postfix, MailHog, Mailpit) and must report real delivery failures.
    """
    if not smtp_config:
        return False
    return bool(smtp_config.get("is_sandbox")) or str(smtp_config.get("host", "")).strip().lower() == "sandbox"


async def get_smtp_config_by_id(smtp_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieve SMTP configuration dictionary from SQLite with decrypted password."""
    from app.auth import decrypt_credential
    async with get_db() as db:
        row = None
        if smtp_id:
            async with db.execute("SELECT * FROM smtp_configs WHERE id = ?", (smtp_id,)) as cursor:
                row = await cursor.fetchone()
        if not row:
            async with db.execute("SELECT * FROM smtp_configs WHERE is_default = 1 AND is_active = 1 LIMIT 1") as cursor:
                row = await cursor.fetchone()
        if not row:
            async with db.execute("SELECT * FROM smtp_configs WHERE is_active = 1 LIMIT 1") as cursor:
                row = await cursor.fetchone()

        if row:
            cfg = dict(row)
            if cfg.get("password"):
                cfg["password"] = decrypt_credential(cfg["password"])
            return cfg

    return None


def resolve_mx_host(domain: str) -> Optional[str]:
    """Resolve highest-priority MX host for recipient domain."""
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX")
        mx_records = sorted([(r.preference, str(r.exchange).rstrip(".")) for r in answers])
        if mx_records:
            return mx_records[0][1]
    except Exception:
        pass
    return None


async def dispatch_smtp_message(
    msg: MIMEMultipart,
    sender_email: str,
    recipient_email: str,
    smtp_config: Optional[Dict[str, Any]] = None
) -> Tuple[bool, str, Optional[str]]:
    """
    Transmit MIME message via SMTP connection using aiosmtplib.
    Supports standard authenticated SMTP relays and Direct MX DNS delivery.
    Returns (success, status_or_message, message_id).
    """
    if not smtp_config:
        return (
            False,
            "No SMTP relay configured. Add a relay under SMTP Relays and mark it default, "
            "or use host 'sandbox' to dry-run without sending.",
            msg["Message-ID"],
        )

    host = smtp_config.get("host", "127.0.0.1")
    port = int(smtp_config.get("port", 587))
    username = smtp_config.get("username")
    password = smtp_config.get("password")
    use_tls = bool(smtp_config.get("use_tls", 1))
    use_ssl = bool(smtp_config.get("use_ssl", 0))

    if password and ("gmail" in host.lower() or (username and "gmail" in username.lower())):
        password = password.replace(" ", "").strip()

    # Direct MX Delivery handling
    if host in ["direct_mx", "direct", "mx"]:
        recipient_domain = recipient_email.split("@")[-1].strip().lower() if "@" in recipient_email else ""
        mx_host = resolve_mx_host(recipient_domain) or f"mail.{recipient_domain}"
        try:
            client = aiosmtplib.SMTP(
                hostname=mx_host,
                port=25,
                timeout=12,
                local_hostname=f"mail.{recipient_domain}"
            )
            await client.connect()
            try:
                await client.starttls()
            except Exception:
                pass
            response = await client.send_message(msg, sender=sender_email, recipients=[recipient_email])
            await client.quit()
            resp_text = str(response) if response else f"250 2.0.0 Delivered directly to {mx_host}"
            return True, resp_text, msg["Message-ID"]
        except Exception as mx_err:
            return False, f"Direct MX delivery to {mx_host}: {mx_err}", msg.get("Message-ID")

    # For explicit sandbox mode, simulate transmission
    if is_sandbox_config(smtp_config):
        import asyncio
        delay = float(smtp_config.get("simulated_delay_sec", 0.02) or 0.02)
        await asyncio.sleep(min(0.2, max(0.005, delay)))
        return True, "250 2.0.0 OK: Sandbox message queued and archived", msg["Message-ID"]

    try:
        client = aiosmtplib.SMTP(
            hostname=host,
            port=port,
            use_tls=use_ssl,
            timeout=settings.SMTP_CONNECTION_TIMEOUT_SECONDS
        )

        await client.connect()

        if use_tls and not use_ssl:
            try:
                await client.starttls()
            except Exception as tls_e:
                if "already using tls" not in str(tls_e).lower():
                    raise tls_e

        if username and password:
            await client.login(username, password)

        response = await client.send_message(
            msg,
            sender=sender_email,
            recipients=[recipient_email]
        )
        try:
            await client.quit()
        except Exception:
            pass

        resp_text = str(response) if response else "250 2.0.0 OK Delivered via SMTP Relay"
        return True, resp_text, msg["Message-ID"]

    except Exception as e:
        return False, str(e), msg.get("Message-ID")


# ----------------------------------------------------------------------
# High-Level Email Send Pipeline
# ----------------------------------------------------------------------

async def send_single_email(
    recipient_email: str,
    subject: str,
    body_html: Optional[str] = None,
    body_text: Optional[str] = None,
    recipient_name: Optional[str] = None,
    sender_email: Optional[str] = None,
    sender_name: Optional[str] = None,
    reply_to: Optional[str] = None,
    template_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
    smtp_config_id: Optional[str] = None,
    merge_variables: Optional[Dict[str, Any]] = None,
    custom_headers: Optional[Dict[str, str]] = None,
    track_opens: bool = True,
    track_clicks: bool = True,
    email_id: Optional[str] = None,
    subscriber_id: Optional[str] = None,
    smtp_config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Full pipeline: Interpolates template & merge vars, injects tracking,
    builds MIME message, stores raw .eml, dispatches via SMTP, records in SQLite.
    """
    now = utc_now_iso()
    email_id = email_id or f"msg_{uuid.uuid4().hex[:12]}"
    merge_vars = merge_variables.copy() if merge_variables else {}

    sender_email = sender_email or settings.DEFAULT_SENDER_EMAIL
    sender_name = sender_name or settings.DEFAULT_SENDER_NAME
    headers_dict = custom_headers.copy() if custom_headers else {}
    if reply_to:
        headers_dict["Reply-To"] = reply_to

    unsub_token = generate_unsubscribe_token(recipient_email, subscriber_id or merge_vars.get("subscriber_id"))
    unsub_url = f"{settings.TRACKING_BASE_URL.rstrip('/')}/unsubscribe/{unsub_token}"

    merge_vars.setdefault("email", recipient_email)
    merge_vars.setdefault("first_name", recipient_name or recipient_email.split("@")[0].capitalize())
    merge_vars.setdefault("last_name", "")
    merge_vars.setdefault("name", recipient_name or recipient_email.split("@")[0].capitalize())
    merge_vars.setdefault("unsubscribe_url", unsub_url)
    merge_vars.setdefault("year", datetime.now().year)
    merge_vars.setdefault("company", settings.COMPANY_NAME)

    if subscriber_id:
        try:
            async with get_db() as db:
                async with db.execute("SELECT custom_fields FROM subscribers WHERE id = ?", (subscriber_id,)) as cur:
                    srow = await cur.fetchone()
                    if srow and srow["custom_fields"]:
                        cf_data = json.loads(srow["custom_fields"])
                        if isinstance(cf_data, dict):
                            for cfk, cfv in cf_data.items():
                                merge_vars.setdefault(cfk, cfv)
                                merge_vars.setdefault(cfk.lower(), cfv)
        except Exception:
            pass

    if template_id:
        async with get_db() as db:
            async with db.execute("SELECT * FROM templates WHERE id = ?", (template_id,)) as cursor:
                tpl_row = await cursor.fetchone()
                if tpl_row:
                    tpl_dict = dict(tpl_row)
                    if not subject:
                        subject = tpl_dict.get("subject", subject)
                    if not body_html:
                        body_html = tpl_dict.get("body_html")
                    if not body_text:
                        body_text = tpl_dict.get("body_text")

    rendered_subject = interpolate_template(subject or "No Subject", merge_vars)
    rendered_html = interpolate_template(body_html or "", merge_vars) if body_html else None
    rendered_text = interpolate_template(body_text or "", merge_vars) if body_text else None

    if rendered_html:
        rendered_html = inject_tracking(
            html_content=rendered_html,
            email_id=email_id,
            track_opens=track_opens,
            track_clicks=track_clicks,
            base_url=settings.TRACKING_BASE_URL
        )

    if campaign_id:
        headers_dict["X-Campaign-ID"] = str(campaign_id)

    mime_msg, raw_eml_str = build_mime_message(
        sender_name=sender_name,
        sender_email=sender_email,
        recipient_name=recipient_name,
        recipient_email=recipient_email,
        subject=rendered_subject,
        body_html=rendered_html,
        body_text=rendered_text,
        headers_dict=headers_dict,
        email_id=email_id,
        unsubscribe_url=unsub_url
    )

    raw_eml_path = persist_raw_eml(email_id, raw_eml_str)
    if smtp_config is None:
        smtp_config = await get_smtp_config_by_id(smtp_config_id)

    is_sandbox = is_sandbox_config(smtp_config)

    async with get_db() as db:
        await db.execute("""
            INSERT OR REPLACE INTO sent_emails (
                id, campaign_id, subscriber_id, recipient_email, recipient_name,
                sender_email, sender_name, subject, body_html, body_text, rendered_html,
                headers, raw_headers_json, status, is_sandbox, error_message, message_id,
                open_count, click_count, first_opened_at, last_opened_at,
                raw_eml_path, eml_file_path, metadata, metadata_json, created_at, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, NULL, NULL, ?, ?, ?, ?, ?, NULL)
        """, (
            email_id,
            campaign_id,
            subscriber_id or merge_vars.get("subscriber_id"),
            recipient_email,
            recipient_name,
            sender_email,
            sender_name,
            rendered_subject,
            rendered_html,
            rendered_text,
            rendered_html,
            json.dumps(dict(mime_msg.items())),
            json.dumps(dict(mime_msg.items())),
            EmailStatus.SENDING.value,
            1 if is_sandbox else 0,
            None,
            mime_msg["Message-ID"],
            raw_eml_path,
            raw_eml_path,
            json.dumps(merge_vars),
            json.dumps(merge_vars),
            now
        ))


        await db.execute("""
            INSERT INTO email_events (id, sent_email_id, campaign_id, event_type, ip_address, user_agent, event_payload, created_at)
            VALUES (?, ?, ?, 'queued', '127.0.0.1', 'NexusMail Dispatcher', ?, ?)
        """, (
            f"evt_{uuid.uuid4().hex[:12]}",
            email_id,
            campaign_id,
            json.dumps({"recipient": recipient_email, "relay": smtp_config.get("name", "Default") if smtp_config else "Mock"}),
            now
        ))
        await db.commit()

    success, result_message, msg_id = await dispatch_smtp_message(
        msg=mime_msg,
        sender_email=sender_email,
        recipient_email=recipient_email,
        smtp_config=smtp_config
    )

    sent_timestamp = utc_now_iso()
    if is_sandbox and success:
        final_status = EmailStatus.SIMULATED.value
    else:
        final_status = EmailStatus.SENT.value if success else EmailStatus.FAILED.value
    error_detail = None if success else result_message

    async with get_db() as db:
        await db.execute("""
            UPDATE sent_emails
            SET status = ?, is_sandbox = ?, error_message = ?, message_id = ?, sent_at = ?
            WHERE id = ?
        """, (final_status, 1 if is_sandbox else 0, error_detail, msg_id, sent_timestamp if success else None, email_id))


        if success:
            await db.execute("""
                INSERT INTO email_events (id, sent_email_id, campaign_id, event_type, ip_address, user_agent, event_payload, created_at)
                VALUES 
                (?, ?, ?, 'sent', '127.0.0.1', 'NexusMail Dispatcher', ?, ?),
                (?, ?, ?, 'delivered', '127.0.0.1', 'Remote SMTP Relay', ?, ?)
            """, (
                f"evt_{uuid.uuid4().hex[:12]}",
                email_id,
                campaign_id,
                json.dumps({"smtp_response": result_message}),
                sent_timestamp,
                f"evt_{uuid.uuid4().hex[:12]}",
                email_id,
                campaign_id,
                json.dumps({"smtp_response": result_message}),
                sent_timestamp
            ))
        else:
            await db.execute("""
                INSERT INTO email_events (id, sent_email_id, campaign_id, event_type, ip_address, user_agent, event_payload, created_at)
                VALUES (?, ?, ?, 'failed', '127.0.0.1', 'NexusMail Dispatcher', ?, ?)
            """, (
                f"evt_{uuid.uuid4().hex[:12]}",
                email_id,
                campaign_id,
                json.dumps({"error": result_message}),
                sent_timestamp
            ))

        await db.commit()

    return {
        "success": success,
        "sent_email_id": email_id,
        "message_id": msg_id,
        "status": final_status,
        "sent_at": sent_timestamp if success else None,
        "error": error_detail,
        "raw_eml_path": raw_eml_path
    }






class EmailSender:
    """Enterprise SMTP and simulated delivery sender client."""

    def __init__(self, timeout: int = 30) -> None:
        self.timeout = timeout

    async def send_email(
        self,
        recipient: Any,
        rendered: Any,
        storage_id: Optional[str] = None,
        campaign_id: Optional[str] = None,
        sender_email: Optional[str] = None,
        sender_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        smtp_config: Optional[Any] = None,
    ) -> Any:
        """Send an email using rendered template content and persist to vault."""
        recipient_email = recipient.email if hasattr(recipient, "email") else str(recipient)
        recipient_name = getattr(recipient, "first_name", None) or getattr(recipient, "name", None)
        sub_id = getattr(recipient, "id", None)

        subject = rendered.subject if hasattr(rendered, "subject") else getattr(rendered, "rendered_subject", "No Subject")
        html_body = rendered.rendered_html if hasattr(rendered, "rendered_html") else getattr(rendered, "html", "")
        text_body = getattr(rendered, "rendered_text", None) or getattr(rendered, "text", None) or ""

        start_time = time.monotonic()
        
        is_sandbox = False
        if smtp_config:
            is_sandbox = getattr(smtp_config, "is_sandbox", False) or getattr(smtp_config, "host", "") == "sandbox"
        
        smtp_dict = None
        if smtp_config:
            if hasattr(smtp_config, "model_dump"):
                smtp_dict = smtp_config.model_dump()
            elif isinstance(smtp_config, dict):
                smtp_dict = smtp_config

        res = await send_single_email(
            recipient_email=recipient_email,
            recipient_name=recipient_name,
            subscriber_id=sub_id,
            subject=subject,
            body_html=html_body,
            body_text=text_body,
            sender_email=sender_email or settings.DEFAULT_SENDER_EMAIL,
            sender_name=sender_name or settings.DEFAULT_SENDER_NAME,
            reply_to=reply_to,
            campaign_id=campaign_id,
            smtp_config=smtp_dict,
            # The template engine already injected the beacon and rewrote the links
            # into `rendered`; injecting again would double-count every open.
            track_opens=False,
            track_clicks=False,
            email_id=storage_id,
        )

        latency = (time.monotonic() - start_time) * 1000

        from app.models import SendResult
        return SendResult(
            success=res.get("success", False),
            storage_id=res.get("sent_email_id") or storage_id or f"eml_{uuid.uuid4().hex}",
            message_id=res.get("message_id"),
            status=EmailStatus.SIMULATED.value if is_sandbox and res.get("success") else (EmailStatus.SENT.value if res.get("success") else EmailStatus.FAILED.value),
            error=res.get("error"),
            latency_ms=latency,
            smtp_response="250 OK - Message queued for delivery" if res.get("success") else res.get("error"),
            eml_path=res.get("raw_eml_path"),
        )


email_sender = EmailSender()

