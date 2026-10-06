"""Async SQLAlchemy 2.0 engine, session factory and FastAPI dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar, Final

from sqlalchemy import MetaData, Table, text
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base shared by every ORM model."""

    metadata = metadata

    #: Narrows the inherited ``FromClause`` annotation: every mapped class here
    #: maps to a real table, so callers get `Table` members without casting.
    __table__: ClassVar[Table]

    def __repr__(self) -> str:
        identity = ", ".join(
            f"{column.key}={getattr(self, column.key)!r}"
            for column in self.__table__.primary_key
        )
        return f"<{type(self).__name__} {identity}>"


def _create_engine() -> AsyncEngine:
    """Build the async engine.

    A bounded pool with pre-ping and recycling protects long-running workers
    against connections dropped by Docker networking or PostgreSQL timeouts.
    """
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle,
        pool_use_lifo=True,
        connect_args={
            "server_settings": {
                "application_name": "svodkapress",
                "jit": "off",
            },
            "timeout": 15,
        },
    )


engine: Final[AsyncEngine] = _create_engine()

AsyncSessionLocal: Final[async_sessionmaker[AsyncSession]] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a transactional session.

    The session is committed on successful handler completion and rolled back
    on any exception, then always closed.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_database_connection() -> bool:
    """Execute `SELECT 1` against PostgreSQL. Returns ``True`` when reachable."""
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))
        return bool(result.scalar_one() == 1)


async def dispose_engine() -> None:
    """Close all pooled connections. Called on application shutdown."""
    await engine.dispose()


__all__ = [
    "AsyncSessionLocal",
    "Base",
    "check_database_connection",
    "dispose_engine",
    "engine",
    "get_db",
    "metadata",
]
