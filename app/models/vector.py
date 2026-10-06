"""Single source of truth for pgvector embedding columns.

The vector width is a *schema* property: changing it requires an `ALTER TABLE`
and an index rebuild, so it cannot be driven by an environment variable at
runtime. Every embedding column and every HNSW index derives its parameters
from this module, and :mod:`app.config` validates the configured embedding
dimension against :data:`EMBEDDING_DIMENSIONS` at startup so a mismatched
embedding model fails loudly instead of at first insert.
"""

from __future__ import annotations

from typing import Any, Final

from pgvector.sqlalchemy import Vector
from sqlalchemy.orm import Mapped, mapped_column

from app.config import SCHEMA_EMBEDDING_DIMENSIONS

#: Width of every embedding column in the schema. Must match the migration.
EMBEDDING_DIMENSIONS: Final[int] = SCHEMA_EMBEDDING_DIMENSIONS

#: HNSW build parameters, shared by the models and the migration.
HNSW_OPTIONS: Final[dict[str, int]] = {"m": 16, "ef_construction": 64}

#: Operator class used for all similarity search (cosine distance).
VECTOR_OPS: Final[str] = "vector_cosine_ops"


def embedding_column(**kwargs: Any) -> Mapped[list[float] | None]:
    """Return a deferred ``vector(1536)`` column.

    Embeddings are ~15-25 KB per row and pgvector transfers them as text, so
    loading them on every ``select()`` would dominate dashboard and worker
    queue queries that never read the vector. The column is deferred into the
    ``embedding`` group; similarity queries opt in with
    ``undefer(Model.column)`` or by selecting the column explicitly.
    """
    return mapped_column(
        Vector(EMBEDDING_DIMENSIONS),
        nullable=True,
        deferred=True,
        deferred_group="embedding",
        **kwargs,
    )


def hnsw_index_kwargs(column_name: str) -> dict[str, Any]:
    """Return the dialect kwargs for an HNSW cosine index on ``column_name``."""
    return {
        "postgresql_using": "hnsw",
        "postgresql_with": dict(HNSW_OPTIONS),
        "postgresql_ops": {column_name: VECTOR_OPS},
    }


__all__ = [
    "EMBEDDING_DIMENSIONS",
    "HNSW_OPTIONS",
    "VECTOR_OPS",
    "embedding_column",
    "hnsw_index_kwargs",
]
