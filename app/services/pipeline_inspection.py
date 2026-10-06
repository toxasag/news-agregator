"""Zero-token inspection run: reset, collect, dedup, watermarks, research.

The Лаборатория already walks one cluster to a finished article, but that answers a
different question. Before trusting the pipeline an operator needs to see the
*intake* in full: every post that arrived from every channel, which of them were
rejected and by which rule, which attached photos carry a channel watermark, and
what the archive already knows about the stories that survived.

This module is that pass, and it is deliberately separated from
:mod:`app.services.pipeline_runner` by one property: **it never calls a paid
model.** Moderation, generation, SEO and cover generation are the only stages that
cost tokens, and none of them run here. What remains still covers the four
decisions that determine whether the pipeline is worth running at all:

* **Collect.** After a reset, ``sources.last_scraped_at`` is ``NULL`` for every
  channel, so :func:`~app.workers.ingestion_worker.run_ingestion` takes exactly
  ``INGEST_MAX_MESSAGES_PER_SOURCE`` posts from each — the "first pass" branch in
  the reader. That is what makes "50 posts from every channel" a consequence of the
  reset rather than a special mode nobody would maintain.

* **Dedup.** Read back from what deduplication persisted per post rather than
  re-run: re-running would produce different numbers, because the first pass has
  already indexed those posts. Every row states the verdict, the ladder level, the
  similarity and the threshold it was compared against.

* **Watermarks.** The one stage that must download bytes, because a watermark
  verdict cannot be reached from metadata. OCR through Tesseract and the overlay
  mask are local and free; the ceiling on how many images are fetched is explicit,
  since the bytes are the expensive part, not the analysis.

* **Research.** Facts the posts agree on plus previously published articles on the
  same topic, both from the database. No web search, no embeddings, no tokens.
"""

from __future__ import annotations

# Operator-facing summaries are Russian, matching the dashboard.
# ruff: noqa: RUF001
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.cluster import NewsCluster
from app.models.enums import DedupStatus, MediaStrategy
from app.models.raw_news import RawNews
from app.models.source import Source
from app.services.pipeline_reset import ResetBlockedError, reset_pipeline_queue
from app.services.pipeline_stages import (
    ArtifactStore,
    RunMode,
    RunSummary,
    StageName,
    StageResult,
    new_run_id,
    preview,
)
from app.services.research import (
    ARCHIVE_LOOKBACK,
    collect_facts,
    find_related_articles,
)
from app.services.telegram_ingestor import (
    TelegramIngestorError,
    TelegramNotConfiguredError,
)
from app.services.telegram_mtproto import MTProtoIngestor
from app.services.watermark_detector import WatermarkError, detect_watermarks
from app.workers.ingestion_worker import IngestionRunReport, run_ingestion

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = ["InspectionRunner", "run_inspection"]

#: Ceiling on images downloaded for the watermark check in one run. The analysis is
#: free; the download is not, and 50 posts per channel across dozens of channels
#: would otherwise pull hundreds of megabytes through one account.
MAX_WATERMARK_IMAGES: Final[int] = 40

#: Per-image size ceiling. Larger attachments are reported as unchecked rather than
#: loaded: the container shares its memory with Chromium and the OCR pipeline.
MAX_IMAGE_BYTES: Final[int] = 12 * 1024 * 1024

#: How many posts the dedup table shows. The counters cover the whole window; the
#: rows are evidence, and a browser table past a few hundred rows is a scrollbar.
MAX_DEDUP_ROWS: Final[int] = 300

#: Clusters described in the research report. Bounded because each one triggers an
#: archive comparison.
MAX_RESEARCH_CLUSTERS: Final[int] = 25

#: Dedup statuses whose explanation needs no numbers. The two that do — a duplicate
#: and a unique post — are worded in :meth:`InspectionRunner._explain`, because they
#: quote the similarity and the threshold it was compared against.
_STATUS_EXPLANATIONS: Final[dict[DedupStatus, str]] = {
    DedupStatus.UPDATE: "Дополнение: в посте есть новые факты, кластер обновлён",
    DedupStatus.PENDING: "Ещё не проверялся",
    DedupStatus.FILTERED_DIGEST: "Отброшен фильтром: дайджест (0 токенов)",
    DedupStatus.FILTERED_AD: "Отброшен фильтром: реклама (0 токенов)",
    # A unique post with no similarity score never reached level 2: nothing in the
    # window was close enough for the ladder to measure against.
    DedupStatus.UNIQUE: "Новая новость: похожих не найдено",
}


@dataclass(slots=True)
class _Stopwatch:
    """Millisecond timer for one stage."""

    started: float

    @classmethod
    def start(cls) -> _Stopwatch:
        return cls(started=time.monotonic())

    def stop(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


def _media_url(entry: Mapping[str, Any]) -> str:
    """Return a fetchable http URL from a media descriptor, if it has one.

    Website media store an absolute URL in ``media_id``; Telegram media store a
    numeric file id there, which is not fetchable over HTTP and needs the MTProto
    client instead. Distinguishing them by scheme is what
    :func:`app.workers.content_worker._media_url` already does, and the two must
    agree or the inspection would report a different media plan than production.
    """
    candidate = str(entry.get("url") or entry.get("media_id") or "")
    return candidate if candidate.startswith(("http://", "https://")) else ""


def _skip_reason(item: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return ``(verdict, reason)`` when this photo must not be checked.

    Separating "never looked" from "looked and found nothing" is the point: both
    produce no watermark, and only one of them means the image is publishable.
    ``ALWAYS_REGENERATE`` is reported as rejected rather than skipped because
    production genuinely will not use the original, so calling it unchecked would
    understate what happens.
    """
    if not item["fetch_media"]:
        return "unchecked", "У источника выключен сбор медиа."
    strategy = str(item["media_strategy"])
    if strategy == MediaStrategy.TEXT_ONLY.value:
        return "unchecked", "Стратегия источника — только текст."
    if strategy == MediaStrategy.ALWAYS_REGENERATE.value:
        return (
            "rejected",
            "Стратегия источника — всегда генерировать обложку, оригиналы "
            "не используются.",
        )
    return None


def _describe_verdict(verdict: Mapping[str, Any]) -> tuple[str, str]:
    """Turn a watermark report into an operator-facing verdict and reason."""
    if not verdict["has_watermark"]:
        return "clean", "Вотермарков не найдено, оригинал можно публиковать."
    if verdict["needs_ai_generation"] or not verdict["is_croppable"]:
        detected = ", ".join(verdict["detected_text"][:3]) or "текст не распознан"
        return (
            "rejected",
            f"Отсеяно: покрытие {verdict['coverage_ratio']:.0%}, найдено: "
            f"{detected}. Потребуется сгенерированная обложка.",
        )
    return (
        "croppable",
        f"Вотермарк в полосе, покрытие {verdict['coverage_ratio']:.0%} — "
        "полоса будет обрезана, изображение сохранится.",
    )


async def _fetch_over_http(url: str, row: dict[str, Any]) -> bytes:
    """Download one website image, recording the failure reason on ``row``."""
    try:
        async with httpx.AsyncClient(
            timeout=settings.ingest_http_timeout, follow_redirects=True
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as error:
        row["reason"] = f"Не удалось скачать: {type(error).__name__}"
        return b""
    if response.status_code >= httpx.codes.BAD_REQUEST:
        row["reason"] = f"Источник ответил HTTP {response.status_code}"
        return b""
    content = response.content
    if not content or len(content) > MAX_IMAGE_BYTES:
        row["reason"] = (
            f"Размер {len(content)} байт вне допустимого диапазона "
            f"(до {MAX_IMAGE_BYTES})"
        )
        return b""
    return content


async def _fetch_over_telegram(
    item: Mapping[str, Any],
    telegram: MTProtoIngestor | None,
    row: dict[str, Any],
    telegram_error: str,
) -> bytes:
    """Download one Telegram photo through the pooled MTProto client."""
    if telegram is None:
        row["reason"] = telegram_error or "Нет клиента Telegram для скачивания."
        return b""
    message_id = item.get("message_id")
    identifier = str(item.get("identifier") or "")
    if not isinstance(message_id, int) or not identifier:
        row["reason"] = "У медиа нет id сообщения — скачать нечем."
        return b""
    try:
        content = await telegram.download_photo(
            identifier, message_id, limit_bytes=MAX_IMAGE_BYTES
        )
    except TelegramIngestorError as error:
        row["reason"] = f"Telegram отказал: {error}"
        return b""
    if not content:
        row["reason"] = "Telegram не отдал файл (или он больше лимита)."
    return content


class InspectionRunner:
    """Runs the zero-token intake inspection, recording every stage.

    Constructed per run. Unlike :class:`~app.services.pipeline_runner.PipelineRunner`
    this never reaches the writer, so it is safe to press repeatedly while tuning
    thresholds: the only cost is Telegram traffic.
    """

    __slots__ = ("_index", "_session", "_store", "_summary")

    def __init__(
        self, session: AsyncSession, *, store: ArtifactStore | None = None
    ) -> None:
        self._session = session
        self._store = store or ArtifactStore()
        self._summary = RunSummary(
            run_id=new_run_id(), mode=RunMode.TEST, started_at=datetime.now(UTC)
        )
        self._index = 0

    @property
    def summary(self) -> RunSummary:
        return self._summary

    def _record(self, result: StageResult) -> StageResult:
        """Append a stage result and persist it as an artifact."""
        self._index += 1
        self._summary.stages.append(result)
        try:
            self._store.write_stage(self._summary.run_id, self._index, result)
        except (OSError, ValueError):
            # Artifacts are for the operator's benefit; losing one must not fail a
            # run whose real work succeeded.
            logger.warning(
                "Could not write artifact for %s", result.stage, exc_info=True
            )
        return result

    async def run(
        self, *, reset: bool = False, include_articles: bool = False
    ) -> RunSummary:
        """Execute the inspection and return the recorded stages.

        ``reset=True`` clears the queue first, which is what turns the following
        collection into a first pass over every channel. Left off by default: the
        reset deletes collected posts, and that must be an explicit choice rather
        than a side effect of pressing "inspect".
        """
        try:
            if reset and not await self._stage_reset(
                include_articles=include_articles
            ):
                self._finish()
                return self._summary
            await self._stage_collect()
            posts = await self._stage_dedup()
            await self._stage_watermarks(posts)
            await self._stage_research()
        # Deliberately broad: this is an operator tool, and an unexpected failure
        # belongs on the timeline rather than in a 500 page.
        except Exception as error:
            logger.exception("Inspection run %s crashed", self._summary.run_id)
            crash = StageResult(stage=StageName.RESEARCH)
            crash.fail(f"Непредвиденная ошибка: {type(error).__name__}: {error}")
            self._record(crash)
        self._finish()
        return self._summary

    def _finish(self) -> None:
        self._summary.finished_at = datetime.now(UTC)
        try:
            self._store.write_summary(self._summary)
        except (OSError, ValueError):
            logger.warning("Could not write run summary", exc_info=True)

    # ------------------------------------------------------------------ stages

    async def _stage_reset(self, *, include_articles: bool) -> bool:
        """Clear the queue. Returns whether the run may continue.

        A refused reset stops the run rather than collecting anyway: the operator
        asked to see a first pass, and collecting without the reset would show the
        overlap window instead, which looks like an empty channel.
        """
        result = StageResult(stage=StageName.RESET)
        watch = _Stopwatch.start()
        try:
            report = await reset_pipeline_queue(
                self._session, include_articles=include_articles
            )
        except ResetBlockedError as error:
            result.duration_ms = watch.stop()
            result.fail(str(error))
            self._record(result)
            return False

        result.duration_ms = watch.stop()
        result.summary = report.summary()
        result.details = report.to_dict()
        result.details["note"] = (
            "Watermark каналов сброшен: следующий сбор возьмёт по "
            f"{settings.ingest_max_messages_per_source} постов с каждого канала, "
            "как при первом запуске."
        )
        for warning in report.warnings:
            result.warn(warning)
        self._record(result)
        return True

    async def _stage_collect(self) -> StageResult:
        """Collect every active channel and report the per-source outcome."""
        result = StageResult(stage=StageName.COLLECT)
        watch = _Stopwatch.start()

        active = await self._session.execute(
            select(func.count()).select_from(Source).where(Source.is_active.is_(True))
        )
        active_count = int(active.scalar_one())
        if not active_count:
            result.duration_ms = watch.stop()
            result.skip(
                "Нет активных источников: отметьте каналы на странице «Источники»."
            )
            return self._record(result)

        report: IngestionRunReport = await run_ingestion()
        payload = report.to_dict()
        totals = payload["totals"]
        result.duration_ms = watch.stop()

        if report.lock_skipped:
            result.skip(
                "Сбор уже идёт в боевом режиме — этот проход ничего не собрал."
            )
            return self._record(result)

        first_pass = [
            source for source in payload["sources"] if source["skipped_existing"] == 0
        ]
        result.summary = (
            f"Каналов опрошено: {totals['sources']}, получено постов: "
            f"{totals['fetched']}, записано новых: {totals['inserted']}, "
            f"уже было в базе: {totals['skipped_existing']}"
        )
        result.details = {
            "active_sources": active_count,
            "limit_per_source": settings.ingest_max_messages_per_source,
            "totals": totals,
            "first_pass_sources": len(first_pass),
            "sources": payload["sources"],
            "note": (
                f"Лимит на канал — {settings.ingest_max_messages_per_source} постов. "
                "Канал, у которого «уже было» больше нуля, собирается не впервые: "
                "чтобы получить полную выборку, сбросьте очередь."
            ),
        }
        for message in report.errors:
            result.warn(message)
        return self._record(result)

    async def _stage_dedup(self) -> list[RawNews]:
        """Report every collected post and the verdict deduplication gave it.

        Returns the posts, so the watermark stage inspects exactly what this stage
        described rather than issuing its own query that could drift from it.
        """
        result = StageResult(stage=StageName.DEDUP)
        watch = _Stopwatch.start()

        window = datetime.now(UTC) - timedelta(hours=settings.dedup_lookback_hours)
        recent = await self._session.execute(
            select(RawNews)
            .where(RawNews.created_at >= window)
            .order_by(RawNews.created_at.desc())
        )
        posts = list(recent.scalars().all())

        by_status: dict[str, int] = {}
        rows: list[dict[str, Any]] = []
        media_total = 0
        for post in posts:
            status = post.dedup_status.value
            by_status[status] = by_status.get(status, 0) + 1
            media = [dict(item) for item in (post.media_data or ())]
            media_total += len(media)
            if len(rows) < MAX_DEDUP_ROWS:
                rows.append(
                    {
                        "raw_news_id": post.id,
                        "source_id": post.source_id,
                        "source_name": post.source.name if post.source else "?",
                        "external_post_id": post.external_post_id,
                        "post_url": post.post_url,
                        "text": preview(post.raw_text or "", 220),
                        "status": status,
                        "level": post.dedup_level,
                        "similarity": (
                            round(float(post.similarity_score), 3)
                            if post.similarity_score is not None
                            else None
                        ),
                        "cluster_id": (
                            str(post.cluster_id) if post.cluster_id else None
                        ),
                        "is_digest": post.is_digest,
                        "is_ad": post.is_ad,
                        "filter_reasons": list(post.filter_reasons or ()),
                        "verdict": self._explain(post),
                        "media": [
                            {
                                "type": str(item.get("type") or "?"),
                                "media_id": str(item.get("media_id") or ""),
                                "size": item.get("size"),
                                "url": _media_url(item),
                            }
                            for item in media
                        ],
                    }
                )

        duplicates = [row for row in rows if row["status"] == "skipped_duplicate"]
        result.duration_ms = watch.stop()
        result.summary = (
            f"Постов за {settings.dedup_lookback_hours}ч: {len(posts)}, медиа: "
            f"{media_total}. "
            + ", ".join(f"{name}: {count}" for name, count in sorted(by_status.items()))
        )
        result.details = {
            "window_hours": settings.dedup_lookback_hours,
            "thresholds": {
                "lower": settings.similarity_lower_threshold,
                "upper": settings.similarity_upper_threshold,
            },
            "by_status": by_status,
            "total_posts": len(posts),
            "media_total": media_total,
            "shown": len(rows),
            "posts": rows,
            "duplicates": duplicates,
            "note": (
                "Вердикты прочитаны из базы, а не пересчитаны: первый проход уже "
                "проиндексировал эти посты, и повторный расчёт дал бы другие числа."
            ),
        }
        if len(posts) > len(rows):
            result.warn(
                f"Показаны первые {len(rows)} постов из {len(posts)}; счётчики "
                "посчитаны по всей выборке."
            )
        self._record(result)
        return posts

    @staticmethod
    def _explain(post: RawNews) -> str:
        """Explain one post's verdict in the operator's terms.

        The zero-token filters are checked before the dedup status because a post
        rejected as a digest never reached the ladder at all, and reporting its
        ``PENDING`` status would read as "not checked yet".
        """
        if post.is_digest:
            return "Отброшен фильтром: дайджест (0 токенов)"
        if post.is_ad:
            return "Отброшен фильтром: реклама (0 токенов)"

        status = post.dedup_status
        similarity = post.similarity_score
        if status is DedupStatus.SKIPPED_DUPLICATE:
            if similarity is None:
                return f"Дубль по уровню {post.dedup_level}"
            return (
                f"Дубль: сходство {similarity:.3f} на уровне {post.dedup_level} "
                f"(порог дубликата {settings.similarity_upper_threshold})"
            )
        if status is DedupStatus.UNIQUE and similarity is not None:
            return (
                f"Новая новость: сходство {similarity:.3f} ниже порога "
                f"{settings.similarity_upper_threshold}"
            )
        return _STATUS_EXPLANATIONS.get(status, status.value)

    async def _stage_watermarks(self, posts: Sequence[RawNews]) -> StageResult:
        """Download the attached photos and report the watermark verdicts.

        The only stage here that fetches bytes, and the only one that can be
        rate-limited, so it is bounded twice: by :data:`MAX_WATERMARK_IMAGES` and by
        :data:`MAX_IMAGE_BYTES` per file. Everything it uses — Tesseract OCR and the
        overlay mask — runs locally and costs nothing.

        Posts whose source is set to ``TEXT_ONLY`` or has media collection off are
        reported as skipped rather than silently omitted: "no watermark found" and
        "never looked" must not read the same.
        """
        result = StageResult(stage=StageName.WATERMARK)
        watch = _Stopwatch.start()

        candidates = self._photo_candidates(posts)
        if not candidates:
            result.duration_ms = watch.stop()
            result.skip(
                "Фотографий среди собранных постов нет — проверять вотермарки не на чем."
            )
            self._record(result)
            return result

        checked: list[dict[str, Any]] = []
        telegram: MTProtoIngestor | None = None
        telegram_error = ""
        needs_telegram = any(not item["url"] for item in candidates)
        if needs_telegram:
            telegram, telegram_error = await self._open_telegram()

        try:
            for item in candidates[:MAX_WATERMARK_IMAGES]:
                checked.append(await self._check_one(item, telegram, telegram_error))
        finally:
            if telegram is not None:
                await telegram.close()

        rejected = [row for row in checked if row["verdict"] == "rejected"]
        croppable = [row for row in checked if row["verdict"] == "croppable"]
        clean = [row for row in checked if row["verdict"] == "clean"]
        unchecked = [row for row in checked if row["verdict"] == "unchecked"]

        result.duration_ms = watch.stop()
        result.summary = (
            f"Проверено изображений: {len(checked)} из {len(candidates)}. "
            f"Чистых: {len(clean)}, обрежется: {len(croppable)}, "
            f"отсеяно по вотермарку: {len(rejected)}, не удалось проверить: "
            f"{len(unchecked)}"
        )
        result.details = {
            "total_photos": len(candidates),
            "checked": len(checked),
            "limit": MAX_WATERMARK_IMAGES,
            "size_limit_bytes": MAX_IMAGE_BYTES,
            "clean": len(clean),
            "croppable": len(croppable),
            "rejected": len(rejected),
            "unchecked": len(unchecked),
            "items": checked,
            "rejected_items": rejected,
            "note": (
                "OCR (Tesseract) и маска накладки считаются локально и не тратят "
                "токенов. Отсеянное изображение означает, что обложка будет "
                "сгенерирована — это единственный платный шаг, и он здесь не "
                "выполняется."
            ),
        }
        if len(candidates) > len(checked):
            result.warn(
                f"Проверены первые {len(checked)} изображений из {len(candidates)}: "
                "скачивание — самая дорогая часть прохода."
            )
        if telegram_error:
            result.warn(telegram_error)
        self._record(result)
        return result

    def _photo_candidates(self, posts: Sequence[RawNews]) -> list[dict[str, Any]]:
        """Collect the photos worth checking, newest post first.

        Duplicate ``media_id`` values are collapsed: an album re-posted by three
        channels is the same file, and OCR-ing it three times would spend the
        image budget on one photograph.
        """
        seen: set[str] = set()
        candidates: list[dict[str, Any]] = []
        for post in posts:
            source = post.source
            strategy = source.media_strategy if source else MediaStrategy.OCR_CHECK
            fetch_media = source.fetch_media if source else True
            for item in post.media_data or ():
                if str(item.get("type") or "") != "photo":
                    continue
                media_id = str(item.get("media_id") or "")
                if media_id and media_id in seen:
                    continue
                if media_id:
                    seen.add(media_id)
                candidates.append(
                    {
                        "raw_news_id": post.id,
                        "source_id": post.source_id,
                        "source_name": source.name if source else "?",
                        "source_type": (
                            source.source_type.value if source else "?"
                        ),
                        "identifier": source.identifier if source else "",
                        "external_post_id": post.external_post_id,
                        "message_id": item.get("original_post_id"),
                        "media_id": media_id,
                        "size": item.get("size"),
                        "url": _media_url(item),
                        "post_text": preview(post.raw_text or "", 140),
                        "media_strategy": strategy.value,
                        "fetch_media": fetch_media,
                    }
                )
        return candidates

    async def _open_telegram(self) -> tuple[MTProtoIngestor | None, str]:
        """Open one MTProto client for the whole watermark stage.

        One client per run rather than per image: each connection claims a pooled
        account and performs a sign-in handshake, so opening dozens would both slow
        the stage and defeat the rotation the pool exists to provide.
        """
        reader = MTProtoIngestor(self._session)
        try:
            await reader.start()
        except TelegramNotConfiguredError as error:
            return None, (
                f"Медиа Telegram не проверено: нет доступного аккаунта ({error})."
            )
        except TelegramIngestorError as error:
            return None, f"Медиа Telegram не проверено: {error}"
        return reader, ""

    async def _check_one(
        self,
        item: Mapping[str, Any],
        telegram: MTProtoIngestor | None,
        telegram_error: str,
    ) -> dict[str, Any]:
        """Fetch and analyse one photo, returning its row for the report."""
        row: dict[str, Any] = dict(item)
        row["verdict"] = "unchecked"
        row["reason"] = ""
        row["watermark"] = None

        skip = _skip_reason(item)
        if skip is not None:
            row["verdict"], row["reason"] = skip
            return row

        content = await self._fetch_image(item, telegram, row, telegram_error)
        if not content:
            return row

        row["bytes"] = len(content)
        try:
            verdict = await detect_watermarks(content)
        except WatermarkError as error:
            row["reason"] = f"Изображение не удалось разобрать: {error}"
            return row

        row["watermark"] = verdict
        row["verdict"], row["reason"] = _describe_verdict(verdict)
        return row

    async def _fetch_image(
        self,
        item: Mapping[str, Any],
        telegram: MTProtoIngestor | None,
        row: dict[str, Any],
        telegram_error: str,
    ) -> bytes:
        """Download one image, by URL for web sources and MTProto for Telegram."""
        url = str(item["url"])
        if url:
            return await _fetch_over_http(url, row)
        return await _fetch_over_telegram(item, telegram, row, telegram_error)

    async def _stage_research(self) -> StageResult:
        """Report what the archive knows about the surviving clusters.

        Token-free by construction: the facts come from the posts themselves and
        the related material from previously published articles, both by database
        query. Title-word overlap rather than embeddings because it is explainable
        in the UI, which matters more here than recall.
        """
        result = StageResult(stage=StageName.RESEARCH)
        watch = _Stopwatch.start()

        window = datetime.now(UTC) - timedelta(hours=settings.dedup_lookback_hours)
        rows = await self._session.execute(
            select(NewsCluster)
            .where(NewsCluster.created_at >= window)
            .order_by(NewsCluster.created_at.desc())
        )
        clusters = list(rows.scalars().all())
        if not clusters:
            result.duration_ms = watch.stop()
            result.skip(
                "Кластеров за окно дедупликации нет: собранные посты либо "
                "отфильтрованы, либо оказались дублями."
            )
            self._record(result)
            return result

        described: list[dict[str, Any]] = []
        for cluster in clusters[:MAX_RESEARCH_CLUSTERS]:
            members = await self._session.execute(
                select(RawNews)
                .where(RawNews.cluster_id == cluster.id)
                .order_by(RawNews.id)
            )
            posts = list(members.scalars().all())
            media: list[dict[str, Any]] = []
            for post in posts:
                for entry in post.media_data or ():
                    media.append(
                        {
                            "type": str(entry.get("type") or "?"),
                            "media_id": str(entry.get("media_id") or ""),
                            "size": entry.get("size"),
                        }
                    )
            facts = collect_facts(cluster, posts)
            related = await find_related_articles(self._session, cluster)
            described.append(
                {
                    "cluster_id": str(cluster.id),
                    "title": cluster.title,
                    "category": cluster.category,
                    "risk_level": cluster.risk_level.value,
                    "posts": len(posts),
                    "sources": sorted(
                        {post.source.name for post in posts if post.source}
                    ),
                    "texts": [
                        {
                            "raw_news_id": post.id,
                            "source_name": post.source.name if post.source else "?",
                            "text": preview(post.raw_text or "", 300),
                        }
                        for post in posts
                    ],
                    "media": media,
                    "facts": facts,
                    "related_articles": related,
                    "is_published": cluster.is_published,
                }
            )

        with_archive = [item for item in described if item["related_articles"]]
        result.duration_ms = watch.stop()
        result.summary = (
            f"Кластеров: {len(clusters)}, разобрано: {len(described)}. "
            f"Есть похожие прошлые материалы у {len(with_archive)}"
        )
        result.details = {
            "total_clusters": len(clusters),
            "described": len(described),
            "window_hours": settings.dedup_lookback_hours,
            "archive_days": ARCHIVE_LOOKBACK.days,
            "clusters": described,
            "note": (
                f"Ресерч читает архив за {ARCHIVE_LOOKBACK.days} дней и собранные "
                "посты. Ни одного платного вызова: ни модерации, ни генерации, ни "
                "эмбеддингов через шлюз."
            ),
        }
        if len(clusters) > len(described):
            result.warn(
                f"Разобраны первые {len(described)} кластеров из {len(clusters)}."
            )
        self._record(result)
        return result


async def run_inspection(
    session: AsyncSession, *, reset: bool = False, include_articles: bool = False
) -> RunSummary:
    """Run one zero-token inspection pass and return its recorded stages."""
    runner = InspectionRunner(session)
    return await runner.run(reset=reset, include_articles=include_articles)
