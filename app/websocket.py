"""
Real-Time WebSocket Hub and Event Dispatcher for Bitmail.
Provides persistent bi-directional communication between backend queue workers,
storage vault events, tracking telemetry, and connected browser clients.
Every connection belongs to one signed-in user and only receives that user's events.
"""

import asyncio
import json
import logging
from typing import Any, Dict
from fastapi import WebSocket

from app.auth import current_user_id

logger = logging.getLogger("bitmail.websocket")


class ConnectionManager:
    """Manages active browser WebSocket connections and sends each user their own events."""

    def __init__(self):
        # websocket -> id of the user it is signed in as
        self.active_connections: Dict[WebSocket, str] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        async with self._lock:
            self.active_connections[websocket] = user_id
        logger.info(f"WebSocket client connected. Total active: {len(self.active_connections)}")

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            self.active_connections.pop(websocket, None)
        logger.info(f"WebSocket client disconnected. Total active: {len(self.active_connections)}")

    async def broadcast(self, event_type: str, data: Dict[str, Any], user_id: str):
        """
        Send JSON payload to every connection of user_id.
        Automatically prunes disconnected sockets.
        """
        if not user_id or not self.active_connections:
            return

        payload = {
            "type": event_type,
            "data": data,
            "timestamp": asyncio.get_event_loop().time()
        }
        message_str = json.dumps(payload)

        async with self._lock:
            dead_connections = []
            for connection, owner in list(self.active_connections.items()):
                if owner != user_id:
                    continue
                try:
                    await connection.send_text(message_str)
                except Exception:
                    dead_connections.append(connection)

            for dead in dead_connections:
                self.active_connections.pop(dead, None)


# Global singleton instance
ws_manager = ConnectionManager()


async def emit_event(event_type: str, data: Dict[str, Any]):
    """Send a real-time event to the browsers of the user in context (nobody if there is none)."""
    try:
        user_id = current_user_id()
    except RuntimeError:
        logger.debug("No user in context; live event %s not sent", event_type)
        return
    try:
        await ws_manager.broadcast(event_type, data, user_id)
    except Exception as e:
        logger.warning(f"Failed to broadcast WS event {event_type}: {e}")
