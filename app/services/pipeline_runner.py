"""Observable pipeline runs: the same stages, with the reasoning made visible.

This is the engine behind the Лаборатория page. It walks one news item through
the pipeline and records, per stage, what went in, which model was asked, what
came back and why the stage decided what it did.

Two guarantees make it safe to press the button on a live system:

* **Test runs never publish and never write an article.** Collection and
  deduplication do touch the database, because that is the data itself and
  re-collecting the same posts is idempotent by the ``(source_id,
  external_post_id)`` constraint. Everything downstream is computed and shown,
  not persisted.
* **Nothing here duplicates pipeline logic.** Each stage calls the same service
  the production worker calls, so the run shows how the system actually behaves
  rather than how a parallel implementation behaves. A test-only copy of the
  dedup ladder would eventually disagree with the real one, and the operator
  would be inspecting a lie.
"""

from __future__ import annotations

# Operator-facing summaries are Russian, matching the dashboard.
# ruff: noqa: RUF001
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.cluster import NewsCluster
from app.models.enums import DedupStatus, SourceType
from app.models.raw_news import RawNews
from app.models.source import Source
from app.services.categories import category_title
from app.services.category_prompts import load_prompt_overrides
from app.services.content_generator import (
    ContentGenerationError,
    generate_article,
)
from app.services.content_prompts import build_system_prompt
from app.services.model_routing import resolve_model
from app.services.moderator import (
    ModerationError,
    ModerationVerdict,
    build_moderation_prompt,
    moderate_cluster,
)
from app.services.pipeline_stages import (
    ArtifactStore,
    RunMode,
    RunSummary,
    StageName,
    StageResult,
    describe_media,
    new_run_id,
    preview,
)
from app.services.research import (
    ARCHIVE_LOOKBACK,
    MAX_INTERLINKS,
    collect_facts,
    find_related_articles,
)
from app.services.social_base import build_hashtags
from app.services.social_dzen import build_dzen_html
from app.services.social_telegram import (
    MAX_CAPTION_LENGTH as TELEGRAM_CAPTION_LIMIT,
)
from app.services.social_telegram import build_caption, caption_length
from app.services.social_vk import build_wall_message
from app.workers.ingestion_worker import IngestionRunReport, run_ingestion

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = ["PipelineRunner", "run_test_pipeline"]




@dataclass(slots=True)
class _Stopwatch:
    """Millisecond timer for one stage."""

    started: float

    @classmethod
    def start(cls) -> _Stopwatch:
        return cls(started=time.monotonic())

    def stop(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


class PipelineRunner:
    """Runs the pipeline for one item, recording every stage.

    Constructed per run. ``mode`` decides whether the article is persisted, which
    is the only behavioural difference between the Лаборатория button and cron.
    """

    __slots__ = ("_index", "_mode", "_session", "_store", "_summary")

    def __init__(
        self,
        session: AsyncSession,
        *,
        mode: RunMode = RunMode.TEST,
        store: ArtifactStore | None = None,
    ) -> None:
        self._session = session
        self._mode = mode
        self._store = store or ArtifactStore()
        self._summary = RunSummary(
            run_id=new_run_id(), mode=mode, started_at=datetime.now(UTC)
        )
        self._index = 0

    @property
    def summary(self) -> RunSummary:
        return self._summary

    async def _overrides(self) -> dict[str, Any]:
        """Load dashboard-edited rubric prompts for this run."""
        return await load_prompt_overrides(self._session)

    async def _log_usage(
        self,
        *,
        role: str,
        model: str,
        usage: dict[str, Any],
        cluster_id: str | None = None,
        article_id: int | None = None,
    ) -> None:
        """Persist one paid model call to the usage ledger."""
        from app.services.usage_log import record_usage  # noqa: PLC0415

        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        cost = usage.get("cost")
        cost_usd = float(cost) if cost is not None else None
        await record_usage(
            self._session,
            role=role,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            provider_name=usage.get("provider_name"),
            latency_ms=usage.get("latency_ms"),
            cluster_id=cluster_id,
            article_id=article_id,
        )

    def _record(self, result: StageResult) -> StageResult:
        """Append a stage result and persist it as an artifact."""
        self._index += 1
        self._summary.stages.append(result)
        try:
            self._store.write_stage(self._summary.run_id, self._index, result)
        except (OSError, ValueError):
            # Artifacts are for the operator's benefit; losing one must not fail
            # a run whose real work succeeded.
            logger.warning("Could not write artifact for %s", result.stage, exc_info=True)
        return result

    async def run(
        self, *, collect: bool = True, cluster_id: uuid.UUID | None = None
    ) -> RunSummary:
        """Execute the full chain and return the recorded run.

        ``collect=False`` reuses whatever is already in the database, which is how
        an operator re-runs generation for an existing cluster without pulling
        fresh posts from Telegram.

        ``cluster_id`` writes *that* cluster instead of the newest unwritten one.
        This is what the inspection timeline hands over: having read the intake, the
        operator picks the story worth paying for, rather than accepting whichever
        one happens to be newest.
        """
        try:
            if collect:
                await self._stage_collect()
            cluster, posts = await self._stage_dedup_and_cluster(cluster_id=cluster_id)
            if cluster is None:
                self._finish()
                return self._summary

            self._summary.cluster_id = str(cluster.id)
            verdict = await self._stage_moderation(cluster, posts)
            if verdict is not None and verdict.blocks_generation:
                # Spam stops here: writing it would cost a writer call, an SEO
                # pass, a cover and a WordPress upload before anyone noticed.
                self._finish()
                return self._summary
            research = await self._stage_research(cluster, posts)
            draft = await self._stage_article(cluster, posts, research)
            if draft is not None:
                await self._stage_media(posts, draft)
                # The URL the platforms link to. In a test run the article has no
                # WordPress post yet, so the slug is shown as it would resolve.
                slug = str(draft.get("url_slug") or "").strip("/")
                base = str(settings.wp_base_url).rstrip("/")
                await self._stage_platforms(
                    draft, f"{base}/{slug}" if slug else base
                )
        # Deliberately broad: the run is an operator tool, and an unexpected
        # failure must be shown on the timeline rather than swallowed into a 500.
        except Exception as error:
            logger.exception("Pipeline run %s crashed", self._summary.run_id)
            crash = StageResult(stage=StageName.ARTICLE)
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

    async def _stage_collect(self) -> StageResult:
        """M0: pull fresh posts from the configured Telegram channels."""
        result = StageResult(stage=StageName.COLLECT)
        watch = _Stopwatch.start()

        active = await self._session.execute(
            select(func.count())
            .select_from(Source)
            .where(Source.is_active.is_(True), Source.source_type == SourceType.TELEGRAM)
        )
        active_count = int(active.scalar_one())
        if not active_count:
            result.duration_ms = watch.stop()
            result.skip(
                "Нет активных Telegram-каналов: отметьте каналы на странице «Источники»."
            )
            return self._record(result)

        report: IngestionRunReport = await run_ingestion()
        totals = report.to_dict()["totals"]
        result.duration_ms = watch.stop()

        if report.lock_skipped:
            result.skip("Сбор уже идёт в боевом режиме — этот проход ничего не собрал.")
            return self._record(result)

        result.summary = (
            f"Каналов опрошено: {totals['sources']}, получено постов: "
            f"{totals['fetched']}, записано новых: {totals['inserted']}"
        )
        result.details = {
            "active_channels": active_count,
            "totals": totals,
            "sources": [
                {
                    "name": source["source_name"],
                    "fetched": source["fetched"],
                    "inserted": source["inserted"],
                    "duplicates": source["duplicates"],
                    "errors": list(source.get("errors") or ()),
                }
                for source in report.to_dict()["sources"]
            ],
        }
        for message in report.errors:
            result.warn(message)
        return self._record(result)

    async def _stage_dedup_and_cluster(
        self, *, cluster_id: uuid.UUID | None = None
    ) -> tuple[NewsCluster | None, Sequence[RawNews]]:
        """M1 and M2: show the dedup verdicts, then pick a cluster to write.

        The verdicts are read from what deduplication already persisted on each
        post (``dedup_status``, ``similarity_score``, ``dedup_level``) rather than
        re-running the ladder: re-running would produce different numbers, because
        the first run already indexed those posts.
        """
        dedup = StageResult(stage=StageName.DEDUP)
        watch = _Stopwatch.start()

        window = datetime.now(UTC) - timedelta(hours=settings.dedup_lookback_hours)
        recent = await self._session.execute(
            select(RawNews)
            .where(RawNews.created_at >= window)
            .order_by(RawNews.created_at.desc())
            .limit(60)
        )
        posts = list(recent.scalars().all())

        by_status: dict[str, int] = {}
        rows: list[dict[str, Any]] = []
        for post in posts:
            status = post.dedup_status.value
            by_status[status] = by_status.get(status, 0) + 1
            rows.append(
                {
                    "raw_news_id": post.id,
                    "text": preview(post.raw_text or "", 160),
                    "status": status,
                    # The three fields that answer "why did it decide that":
                    # which ladder level fired, how similar it was, and to which
                    # cluster it went.
                    "level": post.dedup_level,
                    "similarity": (
                        round(float(post.similarity_score), 3)
                        if post.similarity_score is not None
                        else None
                    ),
                    "cluster_id": str(post.cluster_id) if post.cluster_id else None,
                    "is_digest": post.is_digest,
                    "is_ad": post.is_ad,
                    "verdict": self._explain(post),
                }
            )

        dedup.duration_ms = watch.stop()
        dedup.summary = (
            f"Проверено постов за {settings.dedup_lookback_hours}ч: {len(posts)}. "
            + ", ".join(f"{name}: {count}" for name, count in sorted(by_status.items()))
        )
        dedup.details = {
            "window_hours": settings.dedup_lookback_hours,
            "thresholds": {
                "lower": settings.similarity_lower_threshold,
                "upper": settings.similarity_upper_threshold,
            },
            "by_status": by_status,
            "posts": rows[:30],
        }
        self._record(dedup)

        # --- M2: which cluster this run will write about.
        cluster_stage = StageResult(stage=StageName.CLUSTER)
        watch = _Stopwatch.start()
        cluster = (
            await self._pick_cluster()
            if cluster_id is None
            else await self._session.get(NewsCluster, cluster_id)
        )
        if cluster is None:
            cluster_stage.duration_ms = watch.stop()
            if cluster_id is not None:
                # Asked for by id and gone: the queue was reset, or another pass
                # wrote it between the inspection and this click. Reported as a
                # failure rather than falling back to "newest", because silently
                # writing a different story than the one chosen is worse than
                # saying nothing was written.
                cluster_stage.fail(
                    f"Кластер {cluster_id} не найден: очередь могли очистить или "
                    "статья по нему уже написана. Обновите проверку."
                )
            else:
                cluster_stage.skip(
                    "Нет необработанных кластеров: все свежие новости уже превращены "
                    "в статьи или отфильтрованы."
                )
            self._record(cluster_stage)
            return None, ()

        members = await self._session.execute(
            select(RawNews).where(RawNews.cluster_id == cluster.id).order_by(RawNews.id)
        )
        cluster_posts = list(members.scalars().all())
        media: list[dict[str, Any]] = []
        for post in cluster_posts:
            media.extend(describe_media(post.media_data or ()))

        cluster_stage.duration_ms = watch.stop()
        cluster_stage.summary = (
            f"Кластер {cluster.id}: постов {len(cluster_posts)}, "
            f"медиа {len(media)}, рубрика «{category_title(cluster.category)}»"
        )
        cluster_stage.details = {
            "cluster_id": str(cluster.id),
            "title": cluster.title,
            "category": cluster.category,
            "category_title": category_title(cluster.category),
            "risk_level": cluster.risk_level.value,
            # Whether the operator chose this cluster or the runner did. Worth
            # recording: a run replayed from artifacts should say which story was
            # picked deliberately.
            "chosen_by_operator": cluster_id is not None,
            "posts": [
                {
                    "raw_news_id": post.id,
                    "source_id": post.source_id,
                    "external_post_id": post.external_post_id,
                    "text": preview(post.raw_text or "", 300),
                    "media": describe_media(post.media_data or ()),
                }
                for post in cluster_posts
            ],
            "media_total": len(media),
        }
        self._record(cluster_stage)
        return cluster, cluster_posts

    @staticmethod
    def _explain(post: RawNews) -> str:
        """Explain one post's dedup verdict in the operator's terms."""
        if post.is_digest:
            return "Отброшен фильтром: дайджест (0 токенов)"
        if post.is_ad:
            return "Отброшен фильтром: реклама (0 токенов)"
        status = post.dedup_status
        similarity = post.similarity_score
        level = post.dedup_level
        if status is DedupStatus.SKIPPED_DUPLICATE:
            return (
                f"Дубль: сходство {similarity:.3f} на уровне {level} "
                f"(порог дубликата {settings.similarity_upper_threshold})"
                if similarity is not None
                else f"Дубль по уровню {level}"
            )
        if status is DedupStatus.UNIQUE and similarity is not None:
            return (
                f"Новая новость: сходство {similarity:.3f} ниже порога "
                f"{settings.similarity_upper_threshold}"
            )
        if status is DedupStatus.UNIQUE:
            return "Новая новость: похожих не найдено"
        return status.value

    async def _pick_cluster(self) -> NewsCluster | None:
        """Choose the newest unpublished cluster that actually has text.

        The text requirement is not cosmetic: a media-only cluster (a channel
        posting a bare video, say) cannot be moderated or written, so picking one
        produces a run where every interesting stage reports "skipped" — which is
        what happened on the first live run of the moderation stage.
        """
        result = await self._session.execute(
            select(NewsCluster)
            .join(RawNews, RawNews.cluster_id == NewsCluster.id)
            .where(
                NewsCluster.is_published.is_(False),
                func.length(func.coalesce(RawNews.raw_text, "")) > 0,
            )
            .order_by(NewsCluster.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()


    async def _stage_moderation(
        self, cluster: NewsCluster, posts: Sequence[RawNews]
    ) -> ModerationVerdict | None:
        """M2.5: classify the cluster and decide whether it may be written.

        Persists the verdict onto the cluster even in a test run: the rubric and
        the risk level are *data about the cluster*, not an artifact of the run, and
        the production pass should not re-pay for a classification the lab already
        made. The article itself is still never written in test mode.
        """
        result = StageResult(stage=StageName.MODERATION)
        watch = _Stopwatch.start()

        text = "\n\n".join(
            (post.raw_text or "").strip()
            for post in posts
            if (post.raw_text or "").strip()
        )
        if not text:
            result.duration_ms = watch.stop()
            result.skip("В кластере нет текста: модерация невозможна.")
            self._record(result)
            return None

        model = await resolve_model(self._session, "arbiter")
        result.model = model
        overrides = await self._overrides()
        result.prompt = build_moderation_prompt(overrides=overrides)
        result.tokens_spent = True

        try:
            verdict = await moderate_cluster(
                text, model_name=model, overrides=overrides
            )
        except ModerationError as error:
            result.duration_ms = watch.stop()
            result.fail(f"Модерация не удалась: {error}")
            self._record(result)
            return None

        cluster.category = verdict.category
        cluster.risk_level = verdict.risk_level
        await self._session.commit()

        result.duration_ms = watch.stop()
        result.summary = (
            f"Рубрика: «{verdict.category_title}», риск: {verdict.risk_level.value}"
            + (" — материал отброшен" if verdict.blocks_generation else "")
        )
        result.details = verdict.to_dict()
        result.details["text_preview"] = preview(text, 500)
        if verdict.overridden:
            result.warn(verdict.override_note)
        if verdict.blocks_generation:
            result.warn(
                "Генерация не запускается: реклама и самопиар отбрасываются до "
                "любых платных вызовов."
            )
        self._record(result)
        return verdict

    async def _stage_research(
        self, cluster: NewsCluster, posts: Sequence[RawNews]
    ) -> dict[str, Any]:
        """M3: enrich the cluster before writing, and show what was added.

        Deliberately token-free. For a news desk the useful research is not a web
        search but what the archive already knows: have we covered this story, and
        which facts do the source posts agree on. Both come from the database, so
        this stage costs nothing and cannot rate-limit the run.

        The two lookups live in :mod:`app.services.research` because the zero-token
        inspection pass needs exactly the same answers, and a second copy would
        eventually disagree with this one.
        """
        result = StageResult(stage=StageName.RESEARCH)
        watch = _Stopwatch.start()

        facts = collect_facts(cluster, posts)
        related = await find_related_articles(self._session, cluster)

        result.duration_ms = watch.stop()
        result.summary = (
            f"Фактов собрано: {len(facts)}, похожих прошлых материалов: {len(related)}"
        )
        result.details = {
            "facts": facts,
            "related_articles": related,
            "interlink_candidates": related[:MAX_INTERLINKS],
            "note": (
                f"Ресерч работает по архиву за {ARCHIVE_LOOKBACK.days} дней и не "
                "тратит токены: источник — уже собранные посты и опубликованные "
                "материалы."
            ),
        }
        self._record(result)
        return {"facts": facts, "related": related}

    async def _stage_article(
        self,
        cluster: NewsCluster,
        posts: Sequence[RawNews],
        research: dict[str, Any],
    ) -> dict[str, Any] | None:
        """M4: write the site article, showing the prompt and the model."""
        result = StageResult(stage=StageName.ARTICLE)
        watch = _Stopwatch.start()

        # Imported here rather than at module scope: the worker module imports
        # this one for the runner, and a top-level import would close the cycle.
        from app.workers.content_worker import _build_cluster_payload  # noqa: PLC0415

        payload = _build_cluster_payload(cluster, list(posts))
        if research["facts"] and not payload.get("facts"):
            payload["facts"] = research["facts"]

        model = await resolve_model(self._session, "writer")
        result.model = model
        overrides = await self._overrides()
        result.prompt = build_system_prompt(cluster.category, overrides=overrides)
        result.tokens_spent = True

        try:
            draft = await generate_article(
                payload,
                cluster.category,
                model_name=model,
                overrides=overrides,
            )
        except ContentGenerationError as error:
            result.duration_ms = watch.stop()
            result.fail(f"Генерация отклонена: {error}")
            self._record(result)
            return None

        keywords = list(draft.get("seo_keywords") or ())
        body = str(draft.get("content_html") or "")
        related = research["related"][:MAX_INTERLINKS]
        usage = draft.get("usage")

        result.duration_ms = watch.stop()
        result.summary = (
            f"Статья готова: «{draft.get('title')}», "
            f"{len(body)} символов, теги: {len(keywords)}"
        )
        result.details = {
            "title": draft.get("title"),
            "category": cluster.category,
            "category_title": category_title(cluster.category),
            "seo_title": draft.get("seo_title"),
            "seo_description": draft.get("seo_description"),
            "seo_keywords": keywords,
            "content_html": body,
            "content_length": len(body),
            "attempts": draft.get("attempts"),
            "slop_cleaned": draft.get("slop_cleaned"),
            "slop_notes": list(draft.get("slop_notes") or ()),
            "image_prompt": draft.get("image_prompt"),
            # Interlinking is reported honestly: these are candidates found in the
            # archive, and whether they were woven into the body is a separate
            # question the UI must not blur.
            "interlink_candidates": related,
            "interlinks_embedded": [
                item for item in related if str(item["article_id"]) in body
            ],
            "user_prompt_preview": preview(str(payload.get("text") or ""), 600),
        }
        if usage is not None:
            result.details["usage"] = usage
            await self._log_usage(
                role="writer",
                model=model,
                usage=usage,
                cluster_id=str(cluster.id),
            )
        for note in draft.get("slop_notes") or ():
            result.warn(f"AI-slop: {note}")
        self._record(result)
        return draft

    async def _stage_platforms(self, draft: dict[str, Any], url: str) -> None:
        """M6-M8: show how the article is reshaped for each platform.

        Adaptation only — nothing is posted from a test run. The reshaping is the
        part an operator needs to inspect, because each platform truncates,
        strips tags and places the link differently, and a caption that looks fine
        in the article can arrive on Telegram cut mid-sentence.

        Each module reuses the production builder rather than approximating it, so
        what the timeline shows is what the publisher would send.
        """
        title = str(draft.get("title") or "")
        body = str(draft.get("content_html") or "")
        description = str(draft.get("seo_description") or "")
        keywords = [str(word) for word in (draft.get("seo_keywords") or ())]

        # --- Telegram: HTML caption bounded by UTF-16 units, hashtags, link button.
        telegram = StageResult(stage=StageName.TELEGRAM)
        watch = _Stopwatch.start()
        caption = build_caption(
            title=title,
            body_html=body,
            url=url,
            description=description,
            hashtags=build_hashtags(keywords),
        )
        telegram.duration_ms = watch.stop()
        telegram.summary = (
            f"Подпись собрана: {caption_length(caption)} из "
            f"{TELEGRAM_CAPTION_LIMIT} символов (лимит Telegram для фото)"
        )
        telegram.details = {
            "text": caption,
            "length": caption_length(caption),
            "limit": TELEGRAM_CAPTION_LIMIT,
            "hashtags": build_hashtags(keywords),
            "posting": (
                "В тестовом режиме пост не отправляется. В боевом публикуется "
                "от подключённого аккаунта."
            ),
        }
        telegram.details["body_chars"] = len(body)
        if caption_length(caption) < TELEGRAM_CAPTION_LIMIT // 4:
            # The teaser builder trims aggressively; a very short caption usually
            # means the body had little plain text (a gallery post, say), and the
            # operator should see that before it ships.
            telegram.warn(
                "Подпись получилась короткой — проверьте, что в тексте статьи "
                "достаточно обычных абзацев."
            )
        self._record(telegram)

        # --- Dzen: the tag subset Dzen accepts, published through the WebUI.
        dzen = StageResult(stage=StageName.DZEN)
        watch = _Stopwatch.start()
        dzen_html = build_dzen_html(body)
        dzen.duration_ms = watch.stop()
        dzen.summary = (
            f"HTML приведён к разметке Дзена: {len(dzen_html)} символов "
            f"(из {len(body)} исходных)"
        )
        dzen.details = {
            "html": dzen_html,
            "length": len(dzen_html),
            "source_length": len(body),
            "posting": (
                "Публикация в Дзен идёт через WebUI: в тестовом режиме черновик "
                "не создаётся."
            ),
        }
        if body and not dzen_html:
            dzen.warn("После фильтрации тегов текст оказался пустым.")
        self._record(dzen)

        # --- VK: plain-text wall post; the link travels as an attachment.
        vk = StageResult(stage=StageName.VK)
        watch = _Stopwatch.start()
        wall = build_wall_message(
            title=title,
            body_html=body,
            description=description,
            keywords=keywords,
        )
        vk.duration_ms = watch.stop()
        vk.summary = f"Текст для стены собран: {len(wall)} символов"
        vk.details = {
            "text": wall,
            "length": len(wall),
            "posting": (
                "Ссылка уходит вложением, чтобы ВК отрисовал карточку. "
                "В тестовом режиме пост не публикуется."
            ),
        }
        self._record(vk)

    async def _stage_media(
        self, posts: Sequence[RawNews], draft: dict[str, Any]
    ) -> StageResult:
        """M5: report the media plan and the watermark verdicts.

        Analysis only in test mode: nothing is downloaded, cropped or uploaded to
        WordPress. The operator asked to see whether watermarks are detected and
        whether duplicate frames were taken, and both are decisions, not bytes.
        """
        result = StageResult(stage=StageName.MEDIA)
        watch = _Stopwatch.start()

        items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        duplicates = 0
        for post in posts:
            for item in post.media_data or ():
                media_id = str(item.get("media_id") or "")
                is_duplicate = bool(media_id) and media_id in seen_ids
                if is_duplicate:
                    duplicates += 1
                else:
                    seen_ids.add(media_id)
                items.append(
                    {
                        "raw_news_id": post.id,
                        "type": str(item.get("type") or "?"),
                        "media_id": media_id,
                        "size": item.get("size"),
                        "duplicate_of_earlier": is_duplicate,
                        "skip_media_source": post.source_id,
                    }
                )

        photos = [item for item in items if item["type"] == "photo"]
        videos = [item for item in items if item["type"] == "video"]

        result.duration_ms = watch.stop()
        if not items:
            result.skip(
                "Медиа в кластере нет: обложка будет сгенерирована "
                f"по промпту «{preview(str(draft.get('image_prompt') or ''), 80)}»"
            )
            self._record(result)
            return result

        result.summary = (
            f"Медиа: фото {len(photos)}, видео {len(videos)}, "
            f"повторов отброшено {duplicates}"
        )
        result.details = {
            "items": items,
            "photos": len(photos),
            "videos": len(videos),
            "duplicates_skipped": duplicates,
            "cover_candidate": photos[0] if photos else None,
            "image_prompt": draft.get("image_prompt"),
            "watermark_check": (
                "В тестовом режиме медиа не скачивается: вотермарк-детектор "
                "запускается на боевом проходе, когда файл получен."
            ),
        }
        self._record(result)
        return result


async def run_test_pipeline(
    session: AsyncSession,
    *,
    collect: bool = True,
    cluster_id: uuid.UUID | None = None,
) -> RunSummary:
    """Run one observable test pass and return its recorded stages."""
    runner = PipelineRunner(session, mode=RunMode.TEST)
    return await runner.run(collect=collect, cluster_id=cluster_id)
