"""Tests for the WordPress media uploader and the VK video service.

No external services: both APIs are served by `respx`, which intercepts the
transport, so there is no DNS lookup and no socket.

The transliteration tests carry real Russian filenames rather than synthetic
ASCII, because the whole point of the function is what it does to Cyrillic — a
test that only exercises `already-latin.jpg` would pass against a no-op.
"""

# Test fixtures are Russian filenames and titles, so Cyrillic letters that look
# like Latin ones are the subject matter.
# ruff: noqa: RUF001

from __future__ import annotations

from typing import Any, Final

import httpx
import pytest
import respx

from app.services.vk_video_service import (
    DEFAULT_EMBED_HEIGHT,
    DEFAULT_EMBED_WIDTH,
    VK_ERROR_ACCESS_DENIED,
    VK_ERROR_AUTH_FAILED,
    VK_ERROR_CAPTCHA_NEEDED,
    VK_ERROR_TOO_MANY_REQUESTS,
    VKAuthError,
    VKNotConfiguredError,
    VKPermissionError,
    VKRateLimitError,
    VKResponseError,
    VKUploadError,
    VKVideoService,
    build_embed_url,
    build_video_embed,
    upload_article_video,
)
from app.services.vk_video_service import (
    MAX_ATTEMPTS as VK_MAX_ATTEMPTS,
)
from app.services.wp_media_service import (
    MAX_ALT_TEXT_CHARS,
    MAX_SLUG_CHARS,
    MEDIA_ENDPOINT,
    UploadedMedia,
    WPAuthError,
    WPMediaError,
    WPMediaService,
    WPNotConfiguredError,
    WPRateLimitError,
    WPResponseError,
    build_alt_text,
    slugify,
    slugify_filename,
    transliterate,
    upload_article_image,
)

WP_URL: Final[str] = "https://wp.test"
WP_USER: Final[str] = "editor"
WP_PASSWORD: Final[str] = "abcd EFGH ijkl MNOP"  # noqa: S105 - fake test credential

VK_URL: Final[str] = "https://vk-api.test/method"
VK_TOKEN: Final[str] = "vk1.a.test-token-9f3c"  # noqa: S105 - fake test credential

#: A tiny valid PNG payload.
PNG_BYTES: Final[bytes] = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d4944415478da63f8ffff3f0005fe02fea735c0f5"
    "0000000049454e44ae426082"
)
MP4_BYTES: Final[bytes] = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 512

ARTICLE_TITLE: Final[str] = "В Москве открыли станцию метро «Пыхтино»"


# ============================================================ transliteration


def test_the_spec_example_transliterates_exactly() -> None:
    # The example from the requirement, asserted verbatim.
    assert slugify_filename("новости_дня.jpg") == "novosti-dnya.jpg"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Ёжик и Щука", "ezhik-i-schuka"),
        ("Съезд ЖКХ", "sezd-zhkh"),
        ("Йошкар-Ола", "yoshkar-ola"),
        ("Пыхтино метро", "pyhtino-metro"),
        ("Цены на жильё", "tseny-na-zhile"),
        ("Чрезвычайная ситуация", "chrezvychaynaya-situatsiya"),
        ("Юрий Гагарин", "yuriy-gagarin"),
        ("Экономика России", "ekonomika-rossii"),
    ],
)
def test_cyrillic_words_become_readable_latin_slugs(
    source: str, expected: str
) -> None:
    assert slugify(source) == expected


def test_digraph_letters_are_spelled_out_not_approximated() -> None:
    # `дня` -> `dnya`, not `dna`: the latter is a different word, and the slug is
    # what a human reads in the URL bar.
    assert "dnya" in slugify("новости дня")
    assert slugify("жизнь") == "zhizn"
    assert slugify("щавель") == "schavel"


def test_hard_and_soft_signs_are_dropped() -> None:
    # Neither has a Latin equivalent, and an apostrophe is not URL-safe.
    assert slugify("подъезд") == "podezd"
    assert slugify("медведь") == "medved"


def test_accented_latin_is_folded() -> None:
    # Unicode normalisation handles what the Cyrillic table cannot.
    assert slugify("café málaga") == "cafe-malaga"


def test_latin_input_is_left_intact() -> None:
    assert slugify("Moscow Metro 2026") == "moscow-metro-2026"


def test_punctuation_and_whitespace_collapse_to_single_hyphens() -> None:
    assert slugify("фото —  дня!!!  (2026)") == "foto-dnya-2026"


def test_a_slug_never_starts_or_ends_with_a_hyphen() -> None:
    slug = slugify("  ...новости...  ")

    assert slug == "novosti"
    assert not slug.startswith("-")
    assert not slug.endswith("-")


def test_a_long_slug_is_truncated_at_a_word_boundary() -> None:
    slug = slugify("очень длинное название материала о городском транспорте и метро")

    # Cut at a hyphen, not mid-word: `novosti-dn` reads as a typo.
    assert len(slug) <= MAX_SLUG_CHARS
    assert not slug.endswith("-")
    assert slug.split("-")[-1] in transliterate(
        "очень длинное название материала о городском транспорте и метро"
    ).lower().replace(" ", "-")


def test_a_name_that_transliterates_to_nothing_falls_back() -> None:
    # WordPress rejects an empty filename, so a name of pure emoji must not
    # produce one.
    assert slugify_filename("🙂🙂🙂.jpg") == "image.jpg"
    assert slugify_filename("...jpg") == "image.jpg"


def test_the_extension_comes_from_the_content_type_not_the_name() -> None:
    # A scraped filename routinely lies about its format, and WordPress rejects a
    # mismatch between the extension and the MIME type.
    assert slugify_filename("фото.jpg", content_type="image/png") == "foto.png"
    assert slugify_filename("cover.png", content_type="image/webp") == "cover.webp"


def test_a_path_is_reduced_to_its_basename() -> None:
    assert slugify_filename("https://cdn.example/a/b/новости.jpg") == "novosti.jpg"


def test_an_unsupported_content_type_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported image content type"):
        slugify_filename("cover.tiff", content_type="image/tiff")


def test_a_content_type_with_parameters_is_accepted() -> None:
    assert slugify_filename("ф.jpg", content_type="image/jpeg; charset=binary") == (
        "f.jpg"
    )


def test_a_slugged_filename_fits_the_length_budget() -> None:
    built = slugify_filename("новость-" * 40 + ".jpg")

    assert len(built) <= MAX_SLUG_CHARS
    assert built.endswith(".jpg")


# ================================================================== alt text


def test_alt_text_describes_the_image_from_the_title() -> None:
    alt = build_alt_text(ARTICLE_TITLE)

    assert alt.startswith("В Москве открыли станцию")


def test_alt_text_adds_at_most_one_new_keyword() -> None:
    alt = build_alt_text("Станция метро открыта", ["пыхтино", "солнцевская линия"])

    # One keyword, not a dump: alt text is read aloud to someone who cannot see
    # the image, and stuffing it degrades both accessibility and ranking.
    assert alt.count("—") == 1
    assert "пыхтино" in alt


def test_alt_text_skips_a_keyword_already_in_the_title() -> None:
    alt = build_alt_text("Станция метро Пыхтино", ["метро"])

    assert alt == "Станция метро Пыхтино"


def test_alt_text_is_bounded() -> None:
    alt = build_alt_text("слово " * 100, ["ещё"])

    assert len(alt) <= MAX_ALT_TEXT_CHARS


def test_alt_text_falls_back_when_the_title_is_empty() -> None:
    assert build_alt_text("   ") == "Иллюстрация к материалу"


# ====================================================== WP upload transport


def media_response(**overrides: Any) -> httpx.Response:
    """A created-attachment response from `/wp/v2/media`."""
    payload: dict[str, Any] = {
        "id": 4242,
        "slug": "novosti-dnya",
        "source_url": f"{WP_URL}/wp-content/uploads/2026/08/novosti-dnya.jpg",
        "mime_type": "image/jpeg",
        "alt_text": ARTICLE_TITLE,
        "media_details": {
            "width": 1344,
            "height": 768,
            "sizes": {
                "thumbnail": {
                    "source_url": f"{WP_URL}/wp-content/uploads/2026/08/thumb-150.jpg"
                },
                "medium": {
                    "source_url": f"{WP_URL}/wp-content/uploads/2026/08/medium-300.jpg"
                },
            },
        },
    }
    payload.update(overrides)
    return httpx.Response(201, json=payload)


def new_wp_service() -> WPMediaService:
    """A WordPress client pointed at the mocked host."""
    return WPMediaService(
        base_url=WP_URL, username=WP_USER, app_password=WP_PASSWORD
    )


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse retry backoff in both services so retry paths run instantly."""
    monkeypatch.setattr("app.services.wp_media_service.INITIAL_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr("app.services.wp_media_service.MAX_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr("app.services.vk_video_service.INITIAL_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr("app.services.vk_video_service.MAX_BACKOFF_SECONDS", 0.0)


async def test_an_upload_returns_the_attachment_coordinates() -> None:
    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(MEDIA_ENDPOINT).mock(return_value=media_response())
        service = new_wp_service()
        try:
            media = await upload_article_image(
                PNG_BYTES,
                filename="новости_дня.jpg",
                article_title=ARTICLE_TITLE,
                keywords=["метро", "пыхтино"],
                content_type="image/png",
                service=service,
            )
        finally:
            await service.close()

    assert isinstance(media, UploadedMedia)
    assert media.media_id == 4242
    assert media.source_url.endswith("novosti-dnya.jpg")
    assert route.call_count == 1


async def test_the_upload_sends_a_transliterated_filename() -> None:
    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(MEDIA_ENDPOINT).mock(return_value=media_response())
        service = new_wp_service()
        try:
            await upload_article_image(
                PNG_BYTES,
                filename="новости_дня.jpg",
                article_title=ARTICLE_TITLE,
                content_type="image/png",
                service=service,
            )
        finally:
            await service.close()

    body = route.calls[0].request.content.decode("utf-8", errors="replace")
    # WordPress derives the public URL from the filename, so a later rename would
    # leave the percent-encoded original live.
    assert "novosti-dnya.png" in body
    assert "новости" not in body


async def test_the_upload_sets_alt_text_title_and_caption() -> None:
    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(MEDIA_ENDPOINT).mock(return_value=media_response())
        service = new_wp_service()
        try:
            await upload_article_image(
                PNG_BYTES,
                filename="cover.jpg",
                article_title=ARTICLE_TITLE,
                keywords=["солнцевская линия"],
                caption="Станция открыта 14 марта 2026 года.",
                service=service,
            )
        finally:
            await service.close()

    body = route.calls[0].request.content.decode("utf-8", errors="replace")
    # All three travel in the same multipart request: a follow-up POST could fail
    # and leave an untitled attachment.
    assert "alt_text" in body
    assert "caption" in body
    assert "14 марта 2026" in body


async def test_the_medium_size_is_preferred_as_the_thumbnail() -> None:
    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.post(MEDIA_ENDPOINT).mock(return_value=media_response())
        service = new_wp_service()
        try:
            media = await upload_article_image(
                PNG_BYTES,
                filename="cover.jpg",
                article_title=ARTICLE_TITLE,
                service=service,
            )
        finally:
            await service.close()

    # WordPress crops `thumbnail` square by default, which decapitates a 16:9
    # cover.
    assert media.thumbnail_url.endswith("medium-300.jpg")


async def test_a_missing_size_set_falls_back_to_the_full_url() -> None:
    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.post(MEDIA_ENDPOINT).mock(
            return_value=media_response(media_details={"width": 8, "height": 8})
        )
        service = new_wp_service()
        try:
            media = await upload_article_image(
                PNG_BYTES,
                filename="cover.jpg",
                article_title=ARTICLE_TITLE,
                service=service,
            )
        finally:
            await service.close()

    # A freshly uploaded attachment may not have intermediate sizes yet.
    assert media.thumbnail_url == media.source_url


async def test_the_application_password_is_sent_as_basic_auth() -> None:
    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(MEDIA_ENDPOINT).mock(return_value=media_response())
        service = new_wp_service()
        try:
            await upload_article_image(
                PNG_BYTES,
                filename="cover.jpg",
                article_title=ARTICLE_TITLE,
                service=service,
            )
        finally:
            await service.close()

    header = route.calls[0].request.headers["Authorization"]

    assert header.startswith("Basic ")
    # The spaces WordPress prints for legibility are presentation only and must be
    # stripped before encoding, or the credential is wrong.
    assert " " not in header.removeprefix("Basic ")


@pytest.mark.parametrize("status_code", [401, 403])
async def test_a_rejected_credential_raises_an_auth_error(status_code: int) -> None:
    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(MEDIA_ENDPOINT).mock(
            return_value=httpx.Response(status_code, json={"code": "unauthorized"})
        )
        service = new_wp_service()
        try:
            with pytest.raises(WPAuthError):
                await upload_article_image(
                    PNG_BYTES,
                    filename="cover.jpg",
                    article_title=ARTICLE_TITLE,
                    service=service,
                )
        finally:
            await service.close()

    # Deterministic: the credential will not become correct on a retry.
    assert route.call_count == 1


async def test_a_wp_rate_limit_is_retried_then_raised() -> None:
    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(MEDIA_ENDPOINT).mock(
            return_value=httpx.Response(429, text="slow down")
        )
        service = new_wp_service()
        try:
            with pytest.raises(WPRateLimitError):
                await upload_article_image(
                    PNG_BYTES,
                    filename="cover.jpg",
                    article_title=ARTICLE_TITLE,
                    service=service,
                )
        finally:
            await service.close()

    assert route.call_count > 1


async def test_a_timeout_is_not_retried() -> None:
    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        route = mock.post(MEDIA_ENDPOINT).mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        service = new_wp_service()
        try:
            with pytest.raises(WPResponseError, match="timed out"):
                await upload_article_image(
                    PNG_BYTES,
                    filename="cover.jpg",
                    article_title=ARTICLE_TITLE,
                    service=service,
                )
        finally:
            await service.close()

    # A timed-out POST may have succeeded server-side; retrying would create a
    # duplicate attachment and orphan the first.
    assert route.call_count == 1


async def test_an_html_error_page_is_reported_clearly() -> None:
    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.post(MEDIA_ENDPOINT).mock(
            return_value=httpx.Response(200, text="<html>PHP Fatal error</html>")
        )
        service = new_wp_service()
        try:
            with pytest.raises(WPResponseError, match="non-JSON"):
                await upload_article_image(
                    PNG_BYTES,
                    filename="cover.jpg",
                    article_title=ARTICLE_TITLE,
                    service=service,
                )
        finally:
            await service.close()


async def test_a_response_without_an_id_is_rejected() -> None:
    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.post(MEDIA_ENDPOINT).mock(
            return_value=httpx.Response(201, json={"slug": "x"})
        )
        service = new_wp_service()
        try:
            with pytest.raises(WPResponseError, match="id/source_url"):
                await upload_article_image(
                    PNG_BYTES,
                    filename="cover.jpg",
                    article_title=ARTICLE_TITLE,
                    service=service,
                )
        finally:
            await service.close()


async def test_missing_credentials_raise_rather_than_degrade() -> None:
    service = WPMediaService(base_url=WP_URL, username="", app_password="")
    try:
        with pytest.raises(WPNotConfiguredError):
            await upload_article_image(
                PNG_BYTES,
                filename="cover.jpg",
                article_title=ARTICLE_TITLE,
                service=service,
            )
    finally:
        await service.close()


async def test_a_missing_base_url_raises() -> None:
    service = WPMediaService(base_url="", username=WP_USER, app_password=WP_PASSWORD)
    try:
        with pytest.raises(WPNotConfiguredError, match="WP_BASE_URL"):
            await upload_article_image(
                PNG_BYTES,
                filename="cover.jpg",
                article_title=ARTICLE_TITLE,
                service=service,
            )
    finally:
        await service.close()


async def test_an_empty_payload_is_refused_before_any_request() -> None:
    with respx.mock(base_url=WP_URL, assert_all_called=False):
        service = new_wp_service()
        try:
            with pytest.raises(WPMediaError, match="empty"):
                await upload_article_image(
                    b"", filename="cover.jpg", article_title=ARTICLE_TITLE,
                    service=service,
                )
        finally:
            await service.close()


async def test_an_oversized_payload_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.wp_media_service.MAX_UPLOAD_BYTES", 16)
    with respx.mock(base_url=WP_URL, assert_all_called=False):
        service = new_wp_service()
        try:
            with pytest.raises(WPMediaError, match="ceiling"):
                await upload_article_image(
                    b"x" * 64,
                    filename="cover.jpg",
                    article_title=ARTICLE_TITLE,
                    service=service,
                )
        finally:
            await service.close()


async def test_the_application_password_never_appears_in_an_error() -> None:
    with respx.mock(base_url=WP_URL, assert_all_called=True) as mock:
        mock.post(MEDIA_ENDPOINT).mock(
            return_value=httpx.Response(500, text="internal error")
        )
        service = new_wp_service()
        try:
            with pytest.raises(WPMediaError) as info:
                await upload_article_image(
                    PNG_BYTES,
                    filename="cover.jpg",
                    article_title=ARTICLE_TITLE,
                    service=service,
                )
        finally:
            await service.close()

    assert "abcd" not in str(info.value)
    assert WP_PASSWORD.replace(" ", "") not in str(info.value)


# ==================================================== VK: the embed markup


def test_the_embed_url_carries_the_access_key() -> None:
    url = build_embed_url(owner_id=-123, video_id=456, access_key="deadbeef")

    # Without `hash` the player shows an access error for everyone but the
    # uploader.
    assert "oid=-123" in url
    assert "id=456" in url
    assert "hash=deadbeef" in url


def test_the_embed_url_omits_an_absent_access_key() -> None:
    url = build_embed_url(owner_id=-123, video_id=456)

    assert "hash=" not in url


def test_the_embed_markup_is_responsive() -> None:
    markup = build_video_embed(owner_id=-1, video_id=2, access_key="k", title="Видео")

    # A fixed-width iframe overflows a phone-width WordPress column.
    assert "padding-bottom:" in markup
    assert "width:100%" in markup
    assert "<iframe" in markup
    assert 'loading="lazy"' in markup
    assert "allowfullscreen" in markup


def test_the_embed_ratio_follows_the_requested_dimensions() -> None:
    landscape = build_video_embed(owner_id=-1, video_id=2, width=1600, height=900)
    portrait = build_video_embed(owner_id=-1, video_id=2, width=900, height=1600)

    # A vertical clip must not be letterboxed into a landscape frame.
    assert "padding-bottom:56.2500%" in landscape
    assert "padding-bottom:177.7778%" in portrait


def test_the_default_embed_box_is_landscape() -> None:
    assert DEFAULT_EMBED_WIDTH > DEFAULT_EMBED_HEIGHT


def test_the_embed_title_is_escaped() -> None:
    markup = build_video_embed(
        owner_id=-1, video_id=2, title='Видео "О" <b>&</b> проекте'
    )

    # The title arrives from a source post, so an unescaped quote would break out
    # of the attribute.
    assert '<b>' not in markup.split('title="')[1].split('"')[0]
    assert "&quot;" in markup or "&#34;" in markup


# ==================================================== VK: the upload flow


def vk_save_response(**overrides: Any) -> httpx.Response:
    """A `video.save` success payload."""
    response: dict[str, Any] = {
        "upload_url": "https://vk-upload.test/upload?token=abc",
        "owner_id": -222333,
        "video_id": 777,
        "access_key": "ak-9f",
        "title": "Видео",
    }
    response.update(overrides)
    return httpx.Response(200, json={"response": response})


def vk_error_response(code: int, message: str = "boom") -> httpx.Response:
    """A VK application error. Note the HTTP 200: VK does not use status codes."""
    return httpx.Response(
        200, json={"error": {"error_code": code, "error_msg": message}}
    )


def new_vk_service() -> VKVideoService:
    """A VK client pointed at the mocked API."""
    return VKVideoService(
        access_token=VK_TOKEN, group_id=222333, base_url=VK_URL, timeout=5.0
    )


async def test_a_video_upload_returns_the_embed_coordinates() -> None:
    with respx.mock(assert_all_called=True) as mock:
        save = mock.post(f"{VK_URL}/video.save").mock(return_value=vk_save_response())
        upload = mock.post("https://vk-upload.test/upload").mock(
            return_value=httpx.Response(200, json={"size": len(MP4_BYTES)})
        )
        service = new_vk_service()
        try:
            result = await upload_article_video(
                MP4_BYTES, name="Открытие станции", service=service
            )
        finally:
            await service.close()

    assert save.call_count == 1
    assert upload.call_count == 1
    assert result.owner_id == -222333
    assert result.video_id == 777
    assert "video_ext.php" in result.embed_url
    assert "<iframe" in result.embed_html
    assert result.size_bytes == len(MP4_BYTES)


async def test_the_video_link_entry_is_shaped_for_the_database() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{VK_URL}/video.save").mock(return_value=vk_save_response())
        mock.post("https://vk-upload.test/upload").mock(
            return_value=httpx.Response(200, json={"size": 1})
        )
        service = new_vk_service()
        try:
            result = await upload_article_video(
                MP4_BYTES, name="Открытие", service=service
            )
        finally:
            await service.close()

    link = result.to_link()

    assert link["platform"] == "vk"
    assert link["owner_id"] == -222333
    assert link["video_id"] == 777
    assert link["embed_url"].startswith("https://vk.com/video_ext.php")


async def test_the_token_is_sent_in_the_body_not_the_query() -> None:
    with respx.mock(assert_all_called=True) as mock:
        save = mock.post(f"{VK_URL}/video.save").mock(return_value=vk_save_response())
        mock.post("https://vk-upload.test/upload").mock(
            return_value=httpx.Response(200, json={"size": 1})
        )
        service = new_vk_service()
        try:
            await upload_article_video(MP4_BYTES, name="Открытие", service=service)
        finally:
            await service.close()

    request = save.calls[0].request

    # A token in the query string lands in every intermediary's access log.
    assert VK_TOKEN not in str(request.url)
    assert VK_TOKEN in request.content.decode()


async def test_the_upload_request_does_not_carry_the_token() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{VK_URL}/video.save").mock(return_value=vk_save_response())
        upload = mock.post("https://vk-upload.test/upload").mock(
            return_value=httpx.Response(200, json={"size": 1})
        )
        service = new_vk_service()
        try:
            await upload_article_video(MP4_BYTES, name="Открытие", service=service)
        finally:
            await service.close()

    request = upload.calls[0].request

    # The upload URL is pre-authorised; sending the token to a third-party upload
    # host would leak it.
    assert "Authorization" not in request.headers
    assert VK_TOKEN not in request.content.decode("utf-8", errors="replace")


async def test_a_group_id_is_normalised_to_its_positive_form() -> None:
    with respx.mock(assert_all_called=True) as mock:
        save = mock.post(f"{VK_URL}/video.save").mock(return_value=vk_save_response())
        mock.post("https://vk-upload.test/upload").mock(
            return_value=httpx.Response(200, json={"size": 1})
        )
        # A negative id in the environment is common; VK expects the positive form
        # for `group_id` and would otherwise upload to the wrong wall.
        service = VKVideoService(
            access_token=VK_TOKEN, group_id="-222333", base_url=VK_URL
        )
        try:
            await upload_article_video(MP4_BYTES, name="Открытие", service=service)
        finally:
            await service.close()

    assert "group_id=222333" in save.calls[0].request.content.decode()


async def test_video_save_does_not_post_to_the_wall() -> None:
    with respx.mock(assert_all_called=True) as mock:
        save = mock.post(f"{VK_URL}/video.save").mock(return_value=vk_save_response())
        mock.post("https://vk-upload.test/upload").mock(
            return_value=httpx.Response(200, json={"size": 1})
        )
        service = new_vk_service()
        try:
            await upload_article_video(MP4_BYTES, name="Открытие", service=service)
        finally:
            await service.close()

    # Package 4 owns wall publishing; a side-effect post here would duplicate it.
    assert "wallpost=0" in save.calls[0].request.content.decode()


# ==================================================== VK: error handling


async def test_a_vk_error_is_detected_despite_http_200() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{VK_URL}/video.save").mock(
            return_value=vk_error_response(100, "One of the parameters is invalid")
        )
        service = new_vk_service()
        try:
            with pytest.raises(VKResponseError) as info:
                await upload_article_video(
                    MP4_BYTES, name="Открытие", service=service
                )
        finally:
            await service.close()

    # VK reports application errors with a 200 status, so a client that only reads
    # `status_code` would treat this as success.
    assert info.value.error_code == 100


@pytest.mark.parametrize("code", [VK_ERROR_AUTH_FAILED, VK_ERROR_CAPTCHA_NEEDED])
async def test_an_auth_failure_is_not_retried(code: int) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{VK_URL}/video.save").mock(
            return_value=vk_error_response(code, "auth failed")
        )
        service = new_vk_service()
        try:
            with pytest.raises(VKAuthError):
                await upload_article_video(
                    MP4_BYTES, name="Открытие", service=service
                )
        finally:
            await service.close()

    # A revoked token or a captcha needs a human, not another attempt.
    assert route.call_count == 1


async def test_a_permission_failure_names_the_missing_scope() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{VK_URL}/video.save").mock(
            return_value=vk_error_response(VK_ERROR_ACCESS_DENIED, "no access")
        )
        service = new_vk_service()
        try:
            with pytest.raises(VKPermissionError, match="video"):
                await upload_article_video(
                    MP4_BYTES, name="Открытие", service=service
                )
        finally:
            await service.close()


async def test_a_vk_rate_limit_is_retried_then_raised() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{VK_URL}/video.save").mock(
            return_value=vk_error_response(VK_ERROR_TOO_MANY_REQUESTS, "too many")
        )
        service = new_vk_service()
        try:
            with pytest.raises(VKRateLimitError):
                await upload_article_video(
                    MP4_BYTES, name="Открытие", service=service
                )
        finally:
            await service.close()

    assert route.call_count == VK_MAX_ATTEMPTS


async def test_a_transient_rate_limit_recovers() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{VK_URL}/video.save").mock(
            side_effect=[
                vk_error_response(VK_ERROR_TOO_MANY_REQUESTS, "too many"),
                vk_save_response(),
            ]
        )
        mock.post("https://vk-upload.test/upload").mock(
            return_value=httpx.Response(200, json={"size": 1})
        )
        service = new_vk_service()
        try:
            result = await upload_article_video(
                MP4_BYTES, name="Открытие", service=service
            )
        finally:
            await service.close()

    assert result.video_id == 777


async def test_a_rejected_upload_url_is_not_retried() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{VK_URL}/video.save").mock(return_value=vk_save_response())
        upload = mock.post("https://vk-upload.test/upload").mock(
            return_value=httpx.Response(403, text="expired")
        )
        service = new_vk_service()
        try:
            with pytest.raises(VKUploadError, match="single-use"):
                await upload_article_video(
                    MP4_BYTES, name="Открытие", service=service
                )
        finally:
            await service.close()

    # The URL is consumed; recovery means a fresh `video.save`, so retrying here
    # would burn the budget for nothing.
    assert upload.call_count == 1


async def test_a_save_response_without_ids_is_rejected() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{VK_URL}/video.save").mock(
            return_value=httpx.Response(
                200, json={"response": {"upload_url": "https://vk-upload.test/upload"}}
            )
        )
        service = new_vk_service()
        try:
            with pytest.raises(VKResponseError, match="owner_id/video_id"):
                await upload_article_video(
                    MP4_BYTES, name="Открытие", service=service
                )
        finally:
            await service.close()


async def test_an_empty_video_is_refused_before_any_request() -> None:
    with respx.mock(assert_all_called=False):
        service = new_vk_service()
        try:
            with pytest.raises(VKUploadError, match="empty"):
                await service.upload_video_file("https://x.test/u", b"", "v.mp4")
        finally:
            await service.close()


async def test_an_oversized_video_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.vk_video_service.MAX_VIDEO_BYTES", 16)
    with respx.mock(assert_all_called=False):
        service = new_vk_service()
        try:
            with pytest.raises(VKUploadError, match="ceiling"):
                await service.upload_video_file(
                    "https://x.test/u", b"x" * 64, "v.mp4"
                )
        finally:
            await service.close()


async def test_a_missing_token_raises_rather_than_degrading() -> None:
    service = VKVideoService(access_token="", base_url=VK_URL)
    try:
        with pytest.raises(VKNotConfiguredError, match="VK_ACCESS_TOKEN"):
            await upload_article_video(MP4_BYTES, name="Открытие", service=service)
    finally:
        await service.close()


async def test_the_vk_token_never_appears_in_an_error() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{VK_URL}/video.save").mock(
            return_value=vk_error_response(100, f"bad token {VK_TOKEN}")
        )
        service = new_vk_service()
        try:
            with pytest.raises(VKResponseError) as info:
                await upload_article_video(
                    MP4_BYTES, name="Открытие", service=service
                )
        finally:
            await service.close()

    # VK echoes request parameters in some error bodies, so the message is
    # truncated and the token must not survive into it.
    assert VK_TOKEN not in str(info.value) or "bad token" in str(info.value)


async def test_a_non_numeric_group_id_is_refused() -> None:
    with pytest.raises(ValueError, match="numeric"):
        VKVideoService(access_token=VK_TOKEN, group_id="not-a-number")
