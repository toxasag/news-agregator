# ruff: noqa: RUF001, E501
"""Unit tests for DNA of Winners prompts, Brand Safety filter, and native advertorials."""

from __future__ import annotations

from app.models.promoted_project import PromotedProject
from app.services.content_prompts import (
    NON_COMMERCIAL_CATEGORIES,
    build_messages,
    build_native_advertorial_prompt,
    is_brand_safe_for_commercial_integration,
)


def test_brand_safety_filter_categories() -> None:
    """Non-commercial categories like military news and accidents must be rejected."""
    for cat in NON_COMMERCIAL_CATEGORIES:
        is_safe, reason = is_brand_safe_for_commercial_integration(cat, title="Сводка дня")
        assert is_safe is False
        assert "Brand Safety" in reason


def test_brand_safety_filter_stop_words() -> None:
    """Brand safety stop words must trigger immediate rejection even in safe categories."""
    safe_cat = "novosti-it-i-tehnologij"
    is_safe, _ = is_brand_safe_for_commercial_integration(
        safe_cat,
        title="Новый смартфон поступил в продажу",
        text="Компания представила новинку на конференции в Москве.",
    )
    assert is_safe is True

    # With stop words
    for word in ("погибли", "атака дронов", "взрыв", "теракт", "крушение"):
        unsafe, reason = is_brand_safe_for_commercial_integration(
            safe_cat,
            title=f"Событие: {word}",
            text="Детали инцидента уточняются.",
        )
        assert unsafe is False
        assert "Brand Safety" in reason


def test_build_native_advertorial_prompt() -> None:
    """build_native_advertorial_prompt must generate structured brand journalism rules."""
    project = PromotedProject(
        id="proj_magnit",
        name="Бот-помощник и доставка продуктов",
        brand_name="Магнит Доставка",
        problem_solved="Экономия времени на походах по магазинам",
        target_url="https://magnit.ru/app",
        target_segments=["Активные жители городов 25-45 лет"],
        cta_text="Попробуйте бота в Telegram",
    )

    prompt = build_native_advertorial_prompt(project, cluster_id="testcluster123")
    assert "Магнит Доставка" in prompt
    assert "Бот-помощник и доставка продуктов" in prompt
    assert "https://magnit.ru/app?utm_source=svodkapress" in prompt
    assert "utm_campaign=cluster_testclus" in prompt
    assert "BRAND JOURNALISM" in prompt
    assert "ДНК ПОБЕДИТЕЛЕЙ" in prompt
    assert "ПРАВИЛА ИНТЕГРАЦИИ ПРОДУКТА" in prompt


def test_build_messages_dna_of_winners_rules() -> None:
    """build_messages must embed DNA of winners: emoji, lead 150-250 chars, zero hallucination."""
    cluster_data = {
        "title": "Новый робот-курьер вышел на улицы Казани",
        "text": "В Казани началось тестирование беспилотных доставщиков.",
        "facts": ["10 роботов вышли на маршруты", "Скорость до 8 км/ч"],
        "sources": [{"name": "ТАСС", "text": "ТАСС сообщает о старте тестов.", "url": ""}],
    }

    messages = build_messages(cluster_data, category="novosti-it-i-tehnologij")
    system_prompt = messages[0]["content"]

    # DNA of Winners rules in system prompt
    assert "ДНК ПОБЕДИТЕЛЕЙ" in system_prompt
    assert "1-2 эмодзи" in system_prompt
    assert "150–250 знаков" in system_prompt
    assert "1800–2500 символов" in system_prompt
    assert "ZERO HALLUCINATION" in system_prompt


def test_build_messages_with_promoted_project_brand_safe() -> None:
    """When brand-safe, build_messages embeds native advertorial section."""
    project = PromotedProject(
        id="proj_xyz",
        name="Платежный сервис",
        brand_name="SuperApp",
        problem_solved="Быстрые переводы без комиссии",
        target_url="https://superapp.ru",
    )
    cluster_data = {
        "title": "ЦБ обновил требования к банковским приложениям",
        "text": "Регулятор утвердил новые правила цифровых платежей.",
        "facts": ["Правила вступают в силу с 1 ноября"],
        "sources": [],
    }

    messages = build_messages(
        cluster_data,
        category="novosti-it-i-tehnologij",
        promoted_project=project,
        cluster_id="cid_safe_1",
    )
    user_prompt = messages[1]["content"]
    assert "BRAND JOURNALISM" in user_prompt
    assert "SuperApp" in user_prompt
    assert "https://superapp.ru?utm_source=" in user_prompt


def test_build_messages_with_promoted_project_unsafe() -> None:
    """When cluster is unsafe, advertorial prompt is replaced with a Brand Safety block."""
    project = PromotedProject(
        id="proj_xyz",
        name="Платежный сервис",
        brand_name="SuperApp",
        problem_solved="Быстрые переводы без комиссии",
        target_url="https://superapp.ru",
    )
    cluster_data = {
        "title": "Крушение вертолета в труднодоступном районе",
        "text": "Спасатели работают на месте происшествия.",
        "facts": ["На борту было 3 человека"],
        "sources": [],
    }

    messages = build_messages(
        cluster_data,
        category="chp-i-proisshestvij",
        promoted_project=project,
        cluster_id="cid_unsafe_1",
    )
    user_prompt = messages[1]["content"]
    assert "БЛОКИРОВКА РЕКЛАМЫ (BRAND SAFETY)" in user_prompt
    assert "Нативная реклама продукта отключена" in user_prompt
