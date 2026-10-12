"""
Per-user sending settings, editable from the Settings page.

Stored as (user_id, key, value) rows in app_settings so new switches need no migration.
Read on every send (cheap: one small table, cached for a few seconds).
"""

import json
import time
from typing import Any, Dict

from app.auth import current_user_id
from app.db import get_db, utc_now_iso

DEFAULTS: Dict[str, Any] = {
    # Format new emails start in on Send Message: "html" or "text".
    "default_format": "html",
    # Default state of the tracking checkboxes on Send Message.
    "track_opens": True,
    "track_clicks": True,
    # Add the unsubscribe / company footer to campaign emails (HTML and text).
    "compliance_footer": True,
    # List-Unsubscribe + List-Unsubscribe-Post headers (one-click unsubscribe).
    "list_unsubscribe_header": True,
    # MX / disposable / risky-address checks before each send. Unsubscribed and
    # bounced addresses are blocked regardless of this switch.
    "pre_send_safety": True,
}

_CACHE_SECONDS = 5.0
# user_id -> (fetched_at, values)
_cache: Dict[str, Any] = {}


def _coerce(key: str, value: Any) -> Any:
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if key == "default_format":
        return value if value in ("html", "text") else default
    return value


async def get_settings() -> Dict[str, Any]:
    """Settings of the user in context; DEFAULTS when there is none."""
    try:
        user_id = current_user_id()
    except RuntimeError:
        return dict(DEFAULTS)
    now = time.monotonic()
    cached = _cache.get(user_id)
    if cached and now - cached[0] < _CACHE_SECONDS:
        return dict(cached[1])
    values = dict(DEFAULTS)
    async with get_db() as db:
        async with db.execute("SELECT key, value FROM app_settings WHERE user_id = ?", (user_id,)) as cur:
            for row in await cur.fetchall():
                if row["key"] in DEFAULTS:
                    try:
                        values[row["key"]] = _coerce(row["key"], json.loads(row["value"]))
                    except Exception:
                        pass
    _cache[user_id] = (now, values)
    return dict(values)


async def update_settings(changes: Dict[str, Any]) -> Dict[str, Any]:
    unknown = [k for k in changes if k not in DEFAULTS]
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(unknown)}")
    user_id = current_user_id()
    now = utc_now_iso()
    async with get_db() as db:
        for key, value in changes.items():
            await db.execute(
                "INSERT INTO app_settings (user_id, key, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (user_id, key, json.dumps(_coerce(key, value)), now),
            )
        await db.commit()
    _cache.pop(user_id, None)
    return await get_settings()
