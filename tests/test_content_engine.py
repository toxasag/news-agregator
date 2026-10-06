"""Tests for the content engine: slop blocklist, prompt matrix, generation.

Nothing here reaches the network. The OpenRouter API is served by `respx`, which
intercepts the transport, so no DNS lookup and no socket happen — the same
approach `tests/test_ingestion.py` uses for the GramJS bridge. The database is
not involved at all: the generator returns a plain `dict` by design.

Fixtures are of realistic length. The slop threshold is a *density* per 1000
words, so a 20-word snippet would fail on arithmetic rather than on style and
would "prove" a threshold that does not hold for a real article.
"""

# Test inputs are Russian editorial copy, so Cyrillic letters that look like
# Latin ones are unavoidable and expected; RUF001 would flag every sentence.
# ruff: noqa: RUF001

from __future__ import annotations

import json
from typing import Any, Final

import httpx
import pytest
import respx

from app.config import settings
from app.services.ai_client import AIClient, NoCredentialsError
from app.services.content_generator import (
    GENERATION_MAX_TOKENS,
    MAX_GENERATION_ATTEMPTS,
    GeneratedArticle,
    GenerationRejectedError,
    SchemaViolationError,
    generate_article,
    parse_generation,
    validate_generation,
)
from app.services.content_prompts import (
    CATEGORIES,
    CATEGORY_SLUGS,
    DEFAULT_CATEGORY,
    MAX_SEO_DESCRIPTION_CHARS,
    MAX_SEO_KEYWORDS,
    MAX_TITLE_CHARS,
    MIN_SEO_KEYWORDS,
    REQUIRED_FIELDS,
    RESPONSE_JSON_SCHEMA,
    build_messages,
    build_system_prompt,
    build_user_prompt,
    get_category,
    response_format,
)
from app.services.slop_filter import (
    BLOCKLIST,
    MAX_SLOP_PER_1000_WORDS,
    SlopSeverity,
    analyze_slop,
    clean_ai_slop,
    describe_blocklist,
    slop_density,
    validate_ai_text,
)

#: Base URL of the mocked OpenRouter API. Never resolved.
API_URL: Final[str] = "https://openrouter.test/api/v1"

#: A token that must never appear in an exception message or a log line.
API_KEY: Final[str] = "or-test-key-8c31de"

#: Expected size of the editorial matrix, per the ROADMAP requirement.
EXPECTED_CATEGORY_COUNT: Final[int] = 20


# ============================================================== text fixtures


def _filler(paragraphs: int = 4) -> str:
    """Return clean, news-length body paragraphs.

    Used to pad a fixture past the minimum article length so that a test about
    *style* is not accidentally decided by the length rule.
    """
    block = (
        "<p>Станция расположена рядом с жилым районом, где живут более 80 тысяч "
        "человек, и связана тёплым переходом с автобусным терминалом. "
        "Проектировщики применили безопорную конструкцию платформы, что "
        "позволило сократить сроки монтажа на 15 процентов.</p>"
        "<p>В департаменте транспорта уточнили, что интервал движения поездов в "
        "часы пик составит 90 секунд. Строительные работы велись круглосуточно, "
        "на площадке было занято до 1200 рабочих и 40 единиц техники.</p>"
    )
    return block * paragraphs


#: A body with no blocklist hits: facts, numbers, named sources.
CLEAN_BODY: Final[str] = (
    "<h2>Что открыли</h2>"
    "<p>Мэрия Москвы ввела в эксплуатацию станцию «Пыхтино» на Солнцевской "
    "линии. Строительство велось четыре года и обошлось бюджету города в 23 "
    "миллиарда рублей.</p>"
    "<ul><li>Пассажиропоток — около 45 тысяч человек в сутки</li>"
    "<li>Интервал движения в часы пик — 90 секунд</li></ul>"
    "<h2>Что дальше</h2>" + _filler()
)

#: A body carrying fatal clichés in the positions models favour: an opening
#: warm-up, a tour-guide transition, a summary close and consultant vocabulary.
DIRTY_BODY: Final[str] = (
    "<h2>Введение</h2>"
    "<p>В современном мире городской транспорт меняется стремительно. "
    "Не секрет, что метро является ключевым фактором развития мегаполиса.</p>"
    "<p>Погрузимся в детали этого проекта и давайте рассмотрим его подробнее.</p>"
    "<h2>Итоги</h2>"
    "<p>Подводя итог, стоит отметить, что открытие станции — это неотъемлемая "
    "часть комплексного подхода к решению транспортных задач. Время покажет.</p>"
    + _filler()
)

#: A body whose only defects are repairable hedges — no fatal cliché — at a
#: density just above budget, so `clean_ai_slop` is what makes it publishable.
#: Deliberately not padded further: with enough filler the density alone would
#: pass and the repair path would never be exercised.
SOFT_BODY: Final[str] = (
    "<h2>Что изменилось</h2>"
    "<p>Стоит отметить, что мэрия ввела станцию «Пыхтино» в эксплуатацию. "
    "Строительство обошлось бюджету в 23 миллиарда рублей.</p>"
    "<p>Важно понимать, что интервал движения поездов в часы пик составит "
    "90 секунд.</p>"
    "<p>Как известно, станция связана переходом с автобусным терминалом.</p>"
    "<p>Очевидно, что пассажиропоток вырастет до 45 тысяч человек в сутки.</p>"
    "<h2>Детали проекта</h2>"
    "<p>Проектировщики применили безопорную конструкцию платформы, что "
    "позволило сократить сроки монтажа на 15 процентов. Строительные работы "
    "велись круглосуточно, на площадке было занято до 1200 рабочих и 40 единиц "
    "техники.</p>"
    "<p>Станция расположена рядом с жилым районом, где живут более 80 тысяч "
    "человек. До конца года на этой же линии откроют ещё две станции, сообщили "
    "в департаменте транспорта города.</p>"
    "<p>На объекте установлены лифты для маломобильных пассажиров, тактильная "
    "плитка и система навигации с дублированием на английском языке. Первые "
    "пассажиры прошли через турникеты через час после церемонии.</p>"
)

#: The English half of the blocklist, for models that leak their register.
ENGLISH_SLOP_BODY: Final[str] = (
    "<h2>Overview</h2>"
    "<p>Let's dive into the ever-changing landscape of urban transport. "
    "The new station is a testament to the city's ambition.</p>"
    "<h2>Details</h2>" + _filler()
)

CLUSTER_DATA: Final[dict[str, Any]] = {
    "title": "Открыта станция метро «Пыхтино»",
    "text": "Мэрия Москвы открыла станцию «Пыхтино» на Солнцевской линии.",
    "facts": [
        "date:2026-03-14",
        "money:23000000000:RUB",
        "num:45000",
        "ent:сергей собянин",
    ],
    "sources": [
        {"text": "Открылась станция «Пыхтино».", "url": "https://t.me/channel/48213"},
        {"text": "Стоимость — 23 млрд рублей.", "url": "https://example.ru/news/1"},
    ],
}


def valid_payload(**overrides: Any) -> dict[str, Any]:
    """Build a schema-valid LLM reply, with optional field overrides."""
    payload: dict[str, Any] = {
        "title": "В Москве открыли станцию метро «Пыхтино»",
        "content_html": CLEAN_BODY,
        "seo_title": "Станция метро «Пыхтино»: открытие на Солнцевской линии",
        "seo_description": (
            "Мэрия Москвы открыла станцию «Пыхтино» на Солнцевской линии: "
            "23 млрд рублей, 45 тысяч пассажиров в сутки."
        ),
        "seo_keywords": ["станция пыхтино", "солнцевская линия", "метро москвы"],
        "category_slug": "novosti-obshhestva",
        "image_prompt": (
            "Wide editorial photograph of a newly opened modern metro station "
            "platform, cool daylight from skylights, polished stone floor, "
            "shallow depth of field, photorealistic, no text, no logos."
        ),
    }
    payload.update(overrides)
    return payload


# ================================================================ transport


def completion_response(content: str) -> httpx.Response:
    """Build an OpenRouter chat-completion response carrying ``content``."""
    return httpx.Response(
        200,
        json={
            "id": "gen-test",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}}
            ],
        },
    )


def json_completion(payload: dict[str, Any]) -> httpx.Response:
    """Build a completion whose message is ``payload`` serialised as JSON."""
    return completion_response(json.dumps(payload, ensure_ascii=False))


def new_client() -> AIClient:
    """Return an AI client pointed at the mocked API."""
    return AIClient(api_key=API_KEY, base_url=API_URL, max_attempts=1)


@respx.mock
def _unused() -> None:  # pragma: no cover - marker for the decorator import
    """Keep `respx` imported at module scope for the context-manager style below."""


# ====================================================== slop: dirty vs clean


def test_clean_editorial_copy_passes() -> None:
    valid, detected = validate_ai_text(CLEAN_BODY)

    assert valid is True
    assert detected == []


def test_fatal_cliches_are_detected_and_reject_the_text() -> None:
    valid, detected = validate_ai_text(DIRTY_BODY)

    assert valid is False
    # The exact phrases are reported, not rule ids: an operator has to see what
    # the model actually wrote.
    assert "в современном мире" in detected
    assert "погрузимся в детали" in detected
    assert "подводя итог" in detected
    assert "является ключевым фактором" in detected


@pytest.mark.parametrize(
    "phrase",
    [
        "В современном мире всё иначе.",
        "Не секрет, что это важно.",
        "Давайте рассмотрим подробнее.",
        "В этой статье мы расскажем о том, как это работает.",
        "В заключение отметим детали.",
        "Время покажет, чем это закончится.",
        "Проект играет важную роль в развитии города.",
        "Это неотъемлемая часть городской системы.",
        "Решение вывело отрасль на новый уровень.",
        "Требуется комплексный подход к решению задачи.",
        "Таким образом, мы видим устойчивый рост.",
        "Трудно переоценить значение этого шага.",
    ],
)
def test_each_fatal_pattern_family_is_caught(phrase: str) -> None:
    report = analyze_slop(phrase)

    assert report.fatal, f"{phrase!r} passed the blocklist unnoticed"


@pytest.mark.parametrize(
    "phrase",
    [
        "Delve into the details of the reform.",
        "In conclusion, the market remains volatile.",
        "It is worth noting that the rate changed.",
        "The launch is a testament to the team's work.",
        "Navigating the complexities of the tax code.",
    ],
)
def test_english_slop_is_caught(phrase: str) -> None:
    report = analyze_slop(phrase)

    assert report.fatal, f"{phrase!r} passed the blocklist unnoticed"


def test_english_slop_body_is_rejected() -> None:
    valid, detected = validate_ai_text(ENGLISH_SLOP_BODY)

    assert valid is False
    assert detected


def test_yo_and_typographic_variants_do_not_evade_detection() -> None:
    # The model may spell the same cliché with `ё`, an en dash or curly quotes;
    # normalisation is what makes one pattern cover every variant.
    with_yo = "Подводя итоги, всё изменилось."
    report = analyze_slop(with_yo)

    assert report.fatal


def test_a_cliche_split_by_inline_tags_is_still_detected() -> None:
    # Detection runs on text extracted from the markup, so emphasis inside the
    # phrase cannot hide it.
    split = "<p>В <b>современном</b> мире всё меняется.</p>"
    report = analyze_slop(split)

    assert report.fatal


def test_markup_never_fuses_words_across_tags() -> None:
    # `<p>слово</p><p>другое</p>` must not become `словодругое`: a fused token
    # would corrupt the word count that the density threshold divides by.
    report = analyze_slop("<p>первое</p><p>второе</p>")

    assert report.word_count == 2


def test_empty_text_is_invalid_rather_than_clean() -> None:
    # An empty article is a generation failure; reporting it as clean would let
    # it reach the database.
    for value in ("", "   ", "\n\t"):
        valid, detected = validate_ai_text(value)
        assert valid is False
        assert detected == ["empty_text"]


def test_density_is_normalised_by_length() -> None:
    single_hedge = "<p>Стоит отметить, что ставка выросла.</p>"
    padded = f"<p>Стоит отметить, что ставка выросла.</p>{_filler(8)}"

    # The same lone hedge: fatal by density in a fragment, tolerable in an
    # article. That is the point of scoring per 1000 words.
    assert slop_density(single_hedge) > MAX_SLOP_PER_1000_WORDS
    assert slop_density(padded) <= MAX_SLOP_PER_1000_WORDS


def test_overlapping_rules_report_one_finding() -> None:
    # `стоит отметить, что` also matches the bare `стоит отметить,` rule.
    # Counting both would double the density and wrongly reject the draft.
    report = analyze_slop("<p>Стоит отметить, что ставка выросла.</p>")

    assert len(report.findings) == 1
    assert report.reasons == ["стоит отметить, что"]


# ==================================================== slop: cleaning behaviour


def test_clean_ai_slop_removes_hedges_and_restores_capitalisation() -> None:
    cleaned = clean_ai_slop("<p>Стоит отметить, что ставка выросла на 2 процента.</p>")

    assert cleaned == "<p>Ставка выросла на 2 процента.</p>"


def test_clean_ai_slop_makes_a_soft_body_valid() -> None:
    assert validate_ai_text(SOFT_BODY)[0] is False

    cleaned = clean_ai_slop(SOFT_BODY)

    assert validate_ai_text(cleaned)[0] is True
    # The facts survived the repair.
    assert "23 миллиарда рублей" in cleaned
    assert "«Пыхтино»" in cleaned


def test_clean_ai_slop_preserves_markup_structure() -> None:
    cleaned = clean_ai_slop(SOFT_BODY)

    assert cleaned.count("<h2>") == SOFT_BODY.count("<h2>")
    assert cleaned.count("<p>") == SOFT_BODY.count("<p>")


def test_clean_ai_slop_is_idempotent() -> None:
    once = clean_ai_slop(SOFT_BODY)

    assert clean_ai_slop(once) == once


def test_clean_ai_slop_leaves_fatal_cliches_in_place() -> None:
    # Deliberate: a fatal phrase occupies a structural position, so deleting it
    # would leave a stub sentence. The draft must be regenerated instead, and
    # `validate_ai_text` is what says so.
    cleaned = clean_ai_slop(DIRTY_BODY)

    assert "в современном мире" in cleaned.lower()
    assert validate_ai_text(cleaned)[0] is False


def test_clean_ai_slop_on_clean_text_changes_nothing_material() -> None:
    assert clean_ai_slop(CLEAN_BODY) == CLEAN_BODY.strip()


def test_blocklist_covers_both_severities() -> None:
    severities = {rule.severity for rule in BLOCKLIST}

    assert severities == {SlopSeverity.FATAL, SlopSeverity.SOFT}
    # The rules panel in the dashboard reads this.
    assert len(describe_blocklist()) == len(BLOCKLIST)
    assert all(name.startswith("slop:") for name, _ in describe_blocklist())


# ================================================== prompts: category matrix


def test_the_matrix_carries_twenty_categories() -> None:
    assert len(CATEGORIES) == EXPECTED_CATEGORY_COUNT
    assert len(CATEGORY_SLUGS) == EXPECTED_CATEGORY_COUNT
    # Slugs are persisted to `articles.category` and used as WordPress slugs, so
    # a duplicate would silently merge two rubrics.
    assert len(set(CATEGORY_SLUGS)) == EXPECTED_CATEGORY_COUNT


@pytest.mark.parametrize(
    "slug",
    [
        "novosti-obshhestva",
        "chp-i-proisshestvij",
        "novosti-it-i-tehnologij",
        "ekonomika",
        "novosti-politiki",
        "avtonovosti",
        "novosti-biznesa",
        "novosti-nauki",
    ],
)
def test_the_categories_named_in_the_roadmap_exist(slug: str) -> None:
    assert slug in CATEGORIES
    assert get_category(slug).slug == slug


def test_every_category_declares_a_full_editorial_brief() -> None:
    for slug, category in CATEGORIES.items():
        assert category.slug == slug
        for field_name in ("title_ru", "voice", "lead", "avoid", "structure"):
            value = getattr(category, field_name)
            assert value.strip(), f"{slug}.{field_name} is empty"
        # A slug is a URL component: lowercase latin, digits and hyphens only.
        assert slug == slug.lower()
        assert all(char.isalnum() or char == "-" for char in slug)


@pytest.mark.parametrize("slug", CATEGORY_SLUGS)
def test_every_system_prompt_carries_its_voice_and_the_slop_rules(slug: str) -> None:
    prompt = build_system_prompt(slug)
    category = CATEGORIES[slug]

    assert category.title_ru in prompt
    assert category.voice in prompt
    assert category.avoid in prompt
    # The prohibition is restated in every prompt: cheaper than a retry.
    assert "В современном мире" in prompt
    assert "Подводя итог" in prompt
    assert "JSON" in prompt


def test_system_prompts_differ_between_categories() -> None:
    # A single generic prompt would produce one flat register for twenty beats,
    # which is the texture the blocklist exists to reject.
    prompts = {build_system_prompt(slug) for slug in CATEGORY_SLUGS}

    assert len(prompts) == EXPECTED_CATEGORY_COUNT


def test_an_unknown_category_falls_back_to_the_default() -> None:
    assert get_category("no-such-rubric").slug == DEFAULT_CATEGORY
    assert get_category("").slug == DEFAULT_CATEGORY
    # Case and stray whitespace are operator typos, not new categories.
    assert get_category("  NOVOSTI-POLITIKI  ").slug == "novosti-politiki"


def test_the_user_prompt_carries_the_cluster_facts_and_the_field_spec() -> None:
    prompt = build_user_prompt(CLUSTER_DATA, "novosti-obshhestva")

    assert "Открыта станция метро «Пыхтино»" in prompt
    for fact in CLUSTER_DATA["facts"]:
        assert fact in prompt
    for source in CLUSTER_DATA["sources"]:
        assert source["url"] in prompt
    for name in REQUIRED_FIELDS:
        assert name in prompt
    assert str(MAX_TITLE_CHARS) in prompt
    assert str(MAX_SEO_DESCRIPTION_CHARS) in prompt


def test_the_user_prompt_survives_a_sparse_cluster() -> None:
    # A cluster the enrichment stage has not touched carries almost nothing; a
    # missing key must degrade the prompt, never raise.
    prompt = build_user_prompt({}, "novosti-nauki")

    assert prompt
    assert "(заголовок не задан)" in prompt
    assert "(структурированные факты не выделены)" in prompt
    assert "(нет исходных сообщений)" in prompt


def test_the_user_prompt_ignores_malformed_facts_and_sources() -> None:
    prompt = build_user_prompt(
        {"title": "Т", "facts": "не список", "sources": {"не": "список"}}, "avtonovosti"
    )

    assert "(структурированные факты не выделены)" in prompt
    assert "(нет исходных сообщений)" in prompt


def test_build_messages_returns_the_system_user_pair() -> None:
    messages = build_messages(CLUSTER_DATA, "novosti-politiki")

    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[0]["content"] == build_system_prompt("novosti-politiki")
    assert messages[1]["content"] == build_user_prompt(CLUSTER_DATA, "novosti-politiki")


# ==================================================== prompts: response schema


def test_the_response_schema_matches_the_required_fields() -> None:
    assert RESPONSE_JSON_SCHEMA["required"] == list(REQUIRED_FIELDS)
    assert set(RESPONSE_JSON_SCHEMA["properties"]) == set(REQUIRED_FIELDS)
    # Without this a strict provider rejects a reply carrying extra keys, and a
    # lenient one lets a chatty `explanation` field through.
    assert RESPONSE_JSON_SCHEMA["additionalProperties"] is False


def test_the_schema_pins_the_limits_the_database_enforces() -> None:
    properties = RESPONSE_JSON_SCHEMA["properties"]

    assert properties["title"]["maxLength"] == MAX_TITLE_CHARS
    assert properties["seo_title"]["maxLength"] == MAX_TITLE_CHARS
    assert properties["seo_description"]["maxLength"] == MAX_SEO_DESCRIPTION_CHARS
    assert properties["seo_keywords"]["minItems"] == MIN_SEO_KEYWORDS
    assert properties["seo_keywords"]["maxItems"] == MAX_SEO_KEYWORDS
    # The category must come back as one of ours, not as free text.
    assert properties["category_slug"]["enum"] == list(CATEGORY_SLUGS)


def test_response_format_requests_strict_structured_output() -> None:
    block = response_format()

    assert block["type"] == "json_schema"
    assert block["json_schema"]["strict"] is True
    assert block["json_schema"]["schema"] == RESPONSE_JSON_SCHEMA


# ============================================== validation of mock LLM replies


def test_a_valid_reply_is_accepted() -> None:
    article = validate_generation(
        valid_payload(), expected_category="novosti-obshhestva"
    )

    assert isinstance(article, GeneratedArticle)
    assert article.title.startswith("В Москве открыли")
    assert article.category_slug == "novosti-obshhestva"
    assert len(article.seo_keywords) == 3
    assert article.attempts == 1


def test_parse_generation_tolerates_a_markdown_fence() -> None:
    payload = valid_payload()
    fenced = f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"

    assert parse_generation(fenced) == payload


def test_parse_generation_recovers_an_object_wrapped_in_prose() -> None:
    payload = valid_payload()
    body = json.dumps(payload, ensure_ascii=False)
    chatty = f"Конечно! Вот результат:\n{body}\nГотово."

    assert parse_generation(chatty) == payload


@pytest.mark.parametrize("raw", ["not json at all", "", "[1, 2, 3]", "{oops}"])
def test_parse_generation_rejects_unusable_replies(raw: str) -> None:
    with pytest.raises(SchemaViolationError):
        parse_generation(raw)


@pytest.mark.parametrize("missing", REQUIRED_FIELDS)
def test_a_missing_required_field_is_rejected(missing: str) -> None:
    payload = valid_payload()
    del payload[missing]

    with pytest.raises(SchemaViolationError) as info:
        validate_generation(payload, expected_category="novosti-obshhestva")

    assert missing in str(info.value)


def test_an_overlong_title_is_rejected() -> None:
    payload = valid_payload(title="Ы" * (MAX_TITLE_CHARS + 1))

    with pytest.raises(SchemaViolationError, match="title"):
        validate_generation(payload, expected_category="novosti-obshhestva")


def test_an_overlong_seo_description_is_rejected() -> None:
    payload = valid_payload(
        seo_description="Ы" * (MAX_SEO_DESCRIPTION_CHARS + 1)
    )

    with pytest.raises(SchemaViolationError, match="seo_description"):
        validate_generation(payload, expected_category="novosti-obshhestva")


@pytest.mark.parametrize(
    "keywords",
    [
        ["одно", "два"],
        ["a", "b", "c", "d", "e", "f", "g"],
        [],
    ],
)
def test_a_keyword_count_outside_the_range_is_rejected(keywords: list[str]) -> None:
    payload = valid_payload(seo_keywords=keywords)

    with pytest.raises(SchemaViolationError, match="seo_keywords"):
        validate_generation(payload, expected_category="novosti-obshhestva")


def test_comma_separated_keywords_are_accepted_and_normalised() -> None:
    # The schema asks for an array while the prompt says "comma-separated"; a
    # draft is too expensive to discard over that ambiguity.
    payload = valid_payload(seo_keywords="#Метро, ПЫХТИНО ,  метро  , москва")

    article = validate_generation(
        payload, expected_category="novosti-obshhestva"
    )

    assert article.seo_keywords == ("метро", "пыхтино", "москва")


def test_a_short_body_is_rejected() -> None:
    payload = valid_payload(content_html="<h2>Всё</h2><p>Коротко.</p>")

    with pytest.raises(SchemaViolationError, match="content_html"):
        validate_generation(payload, expected_category="novosti-obshhestva")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (f"<p>Абзац без подзаголовка.</p>{_filler()}", "<h2>"),
        (f"<h2>Заголовок</h2>{'Текст без абзацев. ' * 80}", "<p>"),
    ],
)
def test_a_body_missing_required_structure_is_rejected(body: str, expected: str) -> None:
    payload = valid_payload(content_html=body)

    with pytest.raises(SchemaViolationError) as info:
        validate_generation(payload, expected_category="novosti-obshhestva")

    assert expected in str(info.value)


@pytest.mark.parametrize(
    "leaked",
    [
        "','image_prompt':'a photo of a city street'",
        '","seo_description":"Короткое описание"',
        "', 'title': 'Совсем другой заголовок'",
        '", "seo_keywords": ["одно", "другое"]',
        "','category':'novosti-obshhestva'",
    ],
)
def test_leaked_response_scaffolding_is_rejected(leaked: str) -> None:
    """A model that closed `content_html` with the wrong quote must not pass.

    Observed in production on `auto/fast`: the remaining response keys ended up
    inside the body, so four of eleven articles carried an English image prompt at
    the end of their Russian text. The reply is still *valid JSON* and every other
    field is present, which is exactly why nothing upstream noticed — the only
    place to catch it is the body itself.

    Rejected rather than trimmed: the boundary between article and scaffolding
    cannot be recovered reliably, and a retry costs one call while a silently
    truncated article is published wrong.
    """
    payload = valid_payload(content_html=f"{CLEAN_BODY}{leaked}")

    with pytest.raises(SchemaViolationError, match="leaked response field"):
        validate_generation(payload, expected_category="novosti-obshhestva")


def test_ordinary_punctuation_is_not_mistaken_for_a_leak() -> None:
    """The guard must not fire on quoted speech, which Russian news is full of."""
    quoted = (
        "<p>«Ситуация под контролем», — заявил представитель ведомства. "
        "Он добавил: «работы продолжаются», уточнив сроки завершения работ.</p>"
    )
    # Padded past MIN_CONTENT_HTML_CHARS so the assertion is about the leak guard
    # and not about length.
    body = "<h2>Заголовок</h2>" + quoted * 8
    article = validate_generation(
        valid_payload(content_html=body),
        expected_category="novosti-obshhestva",
    )

    assert "«Ситуация под контролем»" in article.content_html


@pytest.mark.parametrize(
    "injected",
    [
        "<script>alert(1)</script>",
        '<iframe src="https://tracker.example"></iframe>',
        '<p onclick="alert(1)">Текст</p>',
        '<p style="color:red">Текст</p>',
        "<style>body{display:none}</style>",
    ],
)
def test_unsafe_markup_is_rejected_not_sanitised(injected: str) -> None:
    # Rejection is deliberate: a `<script>` in a generated article means the
    # model echoed markup out of a scraped source, and silently stripping it
    # would hide an attempted injection an operator should see.
    payload = valid_payload(content_html=f"{CLEAN_BODY}{injected}")

    with pytest.raises(SchemaViolationError, match="content_html"):
        validate_generation(payload, expected_category="novosti-obshhestva")


def test_an_unknown_category_slug_is_corrected_not_rejected() -> None:
    # The article may be perfectly good, and the caller already knows which
    # category it asked for.
    payload = valid_payload(category_slug="invented-rubric")

    article = validate_generation(payload, expected_category="novosti-nauki")

    assert article.category_slug == "novosti-nauki"


def test_to_article_fields_matches_the_articles_columns() -> None:
    article = validate_generation(
        valid_payload(), expected_category="novosti-obshhestva"
    )

    fields = article.to_article_fields()

    assert set(fields) == {
        "title",
        "content_html",
        "seo_title",
        "seo_description",
        "seo_keywords",
        "category",
    }
    # `seo_keywords` is ARRAY(String(128)): SQLAlchemy binds a list, not a tuple.
    assert isinstance(fields["seo_keywords"], list)


# ==================================================== generation: end to end


async def test_generate_article_returns_a_validated_draft() -> None:
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=json_completion(valid_payload())
        )
        client = new_client()
        try:
            result = await generate_article(
                CLUSTER_DATA, "novosti-obshhestva", client=client
            )
        finally:
            await client.close()

    assert result["title"].startswith("В Москве открыли")
    assert result["category_slug"] == "novosti-obshhestva"
    assert result["attempts"] == 1
    assert result["slop_cleaned"] is False
    assert result["slop_notes"] == []
    assert route.call_count == 1

    # The request carried the schema and a longread-sized budget.
    sent = json.loads(route.calls[0].request.content)
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["max_tokens"] == GENERATION_MAX_TOKENS
    assert [message["role"] for message in sent["messages"]] == ["system", "user"]


async def test_a_slop_ridden_draft_triggers_exactly_one_retry() -> None:
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/chat/completions").mock(
            side_effect=[
                json_completion(valid_payload(content_html=DIRTY_BODY)),
                json_completion(valid_payload()),
            ]
        )
        client = new_client()
        try:
            result = await generate_article(
                CLUSTER_DATA, "novosti-obshhestva", client=client
            )
        finally:
            await client.close()

    assert result["attempts"] == 2
    assert route.call_count == 2

    # The retry replayed the rejected draft and named the offending phrases, so
    # the second attempt is a correction rather than a blind repeat.
    retry = json.loads(route.calls[1].request.content)
    roles = [message["role"] for message in retry["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    correction = retry["messages"][-1]["content"]
    assert "отклонён" in correction
    assert "в современном мире" in correction


async def test_generation_gives_up_after_the_retry_budget() -> None:
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=json_completion(valid_payload(content_html=DIRTY_BODY))
        )
        client = new_client()
        try:
            with pytest.raises(GenerationRejectedError) as info:
                await generate_article(
                    CLUSTER_DATA, "novosti-obshhestva", client=client
                )
        finally:
            await client.close()

    # Bounded on purpose: a model that produced slop twice under an explicit
    # prohibition will not comply on a third paid attempt.
    assert route.call_count == MAX_GENERATION_ATTEMPTS
    assert info.value.attempts == MAX_GENERATION_ATTEMPTS
    assert info.value.reasons


async def test_soft_slop_is_repaired_without_a_second_call() -> None:
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=json_completion(valid_payload(content_html=SOFT_BODY))
        )
        client = new_client()
        try:
            result = await generate_article(
                CLUSTER_DATA, "novosti-obshhestva", client=client
            )
        finally:
            await client.close()

    # Paying for a whole regeneration to delete "стоит отметить, что" would be
    # indefensible, so the hedge is stripped in place.
    assert route.call_count == 1
    assert result["slop_cleaned"] is True
    assert "стоит отметить" not in result["content_html"].lower()
    assert "23 миллиарда рублей" in result["content_html"]


async def test_a_malformed_reply_is_retried_with_the_violations_quoted() -> None:
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/chat/completions").mock(
            side_effect=[
                completion_response("Извините, не могу помочь."),
                json_completion(valid_payload()),
            ]
        )
        client = new_client()
        try:
            result = await generate_article(
                CLUSTER_DATA, "novosti-obshhestva", client=client
            )
        finally:
            await client.close()

    assert result["attempts"] == 2
    retry = json.loads(route.calls[1].request.content)
    assert "JSON" in retry["messages"][-1]["content"]


async def test_the_requested_model_is_forwarded() -> None:
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=json_completion(valid_payload())
        )
        client = new_client()
        try:
            await generate_article(
                CLUSTER_DATA,
                "novosti-obshhestva",
                model_name="anthropic/claude-opus-4",
                client=client,
            )
        finally:
            await client.close()

    assert json.loads(route.calls[0].request.content)["model"] == (
        "anthropic/claude-opus-4"
    )


async def test_the_default_writer_model_is_used_when_none_is_given() -> None:
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=json_completion(valid_payload())
        )
        client = new_client()
        try:
            await generate_article(CLUSTER_DATA, "novosti-nauki", client=client)
        finally:
            await client.close()

    assert json.loads(route.calls[0].request.content)["model"] == (
        settings.ai_model_writer
    )


async def test_an_unconfigured_client_raises_rather_than_generating() -> None:
    # A deployment without credentials must fail loudly here: unlike dedup,
    # content generation has no zero-token fallback.
    client = AIClient(api_key="", base_url=API_URL)
    try:
        with pytest.raises(NoCredentialsError):
            await generate_article(CLUSTER_DATA, "novosti-nauki", client=client)
    finally:
        await client.close()


async def test_the_api_key_never_appears_in_an_error() -> None:
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        mock.post("/chat/completions").mock(
            return_value=httpx.Response(400, text="bad request")
        )
        client = new_client()
        try:
            with pytest.raises(Exception) as info:
                await generate_article(CLUSTER_DATA, "novosti-nauki", client=client)
        finally:
            await client.close()

    assert API_KEY not in str(info.value)


async def test_the_prompt_sent_matches_the_requested_category() -> None:
    with respx.mock(base_url=API_URL, assert_all_called=True) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=json_completion(valid_payload(category_slug="chp-i-proisshestvij"))
        )
        client = new_client()
        try:
            await generate_article(CLUSTER_DATA, "chp-i-proisshestvij", client=client)
        finally:
            await client.close()

    system = json.loads(route.calls[0].request.content)["messages"][0]["content"]
    assert CATEGORIES["chp-i-proisshestvij"].title_ru in system
