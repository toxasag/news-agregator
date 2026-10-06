"""Tests for the ingestion layer: GramJS bridge, album grouping, Playwright pool.

Nothing here talks to the outside world. The Telegram bridge is served by
`respx`, the crawler fixtures are served by a `http.server` bound to
`127.0.0.1` in a background thread, and PostgreSQL is never involved: the
ingestor returns value objects by design (see the module docstring of
`app.services.telegram_ingestor`), so `Source` and `RawNews` are used as plain
Python objects.

Limits and thresholds are read from `settings`, so the tests track the
configuration instead of freezing today's numbers.
"""

# Test inputs are Russian news copy, so Cyrillic letters that look like Latin
# ones are unavoidable and expected; RUF001 would flag every sentence.
# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
import threading
import warnings
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final
from urllib.parse import urlparse

import httpx
import pytest
import respx

from app.config import settings
from app.models.raw_news import RawNews
from app.models.source import Source
from app.services.telegram_ingestor import (
    DIALOGS_PATH,
    MESSAGES_PATH,
    MESSAGES_READ_PATH,
    IngestCandidate,
    MediaItem,
    PreparedPost,
    TelegramAuthError,
    TelegramIngestor,
    TelegramMessage,
    TelegramNotConfiguredError,
    TelegramResponseError,
    TelegramStatusError,
    TelegramTransportError,
    build_post_url,
    group_albums,
    public_username,
)
from app.services.text_utils import (
    REASON_AD_ERID,
    REASON_AD_FOOTER,
    REASON_DIGEST_HEADLINE,
    REASON_DIGEST_NUMBERED_LIST,
    analyze_post,
    telegram_entities_to_html,
)
from app.services.web_scraper import (
    MIN_CONTENT_LENGTH,
    STEALTH_AVAILABLE,
    BrowserPool,
    CrawlPreview,
    ScrapedArticle,
    ScraperError,
    close_browser_pool,
    discover_links,
    get_browser_pool,
    scrape_article,
)
from app.services.web_scraper import (
    # Aliased: pytest would otherwise collect the imported name as a test.
    test_crawl_url as crawl_url_preview,
)
from app.workers.ingestion_worker import _Candidate

#: Base URL of the mocked GramJS bridge. Never resolved: `respx` intercepts the
#: transport, so no DNS lookup and no socket happen.
BRIDGE_URL: Final[str] = "http://gramjs.test:3000"

#: A token that must never appear in an exception message or a log line.
BRIDGE_TOKEN: Final[str] = "gramjs-secret-9f2c1a"  # noqa: S105 - fake test token

#: Exact key set `raw_news` accepts from `PreparedPost.to_raw_news_fields()`.
RAW_NEWS_FIELD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "external_post_id",
        "post_url",
        "raw_text",
        "clean_html",
        "media_data",
        "is_digest",
        "is_ad",
        "filter_reasons",
    }
)

#: Exact key set of one `raw_news.media_data` entry, per docs/03.
MEDIA_DATA_KEYS: Final[frozenset[str]] = frozenset(
    {"type", "original_post_id", "media_id", "size"}
)

#: Width of the `filter_reasons` array elements (`ARRAY(String(64))`).
FILTER_REASON_MAX_LENGTH: Final[int] = 64

#: A post that trips the digest heuristics, so `is_digest` is observable.
DIGEST_POST: Final[str] = (
    "Главное к этому часу.\n"
    "1. Совет директоров банка утвердил стратегию развития до 2030 года.\n"
    "2. Аэропорт Пулково возобновил приём рейсов после ночного снегопада.\n"
    "3. Правительство продлило программу субсидирования ипотеки для семей."
)

#: A post that trips the advertising heuristics, so `is_ad` is observable.
AD_POST: Final[str] = (
    "Новый жилой комплекс на берегу реки открыл продажи квартир с отделкой.\n"
    "На правах рекламы. ООО «Застройщик», erid: 2VtzqvJk9ZH"
)


# =============================================================== helper builders


def bridge_message(
    message_id: int,
    text: str = "",
    *,
    grouped_id: str | int | None = None,
    media: Sequence[Mapping[str, Any]] = (),
    entities: Sequence[Mapping[str, Any]] = (),
    date: int | str | None = 1_773_000_000,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one raw bridge message in the shape GramJS emits."""
    payload: dict[str, Any] = {"id": message_id, "message": text}
    if date is not None:
        payload["date"] = date
    if grouped_id is not None:
        payload["groupedId"] = grouped_id
    if media:
        payload["media"] = [dict(item) for item in media]
    if entities:
        payload["entities"] = [dict(item) for item in entities]
    if extra:
        payload.update(extra)
    return payload


def bridge_photo(media_id: str, *, size: int = 184_320) -> dict[str, Any]:
    """One photo attachment as the bridge reports it."""
    return {"type": "photo", "media_id": media_id, "size": size}


def telegram_message(
    message_id: int,
    text: str = "",
    *,
    channel: str = "@moscow_news",
    grouped_id: str | None = None,
    media: Sequence[MediaItem] = (),
    entities: Sequence[Mapping[str, Any]] = (),
) -> TelegramMessage:
    """Build a normalised `TelegramMessage` without going through the bridge."""
    return TelegramMessage(
        id=message_id,
        channel=channel,
        text=text,
        entities=tuple(entities),
        grouped_id=grouped_id,
        date=None,
        media=tuple(media),
        views=None,
        forwards=None,
        post_url=build_post_url(channel, message_id),
    )


def media_item(media_id: str, original_post_id: int) -> MediaItem:
    """One photo `MediaItem` attached to `original_post_id`."""
    return MediaItem(
        type="photo",
        original_post_id=original_post_id,
        media_id=media_id,
        size=2048,
    )


def telegram_source(
    *,
    source_id: int = 7,
    identifier: str = "@moscow_news",
    skip_media: bool = False,
) -> Source:
    """A transient `Source` row: no database, no flush, no session."""
    return Source(id=source_id, identifier=identifier, skip_media=skip_media)


def json_response(payload: Any, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def messages_response(messages: Sequence[Mapping[str, Any]]) -> httpx.Response:
    return json_response({"messages": [dict(item) for item in messages]})


def new_ingestor(*, token: str = BRIDGE_TOKEN, attempts: int = 1) -> TelegramIngestor:
    """An ingestor pointed at the mocked bridge. The caller must close it."""
    return TelegramIngestor(
        base_url=BRIDGE_URL, token=token, max_attempts=attempts, timeout=1.0
    )


# =================================================================== fixtures


@pytest.fixture
async def ingestor() -> AsyncIterator[TelegramIngestor]:
    """A configured ingestor with retries disabled and the pool closed after.

    `max_attempts=1` keeps the default tests free of backoff sleeps; the retry
    tests build their own instance with the attempt count they need.
    """
    client = new_ingestor()
    try:
        yield client
    finally:
        # httpx pools a live connection: leaving it to the garbage collector
        # emits a ResourceWarning, which this suite treats as an error.
        await client.close()


# ================================================= GramJS entity parsing (HTML)


def test_utf16_offsets_survive_an_emoji_before_a_bold_entity() -> None:
    # Telegram counts `offset`/`length` in UTF-16 code units. The fire emoji is
    # outside the BMP, so it occupies *two* units but only one Python code
    # point: naive `str` slicing shifts every entity that follows it.
    text = "🔥 Москва: важное сообщение о работе метро"
    entity = {"type": "bold", "offset": 3, "length": 6}

    html = telegram_entities_to_html(text, [entity])

    assert html == "🔥 <b>Москва</b>: важное сообщение о работе метро"
    # The emoji itself must survive intact, not as a lone surrogate or a
    # replacement character.
    assert html.startswith("🔥 ")
    assert "\ufffd" not in html

    # Negative control: the same offsets applied with Python code-point slicing
    # produce a different, wrong result. Without this the assertion above would
    # also pass on an implementation that ignored UTF-16 entirely.
    naive = f"{text[:3]}<b>{text[3:9]}</b>{text[9:]}"
    assert naive == "🔥 М<b>осква:</b> важное сообщение о работе метро"
    assert naive != html


def test_an_entity_ending_on_an_emoji_keeps_the_pair_whole() -> None:
    # The bold range covers "Пожар " plus the emoji's high surrogate only; the
    # boundary is snapped outward so the pair is never split.
    html = telegram_entities_to_html(
        "Пожар 🔥 в центре", [{"type": "bold", "offset": 0, "length": 7}]
    )

    assert html == "<b>Пожар 🔥</b> в центре"
    assert "\ufffd" not in html


def test_nested_bold_and_italic_produce_well_formed_html() -> None:
    html = telegram_entities_to_html(
        "Срочные новости дня",
        [
            {"type": "bold", "offset": 0, "length": 19},
            {"type": "italic", "offset": 8, "length": 7},
        ],
    )

    # The wider entity is outermost and the tags do not cross.
    assert html == "<b>Срочные <i>новости</i> дня</b>"
    assert html.index("<b>") < html.index("<i>") < html.index("</i>")
    assert html.index("</i>") < html.index("</b>")


@pytest.mark.parametrize(
    ("entity", "text", "expected"),
    [
        pytest.param(
            {
                "type": "text_link",
                "offset": 20,
                "length": 5,
                "url": "https://a.ru/1",
            },
            "Читайте подробности здесь",
            'Читайте подробности <a href="https://a.ru/1">здесь</a>',
            id="text_link_bot_api",
        ),
        pytest.param(
            {
                "className": "MessageEntityTextUrl",
                "offset": 20,
                "length": 5,
                "url": "https://a.ru/1",
            },
            "Читайте подробности здесь",
            'Читайте подробности <a href="https://a.ru/1">здесь</a>',
            id="text_url_mtproto",
        ),
        pytest.param(
            {"type": "spoiler", "offset": 0, "length": 5},
            "Тайна раскрыта",
            '<span class="tg-spoiler">Тайна</span> раскрыта',
            id="spoiler",
        ),
        pytest.param(
            {"type": "code", "offset": 5, "length": 8},
            "Код: print(1)",
            "Код: <code>print(1)</code>",
            id="code",
        ),
        pytest.param(
            {"type": "pre", "offset": 5, "length": 8, "language": "python"},
            "Код:\nprint(1)\nконец",
            'Код:<br><pre><code class="language-python">print(1)</code></pre>'
            "<br>конец",
            id="pre_with_language",
        ),
        pytest.param(
            {"type": "pre", "offset": 5, "length": 8},
            "Код:\nprint(1)\nконец",
            "Код:<br><pre>print(1)</pre><br>конец",
            id="pre_without_language",
        ),
        pytest.param(
            {"type": "pre", "offset": 5, "length": 8, "language": 'py"><script>'},
            "Код:\nprint(1)\nконец",
            "Код:<br><pre>print(1)</pre><br>конец",
            id="pre_language_cannot_break_out",
        ),
    ],
)
def test_entity_types_map_to_the_documented_markup(
    entity: dict[str, Any], text: str, expected: str
) -> None:
    assert telegram_entities_to_html(text, [entity]) == expected


def test_anchor_uses_the_entity_url_not_the_anchor_text() -> None:
    html = telegram_entities_to_html(
        "Читайте подробности здесь",
        [
            {
                "type": "text_link",
                "offset": 20,
                "length": 5,
                "url": "https://example.ru/news/2026/incident",
            }
        ],
    )

    assert 'href="https://example.ru/news/2026/incident"' in html
    assert ">здесь</a>" in html


def test_unsafe_href_schemes_are_rendered_as_plain_text() -> None:
    html = telegram_entities_to_html(
        "Клик тут",
        [
            {
                "type": "text_link",
                "offset": 0,
                "length": 4,
                "url": "javascript:alert(1)",
            }
        ],
    )

    assert html == "Клик тут"
    assert "<a" not in html
    assert "javascript" not in html


def test_script_tags_in_the_message_text_are_escaped() -> None:
    text = '<script>alert("xss")</script> Новость'

    html = telegram_entities_to_html(
        text, [{"type": "bold", "offset": 30, "length": 7}]
    )

    assert "<script>" not in html
    assert html.startswith("&lt;script&gt;")
    assert "&lt;/script&gt;" in html
    assert html.endswith("<b>Новость</b>")
    # The only tags in the output are the ones the converter emitted itself.
    assert html.count("<") == html.count("<b>") + html.count("</b>")


def test_malformed_entities_are_skipped_without_raising() -> None:
    text = "Обычный текст новости"
    entities: list[dict[str, Any]] = [
        {"type": "bold", "offset": -5, "length": 4},
        {"type": "bold", "offset": 100, "length": 3},
        {"type": "bold", "offset": 0, "length": 0},
        {"type": "bold", "offset": "не число", "length": 4},
        {"type": "bold", "length": 4},
        {"type": "quantum_flux", "offset": 0, "length": 7},
        # Valid, but its length runs past the end of the text: it is clamped
        # rather than dropped, which proves the others were skipped by rule and
        # not because the whole list was thrown away.
        {"type": "italic", "offset": 8, "length": 999},
    ]

    html = telegram_entities_to_html(text, entities)

    assert html == "Обычный <i>текст новости</i>"


def test_empty_text_and_no_entities_are_handled() -> None:
    entity = {"type": "bold", "offset": 0, "length": 1}
    assert telegram_entities_to_html("", [entity]) == ""
    assert telegram_entities_to_html("Строка\nвторая", []) == "Строка<br>вторая"


# ================================================================ album grouping


def test_three_messages_sharing_a_grouped_id_collapse_into_one_candidate() -> None:
    # Telegram delivers an album as several messages with one `groupedId`, and
    # the caption belongs to exactly one of them (here the middle one).
    messages = [
        telegram_message(51, "", grouped_id="7712", media=[media_item("m51", 51)]),
        telegram_message(
            52, "Подпись альбома", grouped_id="7712", media=[media_item("m52", 52)]
        ),
        telegram_message(53, "", grouped_id="7712", media=[media_item("m53", 53)]),
    ]

    candidates = group_albums(messages)

    assert len(candidates) == 1
    album = candidates[0]
    assert album.external_post_id == "51"
    assert album.primary_message_id == 51
    assert album.message_ids == (51, 52, 53)
    assert album.text == "Подпись альбома"
    assert album.is_album is True
    assert album.post_url == "https://t.me/moscow_news/51"
    # Media come from every member, ordered by ascending message id.
    assert [item.media_id for item in album.media] == ["m51", "m52", "m53"]
    assert [item.original_post_id for item in album.media] == [51, 52, 53]


def test_album_entities_come_from_the_caption_carrying_message() -> None:
    # Entity offsets only make sense against the text they were reported with,
    # so they must travel together with the caption.
    caption_entities = ({"type": "bold", "offset": 0, "length": 7},)
    messages = [
        telegram_message(
            51,
            "",
            grouped_id="7712",
            entities=({"type": "italic", "offset": 0, "length": 3},),
        ),
        telegram_message(
            52, "Подпись альбома", grouped_id="7712", entities=caption_entities
        ),
    ]

    album = group_albums(messages)[0]

    assert album.entities == caption_entities
    rendered = analyze_post(album.text, album.entities).clean_html
    assert rendered == "<b>Подпись</b> альбома"


def test_album_media_are_deduplicated_by_media_id() -> None:
    # The overlap window re-delivers the tail of the feed, so the same
    # attachment legitimately arrives twice. Storing it twice would mean two
    # downloads and two OCR runs in Package 3.
    messages = [
        telegram_message(51, "", grouped_id="7712", media=[media_item("m1", 51)]),
        telegram_message(
            52,
            "Подпись",
            grouped_id="7712",
            media=[media_item("m1", 52), media_item("m2", 52)],
        ),
    ]

    album = group_albums(messages)[0]

    assert [item.media_id for item in album.media] == ["m1", "m2"]
    # First occurrence wins, so `original_post_id` stays the lowest id that
    # carried the attachment.
    assert [item.original_post_id for item in album.media] == [51, 52]


def test_repeated_message_ids_are_collapsed_not_duplicated() -> None:
    first = telegram_message(51, "Первая доставка", media=[media_item("a", 51)])
    replay = telegram_message(51, "Вторая доставка", media=[media_item("b", 51)])

    candidates = group_albums([first, replay])

    assert len(candidates) == 1
    assert candidates[0].message_ids == (51,)
    assert candidates[0].text == "Первая доставка"


def test_mixed_batch_yields_deterministic_ascending_candidates() -> None:
    messages = [
        telegram_message(60, "Позднее одиночное сообщение"),
        telegram_message(
            52, "Подпись", grouped_id="7712", media=[media_item("b", 52)]
        ),
        telegram_message(51, "", grouped_id="7712", media=[media_item("a", 51)]),
        telegram_message(45, "Раннее одиночное сообщение"),
        telegram_message(53, "", grouped_id="7712", media=[media_item("c", 53)]),
        telegram_message(
            58, "Второй альбом", grouped_id="8899", media=[media_item("z", 58)]
        ),
    ]

    candidates = group_albums(messages)

    assert [item.primary_message_id for item in candidates] == [45, 51, 58, 60]
    assert [item.message_ids for item in candidates] == [
        (45,),
        (51, 52, 53),
        (58,),
        (60,),
    ]
    assert [item.is_album for item in candidates] == [False, True, True, False]
    # A group whose other members fell outside this page is still an album.
    assert candidates[2].message_ids == (58,)
    # Shuffling the input cannot change the output: the order is derived from
    # message ids, not from delivery order.
    assert group_albums(list(reversed(messages))) == candidates


def test_caption_tie_break_takes_the_lowest_id_that_carries_text() -> None:
    # `_build_candidate` documents this rule explicitly and explains why it
    # deviates from the "longest caption" wording in docs/03: the longest
    # caption is not stable under the overlap window, because a later pass that
    # re-reads only part of the album would pick a different text and rewrite
    # the row.
    messages = [
        telegram_message(51, "   ", grouped_id="7712"),
        telegram_message(52, "Короткая", grouped_id="7712"),
        telegram_message(
            53, "Значительно более длинная подпись альбома", grouped_id="7712"
        ),
    ]

    album = group_albums(messages)[0]

    assert album.text == "Короткая"
    assert album.external_post_id == "51"
    # Whitespace-only text does not count as a caption.
    assert album.text.strip() == album.text
    # Re-reading only the tail of the album keeps the same caption, which is
    # exactly the stability property the rule buys.
    assert group_albums(messages[1:])[0].text == "Короткая"


def test_a_standalone_post_takes_the_single_member_path() -> None:
    candidate = group_albums([telegram_message(60, "Одиночный пост")])[0]

    assert candidate.message_ids == (60,)
    assert candidate.is_album is False
    assert candidate.external_post_id == "60"


def test_group_albums_of_an_empty_page_is_empty() -> None:
    assert group_albums([]) == []


async def test_album_ids_come_from_the_bridge_and_collapse_end_to_end() -> None:
    source = telegram_source()
    payload = messages_response(
        [
            bridge_message(51, grouped_id="7712", media=[bridge_photo("m51", size=1)]),
            bridge_message(
                52,
                "Подпись альбома",
                grouped_id="7712",
                media=[bridge_photo("m52", size=2)],
            ),
            bridge_message(53, grouped_id="7712", media=[bridge_photo("m53", size=3)]),
        ]
    )
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        mock.get(MESSAGES_PATH).mock(side_effect=[payload, messages_response([])])
        client = new_ingestor()
        try:
            result = await client.ingest_source(source, last_max_id=40)
        finally:
            await client.close()

    assert result.fetched_messages == 3
    assert result.albums_collapsed == 1
    assert result.max_message_id == 53
    assert result.ok is True
    assert len(result.candidates) == 1
    fields = result.candidates[0].to_raw_news_fields()
    assert fields["external_post_id"] == "51"
    media_ids = [entry["media_id"] for entry in fields["media_data"]]
    assert media_ids == ["m51", "m52", "m53"]


async def test_skip_media_source_stores_no_media_at_all() -> None:
    source = telegram_source(skip_media=True)
    payload = messages_response(
        [
            bridge_message(51, grouped_id="7712", media=[bridge_photo("m51")]),
            bridge_message(
                52, "Подпись", grouped_id="7712", media=[bridge_photo("m52")]
            ),
        ]
    )
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        mock.get(MESSAGES_PATH).mock(side_effect=[payload, messages_response([])])
        client = new_ingestor()
        try:
            result = await client.ingest_source(source, last_max_id=40)
        finally:
            await client.close()

    prepared = result.candidates[0]
    assert prepared.candidate.media == ()
    assert prepared.to_raw_news_fields()["media_data"] == []
    # The text side of the same candidate is untouched by the media opt-out.
    assert prepared.candidate.text == "Подпись"


async def test_media_metadata_normalisation_drops_unusable_attachments(
    ingestor: TelegramIngestor,
) -> None:
    bot_api_photo = {
        "photo": [
            {"file_id": "small", "file_size": 100},
            {"file_id": "large", "file_size": 900},
        ]
    }
    payload = messages_response(
        [
            bridge_message(12, "Bot API фото", extra=bot_api_photo),
            bridge_message(
                13,
                "Документ с видео-MIME",
                media=[
                    {
                        "className": "MessageMediaDocument",
                        "document": {
                            "id": 555,
                            "size": 4242,
                            "mimeType": "video/mp4",
                        },
                    }
                ],
            ),
            bridge_message(
                14,
                "Стикер",
                media=[{"className": "MessageMediaSticker", "id": "st1"}],
            ),
            bridge_message(
                15, "Без идентификатора", media=[{"type": "photo", "size": 10}]
            ),
        ]
    )
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        mock.get(MESSAGES_PATH).mock(return_value=payload)
        messages = await ingestor.fetch_messages("@moscow_news")

    by_id = {message.id: message.media for message in messages}
    # The Bot API sends size variants of one image; only the largest counts.
    assert [(item.type, item.media_id, item.size) for item in by_id[12]] == [
        ("photo", "large", 900)
    ]
    # A generic document is upgraded to video by its MIME type.
    assert [(item.type, item.media_id, item.size) for item in by_id[13]] == [
        ("video", "555", 4242)
    ]
    # Stickers carry nothing usable, and an attachment without an id could
    # never be fetched by Package 3.
    assert by_id[14] == ()
    assert by_id[15] == ()


# =========================================================== GramJS HTTP layer


async def test_overlap_window_is_applied_to_the_outgoing_min_id() -> None:
    last_max_id = 100
    expected_min_id = last_max_id - settings.ingest_overlap_messages

    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        route = mock.get(MESSAGES_PATH).mock(return_value=messages_response([]))
        client = new_ingestor()
        try:
            messages = await client.fetch_new_messages(
                "@moscow_news", last_max_id=last_max_id
            )
        finally:
            await client.close()

    assert messages == []
    params = route.calls.last.request.url.params
    assert int(params["min_id"]) == expected_min_id
    assert int(params["min_id"]) < last_max_id
    assert params["channel"] == "@moscow_news"
    assert int(params["limit"]) == settings.ingest_max_messages_per_source


async def test_overlap_window_never_goes_below_zero() -> None:
    last_max_id = max(settings.ingest_overlap_messages - 1, 0)

    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        route = mock.get(MESSAGES_PATH).mock(return_value=messages_response([]))
        client = new_ingestor()
        try:
            await client.fetch_new_messages("@moscow_news", last_max_id=last_max_id)
        finally:
            await client.close()

    assert int(route.calls.last.request.url.params["min_id"]) == 0


async def test_without_a_watermark_a_single_recent_page_is_read() -> None:
    # There is no lower bound to walk from, so pagination must not start.
    page = messages_response(
        [bridge_message(index, f"пост {index}") for index in range(1, 6)]
    )
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        route = mock.get(MESSAGES_PATH).mock(return_value=page)
        client = new_ingestor()
        try:
            messages = await client.fetch_new_messages("@moscow_news", last_max_id=None)
        finally:
            await client.close()

    assert [message.id for message in messages] == [1, 2, 3, 4, 5]
    assert len(route.calls) == 1
    assert "min_id" not in route.calls.last.request.url.params


async def test_pagination_walks_forward_and_respects_the_per_source_budget() -> None:
    budget = settings.ingest_max_messages_per_source
    first_page = [bridge_message(index, f"пост {index}") for index in range(101, 121)]
    second_page = [bridge_message(index, f"пост {index}") for index in range(121, 141)]

    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        route = mock.get(MESSAGES_PATH).mock(
            side_effect=[
                messages_response(first_page),
                messages_response(second_page),
                messages_response([]),
            ]
        )
        client = new_ingestor()
        try:
            messages = await client.fetch_new_messages("@moscow_news", last_max_id=110)
        finally:
            await client.close()

    assert [message.id for message in messages] == list(range(101, 141))
    assert len(messages) <= budget
    cursors = [int(call.request.url.params["min_id"]) for call in route.calls]
    limits = [int(call.request.url.params["limit"]) for call in route.calls]
    assert cursors == [110 - settings.ingest_overlap_messages, 120, 140]
    # The remaining budget shrinks with every page, so a channel cannot exceed
    # INGEST_MAX_MESSAGES_PER_SOURCE across a multi-page pass.
    assert limits == [budget, budget - 20, budget - 40]
    assert sum(limits[1:]) < budget


async def test_a_bridge_that_repeats_one_page_cannot_loop_forever() -> None:
    page = messages_response(
        [bridge_message(index, f"пост {index}") for index in (101, 102)]
    )

    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        route = mock.get(MESSAGES_PATH).mock(return_value=page)
        client = new_ingestor()
        try:
            messages = await client.fetch_new_messages("@moscow_news", last_max_id=200)
        finally:
            await client.close()

    # The page brings no id above the cursor, so the walk stops immediately.
    assert [message.id for message in messages] == [101, 102]
    assert len(route.calls) == 1


async def test_messages_are_returned_in_ascending_id_order(
    ingestor: TelegramIngestor,
) -> None:
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        mock.get(MESSAGES_PATH).mock(
            return_value=messages_response(
                [
                    bridge_message(30, "третий"),
                    bridge_message(10, "первый"),
                    bridge_message(20, "второй"),
                ]
            )
        )
        messages = await ingestor.fetch_messages("@moscow_news")

    assert [message.id for message in messages] == [10, 20, 30]


async def test_malformed_messages_are_skipped_and_the_page_survives(
    ingestor: TelegramIngestor,
) -> None:
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        mock.get(MESSAGES_PATH).mock(
            return_value=json_response(
                {
                    "messages": [
                        bridge_message(51, "нормальный пост"),
                        {"no_id_at_all": True},
                        "строка вместо объекта",
                        {"id": 0, "message": "нулевой идентификатор"},
                        bridge_message(52, "второй нормальный пост"),
                    ]
                }
            )
        )
        messages = await ingestor.fetch_messages("@moscow_news")

    assert [message.id for message in messages] == [51, 52]


async def test_fetch_dialogs_parses_the_listing_and_skips_a_malformed_entry(
    ingestor: TelegramIngestor,
) -> None:
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        mock.get(DIALOGS_PATH).mock(
            return_value=json_response(
                {
                    "dialogs": [
                        {
                            "id": -1_001_234_567_890,
                            "title": "Новости Москвы",
                            "username": "@moscow_news",
                            "className": "Channel",
                            "participants_count": 120_500,
                        },
                        {"title": "запись без идентификатора"},
                        "строка вместо объекта",
                        {"id": 42, "name": "Рабочий чат", "className": "Chat"},
                    ]
                }
            )
        )
        dialogs = await ingestor.fetch_dialogs()

    assert [dialog.id for dialog in dialogs] == ["-1001234567890", "42"]
    channel = dialogs[0]
    assert channel.title == "Новости Москвы"
    assert channel.username == "moscow_news"
    assert channel.is_channel is True
    assert channel.participants_count == 120_500
    chat = dialogs[1]
    assert chat.is_channel is False
    assert chat.username is None
    assert chat.participants_count is None


async def test_unauthorized_raises_and_never_echoes_the_token() -> None:
    # A realistic rejection body: the bridge reports that the credential is bad
    # without repeating it. `_status_error` quotes up to 300 characters of the
    # body, so a bridge that *did* echo the bearer token back would leak it into
    # the exception and from there into the worker log and `IngestResult.errors`.
    body = "invalid or expired bearer credential"
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        route = mock.get(MESSAGES_PATH).mock(return_value=httpx.Response(401, text=body))
        client = new_ingestor(attempts=3)
        try:
            with pytest.raises(TelegramAuthError) as caught:
                await client.fetch_messages("@moscow_news")
        finally:
            await client.close()

    error = caught.value
    assert error.status_code == 401
    # A 401 is deterministic: retrying it would only multiply the failure.
    assert len(route.calls) == 1
    assert BRIDGE_TOKEN not in str(error)
    assert BRIDGE_TOKEN not in repr(error)
    assert "GRAMJS_TOKEN" in str(error)
    # The request itself did carry the credential, so the redaction above is
    # about the error text and not about a missing header.
    sent = route.calls.last.request.headers["Authorization"]
    assert sent == f"Bearer {BRIDGE_TOKEN}"


async def test_forbidden_is_also_an_auth_error() -> None:
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        mock.get(DIALOGS_PATH).mock(return_value=httpx.Response(403, text="forbidden"))
        client = new_ingestor(attempts=2)
        try:
            with pytest.raises(TelegramAuthError) as caught:
                await client.fetch_dialogs()
        finally:
            await client.close()

    assert caught.value.status_code == 403


async def test_a_server_error_is_retried_and_then_succeeds() -> None:
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        route = mock.get(MESSAGES_PATH).mock(
            side_effect=[
                httpx.Response(500, text="internal bridge error"),
                messages_response([bridge_message(7, "после повтора")]),
            ]
        )
        client = new_ingestor(attempts=2)
        try:
            messages = await client.fetch_messages("@moscow_news")
        finally:
            await client.close()

    assert [message.id for message in messages] == [7]
    assert [message.text for message in messages] == ["после повтора"]
    assert len(route.calls) == 2


async def test_retries_are_exhausted_into_a_status_error() -> None:
    throttled = httpx.Response(
        429, headers={"Retry-After": "0"}, text="slow down"
    )
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        route = mock.get(MESSAGES_PATH).mock(side_effect=[throttled, throttled])
        client = new_ingestor(attempts=2)
        try:
            with pytest.raises(TelegramStatusError) as caught:
                await client.fetch_messages("@moscow_news")
        finally:
            await client.close()

    assert caught.value.status_code == 429
    assert len(route.calls) == 2


async def test_a_client_error_other_than_429_is_not_retried() -> None:
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        route = mock.get(MESSAGES_PATH).mock(
            return_value=httpx.Response(404, text="no such channel")
        )
        client = new_ingestor(attempts=3)
        try:
            with pytest.raises(TelegramStatusError) as caught:
                await client.fetch_messages("@deleted_channel")
        finally:
            await client.close()

    assert caught.value.status_code == 404
    assert not isinstance(caught.value, TelegramAuthError)
    assert len(route.calls) == 1


async def test_mark_read_failure_is_swallowed_into_false() -> None:
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        route = mock.post(MESSAGES_READ_PATH).mock(
            return_value=httpx.Response(500, text="read marker unavailable")
        )
        client = new_ingestor()
        try:
            # Fire-and-forget by contract: marking read is cosmetic for the
            # reader account and must never fail a collection pass.
            assert await client.mark_read("@moscow_news", 99) is False
        finally:
            await client.close()

    body: dict[str, Any] = json.loads(route.calls.last.request.content)
    assert body == {"channel": "@moscow_news", "max_id": 99}


async def test_mark_read_returns_true_on_success() -> None:
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        mock.post(MESSAGES_READ_PATH).mock(return_value=json_response({"ok": True}))
        client = new_ingestor()
        try:
            assert await client.mark_read("@moscow_news", 140) is True
        finally:
            await client.close()


async def test_mark_read_without_configuration_returns_false() -> None:
    client = new_ingestor(token="")
    try:
        assert await client.mark_read("@moscow_news", 1) is False
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("base_url", "token", "missing"),
    [
        pytest.param(BRIDGE_URL, "", "GRAMJS_TOKEN", id="empty_token"),
        pytest.param(BRIDGE_URL, "   ", "GRAMJS_TOKEN", id="blank_token"),
        pytest.param("", BRIDGE_TOKEN, "GRAMJS_API_URL", id="empty_url"),
    ],
)
async def test_an_unconfigured_bridge_raises_not_configured(
    base_url: str, token: str, missing: str
) -> None:
    client = TelegramIngestor(base_url=base_url, token=token, max_attempts=1)
    try:
        assert client.is_configured is False
        with pytest.raises(TelegramNotConfiguredError, match=missing):
            await client.fetch_dialogs()
    finally:
        await client.close()


async def test_a_non_json_body_becomes_a_response_error(
    ingestor: TelegramIngestor,
) -> None:
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        mock.get(DIALOGS_PATH).mock(
            return_value=httpx.Response(200, text="<html>not json</html>")
        )
        with pytest.raises(TelegramResponseError, match="non-JSON body"):
            await ingestor.fetch_dialogs()


async def test_a_bridge_error_payload_becomes_a_response_error(
    ingestor: TelegramIngestor,
) -> None:
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        mock.get(MESSAGES_PATH).mock(
            return_value=json_response({"error": "peer id invalid"})
        )
        with pytest.raises(TelegramResponseError, match="peer id invalid"):
            await ingestor.fetch_messages("@moscow_news")


async def test_a_transport_failure_becomes_a_transport_error() -> None:
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        mock.get(MESSAGES_PATH).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        client = new_ingestor()
        try:
            with pytest.raises(TelegramTransportError, match="ConnectError"):
                await client.fetch_messages("@moscow_news")
        finally:
            await client.close()


async def test_one_dead_channel_is_reported_not_raised() -> None:
    source = telegram_source(source_id=3, identifier="@deleted_channel")

    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        mock.get(MESSAGES_PATH).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        client = new_ingestor()
        try:
            result = await client.ingest_source(source, last_max_id=None)
        finally:
            await client.close()

    assert result.ok is False
    assert result.source_id == 3
    assert result.candidates == ()
    assert result.fetched_messages == 0
    # No watermark is reported, so `sources.last_scraped_at` stays untouched and
    # the scheduler retries the source naturally.
    assert result.max_message_id is None
    assert len(result.errors) == 1
    assert "transport failure" in result.errors[0]


@pytest.mark.parametrize(
    ("identifier", "expected_username", "expected_url"),
    [
        pytest.param(
            "@channel", "channel", "https://t.me/channel/123", id="at_prefixed"
        ),
        pytest.param("channel", "channel", "https://t.me/channel/123", id="bare"),
        pytest.param("-1001234567890", None, None, id="numeric_id"),
        pytest.param("@ab", None, None, id="too_short"),
        pytest.param("@1channel", None, None, id="leading_digit"),
    ],
)
def test_post_url_is_built_only_for_public_usernames(
    identifier: str, expected_username: str | None, expected_url: str | None
) -> None:
    assert public_username(identifier) == expected_username
    assert build_post_url(identifier, 123) == expected_url


async def test_a_private_channel_candidate_carries_no_post_url() -> None:
    source = telegram_source(source_id=8, identifier="-1001234567890")
    page = messages_response([bridge_message(5, "пост")])

    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        mock.get(MESSAGES_PATH).mock(return_value=page)
        client = new_ingestor()
        try:
            result = await client.ingest_source(source, last_max_id=None)
        finally:
            await client.close()

    assert result.candidates[0].candidate.post_url is None
    assert result.candidates[0].to_raw_news_fields()["post_url"] is None


# ======================================================== prepared-post mapping


def prepared_post(text: str, *, media: Sequence[MediaItem] = ()) -> PreparedPost:
    """Build a `PreparedPost` the way `ingest_source` does, without HTTP."""
    candidate = IngestCandidate(
        external_post_id="51",
        primary_message_id=51,
        message_ids=(51,),
        text=text,
        entities=(),
        media=tuple(media),
        post_url="https://t.me/moscow_news/51",
        published_at=None,
        is_album=False,
    )
    return PreparedPost(candidate=candidate, filter_result=analyze_post(text))


def test_prepared_fields_match_the_raw_news_columns_exactly() -> None:
    post = prepared_post(
        "Мэрия открыла новую станцию метро на Солнцевской линии.",
        media=[
            MediaItem(
                type="photo", original_post_id=51, media_id="m51", size=184_320
            ),
            MediaItem(type="video", original_post_id=52, media_id="m52", size=None),
        ],
    )

    fields = post.to_raw_news_fields()

    assert set(fields) == RAW_NEWS_FIELD_KEYS
    columns = set(RawNews.__table__.columns.keys())
    assert columns >= RAW_NEWS_FIELD_KEYS, RAW_NEWS_FIELD_KEYS - columns
    assert fields["external_post_id"] == "51"
    assert fields["post_url"] == "https://t.me/moscow_news/51"
    assert fields["raw_text"] == (
        "Мэрия открыла новую станцию метро на Солнцевской линии."
    )
    assert isinstance(fields["clean_html"], str)
    assert fields["is_digest"] is False
    assert fields["is_ad"] is False
    assert fields["filter_reasons"] == []
    # The whole mapping must survive the JSONB round trip unchanged.
    assert json.loads(json.dumps(fields, ensure_ascii=False)) == fields


def test_media_data_entries_carry_exactly_the_documented_keys() -> None:
    post = prepared_post(
        "Текст поста с медиа.",
        media=[
            MediaItem(
                type="photo",
                original_post_id=48_213,
                media_id="578901",
                size=184_320,
            ),
            MediaItem(
                type="video", original_post_id=48_214, media_id="578902", size=None
            ),
            MediaItem(
                type="document",
                original_post_id=48_215,
                media_id="578903",
                size=51_204,
            ),
        ],
    )

    media_data: list[dict[str, Any]] = post.to_raw_news_fields()["media_data"]

    assert len(media_data) == 3
    for entry in media_data:
        assert set(entry) == MEDIA_DATA_KEYS
        assert entry["type"] in {"photo", "video", "document"}
        assert isinstance(entry["original_post_id"], int)
        assert isinstance(entry["media_id"], str)
        assert entry["size"] is None or isinstance(entry["size"], int)
    assert media_data[0] == {
        "type": "photo",
        "original_post_id": 48_213,
        "media_id": "578901",
        "size": 184_320,
    }
    assert media_data[1]["size"] is None


def test_filter_reasons_fit_the_array_string_64_column() -> None:
    digest = prepared_post(DIGEST_POST)
    advert = prepared_post(AD_POST)

    for post in (digest, advert):
        reasons: list[str] = post.to_raw_news_fields()["filter_reasons"]
        assert reasons, "a rejected post must carry an audit trail"
        for reason in reasons:
            assert len(reason) <= FILTER_REASON_MAX_LENGTH, (
                f"reason {reason!r} is {len(reason)} chars and would be truncated "
                f"by filter_reasons ARRAY(String({FILTER_REASON_MAX_LENGTH}))"
            )
            assert reason == reason.strip()


def test_a_digest_post_sets_is_digest_and_should_skip() -> None:
    post = prepared_post(DIGEST_POST)
    fields = post.to_raw_news_fields()

    assert post.should_skip is True
    assert fields["is_digest"] is True
    assert fields["is_ad"] is False
    assert REASON_DIGEST_NUMBERED_LIST in fields["filter_reasons"]
    assert REASON_DIGEST_HEADLINE in fields["filter_reasons"]


def test_an_ad_post_sets_is_ad_and_should_skip() -> None:
    post = prepared_post(AD_POST)
    fields = post.to_raw_news_fields()

    assert post.should_skip is True
    assert fields["is_ad"] is True
    assert fields["is_digest"] is False
    assert set(fields["filter_reasons"]) >= {REASON_AD_ERID, REASON_AD_FOOTER}


def test_a_plain_news_post_is_not_skipped() -> None:
    post = prepared_post(
        "Власти Москвы открыли новую станцию метро «Пыхтино» на Солнцевской линии."
    )
    fields = post.to_raw_news_fields()

    assert post.should_skip is False
    assert (fields["is_digest"], fields["is_ad"]) == (False, False)
    assert fields["filter_reasons"] == []


def test_raw_text_keeps_the_signature_the_filter_strips() -> None:
    # `raw_text` is verbatim on purpose: stripping is a filter concern and the
    # stripped variant stays available on `filter_result.clean_text`.
    text = "Мэрия открыла новую станцию метро.\nПодписаться на канал"
    post = prepared_post(text)

    assert post.to_raw_news_fields()["raw_text"] == text
    assert "Подписаться" not in post.filter_result.clean_text


async def test_digest_and_ad_flags_survive_the_full_ingest_pass() -> None:
    source = telegram_source()
    with respx.mock(base_url=BRIDGE_URL, assert_all_called=True) as mock:
        mock.get(MESSAGES_PATH).mock(
            side_effect=[
                messages_response(
                    [
                        bridge_message(60, DIGEST_POST),
                        bridge_message(61, AD_POST),
                        bridge_message(62, "Мэрия открыла новую станцию метро."),
                    ]
                ),
                messages_response([]),
            ]
        )
        client = new_ingestor()
        try:
            result = await client.ingest_source(source, last_max_id=50)
        finally:
            await client.close()

    by_id = {
        post.candidate.external_post_id: post.to_raw_news_fields()
        for post in result.candidates
    }
    assert set(by_id) == {"60", "61", "62"}
    assert (by_id["60"]["is_digest"], by_id["60"]["is_ad"]) == (True, False)
    assert (by_id["61"]["is_digest"], by_id["61"]["is_ad"]) == (False, True)
    assert (by_id["62"]["is_digest"], by_id["62"]["is_ad"]) == (False, False)
    assert [post.should_skip for post in result.candidates] == [True, True, False]


# ============================================================ local fixture site

_ARTICLE_PARAGRAPHS: Final[str] = (
    "<p>Мэрия Москвы сообщила об открытии новой станции метро на Солнцевской"
    " линии. Строительство велось четыре года и обошлось городскому бюджету в"
    " 23 миллиарда рублей. Ожидается, что новым пересадочным узлом будут"
    " пользоваться около 45 тысяч пассажиров ежедневно.</p>"
    "<p>По словам мэра, интервал движения поездов в часы пик составит 90"
    " секунд, а до конца года на этой же линии откроют ещё две станции. На"
    " объекте установлены лифты и тактильная плитка для маломобильных"
    " пассажиров.</p>"
) * 2

#: An article page that references an image, a stylesheet and a font, so the
#: resource blocker has something to abort.
ARTICLE_HTML: Final[str] = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Открытие станции метро</title>
<link rel="stylesheet" href="/blocked.css">
<style>
@font-face {{ font-family: "Blocked"; src: url("/blocked.woff2") format("woff2"); }}
body {{ font-family: "Blocked", serif; }}
</style>
</head>
<body><article class="article">
<h1 class="article__title">Открыта станция метро «Пыхтино»</h1>
<time class="article__date" datetime="2026-03-14T12:30:00+03:00">14 марта 2026</time>
<div class="article__body">{_ARTICLE_PARAGRAPHS}
<img src="/blocked.png" alt="фото станции">
</div>
<aside class="related-news"><p>Читайте также: материал о ремонте дорог.</p></aside>
</article></body></html>"""

_LISTING_ITEMS: Final[str] = "".join(
    f'<article class="news-item">'
    f'<a class="news-item__link" href="{href}">{label}</a>'
    f"</article>"
    for href, label in (
        ("/news/one", "Первая"),
        ("two#comments", "Вторая"),
        ("/news/one", "Первая снова"),
        ("javascript:void(0)", "Мусор"),
    )
)

#: A listing page with a relative href, a document-relative href, a repeated
#: href and an unusable one.
LISTING_HTML: Final[str] = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Лента новостей</title></head>
<body><div class="news-feed">{_LISTING_ITEMS}</div></body></html>"""

#: An article body carrying everything a hostile page can inject into the
#: markup we persist: a script, an inline event handler, a `javascript:` link and
#: a tracking iframe, all *inside* the content container so no selector can
#: exclude them.
_HOSTILE_BODY: Final[str] = (
    "<p>Обычный абзац новости о работе городских служб, который должен"
    " сохраниться целиком после очистки разметки.</p>"
    "<script>alert('xss')</script>"
    '<p onclick="alert(1)">Абзац с обработчиком события.</p>'
    '<p><a href="javascript:alert(2)">Опасная ссылка</a></p>'
    '<p><img src="/blocked.png" onerror="alert(3)" alt="фото"></p>'
    '<iframe src="https://tracker.example/pixel"></iframe>'
    "<p>Ещё один абзац с продолжением истории и уточнением деталей"
    " произошедшего события.</p>"
)

HOSTILE_HTML: Final[str] = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Опасная страница</title></head>
<body><article class="article">
<h1 class="article__title">Опасная страница</h1>
<div class="article__body">{_HOSTILE_BODY}</div>
</article></body></html>"""

#: A body whose paragraphs are bare text nodes: there is no inner markup to
#: keep, so the scraper must fall back to wrapping the text in `<p>` tags. The
#: literal `<b>` is written as an entity so the *source text* contains the
#: characters and the escaping of the fallback is observable.
BARE_HTML: Final[str] = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Без разметки</title></head>
<body><article class="article">
<h1 class="article__title">Материал без разметки</h1>
<div class="article__body">Первый абзац без разметки, в котором описывается
событие и приводятся подробности произошедшего в городе за прошедшие сутки.

Второй абзац содержит текст &lt;b&gt;с угловыми скобками&lt;/b&gt; и продолжает
рассказ о том, как именно развивалась ситуация на месте.</div>
</article></body></html>"""

#: Paths the fixture site serves. Everything else is a 404.
_SITE_ROUTES: Final[Mapping[str, tuple[bytes, str]]] = {
    "/news/article": (ARTICLE_HTML.encode(), "text/html; charset=utf-8"),
    "/news/hostile": (HOSTILE_HTML.encode(), "text/html; charset=utf-8"),
    "/news/bare": (BARE_HTML.encode(), "text/html; charset=utf-8"),
    "/news/list": (LISTING_HTML.encode(), "text/html; charset=utf-8"),
    "/blocked.css": (b"body{color:#111}", "text/css"),
    "/blocked.png": (b"\x89PNG\r\n\x1a\n", "image/png"),
    "/blocked.woff2": (b"wOF2", "font/woff2"),
}


class FixtureSite:
    """A local HTTP server that records every path it is asked for.

    Bound to `127.0.0.1` on an ephemeral port, so the browser tests need no
    external network. The recorded paths are what makes resource blocking
    observable: an aborted request never reaches the server.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requested: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                """Silence the default stderr access log."""

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                site.record(path)
                route = _SITE_ROUTES.get(path)
                if route is None:
                    self.send_error(404, "not found")
                    return
                body, content_type = route
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        host, port = self._server.socket.getsockname()[:2]
        self.base_url = f"http://{host}:{port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fixture-site", daemon=True
        )
        self._thread.start()

    def record(self, path: str) -> None:
        """Called from the server thread, so the list is mutex-protected."""
        with self._lock:
            self._requested.append(path)

    @property
    def requested(self) -> tuple[str, ...]:
        """Every path the server has been asked for, in arrival order."""
        with self._lock:
            return tuple(self._requested)

    def reset(self) -> None:
        with self._lock:
            self._requested.clear()

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)
        assert not self._thread.is_alive(), "the fixture site thread did not stop"


@pytest.fixture
def site() -> Iterator[FixtureSite]:
    """A local fixture site, shut down after the test."""
    server = FixtureSite()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
async def browser_pool() -> AsyncIterator[BrowserPool]:
    """The process-wide pool, torn down through `close_browser_pool()`.

    Chromium is launched eagerly so a machine without a browser skips instead of
    failing halfway through a test, and the pool is always closed: a leaked
    browser process keeps the driver's pipes open and surfaces as a
    `ResourceWarning`, which this suite treats as an error.
    """
    pool = get_browser_pool()
    try:
        try:
            await pool.start()
        except Exception as exc:  # noqa: BLE001 - bare environment fallback
            pytest.skip(f"Chromium is unavailable: {type(exc).__name__}: {exc}")
        assert pool.is_running is True
        yield pool
    finally:
        await close_browser_pool()
        assert pool.is_running is False
        assert pool.active_pages == 0


ARTICLE_RULES: Final[Mapping[str, Any]] = {
    "title_selector": "h1.article__title",
    "content_selector": "div.article__body",
    "date_selector": "time.article__date",
    "media_selector": "div.article__body img",
    "exclude_selectors": ["aside.related-news"],
}

LISTING_RULES: Final[Mapping[str, Any]] = {
    "list_selector": "div.news-feed article.news-item",
    "link_selector": "a.news-item__link",
}

#: Rules for the hostile and bare fixtures: the body selector deliberately keeps
#: the injected nodes in scope, so sanitisation is what has to remove them.
HOSTILE_RULES: Final[Mapping[str, Any]] = {
    "title_selector": "h1.article__title",
    "content_selector": "div.article__body",
}

BARE_RULES: Final[Mapping[str, Any]] = HOSTILE_RULES


# ================================================= Playwright pool and blocking


def test_the_browser_pool_is_a_strict_singleton() -> None:
    # `_scrape` reaches for `get_browser_pool()` on every call, so a second
    # instance here would mean a second Chromium process on a 4 GB VPS.
    assert get_browser_pool() is get_browser_pool()
    assert isinstance(get_browser_pool(), BrowserPool)
    assert isinstance(STEALTH_AVAILABLE, bool)


@pytest.mark.slow
async def test_page_concurrency_never_exceeds_the_configured_bound(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    limit = settings.playwright_max_concurrent_pages
    requested_pages = limit * 3
    # Sampled from inside the running tasks, i.e. while pages are actually
    # checked out, not after the permits have been handed back.
    samples: list[int] = []
    finished = asyncio.Event()

    async def sample_until_done() -> None:
        # `sleep(0)` yields to the event loop instead of waiting: the sampler
        # therefore observes the pool at every scheduling point of every scrape,
        # with no timing assumption anywhere.
        while not finished.is_set():
            samples.append(browser_pool.active_pages)
            await asyncio.sleep(0)

    async def scrape_one() -> ScrapedArticle:
        article = await scrape_article(site.url("/news/article"), ARTICLE_RULES)
        samples.append(browser_pool.active_pages)
        return article

    sampler = asyncio.create_task(sample_until_done())
    scrapes = [asyncio.create_task(scrape_one()) for _ in range(requested_pages)]
    try:
        articles = await asyncio.gather(*scrapes)
    finally:
        finished.set()
        await sampler

    assert len(articles) == requested_pages
    assert all(article.text for article in articles)
    peak = max(samples)
    assert peak <= limit, (
        f"the pool held {peak} concurrent pages with "
        f"PLAYWRIGHT_MAX_CONCURRENT_PAGES={limit}"
    )
    # More work than permits was in flight, and the pool did reach its bound, so
    # the cap was genuinely exercised instead of being satisfied by a serial run
    # that never asked for a second page.
    assert requested_pages > limit
    assert peak == limit, (
        f"the pool never held more than {peak} page(s) while {requested_pages} "
        f"scrapes were in flight, so the bound of {limit} was never tested"
    )
    assert len(samples) > requested_pages
    assert browser_pool.active_pages == 0
    # One browser served every page: the semaphore bounds pages, not browsers.
    assert browser_pool.is_running is True


@pytest.mark.slow
async def test_images_fonts_and_stylesheets_never_reach_the_server(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    assert settings.playwright_block_resources is True, (
        "PLAYWRIGHT_BLOCK_RESOURCES is off in this environment, so the blocker "
        "under test would not run"
    )
    site.reset()

    article = await scrape_article(site.url("/news/article"), ARTICLE_RULES)

    requested = site.requested
    assert "/news/article" in requested, "the document itself must be fetched"
    for blocked in ("/blocked.png", "/blocked.css", "/blocked.woff2"):
        assert blocked not in requested, (
            f"{blocked} was downloaded despite PLAYWRIGHT_BLOCK_RESOURCES"
        )
    assert requested == ("/news/article",)
    # The bytes were never fetched, but the URL stays readable from the DOM,
    # which is exactly what Package 3 needs.
    assert article.media_urls == (site.url("/blocked.png"),)


@pytest.mark.slow
async def test_explicit_rules_win_and_exclusions_are_removed(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    article = await scrape_article(site.url("/news/article"), ARTICLE_RULES)

    assert article.strategy == "rules"
    assert article.title == "Открыта станция метро «Пыхтино»"
    assert len(article.text) >= MIN_CONTENT_LENGTH
    assert article.text.startswith("Мэрия Москвы сообщила")
    # The excluded aside is gone from the extracted body.
    assert "Читайте также" not in article.text
    assert article.published_at is not None
    assert article.published_at.date().isoformat() == "2026-03-14"
    assert article.published_at.hour == 12
    assert article.url == site.url("/news/article")
    assert article.extracted_at.tzinfo is not None


@contextmanager
def trafilatura_fallback_enabled() -> Iterator[None]:
    """Let the Trafilatura fallback run under `filterwarnings = ["error"]`.

    `web_scraper._run_trafilatura` calls `bare_extraction(..., as_dict=True)`,
    and trafilatura 2.0 issues a `PendingDeprecationWarning` for that argument.
    The suite promotes warnings to errors, the extractor catches every exception
    from the parser by design, and the fallback would therefore return `None`
    for reasons that have nothing to do with the code under test. Downgrading
    that one warning — and only inside these blocks — is what makes the fallback
    observable here; production does not run with `-W error`.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "default",
            message='"as_dict" will be deprecated',
            category=PendingDeprecationWarning,
        )
        yield


@pytest.mark.slow
async def test_without_rules_trafilatura_produces_the_body(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    with trafilatura_fallback_enabled():
        article = await scrape_article(site.url("/news/article"))

    assert article.strategy == "trafilatura"
    assert len(article.text) >= MIN_CONTENT_LENGTH
    assert "Мэрия Москвы сообщила" in article.text
    # Trafilatura also recovers the headline and the publication date from the
    # document itself, with no selectors configured.
    assert article.title == "Открыта станция метро «Пыхтино»"
    assert article.published_at is not None
    assert article.published_at.date().isoformat() == "2026-03-14"


@pytest.mark.slow
async def test_a_dead_content_selector_falls_back_to_trafilatura(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    with trafilatura_fallback_enabled():
        article = await scrape_article(
            site.url("/news/article"),
            {
                "title_selector": "h1.article__title",
                "content_selector": "div.absent-body",
            },
        )

    # The title still came from the rules, the body from the fallback: the
    # scraper reports that combination as the hybrid strategy.
    assert article.strategy == "hybrid"
    assert article.title == "Открыта станция метро «Пыхтино»"
    assert len(article.text) >= MIN_CONTENT_LENGTH


@pytest.mark.slow
async def test_discover_links_resolves_and_deduplicates(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    links = await discover_links(site.url("/news/list"), LISTING_RULES)

    assert links == [site.url("/news/one"), site.url("/news/two")]
    assert all(link.startswith(site.base_url) for link in links)
    # The repeated href collapsed, the fragment was dropped, and the
    # `javascript:` href was rejected outright.
    assert len(links) == len(set(links))
    assert not any("#" in link for link in links)
    assert not any("javascript" in link for link in links)


@pytest.mark.slow
async def test_discover_links_honours_max_items(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    links = await discover_links(
        site.url("/news/list"), {**LISTING_RULES, "max_items": 1}
    )

    assert links == [site.url("/news/one")]


@pytest.mark.slow
async def test_test_crawl_url_reports_bad_selectors_as_warnings(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    preview = await crawl_url_preview(
        site.url("/news/article"),
        {
            "title_selector": "h1.deliberately-wrong",
            "content_selector": "div.article__body",
            "date_selector": "time.deliberately-wrong",
            "media_selector": "img.deliberately-wrong",
        },
    )

    assert isinstance(preview, CrawlPreview)
    assert preview.warnings, "a dead selector must be reported, not swallowed"
    assert any("title_selector" in warning for warning in preview.warnings)
    assert any("date_selector" in warning for warning in preview.warnings)
    assert any("media_selector" in warning for warning in preview.warnings)
    # The working selector still produced a body, and the run did not raise.
    assert preview.strategy == "rules"
    assert preview.text_length >= MIN_CONTENT_LENGTH
    assert preview.media_urls == ()
    assert preview.published_at is None
    # The wrong title selector fell back to the document title.
    assert preview.title == "Открытие станции метро"
    assert preview.duration_ms >= 0


@pytest.mark.slow
async def test_test_crawl_url_reports_a_navigation_failure_instead_of_raising(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    preview = await crawl_url_preview(site.url("/news/missing"), {})

    assert preview.strategy == "failed"
    assert preview.text_length == 0
    assert preview.warnings
    assert "404" in preview.warnings[0]


@pytest.mark.slow
async def test_a_failed_scrape_releases_its_page_permit(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    # A leaked permit would wedge the pool for the rest of the worker's life,
    # so the release path matters more than the error itself.
    for _ in range(settings.playwright_max_concurrent_pages + 1):
        with pytest.raises(ScraperError):
            await scrape_article(site.url("/news/missing"))
        assert browser_pool.active_pages == 0

    article = await scrape_article(site.url("/news/article"), ARTICLE_RULES)
    assert article.text


# ==================================================== content_html contract

# `ScrapedArticle.content_html` is persisted to `raw_news.clean_html` and
# rendered later by the dashboard and the publishers, so these tests pin two
# properties: it is always populated when a body was extracted (a missing value
# raised `TypeError` at construction time before), and it never carries
# executable markup out of a hostile page.


@pytest.mark.slow
async def test_rules_path_captures_the_article_body_html(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    article = await scrape_article(site.url("/news/article"), ARTICLE_RULES)

    assert article.strategy == "rules"
    assert article.content_html, "the rules path must capture the body markup"
    # Real markup, not the escaped-text fallback: the paragraphs of the source
    # survive as tags.
    assert "<p>" in article.content_html
    assert "Мэрия Москвы сообщила" in article.content_html
    # The body only: the excluded aside and the page chrome are absent.
    assert "Читайте также" not in article.content_html
    assert "<html" not in article.content_html.lower()
    assert "article__title" not in article.content_html


@pytest.mark.slow
async def test_trafilatura_path_captures_the_article_body_html(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    with trafilatura_fallback_enabled():
        article = await scrape_article(site.url("/news/article"))

    assert article.strategy == "trafilatura"
    assert article.content_html, "the fallback must also produce body markup"
    assert "<p>" in article.content_html
    assert "Мэрия Москвы сообщила" in article.content_html
    # A fragment, not a document: `content_html` is embedded into our templates.
    assert "<body" not in article.content_html.lower()


@pytest.mark.slow
async def test_content_html_never_carries_executable_markup(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    # The fixture article body contains an injected script, an inline handler
    # and a `javascript:` link (see `_HOSTILE_BODY`).
    article = await scrape_article(site.url("/news/hostile"), HOSTILE_RULES)

    body_html = article.content_html
    assert body_html, "a hostile page still has a legitimate body"
    lowered = body_html.lower()
    assert "<script" not in lowered
    assert "<iframe" not in lowered
    assert "onerror" not in lowered
    assert "onclick" not in lowered
    assert "javascript:" not in lowered
    # The script's *body text* is gone too, not merely its tags: a stripped tag
    # would leave `alert('xss')` as visible prose.
    assert "alert(" not in lowered
    # The legitimate content around it survived.
    assert "Обычный абзац новости" in body_html
    assert "<p>" in body_html


@pytest.mark.slow
async def test_clean_html_receives_the_sanitized_article_html(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    article = await scrape_article(site.url("/news/hostile"), HOSTILE_RULES)
    candidate = _Candidate.from_article(article, skip_media=False)

    stored = candidate.insert_values(source_id=1)["clean_html"]
    # `clean_html` is the article markup, not escaped plain text: the tags are
    # live so Package 3 and the publishers keep the story's structure.
    assert "<p>" in stored
    assert "&lt;p&gt;" not in stored
    # ...and it is the sanitised markup, not the raw DOM.
    assert "<script" not in stored.lower()
    assert "javascript:" not in stored.lower()
    assert "alert(" not in stored.lower()
    # The headline is carried into the markup, matching `raw_text`.
    assert "Опасная страница" in stored


@pytest.mark.slow
async def test_a_text_only_body_still_yields_paragraph_html(
    site: FixtureSite, browser_pool: BrowserPool
) -> None:
    # A body of bare text nodes produces no inner markup worth keeping, so the
    # scraper wraps the paragraphs instead of storing an empty fragment.
    article = await scrape_article(site.url("/news/bare"), BARE_RULES)

    assert article.text
    assert article.content_html.startswith("<p>")
    assert "Первый абзац без разметки" in article.content_html
    # Source-controlled text is escaped, never re-interpreted as markup.
    assert "<b>" not in article.content_html
    assert "&lt;b&gt;" in article.content_html


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "chrome://settings", "not-a-url", "ftp://example.ru/x"],
)
async def test_non_http_targets_are_refused_before_the_browser_starts(
    url: str,
) -> None:
    # No browser fixture on purpose: the guard must reject the URL before any
    # page is opened, so this also runs in an environment without Chromium.
    with pytest.raises(ScraperError, match="non-http"):
        await scrape_article(url)
    with pytest.raises(ScraperError, match="non-http"):
        await discover_links(url, {})
