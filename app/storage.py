"""
Email Storage Vault Service.
Manages high-durability persistence of sent emails, raw MIME .eml files on disk,
full audit timeline event logging, open/click tracking logs, and email export capabilities.
"""

import asyncio
from datetime import datetime, timezone
import email
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import uuid
import aiofiles

from app.config import settings
from app.db import get_db
from app.models import EmailRecord, EmailStatus, EventType, TimelineEvent

logger = logging.getLogger("mass_email.storage")


class EmailStorageVault:
    """Enterprise Storage Vault for persistent archiving, query, and audit trail of emails."""

    def __init__(self, archive_base_dir: Optional[Path] = None) -> None:
        self.archive_base_dir = archive_base_dir or settings.EML_ARCHIVE_DIR
        self.archive_base_dir.mkdir(parents=True, exist_ok=True)

    def generate_storage_id(self) -> str:
        """Generate a collision-resistant unique storage identifier."""
        return f"eml_{uuid.uuid4().hex}"

    def get_eml_path_for_email(self, storage_id: str, timestamp: Optional[str] = None) -> Path:
        """
        Generate structured EML archive file path:
        data/eml_archive/{year}/{month}/{day}/{id}.eml
        """
        if timestamp:
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except Exception:
                dt = datetime.now(timezone.utc)
        else:
            dt = datetime.now(timezone.utc)

        year_str = dt.strftime("%Y")
        month_str = dt.strftime("%m")
        day_str = dt.strftime("%d")

        target_dir = self.archive_base_dir / year_str / month_str / day_str
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / f"{storage_id}.eml"

    def build_eml_message(
        self,
        record: EmailRecord,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> EmailMessage:
        """Construct a standards-compliant Python EmailMessage object from an EmailRecord."""
        msg = EmailMessage()
        
        # Core RFC 5322 Headers
        msg["Subject"] = record.subject
        msg["From"] = f"{record.sender_name} <{record.sender_email}>" if record.sender_name else record.sender_email
        msg["To"] = f"{record.recipient_name} <{record.recipient_email}>" if record.recipient_name else record.recipient_email
        
        if record.sent_at:
            try:
                dt = datetime.fromisoformat(record.sent_at.replace("Z", "+00:00"))
                msg["Date"] = formatdate(dt.timestamp(), localtime=False)
            except Exception:
                msg["Date"] = formatdate(localtime=False)
        else:
            msg["Date"] = formatdate(localtime=False)

        domain = record.sender_email.split("@")[-1] if "@" in record.sender_email else "enterprisemail.local"
        msg_id = record.message_id or make_msgid(domain=domain)
        msg["Message-ID"] = msg_id

        # Enterprise & Compliance Headers
        msg["X-Mailer"] = "Enterprise Mass Email Engine v1.0"
        msg["X-Storage-ID"] = record.id
        if record.campaign_id:
            msg["X-Campaign-ID"] = record.campaign_id

        # Parse and append raw_headers_json if present
        if record.raw_headers_json:
            try:
                headers = json.loads(record.raw_headers_json)
                for k, v in headers.items():
                    if k not in msg and v is not None:
                        msg[k] = str(v)
            except Exception:
                pass

        if custom_headers:
            for k, v in custom_headers.items():
                if k not in msg and v is not None:
                    msg[k] = str(v)

        # Set body contents
        plain_text = record.body_text or ""
        html_content = record.rendered_html or record.body_html or ""

        if html_content and plain_text:
            msg.set_content(plain_text, subtype="plain", charset="utf-8")
            msg.add_alternative(html_content, subtype="html", charset="utf-8")
        elif html_content:
            msg.set_content(html_content, subtype="html", charset="utf-8")
        else:
            msg.set_content(plain_text, subtype="plain", charset="utf-8")

        return msg

    async def write_eml_to_disk(self, storage_id: str, eml_bytes: bytes, timestamp: Optional[str] = None) -> Tuple[str, int]:
        """Write raw EML bytes asynchronously to the date-partitioned archive on disk."""
        file_path = self.get_eml_path_for_email(storage_id, timestamp)
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(eml_bytes)
        size_bytes = len(eml_bytes)
        return str(file_path), size_bytes

    async def save_email(
        self,
        record: EmailRecord,
        eml_bytes: Optional[bytes] = None,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Store email in the Email Storage Vault:
        1. Generates storage ID if not present.
        2. Builds and serializes MIME .eml file.
        3. Saves .eml file to data/eml_archive/{year}/{month}/{day}/{id}.eml.
        4. Inserts sent_emails record into SQLite.
        5. Logs initial audit timeline event.
        """
        if not record.id:
            record.id = self.generate_storage_id()

        if not record.created_at:
            record.created_at = datetime.now(timezone.utc).isoformat()

        # Build raw EML bytes if not provided
        if eml_bytes is None and settings.STORE_RAW_EML_FILES:
            eml_msg = self.build_eml_message(record, custom_headers)
            eml_bytes = eml_msg.as_bytes()
            if not record.message_id:
                record.message_id = eml_msg["Message-ID"]

            # Save extracted headers dict into raw_headers_json
            headers_dict = {k: str(v) for k, v in eml_msg.items()}
            record.raw_headers_json = json.dumps(headers_dict)

        # Write EML to disk archive
        if eml_bytes is not None and settings.STORE_RAW_EML_FILES:
            file_path, size_bytes = await self.write_eml_to_disk(record.id, eml_bytes, record.created_at)
            record.eml_file_path = file_path
            record.eml_size_bytes = size_bytes

        async with get_db() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO sent_emails (
                    id, campaign_id, subscriber_id, recipient_email, recipient_name,
                    sender_email, sender_name, subject, body_text, body_html, rendered_html,
                    raw_headers_json, eml_file_path, eml_size_bytes, message_id, status,
                    smtp_host, smtp_port, is_sandbox, error_message, delivery_latency_ms,
                    open_count, opened_at, click_count, clicked_at, created_at, sent_at,
                    metadata_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    record.id,
                    record.campaign_id,
                    record.subscriber_id,
                    record.recipient_email,
                    record.recipient_name or "",
                    record.sender_email,
                    record.sender_name or "",
                    record.subject,
                    record.body_text or "",
                    record.body_html or "",
                    record.rendered_html or "",
                    record.raw_headers_json or "{}",
                    record.eml_file_path,
                    record.eml_size_bytes,
                    record.message_id,
                    record.status,
                    record.smtp_host,
                    record.smtp_port,
                    1 if record.is_sandbox else 0,
                    record.error_message,
                    record.delivery_latency_ms,
                    record.open_count,
                    record.opened_at,
                    record.click_count,
                    record.clicked_at,
                    record.created_at,
                    record.sent_at,
                    json.dumps(record.metadata or {}),
                ),
            )
            
            # Record creation audit event
            await db.execute(
                """
                INSERT INTO audit_events (email_storage_id, event_type, event_timestamp, event_data_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    record.id,
                    EventType.CREATED.value,
                    record.created_at,
                    json.dumps({
                        "recipient": record.recipient_email,
                        "subject": record.subject,
                        "campaign_id": record.campaign_id,
                    }),
                ),
            )
            await db.commit()

        logger.info("Saved email %s to storage vault (status=%s)", record.id, record.status)
        return record.id

    async def update_status(
        self,
        storage_id: str,
        status: str,
        error_message: Optional[str] = None,
        latency_ms: Optional[float] = None,
        message_id: Optional[str] = None,
        sent_at: Optional[str] = None,
    ) -> None:
        """Update delivery status and latency of a stored email and log timeline event."""
        now_iso = datetime.now(timezone.utc).isoformat()
        final_sent_at = sent_at or (now_iso if status in (EmailStatus.SENT.value, EmailStatus.SIMULATED.value) else None)

        async with get_db() as db:
            updates = ["status = ?"]
            params: List[Any] = [status]

            if error_message is not None:
                updates.append("error_message = ?")
                params.append(error_message)

            if latency_ms is not None:
                updates.append("delivery_latency_ms = ?")
                params.append(latency_ms)

            if message_id is not None:
                updates.append("message_id = ?")
                params.append(message_id)

            if final_sent_at:
                updates.append("sent_at = ?")
                params.append(final_sent_at)

            params.append(storage_id)
            query = f"UPDATE sent_emails SET {', '.join(updates)} WHERE id = ?"
            await db.execute(query, params)

            # Record timeline event
            event_payload = {
                "status": status,
                "latency_ms": latency_ms,
                "error": error_message,
                "message_id": message_id,
            }
            await db.execute(
                """
                INSERT INTO audit_events (email_storage_id, event_type, event_timestamp, event_data_json)
                VALUES (?, ?, ?, ?)
                """,
                (storage_id, status, now_iso, json.dumps(event_payload)),
            )
            await db.commit()

    async def record_timeline_event(
        self,
        storage_id: str,
        event_type: str,
        event_data: Optional[Dict[str, Any]] = None,
        timestamp: Optional[str] = None,
    ) -> TimelineEvent:
        """Insert a specific milestone event into the audit timeline."""
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        data_json = json.dumps(event_data or {})
        
        async with get_db() as db:
            cursor = await db.execute(
                """
                INSERT INTO audit_events (email_storage_id, event_type, event_timestamp, event_data_json)
                VALUES (?, ?, ?, ?)
                """,
                (storage_id, event_type, ts, data_json),
            )
            event_id = cursor.lastrowid
            await db.commit()

        return TimelineEvent(
            id=event_id,
            email_storage_id=storage_id,
            event_type=event_type,
            event_timestamp=ts,
            event_data=event_data or {},
        )

    async def record_open(
        self,
        storage_id: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> bool:
        """Record an open tracking event, increment open count, and update campaign stats."""
        now_iso = datetime.now(timezone.utc).isoformat()

        async with get_db() as db:
            # Check email existence
            cursor = await db.execute(
                "SELECT id, campaign_id, open_count, opened_at FROM sent_emails WHERE id = ?",
                (storage_id,),
            )
            row = await cursor.fetchone()
            if not row:
                logger.warning("Tracking open requested for unknown email storage ID: %s", storage_id)
                return False

            first_open = row["opened_at"] is None
            campaign_id = row["campaign_id"]

            # Insert tracking opens log
            await db.execute(
                """
                INSERT INTO tracking_opens (email_storage_id, ip_address, user_agent, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (storage_id, ip_address, user_agent, now_iso),
            )

            # Update sent_emails record
            await db.execute(
                """
                UPDATE sent_emails
                SET open_count = open_count + 1,
                    opened_at = COALESCE(opened_at, ?)
                WHERE id = ?
                """,
                (now_iso, storage_id),
            )

            # Log timeline event
            await db.execute(
                """
                INSERT INTO audit_events (email_storage_id, event_type, event_timestamp, event_data_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    storage_id,
                    EventType.OPENED.value,
                    now_iso,
                    json.dumps({"ip": ip_address, "user_agent": user_agent, "is_unique": first_open}),
                ),
            )

            # Increment campaign unique opens if applicable
            if campaign_id and first_open:
                await db.execute(
                    "UPDATE campaigns SET open_count = open_count + 1 WHERE id = ?",
                    (campaign_id,),
                )

            await db.commit()

        logger.info("Recorded open for email %s (first_open=%s)", storage_id, first_open)
        return True

    async def record_click(
        self,
        storage_id: str,
        original_url: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> bool:
        """Record a link click tracking event, increment click count, and update campaign stats."""
        now_iso = datetime.now(timezone.utc).isoformat()

        async with get_db() as db:
            cursor = await db.execute(
                "SELECT id, campaign_id, click_count, clicked_at FROM sent_emails WHERE id = ?",
                (storage_id,),
            )
            row = await cursor.fetchone()
            if not row:
                logger.warning("Tracking click requested for unknown email storage ID: %s", storage_id)
                return False

            first_click = row["clicked_at"] is None
            campaign_id = row["campaign_id"]

            # Insert click tracking record
            await db.execute(
                """
                INSERT INTO tracking_clicks (email_storage_id, original_url, ip_address, user_agent, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (storage_id, original_url, ip_address, user_agent, now_iso),
            )

            # Update sent_emails record
            await db.execute(
                """
                UPDATE sent_emails
                SET click_count = click_count + 1,
                    clicked_at = COALESCE(clicked_at, ?)
                WHERE id = ?
                """,
                (now_iso, storage_id),
            )

            # Log timeline event
            await db.execute(
                """
                INSERT INTO audit_events (email_storage_id, event_type, event_timestamp, event_data_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    storage_id,
                    EventType.CLICKED.value,
                    now_iso,
                    json.dumps({"url": original_url, "ip": ip_address, "user_agent": user_agent, "is_unique": first_click}),
                ),
            )

            # Increment campaign unique clicks if applicable
            if campaign_id and first_click:
                await db.execute(
                    "UPDATE campaigns SET click_count = click_count + 1 WHERE id = ?",
                    (campaign_id,),
                )

            await db.commit()

        logger.info("Recorded click on '%s' for email %s (first_click=%s)", original_url, storage_id, first_click)
        return True

    async def record_unsubscribe(
        self,
        email_addr: str,
        storage_id: Optional[str] = None,
        campaign_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """Record an unsubscribe event and update audit timeline."""
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_email = email_addr.lower().strip()

        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO unsubscribes (email, campaign_id, email_storage_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (clean_email, campaign_id, storage_id, reason or "", now_iso),
            )

            if storage_id:
                await db.execute(
                    """
                    INSERT INTO audit_events (email_storage_id, event_type, event_timestamp, event_data_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        storage_id,
                        EventType.UNSUBSCRIBED.value,
                        now_iso,
                        json.dumps({"email": clean_email, "reason": reason}),
                    ),
                )

            if campaign_id:
                await db.execute(
                    "UPDATE campaigns SET unsubscribed_count = unsubscribed_count + 1 WHERE id = ?",
                    (campaign_id,),
                )

            await db.commit()

        logger.info("Processed unsubscribe for %s (campaign_id=%s)", clean_email, campaign_id)
        return True

    async def get_email(self, storage_id: str) -> Optional[EmailRecord]:
        """Fetch full email record by storage ID."""
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT * FROM sent_emails WHERE id = ?",
                (storage_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_record(row)

    async def list_emails(
        self,
        campaign_id: Optional[str] = None,
        recipient_email: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[EmailRecord]:
        """Query emails with flexible filters and pagination."""
        clauses = []
        params: List[Any] = []

        if campaign_id:
            clauses.append("campaign_id = ?")
            params.append(campaign_id)

        if recipient_email:
            clauses.append("recipient_email = ?")
            params.append(recipient_email)

        if status:
            clauses.append("status = ?")
            params.append(status)

        if search:
            clauses.append("(recipient_email LIKE ? OR subject LIKE ? OR id LIKE ?)")
            search_param = f"%{search}%"
            params.extend([search_param, search_param, search_param])

        where_str = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM sent_emails {where_str} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        async with get_db() as db:
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
            return [self._row_to_record(row) for row in rows]

    async def count_emails(
        self,
        campaign_id: Optional[str] = None,
        recipient_email: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> int:
        """Count total stored emails matching filter criteria."""
        clauses = []
        params: List[Any] = []

        if campaign_id:
            clauses.append("campaign_id = ?")
            params.append(campaign_id)

        if recipient_email:
            clauses.append("recipient_email = ?")
            params.append(recipient_email)

        if status:
            clauses.append("status = ?")
            params.append(status)

        if search:
            clauses.append("(recipient_email LIKE ? OR subject LIKE ? OR id LIKE ?)")
            search_param = f"%{search}%"
            params.extend([search_param, search_param, search_param])

        where_str = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT COUNT(*) as count FROM sent_emails {where_str}"

        async with get_db() as db:
            cursor = await db.execute(query, params)
            row = await cursor.fetchone()
            return row["count"] if row else 0

    async def get_raw_headers(self, storage_id: str) -> Dict[str, str]:
        """
        View raw RFC 5322 headers for a stored email.
        Extracts from database JSON record or parses directly from the archived .eml file.
        """
        record = await self.get_email(storage_id)
        if not record:
            return {}

        if record.raw_headers_json and record.raw_headers_json != "{}":
            try:
                return json.loads(record.raw_headers_json)
            except Exception:
                pass

        # Try parsing from raw .eml file on disk
        if record.eml_file_path and os.path.exists(record.eml_file_path):
            try:
                async with aiofiles.open(record.eml_file_path, "rb") as f:
                    content = await f.read()
                msg = email.message_from_bytes(content)
                return {k: str(v) for k, v in msg.items()}
            except Exception as e:
                logger.warning("Failed to parse headers from .eml file: %s", e)

        # Fallback generated headers dict
        sender_domain = record.sender_email.split("@")[-1] if "@" in record.sender_email else "nexusmail.local"
        unsub_token = f"{record.recipient_email}:{record.id}"
        unsub_url = f"{settings.TRACKING_BASE_URL.rstrip('/')}/unsubscribe/{unsub_token}"
        unsub_mailto = f"<mailto:unsubscribe+{record.id}@{sender_domain}?subject=unsubscribe>"

        return {
            "Message-ID": record.message_id or f"<{record.id}@{sender_domain}>",
            "From": f"{record.sender_name} <{record.sender_email}>" if record.sender_name else record.sender_email,
            "To": f"{record.recipient_name} <{record.recipient_email}>" if record.recipient_name else record.recipient_email,
            "Subject": record.subject,
            "Date": record.sent_at or record.created_at,
            "List-Unsubscribe": f"{unsub_mailto}, <{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            "X-Mailer": "Enterprise Mass Email Engine v1.0",
            "X-Storage-ID": record.id,
            "X-Campaign-ID": record.campaign_id or "",
        }


    async def get_live_rendered_html(self, storage_id: str) -> Optional[str]:
        """
        Get exact rendered HTML as received by recipient (with tracking pixel and link rewrites).
        """
        record = await self.get_email(storage_id)
        if not record:
            return None

        if record.rendered_html:
            return record.rendered_html

        if record.body_html:
            return record.body_html

        # Try extracting HTML part from .eml file
        if record.eml_file_path and os.path.exists(record.eml_file_path):
            try:
                async with aiofiles.open(record.eml_file_path, "rb") as f:
                    content = await f.read()
                msg = email.message_from_bytes(content)
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        payload = part.get_payload(decode=True)
                        if payload:
                            return payload.decode("utf-8", errors="replace")
            except Exception as e:
                logger.warning("Failed to extract HTML from .eml: %s", e)

        return f"<pre>{record.body_text or ''}</pre>"

    async def get_audit_timeline(self, storage_id: str) -> List[TimelineEvent]:
        """
        Retrieve complete chronological audit timeline (created -> queued -> sent -> opened -> clicked -> unsubscribed).
        """
        async with get_db() as db:
            cursor = await db.execute(
                """
                SELECT id, email_storage_id, event_type, event_timestamp, event_data_json
                FROM audit_events
                WHERE email_storage_id = ?
                ORDER BY event_timestamp ASC, id ASC
                """,
                (storage_id,),
            )
            rows = await cursor.fetchall()
            events = []
            for row in rows:
                try:
                    data = json.loads(row["event_data_json"] or "{}")
                except Exception:
                    data = {}
                events.append(TimelineEvent(
                    id=row["id"],
                    email_storage_id=row["email_storage_id"],
                    event_type=row["event_type"],
                    event_timestamp=row["event_timestamp"],
                    event_data=data,
                ))
            return events

    async def export_eml(self, storage_id: str) -> Optional[bytes]:
        """
        Export raw .eml RFC 5322 MIME file for inspection or download.
        Reads file from disk or reconstructs on the fly.
        """
        record = await self.get_email(storage_id)
        if not record:
            return None

        if record.eml_file_path and os.path.exists(record.eml_file_path):
            try:
                async with aiofiles.open(record.eml_file_path, "rb") as f:
                    return await f.read()
            except Exception as e:
                logger.warning("Error reading .eml from disk for %s: %s", storage_id, e)

        # Reconstruct on demand
        msg = self.build_eml_message(record)
        return msg.as_bytes()

    async def get_storage_stats(self) -> Dict[str, Any]:
        """Get aggregate email vault statistics and disk usage metrics."""
        async with get_db() as db:
            cursor = await db.execute("SELECT COUNT(*) as total_emails, SUM(eml_size_bytes) as total_disk_bytes, SUM(open_count) as total_opens, SUM(click_count) as total_clicks FROM sent_emails")
            totals_row = await cursor.fetchone()

            cursor = await db.execute("SELECT status, COUNT(*) as count FROM sent_emails GROUP BY status")
            status_rows = await cursor.fetchall()

            cursor = await db.execute("SELECT COUNT(*) as unique_opens FROM sent_emails WHERE open_count > 0")
            unique_opens_row = await cursor.fetchone()

            cursor = await db.execute("SELECT COUNT(*) as unique_clicks FROM sent_emails WHERE click_count > 0")
            unique_clicks_row = await cursor.fetchone()

            status_breakdown = {row["status"]: row["count"] for row in status_rows}
            total_emails = totals_row["total_emails"] if totals_row else 0
            unique_opens = unique_opens_row["unique_opens"] if unique_opens_row else 0
            unique_clicks = unique_clicks_row["unique_clicks"] if unique_clicks_row else 0

            open_rate = (unique_opens / total_emails * 100.0) if total_emails > 0 else 0.0
            click_rate = (unique_clicks / total_emails * 100.0) if total_emails > 0 else 0.0

            return {
                "total_emails": total_emails,
                "total_disk_bytes": totals_row["total_disk_bytes"] or 0 if totals_row else 0,
                "total_opens": totals_row["total_opens"] or 0 if totals_row else 0,
                "total_clicks": totals_row["total_clicks"] or 0 if totals_row else 0,
                "unique_opens": unique_opens,
                "unique_clicks": unique_clicks,
                "open_rate_percent": round(open_rate, 2),
                "click_rate_percent": round(click_rate, 2),
                "status_breakdown": status_breakdown,
                "archive_directory": str(self.archive_base_dir),
            }

    def _row_to_record(self, row: Any) -> EmailRecord:
        """Convert a database row into an EmailRecord instance."""
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            metadata = {}

        return EmailRecord(
            id=row["id"],
            campaign_id=row["campaign_id"],
            subscriber_id=row["subscriber_id"],
            recipient_email=row["recipient_email"],
            recipient_name=row["recipient_name"] or "",
            sender_email=row["sender_email"],
            sender_name=row["sender_name"] or "",
            subject=row["subject"],
            body_text=row["body_text"] or "",
            body_html=row["body_html"] or "",
            rendered_html=row["rendered_html"] or row["body_html"] or "",
            raw_headers_json=row["raw_headers_json"] or "{}",
            eml_file_path=row["eml_file_path"],
            eml_size_bytes=row["eml_size_bytes"] or 0,
            message_id=row["message_id"],
            status=row["status"],
            smtp_host=row["smtp_host"],
            smtp_port=row["smtp_port"],
            is_sandbox=bool(row["is_sandbox"]),
            error_message=row["error_message"],
            delivery_latency_ms=row["delivery_latency_ms"],
            open_count=row["open_count"] or 0,
            opened_at=row["opened_at"],
            click_count=row["click_count"] or 0,
            clicked_at=row["clicked_at"],
            created_at=row["created_at"],
            sent_at=row["sent_at"],
            metadata=metadata,
        )


# Global storage vault singleton
storage_vault = EmailStorageVault()
