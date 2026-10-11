"""
Account-wide sending settings, editable from the Settings page.

Stored as key/value rows in app_settings so new switches need no migration.
Read on every send (cheap: one small table, cached for a few seconds).
"""

import json
import time
from typing import Any, Dict

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
_cache: Dict[str, Any] = {"at": 0.0, "values": None}


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
    now = time.monotonic()
    if _cache["values"] is not None and now - _cache["at"] < _CACHE_SECONDS:
        return dict(_cache["values"])
    values = dict(DEFAULTS)
    async with get_db() as db:
        async with db.execute("SELECT key, value FROM app_settings") as cur:
            for row in await cur.fetchall():
                if row["key"] in DEFAULTS:
                    try:
                        values[row["key"]] = _coerce(row["key"], json.loads(row["value"]))
                    except Exception:
                        pass
    _cache.update(at=now, values=values)
    return dict(values)


async def update_settings(changes: Dict[str, Any]) -> Dict[str, Any]:
    unknown = [k for k in changes if k not in DEFAULTS]
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(unknown)}")
    now = utc_now_iso()
    async with get_db() as db:
        for key, value in changes.items():
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, json.dumps(_coerce(key, value)), now),
            )
        await db.commit()
    _cache["values"] = None
    return await get_settings()
