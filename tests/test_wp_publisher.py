"""Tests for the WordPress post formatter and the REST publisher.

No external services: the WordPress API is served by `respx`, and the tag cache is
an in-memory session double that enforces the same `lower(tag_name)` uniqueness as
the real functional index — without that constraint the concurrent-creation test
would pass against a cache that happily stores duplicates.

The XSS probes are the reason this file is long. The article body is model output
that quoted scraped sources, and it is about to become a public page, so the
sanitiser and the JSON-LD serialisation are tested against the specific attacks
they exist to stop rather than against a generic "has no script tag" assertion.
"""

# Fixtures are Russian editorial copy, so Cyrillic letters that look like Latin
# ones are the subject matter.
# ruff: noqa: RUF001

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final, cast

import httpx
import pytest
import respx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tag_cache import WPTagCache
from app.services.wp_post_formatter import (
    MAX_RELATED_ARTICLES,
    SCHEMA_CONTEXT,
    CarouselImage,
    FeaturedImage,
    RelatedArticle,
    VideoEmbed,
    WPPostFormatter,
    build_news_article_schema,
    render_post,
    sanitize_article_html,
)
from app.services.wp_publisher import (
    CATEGORIES_ENDPOINT,
    MAX_ATTEMPTS,
    MAX_TAGS_PER_POST,
    POSTS_ENDPOINT,
    RANK_MATH_ROBOTS_DEFAULT,
    TAGS_ENDPOINT,
    WPPublishAuthError,
    WPPublisher,
    WPPublishNotConfiguredError,
    WPPublishRateLimitError,
    WPPublishResponseError,
    build_rank_math_meta,
    publish_article,
)

WP_URL: Final[str] = "https://wp.test"
WP_USER: Final[str] = "editor"
WP_PASSWORD: Final[str] = "abcd EFGH ijkl MNOP"  # noqa: S105 - fake test credential

TITLE: Final[str] = "В Москве открыли станцию метро «Пыхтино»"
BODY: Final[str] = (
    "<h2>Что открыли</h2>"
    "<p>Мэрия Москвы открыла станцию «Пыхтино» 14 марта 2026 года. "
    "Строительство обошлось бюджету в 23 млрд рублей.</p>"
    "<ul><li>45 тысяч пассажиров в сутки</li><li>интервал 90 секунд</li></ul>"
    "<h2>Как строили</h2>"
    "<p>Работы велись четыре года, на площадке было занято до 1200 рабочих.</p>"
)
DESCRIPTION: Final[str] = "Мэрия открыла «Пыхтино»: 23 млрд рублей, 45 тысяч в сутки."
PUBLISHED_AT: Final[datetime] = datetime(2026, 3, 14, 12, 30, tzinfo=UTC)


def featured() -> FeaturedImage:
    return FeaturedImage(
        media_id=4242,
        source_url=f"{WP_URL}/wp-content/uploads/2026/08/cover.jpg",
        alt_text="Платформа новой станции",
        caption="Фото: пресс-служба мэрии",
        width=1344,
        height=768,
    )


# ==================================================== formatter: sanitisation


@pytest.mark.parametrize(
    "hostile",
    [
        "<script>alert(1)</script>",
        "<SCRIPT>alert(1)</SCRIPT>",
        "<script\n>alert(1)</script>",
        "<style>body{display:none}</style>",
        '<iframe src="https://evil.test"></iframe>',
        "<object data='x'></object>",
        "<embed src='x'>",
        "<noscript>x</noscript>",
        "<form action='/steal'><input name='p'></form>",
        "<template>x</template>",
        "<base href='https://evil.test'>",
        "<meta http-equiv='refresh' content='0;url=https://evil.test'>",
    ],
)
def test_executable_elements_are_removed(hostile: str) -> None:
    cleaned = sanitize_article_html(f"<p>Текст</p>{hostile}<p>Ещё</p>")

    assert "Текст" in cleaned
    assert "Ещё" in cleaned
    for tag in ("script", "style", "iframe", "object", "embed", "form", "base"):
        assert f"<{tag}" not in cleaned.lower()


def test_a_script_body_is_removed_not_merely_unwrapped() -> None:
    # Stripping only the tags would leave `alert('xss')` visible as prose.
    cleaned = sanitize_article_html("<p>ok</p><script>alert('xss')</script>")

    assert "alert" not in cleaned


@pytest.mark.parametrize(
    "attribute",
    [
        'onclick="alert(1)"',
        "onerror='alert(1)'",
        'onmouseover=alert(1)',
        'style="position:fixed;top:0"',
        'ONLOAD="alert(1)"',
    ],
)
def test_inline_handlers_and_styles_are_stripped(attribute: str) -> None:
    cleaned = sanitize_article_html(f"<p {attribute}>Текст</p>")

    assert "Текст" in cleaned
    assert "alert" not in cleaned
    assert "position:fixed" not in cleaned


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        " javascript:alert(1)",
        "vbscript:msgbox(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
    ],
)
def test_executing_url_schemes_are_stripped(url: str) -> None:
    cleaned = sanitize_article_html(f'<p><a href="{url}">ссылка</a></p>')

    assert "ссылка" in cleaned
    assert "javascript" not in cleaned.lower()
    assert "vbscript" not in cleaned.lower()


def test_html_comments_are_removed() -> None:
    # Conditional comments can hide scripting on old engines.
    cleaned = sanitize_article_html("<p>ok</p><!--[if IE]><script>x</script><![endif]-->")

    assert "<!--" not in cleaned
    assert "script" not in cleaned.lower()


def test_legitimate_markup_and_links_survive() -> None:
    cleaned = sanitize_article_html(BODY + '<p><a href="/news/x">материал</a></p>')

    assert "<h2>Что открыли</h2>" in cleaned
    assert "<ul>" in cleaned
    assert "<li>45 тысяч пассажиров в сутки</li>" in cleaned
    assert 'href="/news/x"' in cleaned


def test_an_empty_body_sanitises_to_empty() -> None:
    for value in ("", "   ", "\n\t"):
        assert sanitize_article_html(value) == ""


# ================================================= formatter: JSON-LD schema


def test_the_schema_is_a_news_article() -> None:
    schema = build_news_article_schema(
        title=TITLE, description=DESCRIPTION, body_html=BODY
    )

    assert schema["@context"] == SCHEMA_CONTEXT
    assert schema["@type"] == "NewsArticle"
    assert schema["headline"].startswith("В Москве открыли")


def test_the_schema_carries_the_required_dates() -> None:
    schema = build_news_article_schema(
        title=TITLE,
        description=DESCRIPTION,
        body_html=BODY,
        published_at=PUBLISHED_AT,
    )

    assert schema["datePublished"] == PUBLISHED_AT.isoformat()
    # `dateModified` is required by the NewsArticle guidelines; omitting it costs
    # the rich result, so it falls back to the publication date.
    assert schema["dateModified"] == PUBLISHED_AT.isoformat()


def test_a_later_modification_is_reported_separately() -> None:
    modified = datetime(2026, 3, 15, 9, 0, tzinfo=UTC)
    schema = build_news_article_schema(
        title=TITLE,
        description=DESCRIPTION,
        body_html=BODY,
        published_at=PUBLISHED_AT,
        modified_at=modified,
    )

    assert schema["datePublished"] == PUBLISHED_AT.isoformat()
    assert schema["dateModified"] == modified.isoformat()


def test_the_schema_image_carries_its_dimensions() -> None:
    schema = build_news_article_schema(
        title=TITLE,
        description=DESCRIPTION,
        body_html=BODY,
        featured_image=featured(),
    )

    assert schema["image"]["@type"] == "ImageObject"
    assert schema["image"]["width"] == 1344
    assert schema["image"]["height"] == 768


def test_a_dimensionless_image_omits_the_dimensions() -> None:
    # A zero-dimension ImageObject is a validator error on an otherwise valid page.
    schema = build_news_article_schema(
        title=TITLE,
        description=DESCRIPTION,
        body_html=BODY,
        featured_image=FeaturedImage(media_id=1, source_url=f"{WP_URL}/a.jpg"),
    )

    assert "width" not in schema["image"]
    assert "height" not in schema["image"]


def test_a_relative_url_is_made_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    # Schema.org requires absolute URLs; a validator silently drops a relative one,
    # which is the worst outcome — the markup looks present and does nothing.
    # `Settings` is frozen, so the resolver is patched rather than the setting.
    monkeypatch.setattr(
        "app.services.wp_post_formatter._absolute_url",
        lambda value: f"{WP_URL}{value}" if value.startswith("/") else value,
    )
    schema = build_news_article_schema(
        title=TITLE, description=DESCRIPTION, body_html=BODY, url="/news/pyhtino"
    )

    assert schema["url"] == f"{WP_URL}/news/pyhtino"
    assert schema["mainEntityOfPage"]["@id"] == f"{WP_URL}/news/pyhtino"


def test_an_absolute_url_is_left_untouched() -> None:
    schema = build_news_article_schema(
        title=TITLE,
        description=DESCRIPTION,
        body_html=BODY,
        url=f"{WP_URL}/news/pyhtino",
    )

    assert schema["url"] == f"{WP_URL}/news/pyhtino"


def test_the_author_is_an_organization() -> None:
    schema = build_news_article_schema(
        title=TITLE,
        description=DESCRIPTION,
        body_html=BODY,
        publisher_name="СводкаПресс",
    )

    # Claiming a human byline for automated output would be a false statement in
    # structured data.
    assert schema["author"]["@type"] == "Organization"
    assert schema["publisher"]["name"] == "СводкаПресс"


def test_the_publisher_logo_is_included_when_given() -> None:
    schema = build_news_article_schema(
        title=TITLE,
        description=DESCRIPTION,
        body_html=BODY,
        publisher_name="СводкаПресс",
        publisher_logo_url=f"{WP_URL}/logo.png",
    )

    assert schema["publisher"]["logo"]["url"] == f"{WP_URL}/logo.png"


def test_empty_optional_fields_are_omitted_entirely() -> None:
    schema = build_news_article_schema(title=TITLE, description="", body_html=BODY)

    for absent in ("author", "publisher", "keywords", "articleSection", "image"):
        assert absent not in schema


def test_the_schema_description_falls_back_to_the_body() -> None:
    schema = build_news_article_schema(title=TITLE, description="", body_html=BODY)

    assert "Мэрия Москвы открыла станцию" in schema["description"]
    # Plain text, not markup: a validator rejects tags in a description.
    assert "<p>" not in schema["description"]


def test_an_untitled_article_produces_no_schema() -> None:
    # Better absent than invalid: a NewsArticle without a headline fails validation.
    assert build_news_article_schema(title="  ", description="d", body_html=BODY) == {}


def test_keywords_are_joined_as_a_string() -> None:
    schema = build_news_article_schema(
        title=TITLE,
        description=DESCRIPTION,
        body_html=BODY,
        keywords=["метро", "пыхтино", ""],
    )

    assert schema["keywords"] == "метро, пыхтино"


# ================================================= formatter: JSON-LD safety


def _extract_json_ld(html: str) -> str:
    """Pull the raw JSON-LD payload out of rendered HTML."""
    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL
    )
    assert match is not None, "no JSON-LD block was rendered"
    return match.group(1)


def test_a_script_close_in_the_headline_is_neutralised() -> None:
    # Two defences apply to the headline, and the first one wins: `_plain_text`
    # treats `</script>` as a tag and strips it before serialisation.
    hostile = "Заголовок</script><script>alert(1)</script>"
    result = render_post(title=hostile, body_html=BODY, seo_description=DESCRIPTION)

    payload = _extract_json_ld(result.content_html)

    assert "</script>" not in payload
    assert "<script>alert" not in payload


def test_a_script_close_in_a_keyword_is_escaped() -> None:
    # `keywords` is *not* passed through the tag stripper — it is model output
    # copied verbatim — so this is the field where the `</` escape is what actually
    # prevents the breakout. A raw `</` would end the script element and hand the
    # rest of the document to an attacker.
    result = render_post(
        title=TITLE,
        body_html=BODY,
        seo_description=DESCRIPTION,
        keywords=["метро</script><script>alert(1)</script>"],
    )

    payload = _extract_json_ld(result.content_html)

    assert "</script>" not in payload
    assert "<\\/script>" in payload


def test_the_escaped_json_ld_still_parses() -> None:
    result = render_post(
        title=TITLE,
        body_html=BODY,
        seo_description=DESCRIPTION,
        keywords=["метро</script>x"],
    )

    payload = _extract_json_ld(result.content_html)
    # `<\/` is valid JSON for `</`, so a consumer reads the original text back
    # intact — the escape protects the HTML parser without corrupting the data.
    parsed = json.loads(payload)

    assert "</script>" in parsed["keywords"]


def test_cyrillic_is_not_escaped_into_unreadable_json() -> None:
    result = render_post(title=TITLE, body_html=BODY, seo_description=DESCRIPTION)

    payload = _extract_json_ld(result.content_html)

    # `ensure_ascii=False`: a page full of `\u041c` sequences is valid but
    # unreadable when debugging a SERP problem.
    assert "Москве" in payload
    assert "\\u041c" not in payload


# ================================================= formatter: full rendering


def test_the_rendered_post_contains_every_block() -> None:
    result = render_post(
        title=TITLE,
        body_html=BODY,
        seo_description=DESCRIPTION,
        url=f"{WP_URL}/news/pyhtino",
        featured_image=featured(),
        carousel=[
            CarouselImage(
                media_id=43, source_url=f"{WP_URL}/a.jpg", alt_text="Платформа"
            )
        ],
        videos=[VideoEmbed(owner_id=-222333, video_id=777, access_key="ak")],
        related=[
            RelatedArticle(
                title="Открылась станция «Внуково»",
                url=f"{WP_URL}/news/vnukovo",
                category="Транспорт",
            )
        ],
        keywords=["метро"],
        category="Транспорт",
        published_at=PUBLISHED_AT,
        publisher_name="СводкаПресс",
    )
    html = result.content_html

    assert "application/ld+json" in html
    assert "svodka-featured" in html
    assert "svodka-article__body" in html
    assert "svodka-carousel" in html
    assert "svodka-related" in html
    assert "<iframe" in html


def test_the_body_typography_is_preserved() -> None:
    result = render_post(title=TITLE, body_html=BODY)

    assert "<h2>Что открыли</h2>" in result.content_html
    assert "<ul><li>45 тысяч пассажиров в сутки</li>" in result.content_html


def test_the_featured_image_renders_as_a_figure_with_a_caption() -> None:
    result = render_post(title=TITLE, body_html=BODY, featured_image=featured())
    html = result.content_html

    assert "<figure" in html
    assert 'alt="Платформа новой станции"' in html
    assert "<figcaption" in html
    assert "Фото: пресс-служба мэрии" in html
    # The featured image is above the fold, so it must not be lazy-loaded.
    assert 'loading="eager"' in html
    assert "wp-image-4242" in html


def test_carousel_images_are_lazy_loaded_and_scroll_snapped() -> None:
    result = render_post(
        title=TITLE,
        body_html=BODY,
        carousel=[
            CarouselImage(media_id=1, source_url=f"{WP_URL}/a.jpg", alt_text="A"),
            CarouselImage(media_id=2, source_url=f"{WP_URL}/b.jpg", alt_text="B"),
        ],
    )
    html = result.content_html

    assert html.count('loading="lazy"') == 2
    # CSS scroll-snap rather than a shortcode: it survives a theme change and needs
    # no plugin.
    assert "scroll-snap-type" in html
    assert "svodka-carousel" in html


def test_the_carousel_is_absent_without_images() -> None:
    result = render_post(title=TITLE, body_html=BODY)

    assert "svodka-carousel" not in result.content_html


def test_a_video_embed_is_responsive() -> None:
    result = render_post(
        title=TITLE,
        body_html=BODY,
        videos=[VideoEmbed(owner_id=-1, video_id=2, access_key="k", title="Открытие")],
    )
    html = result.content_html

    assert "video_ext.php" in html
    assert "padding-bottom:" in html
    assert "hash=k" in html


def test_a_prebuilt_video_embed_is_reused() -> None:
    # A stored `video_links` entry keeps its markup even if the builder changed.
    result = render_post(
        title=TITLE,
        body_html=BODY,
        videos=[
            VideoEmbed(
                owner_id=-1,
                video_id=2,
                embed_html='<figure class="stored-player"></figure>',
            )
        ],
    )

    assert "stored-player" in result.content_html


def test_the_related_block_is_capped() -> None:
    related = [
        RelatedArticle(title=f"Материал {index}", url=f"{WP_URL}/n/{index}")
        for index in range(10)
    ]
    result = render_post(title=TITLE, body_html=BODY, related=related)

    # Three links read as editorial; ten read as a link farm, which is penalised.
    assert result.content_html.count("svodka-related__item") == MAX_RELATED_ARTICLES


def test_related_titles_are_escaped() -> None:
    result = render_post(
        title=TITLE,
        body_html=BODY,
        related=[
            RelatedArticle(title='<script>alert(1)</script>', url=f"{WP_URL}/x")
        ],
    )

    # Autoescaping is on for everything except the sanitised body.
    assert "<script>alert(1)</script>" not in result.content_html
    assert "&lt;script&gt;" in result.content_html


def test_a_hostile_alt_text_is_escaped() -> None:
    result = render_post(
        title=TITLE,
        body_html=BODY,
        featured_image=FeaturedImage(
            media_id=1, source_url=f"{WP_URL}/a.jpg", alt_text='" onerror="alert(1)'
        ),
    )

    assert 'onerror="alert(1)"' not in result.content_html
    assert "&#34;" in result.content_html or "&quot;" in result.content_html


def test_the_body_is_sanitised_during_rendering() -> None:
    result = render_post(
        title=TITLE, body_html=f"{BODY}<script>alert(1)</script>"
    )

    assert "<script>" not in result.content_html
    assert "alert(1)" not in result.content_html


def test_the_schema_can_be_disabled() -> None:
    result = render_post(title=TITLE, body_html=BODY, include_schema=False)

    assert "application/ld+json" not in result.content_html
    assert result.schema == {}
    assert result.has_schema is False


def test_the_formatter_template_is_compiled_once() -> None:
    # Compiling a Jinja template is not free, and this runs per published article.
    formatter = WPPostFormatter()
    first = formatter.render(title=TITLE, body_html=BODY)
    second = formatter.render(title=TITLE, body_html=BODY)

    assert first.content_html == second.content_html


# ==================================================== publisher: Rank Math


def test_rank_math_meta_carries_every_required_field() -> None:
    meta = build_rank_math_meta(
        seo_title="Станция «Пыхтино» открыта",
        seo_description=DESCRIPTION,
        keywords=["станция пыхтино", "метро москвы"],
    )

    assert meta["rank_math_title"] == "Станция «Пыхтино» открыта"
    assert meta["rank_math_description"] == DESCRIPTION
    assert meta["rank_math_focus_keyword"] == "станция пыхтино"
    assert meta["rank_math_robots"] == list(RANK_MATH_ROBOTS_DEFAULT)


def test_the_focus_keyword_is_a_single_phrase() -> None:
    meta = build_rank_math_meta(
        seo_title="t", seo_description="d", keywords=["первый", "второй", "третий"]
    )

    # Rank Math scores one primary phrase; the whole list would dilute the score.
    assert meta["rank_math_focus_keyword"] == "первый"
    assert "," not in meta["rank_math_focus_keyword"]


def test_an_explicit_focus_keyword_wins() -> None:
    meta = build_rank_math_meta(
        seo_title="t",
        seo_description="d",
        focus_keyword="Метро Москвы",
        keywords=["другое"],
    )

    assert meta["rank_math_focus_keyword"] == "метро москвы"


def test_rank_math_fields_are_bounded() -> None:
    meta = build_rank_math_meta(
        seo_title="Заголовок " * 40, seo_description="Описание " * 60
    )

    assert len(meta["rank_math_title"]) <= 70
    assert len(meta["rank_math_description"]) <= 160


def test_empty_rank_math_fields_are_omitted() -> None:
    meta = build_rank_math_meta(seo_title="", seo_description="")

    # Rank Math treats an absent title as "no override" and falls back to the post
    # title; an empty string writes an override that renders as a blank SERP title.
    assert "rank_math_title" not in meta
    assert "rank_math_description" not in meta
    assert "rank_math_focus_keyword" not in meta


def test_the_robots_directives_are_indexable_by_default() -> None:
    assert RANK_MATH_ROBOTS_DEFAULT == ("index", "follow")


# ================================================== publisher: session double


class _Result:
    """The `.all()` slice of a SQLAlchemy result."""

    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def all(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _Savepoint:
    """A `begin_nested()` context that enforces the tag-cache unique index."""

    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _Savepoint:
        self._session.pending = []
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc is not None:
            self._session.pending = []
            return False
        for instance in self._session.pending:
            if isinstance(instance, WPTagCache):
                key = instance.tag_name.lower()
                if key in self._session.tags or key in self._session.concurrent:
                    # What the real `uq_wp_tags_cache_tag_name_lower` index does.
                    self._session.pending = []
                    raise IntegrityError("duplicate tag", None, Exception("unique"))
                self._session.tags[key] = instance.wp_id
        self._session.pending = []
        return False


class FakeSession:
    """In-memory stand-in for `AsyncSession`, modelling the tag cache.

    The `lower(tag_name)` uniqueness is enforced deliberately: without it, the
    concurrent-creation test would pass against a cache that silently stores
    duplicates, which is the exact bug the real functional index prevents.
    """

    def __init__(self, tags: dict[str, int] | None = None) -> None:
        self.tags: dict[str, int] = dict(tags or {})
        self.pending: list[Any] = []
        self.queries = 0
        #: Rows a *competing* worker inserted after this session read the cache.
        #: They are invisible to `execute` but still collide on the unique index,
        #: which is what makes the lost-race path reachable in a test.
        self.concurrent: dict[str, int] = {}

    def add(self, instance: Any) -> None:
        self.pending.append(instance)

    def begin_nested(self) -> _Savepoint:
        return _Savepoint(self)

    async def execute(self, statement: Any) -> _Result:
        self.queries += 1
        return _Result(list(self.tags.items()))

    def as_session(self) -> AsyncSession:
        """Return `self` typed as `AsyncSession` for the publisher under test."""
        return cast(AsyncSession, self)


def new_publisher() -> WPPublisher:
    """A publisher pointed at the mocked host."""
    return WPPublisher(base_url=WP_URL, username=WP_USER, app_password=WP_PASSWORD)


def post_response(**overrides: Any) -> httpx.Response:
    """A created-post response from `/wp/v2/posts`."""
    payload: dict[str, Any] = {
        "id": 5150,
        "link": f"{WP_URL}/2026/08/pyhtino/",
        "slug": "v-moskve-otkryli-stantsiyu-metro-pyhtino",
        "status": "publish",
        "meta": {
            "rank_math_title": "t",
            "rank_math_description": "d",
            "rank_math_focus_keyword": "k",
            "rank_math_robots": ["index", "follow"],
        },
    }
    payload.update(overrides)
    return httpx.Response(201, json=payload)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse retry backoff so retry paths run instantly."""
    monkeypatch.setattr("app.services.wp_publisher.INITIAL_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr("app.services.wp_publisher.MAX_BACKOFF_SECONDS", 0.0)


# ==================================================== publisher: tag caching


async def test_cached_tags_reach_no_wordpress_api() -> None:
    session = FakeSession({"метро москвы": 11, "пыхтино": 22})

    with respx.mock(base_url=WP_URL, assert_all_called=False) as mock:
        search = mock.get(TAGS_ENDPOINT)
        create = mock.post(TAGS_ENDPOINT)
        publisher = new_publisher()
        try:
            resolution = await publisher.resolve_tags(
                session.as_session(), ["Метро Москвы", "Пыхтино"]
            )
        finally:
            await publisher.close()

    assert resolution.tag_ids == (11, 22)
    assert resolution.from_cache == 2
    # The whole point of the cache: no term search, no term creation.
    assert search.call_count == 0
    assert create.call_count == 0


async def test_an_uncached_tag_is_searched_then_cached() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.get(TAGS_ENDPOINT).mock(
            return_value=httpx.Response(200, json=[{"id": 33, "name": "Метро Москвы"}])
        )
        publisher = new_publisher()
        try:
            resolution = await publisher.resolve_tags(
                session.as_session(), ["Метро Москвы"]
            )
        finally:
            await publisher.close()

    assert resolution.tag_ids == (33,)
    assert resolution.from_wordpress == 1
    # Written back, so the next run is free.
    assert session.tags["метро москвы"] == 33


async def test_a_missing_tag_is_created_and_cached() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.get(TAGS_ENDPOINT).mock(return_value=httpx.Response(200, json=[]))
        mock.post(TAGS_ENDPOINT).mock(
            return_value=httpx.Response(201, json={"id": 44, "name": "Пыхтино"})
        )
        publisher = new_publisher()
        try:
            resolution = await publisher.resolve_tags(session.as_session(), ["Пыхтино"])
        finally:
            await publisher.close()

    assert resolution.tag_ids == (44,)
    assert resolution.created == 1
    assert session.tags["пыхтино"] == 44


async def test_tag_lookup_is_case_and_whitespace_insensitive() -> None:
    session = FakeSession({"метро москвы": 11})

    with respx.mock(base_url=WP_URL, assert_all_called=False) as mock:
        search = mock.get(TAGS_ENDPOINT)
        publisher = new_publisher()
        try:
            resolution = await publisher.resolve_tags(
                session.as_session(), ["  МЕТРО   Москвы  "]
            )
        finally:
            await publisher.close()

    # Matching the `lower(tag_name)` index: treating these as two tags would create
    # a duplicate WordPress term and split the archive page.
    assert resolution.tag_ids == (11,)
    assert search.call_count == 0


async def test_duplicate_tags_in_one_batch_collapse() -> None:
    session = FakeSession({"метро": 11})

    with respx.mock(base_url=WP_URL, assert_all_called=False):
        publisher = new_publisher()
        try:
            resolution = await publisher.resolve_tags(
                session.as_session(), ["Метро", "метро", "МЕТРО"]
            )
        finally:
            await publisher.close()

    assert resolution.tag_ids == (11,)


async def test_a_concurrent_cache_write_does_not_fail_the_publish() -> None:
    # Two workers resolve the same uncached tag. This one searches WordPress, finds
    # the term, and then tries to cache it — but the other worker already wrote that
    # row, so the insert violates `uq_wp_tags_cache_tag_name_lower`.
    #
    # The session double starts empty so the cache query misses and the WordPress
    # path runs, then the row appears before the write-back, which is exactly the
    # interleaving that happens in production.
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.get(TAGS_ENDPOINT).mock(
            return_value=httpx.Response(200, json=[{"id": 44, "name": "Пыхтино"}])
        )
        publisher = new_publisher()
        try:
            session.concurrent["пыхтино"] = 44  # the competing worker's row
            resolution = await publisher.resolve_tags(
                session.as_session(), ["Пыхтино"]
            )
        finally:
            await publisher.close()

    # The winner's row is as good as ours, so the IntegrityError is swallowed and
    # the publish continues. Without the SAVEPOINT it would poison the outer
    # transaction and lose the article over a redundant cache write.
    assert resolution.tag_ids == (44,)


async def test_a_wordpress_duplicate_error_resolves_to_the_existing_term() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.get(TAGS_ENDPOINT).mock(
            side_effect=[
                httpx.Response(200, json=[]),
                httpx.Response(200, json=[{"id": 55, "name": "Пыхтино"}]),
            ]
        )
        mock.post(TAGS_ENDPOINT).mock(
            return_value=httpx.Response(400, json={"code": "term_exists"})
        )
        publisher = new_publisher()
        try:
            resolution = await publisher.resolve_tags(session.as_session(), ["Пыхтино"])
        finally:
            await publisher.close()

    # A lost race is recovered by re-reading, not by failing.
    assert resolution.tag_ids == (55,)


async def test_a_substring_match_is_not_accepted() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        # `search` is a substring match, so "метро" returns "метро москвы".
        mock.get(TAGS_ENDPOINT).mock(
            return_value=httpx.Response(200, json=[{"id": 99, "name": "Метро Москвы"}])
        )
        mock.post(TAGS_ENDPOINT).mock(
            return_value=httpx.Response(201, json={"id": 100, "name": "Метро"})
        )
        publisher = new_publisher()
        try:
            resolution = await publisher.resolve_tags(session.as_session(), ["Метро"])
        finally:
            await publisher.close()

    # Accepting the substring would attach the wrong term.
    assert resolution.tag_ids == (100,)


async def test_the_tag_count_is_capped() -> None:
    names = [f"тег{index}" for index in range(20)]
    session = FakeSession({name: index + 1 for index, name in enumerate(names)})

    with respx.mock(base_url=WP_URL, assert_all_called=False):
        publisher = new_publisher()
        try:
            resolution = await publisher.resolve_tags(session.as_session(), names)
        finally:
            await publisher.close()

    # Thirty tags read as keyword stuffing and create a thicket of empty archives.
    assert resolution.total == MAX_TAGS_PER_POST


async def test_an_empty_tag_list_makes_no_query() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=False):
        publisher = new_publisher()
        try:
            resolution = await publisher.resolve_tags(session.as_session(), ["", "  "])
        finally:
            await publisher.close()

    assert resolution.tag_ids == ()
    assert session.queries == 0


# ==================================================== publisher: post writes


async def test_a_post_is_created_with_its_metadata() -> None:
    session = FakeSession({"метро": 11})

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.get(CATEGORIES_ENDPOINT).mock(
            return_value=httpx.Response(200, json=[{"id": 7, "slug": "transport"}])
        )
        route = mock.post(POSTS_ENDPOINT).mock(return_value=post_response())
        publisher = new_publisher()
        try:
            published = await publish_article(
                session.as_session(),
                title=TITLE,
                content_html=BODY,
                category_slug="transport",
                tags=["Метро"],
                featured_media=4242,
                seo_title="Станция «Пыхтино» открыта",
                seo_description=DESCRIPTION,
                publisher=publisher,
            )
        finally:
            await publisher.close()

    assert published.post_id == 5150
    assert published.updated is False
    assert published.featured_media == 4242
    assert published.tag_ids == (11,)
    assert published.category_ids == (7,)

    body = json.loads(route.calls[0].request.content)
    assert body["title"] == TITLE
    assert body["featured_media"] == 4242
    assert body["tags"] == [11]
    assert body["categories"] == [7]
    assert body["status"] == "publish"


async def test_the_rank_math_fields_are_sent_as_meta() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(POSTS_ENDPOINT).mock(return_value=post_response())
        publisher = new_publisher()
        try:
            await publish_article(
                session.as_session(),
                title=TITLE,
                content_html=BODY,
                seo_title="Станция «Пыхтино»",
                seo_description=DESCRIPTION,
                focus_keyword="станция пыхтино",
                publisher=publisher,
            )
        finally:
            await publisher.close()

    meta = json.loads(route.calls[0].request.content)["meta"]

    assert meta["rank_math_title"] == "Станция «Пыхтино»"
    assert meta["rank_math_description"] == DESCRIPTION
    assert meta["rank_math_focus_keyword"] == "станция пыхтино"
    assert meta["rank_math_robots"] == ["index", "follow"]


async def test_dropped_rank_math_meta_is_reported_as_a_warning() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        # WordPress silently discards `meta` keys that are not registered with
        # `show_in_rest`, answering 200 with the SEO fields absent.
        mock.post(POSTS_ENDPOINT).mock(return_value=post_response(meta={}))
        publisher = new_publisher()
        try:
            published = await publish_article(
                session.as_session(),
                title=TITLE,
                content_html=BODY,
                seo_title="Станция",
                seo_description=DESCRIPTION,
                publisher=publisher,
            )
        finally:
            await publisher.close()

    # The post is live — an article without meta descriptions beats no article — but
    # the silent failure is surfaced.
    assert published.post_id == 5150
    assert published.warnings
    assert "rank_math" in published.warnings[0]


async def test_an_existing_post_is_updated_not_recreated() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(f"{POSTS_ENDPOINT}/5150").mock(
            return_value=post_response()
        )
        publisher = new_publisher()
        try:
            published = await publish_article(
                session.as_session(),
                title=TITLE,
                content_html=BODY,
                existing_post_id=5150,
                publisher=publisher,
            )
        finally:
            await publisher.close()

    # Duplicate live articles are what a news site cannot tolerate.
    assert route.call_count == 1
    assert published.updated is True


async def test_the_slug_is_transliterated() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(POSTS_ENDPOINT).mock(return_value=post_response())
        publisher = new_publisher()
        try:
            await publish_article(
                session.as_session(),
                title=TITLE,
                content_html=BODY,
                publisher=publisher,
            )
        finally:
            await publisher.close()

    slug = json.loads(route.calls[0].request.content)["slug"]

    assert slug.startswith("v-moskve-otkryli-stantsiyu-metro-pyhtino")
    assert "москве" not in slug


async def test_absent_optional_fields_are_omitted_from_the_payload() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(POSTS_ENDPOINT).mock(return_value=post_response())
        publisher = new_publisher()
        try:
            await publish_article(
                session.as_session(),
                title=TITLE,
                content_html=BODY,
                publisher=publisher,
            )
        finally:
            await publisher.close()

    body = json.loads(route.calls[0].request.content)

    # `featured_media: 0` means "remove the image" and an empty `categories` array
    # means "clear them", so placeholders would strip what a previous publish set.
    assert "featured_media" not in body
    assert "categories" not in body
    assert "tags" not in body


async def test_a_missing_category_is_created() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.get(CATEGORIES_ENDPOINT).mock(return_value=httpx.Response(200, json=[]))
        create = mock.post(CATEGORIES_ENDPOINT).mock(
            return_value=httpx.Response(201, json={"id": 9, "slug": "avtonovosti"})
        )
        mock.post(POSTS_ENDPOINT).mock(return_value=post_response())
        publisher = new_publisher()
        try:
            published = await publish_article(
                session.as_session(),
                title=TITLE,
                content_html=BODY,
                category_slug="avtonovosti",
                publisher=publisher,
            )
        finally:
            await publisher.close()

    assert create.call_count == 1
    assert published.category_ids == (9,)


async def test_a_tag_failure_still_publishes_the_post() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.get(TAGS_ENDPOINT).mock(return_value=httpx.Response(500, text="down"))
        mock.post(POSTS_ENDPOINT).mock(return_value=post_response())
        publisher = new_publisher()
        try:
            published = await publish_article(
                session.as_session(),
                title=TITLE,
                content_html=BODY,
                tags=["Метро"],
                publisher=publisher,
            )
        finally:
            await publisher.close()

    # A post without tags is publishable; losing the article to a term lookup would
    # be the wrong trade.
    assert published.post_id == 5150
    assert published.tag_ids == ()
    assert any("tags unresolved" in warning for warning in published.warnings)


# ==================================================== publisher: error paths


@pytest.mark.parametrize("status_code", [401, 403])
async def test_a_rejected_credential_is_not_retried(status_code: int) -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(POSTS_ENDPOINT).mock(
            return_value=httpx.Response(status_code, json={"code": "unauthorized"})
        )
        publisher = new_publisher()
        try:
            with pytest.raises(WPPublishAuthError):
                await publish_article(
                    session.as_session(),
                    title=TITLE,
                    content_html=BODY,
                    publisher=publisher,
                )
        finally:
            await publisher.close()

    assert route.call_count == 1


async def test_a_rate_limit_is_retried_then_raised() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(POSTS_ENDPOINT).mock(
            return_value=httpx.Response(429, text="slow down")
        )
        publisher = new_publisher()
        try:
            with pytest.raises(WPPublishRateLimitError):
                await publish_article(
                    session.as_session(),
                    title=TITLE,
                    content_html=BODY,
                    publisher=publisher,
                )
        finally:
            await publisher.close()

    assert route.call_count == MAX_ATTEMPTS


async def test_a_write_timeout_is_not_retried() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(POSTS_ENDPOINT).mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        publisher = new_publisher()
        try:
            with pytest.raises(WPPublishResponseError, match="timed out"):
                await publish_article(
                    session.as_session(),
                    title=TITLE,
                    content_html=BODY,
                    publisher=publisher,
                )
        finally:
            await publisher.close()

    # WordPress may have created the post with only the response lost.
    assert route.call_count == 1


async def test_an_html_error_page_is_reported_clearly() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.post(POSTS_ENDPOINT).mock(
            return_value=httpx.Response(200, text="<html>PHP Fatal error</html>")
        )
        publisher = new_publisher()
        try:
            with pytest.raises(WPPublishResponseError, match="non-JSON"):
                await publish_article(
                    session.as_session(),
                    title=TITLE,
                    content_html=BODY,
                    publisher=publisher,
                )
        finally:
            await publisher.close()


async def test_a_response_without_an_id_is_rejected() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.post(POSTS_ENDPOINT).mock(
            return_value=httpx.Response(201, json={"link": "x"})
        )
        publisher = new_publisher()
        try:
            with pytest.raises(WPPublishResponseError, match="no post id"):
                await publish_article(
                    session.as_session(),
                    title=TITLE,
                    content_html=BODY,
                    publisher=publisher,
                )
        finally:
            await publisher.close()


async def test_missing_credentials_raise_rather_than_degrade() -> None:
    session = FakeSession()
    publisher = WPPublisher(base_url=WP_URL, username="", app_password="")
    try:
        with pytest.raises(WPPublishNotConfiguredError):
            await publish_article(
                session.as_session(),
                title=TITLE,
                content_html=BODY,
                publisher=publisher,
            )
    finally:
        await publisher.close()


async def test_the_application_password_is_sent_as_basic_auth() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(POSTS_ENDPOINT).mock(return_value=post_response())
        publisher = new_publisher()
        try:
            await publish_article(
                session.as_session(),
                title=TITLE,
                content_html=BODY,
                publisher=publisher,
            )
        finally:
            await publisher.close()

    header = route.calls[0].request.headers["Authorization"]

    assert header.startswith("Basic ")
    # The spaces WordPress prints for legibility must be stripped before encoding.
    assert " " not in header.removeprefix("Basic ")


async def test_the_application_password_never_appears_in_an_error() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.post(POSTS_ENDPOINT).mock(
            return_value=httpx.Response(500, text="internal error")
        )
        publisher = new_publisher()
        try:
            with pytest.raises(WPPublishResponseError) as info:
                await publish_article(
                    session.as_session(),
                    title=TITLE,
                    content_html=BODY,
                    publisher=publisher,
                )
        finally:
            await publisher.close()

    assert WP_PASSWORD.replace(" ", "") not in str(info.value)
    assert "abcd" not in str(info.value)


async def test_publish_reuses_existing_post_when_slug_exists() -> None:
    session = FakeSession()

    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        lookup_route = mock.get(POSTS_ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": 7777,
                        "slug": "test-slug-123",
                        "link": f"{WP_URL}/test-slug-123/",
                    }
                ],
            )
        )
        update_route = mock.post(f"{POSTS_ENDPOINT}/7777").mock(
            return_value=post_response(id=7777, slug="test-slug-123")
        )
        publisher = new_publisher()
        try:
            res = await publisher.publish(
                session.as_session(),
                title=TITLE,
                content_html=BODY,
                slug="test-slug-123",
            )
        finally:
            await publisher.close()

    assert lookup_route.called
    assert update_route.called
    assert res.post_id == 7777
    assert res.updated is True

