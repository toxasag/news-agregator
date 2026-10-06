"""Single-node distributed lock for the background workers.

The scheduler and the dashboard can both ask for the same work at the same
moment, so every worker run is fenced by a Redis key. The primitive here is the
classic ``SET key <token> NX PX <ttl>`` lock with a **fencing token**, which is
what the Redlock write-up describes for a single Redis instance.

**Honest naming.** This deployment runs exactly one Redis (see
``docker-compose.yml``), so this is a *single-node* lock, not the multi-master
Redlock algorithm: it inherits that algorithm's single-instance safety properties
and nothing more. If Redis is lost, the lock is lost with it, and two workers
could then overlap. That residual risk is acceptable because the writes the lock
protects are themselves idempotent — ``raw_news`` is written with
``INSERT ... ON CONFLICT DO NOTHING`` on ``(source_id, external_post_id)`` — so
the lock avoids duplicated *work*, it is not what guarantees correctness.

**Why the token exists.** A lock storing a constant value cannot be released
safely. If the work outlives the TTL, the key expires, another worker acquires
it, and the first worker's ``DEL`` then deletes *that* worker's lock. A unique
:func:`secrets.token_hex` value per acquisition turns release into "delete only
if the value is still mine".

**Why Lua.** Checking ownership with ``GET`` and then deleting with ``DEL`` is
two round trips with a gap between them: inside that gap the key can expire and
be re-acquired, and the ``DEL`` would hit the new owner's lock. Redis runs an
``EVAL`` script atomically, so the compare-and-delete cannot interleave with
anybody else. :meth:`RedisLock.extend` needs the same guarantee, since it must
only refresh a TTL the caller still owns.

**Fallback for engines without Lua.** Some Redis-compatible test doubles
(``fakeredis`` without the optional ``lupa`` package) reject ``EVAL`` outright.
Rather than silently degrading to the unsafe ``GET`` + ``DEL`` pair, this module
falls back to an optimistic ``WATCH``/``MULTI``/``EXEC`` transaction, which is
equally safe for this purpose: any competing ``SET`` on the watched key aborts
the transaction, so the delete can never land on another worker's lock. Lua stays
the primary path because it costs one round trip instead of three.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, Final, Self, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError, WatchError

logger: Final[logging.Logger] = logging.getLogger(__name__)

#: Bytes of entropy per fencing token. 16 bytes (32 hex characters) makes a
#: collision between two live acquisitions impossible in practice, which is the
#: property the compare-and-delete relies on.
TOKEN_BYTES: Final[int] = 16

#: Atomic compare-and-delete. Returns 1 when the caller still owned the key.
RELEASE_SCRIPT: Final[str] = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

#: Atomic compare-and-refresh. Returns 1 when the TTL was extended, 0 when the
#: caller no longer owns the key (expired, or taken over after expiry).
EXTEND_SCRIPT: Final[str] = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

#: How often :meth:`RedisLock.acquire` retries while blocking, in seconds.
BLOCKING_POLL_INTERVAL: Final[float] = 0.1

#: Set to ``False`` the first time a server rejects ``EVAL`` so the fallback path
#: is not preceded by a doomed round trip on every subsequent call.
_SCRIPTING_SUPPORTED: bool = True


class LockError(RuntimeError):
    """Base class for every failure raised by this module."""


class LockNotAcquiredError(LockError):
    """The lock is held by another worker.

    Raised only when a caller opts in (``raise_on_conflict=True``, or the
    :class:`RedisLock` context manager); the default is to report contention as a
    ``False`` return value.
    """

    def __init__(self, key: str) -> None:
        super().__init__(f"lock {key!r} is already held by another worker")
        self.key = key


class RedisLock:
    """A single-node mutual-exclusion lock backed by one Redis key.

    Acquisition is ``SET key <token> NX PX <ttl>``: atomic, self-expiring and
    tagged with a token only this instance knows. The TTL is a crash guard — a
    worker killed mid-run releases by expiry instead of wedging the key
    forever — so it must comfortably exceed a normal run.

    Instances are reusable: :meth:`acquire` mints a fresh token every time, so a
    released lock can be taken again.
    """

    __slots__ = ("_key", "_redis", "_token", "_ttl_ms")

    def __init__(self, redis: Redis, key: str, *, ttl: float) -> None:
        """Create a lock handle. ``ttl`` is in **seconds** and must be positive."""
        if ttl <= 0:
            raise ValueError(f"lock ttl must be positive, got {ttl!r}")
        self._redis = redis
        self._key = key
        self._ttl_ms = max(int(ttl * 1000), 1)
        self._token: str | None = None

    @property
    def key(self) -> str:
        """The Redis key this lock occupies."""
        return self._key

    @property
    def ttl_ms(self) -> int:
        """Lock lifetime in milliseconds, as sent to ``PX``."""
        return self._ttl_ms

    @property
    def token(self) -> str | None:
        """The current fencing token, or ``None`` when the lock is not held.

        Never logged: a leaked token would let another process release a lock it
        does not own.
        """
        return self._token

    @property
    def is_held(self) -> bool:
        """Whether *this instance* believes it holds the lock.

        Optimistic by nature — the key may have expired server-side without this
        process noticing. :meth:`release` reports the authoritative answer.
        """
        return self._token is not None

    # ASYNC109: `timeout` here is an acquisition deadline, not a cancellation
    # budget for the body of a call. It bounds how long this method keeps polling
    # and then reports failure as `False`; `asyncio.timeout` would raise instead,
    # which is the opposite of the "skip, do not fail" contract callers rely on.
    async def acquire(
        self,
        *,
        blocking: bool = False,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> bool:
        """Try to take the lock. Returns ``True`` on success.

        With ``blocking=False`` (the default) this is a single ``SET NX`` that
        returns immediately, which is what the ingestion worker wants: a
        scheduled run whose predecessor is still going should be skipped, not
        queued behind it.

        With ``blocking=True`` the attempt is retried every
        :data:`BLOCKING_POLL_INTERVAL` seconds until ``timeout`` elapses, or
        indefinitely when ``timeout`` is ``None``.
        """
        deadline: float | None = None
        if blocking and timeout is not None:
            deadline = asyncio.get_running_loop().time() + timeout

        while True:
            token = secrets.token_hex(TOKEN_BYTES)
            stored = await self._redis.set(self._key, token, nx=True, px=self._ttl_ms)
            if stored:
                self._token = token
                return True
            if not blocking:
                return False
            if deadline is None:
                await asyncio.sleep(BLOCKING_POLL_INTERVAL)
                continue
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(BLOCKING_POLL_INTERVAL, remaining))

    async def _eval(self, script: str, *args: str) -> int | None:
        """Run one Lua script against this key, or ``None`` without a Lua engine."""
        global _SCRIPTING_SUPPORTED  # noqa: PLW0603 - per-process capability flag
        if not _SCRIPTING_SUPPORTED:
            return None
        try:
            # `Redis.eval` is declared as `Awaitable[str] | str` to serve both the
            # sync and async clients; on this async client it is always awaitable.
            result = await cast(
                "Awaitable[Any]", self._redis.eval(script, 1, self._key, *args)
            )
        except ResponseError:
            _SCRIPTING_SUPPORTED = False
            logger.info(
                "Redis rejected EVAL; lock operations fall back to a WATCH/MULTI "
                "compare-and-set, which is equally safe but costs extra round trips"
            )
            return None
        return int(result or 0)

    async def _compare_and_delete(self, token: str) -> bool:
        """``WATCH``-guarded fallback for :data:`RELEASE_SCRIPT`.

        A competing ``SET`` on the watched key aborts ``EXEC``, so the delete can
        never remove a lock this worker no longer owns.
        """
        async with self._redis.pipeline() as pipe:
            try:
                await pipe.watch(self._key)
                if await pipe.get(self._key) != token:
                    await pipe.unwatch()  # type: ignore[no-untyped-call]
                    return False
                pipe.multi()  # type: ignore[no-untyped-call]
                pipe.delete(self._key)
                results = await pipe.execute()
            except WatchError:
                return False
        return bool(results and results[0])

    async def _compare_and_expire(self, token: str, ttl_ms: int) -> bool:
        """``WATCH``-guarded fallback for :data:`EXTEND_SCRIPT`."""
        async with self._redis.pipeline() as pipe:
            try:
                await pipe.watch(self._key)
                if await pipe.get(self._key) != token:
                    await pipe.unwatch()  # type: ignore[no-untyped-call]
                    return False
                pipe.multi()  # type: ignore[no-untyped-call]
                pipe.pexpire(self._key, ttl_ms)
                results = await pipe.execute()
            except WatchError:
                return False
        return bool(results and results[0])

    async def release(self) -> bool:
        """Release the lock, reporting whether it was still owned.

        Never raises for the ordinary "lost it" cases: a lock whose TTL expired
        mid-run is simply reported as ``False``, so the caller can log that the
        configured TTL is too short for its workload. The delete is conditional on
        the token, so a key that expired and was re-acquired by another worker is
        left untouched.

        Idempotent: releasing twice returns ``False`` the second time.
        """
        token = self._token
        if token is None:
            return False
        self._token = None
        scripted = await self._eval(RELEASE_SCRIPT, token)
        deleted = (
            bool(scripted)
            if scripted is not None
            else await self._compare_and_delete(token)
        )
        if not deleted:
            logger.warning(
                "Lock %s was no longer owned at release: it most likely expired "
                "because the work outlived its %d ms TTL, so another worker may "
                "have run in parallel",
                self._key,
                self._ttl_ms,
            )
        return deleted

    async def extend(self, additional_ms: int | None = None) -> bool:
        """Refresh the TTL, but only while still owned. Returns whether it worked.

        For runs that can legitimately outlast the configured TTL: extending
        periodically is better than provisioning a TTL long enough for the worst
        case, because that same TTL is how long a crashed worker blocks the key.
        ``additional_ms`` defaults to this lock's original TTL.
        """
        token = self._token
        if token is None:
            return False
        ttl_ms = self._ttl_ms if additional_ms is None else max(int(additional_ms), 1)
        scripted = await self._eval(EXTEND_SCRIPT, token, str(ttl_ms))
        refreshed = (
            bool(scripted)
            if scripted is not None
            else await self._compare_and_expire(token, ttl_ms)
        )
        if not refreshed:
            self._token = None
            logger.warning(
                "Lock %s could not be extended: it is no longer owned by this worker",
                self._key,
            )
        return refreshed

    async def __aenter__(self) -> Self:
        """Acquire without blocking, raising when the lock is held elsewhere."""
        if not await self.acquire():
            raise LockNotAcquiredError(self._key)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.release()


@asynccontextmanager
# ASYNC109: forwarded verbatim to `RedisLock.acquire`; see the note there for why
# a deadline that resolves to `False` cannot be expressed with `asyncio.timeout`.
async def acquire_lock(
    redis: Redis,
    key: str,
    *,
    ttl: float,
    blocking: bool = False,
    timeout: float | None = None,  # noqa: ASYNC109
    raise_on_conflict: bool = False,
) -> AsyncIterator[bool]:
    """Hold ``key`` for the duration of the block, yielding whether it was taken.

    Yielding a ``bool`` instead of always raising is the ergonomic choice for the
    ingestion worker's "skip if already running" need::

        async with acquire_lock(redis, KEY, ttl=settings.ingest_lock_ttl) as taken:
            if not taken:
                return report(lock_skipped=True)

    An overlapping scheduled run is expected operation, not an error, and the
    worker still has to return a report. Callers for which contention *is* an
    error pass ``raise_on_conflict=True`` and get :class:`LockNotAcquiredError`
    instead, so both styles exist without two separate helpers.

    Release always runs and never raises for a lock that expired mid-block; the
    expiry is logged as a warning by :meth:`RedisLock.release`.
    """
    lock = RedisLock(redis, key, ttl=ttl)
    acquired = await lock.acquire(blocking=blocking, timeout=timeout)
    if not acquired and raise_on_conflict:
        raise LockNotAcquiredError(key)
    try:
        yield acquired
    finally:
        if acquired:
            await lock.release()


__all__ = [
    "BLOCKING_POLL_INTERVAL",
    "EXTEND_SCRIPT",
    "RELEASE_SCRIPT",
    "TOKEN_BYTES",
    "LockError",
    "LockNotAcquiredError",
    "RedisLock",
    "acquire_lock",
]
