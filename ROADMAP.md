# SvodkaPress NextGen — Master Project Roadmap

## Package Status

| Package | Scope | Phase Section | Status |
| --- | --- | --- | --- |
| Package 1 | Infrastructure, Docker, Async DB Engine, SQLAlchemy Models, Alembic Migrations | Phase 0 (summary only) | Complete |
| Package 2 | Ingestion & Zero-Cost Deduplication | Phase 1 | Complete |
| Package 3 | AI Content Generation & Media Pipeline | Phase 2 | Planned |
| Package 4 | Multi-Platform Publisher Engine | Phase 3 | Planned |
| Package 5 | Control Panel, Analytics & DevOps | Phase 4 | Planned |

Package 1 has no implementation phase of its own below because it is already delivered — Phase 0 is a read-only summary of the shipped foundation, and its full record lives in `CHANGELOG.md`.

## Phase 0: Infrastructure Foundation (Package 1) — COMPLETE

Delivered capabilities that later packages build on rather than re-create:

- Runtime & data layer: Python 3.12, FastAPI, SQLAlchemy 2.0 async + asyncpg, Alembic async migrations, PostgreSQL 16 + pgvector, Redis 7, Pydantic v2 settings.
- Container image: multi-stage Dockerfile (`python:3.12-slim-bookworm`) with Tesseract OCR (rus+eng), OpenCV runtime libs, Playwright Chromium, non-root user uid 1000, tini entrypoint.
- Compose topology: base file, dev override (loopback-only published ports, hot reload), prod (no datastore host exposure, gunicorn + uvicorn workers, memory limits sized for a 4 GB VPS).
- Schema: seven tables (`users`, `sources`, `raw_news`, `news_clusters`, `articles`, `wp_tags_cache`, `system_settings`), four native PostgreSQL enums, `uuid-ossp` and `vector` extensions.
- Vector search: `vector`(1536) columns on `news_clusters.embedding` and `articles.final_embedding`, each with an HNSW cosine index; width is a single constant in `app/models/vector.py`, validated against `AI_EMBEDDING_DIMENSIONS` at startup; embedding columns are deferred by default, load them via `undefer()` for similarity queries.
- Redis policy: `BlockingConnectionPool` client, server runs with `maxmemory-policy noeviction` because it is the ARQ broker and MinHash LSH index, not a cache.
- Retry idempotency support: JSONB columns use mutation-tracking types so in-place nested edits persist.
- Probes: `GET /health` is liveness (PostgreSQL only), `GET /health/ready` is readiness (PostgreSQL + Redis).

## Phase 1: Ingestion & Zero-Cost Deduplication (Package 2)
### Module 2.1: Ingestion Engine
- Telegram Ingestor via GramJS API:
  - Fetch `/dialogs` & `/messages` with overlap `min_id - 10`.
  - HTML entity parser (converts TG bold/italic/links to HTML).
  - Album grouper by `groupedId` taking the lowest message ID as primary.
  - Background task to mark read via `/messages/read`.
- Web Crawler via Playwright Async:
  - Headless Chromium singleton `BrowserPool` (max 2 concurrent pages, resource blocking).
  - Parser using CSS/XPath rules from `sources.crawl_rules`.
  - Trafilatura cleaner for extracting main article content.
- Heuristic Filter (0 Tokens):
  - Strip numbered digests ("Главное к этому часу", "1.", "2.").
  - Strip pure PR/ads (erid:, promo codes, bot links).

### Module 2.2: Deduplication Engine (Zero-Cost First)
- Level 1 (Local In-Memory): MinHash LSH (64-bit fingerprint stored in Redis). Fast fuzzy matching <2ms without external API.
- Level 2 (Semantic Vector Search): `pgvector` Cosine distance against `raw_news` & `news_clusters` for last 48h.
- Level 3 (Novelty Checker): Entity & fact delta comparison. Skip duplicates without new facts; create updates if new details emerge.

## Phase 2: AI Content Generation & Media Pipeline (Package 3)
### Module 3.1: Content Engine
- Prompt matrix for 20 editorial categories (Mash style, Habr visionary, Tinkoff Journal analyst, etc.).
- Slop Validator integrating Excalibur `ai-slop-blocklist` (strict rejection of AI clichés).
- Fixer & QA sub-agent verifying readability, structure, and factual consistency.
- SEO Metadata Generator: Click-worthy title (<=70 chars), description (<=160 chars), focus keywords.
- Vector Embedding generator (`openai/text-embedding-3-small` or local).

### Module 3.2: Media Pipeline
- OCR Watermark Detector (OpenCV + Tesseract) scanning corners/center for channel logos.
- Fast Inpainting / Clean Image Worker.
- Fal AI Flux Schnell prompt generator & downloader if watermark >15% or no photo exists.
- VK Video API integration: `video.save` -> Upload -> Save embed iframe link.
- WordPress Media Library uploader with slug transliteration and Alt tags.

## Phase 3: Multi-Platform Publisher Engine (Package 4)
### Module 4.1: WordPress Publisher (Master Platform)
- WP REST API adapter with Application Passwords.
- Jinja2 Universal Post Template (Featured Image, Video Embed, Carousel Shortcode, Related Posts, Schema.org NewsArticle).
- Rank Math SEO Meta Fields injector (`rank_math_focus_keyword`, `rank_math_title`, `rank_math_description`).
- Infinite Scroll WordPress Theme snippet (IntersectionObserver + history.pushState + Yandex Metrika virtual hit).
- Tag Cache Synchronizer (`wp_tags_cache` lowercase B-Tree).

### Module 4.2: Social Media Adapters (Pluggable)
- Telegram Adapter: Format <=1200 chars HTML with cover photo & "Read more" link.
- VK Community Adapter: Wall post via `wall.post` + attached VK Video or Photo Album.
- OK Adapter: Group topic publication via `mediatopic.post`.
- Dzen Adapter: Hybrid API / WebUI Playwright automation with session state (`storage_state.json`).

## Phase 4: Control Panel, Analytics & DevOps (Package 5)
### Module 5.1: Web UI Dashboard (FastAPI + Jinja2 + HTMX + Tailwind)
- Auth: Login/Password + JWT HttpOnly Secure Cookie.
- Live Pipeline Monitor (SSE streaming real-time worker logs).
- Articles Editor: Visual HTML/Prompt/Tags editing & manual trigger.
- Source Management: TG Channel whitelist with `skip_media` toggle + Web Crawler selector builder.
- AI Model Matrix: Key manager (Fernet encrypted) & Task-to-Model router.
- Platform Toggles: Per-platform enable/disable switch (API / WebUI mode).

### Module 5.2: SEO Analytics & Ranking Watcher
- Yandex Metrika & Google Search Console API connector.
- CTR & Position Watcher: identify positions 4–15 with low CTR.
- One-click Soft Content Optimization loop.

### Module 5.3: DevOps & Dual Environment
- Local Dev Compose (ports open, hot reload).
- Prod VPS Compose (isolated network, healthchecks, auto-restart).
- Remote SSH script executor for target WordPress server.

## Mandatory Workflow

- Always consult `ROADMAP.md` before writing code for any package.
- Update `TODO.md` status before starting and after finishing each task (`[ ]` -> `[/]` -> `[x]`).
- Append entries to `CHANGELOG.md` upon completing each package.

Strict tech-stack and code-quality constraints are defined in `AGENTS.md`; per-module specifications live in `docs/`.
