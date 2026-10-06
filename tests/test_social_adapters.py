"""Tests for the four pluggable social adapters.

No external services: every platform API is served by `respx`, which intercepts the
transport, so there is no DNS lookup and no socket.

The OK signature is checked against a **hand-computed** MD5 rather than against the
implementation, because a signature test that calls the same function it verifies
proves only that the function is deterministic. Telegram's caption cap is checked in
UTF-16 code units, which is how Telegram counts and where a naive `len()` test would
pass while production rejected the message.
"""

# Fixtures are Russian editorial copy, so Cyrillic letters that look like Latin
# ones are the subject matter.
# ruff: noqa: RUF001
#
# S106: every credential in this file is a fabricated placeholder passed to an
# adapter constructor. There are no real secrets here, and the token arguments are
# exactly what these tests exist to exercise.
# ruff: noqa: S106

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pytest
import respx

from app.models.article import Article
from app.models.enums import Platform, PlatformStatus
from app.services.social_base import (
    MAX_HASHTAGS,
    BaseSocialAdapter,
    PublishResult,
    SocialAdapterError,
    SocialAuthError,
    SocialRateLimitError,
    SocialResponseError,
    build_hashtags,
    build_teaser,
)
from app.services.social_browser_publisher import build_social_browser_adapters
from app.services.social_dzen import (
    AUTH_STATE_TTL_SECONDS,
    DzenAdapter,
    DzenAuthState,
    build_dzen_html,
    build_publication_payload,
)
from app.services.social_ok import (
    OKAdapter,
    build_media_topic,
    calculate_signature,
    derive_session_secret,
)
from app.services.social_telegram import (
    MAX_CAPTION_LENGTH,
    MAX_MESSAGE_LENGTH,
    TelegramAdapter,
    build_caption,
    build_inline_keyboard,
    caption_length,
    escape_html,
)
from app.services.social_vk import (
    MAX_POST_LENGTH,
    VKWallAdapter,
    build_attachments,
    build_wall_message,
)

TG_URL: Final[str] = "https://tg.test"
TG_TOKEN: Final[str] = "123456:AAtest-bot-token"  # noqa: S105 - fake test credential
TG_CHANNEL: Final[str] = "@svodkapress"

VK_URL: Final[str] = "https://vk-api.test/method"
VK_TOKEN: Final[str] = "vk1.a.social-token"  # noqa: S105 - fake test credential

OK_URL: Final[str] = "https://ok-api.test"
OK_TOKEN: Final[str] = "ok-access-token"  # noqa: S105 - fake test credential
OK_APP_KEY: Final[str] = "CBAXXXXXXXABABABA"
OK_SECRET: Final[str] = "ok-session-secret"  # noqa: S105 - fake test credential
OK_GROUP: Final[str] = "70000000000001"

DZEN_URL: Final[str] = "https://dzen.test/api/v3/publisher"
DZEN_TOKEN: Final[str] = "dzen-oauth-token"  # noqa: S105 - fake test credential
DZEN_CHANNEL: Final[str] = "svodkapress"

SITE_URL: Final[str] = "https://svodkapress.test/news/pyhtino"

TITLE: Final[str] = "В Москве открыли станцию метро «Пыхтино»"
BODY: Final[str] = (
    "<h2>Что открыли</h2>"
    "<p>Мэрия Москвы открыла станцию «Пыхтино» на Солнцевской линии 14 марта "
    "2026 года. Строительство обошлось бюджету в 23 млрд рублей.</p>"
    "<ul><li>45 тысяч пассажиров в сутки</li><li>интервал 90 секунд</li></ul>"
    "<h2>Как строили</h2>"
    "<p>Работы велись четыре года, на площадке было занято до 1200 рабочих.</p>"
)
DESCRIPTION: Final[str] = "Мэрия открыла «Пыхтино»: 23 млрд рублей, 45 тысяч в сутки."
COVER_URL: Final[str] = "https://svodkapress.test/uploads/cover.jpg"


def build_article(**overrides: Any) -> Article:
    """Build a transient `Article`. Nothing is persisted."""
    values: dict[str, Any] = {
        "id": 5150,
        "cluster_id": None,
        "category": "novosti-obshhestva",
        "title": TITLE,
        "content_html": BODY,
        "seo_title": "Станция «Пыхтино» открыта",
        "seo_description": DESCRIPTION,
        "seo_keywords": ["станция пыхтино", "метро москвы"],
        "video_links": [],
    }
    values.update(overrides)
    return Article(**values)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse backoff in every adapter so retry paths run instantly."""
    for module in (
        "app.services.social_telegram",
        "app.services.social_vk",
        "app.services.social_ok",
        "app.services.social_dzen",
    ):
        monkeypatch.setattr(f"{module}.INITIAL_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr("app.services.social_telegram.MAX_RETRY_AFTER_SECONDS", 0.0)
    for module in (
        "app.services.social_vk",
        "app.services.social_ok",
        "app.services.social_dzen",
    ):
        monkeypatch.setattr(f"{module}.MAX_BACKOFF_SECONDS", 0.0)


# ============================================================== shared contract


def test_a_published_result_matches_the_required_shape() -> None:
    result = PublishResult.published("tg", external_id="42", url="https://t.me/x/42")

    payload = result.to_dict()

    assert set(payload) == {"platform", "external_id", "url", "status", "error"}
    assert payload["status"] == PlatformStatus.PUBLISHED.value
    assert payload["error"] is None
    assert result.is_published is True


def test_a_failed_result_carries_its_retryability() -> None:
    result = PublishResult.failed("vk", error="rate limited", retryable=True)

    assert result.status == PlatformStatus.FAILED.value
    assert result.retryable is True
    assert result.is_published is False


def test_an_unconfigured_platform_is_disabled_not_failed() -> None:
    result = PublishResult.disabled("ok", reason="no token")

    # A missing token is a deployment choice; reporting it as FAILED would make a
    # deliberately partial deployment look permanently broken.
    assert result.status == PlatformStatus.DISABLED.value
    assert result.retryable is False


def test_a_result_maps_onto_the_platform_statuses_column() -> None:
    result = PublishResult.published("tg", external_id="42", url="https://t.me/x/42")

    entry = result.to_platform_status(attempts=2)

    assert set(entry) == {
        "status",
        "external_id",
        "url",
        "error",
        "attempts",
        "published_at",
    }
    assert entry["attempts"] == 2
    assert entry["published_at"] is not None


def test_empty_ids_become_null_in_the_column() -> None:
    entry = PublishResult.failed("vk", error="x").to_platform_status()

    # The column's default shape uses null, not "".
    assert entry["external_id"] is None
    assert entry["url"] is None


def test_a_teaser_prefers_the_description() -> None:
    teaser = build_teaser(
        title=TITLE, body_html=BODY, limit=400, description=DESCRIPTION
    )

    # The description is already a self-contained summary; a body's first paragraph
    # often opens mid-context.
    assert teaser == DESCRIPTION


def test_a_teaser_trims_at_a_sentence_boundary() -> None:
    text = "<p>Первое предложение. Второе предложение. Третье предложение.</p>"
    teaser = build_teaser(title="T", body_html=text, limit=40)

    assert teaser.endswith(".")
    assert "Третье" not in teaser
    assert len(teaser) <= 40


def test_a_teaser_without_a_sentence_boundary_cuts_at_a_word() -> None:
    teaser = build_teaser(title="T", body_html="<p>" + "слово " * 40 + "</p>", limit=50)

    assert len(teaser) <= 51  # the ellipsis
    assert teaser.endswith("…")
    assert not teaser.endswith("сл…")


def test_a_teaser_excludes_the_title() -> None:
    # Every adapter renders the title separately, so including it here would
    # duplicate it in the post.
    teaser = build_teaser(title=TITLE, body_html=BODY, limit=500)

    assert not teaser.startswith(TITLE)


def test_hashtags_join_multiword_phrases() -> None:
    tags = build_hashtags(["станция пыхтино", "метро москвы"])

    # `#станцияпыхтино` is specific; `#станция #пыхтино` competes with unrelated
    # content.
    assert tags == ["#станцияпыхтино", "#метромосквы"]


def test_hashtags_are_capped_and_deduplicated() -> None:
    tags = build_hashtags(["метро", "Метро", "МЕТРО!", *[f"тег{i}" for i in range(10)]])

    assert len(tags) <= MAX_HASHTAGS
    assert len(set(tags)) == len(tags)


def test_one_character_keywords_are_dropped() -> None:
    assert build_hashtags(["a", "—", "метро"]) == ["#метро"]


class _BrokenAdapter(BaseSocialAdapter):
    """An adapter that raises something unexpected."""

    platform = Platform.TELEGRAM

    @property
    def is_configured(self) -> bool:
        return True

    async def publish(
        self, article: Article, wp_post_url: str, **kwargs: Any
    ) -> PublishResult:
        raise ZeroDivisionError("a bug in the adapter")


class _TypedFailureAdapter(BaseSocialAdapter):
    """An adapter that raises a typed adapter error."""

    platform = Platform.VK

    def __init__(self, error: SocialAdapterError) -> None:
        self._error = error

    @property
    def is_configured(self) -> bool:
        return True

    async def publish(
        self, article: Article, wp_post_url: str, **kwargs: Any
    ) -> PublishResult:
        raise self._error


async def test_safe_publish_survives_an_adapter_bug() -> None:
    result = await _BrokenAdapter().safe_publish(build_article(), SITE_URL)

    # A Dzen regression must not stop Telegram posts.
    assert result.status == PlatformStatus.FAILED.value
    assert "ZeroDivisionError" in (result.error or "")
    assert result.retryable is False


@pytest.mark.parametrize(
    ("error", "expected_retryable"),
    [
        (SocialRateLimitError("throttled"), True),
        (SocialAuthError("revoked"), False),
        (SocialResponseError("5xx", retryable=True), True),
        (SocialResponseError("bad request", retryable=False), False),
    ],
)
async def test_safe_publish_maps_retryability(
    error: SocialAdapterError, expected_retryable: bool
) -> None:
    adapter = _TypedFailureAdapter(error)

    result = await adapter.safe_publish(build_article(), SITE_URL)

    assert result.retryable is expected_retryable


async def test_safe_publish_skips_an_unconfigured_adapter() -> None:
    class _Unconfigured(_BrokenAdapter):
        @property
        def is_configured(self) -> bool:
            return False

    result = await _Unconfigured().safe_publish(build_article(), SITE_URL)

    # No wasted HTTP request, and the platform is not reported as broken.
    assert result.status == PlatformStatus.DISABLED.value


# ================================================================== Telegram


def new_telegram() -> TelegramAdapter:
    return TelegramAdapter(
        bot_token=TG_TOKEN, channel_id=TG_CHANNEL, base_url=TG_URL
    )


def tg_ok(**result: Any) -> httpx.Response:
    payload: dict[str, Any] = {
        "message_id": 777,
        "chat": {"id": -1001234567890, "username": "svodkapress"},
    }
    payload.update(result)
    return httpx.Response(200, json={"ok": True, "result": payload})


def tg_error(
    code: int, description: str, *, retry_after: int | None = None
) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if retry_after is not None:
        body["parameters"] = {"retry_after": retry_after}
    return httpx.Response(200, json=body)


def test_the_caption_fits_the_1024_limit() -> None:
    caption = build_caption(
        title=TITLE,
        body_html="<p>" + "Текст новости о городском транспорте. " * 60 + "</p>",
        url=SITE_URL,
        hashtags=["#метро", "#пыхтино"],
    )

    assert caption_length(caption) <= MAX_CAPTION_LENGTH


def test_the_caption_limit_is_measured_in_utf16_units() -> None:
    # Telegram counts like JavaScript: an emoji costs two. A `len()`-based check
    # would pass here while Telegram rejected the message.
    assert len("🙂") == 1
    assert caption_length("🙂") == 2

    caption = build_caption(
        title="Заголовок",
        body_html="<p>" + "🙂" * 2000 + "</p>",
        url=SITE_URL,
    )

    assert caption_length(caption) <= MAX_CAPTION_LENGTH


def test_the_caption_leads_with_a_bold_headline() -> None:
    caption = build_caption(title=TITLE, body_html=BODY, url=SITE_URL)

    assert caption.startswith("<b>")
    assert "В Москве открыли" in caption


def test_the_caption_escapes_html_metacharacters() -> None:
    caption = build_caption(
        title="Цены & тарифы <выросли>", body_html=BODY, url=SITE_URL
    )

    # An unescaped `&` or `<` makes Telegram reject the whole message.
    assert "&amp;" in caption
    assert "&lt;выросли&gt;" in caption


def test_unsupported_tags_are_stripped_from_the_teaser() -> None:
    caption = build_caption(
        title="Заголовок",
        body_html="<h2>Подзаголовок</h2><p>Текст новости здесь.</p>",
        url=SITE_URL,
        description="",
    )

    # A surviving `<h2>` fails the parse with "can't parse entities".
    assert "<h2>" not in caption
    assert "&lt;h2&gt;" not in caption


def test_escape_html_leaves_quotes_alone() -> None:
    # Escaping quotes produces visible `&quot;` in a caption.
    assert escape_html('«Пыхтино" & <b>') == '«Пыхтино" &amp; &lt;b&gt;'


def test_the_inline_keyboard_has_the_documented_schema() -> None:
    markup = build_inline_keyboard(SITE_URL)

    assert markup == {
        "inline_keyboard": [[{"text": "Читать на сайте", "url": SITE_URL}]]
    }


def test_the_caption_link_is_omitted_when_a_button_is_used() -> None:
    with_link = build_caption(
        title=TITLE, body_html=BODY, url=SITE_URL, include_link=True
    )
    without = build_caption(
        title=TITLE, body_html=BODY, url=SITE_URL, include_link=False
    )

    assert "Читать полностью" in with_link
    # The button already carries the CTA; a duplicate link wastes caption budget.
    assert "Читать полностью" not in without


async def test_a_photo_post_carries_the_caption_and_button() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{TG_URL}/bot{TG_TOKEN}/sendPhoto").mock(
            return_value=tg_ok()
        )
        adapter = new_telegram()
        try:
            result = await adapter.publish(
                build_article(), SITE_URL, photo_url=COVER_URL
            )
        finally:
            await adapter.close()

    body = json.loads(route.calls[0].request.content)

    assert body["chat_id"] == TG_CHANNEL
    assert body["photo"] == COVER_URL
    assert body["parse_mode"] == "HTML"
    assert caption_length(body["caption"]) <= MAX_CAPTION_LENGTH
    assert body["reply_markup"]["inline_keyboard"][0][0]["url"] == SITE_URL

    assert result.is_published
    assert result.external_id == "777"
    assert result.url == "https://t.me/svodkapress/777"


async def test_an_article_without_a_cover_is_sent_as_text() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{TG_URL}/bot{TG_TOKEN}/sendMessage").mock(
            return_value=tg_ok()
        )
        adapter = new_telegram()
        try:
            result = await adapter.publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    body = json.loads(route.calls[0].request.content)

    # A published text post beats an unpublished perfect one.
    assert caption_length(body["text"]) <= MAX_MESSAGE_LENGTH
    assert result.is_published
    assert any("text message" in warning for warning in result.warnings)


async def test_a_rejected_photo_falls_back_to_text() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{TG_URL}/bot{TG_TOKEN}/sendPhoto").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "failed to get HTTP URL content",
                },
            )
        )
        text_route = mock.post(f"{TG_URL}/bot{TG_TOKEN}/sendMessage").mock(
            return_value=tg_ok()
        )
        adapter = new_telegram()
        try:
            result = await adapter.publish(
                build_article(), SITE_URL, photo_url=COVER_URL
            )
        finally:
            await adapter.close()

    assert text_route.call_count == 1
    assert result.is_published
    assert any("photo rejected" in warning for warning in result.warnings)


async def test_a_429_honours_the_reported_retry_after() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{TG_URL}/bot{TG_TOKEN}/sendMessage").mock(
            side_effect=[tg_error(429, "Too Many Requests", retry_after=1), tg_ok()]
        )
        adapter = new_telegram()
        try:
            result = await adapter.publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    # Telegram states the exact wait; guessing our own is how a bot's limit gets
    # tightened.
    assert route.call_count == 2
    assert result.is_published


async def test_a_sustained_429_is_reported_as_retryable() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{TG_URL}/bot{TG_TOKEN}/sendMessage").mock(
            return_value=tg_error(429, "Too Many Requests", retry_after=1)
        )
        adapter = new_telegram()
        try:
            result = await adapter.safe_publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    assert result.status == PlatformStatus.FAILED.value
    assert result.retryable is True


@pytest.mark.parametrize("description", ["chat not found", "bot was blocked by the user"])
async def test_a_permanent_telegram_error_is_not_retryable(description: str) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{TG_URL}/bot{TG_TOKEN}/sendMessage").mock(
            return_value=tg_error(400, description)
        )
        adapter = new_telegram()
        try:
            result = await adapter.safe_publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    assert result.retryable is False
    assert route.call_count == 1


async def test_a_401_is_an_auth_failure() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{TG_URL}/bot{TG_TOKEN}/sendMessage").mock(
            return_value=tg_error(401, "Unauthorized")
        )
        adapter = new_telegram()
        try:
            result = await adapter.safe_publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    assert result.retryable is False
    assert "token" in (result.error or "").lower()


async def test_a_numeric_channel_yields_no_permalink() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{TG_URL}/bot{TG_TOKEN}/sendMessage").mock(
            return_value=tg_ok(chat={"id": -1001234567890})
        )
        adapter = TelegramAdapter(
            bot_token=TG_TOKEN, channel_id="-1001234567890", base_url=TG_URL
        )
        try:
            result = await adapter.publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    # A private channel has no t.me permalink; a fabricated URL would 404.
    assert result.is_published
    assert result.url == ""


async def test_an_http_cta_url_omits_the_button() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{TG_URL}/bot{TG_TOKEN}/sendMessage").mock(
            return_value=tg_ok()
        )
        adapter = new_telegram()
        try:
            result = await adapter.publish(build_article(), "http://insecure.test/a")
        finally:
            await adapter.close()

    # Telegram rejects a non-https URL button outright.
    assert "reply_markup" not in json.loads(route.calls[0].request.content)
    assert any("https" in warning for warning in result.warnings)


async def test_an_unconfigured_telegram_adapter_is_disabled() -> None:
    adapter = TelegramAdapter(bot_token="", channel_id="", base_url=TG_URL)

    result = await adapter.safe_publish(build_article(), SITE_URL)

    assert result.status == PlatformStatus.DISABLED.value


async def test_the_bot_token_never_appears_in_an_error() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{TG_URL}/bot{TG_TOKEN}/sendMessage").mock(
            return_value=tg_error(400, "Bad Request: something went wrong")
        )
        adapter = new_telegram()
        try:
            result = await adapter.safe_publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    # The token sits in the URL path, so a leaked error string would leak it.
    assert TG_TOKEN not in (result.error or "")


# ======================================================================== VK


def new_vk() -> VKWallAdapter:
    return VKWallAdapter(
        access_token=VK_TOKEN, group_id=222333, base_url=VK_URL
    )


def vk_ok(post_id: int = 4242) -> httpx.Response:
    return httpx.Response(200, json={"response": {"post_id": post_id}})


def vk_error(code: int, message: str = "boom") -> httpx.Response:
    # Note the HTTP 200: VK reports application errors in the body.
    return httpx.Response(
        200, json={"error": {"error_code": code, "error_msg": message}}
    )


def test_a_video_attachment_is_serialised_with_its_owner_sign() -> None:
    attachments = build_attachments(
        video_links=[
            {"platform": "vk", "owner_id": -222333, "video_id": 777, "access_key": "ak"}
        ],
        link_url=SITE_URL,
    )

    # A negative owner id addresses community media; dropping the sign addresses a
    # user's media of the same numeric id.
    assert attachments == f"video-222333_777_ak,{SITE_URL}"


def test_a_video_without_an_access_key_omits_the_suffix() -> None:
    attachments = build_attachments(
        video_links=[{"platform": "vk", "owner_id": -1, "video_id": 2}]
    )

    assert attachments == "video-1_2"


def test_photos_are_attached_when_there_is_no_video() -> None:
    attachments = build_attachments(
        photo_ids=[
            {"owner_id": -222333, "photo_id": 11},
            {"owner_id": -222333, "photo_id": 12},
        ],
        link_url=SITE_URL,
    )

    assert attachments == f"photo-222333_11,photo-222333_12,{SITE_URL}"


def test_a_video_takes_precedence_over_photos() -> None:
    attachments = build_attachments(
        video_links=[{"platform": "vk", "owner_id": -1, "video_id": 2}],
        photo_ids=[{"owner_id": -1, "photo_id": 3}],
    )

    # VK renders one primary media block, and a video card outperforms a still.
    assert attachments == "video-1_2"
    assert "photo" not in attachments


def test_only_one_video_is_attached() -> None:
    attachments = build_attachments(
        video_links=[
            {"platform": "vk", "owner_id": -1, "video_id": 2},
            {"platform": "vk", "owner_id": -1, "video_id": 3},
        ]
    )

    assert attachments == "video-1_2"


def test_a_malformed_attachment_is_skipped() -> None:
    # VK drops a malformed attachment silently and returns success, so the omission
    # is made here where it can be logged.
    attachments = build_attachments(
        video_links=[{"platform": "vk", "owner_id": "nope", "video_id": None}],
        link_url=SITE_URL,
    )

    assert attachments == SITE_URL


def test_a_non_vk_video_link_is_ignored() -> None:
    attachments = build_attachments(
        video_links=[{"platform": "youtube", "owner_id": -1, "video_id": 2}]
    )

    assert attachments == ""


def test_the_wall_message_fits_above_the_fold() -> None:
    message = build_wall_message(
        title=TITLE,
        body_html="<p>" + "Текст новости. " * 200 + "</p>",
        keywords=["станция пыхтино"],
    )

    # A reader who has to expand the post does not click the link.
    assert len(message) <= MAX_POST_LENGTH


def test_the_wall_message_omits_the_url_by_default() -> None:
    message = build_wall_message(title=TITLE, body_html=BODY, keywords=[])

    # The URL travels as an attachment so VK renders a preview card.
    assert SITE_URL not in message


def test_the_wall_message_includes_the_url_when_asked() -> None:
    message = build_wall_message(
        title=TITLE, body_html=BODY, keywords=[], include_link=SITE_URL
    )

    assert SITE_URL in message


def test_the_wall_message_carries_hashtags() -> None:
    message = build_wall_message(
        title=TITLE, body_html=BODY, keywords=["станция пыхтино", "метро москвы"]
    )

    assert "#станцияпыхтино" in message


async def test_a_wall_post_targets_the_community() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{VK_URL}/wall.post").mock(return_value=vk_ok())
        adapter = new_vk()
        try:
            result = await adapter.publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    sent = route.calls[0].request.content.decode()

    # A negative owner_id targets the group; `from_group=1` attributes the post to
    # the community rather than to the admin who owns the token.
    assert "owner_id=-222333" in sent
    assert "from_group=1" in sent
    assert result.external_id == "-222333_4242"
    assert result.url == "https://vk.com/wall-222333_4242"


async def test_a_wall_post_attaches_the_article_video() -> None:
    article = build_article(
        video_links=[
            {"platform": "vk", "owner_id": -222333, "video_id": 777, "access_key": "ak"}
        ]
    )

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{VK_URL}/wall.post").mock(return_value=vk_ok())
        adapter = new_vk()
        try:
            await adapter.publish(article, SITE_URL)
        finally:
            await adapter.close()

    assert "video-222333_777_ak" in route.calls[0].request.content.decode()


async def test_the_vk_token_is_sent_in_the_body() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{VK_URL}/wall.post").mock(return_value=vk_ok())
        adapter = new_vk()
        try:
            await adapter.publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    request = route.calls[0].request

    # A token in the query string lands in every intermediary's access log.
    assert VK_TOKEN not in str(request.url)
    assert VK_TOKEN in request.content.decode()


async def test_a_vk_error_is_detected_despite_http_200() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{VK_URL}/wall.post").mock(return_value=vk_error(100, "bad param"))
        adapter = new_vk()
        try:
            result = await adapter.safe_publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    assert result.status == PlatformStatus.FAILED.value


async def test_a_vk_auth_error_is_not_retryable() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{VK_URL}/wall.post").mock(
            return_value=vk_error(5, "User authorization failed")
        )
        adapter = new_vk()
        try:
            result = await adapter.safe_publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    assert result.retryable is False
    assert route.call_count == 1


async def test_a_vk_rate_limit_is_retried() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{VK_URL}/wall.post").mock(
            side_effect=[vk_error(6, "Too many requests per second"), vk_ok()]
        )
        adapter = new_vk()
        try:
            result = await adapter.publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    assert route.call_count == 2
    assert result.is_published


async def test_vk_without_a_group_is_disabled() -> None:
    # Without a group the post would land on the token owner's personal wall.
    adapter = VKWallAdapter(access_token=VK_TOKEN, group_id=None, base_url=VK_URL)

    result = await adapter.safe_publish(build_article(), SITE_URL)

    assert result.status == PlatformStatus.DISABLED.value


# ======================================================================== OK


def new_ok() -> OKAdapter:
    return OKAdapter(
        access_token=OK_TOKEN,
        application_key=OK_APP_KEY,
        session_secret_key=OK_SECRET,
        group_id=OK_GROUP,
        base_url=OK_URL,
    )


def test_the_signature_matches_an_independent_computation() -> None:
    params = {
        "method": "mediatopic.post",
        "gid": OK_GROUP,
        "application_key": OK_APP_KEY,
        "format": "json",
        "access_token": OK_TOKEN,
    }

    # Hand-computed per the documented algorithm, not by calling the function under
    # test: a self-referential check proves only determinism.
    signable = {key: value for key, value in params.items() if key != "access_token"}
    joined = "".join(f"{key}={signable[key]}" for key in sorted(signable))
    expected = hashlib.md5(f"{joined}{OK_SECRET}".encode()).hexdigest().lower()  # noqa: S324

    assert calculate_signature(params, secret=OK_SECRET) == expected


def test_the_access_token_is_excluded_from_the_signature() -> None:
    with_token = {"a": "1", "access_token": OK_TOKEN}
    without_token = {"a": "1"}

    # Including the token is the single most common cause of a rejected signature,
    # and OK's error message does not hint at it.
    assert calculate_signature(with_token, secret=OK_SECRET) == calculate_signature(
        without_token, secret=OK_SECRET
    )


def test_an_existing_sig_does_not_affect_the_signature() -> None:
    assert calculate_signature(
        {"a": "1", "sig": "stale"}, secret=OK_SECRET
    ) == calculate_signature({"a": "1"}, secret=OK_SECRET)


def test_the_signature_sorts_parameters_lexicographically() -> None:
    # Insertion order must not change the digest.
    first = calculate_signature({"b": "2", "a": "1"}, secret=OK_SECRET)
    second = calculate_signature({"a": "1", "b": "2"}, secret=OK_SECRET)

    assert first == second


def test_the_signature_is_lowercase() -> None:
    signature = calculate_signature({"a": "1"}, secret=OK_SECRET)

    assert signature == signature.lower()
    assert len(signature) == 32


def test_the_session_secret_is_derived_when_absent() -> None:
    derived = derive_session_secret(
        access_token="T", application_secret_key="A", session_secret_key=""
    )

    # Documented as MD5(access_token + application_secret_key), lowercased.
    assert derived == hashlib.md5(b"TA").hexdigest().lower()  # noqa: S324


def test_an_explicit_session_secret_is_used_directly() -> None:
    assert (
        derive_session_secret(
            access_token="T", application_secret_key="A", session_secret_key="S"
        )
        == "S"
    )


def test_the_media_topic_orders_its_blocks() -> None:
    topic = build_media_topic(
        title=TITLE,
        body_html=BODY,
        url=SITE_URL,
        keywords=["метро"],
        image_urls=[COVER_URL],
    )

    kinds = [block["type"] for block in topic["media"]]

    # Text first so the teaser is what a reader sees; the link last so its preview
    # card sits beneath the copy.
    assert kinds == ["text", "photo", "link"]


def test_the_media_topic_link_block_carries_the_url() -> None:
    topic = build_media_topic(title=TITLE, body_html=BODY, url=SITE_URL)

    link_block = next(block for block in topic["media"] if block["type"] == "link")

    assert link_block["url"] == SITE_URL


def test_the_media_topic_omits_photos_when_there_are_none() -> None:
    topic = build_media_topic(title=TITLE, body_html=BODY, url=SITE_URL)

    assert all(block["type"] != "photo" for block in topic["media"])


async def test_an_ok_topic_is_signed_and_posted() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{OK_URL}/fb.do").mock(
            return_value=httpx.Response(200, json="778899")
        )
        adapter = new_ok()
        try:
            result = await adapter.publish(
                build_article(), SITE_URL, image_urls=[COVER_URL]
            )
        finally:
            await adapter.close()

    sent = dict(
        pair.split("=", 1)
        for pair in route.calls[0].request.content.decode().split("&")
    )

    assert sent["method"] == "mediatopic.post"
    assert "sig" in sent
    assert len(sent["sig"]) == 32
    assert result.is_published
    assert result.external_id == "778899"


async def test_the_ok_attachment_is_sent_as_a_json_string() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{OK_URL}/fb.do").mock(
            return_value=httpx.Response(200, json="1")
        )
        adapter = new_ok()
        try:
            await adapter.publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    body = route.calls[0].request.content.decode()

    # OK expects a JSON string, not nested form fields, and the value is signed so
    # any whitespace change would alter the digest.
    assert "attachment=" in body
    assert "media" in body


async def test_an_ok_auth_error_is_not_retryable() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{OK_URL}/fb.do").mock(
            return_value=httpx.Response(
                200, json={"error_code": 102, "error_msg": "session expired"}
            )
        )
        adapter = new_ok()
        try:
            result = await adapter.safe_publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    assert result.retryable is False
    assert route.call_count == 1
    assert "scope" in (result.error or "").lower()


async def test_an_ok_rate_limit_is_retried() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{OK_URL}/fb.do").mock(
            side_effect=[
                httpx.Response(200, json={"error_code": 4, "error_msg": "flood"}),
                httpx.Response(200, json="99"),
            ]
        )
        adapter = new_ok()
        try:
            result = await adapter.publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    assert route.call_count == 2
    assert result.is_published


async def test_an_ok_response_without_an_id_fails() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{OK_URL}/fb.do").mock(return_value=httpx.Response(200, json={}))
        adapter = new_ok()
        try:
            result = await adapter.safe_publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    assert result.status == PlatformStatus.FAILED.value


async def test_a_partially_configured_ok_adapter_is_disabled() -> None:
    adapter = OKAdapter(
        access_token=OK_TOKEN,
        application_key="",
        session_secret_key=OK_SECRET,
        group_id=OK_GROUP,
        base_url=OK_URL,
    )

    result = await adapter.safe_publish(build_article(), SITE_URL)

    assert result.status == PlatformStatus.DISABLED.value


async def test_the_ok_secret_never_appears_in_an_error() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{OK_URL}/fb.do").mock(
            return_value=httpx.Response(
                200, json={"error_code": 999, "error_msg": "unknown"}
            )
        )
        adapter = new_ok()
        try:
            result = await adapter.safe_publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    assert OK_SECRET not in (result.error or "")
    assert OK_TOKEN not in (result.error or "")


# ====================================================================== Dzen


def new_dzen() -> DzenAdapter:
    return DzenAdapter(
        token=DZEN_TOKEN, channel_id=DZEN_CHANNEL, base_url=DZEN_URL
    )


def test_dzen_html_keeps_the_supported_subset() -> None:
    rendered = build_dzen_html(BODY)

    assert "<h2>" in rendered
    assert "<p>" in rendered
    assert "<ul>" in rendered
    assert "<li>" in rendered


def test_dzen_html_remaps_an_h1() -> None:
    rendered = build_dzen_html("<h1>Крупный</h1><p>Текст</p>")

    # Dzen renders the article title as h1; a second one competes with it.
    assert "<h1>" not in rendered
    assert rendered.startswith("<h2>Крупный</h2>")


def test_dzen_html_unwraps_unsupported_tags_but_keeps_their_text() -> None:
    rendered = build_dzen_html("<div>В диве</div><span>инлайн</span>")

    # An unsupported tag must not take its content with it, or the article loses
    # paragraphs.
    assert "<div>" not in rendered
    assert "<span>" not in rendered
    assert "В диве" in rendered
    assert "инлайн" in rendered


def test_dzen_html_removes_executable_blocks_with_their_bodies() -> None:
    rendered = build_dzen_html("<p>ok</p><script>alert(1)</script>")

    assert "script" not in rendered.lower()
    assert "alert" not in rendered


def test_dzen_html_strips_inline_handlers() -> None:
    rendered = build_dzen_html('<p onclick="alert(1)" style="color:red">Текст</p>')

    assert "onclick" not in rendered
    assert "style" not in rendered
    assert "Текст" in rendered


def test_dzen_html_keeps_href_and_drops_presentational_attributes() -> None:
    rendered = build_dzen_html('<a href="/n/1" class="link" id="x">ссылка</a>')

    assert 'href="/n/1"' in rendered
    assert "class=" not in rendered
    assert "id=" not in rendered


def test_the_publication_payload_carries_the_canonical_url() -> None:
    payload = build_publication_payload(
        title=TITLE, body_html=BODY, canonical_url=SITE_URL, description=DESCRIPTION
    )

    # Without it Dzen's full copy competes with the original in search and can
    # outrank it.
    assert payload["canonicalUrl"] == SITE_URL
    assert payload["contentFormat"] == "html"


def test_the_publication_title_is_bounded() -> None:
    payload = build_publication_payload(
        title="Заголовок " * 40, body_html=BODY, canonical_url=SITE_URL
    )

    assert len(payload["title"]) <= 120


def test_the_auth_state_expires() -> None:
    fresh = DzenAuthState(
        token="t",
        channel_id="c",
        expires_at=datetime.now(UTC) + timedelta(seconds=AUTH_STATE_TTL_SECONDS),
    )
    stale = DzenAuthState(
        token="t", channel_id="c", expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )

    assert fresh.is_valid is True
    assert stale.is_valid is False


def test_a_near_expiry_auth_state_is_treated_as_stale() -> None:
    # The 60-second margin avoids a token expiring in flight after passing the
    # check.
    almost = DzenAuthState(
        token="t", channel_id="c", expires_at=datetime.now(UTC) + timedelta(seconds=30)
    )

    assert almost.is_valid is False


async def test_a_dzen_publication_is_created_then_published() -> None:
    with respx.mock(assert_all_called=True) as mock:
        create = mock.post(f"{DZEN_URL}/channel/{DZEN_CHANNEL}/publication").mock(
            return_value=httpx.Response(200, json={"publicationId": "pub-1"})
        )
        publish = mock.post(
            f"{DZEN_URL}/channel/{DZEN_CHANNEL}/publication/pub-1/publish"
        ).mock(
            return_value=httpx.Response(
                200, json={"url": "https://dzen.ru/a/pub-1"}
            )
        )
        adapter = new_dzen()
        try:
            result = await adapter.publish(
                build_article(), SITE_URL, cover_url=COVER_URL
            )
        finally:
            await adapter.close()

    assert create.call_count == 1
    assert publish.call_count == 1
    assert result.is_published
    assert result.external_id == "pub-1"
    assert result.url == "https://dzen.ru/a/pub-1"


async def test_a_created_draft_that_cannot_publish_reports_its_id() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{DZEN_URL}/channel/{DZEN_CHANNEL}/publication").mock(
            return_value=httpx.Response(200, json={"publicationId": "pub-2"})
        )
        mock.post(
            f"{DZEN_URL}/channel/{DZEN_CHANNEL}/publication/pub-2/publish"
        ).mock(return_value=httpx.Response(500, text="server error"))
        adapter = new_dzen()
        try:
            result = await adapter.publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    # The draft exists; reporting its id lets a retry finish the transition instead
    # of creating a duplicate publication.
    assert result.is_published is False
    assert result.external_id == "pub-2"
    assert result.retryable is True


async def test_dzen_refuses_to_publish_without_a_canonical_url() -> None:
    with respx.mock(assert_all_called=False):
        adapter = new_dzen()
        try:
            result = await adapter.safe_publish(build_article(), "")
        finally:
            await adapter.close()

    # A full copy without a canonical pointer would let Dzen outrank the original.
    assert result.status == PlatformStatus.FAILED.value
    assert "canonical" in (result.error or "").lower()


async def test_a_dzen_401_invalidates_the_cached_auth_state() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{DZEN_URL}/channel/{DZEN_CHANNEL}/publication").mock(
            return_value=httpx.Response(401, text="unauthorized")
        )
        adapter = new_dzen()
        try:
            result = await adapter.safe_publish(build_article(), SITE_URL)
            # Replaying a dead token on the next article would fail identically.
            assert adapter.auth_state is None
        finally:
            await adapter.close()

    assert result.retryable is False


async def test_the_dzen_auth_state_is_reused_across_publishes() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{DZEN_URL}/channel/{DZEN_CHANNEL}/publication").mock(
            return_value=httpx.Response(200, json={"publicationId": "pub-3"})
        )
        mock.post(
            f"{DZEN_URL}/channel/{DZEN_CHANNEL}/publication/pub-3/publish"
        ).mock(return_value=httpx.Response(200, json={"url": "https://dzen.ru/a/3"}))
        adapter = new_dzen()
        try:
            await adapter.publish(build_article(), SITE_URL)
            first_state = adapter.auth_state
            await adapter.publish(build_article(), SITE_URL)
            second_state = adapter.auth_state
        finally:
            # `close()` drops the cached state, so both reads happen before it.
            await adapter.close()

    # A batch performs one exchange rather than one per article.
    assert first_state is not None
    assert second_state is first_state
    assert adapter.auth_state is None


async def test_an_empty_body_is_refused_by_dzen() -> None:
    with respx.mock(assert_all_called=False):
        adapter = new_dzen()
        try:
            result = await adapter.safe_publish(
                build_article(content_html="<script>x</script>"), SITE_URL
            )
        finally:
            await adapter.close()

    assert result.status == PlatformStatus.FAILED.value
    assert "sanitis" in (result.error or "").lower()


async def test_an_unconfigured_dzen_adapter_is_disabled() -> None:
    adapter = DzenAdapter(token="", channel_id="", base_url=DZEN_URL)

    result = await adapter.safe_publish(build_article(), SITE_URL)

    assert result.status == PlatformStatus.DISABLED.value


async def test_the_dzen_token_never_appears_in_an_error() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{DZEN_URL}/channel/{DZEN_CHANNEL}/publication").mock(
            return_value=httpx.Response(400, text="bad request")
        )
        adapter = new_dzen()
        try:
            result = await adapter.safe_publish(build_article(), SITE_URL)
        finally:
            await adapter.close()

    assert DZEN_TOKEN not in (result.error or "")


# ============================================================ cross-platform


def test_every_adapter_declares_a_distinct_platform() -> None:
    adapters: list[BaseSocialAdapter] = [
        TelegramAdapter(bot_token="t", channel_id="@c", base_url=TG_URL),
        VKWallAdapter(access_token="t", group_id=1, base_url=VK_URL),
        OKAdapter(
            access_token="t",
            application_key="k",
            session_secret_key="s",
            group_id="g",
            base_url=OK_URL,
        ),
        DzenAdapter(token="t", channel_id="c", base_url=DZEN_URL),
        *build_social_browser_adapters(),
    ]

    codes = [adapter.platform_code for adapter in adapters]

    # The code is the key in `articles.platform_statuses`, so a collision would
    # overwrite another platform's state.
    assert sorted(codes) == sorted(platform.value for platform in Platform)
    assert len(set(codes)) == len(Platform)


def test_every_adapter_reports_configured_when_given_credentials() -> None:
    assert TelegramAdapter(bot_token="t", channel_id="@c").is_configured
    assert VKWallAdapter(access_token="t", group_id=1).is_configured
    assert OKAdapter(
        access_token="t", application_key="k", session_secret_key="s", group_id="g"
    ).is_configured
    assert DzenAdapter(token="t", channel_id="c").is_configured
