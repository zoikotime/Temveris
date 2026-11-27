"""
Temveris – WebSocket Connection Manager
=========================================
Server-side per-organization tenant isolation for WebSocket events.

Architecture:
  • Connections are stored in per-org rooms: _rooms[org_id] → [WebSocket, ...]
  • Clients without a valid JWT land in _unauthed and receive only ping frames.
  • broadcast_to_org / broadcast_to_org_sync send to one org only.
  • broadcast / broadcast_sync accept CONTROL FRAMES ONLY (see CONTROL_TYPES).
    A business payload is refused and logged — the isolation rule is enforced
    by the manager, not left to each caller to remember.
  • Dead connections are removed eagerly on send failure — no periodic sweep
    needed, though ping_all() can be called from a background task if desired.
"""
import asyncio
import json
import logging

from fastapi import WebSocket
from typing import Dict, List, Optional

_log = logging.getLogger("zoiko.ws")


class ConnectionManager:
    def __init__(self):
        # org_id → list of websockets authenticated for that org
        self._rooms: Dict[int, List[WebSocket]] = {}
        # connections that connected without a valid token
        self._unauthed: List[WebSocket] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once at startup with the running event loop."""
        self._loop = loop

    async def connect(self, websocket: WebSocket, organization_id: Optional[int] = None) -> None:
        await websocket.accept()
        if organization_id is not None:
            self._rooms.setdefault(organization_id, []).append(websocket)
        else:
            self._unauthed.append(websocket)
        print(
            f"[ws] connected org={organization_id} "
            f"— rooms={len(self._rooms)} authed={self._total_authed()} unauthed={len(self._unauthed)}",
            flush=True,
        )

    def disconnect(self, websocket: WebSocket, organization_id: Optional[int] = None) -> None:
        removed = False
        if organization_id is not None:
            room = self._rooms.get(organization_id)
            if room and websocket in room:
                room.remove(websocket)
                removed = True
                if not room:
                    del self._rooms[organization_id]
        if not removed:
            # exhaustive search — handles the case where org_id is unknown at
            # disconnect time (e.g. unauthenticated path, exception handlers)
            if websocket in self._unauthed:
                self._unauthed.remove(websocket)
                removed = True
            if not removed:
                for org_id, room in list(self._rooms.items()):
                    if websocket in room:
                        room.remove(websocket)
                        if not room:
                            del self._rooms[org_id]
                        break
        print(
            f"[ws] disconnected org={organization_id} "
            f"— rooms={len(self._rooms)} authed={self._total_authed()} unauthed={len(self._unauthed)}",
            flush=True,
        )

    # ── targeted broadcast ─────────────────────────────────────────────────────

    async def broadcast_to_org(self, organization_id: int, message: str) -> None:
        """Send message only to connections belonging to organization_id."""
        conns = list(self._rooms.get(organization_id, []))
        dead: List[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for d in dead:
            self.disconnect(d, organization_id)

    def broadcast_to_org_sync(self, organization_id: int, message: str) -> None:
        """Thread-safe fire-and-forget wrapper for broadcast_to_org."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast_to_org(organization_id, message), loop
            )
        except Exception:
            pass

    # ── global broadcast — CONTROL FRAMES ONLY, enforced ──────────────────────

    # A documented rule is not a safeguard. Four workforce producers used the
    # global broadcast anyway (Phase 3 P0), pushing employee names, departments,
    # break behaviour and time-adjustment reasons to every tenant's sockets —
    # and to the unauthenticated pool. The allow-list below makes that
    # structurally impossible rather than merely discouraged: anything whose
    # `type` is not a control frame is REFUSED and logged, so a future producer
    # that reaches for the wrong method fails loudly instead of leaking.
    CONTROL_TYPES = frozenset({"ping", "pong", "server_shutdown", "reload"})

    @classmethod
    def _is_control_frame(cls, message: str) -> bool:
        try:
            payload = json.loads(message)
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        return payload.get("type") in cls.CONTROL_TYPES

    async def broadcast(self, message: str) -> None:
        """Send a CONTROL frame to all connections regardless of org.

        Business data must never travel this way — use broadcast_to_org.
        A non-control payload is dropped, not delivered.
        """
        if not self._is_control_frame(message):
            _log.error(
                "[ws] REFUSED global broadcast of a non-control frame — this is "
                "tenant business data and must use broadcast_to_org(). "
                "payload prefix=%r", message[:120],
            )
            return
        all_conns: List[WebSocket] = list(self._unauthed)
        for room in self._rooms.values():
            all_conns.extend(room)
        dead: List[WebSocket] = []
        for ws in all_conns:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for d in dead:
            self.disconnect(d)  # exhaustive search path

    def broadcast_sync(self, message: str) -> None:
        """Global sync broadcast — control frames only (see broadcast)."""
        if not self._is_control_frame(message):
            _log.error(
                "[ws] REFUSED global broadcast_sync of a non-control frame — "
                "use broadcast_to_org_sync(org_id, ...). payload prefix=%r",
                message[:120],
            )
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(message), loop)
        except Exception:
            pass

    # ── health / maintenance ───────────────────────────────────────────────────

    async def ping_all(self) -> None:
        """Send ping to all connections to detect and remove dead ones."""
        ping = '{"type":"ping"}'
        all_conns: List[tuple] = [(ws, None) for ws in self._unauthed]
        for org_id, room in self._rooms.items():
            for ws in room:
                all_conns.append((ws, org_id))
        dead: List[tuple] = []
        for ws, org_id in all_conns:
            try:
                await ws.send_text(ping)
            except Exception:
                dead.append((ws, org_id))
        for ws, org_id in dead:
            self.disconnect(ws, org_id)

    def _total_authed(self) -> int:
        return sum(len(room) for room in self._rooms.values())


manager = ConnectionManager()
