"""
REST API Routes for System Logs & Diagnostic Telemetry.
Provides log querying with multi-criteria filtering, buffer management, export, and testing.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.logging_service import system_log_buffer

router = APIRouter(prefix="/api/logs", tags=["System & Dispatch Logs"])
logger = logging.getLogger("bitmail.logs")


class TestLogPayload(BaseModel):
    level: str = Field(default="info", description="Log level (info, warning, error)")
    message: str = Field(default="Test log event triggered from Bitmail diagnostic console.", description="Log message")
    source: str = Field(default="system", description="Log source category")


@router.get("")
async def get_system_logs(
    level: Optional[str] = Query(default="all", description="Filter by severity level (all, info, warning, error, debug)"),
    source: Optional[str] = Query(default="all", description="Filter by source component (all, queue, scheduler, auth, smtp, storage, tracking, system)"),
    search: Optional[str] = Query(default=None, description="Full-text search query across message and logger"),
    limit: int = Query(default=150, ge=1, le=1000, description="Max entries to return"),
    offset: int = Query(default=0, ge=0, description="Pagination offset")
):
    """
    Query system and dispatch logs with level, component, and full-text filtering.
    """
    entries = system_log_buffer.get_entries(
        level=level,
        source=source,
        search=search,
        limit=limit,
        offset=offset
    )
    stats = system_log_buffer.get_stats()
    return {
        "logs": entries,
        "total": len(entries),
        "stats": stats
    }


@router.delete("")
async def clear_system_logs():
    """
    Clear the in-memory runtime log buffer.
    """
    cleared = system_log_buffer.clear()
    logger.info(f"System log buffer cleared by administrator ({cleared} entries removed).")
    return {
        "success": True,
        "cleared_count": cleared,
        "message": f"Cleared {cleared} log entries from memory buffer."
    }


@router.get("/export")
async def export_system_logs(
    format: str = Query(default="text", description="Export format: 'text' (.log) or 'json'")
):
    """
    Download system logs as a formatted plain-text log file or JSON dataset.
    """
    if format.lower() == "json":
        entries = system_log_buffer.get_entries(limit=2000)
        import json
        content = json.dumps(entries, indent=2)
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="bitmail-system-logs.json"'}
        )

    log_text = system_log_buffer.export_text()
    if not log_text:
        log_text = "# Bitmail System Log Export\n# No log records currently in buffer.\n"
    return Response(
        content=log_text,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="bitmail-system.log"'}
    )


@router.post("/test")
async def emit_test_log(payload: TestLogPayload):
    """
    Emit a diagnostic log event to verify live capture and WebSocket streaming.
    """
    lvl = payload.level.strip().lower()
    target_logger = logging.getLogger(f"bitmail.{payload.source.strip().lower()}")

    if lvl == "error":
        target_logger.error(payload.message)
    elif lvl in ("warning", "warn"):
        target_logger.warning(payload.message)
    elif lvl == "debug":
        target_logger.debug(payload.message)
    else:
        target_logger.info(payload.message)

    return {
        "success": True,
        "level": lvl.upper(),
        "message": payload.message,
        "source": payload.source
    }
