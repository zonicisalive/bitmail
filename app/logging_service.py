"""
High-Performance In-Memory Logging & Real-Time Telemetry Interceptor for Bitmail.
Captures system, worker, scheduler, relay, and audit logs with thread-safe ring buffering
and real-time WebSocket streaming to connected browser consoles.
"""

import asyncio
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import logging
import threading
import uuid
from typing import Any, Dict, List, Optional


def utc_now_str() -> str:
    """Return ISO-like UTC timestamp string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class LogEntry:
    """Representation of an individual system log record."""
    id: str
    timestamp: str
    level: str
    logger: str
    source: str
    message: str
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SystemLogBuffer:
    """
    Thread-safe, fixed-capacity ring buffer for storing recent log records.
    Default capacity is 2,000 entries with automatic FIFO eviction.
    """

    def __init__(self, max_capacity: int = 2000):
        self.max_capacity = max_capacity
        self._buffer: deque[LogEntry] = deque(maxlen=max_capacity)
        self._lock = threading.Lock()

    def add(self, entry: LogEntry) -> None:
        """Add a log entry to the buffer."""
        with self._lock:
            self._buffer.append(entry)

    def get_entries(
        self,
        level: Optional[str] = None,
        source: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 150,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Query and filter log entries ordered from newest to oldest.
        """
        with self._lock:
            # Snapshot current items in reverse chronological order
            entries = list(reversed(self._buffer))

        filtered = []
        clean_level = level.strip().upper() if level and level.strip().lower() != "all" else None
        clean_source = source.strip().lower() if source and source.strip().lower() != "all" else None
        clean_search = search.strip().lower() if search and search.strip() else None

        for item in entries:
            if clean_level and item.level != clean_level:
                continue
            if clean_source and item.source != clean_source:
                continue
            if clean_search:
                match = (
                    clean_search in item.message.lower()
                    or clean_search in item.logger.lower()
                    or clean_search in item.source.lower()
                    or (item.details and clean_search in str(item.details).lower())
                )
                if not match:
                    continue
            filtered.append(item.to_dict())

        return filtered[offset : offset + limit]

    def get_stats(self) -> Dict[str, int]:
        """Calculate counts for dashboard summary pills."""
        with self._lock:
            items = list(self._buffer)

        total = len(items)
        errors = sum(1 for x in items if x.level in ("ERROR", "CRITICAL"))
        warnings = sum(1 for x in items if x.level == "WARNING")
        info = sum(1 for x in items if x.level == "INFO")
        debug = sum(1 for x in items if x.level == "DEBUG")

        return {
            "total": total,
            "errors": errors,
            "warnings": warnings,
            "info": info,
            "debug": debug
        }

    def clear(self) -> int:
        """Clear all stored entries and return number of cleared records."""
        with self._lock:
            count = len(self._buffer)
            self._buffer.clear()
            return count

    def export_text(self) -> str:
        """Export stored logs in standard log file format."""
        with self._lock:
            items = list(self._buffer)

        lines = [
            f"[{item.timestamp} UTC] [{item.level:<7}] [{item.source.upper():<9}] ({item.logger}) {item.message}"
            for item in items
        ]
        return "\n".join(lines)


# Global singleton log buffer
system_log_buffer = SystemLogBuffer(max_capacity=2000)


def resolve_log_source(logger_name: str) -> str:
    """Categorize logger name into a friendly UI source tag."""
    name = (logger_name or "").lower()
    if "queue" in name:
        return "queue"
    elif "scheduler" in name:
        return "scheduler"
    elif "auth" in name:
        return "auth"
    elif "smtp" in name or "relay" in name:
        return "smtp"
    elif "storage" in name or "vault" in name:
        return "storage"
    elif "tracking" in name:
        return "tracking"
    elif "server" in name or "uvicorn" in name:
        return "server"
    elif "security" in name:
        return "security"
    return "system"


class SystemLogHandler(logging.Handler):
    """
    Custom logging handler that intercepts Python logs, buffers them in-memory,
    and broadcasts updates to connected WebSockets in real time.
    """

    def __init__(self, buffer: SystemLogBuffer):
        super().__init__()
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            source = resolve_log_source(record.name)
            level = record.levelname.upper()

            details = None
            if record.exc_info and record.exc_text:
                details = {"exception": record.exc_text}
            elif hasattr(record, "details") and isinstance(record.details, dict):
                details = record.details

            entry = LogEntry(
                id=f"log_{uuid.uuid4().hex[:10]}",
                timestamp=datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                level=level,
                logger=record.name,
                source=source,
                message=msg,
                details=details
            )

            self.buffer.add(entry)

            # Attempt live WebSocket dispatch if an event loop is running
            try:
                loop = asyncio.get_running_loop()
                if loop and loop.is_running():
                    from app.websocket import ws_manager
                    loop.create_task(ws_manager.broadcast({
                        "type": "system_log",
                        "data": entry.to_dict()
                    }))
            except (RuntimeError, Exception):
                pass
        except Exception:
            self.handleError(record)


_interceptor_installed = False


def setup_logging_interceptor() -> None:
    """
    Attach the SystemLogHandler to the root logger once during application startup.
    """
    global _interceptor_installed
    if _interceptor_installed:
        return

    handler = SystemLogHandler(system_log_buffer)
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)
    handler.setLevel(logging.INFO)

    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    # Ensure key app loggers propagate to root
    for name in ("nexusmail", "bitmail", "mass_email"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)

    _interceptor_installed = True
    logging.getLogger("nexusmail.system").info("System logging interceptor initialized successfully.")
