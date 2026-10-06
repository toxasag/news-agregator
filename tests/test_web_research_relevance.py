# ruff: noqa: RUF001, E501
"""Unit tests for web research article relevance evaluation and image classification."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.web_researcher import (
    WebArticleContent,
    classify_web_image,
    conduct_web_research,
    evaluate_article_relevance,
)
from app.services.web_search import SearchResult


def test_evaluate_article_relevance_matching_ai_story() -> None:
    """Articles mentioning key models/entities should score high and be accepted."""
    news_title = "Китайская Z.ai выпустила открытую модель GLM-5.3"
    news_text = "Стартап Z.ai опубликовал веса новой языковой модели GLM-5.3 для программирования."

    cand_title = "Z.ai выложила веса модели GLM-5.3: тесты и бенчмарки"
    cand_snippet = "Китайская компания Z.ai представила модель GLM-5.3 с рекордными показателями."
    cand_text = "Модель GLM-5.3 от Z.ai доступна для свободного скачивания."

    score, verdict, accepted = evaluate_article_relevance(
        news_title, news_text, cand_title, cand_snippet, cand_text
    )

    assert accepted is True
    assert score >= 0.60
    assert "релевантность" in verdict.lower() or "в тему" in verdict.lower()


def test_evaluate_article_relevance_rejects_offtopic_cars() -> None:
    """Off-topic articles (e.g. car reviews) sharing only minor words must be rejected."""
    news_title = "Китайская Z.ai выпустила открытую модель GLM-5.3"
    news_text = "Стартап Z.ai опубликовал веса новой языковой модели GLM-5.3 для программирования."

    cand_title = "Обзор нового китайского кроссовера Z-5 в автосалонах Москвы"
    cand_snippet = "Китайский производитель привез новую модель автомобиля с вариатором."
    cand_text = "Двигатель 1.5 литра, расход топлива и цены на китайский кроссовер."

    score, verdict, accepted = evaluate_article_relevance(
        news_title, news_text, cand_title, cand_snippet, cand_text
    )

    assert accepted is False
    assert score < 0.35
    assert verdict == "Не по теме"


def test_classify_web_image() -> None:
    """Classify image URLs into photo, infographic, logo, and graphic."""
    # Infographic / chart
    kind1, label1 = classify_web_image(
        "https://vc.ru/uploads/content/benchmark_table_glm53.png",
        title="Сравнение бенчмарков модели",
    )
    assert kind1 == "infographic"
    assert "Инфографика" in label1

    # Logo / emblem
    kind2, label2 = classify_web_image(
        "https://veruna.io/static/brand_logo.svg",
        title="Логотип компании Z.ai",
    )
    assert kind2 == "logo"
    assert "Логотип" in label2

    # Banner / promo
    kind3, label3 = classify_web_image(
        "https://media.com/ads/promo_banner_728x90.jpg",
        title="Рекламный баннер",
    )
    assert kind3 == "graphic"
    assert "Баннер" in label3

    # Editorial photo
    kind4, label4 = classify_web_image(
        "https://im.kommersant.ru/Issues.photo/CORP/2026/09/27/KMO_123456_001.jpg",
        title="Пресс-конференция разработчиков",
    )
    assert kind4 == "photo"
    assert "Фоторепортаж" in label4


@pytest.mark.asyncio
async def test_conduct_web_research_filters_irrelevant_candidate() -> None:
    """Web research should automatically discard off-topic articles from report.sources."""
    mock_search_results = [
        SearchResult(
            title="Обзор китайского авто Z 2026",
            url="https://auto-news.ru/reviews/z-car",
            snippet="Новый китайский автомобиль поступил в продажу.",
            position=1,
        ),
        SearchResult(
            title="Z.ai представила веса модели GLM-5.3",
            url="https://habr.com/ru/news/glm-53-weights",
            snippet="Z.ai выложила открытые веса модели GLM-5.3 для разработчиков.",
            position=2,
        ),
    ]

    async def fake_fetch(url: str, **kwargs: object) -> WebArticleContent:
        if "auto-news" in url:
            return WebArticleContent(
                url=url,
                domain="auto-news.ru",
                title="Обзор китайского авто Z 2026",
                clean_text="Тест-драйв нового седана в городских условиях. Подвеска мягкая.",
                char_count=600,
                image_url="https://auto-news.ru/car.jpg",
                image_kind="photo",
            )
        return WebArticleContent(
            url=url,
            domain="habr.com",
            title="Z.ai представила веса модели GLM-5.3",
            clean_text="Сегодня компания Z.ai анонсировала модель GLM-5.3 с открытым исходным кодом.",
            char_count=800,
            image_url="https://habr.com/glm53.jpg",
            image_kind="photo",
        )

    with (
        patch("app.services.web_researcher.search_duckduckgo", AsyncMock(return_value=mock_search_results)),
        patch("app.services.web_researcher.fetch_clean_article", side_effect=fake_fetch),
    ):
        report = await conduct_web_research(
            "Китайская Z.ai выпустила модель GLM-5.3",
            fallback_text="Z.ai выложила веса новой модели",
            max_sources=3,
        )

        assert report.is_success is True
        # Only the relevant Habr article should be accepted; auto-news must be dropped
        assert len(report.sources) == 1
        assert report.sources[0].domain == "habr.com"
        assert report.sources[0].relevance_score >= 0.50
        assert report.sources[0].relevance_verdict in {"Высокая релевантность", "Косвенный контекст"}
