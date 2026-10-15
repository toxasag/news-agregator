"""Tests for the content & media orchestrator.

The worker is the one module that composes all seven Package 3 services, so these
tests are about *wiring and policy* rather than about the services themselves,
which have their own suites. What is asserted here: that a cluster becomes an
`Article` at the right status, that text failures abort while media failures do
not, that generation is never paid for twice, and that the strictest media
strategy across a cluster's sources wins.

Every collaborator is patched at the worker's own module boundary. That is
deliberate: patching the services in place would test `unittest.mock` rather than
the orchestration, and the real HTTP clients are already covered elsewhere. The
database is a purpose-built session double — the shared `FakeSession` in
`conftest.py` implements only what the deduplicator calls, and widening it for
`scalars`/`commit`/`refresh` would couple two unrelated suites.
"""

# Fixtures are Russian news copy, so Cyrillic letters that look like Latin ones
# are the subject matter.
# ruff: noqa: RUF001

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final, cast

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.cluster import NewsCluster
from app.models.enums import ArticleStatus, MediaStrategy, RiskLevel, SourceType
from app.models.raw_news import RawNews
from app.models.source import Source
from app.services.content_generator import GenerationRejectedError
from app.services.image_generator import ImageGenerationError
from app.services.model_pool import Attempt, PoolResult
from app.services.moderator import ModerationVerdict
from app.services.vk_video_service import UploadedVideo, VKAuthError
from app.services.wp_media_service import UploadedMedia, WPAuthError
from app.workers import content_worker
from app.workers.content_worker import (
    ArticleOutcome,
    ContentRunReport,
    cluster_lock_key,
    process_cluster,
)

CLUSTER_TITLE: Final[str] = "Открыта станция метро «Пыхтино»"

SOURCE_TEXT: Final[str] = (
    "Мэрия Москвы открыла станцию метро «Пыхтино» на Солнцевской линии 14 марта "
    "2026 года. Строительство обошлось бюджету в 23 млрд рублей. Ожидается около "
    "45 тысяч пассажиров в сутки, интервал движения — 90 секунд."
)

GENERATED_BODY: Final[str] = (
    "<h2>Что открыли</h2>"
    "<p>Мэрия Москвы открыла станцию «Пыхтино» 14 марта 2026 года. Стоимость "
    "строительства составила 23 млрд рублей.</p>"
    "<ul><li>45 тысяч пассажиров в сутки</li><li>интервал 90 секунд</li></ul>"
    "<h2>Как строили</h2>"
    "<p>Работы велись четыре года, на площадке было занято до 1200 рабочих.</p>"
)

DRAFT: Final[dict[str, Any]] = {
    "title": "В Москве открыли станцию метро «Пыхтино»",
    "content_html": GENERATED_BODY,
    "seo_title": "Станция «Пыхтино»: открытие на Солнцевской линии",
    "seo_description": "Мэрия открыла станцию «Пыхтино»: 23 млрд рублей, 45 тысяч.",
    "seo_keywords": ["станция пыхтино", "метро москвы"],
    "category_slug": "novosti-obshhestva",
    "image_prompt": "A modern metro station platform at midday, wide editorial shot",
    "attempts": 1,
    "slop_cleaned": False,
    "slop_notes": [],
}

def _make_test_png() -> bytes:
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    np.random.seed(42)
    img = np.random.randint(60, 200, (800, 1200, 3), dtype=np.uint8)
    cv2.rectangle(img, (100, 100), (600, 600), (220, 100, 50), -1)
    cv2.putText(
        img,
        "Editorial News Photo Test",
        (50, 400),
        cv2.FONT_HERSHEY_SIMPLEX,
        2,
        (255, 255, 255),
        3,
    )
    _, buf = cv2.imencode(".png", img)
    return bytes(buf)


PNG_BYTES: Final[bytes] = _make_test_png()
MP4_BYTES: Final[bytes] = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 256


# ============================================================ session double


class _Scalars:
    """The `.scalars()` slice of a SQLAlchemy result."""

    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def all(self) -> list[Any]:
        return list(self._rows)


class _Result:
    """The slice of `sqlalchemy.Result` the worker consumes."""

    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)

    def scalar_one_or_none(self) -> Any:
        """Return the single scalar, or ``None`` for an unscripted query.

        The worker now resolves its writer model from `system_settings` before
        generating, so this answers that lookup. ``None`` means "no override
        stored", which makes the model fall back to the environment — the state a
        deployment is in until an operator edits the matrix.
        """
        if not self._rows:
            return None
        first = self._rows[0]
        return first[0] if isinstance(first, tuple) else first


class FakeSession:
    """In-memory stand-in for `AsyncSession`, no PostgreSQL required.

    `execute` answers from a queue of result sets so one test can script the
    cluster query and the post query independently. `commit` can be made to fail,
    which is how the rollback path is exercised without a database.
    """

    def __init__(self, results: Sequence[Sequence[Any]] = ()) -> None:
        self._results: list[Sequence[Any]] = list(results)
        self.added: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.fail_commit: Exception | None = None

    def queue(self, rows: Sequence[Any]) -> None:
        """Append one result set for a later `execute`."""
        self._results.append(rows)

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def execute(self, statement: Any) -> _Result:
        rows = self._results.pop(0) if self._results else []
        return _Result(rows)

    async def commit(self) -> None:
        if self.fail_commit is not None:
            raise self.fail_commit
        self.commits += 1
        # A real commit assigns the primary key; the worker reads it afterwards.
        for instance in self.added:
            if isinstance(instance, Article) and instance.id is None:
                instance.id = 900 + self.commits

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, instance: Any, attrs: Sequence[str] | None = None) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    def as_session(self) -> AsyncSession:
        """Return `self` typed as `AsyncSession` for the worker under test."""
        return cast(AsyncSession, self)

    @property
    def article(self) -> Article:
        """The single `Article` added, failing loudly when there is not exactly one."""
        articles = [item for item in self.added if isinstance(item, Article)]
        assert len(articles) == 1, f"expected one Article, got {len(articles)}"
        return articles[0]


# =============================================================== ORM fixtures


def build_source(strategy: MediaStrategy, *, source_id: int = 1) -> Source:
    """Build a transient `Source` carrying one media strategy.

    A real ORM object rather than a stub: assigning to `RawNews.source` fires
    SQLAlchemy's backref event, which requires a mapped instance. Nothing is
    persisted, so a transient object is enough.
    """
    return Source(
        id=source_id,
        source_type=SourceType.WEBSITE,
        name=f"source-{source_id}",
        identifier=f"https://example.ru/{source_id}",
        media_strategy=strategy,
    )


def build_cluster(
    *, title: str = CLUSTER_TITLE, category: str = "novosti-obshhestva"
) -> NewsCluster:
    """Build a transient cluster with extracted facts."""
    return NewsCluster(
        id=uuid.uuid4(),
        title=title,
        category=category,
        enrichment_data={
            "facts": ["date:2026-03-14", "money:23000000000:RUB", "num:45000"]
        },
        embedding=None,
    )


def build_post(
    *,
    text: str = SOURCE_TEXT,
    media: Sequence[Mapping[str, Any]] = (),
    strategy: MediaStrategy = MediaStrategy.OCR_CHECK,
    post_id: int = 1,
) -> RawNews:
    """Build a transient source post with optional media descriptors."""
    post = RawNews(
        id=post_id,
        source_id=1,
        external_post_id=str(post_id),
        post_url=f"https://example.ru/news/{post_id}",
        raw_text=text,
        media_data=[dict(entry) for entry in media],
    )
    # `source` is a lazy="selectin" relationship; assigning a transient Source
    # avoids a database round trip while giving `_media_strategy` what it reads.
    post.source = build_source(strategy, source_id=post_id)
    return post


def photo(url: str = "https://cdn.example.ru/photo.jpg") -> dict[str, Any]:
    return {"type": "photo", "media_id": url, "original_post_id": None, "size": None}


def video(url: str = "https://cdn.example.ru/clip.mp4") -> dict[str, Any]:
    return {"type": "video", "media_id": url, "original_post_id": None, "size": None}


# ================================================================== patching


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, list[Any]]]:
    """Replace every collaborator with a recording stub.

    Returns a call log keyed by step, so a test asserts *which* stages ran rather
    than only inspecting the persisted row. Each stub can be overridden per test
    by patching again on top.
    """
    calls: dict[str, list[Any]] = {
        "generate": [],
        "review": [],
        "embed": [],
        "detect": [],
        "crop": [],
        "flux": [],
        "wp": [],
        "vk": [],
        "fetch": [],
    }

    async def fake_generate(
        payload: Mapping[str, Any], category: str, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        calls["generate"].append((dict(payload), category))
        return dict(DRAFT)

    async def fake_review(
        sources: str, draft: Mapping[str, Any], *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        calls["review"].append((sources, dict(draft)))
        reviewed = dict(draft)
        reviewed.update(
            {
                "qa_fixed": False,
                "qa_attempts": 0,
                "qa_issues": [],
                "qa_ok": True,
                "qa_remaining_issues": [],
                "qa_dropped_facts": [],
                "qa_invented_facts": [],
            }
        )
        return reviewed

    async def fake_embed(text: str) -> list[float]:
        calls["embed"].append(text)
        return [0.01] * 1536

    async def fake_detect(image_bytes: bytes) -> dict[str, Any]:
        calls["detect"].append(len(image_bytes))
        return {
            "has_watermark": False,
            "coverage_ratio": 0.0,
            "detected_text": [],
            "needs_ai_generation": False,
            "is_croppable": False,
            "critical_text": [],
            "width": 1344,
            "height": 768,
            "ocr_available": True,
            "zones": [],
            "warnings": [],
        }

    async def fake_crop(image_bytes: bytes) -> bytes:
        calls["crop"].append(len(image_bytes))
        return b"cropped" + image_bytes

    async def fake_cover(*args: Any, **kwargs: Any) -> bytes:
        if len(args) >= 2:
            prompt = args[1]
        elif len(args) == 1:
            prompt = args[0]
        else:
            prompt = kwargs.get("prompt", "")
        calls["flux"].append(prompt)
        return b"generated-cover"

    async def fake_wp(image_bytes: bytes, **kwargs: Any) -> UploadedMedia:
        calls["wp"].append({"size": len(image_bytes), **kwargs})
        return UploadedMedia(
            media_id=4242,
            source_url="https://wp.test/uploads/cover.jpg",
            thumbnail_url="https://wp.test/uploads/cover-300.jpg",
            slug="cover",
            alt_text=str(kwargs.get("article_title") or ""),
            mime_type="image/jpeg",
            width=1344,
            height=768,
        )

    async def fake_vk(video_bytes: bytes, **kwargs: Any) -> UploadedVideo:
        calls["vk"].append({"size": len(video_bytes), **kwargs})
        return UploadedVideo(
            owner_id=-222333,
            video_id=777,
            access_key="ak",
            embed_url="https://vk.com/video_ext.php?oid=-222333&id=777",
            embed_html='<figure class="svodka-video"><iframe src="x"></iframe></figure>',
            title=str(kwargs.get("name") or ""),
            size_bytes=len(video_bytes),
        )

    async def fake_fetch(client: Any, url: str, *, limit: int) -> tuple[bytes, str]:
        calls["fetch"].append(url)
        if url.endswith(".mp4"):
            return MP4_BYTES, "video/mp4"
        return PNG_BYTES, "image/png"

    async def fake_moderate(text: str, chain: Any, **kwargs: Any) -> Any:
        """A green verdict routed as if the primary model served it.

        Stubbed because moderation now runs first, through the fallback pool, and
        calls the gateway. These tests are about what happens *after* a cluster is
        cleared for writing; the pool itself is covered in `test_model_pool.py`.
        """
        verdict = ModerationVerdict(
            category="novosti-obshhestva",
            risk_level=RiskLevel.GREEN,
            reason="тестовый вердикт",
        )
        return verdict, PoolResult(
            value=verdict,
            model="test/model",
            attempts=[Attempt(model="test/model", ok=True, duration_ms=1)],
        )

    monkeypatch.setattr(content_worker, "moderate_with_fallback", fake_moderate)

    async def fake_fallback(role: str, chain: Any, call: Any, **kwargs: Any) -> Any:
        """Run the first model in the chain, bypassing cooldown bookkeeping."""
        value = await call("test/model")
        return PoolResult(
            value=value,
            model="test/model",
            attempts=[Attempt(model="test/model", ok=True, duration_ms=1)],
        )

    monkeypatch.setattr(content_worker, "run_with_fallback", fake_fallback)
    monkeypatch.setattr(content_worker, "generate_article", fake_generate)
    monkeypatch.setattr(content_worker, "review_and_fix_article", fake_review)
    monkeypatch.setattr(content_worker, "get_text_embedding", fake_embed)
    monkeypatch.setattr(content_worker, "detect_watermarks", fake_detect)
    monkeypatch.setattr(content_worker, "crop_watermarks_async", fake_crop)
    monkeypatch.setattr(content_worker, "generate_cover_image", fake_cover)
    monkeypatch.setattr(content_worker, "upload_article_image", fake_wp)
    monkeypatch.setattr(content_worker, "upload_article_video", fake_vk)
    monkeypatch.setattr(content_worker, "_fetch_bytes", fake_fetch)
    yield calls


def _no_client() -> Any:
    """A placeholder HTTP client: every fetch is stubbed, so it is never used."""
    return cast(Any, object())


async def run_one(
    session: FakeSession, cluster: NewsCluster, posts: Sequence[RawNews]
) -> ArticleOutcome:
    """Process one cluster with its posts already scripted into the session."""
    session.queue(list(posts))
    return await process_cluster(
        session.as_session(), cluster, http_client=_no_client()
    )


# ============================================================= the happy path


async def test_a_cluster_becomes_an_article(wired: dict[str, list[Any]]) -> None:
    session = FakeSession()
    outcome = await run_one(session, build_cluster(), [build_post()])

    assert outcome.status == "created"
    assert outcome.article_id == 901
    assert session.commits == 1

    article = session.article
    assert article.title == DRAFT["title"]
    assert article.content_html == GENERATED_BODY
    assert article.seo_keywords == DRAFT["seo_keywords"]
    assert article.category == "novosti-obshhestva"


async def test_the_article_is_saved_at_media_ready(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    await run_one(session, build_cluster(), [build_post()])

    # `ArticleStatus` has no READY_TO_PUBLISH member: per docs/01 the state after
    # media processing is MEDIA_READY, and Package 4's publisher moves it on.
    assert session.article.status is ArticleStatus.MEDIA_READY


async def test_every_pipeline_stage_runs_in_order(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    await run_one(session, build_cluster(), [build_post(media=[photo()])])

    assert len(wired["generate"]) == 1
    assert len(wired["review"]) == 1
    assert len(wired["embed"]) == 1
    assert len(wired["detect"]) == 1
    assert len(wired["wp"]) == 1


async def test_the_generator_receives_the_cluster_facts_and_sources(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    cluster = build_cluster()
    await run_one(session, cluster, [build_post()])

    payload, category = wired["generate"][0]

    assert payload["title"] == CLUSTER_TITLE
    assert "money:23000000000:RUB" in payload["facts"]
    assert payload["sources"][0]["url"].startswith("https://example.ru/news/")
    # The editorial voice comes from the cluster's own category.
    assert category == cluster.category


async def test_the_qa_stage_receives_the_source_text(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    await run_one(session, build_cluster(), [build_post()])

    sources, draft = wired["review"][0]

    # The fixer compares the draft's figures against the sources, so it needs the
    # raw post text rather than the cluster title.
    assert "23 млрд рублей" in sources
    assert draft["content_html"] == GENERATED_BODY


async def test_the_embedding_is_persisted(wired: dict[str, list[Any]]) -> None:
    session = FakeSession()
    outcome = await run_one(session, build_cluster(), [build_post()])

    assert outcome.embedded is True
    assert session.article.final_embedding is not None
    assert len(session.article.final_embedding) == 1536


async def test_the_slug_is_transliterated_and_unique(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    cluster = build_cluster()
    outcome = await run_one(session, cluster, [build_post()])

    # `url_slug` is unique, and two clusters about one event produce near-identical
    # titles, so the cluster id discriminates.
    assert outcome.slug.startswith("v-moskve-otkryli-stantsiyu-metro-pyhtino")
    assert outcome.slug.endswith(cluster.id.hex[:8])


async def test_a_cluster_without_posts_is_skipped(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    outcome = await run_one(session, build_cluster(), [])

    assert outcome.status == "skipped"
    assert outcome.error is not None
    # Nothing was generated, so nothing was paid for.
    assert wired["generate"] == []
    assert session.commits == 0


# ================================================== media: source vs generated


async def test_a_clean_source_photo_is_used_as_the_cover(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    outcome = await run_one(session, build_cluster(), [build_post(media=[photo()])])

    # A real photograph of the event beats a synthesised illustration of it.
    assert outcome.image_source == "source"
    assert wired["flux"] == []
    assert outcome.wp_media_id == 4242


async def test_a_lightly_branded_photo_is_cropped(
    monkeypatch: pytest.MonkeyPatch, wired: dict[str, list[Any]]
) -> None:
    async def branded(image_bytes: bytes) -> dict[str, Any]:
        return {
            "has_watermark": True,
            "coverage_ratio": 0.07,
            "detected_text": [],
            "needs_ai_generation": False,
            "is_croppable": True,
            "critical_text": [],
            "width": 1344,
            "height": 768,
            "ocr_available": True,
            "zones": [],
            "warnings": [],
        }

    monkeypatch.setattr(content_worker, "detect_watermarks", branded)
    session = FakeSession()
    outcome = await run_one(session, build_cluster(), [build_post(media=[photo()])])

    assert outcome.image_source == "cropped"
    assert len(wired["crop"]) == 1
    assert wired["flux"] == []


async def test_a_heavily_branded_photo_triggers_generation(
    monkeypatch: pytest.MonkeyPatch, wired: dict[str, list[Any]]
) -> None:
    async def branded(image_bytes: bytes) -> dict[str, Any]:
        return {
            "has_watermark": True,
            "coverage_ratio": 0.31,
            "detected_text": ["@rival_channel"],
            "needs_ai_generation": True,
            "is_croppable": False,
            "critical_text": ["@rival_channel"],
            "width": 1344,
            "height": 768,
            "ocr_available": True,
            "zones": [],
            "warnings": [],
        }

    monkeypatch.setattr(content_worker, "detect_watermarks", branded)
    session = FakeSession()
    outcome = await run_one(session, build_cluster(), [build_post(media=[photo()])])

    # Republishing a competitor's watermark is what this whole path exists to
    # prevent.
    assert outcome.image_source == "generated"
    assert wired["flux"] == [DRAFT["image_prompt"]]
    assert outcome.warnings


async def test_a_cluster_without_photos_generates_a_cover(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    outcome = await run_one(session, build_cluster(), [build_post()])

    assert outcome.image_source == "generated"
    assert len(wired["flux"]) == 1


async def test_the_cover_upload_carries_the_seo_metadata(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    await run_one(session, build_cluster(), [build_post()])

    upload = wired["wp"][0]

    assert upload["article_title"] == DRAFT["title"]
    assert upload["keywords"] == DRAFT["seo_keywords"]
    assert "СводкаПресс (нейросеть)" in upload["caption"]


async def test_all_unique_usable_photos_are_processed(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    post = build_post(media=[photo("https://cdn.example.ru/a.jpg"), photo("https://cdn.example.ru/b.jpg")])
    await run_one(session, build_cluster(), [post])

    # All unique usable photos (up to MAX_SOURCE_IMAGES) are processed and uploaded
    assert len(wired["detect"]) == 2
    assert len(wired["wp"]) == 2


# ============================================================ media strategy


async def test_text_only_sources_skip_media_entirely(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    post = build_post(media=[photo()], strategy=MediaStrategy.TEXT_ONLY)
    outcome = await run_one(session, build_cluster(), [post])

    assert outcome.image_source == "skipped"
    assert wired["detect"] == []
    assert wired["flux"] == []
    assert wired["wp"] == []


async def test_always_regenerate_skips_the_source_photo(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    post = build_post(media=[photo()], strategy=MediaStrategy.ALWAYS_REGENERATE)
    outcome = await run_one(session, build_cluster(), [post])

    assert outcome.image_source == "generated"
    # The photo is never even analysed: the source is known to be unusable.
    assert wired["detect"] == []
    assert len(wired["flux"]) == 1


async def test_the_strictest_strategy_across_sources_wins(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    posts = [
        build_post(post_id=1, media=[photo()], strategy=MediaStrategy.ORIGINAL),
        build_post(post_id=2, strategy=MediaStrategy.TEXT_ONLY),
    ]
    outcome = await run_one(session, build_cluster(), posts)

    # Honouring the most permissive setting would leak exactly the media an
    # operator disabled on the other source.
    assert outcome.image_source == "skipped"


# ================================================================= VK video


async def test_an_attached_video_is_hosted_and_embedded(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    post = build_post(media=[video()])
    outcome = await run_one(session, build_cluster(), [post])

    assert outcome.video_uploaded is True
    assert len(wired["vk"]) == 1
    assert "svodka-video" in session.article.content_html
    assert session.article.video_links[0]["video_id"] == 777


async def test_the_player_is_injected_after_the_first_paragraph(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    await run_one(session, build_cluster(), [build_post(media=[video()])])

    body = session.article.content_html
    first_close = body.index("</p>")

    # After the lead, so the reader sees what the story is before the player.
    assert body.index("svodka-video") > first_close
    assert body.index("svodka-video") < body.index("<h2>Как строили</h2>")


async def test_a_cluster_without_video_touches_no_vk_api(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    outcome = await run_one(session, build_cluster(), [build_post()])

    assert outcome.video_uploaded is False
    assert wired["vk"] == []
    assert session.article.video_links == []


async def test_a_telegram_file_id_video_is_skipped(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    # Telegram media store a file id, not a URL; the bridge owns that download.
    post = build_post(media=[{"type": "video", "media_id": "BAADAgAD", "size": None}])
    outcome = await run_one(session, build_cluster(), [post])

    assert outcome.video_uploaded is False
    assert wired["vk"] == []
    assert any("not fetchable" in warning for warning in outcome.warnings)


# ====================================================== failure containment


async def test_a_generation_failure_aborts_the_article(
    monkeypatch: pytest.MonkeyPatch, wired: dict[str, list[Any]]
) -> None:
    async def rejected(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise GenerationRejectedError("no publishable draft", attempts=2, reasons=["x"])

    monkeypatch.setattr(content_worker, "generate_article", rejected)
    session = FakeSession()
    outcome = await run_one(session, build_cluster(), [build_post()])

    # Without text there is nothing to publish.
    assert outcome.status == "failed"
    assert outcome.error is not None
    assert session.commits == 0
    assert session.added == []


async def test_an_embedding_failure_still_saves_the_article(
    monkeypatch: pytest.MonkeyPatch, wired: dict[str, list[Any]]
) -> None:
    async def failing(text: str) -> list[float]:
        raise content_worker.EmbeddingError("no vector")

    monkeypatch.setattr(content_worker, "get_text_embedding", failing)
    session = FakeSession()
    outcome = await run_one(session, build_cluster(), [build_post()])

    # An article without an embedding is publishable; it only misses semantic
    # search until a backfill runs.
    assert outcome.status == "created"
    assert outcome.embedded is False
    assert session.article.final_embedding is None
    assert outcome.warnings


async def test_a_cover_generation_failure_still_saves_the_article(
    monkeypatch: pytest.MonkeyPatch, wired: dict[str, list[Any]]
) -> None:
    async def failing(*args: Any, **kwargs: Any) -> bytes:
        raise ImageGenerationError("fal is down")

    monkeypatch.setattr(content_worker, "generate_cover_image", failing)
    session = FakeSession()
    outcome = await run_one(session, build_cluster(), [build_post()])

    # WordPress renders a post without a featured image; losing the article to a
    # CDN outage would be the wrong trade.
    assert outcome.status == "created"
    assert outcome.image_source == "none"
    assert outcome.wp_media_id is None
    assert session.article.wp_image_id is None


async def test_a_wordpress_failure_still_saves_the_article(
    monkeypatch: pytest.MonkeyPatch, wired: dict[str, list[Any]]
) -> None:
    async def failing(image_bytes: bytes, **kwargs: Any) -> UploadedMedia:
        raise WPAuthError("bad application password")

    monkeypatch.setattr(content_worker, "upload_article_image", failing)
    session = FakeSession()
    outcome = await run_one(session, build_cluster(), [build_post()])

    assert outcome.status == "created"
    assert outcome.wp_media_id is None
    assert any("WordPress" in warning for warning in outcome.warnings)


async def test_a_vk_failure_still_saves_the_article(
    monkeypatch: pytest.MonkeyPatch, wired: dict[str, list[Any]]
) -> None:
    async def failing(video_bytes: bytes, **kwargs: Any) -> UploadedVideo:
        raise VKAuthError("token revoked")

    monkeypatch.setattr(content_worker, "upload_article_video", failing)
    session = FakeSession()
    outcome = await run_one(session, build_cluster(), [build_post(media=[video()])])

    assert outcome.status == "created"
    assert outcome.video_uploaded is False
    assert session.article.video_links == []
    # The body is stored without a player rather than with a dangling reference.
    assert "svodka-video" not in session.article.content_html


async def test_media_warnings_are_recorded_on_the_row(
    monkeypatch: pytest.MonkeyPatch, wired: dict[str, list[Any]]
) -> None:
    async def failing(*args: Any, **kwargs: Any) -> bytes:
        raise ImageGenerationError("fal is down")

    monkeypatch.setattr(content_worker, "generate_cover_image", failing)
    session = FakeSession()
    await run_one(session, build_cluster(), [build_post()])

    # An operator reviewing a coverless article needs to know why.
    assert session.article.error_message
    assert "fal is down" in session.article.error_message


async def test_a_commit_failure_is_rolled_back(
    wired: dict[str, list[Any]],
) -> None:
    session = FakeSession()
    session.fail_commit = SQLAlchemyError("unique violation on url_slug")
    outcome = await run_one(session, build_cluster(), [build_post()])

    assert outcome.status == "failed"
    assert session.rollbacks == 1
    assert outcome.article_id is None


# ================================================================ reporting


def test_the_lock_key_is_scoped_per_cluster() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()

    assert cluster_lock_key(first) != cluster_lock_key(second)
    assert str(first) in cluster_lock_key(first)


def test_the_report_counts_outcomes_by_status() -> None:
    report = ContentRunReport(
        outcomes=[
            ArticleOutcome(cluster_id=uuid.uuid4(), category="a", status="created"),
            ArticleOutcome(cluster_id=uuid.uuid4(), category="b", status="created"),
            ArticleOutcome(cluster_id=uuid.uuid4(), category="c", status="failed"),
            ArticleOutcome(cluster_id=uuid.uuid4(), category="d", status="skipped"),
        ]
    )

    assert report.created == 2
    assert report.failed == 1
    payload = report.to_dict()
    assert payload["clusters"] == 4
    assert len(payload["outcomes"]) == 4


def test_an_outcome_serialises_for_arq() -> None:
    outcome = ArticleOutcome(cluster_id=uuid.uuid4(), category="novosti-politiki")
    outcome.warn("something worth knowing")

    payload = outcome.to_dict()

    # ARQ persists the job result as JSON, so a UUID must already be a string.
    assert isinstance(payload["cluster_id"], str)
    assert payload["warnings"] == ["something worth knowing"]


def test_video_injection_appends_when_there_is_no_paragraph() -> None:
    injected = content_worker._inject_video("<h2>Заголовок</h2>", "<iframe/>")

    assert injected.endswith("<iframe/>")


def test_video_injection_lands_after_the_first_paragraph() -> None:
    injected = content_worker._inject_video(
        "<p>Первый</p><p>Второй</p>", "<iframe/>"
    )

    assert injected == "<p>Первый</p><iframe/><p>Второй</p>"


def test_title_stems_extraction_and_event_lock_key() -> None:
    sample = "Учения Центр-2026: масштаб и дроны меняют правила"
    stems = content_worker._extract_title_stems(sample)
    assert "учен" in stems
    assert "центр" in stems
    assert "2026" in stems

    lock_key = content_worker._make_event_lock_key(sample)
    assert lock_key.startswith("dedup:generating_event:")
    assert "центр" in lock_key


async def test_find_duplicate_article_lexical_stem_match() -> None:
    from datetime import UTC, datetime  # noqa: PLC0415

    art = Article(
        id=101,
        title="Учения Центр-2026: масштаб и дроны",
        content_html="<p>Текст</p>",
        created_at=datetime.now(UTC),
    )
    session = FakeSession([(art,)])

    cluster = build_cluster(title="Учения Центр-2026: маневры армии")
    post = build_post()

    dup_art, reason, is_followup = await content_worker._find_duplicate_article(
        session.as_session(), cluster, [post]
    )
    assert dup_art is not None
    assert dup_art.id == 101
    assert "lexical stem match" in reason
    # Since created_at is recent (< 2 hours), is_followup must be False
    assert is_followup is False


async def test_find_duplicate_article_followup_topic_evolution() -> None:
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    # Article published 3 hours ago
    old_time = datetime.now(UTC) - timedelta(hours=3)
    art = Article(
        id=202,
        title="Учения Центр-2026: масштаб и дроны",
        content_html="<p>Текст</p>",
        created_at=old_time,
        published_at=old_time,
        url_slug="ucheniya-tsentr-2026-masshtab-i-drony-1234",
        category="armiya-i-opk",
    )
    session = FakeSession([(art,)])

    # New cluster with substantial new developments (consequences, statistics)
    cluster = build_cluster(
        title="Итоги учений Центр-2026: разгром условного противника и заявления",
        category="armiya-i-opk",
    )
    post = build_post(
        text="Генштаб подвел официальные итоги масштабных маневров Центр-2026 в Сибири."
    )

    dup_art, _reason, is_followup = await content_worker._find_duplicate_article(
        session.as_session(), cluster, [post]
    )
    assert dup_art is not None
    assert dup_art.id == 202
    assert is_followup is True


async def test_enrich_and_decorate_draft_injects_followup_backlink() -> None:
    cluster = build_cluster(
        title="Развитие ситуации в Омске: новые последствия",
        category="proisshestviya",
    )
    cluster.enrichment_data = {
        "parent_article": {
            "id": 55,
            "title": "Взрыв на заводе в Омске: первые подробности",
            "url_slug": "vzryv-na-zavode-v-omske-pervye-podrobnosti-abcd",
            "category": "proisshestviya",
        }
    }
    reviewed = {
        "title": "Развитие ситуации в Омске: новые последствия",
        "content_html": "<p>Спасатели локализовали возгорание на объекте.</p>",
        "category_slug": "proisshestviya",
    }
    session = FakeSession([])

    await content_worker._enrich_and_decorate_draft(
        session.as_session(), cluster, [build_post()], reviewed
    )

    content = reviewed["content_html"]
    assert "svodka-followup" in content
    assert "Ранее по теме:" in content
    expected_link = (
        "https://svodka.press/proisshestviya/"
        "vzryv-na-zavode-v-omske-pervye-podrobnosti-abcd/"
    )
    assert expected_link in content
    assert "Взрыв на заводе в Омске: первые подробности" in content

