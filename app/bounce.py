"""
Smart Bounce & Feedback Loop (FBL) Processor for Bitmail.
Parses RFC 3464 DSN and RFC 5965 ARF complaint reports, classifies bounces into
Hard (5xx), Soft (4xx), and Spam Complaints, self-healing recipient and suppression lists.
"""

import email
import email.policy
import logging
import re
import uuid
from typing import Any, Dict, Optional, Tuple

from app.db import get_db, utc_now_iso
from app.models import BounceType, InboundBounceResponse
from app.webhooks import WebhookDispatcher

logger = logging.getLogger("bitmail.bounce")

# Regex to capture RFC 3463 status codes like 5.1.1, 4.2.2, 5.7.1
STATUS_CODE_REGEX = re.compile(r"\b([45]\.\d{1,3}\.\d{1,3})\b")

# Common hard bounce diagnostic triggers
HARD_BOUNCE_PATTERNS = [
    r"user unknown",
    r"recipient unknown",
    r"no such user",
    r"does not exist",
    r"mailbox not found",
    r"invalid recipient",
    r"address rejected",
    r"undeliverable",
    r"account closed",
    r"domain not found",
    r"unrouteable address",
    r"mailbox disabled",
    r"550\b",
    r"551\b",
    r"552\b",
    r"553\b",
    r"554\b",
]

# Common soft bounce diagnostic triggers
SOFT_BOUNCE_PATTERNS = [
    r"mailbox full",
    r"quota exceeded",
    r"over quota",
    r"try again later",
    r"temporary failure",
    r"greylisted",
    r"rate limit",
    r"connection refused",
    r"too many connections",
    r"450\b",
    r"451\b",
    r"452\b",
]


class BounceClassifier:
    """
    Classifies delivery failure reports and updates suppression registers.
    """

    @classmethod
    def classify(
        cls,
        status_code: Optional[str] = None,
        diagnostic: Optional[str] = None,
        raw_text: Optional[str] = None,
        type_hint: Optional[str] = None
    ) -> Tuple[BounceType, str]:
        """
        Determines whether an event is HARD bounce, SOFT bounce, or COMPLAINT.
        Returns (BounceType, classification_reason).
        """
        if type_hint:
            hint_lower = type_hint.strip().lower()
            if hint_lower in ("complaint", "abuse", "fbl"):
                return BounceType.COMPLAINT, "Spam complaint report (Feedback Loop)"
            elif hint_lower in ("soft", "transient", "deferral"):
                return BounceType.SOFT, "Temporary soft bounce"
            elif hint_lower in ("hard", "permanent"):
                return BounceType.HARD, "Permanent hard bounce"

        combined_text = f"{status_code or ''} {diagnostic or ''} {raw_text or ''}".lower()

        # Check for abuse / complaint indicators
        if "feedback-type: abuse" in combined_text or "spam complaint" in combined_text:
            return BounceType.COMPLAINT, "Spam complaint received via Feedback Loop"

        # Check explicit RFC 3463 status code first
        code_match = STATUS_CODE_REGEX.search(combined_text)
        if code_match:
            code = code_match.group(1)
            if code.startswith("5."):
                return BounceType.HARD, f"RFC 3463 permanent failure code {code}"
            elif code.startswith("4."):
                return BounceType.SOFT, f"RFC 3463 temporary failure code {code}"

        # Heuristic inspection
        for p in HARD_BOUNCE_PATTERNS:
            if re.search(p, combined_text, re.IGNORECASE):
                return BounceType.HARD, f"Diagnostic matched hard bounce pattern '{p}'"

        for p in SOFT_BOUNCE_PATTERNS:
            if re.search(p, combined_text, re.IGNORECASE):
                return BounceType.SOFT, f"Diagnostic matched soft bounce pattern '{p}'"

        # Default fallback
        if status_code and status_code.startswith("5"):
            return BounceType.HARD, f"Status code {status_code}"
        return BounceType.SOFT, "Uncategorized delivery failure (assumed transient soft bounce)"

    @classmethod
    def parse_raw_dsn(cls, raw_content: str) -> Dict[str, Any]:
        """
        Parses a multipart MIME delivery status notification (DSN / RFC 3464).
        Extracts recipient, action, status code, and diagnostic message.
        """
        parsed_email = email.message_from_string(raw_content, policy=email.policy.default)
        recipient = ""
        status_code = None
        diagnostic = ""
        action = "failed"

        for part in parsed_email.walk():
            content_type = part.get_content_type()
            if content_type == "message/delivery-status":
                payload = part.get_payload()
                lines = str(payload).splitlines()
                for line in lines:
                    line_lower = line.lower()
                    if line_lower.startswith("final-recipient:"):
                        match = re.search(r"rfc822;\s*([^\s;]+)", line, re.IGNORECASE)
                        if match:
                            recipient = match.group(1).strip("<>")
                    elif line_lower.startswith("action:"):
                        action = line.split(":", 1)[1].strip()
                    elif line_lower.startswith("status:"):
                        status_code = line.split(":", 1)[1].strip()
                    elif line_lower.startswith("diagnostic-code:"):
                        diagnostic = line.split(":", 1)[1].strip()

            elif content_type == "message/feedback-report":
                payload = part.get_payload()
                lines = str(payload).splitlines()
                for line in lines:
                    if line.lower().startswith("original-rcpt-to:"):
                        recipient = line.split(":", 1)[1].strip()

        # If recipient not found in subparts, check main headers
        if not recipient:
            to_header = parsed_email.get("To", "")
            if to_header:
                recipient = email.utils.parseaddr(to_header)[1]

        return {
            "recipient_email": recipient,
            "action": action,
            "status_code": status_code,
            "diagnostic_code": diagnostic,
            "subject": parsed_email.get("Subject", "")
        }

    @classmethod
    async def process_bounce(
        cls,
        recipient_email: str,
        bounce_type: BounceType,
        reason: str,
        status_code: Optional[str] = None,
        diagnostic_code: Optional[str] = None,
        campaign_id: Optional[str] = None,
        message_id: Optional[str] = None
    ) -> InboundBounceResponse:
        """
        Applies self-healing actions:
        - If Hard Bounce (5xx):
            - Adds recipient to suppressions & suppression_list tables
            - Sets subscriber status = 'bounced'
            - Updates sent_emails status = 'bounced'
            - Increments campaign bounce_count
            - Dispatches outbound webhook 'email.bounced'
        - If Spam Complaint:
            - Adds recipient to suppressions & suppression_list (reason = 'spam_complaint')
            - Sets subscriber status = 'complained'
            - Dispatches outbound webhook 'subscriber.unsubscribed'
        - If Soft Bounce (4xx):
            - Logs transient deferral without blacklisting recipient
        """
        clean_email = recipient_email.strip().lower()
        now = utc_now_iso()
        suppressed = False
        action_taken = ""

        async with get_db() as db:
            # Match recent sent email if not provided
            email_record_id: Optional[str] = None
            if not campaign_id or not message_id:
                async with db.execute("""
                    SELECT id, campaign_id FROM sent_emails
                    WHERE recipient_email = ?
                    ORDER BY created_at DESC LIMIT 1
                """, (clean_email,)) as cur:
                    sent_row = await cur.fetchone()
                    if sent_row:
                        email_record_id = sent_row["id"]
                        if not campaign_id:
                            campaign_id = sent_row["campaign_id"]

            if bounce_type == BounceType.HARD:
                # 1. Add to suppressions
                await db.execute("""
                    INSERT OR IGNORE INTO suppressions (id, email, campaign_id, reason, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (f"sup_{uuid.uuid4().hex[:10]}", clean_email, campaign_id, f"hard_bounce: {reason}", now))

                await db.execute("""
                    INSERT OR IGNORE INTO suppression_list (id, email, campaign_id, reason, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (f"sup_{uuid.uuid4().hex[:10]}", clean_email, campaign_id, f"hard_bounce: {reason}", now))

                # 2. Update subscriber status
                await db.execute("""
                    UPDATE subscribers
                    SET status = 'bounced', updated_at = ?
                    WHERE email = ?
                """, (now, clean_email))

                # 3. Update sent_emails record
                if email_record_id:
                    await db.execute("""
                        UPDATE sent_emails
                        SET status = 'bounced', error_message = ?
                        WHERE id = ?
                    """, (f"Hard bounce: {reason} ({status_code or ''})", email_record_id))

                # 4. Increment campaign bounce count
                if campaign_id:
                    await db.execute("""
                        UPDATE campaigns
                        SET bounce_count = bounce_count + 1, updated_at = ?
                        WHERE id = ?
                    """, (now, campaign_id))

                suppressed = True
                action_taken = "Suppressed email and marked subscriber as bounced."

                # Dispatched webhook event
                await WebhookDispatcher.dispatch_event("email.bounced", {
                    "recipient_email": clean_email,
                    "bounce_type": "hard",
                    "status_code": status_code,
                    "reason": reason,
                    "campaign_id": campaign_id,
                    "suppressed": True
                })

            elif bounce_type == BounceType.COMPLAINT:
                # 1. Add to suppressions
                await db.execute("""
                    INSERT OR IGNORE INTO suppressions (id, email, campaign_id, reason, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (f"sup_{uuid.uuid4().hex[:10]}", clean_email, campaign_id, "spam_complaint", now))

                await db.execute("""
                    INSERT OR IGNORE INTO suppression_list (id, email, campaign_id, reason, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (f"sup_{uuid.uuid4().hex[:10]}", clean_email, campaign_id, "spam_complaint", now))

                # 2. Update subscriber status
                await db.execute("""
                    UPDATE subscribers
                    SET status = 'complained', updated_at = ?
                    WHERE email = ?
                """, (now, clean_email))

                suppressed = True
                action_taken = "Suppressed email due to Feedback Loop spam complaint."

                await WebhookDispatcher.dispatch_event("subscriber.unsubscribed", {
                    "recipient_email": clean_email,
                    "reason": "spam_complaint",
                    "campaign_id": campaign_id
                })

            else:
                # Soft bounce: temporary deferral
                action_taken = "Logged transient soft bounce (recipient not suppressed)."
                if email_record_id:
                    await db.execute("""
                        UPDATE sent_emails
                        SET error_message = ?
                        WHERE id = ?
                    """, (f"Soft bounce: {reason} ({status_code or ''})", email_record_id))

            await db.commit()

        logger.info(
            "Processed %s bounce for %s: %s (Suppressed: %s)",
            bounce_type.value, clean_email, reason, suppressed
        )

        return InboundBounceResponse(
            success=True,
            recipient_email=clean_email,
            bounce_type=bounce_type,
            suppressed=suppressed,
            action_taken=action_taken,
            message=f"Bounce processed successfully as {bounce_type.value}."
        )


# Global singleton
bounce_classifier = BounceClassifier()
