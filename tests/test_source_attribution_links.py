from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.image_cleaner import _resolve_photo_source_href
from app.services.lab_pipeline import (
    _ensure_sources_block,
    _resolve_source_name_to_url,
    resolve_raw_post_attribution,
)


def test_resolve_raw_post_attribution_direct_url():
    post = SimpleNamespace(
        post_url="https://t.me/shot_shot/12345",
        source=SimpleNamespace(name="SHOT", identifier="@shot_shot"),
        external_post_id=12345,
    )
    url, label = resolve_raw_post_attribution(post)
    assert url == "https://t.me/shot_shot/12345"
    assert "SHOT" in label


def test_resolve_raw_post_attribution_mtproto_mash():
    post = SimpleNamespace(
        post_url="",
        source=SimpleNamespace(name="Mash", identifier="-1001117628569"),
        external_post_id=78326,
    )
    url, label = resolve_raw_post_attribution(post)
    assert url == "https://t.me/breakingmash/78326"
    assert "Mash" in label


def test_resolve_raw_post_attribution_known_media():
    post = SimpleNamespace(
        post_url="",
        source=SimpleNamespace(name="РИА Новости", identifier="ria.ru"),
        external_post_id=None,
    )
    url, label = resolve_raw_post_attribution(post)
    assert url == "https://ria.ru"
    assert "РИА Новости" in label

    post_tg = SimpleNamespace(
        post_url="",
        source=SimpleNamespace(name="РИА Новости", identifier="@rian_ru"),
        external_post_id=123,
    )
    url_tg, _ = resolve_raw_post_attribution(post_tg)
    assert url_tg == "https://t.me/rian_ru/123"


def test_ensure_sources_block_purges_vague_hallucinated_text():
    html_with_vague_text = (
        "<p>Тестовая статья о событиях во Вьетнаме.</p>\n"
        "<p><strong>Источники информации:</strong> данные госпиталей провинции Кханьхоа, "
        "сообщения российских туристов во Вьетнаме.</p>"
    )
    post = SimpleNamespace(
        post_url="https://t.me/breakingmash/78326",
        source=SimpleNamespace(name="Mash", identifier="-1001117628569"),
        external_post_id=78326,
    )
    result = _ensure_sources_block(
        html_with_vague_text,
        web_sources=[{"domain": "ria.ru", "url": "https://ria.ru/article", "title": "РИА Новости"}],
        raw_posts=[post],
    )

    # Hallucinated vague phrases MUST NOT appear in the final HTML
    assert "данные госпиталей" not in result
    assert "сообщения российских туристов" not in result

    # Verified sources MUST appear as active <a> links
    assert '<a href="https://ria.ru/article"' in result
    assert '<a href="https://t.me/breakingmash/78326"' in result
    assert "Источники информации:" in result


def test_ensure_sources_block_replaces_photo_caption():
    html_with_plain_photo = (
        '<figure class="article-photo">\n'
        '<img src="https://svodka.press/img.jpg" alt="test" />\n'
        '<figcaption>Фото: Telegram-канал Mash</figcaption>\n'
        '</figure>'
    )
    post = SimpleNamespace(
        post_url="https://t.me/breakingmash/78326",
        source=SimpleNamespace(name="Mash", identifier="-1001117628569"),
        external_post_id=78326,
    )
    result = _ensure_sources_block(
        html_with_plain_photo,
        web_sources=[],
        raw_posts=[post],
    )
    assert '<a href="https://t.me/breakingmash/78326"' in result
    assert "Telegram-канал Mash" in result


def test_resolve_photo_source_href_known_domains():
    assert _resolve_photo_source_href({"domain": "ТАСС"}) == "https://tass.ru"
    assert _resolve_photo_source_href({"domain": "SHOT"}) == "https://t.me/shot_shot"
    assert _resolve_photo_source_href({"source_url": "https://ria.ru/123"}) == "https://ria.ru/123"
