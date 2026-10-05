"""
Outbound Webhooks Engine for Bitmail.
Dispatches real-time HMAC-SHA256 signed JSON webhooks to external endpoints
when email events occur (sent, delivered, opened, clicked, bounced, unsubscribed).
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

from app.db import get_db, utc_now_iso

logger = logging.getLogger("bitmail.webhooks")


class WebhookDispatcher:
    """
    Manages and dispatches HMAC-SHA256 signed webhooks.
    """

    @classmethod
    def compute_signature(cls, secret: str, payload_bytes: bytes) -> str:
        """Computes HMAC-SHA256 hex digest for payload using webhook secret."""
        return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()

    @classmethod
    async def dispatch_event(
        cls,
        event_type: str,
        data: Dict[str, Any],
        background: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Dispatches an event to all active webhooks subscribed to event_type.
        If background is True, runs delivery asynchronously in task.
        """
        if background:
            asyncio.create_task(cls._execute_dispatch(event_type, data))
            return []
        else:
            return await cls._execute_dispatch(event_type, data)

    @classmethod
    async def _execute_dispatch(
        cls,
        event_type: str,
        data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Executes HTTP dispatches to matched webhook endpoints."""
        matched_webhooks: List[Dict[str, Any]] = []

        try:
            async with get_db() as db:
                async with db.execute(
                    "SELECT id, name, url, secret, events_json, is_active FROM webhooks WHERE is_active = 1"
                ) as cur:
                    rows = await cur.fetchall()
                    for r in rows:
                        events_list = []
                        try:
                            events_list = json.loads(r["events_json"]) if r["events_json"] else []
                        except Exception:
                            events_list = []

                        if "*" in events_list or "all" in events_list or event_type in events_list:
                            matched_webhooks.append({
                                "id": r["id"],
                                "name": r["name"],
                                "url": r["url"],
                                "secret": r["secret"]
                            })
        except Exception as e:
            logger.error("Failed to query webhooks for event '%s': %s", event_type, e)
            return []

        if not matched_webhooks:
            return []

        delivery_results = []
        for wh in matched_webhooks:
            res = await cls._deliver_single_webhook(wh, event_type, data)
            delivery_results.append(res)

        return delivery_results

    @classmethod
    async def _deliver_single_webhook(
        cls,
        webhook: Dict[str, Any],
        event_type: str,
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Sends signed HTTP POST to a single webhook endpoint and logs delivery."""
        delivery_id = f"whd_{uuid.uuid4().hex[:12]}"
        now = utc_now_iso()

        payload = {
            "event": event_type,
            "timestamp": now,
            "delivery_id": delivery_id,
            "webhook_id": webhook["id"],
            "data": data
        }
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = cls.compute_signature(webhook["secret"], payload_bytes)

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Bitmail-Webhook/2.0",
            "X-Bitmail-Event": event_type,
            "X-Bitmail-Delivery": delivery_id,
            "X-Bitmail-Signature": f"sha256={signature}",
            "X-Bitmail-Timestamp": now
        }

        status_code: Optional[int] = None
        response_body: Optional[str] = None
        success = False

        start_time = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.post(webhook["url"], content=payload_bytes, headers=headers)
                status_code = resp.status_code
                response_body = resp.text[:1000] if resp.text else ""
                success = (200 <= resp.status_code < 300)
        except Exception as err:
            response_body = f"Connection error: {err}"
            success = False

        latency_ms = round((time.monotonic() - start_time) * 1000, 1)

        # Log delivery in database
        try:
            async with get_db() as db:
                await db.execute("""
                    INSERT INTO webhook_deliveries (
                        id, webhook_id, event_type, payload_json, status_code, response_body, success, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    delivery_id,
                    webhook["id"],
                    event_type,
                    payload_bytes.decode("utf-8", errors="ignore"),
                    status_code,
                    response_body,
                    1 if success else 0,
                    now
                ))
                await db.commit()
        except Exception as db_err:
            logger.error("Failed to log webhook delivery %s: %s", delivery_id, db_err)

        return {
            "delivery_id": delivery_id,
            "webhook_id": webhook["id"],
            "event": event_type,
            "success": success,
            "status_code": status_code,
            "response_body": response_body,
            "latency_ms": latency_ms
        }

    @classmethod
    async def test_webhook(cls, webhook_id: str) -> Dict[str, Any]:
        """
        Sends a test ping event to the specified webhook endpoint.
        """
        webhook: Optional[Dict[str, Any]] = None
        async with get_db() as db:
            async with db.execute(
                "SELECT id, name, url, secret FROM webhooks WHERE id = ?", (webhook_id,)
            ) as cur:
                r = await cur.fetchone()
                if r:
                    webhook = dict(r)

        if not webhook:
            raise ValueError(f"Webhook {webhook_id} not found")

        test_data = {
            "test": True,
            "message": "Ping from Bitmail Webhook Engine",
            "webhook_name": webhook["name"],
            "endpoint": webhook["url"]
        }

        return await cls._deliver_single_webhook(webhook, "ping", test_data)


# Global singleton
webhook_dispatcher = WebhookDispatcher()
