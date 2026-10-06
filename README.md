# SvodkaPress NextGen

Autonomous news aggregation, deduplication, AI-enrichment, media-processing and
multi-platform publishing pipeline.

Architecture and module specs live in [`docs/`](docs/00_INDEX.md).

## Package 1 scope

Infrastructure, Docker setup, async database engine, ORM models and Alembic
migrations. Later packages add ingestion, dedup, the content engine, media
processing, distribution adapters and the HTMX dashboard.

## Requirements

- Docker + Docker Compose v2
- Python 3.12+ (only needed for running tooling outside containers)

## Quick start (development)

```bash
cp .env.example .env
# Required before first start:
#   POSTGRES_PASSWORD, SECRET_KEY, REDIS_PASSWORD
# REDIS_PASSWORD must also appear inside REDIS_URL:
#   REDIS_URL=redis://:<password>@redis:6379/0
python -c "import secrets; print(secrets.token_urlsafe(64))"   # SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"   # passwords

mkdir -p secrets
docker compose up -d --build
docker compose exec app alembic upgrade head
curl -s http://127.0.0.1:8000/health
```

`docker-compose.override.yml` is picked up automatically, mounts the repository
at `/app`, enables `--reload`, and publishes PostgreSQL, Redis and the app on
`127.0.0.1` only. If another local project already owns a port, override
`POSTGRES_HOST_PORT`, `REDIS_HOST_PORT` or `APP_HOST_PORT` in `.env` instead of
editing the compose files.

## Production (VPS)

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
    exec app alembic upgrade head
```

Passing `-f` explicitly skips the dev override, so PostgreSQL and Redis are
reachable only on the internal `svodkapress_net` bridge. The app listens on
`127.0.0.1:8000` under gunicorn + uvicorn workers, so a host reverse proxy
terminates TLS in front of it.

With `APP_ENV=production`, startup refuses to boot when `SECRET_KEY` or
`POSTGRES_PASSWORD` is missing, still set to an example placeholder, or (for the
secret key) shorter than 32 characters. Redis runs with `maxmemory-policy
noeviction` because it is the ARQ broker and the MinHash LSH dedup index, not a
cache: eviction there would silently drop queued jobs and cause duplicate posts.
Memory limits assume a 4 GB VPS (postgres 1.5G, redis 768M, app 1.5G).

## Database

PostgreSQL 16 with the `vector` and `uuid-ossp` extensions, both created by the
initial migration. Embeddings are `vector(1536)` on `news_clusters.embedding`
and `articles.final_embedding`, each backed by an HNSW cosine index.

Both embedding columns are **deferred**: a plain `select(Article)` does not load
the vector. Opt in explicitly for similarity work:

```python
from sqlalchemy.orm import undefer
await session.execute(select(Article).options(undefer(Article.final_embedding)))
```

The vector width lives in `app/models/vector.py` and is asserted against
`AI_EMBEDDING_DIMENSIONS` at startup, so a mismatched embedding model fails on
boot instead of at the first write. Changing it requires an `ALTER TABLE` plus an
index rebuild in a new migration.

JSONB columns (`platform_statuses`, `enrichment_data`, `entities`, `media_data`,
`crawl_rules`, `video_links`) use mutation-tracking types, so in-place edits like
`article.platform_statuses["tg"]["attempts"] += 1` are flushed. This is what
keeps publishing idempotent across retries.

```bash
alembic upgrade head          # apply migrations
alembic downgrade base        # roll back the whole schema
alembic check                 # fail if models drift from the migrated schema
alembic revision --autogenerate -m "description"
```

The URL is resolved from `POSTGRES_*` by `migrations/env.py` via
`app.config.settings`, so `alembic.ini` holds no credentials.

## Verification

```bash
ruff check .
mypy app scripts migrations/env.py
POSTGRES_DB=svodkapress_verify python -m scripts.smoke_test
```

`scripts/smoke_test.py` verifies the DDL server defaults (via Core inserts that
omit the columns), `uuid_generate_v4()`, pgvector cosine *ordering* across
distinct vectors, JSONB mutation persistence, deferred embedding loading, the
dedup and tag-cache uniqueness guards, and the FK cascade behaviour.

It is destructive, so it refuses to run unless `APP_ENV` is not `production`, the
database name ends in `_verify`/`_test`/`_scratch`/`_ci`, **and** every table is
already empty. It never deletes pre-existing data.

## Layout

```
app/
  config.py        Pydantic v2 settings
  database.py      async engine, session factory, Base, get_db
  redis_client.py  shared Redis pool
  main.py          FastAPI app, lifespan, /health
  models/          SQLAlchemy 2.0 models
migrations/        Alembic async environment and versions
scripts/           verification and maintenance entrypoints
```

## Health

Two probes with different contracts:

- `GET /health` — **liveness**. `200` while the process can reach PostgreSQL,
  `503` only when it cannot. A Redis outage does *not* fail it, matching the
  startup behaviour. Used by the container `HEALTHCHECK` and load balancers, so a
  degraded queue never pulls a serving container out of rotation.
- `GET /health/ready` — **readiness**. `200` only when PostgreSQL *and* Redis are
  both up, otherwise `503` with `status: degraded`. Use it to gate work
  scheduling and for alerting.

Both report per-service state without leaking connection details; the underlying
exception is logged server-side only. Startup aborts if PostgreSQL is
unreachable; an unreachable Redis is logged as a warning so the dashboard stays
available for diagnostics.
