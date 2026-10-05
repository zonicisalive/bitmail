"""
Real-Time WebSocket Hub and Event Dispatcher for NexusMail.
Provides persistent bi-directional communication between backend queue workers,
storage vault events, tracking telemetry, and connected browser clients.
"""

import asyncio
import json
import logging
from typing import Any, Dict, Set
from fastapi import WebSocket

logger = logging.getLogger("nexusmail.websocket")


class ConnectionManager:
    """Manages active browser WebSocket connections and broadcasts real-time events."""

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)
        logger.info(f"WebSocket client connected. Total active: {len(self.active_connections)}")

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            self.active_connections.discard(websocket)
        logger.info(f"WebSocket client disconnected. Total active: {len(self.active_connections)}")

    async def broadcast(self, event_type: str, data: Dict[str, Any]):
        """
        Broadcast JSON payload to all currently connected clients.
        Automatically prunes disconnected sockets.
        """
        if not self.active_connections:
            return

        payload = {
            "type": event_type,
            "data": data,
            "timestamp": asyncio.get_event_loop().time()
        }
        message_str = json.dumps(payload)

        async with self._lock:
            dead_connections = []
            for connection in list(self.active_connections):
                try:
                    await connection.send_text(message_str)
                except Exception:
                    dead_connections.append(connection)

            for dead in dead_connections:
                self.active_connections.discard(dead)


# Global singleton instance
ws_manager = ConnectionManager()


async def emit_event(event_type: str, data: Dict[str, Any]):
    """Global helper to emit real-time event to all connected dashboards."""
    try:
        await ws_manager.broadcast(event_type, data)
    except Exception as e:
        logger.warning(f"Failed to broadcast WS event {event_type}: {e}")
