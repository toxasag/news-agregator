"""Redis connection pool shared by the API, workers and the LSH dedup index."""

from __future__ import annotations

from typing import Final

from redis.asyncio import BlockingConnectionPool, Redis

from app.config import settings

#: A blocking pool applies backpressure instead of raising "Too many
#: connections" the moment the API and the ARQ workers burst past the limit.
_pool: Final[BlockingConnectionPool] = BlockingConnectionPool.from_url(
    str(settings.redis_url),
    decode_responses=True,
    max_connections=settings.redis_max_connections,
    timeout=settings.redis_pool_timeout,
    health_check_interval=30,
    socket_connect_timeout=5,
    socket_timeout=5,
    retry_on_timeout=True,
)


def get_redis() -> Redis:
    """Return a client bound to the shared connection pool."""
    return Redis(connection_pool=_pool)


async def check_redis_connection() -> bool:
    """Ping Redis. Returns ``True`` when the server answers."""
    client = get_redis()
    try:
        return bool(await client.ping())
    finally:
        await client.aclose()


async def close_redis_pool() -> None:
    """Release every pooled Redis connection on shutdown."""
    await _pool.aclose()


__all__ = ["check_redis_connection", "close_redis_pool", "get_redis"]
