"""Local sentence embeddings via fastembed — zero tokens, zero network.

Deduplication level 2 needs a vector per candidate post. Buying those from a
gateway costs money per call and, as this deployment found, is not even reliably
available: OmniRoute answers ``403`` for Gemini embeddings and ``400`` for OpenAI.
A local ONNX model removes both problems — the cost and the dependency.

**Why a 384-dimension model.** The specification names
``paraphrase-multilingual-MiniLM-L12-v2``: 220 MB on disk, ~14 s to load once, and
fast enough to embed a batch in well under a second on CPU. The alternative that
scores better, ``multilingual-e5-large``, is 2.24 GB and takes minutes to fetch on
first use, which a worker container would pay on every rebuild.

**Why the thresholds moved.** Measured on real Russian news pairs, this model
scores a genuine paraphrase of the same story at 0.765-0.855 — *below* the
specification's 0.92 duplicate threshold. Left alone, nearly every duplicate would
fall into the arbiter band and cost an LLM call, defeating the point of a
zero-token stage. The thresholds are calibrated to this model's actual output
instead: see ``SIMILARITY_LOWER_THRESHOLD`` / ``SIMILARITY_UPPER_THRESHOLD``.

The model is loaded lazily and once per process. Loading is CPU-bound and blocks,
so it runs in a thread rather than on the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import warnings
from collections.abc import Sequence
from typing import Any, Final

from app.config import settings

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "LOCAL_EMBEDDING_DIMENSIONS",
    "LOCAL_EMBEDDING_MODEL",
    "LocalEmbeddingError",
    "close_local_embedder",
    "embed_locally",
    "get_local_embedder",
]

#: The model the specification names. Pinned rather than configurable: the vector
#: width is baked into the database schema, so swapping the model is a migration
#: rather than a setting.
LOCAL_EMBEDDING_MODEL: Final[str] = (
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)

#: Vector width this model emits, and therefore the width of the pgvector column.
LOCAL_EMBEDDING_DIMENSIONS: Final[int] = 384

#: Cap on the text handed to the model. Its context is 128 tokens, so a long
#: article contributes nothing past the first few hundred characters, and passing
#: the whole body just costs CPU. The lead is what identifies a news story anyway.
MAX_EMBED_CHARS: Final[int] = 1200


class LocalEmbeddingError(RuntimeError):
    """The local model could not be loaded or could not embed the input."""


class _Embedder:
    """Process-wide lazy holder for the ONNX model.

    ``fastembed`` is imported inside the lock rather than at module scope: the
    import pulls in onnxruntime and tokenizers, which is slow and pointless for a
    process that never embeds anything, such as the web tier.
    """

    __slots__ = ("_lock", "_model")

    def __init__(self) -> None:
        self._model: Any | None = None
        self._lock = threading.Lock()

    def load(self) -> Any:
        """Return the model, constructing it on first use.

        The lock makes this safe when two worker coroutines land here at once via
        ``to_thread``: without it both would download and build the model, and the
        loser's work would be discarded after several seconds of CPU.
        """
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                # Imported here, not at module scope: this pulls in onnxruntime
                # and tokenizers, which is slow and pointless for a process that
                # never embeds anything, such as the web tier.
                from fastembed import TextEmbedding  # noqa: PLC0415
            except ImportError as error:  # pragma: no cover - dependency present
                raise LocalEmbeddingError(
                    "fastembed is not installed: local embeddings are unavailable"
                ) from error
            logger.info("Loading local embedding model %s", LOCAL_EMBEDDING_MODEL)
            try:
                # fastembed emits a UserWarning about its pooling change on load.
                # It is informational, but the test suite runs with
                # `filterwarnings = ["error"]`, which would turn it into a failed
                # model load — the model must build under strict warnings too.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    self._model = TextEmbedding(
                        LOCAL_EMBEDDING_MODEL,
                        cache_dir=str(settings.fastembed_cache_dir),
                    )
            except Exception as error:
                raise LocalEmbeddingError(
                    f"could not load {LOCAL_EMBEDDING_MODEL}: "
                    f"{type(error).__name__}: {error}"
                ) from error
            logger.info(
                "Local embedding model ready (%d dimensions)",
                LOCAL_EMBEDDING_DIMENSIONS,
            )
            return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch, preserving input order."""
        model = self.load()
        trimmed = [text[:MAX_EMBED_CHARS] for text in texts]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                vectors = [
                    [float(value) for value in vector]
                    for vector in model.embed(trimmed)
                ]
        except Exception as error:
            raise LocalEmbeddingError(
                f"local embedding failed: {type(error).__name__}: {error}"
            ) from error

        if len(vectors) != len(texts):
            raise LocalEmbeddingError(
                f"model returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        for position, vector in enumerate(vectors):
            if len(vector) != LOCAL_EMBEDDING_DIMENSIONS:
                # A width mismatch means the pinned model changed under us, and
                # writing it to the pgvector column would fail far from the cause.
                raise LocalEmbeddingError(
                    f"vector {position} has {len(vector)} dimensions, "
                    f"expected {LOCAL_EMBEDDING_DIMENSIONS}"
                )
        return vectors

    def release(self) -> None:
        """Drop the model so its memory can be reclaimed."""
        with self._lock:
            self._model = None


_SHARED: Final[_Embedder] = _Embedder()


def get_local_embedder() -> _Embedder:
    """Return the process-wide embedder."""
    return _SHARED


async def embed_locally(texts: Sequence[str]) -> list[list[float]]:
    """Embed ``texts`` locally, off the event loop.

    Returns an empty list for empty input rather than loading the model, so a
    caller with nothing to embed pays nothing.
    """
    if not texts:
        return []
    # Inference is CPU-bound: on the event loop it would stall every other
    # coroutine in the worker for the duration of the batch.
    return await asyncio.to_thread(_SHARED.embed, list(texts))


async def close_local_embedder() -> None:
    """Release the model. Safe to call when it was never loaded."""
    await asyncio.to_thread(_SHARED.release)
