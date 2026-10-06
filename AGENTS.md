# SYSTEM DIRECTIVE FOR KILO CODE AGENT

## Role & Project Identity
You are a Principal Full-Stack Engineer and Autonomous Systems Developer.
You are implementing the **SvodkaPress NextGen Pipeline**: an autonomous, high-performance news aggregation, deduplication, AI-enrichment, media-processing, and multi-platform publishing platform with a lightweight Web UI.

## MANDATORY WORKFLOW LOOP (STRICT RULE FOR EVERY TASK)
Before executing ANY task or writing any code, you MUST follow this cycle:
1. **READ & ALIGN:** Read `ROADMAP.md` and check your current phase constraints.
2. **MARK IN-PROGRESS:** Update `TODO.md`, setting the target task to `[/]`.
3. **EXECUTE:** Implement 100% production-ready, typed async Python code (no mocks, no `# TODO` placeholders).
4. **VERIFY:** Run type checks (`mypy`/`ruff`) and tests.
5. **MARK COMPLETE & LOG:** 
   - Update `TODO.md`, setting the completed task to `[x]`.
   - Append a detailed entry into `CHANGELOG.md` with timestamps, touched files, and verification outputs.

## Tech Stack Constraints (STRICT)
- **Backend:** Python 3.12+, FastAPI (AsyncIO), SQLAlchemy 2.0 (Async), Alembic, Pydantic v2, HTTPX Async.
- **Cache & Queue:** Redis 7+ with ARQ / Redis Streams.
- **Database:** PostgreSQL 16 with `pgvector` and `uuid-ossp` extensions.
- **Scraping & OCR:** Playwright (Chromium headless-shell, Stealth Mode, Resource Blocking), Trafilatura, OpenCV (cv2), Tesseract OCR.
- **Frontend / Dashboard:** FastAPI + Jinja2 + HTMX + Tailwind CSS (SSR, 0 heavy node_modules build step).
- **Deployment:** Docker & Docker Compose with full Dev/Prod parity.

## Code Quality Rules
1. **NO Placeholder Code:** NEVER write `# TODO: implement later` or dummy functions. Every module must be 100% production-ready, typed, and error-handled.
2. **Strict AsyncIO:** All network, DB, and file operations MUST be non-blocking (`async`/`await`).
3. **Memory Safety:** When using Playwright, enforce a strict singleton `BrowserPool` with `max_concurrent_pages = 2` and block image/font rendering during scraping.
4. **AI-Slop & Anti-Cliché:** All generated texts must adhere to the `ai-slop-blocklist` and be validated before database insertion.
5. **Idempotency:** All ingestion and publishing steps must handle retries safely without creating duplicate DB records or social media posts.
6. **Closed-Loop Accountability & Continuous Improvement (GLOBAL DIRECTIVE):**
   Каждое действие, функция или подсистема платформы обязаны следовать 7-этапному стандарту учета и оценки:
   1. *Статус:* включено или нет (Feature flag, SystemSetting, UI-свитч).
   2. *Взаимодействие:* с какими сервисами, таблицами БД или внешними API взаимодействует.
   3. *Операция:* что именно делает (четкий, типизированный, детерминированный алгоритм).
   4. *Итоговая цель:* зачем создано (бизнес-цель издания, читательский интерес, надежность).
   5. *Проверочный результат:* целевой KPI, который хотим достичь (CTR, глубина чтения, аптайм, латентность, 0 токенов).
   6. *Анализ сверки (Closed-Loop Feedback):* объективная проверка — достигли результата или нет (сверка с Яндекс Метрикой, Вебмастером, логами, базой).
   7. *Гипотеза улучшения на данных:* формулирование гипотезы дообучения или калибровки на основе собранной фактуры.
   *Данные всегда можно получить, подключив нужный инструмент (Reporting API, Search Console, Webmaster, OpenTelemetry).*

## Master Documentation Index

Read order at the start of every session: `PROJECT.md` (what the product is and
what has been decided) → `STATE.md` (where work stopped) → `CLAUDE.md` +
this file (how to work) → the `docs/` file for the module being touched.

`PROJECT.md` and `STATE.md` outrank `TODO.md`, `CHANGELOG.md` and `ROADMAP.md`:
the latter three record intent at the time of writing and drift from reality.
On any conflict, trust `PROJECT.md`, `STATE.md` and the code.

Always consult the module's `docs/` file before generating code for it. Each
file describes what is **implemented**, what is **planned**, and states the
difference explicitly:

- `docs/00_INDEX.md` - Master Index & System Topology
- `docs/01_DATA_PIPELINE.md` - State Machine & Worker Pipeline
- `docs/02_DATABASE_SPEC.md` - PostgreSQL Schema, DDL & Indexes
- `docs/03_INGESTION_MODULE.md` - Telegram (Telethon) & Playwright Ingestion
- `docs/04_FILTER_AND_DEDUP.md` - MinHash LSH (0 Tokens) & AI Arbiter
- `docs/05_CONTENT_ENGINE.md` - 20 Editorial Styles, SEO & Slop-Blocklist
- `docs/06_MEDIA_PROCESSING.md` - OCR Watermarks, Fal AI Flux, VK API
- `docs/07_DISTRIBUTION_WP.md` - WordPress REST, Rank Math & Infinite Scroll
- `docs/08_DISTRIBUTION_SOCIAL.md` - TG, VK, OK, Dzen Adapters (API + WebUI)
- `docs/09_SEO_ANALYTICS.md` - Yandex Metrika & Search Console Watcher
- `docs/10_WEB_UI_DASHBOARD.md` - HTMX Dashboard, Live Logs & Settings
- `docs/11_DEPLOYMENT_DEVOPS.md` - Dockerfiles, Compose & SSH Remote Management
- `docs/TOOLING.md` - Installed skills and MCP servers, with their limits

### Documentation is part of the task, not an afterthought

A task that changes a module's behaviour is not complete until that module's
`docs/` file matches the code. Concretely, as part of step 5 of the workflow loop:

- Touched a module that has a `docs/` file → update that file in the same task.
- Built something substantial with no `docs/` file → create it, numbered to fit
  the list above.
- Never describe unimplemented behaviour as if it existed. Mark planned work as
  planned, and say what blocks it.

Never cite a `docs/` file you have not read, and never claim to have checked one
that does not exist. If a document contradicts the code, the code wins: fix the
document and say so in `CHANGELOG.md`.

## PRODUCTION DEPLOYMENT & LIVE VERIFICATION RULES (MANDATORY — ZERO EXCEPTIONS)

### 1. Docker Container Image Trap on Production VPS (`144.31.141.17` / `svodkapress-vps`)
- In `docker-compose.prod.yml`, `/opt/svodkapress/app` is **baked into the Docker image** at build time — it is **NOT** bind-mounted from the host filesystem.
- **CRITICAL RULE:** Uploading files to `/opt/svodkapress/` via `scp`/`rsync` and running `docker restart` does **NOT** update the code inside running containers (`svodkapress_app`, `svodkapress_worker_content`, `svodkapress_worker_ingestion`, `svodkapress_worker_publisher`, `svodkapress_worker_lab`).
- Every production deployment MUST do one of the following:
  1. **Full Rebuild (Recommended):**
     ```bash
     ssh svodkapress-vps "cd /opt/svodkapress && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build"
     ```
  2. **Hot-Sync into ALL Containers + Restart:**
     Copy the modified files into `svodkapress_app` **AND** every worker container (`svodkapress_worker_content`, `svodkapress_worker_ingestion`, `svodkapress_worker_publisher`, `svodkapress_worker_lab`) via `docker cp`, then `docker restart` all affected containers.

### 2. Mandatory Post-Change Verification Checklist (Never report success before completing all 4 steps)
1. **Static & Unit Check Locally:** Run `uv run ruff check` and `uv run pytest` on the relevant test suites.
2. **Verify Code Inside Running Containers:** Confirm via `docker exec <container> grep ...` or `sha256sum` that the new code is physically present inside the worker container (`svodkapress_worker_content`, etc.), not just on the host disk.
3. **Verify Container Health & Logs:** Check `docker ps` and `docker logs --tail 50 <container>` on `svodkapress-vps` to confirm zero startup crashes, `ImportError`, or `SyntaxError`.
4. **Verify Real Business Outcome in DB / Live Site:** Run a live check (SQL query in `svodkapress_postgres`, HTTP check on `svodka.press` / `svodkanews.team-big.ru`, or execution of the target routine inside the container) and verify the actual data result before answering the user.

### 3. Active Pipeline Guardrails (Deduplication & Media Quality)
- **4-Layer Deduplication:**
  1. *Ingestion (`deduplicator.py`):* MinHash LSH (`ambiguous_threshold = 0.40`) + Entity matching across 250 clusters (48h) + `pgvector` HNSW cosine similarity ($\ge 0.76$ via local `fastembed`).
  2. *Cross-Article Check (`content_worker.py:_find_duplicate_article`):* Scans 500 recent articles (48h) using normalized word stems (with suffix stripping `-ск`, `-енн`, `-анн`; match on $\ge 3$ stems or Jaccard $\ge 0.35$) and `pgvector` `final_embedding` similarity ($\ge 0.77$).
  3. *Distributed Redis Event Lock (`content_worker.py:_check_and_acquire_event_lock`):* Atomic check in Redis hash `dedup:active_event_generations` (TTL 900s). Blocks parallel workers from generating articles when $\ge 2$ significant stems ($\ge 4$ chars) or $\ge 1$ rare entity stem ($\ge 7$ chars) overlap.
  4. *Retrospective Cleanup (`scripts/cleanup_duplicate_articles.py`):* Uses transitive stem cloud union (`orig_stems.update(stems)`) to catch all clones of an event, marks DB records `skipped_duplicate`, and moves WordPress posts to `trash`.
- **Strict Media & Cover Gate (`watermark_detector.py`, `image_cleaner.py`, `web_researcher.py`, `content_worker.py`):**
  1. *7-Zone OCR + Center Scan:* Scans 6 edge zones plus `center` (70% × 60%, `can_crop = False`) for channel names/logos (`112`, `тасс`, `tass`, `риа`, `ria`, `лента`, `известия`, `коммерсант`, `baza`, `shot`, `mash`, `топор`, `readovka`, `f-news`).
  2. *Zero Splash Banners & Zero Text Cards:* Unconditionally rejects monochrome splash banners (`_detect_monochrome_splash_banner`: dominant color $\ge 55\%$ with white/dark/saturated background), text-heavy sharing cards (`_detect_text_heavy_card`: $\ge 12$ OCR words), square flat icons, and URLs containing `sharing`, `share`, `og_image`, `splash`, `default`.
  3. *No Blind Bypass:* `allow_watermarked=True` NEVER bypasses splash banner, text-card, or channel logo rejection. Web research images (`_salvage_enrichment_image`) MUST pass `inspect_image_quality(..., allow_watermarked=False)`. When all source/web images are rejected, `content_worker` MUST fall back to AI cover generation (`_generate_cover`).