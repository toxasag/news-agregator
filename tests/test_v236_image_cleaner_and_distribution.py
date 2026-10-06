# ruff: noqa: RUF001
"""Tests for v2.36.0: image deduplication, quality inspection, and distribution."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.services import image_cleaner
from app.services.image_cleaner import (
    compute_dhash,
    filter_and_deduplicate_web_images,
    hamming_distance,
    inject_article_images,
    inspect_image_quality,
    is_blurry_or_solid,
)


def _generate_synthetic_image(
    width: int = 600,
    height: int = 400,
    pattern: str = "natural",
) -> bytes:
    """Generate in-memory JPEG bytes for testing without external network calls."""
    if pattern == "solid":
        img = np.full((height, width, 3), 128, dtype=np.uint8)
    elif pattern == "gradient":
        # Smooth gradient that has low variance
        img = np.zeros((height, width, 3), dtype=np.uint8)
        for i in range(height):
            img[i, :, :] = int(i * 255 / max(1, height))
    elif pattern == "checkerboard":
        img = np.zeros((height, width, 3), dtype=np.uint8)
        step = 40
        for y in range(0, height, step):
            for x in range(0, width, step):
                if ((x // step) + (y // step)) % 2 == 0:
                    img[y : y + step, x : x + step] = [240, 240, 240]
                else:
                    img[y : y + step, x : x + step] = [20, 40, 180]
    else:  # natural scenery with texture
        y_coords, x_coords = np.mgrid[0:height, 0:width]
        c1 = (140 + 40 * np.sin(x_coords / 40.0)).astype(np.uint8)
        c2 = (110 + 30 * np.cos(y_coords / 30.0)).astype(np.uint8)
        c3 = np.clip(70 + y_coords * 120 // max(1, height), 0, 255).astype(np.uint8)
        img = np.stack([c1, c2, c3], axis=-1)
        noise = np.random.RandomState(42).randint(-15, 15, (height, width, 3))
        img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    success, encoded = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert success
    return bytes(encoded)


def test_dhash_and_hamming_distance() -> None:
    img1 = _generate_synthetic_image(600, 400, pattern="natural")
    hash1 = compute_dhash(img1)
    assert hash1 is not None

    # Identical image gives distance 0
    assert hamming_distance(hash1, hash1) == 0

    # Slight bit difference
    hash2 = hash1 ^ 0b101  # 2 flipped bits
    assert hamming_distance(hash1, hash2) == 2


def test_blur_and_solid_rejection() -> None:
    solid_bytes = _generate_synthetic_image(600, 400, pattern="solid")
    is_bad = is_blurry_or_solid(solid_bytes)
    assert is_bad is True

    good_bytes = _generate_synthetic_image(600, 400, pattern="natural")
    is_bad = is_blurry_or_solid(good_bytes)
    assert is_bad is False


def test_inspect_image_quality_dimensions() -> None:
    # Too small thumbnail (< 350x200)
    small_bytes = _generate_synthetic_image(150, 100, pattern="natural")
    valid, reason, _ = inspect_image_quality(
        small_bytes, url="https://example.com/icon.jpg"
    )
    assert valid is False
    assert "Resolution too low" in reason

    # Extreme aspect ratio (1200x300 -> ratio 4.0 > 2.7)
    banner_bytes = _generate_synthetic_image(1200, 300, pattern="natural")
    valid, reason, _ = inspect_image_quality(
        banner_bytes, url="https://example.com/banner.jpg"
    )
    assert valid is False
    assert "aspect ratio" in reason

    # Good full-size image
    good_bytes = _generate_synthetic_image(800, 600, pattern="natural")
    valid, reason, final_bytes = inspect_image_quality(
        good_bytes, url="https://example.com/photo.jpg"
    )
    assert valid is True
    assert reason == "Clean photo"
    assert len(final_bytes) > 0


@pytest.mark.asyncio
async def test_filter_and_deduplicate_web_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    img_a = _generate_synthetic_image(600, 400, pattern="natural")
    img_solid = _generate_synthetic_image(600, 400, pattern="solid")

    candidates = [
        {
            "url": "https://media.ru/news/photo1.jpg",
            "title": "Пожар 1",
            "domain": "media.ru",
        },
        {
            "url": "https://media.ru/news/photo1.jpg?v=2",
            "title": "Дубль URL",
            "domain": "media.ru",
        },
        {
            "url": "https://other.ru/photo_dup.jpg",
            "title": "Перцептивный дубль",
            "domain": "other.ru",
        },
        {
            "url": "https://bad.ru/solid_gray.png",
            "title": "Однотонная плашка",
            "domain": "bad.ru",
        },
    ]

    # Mock _fetch_image_bytes to return synthetic images
    async def mock_fetch(url: str, *args, **kwargs) -> bytes:
        if "photo1" in url or "photo_dup" in url:
            return img_a
        return img_solid

    monkeypatch.setattr(image_cleaner, "_fetch_image_bytes", mock_fetch)

    accepted = await filter_and_deduplicate_web_images(
        candidates, cache_dir=tmp_path
    )
    assert len(accepted) == 1
    assert accepted[0]["domain"] == "media.ru"
    assert accepted[0]["clean_verdict"] == "Clean photo"


def test_inject_article_images_inline() -> None:
    html_input = (
        "<p>Первый абзац с лидом новости.</p>\n"
        "<p>Второй абзац с развитием событий.</p>\n"
        "<h2>Подробности происшествия</h2>\n"
        "<p>Третий абзац с деталями.</p>\n"
        "<p>Четвертый абзац с комментарием МЧС.</p>\n"
    )
    images = [
        {
            "url": "https://img.ru/p1.jpg",
            "title": "Работа спасателей",
            "domain": "mchs.gov.ru",
        },
        {
            "url": "https://img.ru/p2.jpg",
            "title": "Спецтехника на месте",
            "domain": "yarreg.ru",
        },
    ]

    result = inject_article_images(html_input, images, layout="inline")

    # Images must be injected with semantic figures and figcaption attribution
    assert '<figure class="article-image' in result
    assert "https://img.ru/p1.jpg" in result
    assert "Фото:" in result
    assert "mchs.gov.ru" in result
    assert "yarreg.ru" in result
    # Content must retain all headings and text
    assert "<h2>Подробности происшествия</h2>" in result
    assert "Четвертый абзац" in result


def test_inject_article_images_slideshow() -> None:
    html_input = (
        "<p>Первый абзац с лидом.</p>\n"
        "<p>Второй абзац с контекстом.</p>\n"
        "<p>Третий абзац.</p>\n"
    )
    images = [
        {"url": "https://img.ru/slide1.jpg", "title": "Кадр 1", "domain": "tass.ru"},
        {"url": "https://img.ru/slide2.jpg", "title": "Кадр 2", "domain": "ria.ru"},
    ]

    result = inject_article_images(html_input, images, layout="slideshow")

    assert '<div class="svodka-carousel' in result
    assert 'class="svodka-carousel__track"' in result
    assert "https://img.ru/slide1.jpg" in result
    assert "https://img.ru/slide2.jpg" in result
    assert "Фотогалерея события" in result


def test_inject_article_images_cover_only() -> None:
    html_input = "<p>Абзац текста.</p>"
    images = [{"url": "https://img.ru/p1.jpg", "title": "Фото", "domain": "smi.ru"}]

    result = inject_article_images(html_input, images, layout="cover_only")
    assert result == html_input
    assert "https://img.ru/p1.jpg" not in result
