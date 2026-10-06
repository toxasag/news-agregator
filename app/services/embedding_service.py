"""Embedding generation for ``articles.final_embedding`` and cluster vectors.

The schema stores ``vector(1536)`` columns with HNSW cosine indexes, so an
embedding is only useful if it is exactly the configured width — a short vector
is rejected by pgvector at INSERT, far away from the model that produced it.
:class:`~app.services.ai_client.AIClient` already enforces that width, and this
module adds the two things the pipeline needs on top of it:

* **A Redis cache with a 24-hour TTL.** Embeddings are deterministic for a given
  ``(model, text)`` pair, and the pipeline re-embeds the same text more often
  than it looks: a cluster's title is embedded when the cluster opens and again
  whenever an article is generated for it, level 2 of the deduplicator embeds
  candidate bodies that frequently repeat across channels republishing one wire
  story, and a retried worker job re-embeds everything it already did. Each of
  those is a paid API call for a byte-identical answer.

* **Rate-limit-aware retries.** The shared client already retries 429 and 5xx
  with backoff. This module adds a second, outer retry for the case that matters
  to a batch: a quota exhausted mid-run, where the useful response is to back
  off further rather than to fail the whole batch of clusters.

**The cache is keyed by model.** ``AI_MODEL_EMBEDDING`` appears in the key, so
switching models does not silently serve vectors from the old one. Mixing two
models' vectors in one HNSW index is not a degraded search, it is a meaningless
one: cosine distances between different embedding spaces carry no information.

**Redis is treated as optional.** Every cache operation is wrapped: a Redis
outage degrades this service to "always call the API", which is slower and more
expensive but still correct. Failing an article because a cache was unreachable
would be the wrong trade.

Nothing here writes to PostgreSQL. Callers own the transaction, which is what
lets the whole test suite run without a database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Sequence
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import settings
from app.models.vector import EMBEDDING_DIMENSIONS
from app.redis_client import get_redis
from app.services.ai_client import (
    AIClient,
    AIClientError,
    AIRateLimitError,
    get_ai_client,
)
from app.services.local_embeddings import LocalEmbeddingError, embed_locally

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "CACHE_PREFIX",
    "CACHE_TTL_SECONDS",
    "MAX_CACHEABLE_CHARS",
    "RATE_LIMIT_BACKOFF_SECONDS",
    "RATE_LIMIT_MAX_ATTEMPTS",
    "EmbeddingError",
    "EmbeddingService",
    "cache_key",
    "close_embedding_service",
    "embed_texts",
    "get_embedding_service",
    "get_text_embedding",
]

#: 24 hours, per the micro-step requirement. Long enough to cover a full day of
#: ingestion — the window in which the same wire story keeps reappearing — and
#: short enough that a model change or a text correction ages out on its own.
CACHE_TTL_SECONDS: Final[int] = 24 * 60 * 60

CACHE_PREFIX: Final[str] = "emb"

#: Texts longer than this are embedded but not cached. A cached entry costs
#: ~25 KB for the vector regardless of input size, and Redis runs with
#: ``maxmemory-policy noeviction`` (it is the ARQ broker and the LSH index, not a
#: cache), so caching one-off long bodies would spend the instance's memory on
#: entries that will never be read twice. Short, repeated strings — titles,
#: headlines, phrases — are exactly what benefits.
MAX_CACHEABLE_CHARS: Final[int] = 4000

#: Outer retry budget for a quota exhausted mid-batch. Deliberately small: the
#: inner client already retried, so reaching here means the limit is sustained
#: rather than a momentary burst.
RATE_LIMIT_MAX_ATTEMPTS: Final[int] = 3

#: Seconds waited before each outer retry. Longer than the client's own backoff
#: because a 429 that survived that backoff needs a real pause, not another
#: immediate probe.
RATE_LIMIT_BACKOFF_SECONDS: Final[tuple[float, ...]] = (5.0, 15.0)


class EmbeddingError(RuntimeError):
    """An embedding could not be produced.

    Distinct from :class:`~app.services.ai_client.AIClientError` so a caller can
    tell "the vector is unusable" from "the API call failed"; only the first
    means the text itself is the problem.
    """


def cache_key(text: str, *, model: str) -> str:
    """Return the Redis key for one ``(model, text)`` pair.

    The text is hashed rather than embedded in the key: article bodies run to
    kilobytes, and Redis keys are values in their own right — a multi-kilobyte
    key wastes memory and makes ``SCAN`` output unreadable. blake2b at 16 bytes
    makes a collision unreachable in practice for this corpus size.

    The model is part of the key because vectors from two models are not
    interchangeable: cosine distance between different embedding spaces is
    noise, so serving an old model's vector after a switch would silently
    corrupt every similarity query it reaches.
    """
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
    return f"{CACHE_PREFIX}:{model}:{digest}"


def _decode_vector(payload: str) -> list[float] | None:
    """Decode a cached vector, returning ``None`` when it is unusable.

    A cached entry is untrusted input like any other stored data: it may predate
    a dimension change, or have been written by a different version. Anything
    that is not a full-width list of numbers is discarded and re-fetched, which
    is cheaper than debugging a pgvector error later.
    """
    try:
        decoded: Any = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(decoded, list) or len(decoded) != EMBEDDING_DIMENSIONS:
        return None
    values: list[float] = []
    for item in decoded:
        if isinstance(item, bool) or not isinstance(item, int | float):
            return None
        values.append(float(item))
    return values


def _validate_width(vector: Sequence[float], *, text_length: int) -> list[float]:
    """Enforce the schema width on a vector before it is returned or cached.

    :class:`AIClient` already checks this against ``AI_EMBEDDING_DIMENSIONS``;
    the check here is against :data:`EMBEDDING_DIMENSIONS`, the *schema*
    constant. They are validated to agree at startup, so this is the second half
    of that guarantee rather than a duplicate: it also covers a vector that
    arrived from the cache or from an injected client in a test.
    """
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise EmbeddingError(
            f"embedding model {settings.ai_model_embedding!r} returned "
            f"{len(vector)} dimensions for a {text_length}-char text, but the "
            f"schema stores vector({EMBEDDING_DIMENSIONS})"
        )
    return list(vector)


class EmbeddingService:
    """Cached, retrying embedding generator.

    Both collaborators are injectable so tests exercise the cache and the retry
    logic against ``fakeredis`` and a stub client, with no network and no Redis
    server. A client or Redis handle created internally is released by
    :meth:`close`; an injected one is left to its owner, matching
    :class:`~app.services.deduplicator.Deduplicator`.
    """

    __slots__ = ("_ai_client", "_owns_ai", "_owns_redis", "_redis")

    def __init__(
        self,
        *,
        redis: Redis | None = None,
        ai_client: AIClient | None = None,
    ) -> None:
        self._redis = redis
        self._owns_redis = redis is None
        self._ai_client = ai_client
        self._owns_ai = ai_client is None

    def _client(self) -> Redis:
        if self._redis is None:
            self._redis = get_redis()
        return self._redis

    def _ai(self) -> AIClient:
        if self._ai_client is None:
            # The shared client is process-wide, so it is never "owned" here
            # even though this instance created the reference.
            self._ai_client = get_ai_client()
            self._owns_ai = False
        return self._ai_client

    async def close(self) -> None:
        """Release an internally created Redis client."""
        client = self._redis
        if client is not None and self._owns_redis:
            self._redis = None
            await client.aclose()

    # ------------------------------------------------------------------ cache
    def _is_cacheable(self, text: str) -> bool:
        """Whether one text is worth a cache entry. See :data:`MAX_CACHEABLE_CHARS`."""
        return len(text) <= MAX_CACHEABLE_CHARS

    async def _cache_get(self, keys: Sequence[str]) -> list[list[float] | None]:
        """Read several cached vectors in one round trip.

        A Redis failure returns all-misses rather than raising: the cache is an
        optimisation, and an outage must degrade cost, not correctness.
        """
        if not keys:
            return []
        try:
            payloads: list[Any] = list(await self._client().mget(list(keys)))
        except RedisError:
            logger.warning(
                "Embedding cache read failed for %d key(s); falling back to the API",
                len(keys),
                exc_info=True,
            )
            return [None] * len(keys)
        return [
            None if payload is None else _decode_vector(str(payload))
            for payload in payloads
        ]

    async def _cache_set(self, entries: Sequence[tuple[str, list[float]]]) -> None:
        """Write vectors with their TTL in one pipeline.

        The TTL is mandatory, not optional: Redis runs with
        ``maxmemory-policy noeviction``, so an entry written without an expiry
        stays until the instance runs out of memory and starts refusing writes —
        which would take the ARQ broker and the dedup index down with it.
        """
        if not entries:
            return
        try:
            async with self._client().pipeline(transaction=False) as pipe:
                for key, vector in entries:
                    pipe.set(
                        key,
                        json.dumps(vector, separators=(",", ":")),
                        ex=CACHE_TTL_SECONDS,
                    )
                await pipe.execute()
        except RedisError:
            logger.warning(
                "Embedding cache write failed for %d entry(ies); continuing",
                len(entries),
                exc_info=True,
            )

    # ------------------------------------------------------------------- API
    async def _embed_with_backoff(self, texts: Sequence[str]) -> list[list[float]]:
        """Call the embeddings API, backing off further on a sustained 429.

        The shared client has already retried with its own backoff by the time a
        :class:`AIRateLimitError` surfaces here, so this outer loop is for a
        quota that is genuinely exhausted rather than momentarily saturated. It
        matters for a batch: without it, one throttled call would fail every
        cluster in the run.
        """
        # Local inference first, and normally the only path: it costs no tokens,
        # makes no network call, and cannot be rate limited. The gateway remains
        # as a fallback because a worker whose model file is missing should still
        # produce vectors rather than stall the dedup ladder — though on this
        # deployment the gateway has no embedding provider at all.
        if settings.local_embeddings_enabled:
            try:
                return await embed_locally(texts)
            except LocalEmbeddingError as error:
                logger.warning(
                    "Local embeddings unavailable (%s); falling back to the gateway",
                    error,
                )

        last_error: AIClientError = AIRateLimitError(
            "embeddings were never attempted", status_code=429
        )
        for attempt in range(1, RATE_LIMIT_MAX_ATTEMPTS + 1):
            try:
                return await self._ai().embed_batch(texts)
            except AIRateLimitError as exc:
                last_error = exc
                if attempt == RATE_LIMIT_MAX_ATTEMPTS:
                    break
                delay = RATE_LIMIT_BACKOFF_SECONDS[
                    min(attempt - 1, len(RATE_LIMIT_BACKOFF_SECONDS) - 1)
                ]
                logger.warning(
                    "Embeddings rate-limited (attempt %d/%d); waiting %.0fs",
                    attempt,
                    RATE_LIMIT_MAX_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
        raise last_error

    async def embed(self, text: str) -> list[float]:
        """Return the embedding of one text, from cache when available."""
        vectors = await self.embed_batch([text])
        return vectors[0]

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed several texts, preserving input order.

        Cache hits are served without an API call, and only the misses are sent
        upstream — in one request, because a batch of 20 cluster titles is one
        round trip rather than 20. Duplicate texts within the batch are collapsed
        to a single API entry and expanded again on the way out, which matters
        when a run re-embeds the same headline from several channels.

        An empty or whitespace-only text is rejected: an all-zero or degenerate
        vector would sit in the HNSW index and match everything, quietly
        poisoning every similarity query that touches it.
        """
        if not texts:
            return []
        for position, text in enumerate(texts):
            if not text or not text.strip():
                raise EmbeddingError(
                    f"input #{position} is empty; an empty text has no meaningful "
                    "embedding and would match every vector in the index"
                )

        model = settings.ai_model_embedding
        keys = [cache_key(text, model=model) for text in texts]
        cached = await self._cache_get(keys)

        # Unique misses only: a batch that repeats one headline must pay for it
        # once. `dict` preserves insertion order, so the request order is stable.
        pending: dict[str, None] = {}
        for text, hit in zip(texts, cached, strict=True):
            if hit is None:
                pending[text] = None

        fetched: dict[str, list[float]] = {}
        if pending:
            unique = list(pending)
            vectors = await self._embed_with_backoff(unique)
            if len(vectors) != len(unique):
                raise EmbeddingError(
                    f"embeddings response carried {len(vectors)} vector(s) for "
                    f"{len(unique)} input(s)"
                )
            for text, vector in zip(unique, vectors, strict=True):
                fetched[text] = _validate_width(vector, text_length=len(text))
            await self._cache_set(
                [
                    (cache_key(text, model=model), vector)
                    for text, vector in fetched.items()
                    if self._is_cacheable(text)
                ]
            )
            logger.debug(
                "Embedded %d text(s), served %d from cache",
                len(unique),
                len(texts) - len(unique),
            )

        results: list[list[float]] = []
        for text, hit in zip(texts, cached, strict=True):
            if hit is not None:
                results.append(_validate_width(hit, text_length=len(text)))
            else:
                results.append(fetched[text])
        return results


_shared_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    """Return the process-wide service, creating it on first call."""
    global _shared_service  # noqa: PLW0603 - module-level singleton accessor
    if _shared_service is None:
        _shared_service = EmbeddingService()
    return _shared_service


async def close_embedding_service() -> None:
    """Release the shared service's Redis client. Call on shutdown."""
    global _shared_service  # noqa: PLW0603 - module-level singleton accessor
    service = _shared_service
    _shared_service = None
    if service is not None:
        await service.close()


async def get_text_embedding(text: str) -> list[float]:
    """Return the 1536-dimension embedding of ``text``.

    The module's entry point: cached for 24 hours, retried on a sustained rate
    limit, and validated against the schema width before it is returned. Raises
    :class:`EmbeddingError` for an unusable text or vector and
    :class:`~app.services.ai_client.AIClientError` when the API is unreachable
    or unconfigured, so a caller can tell a content problem from an outage.
    """
    return await get_embedding_service().embed(text)


async def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """Embed several texts in one batched, cached request, in input order."""
    return await get_embedding_service().embed_batch(texts)
