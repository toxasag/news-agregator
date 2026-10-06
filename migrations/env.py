"""Alembic async migration environment.

The database URL is taken from :mod:`app.config` rather than ``alembic.ini`` so
migrations always target the same instance as the application and no
credentials are committed. The URL is passed straight to the engine instead of
through ``alembic.ini``: percent-encoded characters in a password (produced by
``PostgresDsn.build``) would otherwise be parsed as ``configparser``
interpolation syntax and abort every Alembic command.

Importing :mod:`app.models` registers every mapped class on ``Base.metadata``,
which autogenerate compares against the live schema.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import Base
from app.models import *  # populates Base.metadata for autogenerate

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
        render_as_batch=False,
        transaction_per_migration=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    _configure(
        url=settings.database_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        as_sql=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open an asyncpg engine and run migrations inside a sync-bridged context."""
    connectable = create_async_engine(settings.database_url, poolclass=NullPool)

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
