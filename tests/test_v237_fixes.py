# ruff: noqa: RUF001, E501
"""Tests for v2.37.0 fixes: aggregator blocking, dark screen rejection, event relevance."""

import cv2
import numpy as np

from app.services.image_cleaner import _filter_url_candidates, is_blurry_or_solid
from app.services.lab_pipeline import _stem_ru
from app.services.web_researcher import _DISALLOWED_DOMAINS, evaluate_article_relevance


def _create_solid_screen(brightness: int, h: int = 300, w: int = 500) -> bytes:
    img = np.full((h, w, 3), brightness, dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", img)
    return bytes(buf)


def _create_dark_screen_with_white_text() -> bytes:
    """Simulate tvgram-style promotional card: black background with small logo/text."""
    img = np.zeros((300, 500, 3), dtype=np.uint8)
    cv2.putText(
        img, "TVGRAM", (100, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2
    )
    cv2.putText(
        img,
        "Telegram channel news",
        (80, 200),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (200, 200, 200),
        1,
    )
    _, buf = cv2.imencode(".jpg", img)
    return bytes(buf)


def test_aggregator_domains_in_disallowed():
    assert "tvgram.ru" in _DISALLOWED_DOMAINS
    assert "tgstat.ru" in _DISALLOWED_DOMAINS
    assert "telemetr.io" in _DISALLOWED_DOMAINS


def test_filter_url_candidates_rejects_aggregators():
    cands = [
        {"url": "https://tvgram.ru/media/preview123.jpg", "title": "Cat hero"},
        {"url": "https://tgstat.ru/img/banner.jpg", "title": "Channel stats"},
        {"url": "https://www.obozrevatel.com/photo/cat.jpg", "title": "Valid photo"},
    ]
    res = _filter_url_candidates(cands)
    assert len(res) == 1
    assert "obozrevatel.com" in res[0]["url"]


def test_is_blurry_or_solid_rejects_dark_promotional_screens():
    dark_text_card = _create_dark_screen_with_white_text()
    assert is_blurry_or_solid(dark_text_card) is True

    pure_black = _create_solid_screen(0)
    assert is_blurry_or_solid(pure_black) is True

    pure_white = _create_solid_screen(255)
    assert is_blurry_or_solid(pure_white) is True


def test_evaluate_article_relevance_rejects_event_mismatch():
    """Fire in Yaroslavl must NOT accept a food/cooking article about pancakes."""
    news_title = "Пожар в Ярославской области: число погибших увеличилось"
    news_text = "В Ярославской области произошел крупный пожар в жилом доме, погибли люди."

    # Off-topic: same region, but event is food/pancakes
    cand_title = "В Ярославской области 14 кафе будут угощать сырниками и блинами"
    cand_snippet = "Кулинарный фестиваль проходит в Ярославской области в эти выходные."
    cand_text = "Жители региона смогут попробовать традиционную выпечку и десерты."

    _score, verdict, accepted = evaluate_article_relevance(
        news_title, news_text, cand_title, cand_snippet, cand_text
    )
    assert accepted is False
    assert "не совпадает" in verdict.lower() or "не по теме" in verdict.lower()


def test_evaluate_article_relevance_accepts_matching_event():
    """Fire in Yaroslavl must accept a matching news report about the fire."""
    news_title = "Пожар в Ярославской области: число погибших увеличилось"
    news_text = "В Ярославской области произошел крупный пожар в жилом доме, погибли люди."

    cand_title = "Число погибших на пожаре в Ярославской области выросло до трех человек"
    cand_snippet = "МЧС ликвидировало открытое горение в жилом секторе Ярославской области."
    cand_text = "На месте трагедии продолжают работать следователи и пожарные."

    score, _verdict, accepted = evaluate_article_relevance(
        news_title, news_text, cand_title, cand_snippet, cand_text
    )
    assert accepted is True
    assert score >= 0.32


def test_stem_ru_handles_basic_inflections():
    assert _stem_ru("пожаре") == "пожар"
    assert _stem_ru("области") == "област"
    assert _stem_ru("военные") == "военн"
