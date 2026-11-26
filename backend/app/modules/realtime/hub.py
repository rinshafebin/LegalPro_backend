"""Per-process registry of open sockets plus one shared Redis subscription.

Each connection has a bounded outbound queue drained by its own writer task, so a slow
client can't hold up delivery to anyone else; if its queue fills up it is disconnected
(it can catch up over REST after reconnecting).
"""

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass, field

from redis.asyncio.client import PubSub
from starlette.websockets import WebSocket

from app.core.logging import get_logger
from app.core.redis import get_redis
from app.modules.realtime.pubsub import SESSION_REVOKED, user_channel

logger = get_logger(__name__)

OUTBOUND_QUEUE_SIZE = 256
CLOSE_SESSION_REVOKED = 4401
CLOSE_TOO_SLOW = 4408


@dataclass(eq=False)
class Connection:
    websocket: WebSocket
    user_id: uuid.UUID
    token_version: int
    outbound: asyncio.Queue[str | int] = field(
        default_factory=lambda: asyncio.Queue(OUTBOUND_QUEUE_SIZE)
    )

    def send(self, data: str) -> None:
        try:
            self.outbound.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("ws_client_too_slow", user_id=str(self.user_id))
            self.close(CLOSE_TOO_SLOW)

    def send_json(self, event: dict[str, object]) -> None:
        self.send(json.dumps(event, default=str, separators=(",", ":")))

    def close(self, code: int) -> None:
        """Ask the writer to close the socket (an int in the queue means 'close')."""
        with contextlib.suppress(asyncio.QueueFull):
            self.outbound.put_nowait(code)
            return
        # Queue full: drop what's pending, the close matters more.
        while not self.outbound.empty():
            self.outbound.get_nowait()
        self.outbound.put_nowait(code)

    async def run_writer(self) -> None:
        while True:
            item = await self.outbound.get()
            if isinstance(item, int):
                with contextlib.suppress(Exception):
                    await self.websocket.close(code=item)
                return
            await self.websocket.send_text(item)


class Hub:
    def __init__(self) -> None:
        self._local: dict[str, set[Connection]] = {}
        self._pubsub: PubSub | None = None
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def local_count(self, user_id: uuid.UUID) -> int:
        return len(self._local.get(user_channel(user_id), ()))

    async def register(self, conn: Connection) -> None:
        channel = user_channel(conn.user_id)
        async with self._lock:
            conns = self._local.setdefault(channel, set())
            first = not conns
            conns.add(conn)
            if first:
                if self._pubsub is None:
                    self._pubsub = get_redis().pubsub(ignore_subscribe_messages=True)
                await self._pubsub.subscribe(channel)
            if self._reader is None or self._reader.done():
                self._reader = asyncio.create_task(self._run(), name="realtime-hub")

    async def unregister(self, conn: Connection) -> None:
        channel = user_channel(conn.user_id)
        async with self._lock:
            conns = self._local.get(channel)
            if conns is None:
                return
            conns.discard(conn)
            if not conns:
                del self._local[channel]
                if self._pubsub is not None:
                    with contextlib.suppress(Exception):
                        await self._pubsub.unsubscribe(channel)

    async def _run(self) -> None:
        while True:
            pubsub = self._pubsub
            if pubsub is None:
                return
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Redis went away; redis-py reconnects and re-subscribes on the next read.
                logger.exception("realtime_hub_read_failed")
                await asyncio.sleep(1)
                continue
            if message is not None and message.get("type") == "message":
                self._deliver(str(message["channel"]), str(message["data"]))

    def _deliver(self, channel: str, data: str) -> None:
        conns = list(self._local.get(channel, ()))
        if not conns:
            return
        if SESSION_REVOKED in data and json.loads(data).get("type") == SESSION_REVOKED:
            for conn in conns:
                conn.close(CLOSE_SESSION_REVOKED)
            return
        for conn in conns:
            conn.send(data)

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.aclose()
            self._pubsub = None
        self._local.clear()


hub = Hub()
