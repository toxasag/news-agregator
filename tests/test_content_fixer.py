"""Tests for the QA fixer and the embedding service.

No external services. The OpenRouter API is served by `respx`, Redis is
`fakeredis`, and nothing touches PostgreSQL: both modules under test return plain
Python values by design.

The source and draft fixtures are of real news length. The QA fact check is
*ratio*-based (`MAX_DROPPED_FACT_RATIO`), so a two-fact fixture would swing from
pass to fail on a single omission and would "prove" a threshold that does not
hold for a real article.
"""

# Test inputs are Russian news copy, so Cyrillic letters that look like Latin
# ones are unavoidable and expected; RUF001 would flag every sentence.
# ruff: noqa: RUF001

from __future__ import annotations

import json
import re
from typing import Any, Final

import httpx
import pytest
import respx
from fakeredis.aioredis import FakeRedis

from app.config import settings
from app.models.vector import EMBEDDING_DIMENSIONS
from app.services.ai_client import AIClient, NoCredentialsError
from app.services.content_fixer import (
    MAX_DROPPED_FACT_RATIO,
    review_and_fix_article,
    review_article,
)
from app.services.content_prompts import MAX_SEO_DESCRIPTION_CHARS, MAX_TITLE_CHARS
from app.services.embedding_service import (
    CACHE_TTL_SECONDS,
    MAX_CACHEABLE_CHARS,
    RATE_LIMIT_MAX_ATTEMPTS,
    EmbeddingError,
    EmbeddingService,
    cache_key,
)

API_URL: Final[str] = "https://openrouter.test/api/v1"
API_KEY: Final[str] = "or-fixer-key-77af21"

#: The sources a draft is checked against. Deliberately fact-dense: a date, a
#: sum, several counts, an interval and a percentage, so the ratio-based
#: omission check has enough denominator to be meaningful.
SOURCES_TEXT: Final[str] = (
    "Мэрия Москвы открыла станцию метро «Пыхтино» на Солнцевской линии "
    "14 марта 2026 года. Строительство велось четыре года и обошлось городскому "
    "бюджету в 23 млрд рублей. Ожидается, что новым пересадочным узлом ежедневно "
    "будут пользоваться около 45 тысяч пассажиров. Интервал движения поездов в "
    "часы пик составит 90 секунд. Проектировщики применили безопорную "
    "конструкцию платформы, что позволило сократить сроки монтажа на 15 "
    "процентов. Рядом с новой станцией живут более 80 тысяч человек. На площадке "
    "было занято до 1200 рабочих."
)

#: A faithful draft: correct figures, required structure, coherent headline.
GOOD_BODY: Final[str] = (
    "<h2>Что открыли</h2>"
    "<p>Мэрия Москвы открыла станцию «Пыхтино» на Солнцевской линии 14 марта "
    "2026 года. Строительство обошлось городскому бюджету в 23 млрд рублей.</p>"
    "<ul>"
    "<li>Пассажиропоток — около 45 тысяч человек в сутки</li>"
    "<li>Интервал движения в часы пик — 90 секунд</li>"
    "<li>Рядом со станцией живут более 80 тысяч человек</li>"
    "</ul>"
    "<h2>Как строили</h2>"
    "<p>Безопорная конструкция платформы позволила сократить сроки монтажа на "
    "15 процентов. На площадке было занято до 1200 рабочих, работы велись "
    "круглосуточно в две смены.</p>"
    "<p>Станция связана тёплым переходом с автобусным терминалом, что позволяет "
    "пассажирам пересаживаться на пригородные маршруты без выхода на улицу. "
    "Проектировщики предусмотрели лифты для маломобильных пассажиров и "
    "тактильную плитку по всей длине платформы.</p>"
    "<h2>Что дальше</h2>"
    "<p>В департаменте транспорта уточнили, что до конца года на этой же линии "
    "планируется открыть ещё две станции. Благоустройство прилегающей "
    "территории продолжается: подрядчик высаживает деревья и завершает "
    "обустройство пешеходных подходов к вестибюлям.</p>"
)

GOOD_DRAFT: Final[dict[str, Any]] = {
    "title": "В Москве открыли станцию метро «Пыхтино»",
    "content_html": GOOD_BODY,
    "seo_title": "Станция метро «Пыхтино»: открытие на Солнцевской линии",
    "seo_description": (
        "Мэрия Москвы открыла станцию «Пыхтино»: 23 млрд рублей, 45 тысяч "
        "пассажиров в сутки."
    ),
    "seo_keywords": ["станция пыхтино", "солнцевская линия", "метро москвы"],
    "category_slug": "novosti-obshhestva",
    "image_prompt": (
        "Wide editorial photograph of a modern metro station platform, cool "
        "daylight, polished stone floor, photorealistic, no text, no logos."
    ),
}


def draft(**overrides: Any) -> dict[str, Any]:
    """Return a copy of the faithful draft with fields replaced."""
    payload = dict(GOOD_DRAFT)
    payload.update(overrides)
    return payload


def completion_response(content: str) -> httpx.Response:
    """Build an OpenRouter chat-completion response carrying ``content``."""
    return httpx.Response(
        200,
        json={
            "id": "fix-test",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}}
            ],
        },
    )


def json_completion(payload: dict[str, Any]) -> httpx.Response:
    """Build a completion whose message is ``payload`` as JSON."""
    return completion_response(json.dumps(payload, ensure_ascii=False))


def embeddings_response(
    count: int, *, width: int = EMBEDDING_DIMENSIONS
) -> httpx.Response:
    """Build an embeddings response with ``count`` vectors of ``width`` floats."""
    return httpx.Response(
        200,
        json={
            "data": [
                {"index": position, "embedding": [0.01 * (position + 1)] * width}
                for position in range(count)
            ]
        },
    )


def new_client() -> AIClient:
    """Return an AI client pointed at the mocked API, without inner retries."""
    return AIClient(api_key=API_KEY, base_url=API_URL, max_attempts=1)


@pytest.fixture
async def fake_redis() -> Any:
    """A flushed fake Redis, closed after each test.

    `decode_responses=True` matches `app.redis_client.get_redis`: the service
    compares JSON payloads as `str`, so a byte-mode client would exercise a
    different code path.
    """
    client = FakeRedis(decode_responses=True)
    await client.flushall()
    try:
        yield client
    finally:
        await client.flushall()
        # The pool must go too: a pooled connection reclaimed by the garbage
        # collector emits a ResourceWarning, which this suite treats as an error.
        await client.aclose(close_connection_pool=True)


# ==================================================== QA: a faithful draft


def test_a_faithful_draft_passes_with_no_findings() -> None:
    report = review_article(SOURCES_TEXT, GOOD_DRAFT)

    assert report.ok is True
    assert report.codes == ()
    assert report.invented_facts == ()


def test_the_source_facts_are_actually_extracted() -> None:
    # If extraction returned nothing the fidelity checks would pass vacuously,
    # so the fixture's own fact density is asserted.
    report = review_article(SOURCES_TEXT, GOOD_DRAFT)

    assert len(report.source_facts) >= 6
    assert "money:23000000000:RUB" in report.source_facts
    assert "date:2026-03-14" in report.source_facts
    assert "pct:15" in report.source_facts


async def test_a_clean_draft_costs_no_tokens() -> None:
    # The fast path: `assert_all_called=False` with no route registered means any
    # HTTP call at all would raise.
    with respx.mock(base_url=API_URL, assert_all_called=False):
        client = new_client()
        try:
            result = await review_and_fix_article(
                SOURCES_TEXT, GOOD_DRAFT, client=client
            )
        finally:
            await client.close()

    assert result["qa_fixed"] is False
    assert result["qa_attempts"] == 0
    assert result["qa_ok"] is True
    assert result["content_html"] == GOOD_BODY


# ================================================ QA: inconsistent numbers


def test_an_invented_sum_is_detected() -> None:
    bad = draft(content_html=GOOD_BODY.replace("23 млрд", "47 млрд"))

    report = review_article(SOURCES_TEXT, bad)

    assert report.ok is False
    assert "qa:invented_facts" in report.codes
    assert "money:47000000000:RUB" in report.invented_facts
    # Fabrication is always fatal, at any quantity.
    assert report.fatal


def test_an_invented_date_is_detected() -> None:
    bad = draft(content_html=GOOD_BODY.replace("14 марта 2026", "19 апреля 2026"))

    report = review_article(SOURCES_TEXT, bad)

    assert "qa:invented_facts" in report.codes
    assert "date:2026-04-19" in report.invented_facts


def test_an_invented_percentage_is_detected() -> None:
    bad = draft(content_html=GOOD_BODY.replace("15 процентов", "63 процента"))

    report = review_article(SOURCES_TEXT, bad)

    assert "qa:invented_facts" in report.codes
    assert "pct:63" in report.invented_facts


def test_a_changed_headcount_is_detected() -> None:
    bad = draft(content_html=GOOD_BODY.replace("45 тысяч", "450 тысяч"))

    report = review_article(SOURCES_TEXT, bad)

    assert "qa:invented_facts" in report.codes
    assert "num:450000" in report.invented_facts


def test_small_incidental_numbers_are_not_treated_as_fabrication() -> None:
    # "три станции" is prose, not a statistic. Flagging it would reject sound
    # drafts, so bare integers at or below the ceiling are exempt.
    body = GOOD_BODY.replace(
        "<h2>Как строили</h2>", "<h2>Как строили</h2><p>Рядом работают три станции.</p>"
    )

    report = review_article(SOURCES_TEXT, draft(content_html=body))

    assert "qa:invented_facts" not in report.codes


def test_a_large_unmatched_number_is_still_fabrication() -> None:
    # Above the ceiling the same reasoning does not apply: a five-figure count
    # absent from every source is a claim.
    body = GOOD_BODY.replace(
        "<h2>Как строили</h2>",
        "<h2>Как строили</h2><p>Проект затронул 98700 жителей.</p>",
    )

    report = review_article(SOURCES_TEXT, draft(content_html=body))

    assert "qa:invented_facts" in report.codes
    assert "num:98700" in report.invented_facts


def test_modest_omission_is_tolerated() -> None:
    # Condensing a wire story is what editing is. A draft that keeps the headline
    # figures but drops a secondary one must not be rejected.
    trimmed = (
        "<h2>Что открыли</h2>"
        "<p>Мэрия Москвы открыла станцию «Пыхтино» 14 марта 2026 года. "
        "Строительство обошлось бюджету в 23 млрд рублей.</p>"
        "<ul><li>Пассажиропоток — около 45 тысяч человек в сутки</li>"
        "<li>Интервал движения — 90 секунд</li>"
        "<li>Рядом живут более 80 тысяч человек</li></ul>"
        "<h2>Как строили</h2><p>Сроки монтажа сократились на 15 процентов.</p>"
    )

    report = review_article(SOURCES_TEXT, draft(content_html=trimmed))

    assert "qa:dropped_facts" not in report.codes


def test_losing_most_of_the_facts_is_reported() -> None:
    gutted = (
        "<h2>Открытие</h2>"
        "<p>Мэрия Москвы открыла новую станцию метро на Солнцевской линии "
        "города, сообщили в пресс-службе мэрии.</p>"
        "<ul><li>Станция принимает пассажиров</li></ul>"
        "<h2>Подробности</h2>"
        "<p>Работы по благоустройству прилегающей территории продолжаются.</p>"
    )

    report = review_article(SOURCES_TEXT, draft(content_html=gutted))

    assert "qa:dropped_facts" in report.codes
    assert report.dropped_facts
    assert len(report.dropped_facts) / len(report.source_facts) > MAX_DROPPED_FACT_RATIO


def test_a_draft_without_sources_is_not_accused_of_omission() -> None:
    # No source facts means no denominator: dividing would raise, and there is
    # nothing to have dropped.
    report = review_article("Пресс-служба сообщила об открытии.", GOOD_DRAFT)

    assert "qa:dropped_facts" not in report.codes


# ======================================================= QA: structure & meta


def test_a_missing_h2_is_reported() -> None:
    body = re.sub(r"<h2>.*?</h2>", "", GOOD_BODY)
    assert "<h2>" not in body

    report = review_article(SOURCES_TEXT, draft(content_html=body))

    assert "qa:missing_h2" in report.codes
    assert report.fatal


def test_a_missing_list_is_reported_but_not_fatal() -> None:
    # A short incident report legitimately has nothing to enumerate, and forcing
    # a list would produce filler bullets — slop by another route.
    body = "<h2>Что открыли</h2><p>Мэрия открыла станцию за 23 млрд рублей.</p>"

    report = review_article(SOURCES_TEXT, draft(content_html=body))

    assert "qa:missing_list" in report.codes
    listed = next(issue for issue in report.issues if issue.code == "qa:missing_list")
    assert listed.fatal is False


def test_an_empty_body_is_fatal() -> None:
    report = review_article(SOURCES_TEXT, draft(content_html="   "))

    assert "qa:empty_body" in report.codes
    assert report.fatal


def test_an_overlong_title_is_reported() -> None:
    report = review_article(
        SOURCES_TEXT, draft(title="Станция " + "и" * MAX_TITLE_CHARS)
    )

    assert "qa:title_too_long" in report.codes


def test_an_overlong_description_is_reported() -> None:
    long_description = "Станция открыта. " * MAX_SEO_DESCRIPTION_CHARS

    report = review_article(SOURCES_TEXT, draft(seo_description=long_description))

    assert "qa:description_too_long" in report.codes


def test_a_headline_about_another_event_is_reported() -> None:
    report = review_article(
        SOURCES_TEXT, draft(title="Землетрясение произошло в отдалённом районе")
    )

    assert "qa:title_body_mismatch" in report.codes


def test_inflection_does_not_read_as_a_mismatch() -> None:
    # The headline's "станция" against the body's "станцию": whole-word
    # comparison would report a false mismatch on a coherent draft, which is why
    # the check compares stems.
    report = review_article(SOURCES_TEXT, draft(title="Станция «Пыхтино» открыта"))

    assert "qa:title_body_mismatch" not in report.codes


def test_residual_ai_slop_is_reported() -> None:
    body = GOOD_BODY.replace(
        "<h2>Что открыли</h2>",
        "<h2>Что открыли</h2><p>В современном мире метро меняется. Подводя итог, "
        "это является ключевым фактором.</p>",
    )

    report = review_article(SOURCES_TEXT, draft(content_html=body))

    assert "qa:ai_slop" in report.codes


# ==================================================== QA: the repair flow


async def test_a_defective_draft_is_repaired_by_the_model() -> None:
    broken = draft(content_html=GOOD_BODY.replace("23 млрд", "47 млрд"))

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=json_completion(GOOD_DRAFT)
        )
        client = new_client()
        try:
            result = await review_and_fix_article(SOURCES_TEXT, broken, client=client)
        finally:
            await client.close()

    assert route.call_count == 1
    assert result["qa_fixed"] is True
    assert result["qa_attempts"] == 1
    assert result["qa_ok"] is True
    assert "47 млрд" not in result["content_html"]
    assert "qa:invented_facts" in result["qa_issues"]
    assert result["qa_remaining_issues"] == []


async def test_the_repair_request_enumerates_the_defects_and_the_sources() -> None:
    broken = draft(content_html=GOOD_BODY.replace("23 млрд", "47 млрд"))

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=json_completion(GOOD_DRAFT)
        )
        client = new_client()
        try:
            await review_and_fix_article(SOURCES_TEXT, broken, client=client)
        finally:
            await client.close()

    sent = json.loads(route.calls[0].request.content)
    system, user = sent["messages"][0]["content"], sent["messages"][1]["content"]

    # Framed as copy-editing, not writing: an open rewrite would reintroduce the
    # clichés the generator was forced to avoid.
    assert "Не переписывайте материал заново" in system
    # The model needs the real figure, or it will invent a replacement for the
    # one it was told to remove.
    assert "23 млрд" in user
    assert "money:47000000000:RUB" in user
    assert sent["temperature"] < 0.5


async def test_the_fast_model_is_used_by_default() -> None:
    broken = draft(content_html=GOOD_BODY.replace("23 млрд", "47 млрд"))

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=json_completion(GOOD_DRAFT)
        )
        client = new_client()
        try:
            await review_and_fix_article(SOURCES_TEXT, broken, client=client)
        finally:
            await client.close()

    # A targeted correction does not need the expensive writer model.
    assert json.loads(route.calls[0].request.content)["model"] == settings.ai_model_seo


async def test_an_explicit_model_is_forwarded() -> None:
    broken = draft(content_html=GOOD_BODY.replace("23 млрд", "47 млрд"))

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=json_completion(GOOD_DRAFT)
        )
        client = new_client()
        try:
            await review_and_fix_article(
                SOURCES_TEXT, broken, "deepseek/deepseek-chat", client=client
            )
        finally:
            await client.close()

    assert json.loads(route.calls[0].request.content)["model"] == (
        "deepseek/deepseek-chat"
    )


async def test_a_malformed_repair_keeps_the_original_draft() -> None:
    broken = draft(content_html=GOOD_BODY.replace("23 млрд", "47 млрд"))

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        mock.post("/chat/completions").mock(
            return_value=completion_response("Извините, не могу помочь.")
        )
        client = new_client()
        try:
            result = await review_and_fix_article(SOURCES_TEXT, broken, client=client)
        finally:
            await client.close()

    # The original is a known quantity; a malformed replacement is not.
    assert result["qa_fixed"] is False
    assert result["qa_attempts"] == 1
    assert result["content_html"] == broken["content_html"]
    assert result["qa_ok"] is False


async def test_a_repair_that_does_not_help_is_rejected() -> None:
    broken = draft(content_html=GOOD_BODY.replace("23 млрд", "47 млрд"))
    # Schema-valid, but it fabricates a different figure instead of fixing the one
    # it was asked about.
    still_wrong = draft(content_html=GOOD_BODY.replace("23 млрд", "51 млрд"))

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        mock.post("/chat/completions").mock(
            return_value=json_completion(still_wrong)
        )
        client = new_client()
        try:
            result = await review_and_fix_article(SOURCES_TEXT, broken, client=client)
        finally:
            await client.close()

    # Taking a lateral move would let a draft degrade across pipeline stages.
    assert result["qa_fixed"] is False
    assert "47 млрд" in result["content_html"]


async def test_repairable_slop_is_fixed_without_an_api_call() -> None:
    hedged = draft(
        content_html=GOOD_BODY.replace(
            "<p>Мэрия Москвы открыла станцию",
            "<p>Стоит отметить, что мэрия Москвы открыла станцию",
        )
    )

    with respx.mock(base_url=API_URL, assert_all_called=False):
        client = new_client()
        try:
            result = await review_and_fix_article(SOURCES_TEXT, hedged, client=client)
        finally:
            await client.close()

    # Paying for a rewrite to delete "стоит отметить, что" would be indefensible.
    assert result["qa_fixed"] is True
    assert result["qa_attempts"] == 0
    assert "стоит отметить" not in result["content_html"].lower()
    assert "23 млрд рублей" in result["content_html"]


async def test_the_qa_trail_travels_with_the_article() -> None:
    broken = draft(content_html=GOOD_BODY.replace("23 млрд", "47 млрд"))

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        mock.post("/chat/completions").mock(return_value=json_completion(GOOD_DRAFT))
        client = new_client()
        try:
            result = await review_and_fix_article(SOURCES_TEXT, broken, client=client)
        finally:
            await client.close()

    # The dashboard has to be able to explain why an article was corrected.
    for key in (
        "qa_fixed",
        "qa_attempts",
        "qa_issues",
        "qa_ok",
        "qa_remaining_issues",
        "qa_dropped_facts",
        "qa_invented_facts",
    ):
        assert key in result
    # The generator's own fields survive alongside the QA metadata.
    assert result["category_slug"] == "novosti-obshhestva"
    assert result["image_prompt"] == GOOD_DRAFT["image_prompt"]


# ============================================== embeddings: width & validation


async def test_an_embedding_has_the_schema_width(fake_redis: Any) -> None:
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        mock.post("/embeddings").mock(return_value=embeddings_response(1))
        client = new_client()
        service = EmbeddingService(redis=fake_redis, ai_client=client)
        try:
            vector = await service.embed("станция метро «Пыхтино»")
        finally:
            await service.close()
            await client.close()

    assert len(vector) == EMBEDDING_DIMENSIONS
    # 384: the width of the pinned local fastembed model, which replaced the
    # gateway's 1536-dim embeddings when they moved on-worker for zero tokens.
    assert EMBEDDING_DIMENSIONS == 384
    assert all(isinstance(value, float) for value in vector)


async def test_a_wrong_width_vector_is_rejected(fake_redis: Any) -> None:
    # pgvector would reject this at INSERT, far from the model that produced it.
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        mock.post("/embeddings").mock(return_value=embeddings_response(1, width=512))
        client = new_client()
        service = EmbeddingService(redis=fake_redis, ai_client=client)
        try:
            with pytest.raises(Exception) as info:
                await service.embed("текст")
        finally:
            await service.close()
            await client.close()

    assert "512" in str(info.value)


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
async def test_an_empty_text_is_refused(fake_redis: Any, text: str) -> None:
    # A degenerate vector would sit in the HNSW index and match everything.
    service = EmbeddingService(redis=fake_redis, ai_client=new_client())
    try:
        with pytest.raises(EmbeddingError):
            await service.embed(text)
    finally:
        await service.close()


# ==================================================== embeddings: the cache


async def test_a_second_call_is_served_from_the_cache(fake_redis: Any) -> None:
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/embeddings").mock(return_value=embeddings_response(1))
        client = new_client()
        service = EmbeddingService(redis=fake_redis, ai_client=client)
        try:
            first = await service.embed("станция метро «Пыхтино»")
            second = await service.embed("станция метро «Пыхтино»")
        finally:
            await service.close()
            await client.close()

    assert first == second
    # The point of the cache: one paid call for two requests.
    assert route.call_count == 1


async def test_the_cache_entry_carries_a_24_hour_ttl(fake_redis: Any) -> None:
    text = "станция метро «Пыхтино»"

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        mock.post("/embeddings").mock(return_value=embeddings_response(1))
        client = new_client()
        service = EmbeddingService(redis=fake_redis, ai_client=client)
        try:
            await service.embed(text)
        finally:
            await service.close()
            await client.close()

    key = cache_key(text, model=settings.ai_model_embedding)
    ttl = int(await fake_redis.ttl(key))

    # Redis runs with `noeviction`, so an entry without an expiry would stay
    # until the instance refuses writes — taking the ARQ broker down with it.
    assert 0 < ttl <= CACHE_TTL_SECONDS
    assert ttl > CACHE_TTL_SECONDS - 60


async def test_the_cache_key_is_scoped_to_the_model() -> None:
    # Vectors from two models are not interchangeable: cosine distance between
    # different embedding spaces is noise.
    first = cache_key("текст", model="openai/text-embedding-3-small")
    second = cache_key("текст", model="openai/text-embedding-3-large")

    assert first != second
    # The text itself is hashed, not embedded in the key.
    assert "текст" not in first


async def test_a_long_text_is_embedded_but_not_cached(fake_redis: Any) -> None:
    long_text = "станция " * (MAX_CACHEABLE_CHARS // 4)

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/embeddings").mock(
            side_effect=[embeddings_response(1), embeddings_response(1)]
        )
        client = new_client()
        service = EmbeddingService(redis=fake_redis, ai_client=client)
        try:
            await service.embed(long_text)
            await service.embed(long_text)
        finally:
            await service.close()
            await client.close()

    # A cache entry costs ~25 KB regardless of input size, and a one-off article
    # body will never be read twice.
    assert route.call_count == 2
    assert await fake_redis.dbsize() == 0


async def test_a_corrupt_cache_entry_is_refetched(fake_redis: Any) -> None:
    text = "станция метро"
    key = cache_key(text, model=settings.ai_model_embedding)
    await fake_redis.set(key, "not json")

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        mock.post("/embeddings").mock(return_value=embeddings_response(1))
        client = new_client()
        service = EmbeddingService(redis=fake_redis, ai_client=client)
        try:
            vector = await service.embed(text)
        finally:
            await service.close()
            await client.close()

    assert len(vector) == EMBEDDING_DIMENSIONS


async def test_a_cached_vector_of_the_wrong_width_is_refetched(fake_redis: Any) -> None:
    # An entry predating a dimension change must not be served.
    text = "станция метро"
    key = cache_key(text, model=settings.ai_model_embedding)
    await fake_redis.set(key, json.dumps([0.1] * 8))

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/embeddings").mock(return_value=embeddings_response(1))
        client = new_client()
        service = EmbeddingService(redis=fake_redis, ai_client=client)
        try:
            vector = await service.embed(text)
        finally:
            await service.close()
            await client.close()

    assert route.call_count == 1
    assert len(vector) == EMBEDDING_DIMENSIONS


async def test_a_redis_outage_degrades_to_the_api(fake_redis: Any) -> None:
    # The cache is an optimisation: an outage must cost money, not correctness.
    await fake_redis.aclose(close_connection_pool=True)

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        mock.post("/embeddings").mock(return_value=embeddings_response(1))
        client = new_client()
        service = EmbeddingService(redis=fake_redis, ai_client=client)
        try:
            vector = await service.embed("станция метро")
        finally:
            await client.close()

    assert len(vector) == EMBEDDING_DIMENSIONS


# ==================================================== embeddings: batching


async def test_a_batch_is_one_request_and_keeps_input_order(fake_redis: Any) -> None:
    texts = ["первый текст", "второй текст", "третий текст"]

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/embeddings").mock(return_value=embeddings_response(3))
        client = new_client()
        service = EmbeddingService(redis=fake_redis, ai_client=client)
        try:
            vectors = await service.embed_batch(texts)
        finally:
            await service.close()
            await client.close()

    assert route.call_count == 1
    assert len(vectors) == 3
    # Distinct per-input values, in input order: a batch that returned them
    # shuffled would silently attach the wrong vector to each cluster.
    assert vectors[0][0] < vectors[1][0] < vectors[2][0]


async def test_a_repeated_text_in_a_batch_is_paid_for_once(fake_redis: Any) -> None:
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/embeddings").mock(return_value=embeddings_response(2))
        client = new_client()
        service = EmbeddingService(redis=fake_redis, ai_client=client)
        try:
            vectors = await service.embed_batch(["один", "два", "один"])
        finally:
            await service.close()
            await client.close()

    # Two unique inputs, so two vectors requested; the duplicate is expanded on
    # the way out.
    sent = json.loads(route.calls[0].request.content)
    assert sent["input"] == ["один", "два"]
    assert vectors[0] == vectors[2]


async def test_a_partially_cached_batch_only_fetches_the_misses(
    fake_redis: Any,
) -> None:
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/embeddings").mock(
            side_effect=[embeddings_response(1), embeddings_response(1)]
        )
        client = new_client()
        service = EmbeddingService(redis=fake_redis, ai_client=client)
        try:
            await service.embed("уже в кэше")
            vectors = await service.embed_batch(["уже в кэше", "новый текст"])
        finally:
            await service.close()
            await client.close()

    assert len(vectors) == 2
    assert route.call_count == 2
    assert json.loads(route.calls[1].request.content)["input"] == ["новый текст"]


async def test_an_empty_batch_makes_no_request(fake_redis: Any) -> None:
    with respx.mock(base_url=API_URL, assert_all_called=False):
        service = EmbeddingService(redis=fake_redis, ai_client=new_client())
        try:
            assert await service.embed_batch([]) == []
        finally:
            await service.close()


# =============================================== embeddings: rate limiting


async def test_a_sustained_rate_limit_is_retried_then_raised(
    fake_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The waits are real seconds in production; patched here so the test does not
    # sit for twenty of them.
    monkeypatch.setattr(
        "app.services.embedding_service.RATE_LIMIT_BACKOFF_SECONDS", (0.0,)
    )

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/embeddings").mock(
            return_value=httpx.Response(429, text="rate limited")
        )
        client = new_client()
        service = EmbeddingService(redis=fake_redis, ai_client=client)
        try:
            with pytest.raises(Exception) as info:
                await service.embed("текст")
        finally:
            await service.close()
            await client.close()

    # The inner client already retried, so reaching the outer budget means the
    # quota is genuinely exhausted rather than momentarily saturated.
    assert route.call_count == RATE_LIMIT_MAX_ATTEMPTS
    assert "429" in str(info.value)


async def test_a_transient_rate_limit_recovers(
    fake_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.services.embedding_service.RATE_LIMIT_BACKOFF_SECONDS", (0.0,)
    )

    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/embeddings").mock(
            side_effect=[
                httpx.Response(429, text="slow down"),
                embeddings_response(1),
            ]
        )
        client = new_client()
        service = EmbeddingService(redis=fake_redis, ai_client=client)
        try:
            vector = await service.embed("текст")
        finally:
            await service.close()
            await client.close()

    assert route.call_count == 2
    assert len(vector) == EMBEDDING_DIMENSIONS


async def test_an_unconfigured_client_raises_rather_than_returning_zeros() -> None:
    # A zero vector would poison every similarity query it reached.
    client = AIClient(api_key="", base_url=API_URL)
    service = EmbeddingService(redis=None, ai_client=client)
    try:
        with pytest.raises(NoCredentialsError):
            await service.embed("текст")
    finally:
        await client.close()


async def test_the_api_key_never_appears_in_an_error(fake_redis: Any) -> None:
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        mock.post("/embeddings").mock(return_value=httpx.Response(400, text="bad"))
        client = new_client()
        service = EmbeddingService(redis=fake_redis, ai_client=client)
        try:
            with pytest.raises(Exception) as info:
                await service.embed("текст")
        finally:
            await service.close()
            await client.close()

    assert API_KEY not in str(info.value)
