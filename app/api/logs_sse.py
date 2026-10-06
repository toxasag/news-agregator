"""Live pipeline log stream: Redis pub/sub in, Server-Sent Events out.

The workers run in their own processes, so the dashboard cannot read their logs
directly. A :class:`logging.Handler` publishes structured records onto the Redis
channel ``pipeline:events``, and :func:`stream_events` relays them to the browser
as SSE.

**SSE rather than WebSockets.** The stream is one-directional — the browser never
sends anything back — and SSE gives automatic reconnection, works through any
reverse proxy that can do chunked responses, and needs no protocol upgrade. A
WebSocket would add a second transport to operate for no capability we use.

**Publishing must never break the worker.** The handler swallows every Redis
failure and drops the record: a logging call that raises inside an exception
handler would replace the original error with a Redis error, which is how a
transient outage turns into a debugging dead end. Log delivery is best-effort by
design.

**The publish is fire-and-forget.** ``logging`` is synchronous and the Redis client
is async, so the handler schedules the publish on the running loop rather than
blocking a worker coroutine on it. When no loop is running — a synchronous script,
or interpreter shutdown — the record is dropped rather than starting a loop to
deliver a log line.

**The stream has a heartbeat.** An idle SSE connection is indistinguishable from a
dead one to every proxy in the path, and most close it after 30-60 seconds. A
periodic comment frame keeps the connection open and lets the client detect a
genuinely broken pipe.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.api.auth import AdminUser, get_current_admin_user
from app.redis_client import get_redis

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "EVENTS_CHANNEL",
    "HEARTBEAT_SECONDS",
    "LOGS_HISTORY_KEY",
    "MAX_HISTORY_LOGS",
    "MAX_MESSAGE_CHARS",
    "RedisLogHandler",
    "format_sse",
    "install_log_handler",
    "publish_event",
    "router",
    "stream_events",
]

#: The pub/sub channel workers publish to and the dashboard subscribes to.
EVENTS_CHANNEL: Final[str] = "pipeline:events"

#: The Redis list key used to buffer historical logs for page reload.
LOGS_HISTORY_KEY: Final[str] = "pipeline:logs:history"

#: Maximum number of historical logs kept in the Redis ring buffer.
MAX_HISTORY_LOGS: Final[int] = 500

#: Seconds between heartbeat comments. Below the 30-60s idle timeout that proxies
#: and load balancers typically apply to a streaming response.
HEARTBEAT_SECONDS: Final[float] = 15.0

#: Pause between empty pub/sub polls. `get_message` returns `None` immediately when
#: the queue is empty, so without this the read loop busy-spins and starves the
#: rest of the event loop — including whatever is trying to publish to it.
_IDLE_POLL_SECONDS: Final[float] = 0.05

#: Log messages are truncated to this length. A single stack trace can run to
#: kilobytes, and one runaway record must not be able to fill the subscriber's
#: buffer or the browser's DOM.
MAX_MESSAGE_CHARS: Final[int] = 2000

#: Records below this level are not published. The stream is an operator's live
#: view, not an archive: DEBUG from Playwright and httpx would bury the pipeline
#: events it exists to show.
DEFAULT_MIN_LEVEL: Final[int] = logging.INFO

#: Loggers whose records are never published, however severe. These are chatty
#: third-party libraries whose output is noise in an operator view and which can
#: emit records *while* the handler is publishing, risking recursion.
_MUTED_LOGGERS: Final[frozenset[str]] = frozenset(
    {
        "httpx",
        "httpcore",
        "asyncio",
        "redis",
        "urllib3",
        "app.api.logs_sse",
    }
)

router: Final[APIRouter] = APIRouter(prefix="/api/logs", tags=["logs"])


def _serialise(record: logging.LogRecord) -> str:
    """Render a log record as a compact JSON event.

    ``record.getMessage()`` is called inside the try: a record whose format
    arguments do not match its template raises here, and that must not propagate
    into the worker that logged it.
    """
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - a broken format string must not escape
        message = f"<unformattable log record from {record.name}>"

    payload: dict[str, Any] = {
        "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
        "level": record.levelname,
        "logger": record.name,
        "message": message[:MAX_MESSAGE_CHARS],
    }
    if record.exc_info and record.exc_text:
        payload["traceback"] = record.exc_text[:MAX_MESSAGE_CHARS]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


async def publish_event(payload: str, *, redis: Redis | None = None) -> bool:
    """Publish one serialised event and buffer it in Redis history.

    Returns ``False`` on any Redis failure rather than raising: the caller is a
    logging handler, and a raising logger is worse than a missing log line.
    """
    client = redis if redis is not None else get_redis()
    try:
        await client.publish(EVENTS_CHANNEL, payload)
        await client.rpush(LOGS_HISTORY_KEY, payload)  # type: ignore[misc]
        await client.ltrim(LOGS_HISTORY_KEY, -MAX_HISTORY_LOGS, -1)  # type: ignore[misc]
    except RedisError:
        return False
    else:
        return True
    finally:
        if redis is None:
            with contextlib.suppress(RedisError):
                await client.aclose()


class RedisLogHandler(logging.Handler):
    """Publishes log records onto the pipeline event channel.

    Installed in the worker processes so their output reaches the dashboard. The
    handler is deliberately defensive: it never raises, never blocks and drops
    records rather than queueing them without bound.
    """

    def __init__(self, *, level: int = DEFAULT_MIN_LEVEL) -> None:
        super().__init__(level=level)
        #: Tasks kept referenced until they finish. Without this the garbage
        #: collector can collect a pending task mid-flight, and the publish is
        #: silently lost.
        self._pending: set[asyncio.Task[bool]] = set()

    def emit(self, record: logging.LogRecord) -> None:
        """Schedule the record for publication. Never raises, never blocks."""
        if record.name in _MUTED_LOGGERS or any(
            record.name.startswith(f"{muted}.") for muted in _MUTED_LOGGERS
        ):
            return
        try:
            payload = _serialise(record)
        except Exception:  # noqa: BLE001 - see the class docstring
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop: a synchronous context or interpreter shutdown.
            # Starting one to deliver a log line would be absurd.
            return

        task = loop.create_task(publish_event(payload))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    # N802: `handleError` is `logging.Handler`'s own camelCase hook. Renaming it to
    # satisfy the convention would simply stop overriding anything.
    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        """Swallow handler failures.

        The base implementation writes to stderr, which in a container means the
        same stream the application logs to — a Redis outage would then print a
        traceback for every record and drown the real output.
        """
        return


def install_log_handler(*, level: int = DEFAULT_MIN_LEVEL) -> RedisLogHandler:
    """Attach the publishing handler to the root logger.

    Idempotent: a second call replaces the existing handler rather than adding a
    duplicate, so every record would otherwise be published twice — which shows up
    on the dashboard as every line appearing in pairs.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, RedisLogHandler):
            root.removeHandler(existing)
    handler = RedisLogHandler(level=level)
    root.addHandler(handler)
    logger.debug("Redis log handler installed at level %s", logging.getLevelName(level))
    return handler


def format_sse(
    data: str, *, event: str | None = None, retry_ms: int | None = None
) -> str:
    """Frame a payload as a Server-Sent Event.

    Every line of ``data`` is prefixed separately: a bare newline inside a ``data:``
    field terminates the event, so an unescaped multi-line payload — a traceback —
    would be delivered as several malformed events.
    """
    lines: list[str] = []
    if event:
        lines.append(f"event: {event}")
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    for line in data.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


async def stream_events(
    *,
    redis: Redis | None = None,
    heartbeat: float = HEARTBEAT_SECONDS,
    max_events: int | None = None,
) -> AsyncIterator[str]:
    """Yield SSE frames for every event published to the channel.

    ``max_events`` bounds the stream, which exists for the tests: an unbounded
    generator would hang a test run. Production leaves it ``None`` and the stream
    ends when the client disconnects.

    The subscription is torn down in a ``finally`` so a disconnected browser does
    not leak a Redis pub/sub connection — with a pooled client that leak exhausts
    the pool after enough reloads and takes the whole application down with it.
    """
    client = redis if redis is not None else get_redis()
    owns_client = redis is None
    pubsub = client.pubsub()
    delivered = 0

    try:
        await pubsub.subscribe(EVENTS_CHANNEL)
        # `retry` tells the browser how long to wait before reconnecting, and is
        # sent once up front because EventSource remembers it across reconnects.
        yield format_sse(
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "level": "INFO",
                    "logger": "dashboard",
                    "message": "Connected to the live pipeline log.",
                },
                ensure_ascii=False,
            ),
            event="connected",
            retry_ms=3000,
        )

        # Replay recent history from Redis so the dashboard has immediate context
        try:
            history_items = await client.lrange(LOGS_HISTORY_KEY, 0, -1)  # type: ignore[misc]
            for raw_item in history_items:
                item_str = (
                    raw_item.decode("utf-8")
                    if isinstance(raw_item, bytes)
                    else str(raw_item)
                )
                delivered += 1
                yield format_sse(item_str, event="log")
                if max_events is not None and delivered >= max_events:
                    return
        except RedisError:
            pass

        loop = asyncio.get_running_loop()
        last_frame_at = loop.time()

        while max_events is None or delivered < max_events:
            try:
                message = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True),
                    timeout=heartbeat,
                )
            except TimeoutError:
                # A blocking read that genuinely timed out.
                last_frame_at = loop.time()
                yield ": heartbeat\n\n"
                continue
            except RedisError:
                yield format_sse(
                    json.dumps(
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "level": "ERROR",
                            "logger": "dashboard",
                            "message": "Lost the connection to Redis; reconnecting.",
                        },
                        ensure_ascii=False,
                    ),
                    event="error",
                )
                return

            if message is None:
                # `get_message` returns `None` *immediately* when nothing is queued,
                # so two things have to happen here. A short sleep yields control —
                # without it this is a tight loop that starves every other task on
                # the event loop, including the publisher trying to deliver the
                # event this stream is waiting for. And because the read returns
                # rather than timing out, the `TimeoutError` branch above never
                # fires on such a client, so the idle interval is tracked
                # explicitly: otherwise the connection would sit silent forever and
                # be closed by the first proxy in the path.
                await asyncio.sleep(_IDLE_POLL_SECONDS)
                if loop.time() - last_frame_at >= heartbeat:
                    last_frame_at = loop.time()
                    yield ": heartbeat\n\n"
                continue
            data = message.get("data")
            if data is None:
                continue
            payload = data.decode("utf-8") if isinstance(data, bytes) else str(data)
            delivered += 1
            last_frame_at = loop.time()
            yield format_sse(payload, event="log")
    finally:
        with contextlib.suppress(RedisError, RuntimeError):
            await pubsub.unsubscribe(EVENTS_CHANNEL)
        with contextlib.suppress(RedisError, RuntimeError):
            # `PubSub.aclose` is untyped in redis-py's stubs.
            await pubsub.aclose()  # type: ignore[no-untyped-call]
        if owns_client:
            with contextlib.suppress(RedisError):
                await client.aclose()


@router.get("/stream", summary="Live pipeline log stream (SSE)")
async def logs_stream(
    admin: Annotated[AdminUser, Depends(get_current_admin_user)],
    limit: Annotated[int | None, Query(ge=1, le=10_000)] = None,
) -> StreamingResponse:
    """Stream pipeline events to the dashboard terminal.

    Authenticated like every other dashboard route: worker logs name channels,
    source URLs and failure details, which is operational intelligence rather than
    public information.

    The headers matter. ``no-cache`` stops a proxy serving a stale prefix of the
    stream, and ``X-Accel-Buffering: no`` disables nginx's response buffering —
    without it nginx accumulates the stream and the terminal updates in bursts
    minutes apart, which looks exactly like a broken worker.
    """
    logger.debug("Opening a log stream for %s", admin.username)
    return StreamingResponse(
        stream_events(max_events=limit),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/clear", summary="Clear live logs history")
async def clear_logs_history(
    admin: Annotated[AdminUser, Depends(get_current_admin_user)],
) -> dict[str, str]:
    """Clear buffered pipeline logs history from Redis."""
    logger.info("Admin %s cleared live logs history", admin.username)
    client = get_redis()
    try:
        await client.delete(LOGS_HISTORY_KEY)
    except RedisError as exc:
        return {"status": "error", "message": str(exc)}
    finally:
        with contextlib.suppress(RedisError):
            await client.aclose()
    return {"status": "ok", "message": "История логов очищена"}
