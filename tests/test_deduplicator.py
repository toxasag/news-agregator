"""Tests for the 0-token heuristic filter and the three-level dedup engine.

Everything here runs against `fakeredis` and an in-memory session double, so the
suite needs no Redis, no PostgreSQL and no network. Thresholds are read from
`settings` rather than hardcoded, so the tests track configuration instead of
freezing yesterday's numbers.
"""

# Test inputs are Russian news copy, so Cyrillic letters that look like Latin
# ones are unavoidable and expected; RUF001 would flag every sentence.
# ruff: noqa: RUF001

from __future__ import annotations

import json
import statistics
import time
import uuid
from typing import Any, Final

import httpx
import pytest
import respx
from datasketch import MinHash
from fakeredis.aioredis import FakeRedis

from app.config import settings
from app.models.enums import DedupStatus
from app.services import text_utils
from app.services.ai_client import (
    CHAT_COMPLETIONS_PATH,
    AIClient,
    parse_yes_no,
)
from app.services.deduplicator import (
    FACTS_DELTA_KEY,
    FACTS_KEY,
    LSH_BUCKET_PREFIX,
    LSH_SIGNATURE_PREFIX,
    REASON_EMPTY_TEXT,
    REASON_FILTERED_AD,
    REASON_FILTERED_DIGEST,
    REASON_L1_DUPLICATE,
    REASON_L1_UNIQUE,
    REASON_L2_DEGRADED,
    REASON_L2_NO_CANDIDATES,
    REASON_L3_NEW_FACTS,
    REASON_L3_NO_NEW_FACTS,
    Deduplicator,
    bucket_keys,
    compute_minhash,
    compute_shingles,
    extract_facts,
    index_ttl_seconds,
    jaccard,
    signature_from_list,
    signature_key,
    signature_to_list,
)
from app.services.text_utils import FilterResult, analyze_post
from tests.conftest import (
    FailingAIClient,
    FakeSession,
    StubAIClient,
    build_cluster,
    build_raw_news,
    near_duplicate_of,
    news_corpus,
    news_text,
    news_text_minor_edit,
    news_text_rewritten,
    news_text_unrelated,
    news_text_with_extra_fact,
    redis_get,
    redis_keys,
    redis_members,
    redis_set_size,
    redis_size,
    redis_ttl,
    semantic_row,
)

#: Digest rule identifiers, i.e. the ones that set `FilterResult.is_digest`.
DIGEST_REASONS: Final[frozenset[str]] = frozenset(
    {
        text_utils.REASON_DIGEST_NUMBERED_LIST,
        text_utils.REASON_DIGEST_BULLET_LIST,
        text_utils.REASON_DIGEST_HEADLINE,
    }
)

#: Advertising rule identifiers, i.e. the ones that set `FilterResult.is_ad`.
AD_REASONS: Final[frozenset[str]] = frozenset(
    {
        text_utils.REASON_AD_ERID,
        text_utils.REASON_AD_FOOTER,
        text_utils.REASON_AD_PROMO_CODE,
        text_utils.REASON_AD_REFERRAL_LINK,
        text_utils.REASON_AD_SHORTENER,
        text_utils.REASON_AD_LINK_DENSITY,
    }
)

#: Every stable rule identifier the filter may report. `filter_reasons` is an
#: `ARRAY(String(64))` column, so the set doubles as the length contract.
ALL_FILTER_REASONS: Final[frozenset[str]] = DIGEST_REASONS | AD_REASONS

#: Width of the `filter_reasons` column elements.
FILTER_REASON_MAX_LENGTH: Final[int] = 64

#: Corpus sizes. The collision corpus only needs to exceed a hundred documents;
#: the latency corpus is larger so the index is genuinely crowded.
COLLISION_CORPUS_SIZE: Final[int] = 120
LATENCY_CORPUS_SIZE: Final[int] = 220

#: Level 1 budget from docs/04_FILTER_AND_DEDUP.md.
LEVEL1_BUDGET_MS: Final[float] = 5.0
LATENCY_SAMPLES: Final[int] = 90

#: Largest value PostgreSQL `BIGINT` can hold; `raw_news.minhash_signature` is
#: `ARRAY(BIGINT)`, so every persisted permutation value must fit below it.
SIGNED_BIGINT_MAX: Final[int] = 2**63 - 1


def _require(signature: MinHash | None) -> MinHash:
    """Assert a fixture text is hashable and return its signature."""
    assert signature is not None, "fixture text must produce a MinHash signature"
    return signature


# ============================================================ MinHash / level 1


def test_fixture_texts_are_news_length() -> None:
    # The 0.85 duplicate threshold assumes news-length input: a short snippet has
    # too few word 3-grams for the estimate to be stable, so the fixtures would
    # "prove" a threshold that does not hold in production.
    for text in (
        news_text(),
        news_text_minor_edit(),
        news_text_rewritten(),
        news_text_unrelated(),
    ):
        words = len(text.split())
        assert words >= 120, f"fixture carries only {words} words"
        assert len(compute_shingles(text)) >= 100


def test_identical_texts_estimate_jaccard_one() -> None:
    left = _require(compute_minhash(news_text()))
    right = _require(compute_minhash(news_text()))
    assert jaccard(left, right) == 1.0
    assert signature_to_list(left) == signature_to_list(right)


async def test_identical_text_is_a_level1_duplicate_without_tokens(
    deduplicator: Deduplicator, session: FakeSession
) -> None:
    first = build_raw_news(1, news_text())
    original = await deduplicator.deduplicate(session.as_session(), first)
    assert original.status is DedupStatus.UNIQUE
    assert original.is_new_cluster is True

    repost = build_raw_news(2, news_text())
    decision = await deduplicator.deduplicate(session.as_session(), repost)

    assert decision.similarity == 1.0
    assert decision.cluster_id == original.cluster_id
    # A confirmed level 1 duplicate is resolved through level 3; with identical
    # text there are no new facts, so the post is closed as SKIPPED_DUPLICATE.
    assert decision.status is DedupStatus.SKIPPED_DUPLICATE
    assert decision.reason == REASON_L3_NO_NEW_FACTS
    assert decision.tokens_spent is False
    assert repost.cluster_id is None
    assert repost.is_processed is True


async def test_level1_duplicate_hit_is_reported_before_novelty(
    deduplicator: Deduplicator, session: FakeSession
) -> None:
    cluster = build_cluster(facts=["ent:тестовый кластер"])
    session.add(cluster)
    signature = _require(compute_minhash(news_text()))
    await deduplicator.index(11, cluster.id, signature)

    candidates = await deduplicator.find_candidates(signature, exclude_id=12)

    assert [candidate.raw_news_id for candidate in candidates] == [11]
    assert candidates[0].cluster_id == cluster.id
    assert candidates[0].jaccard >= settings.lsh_duplicate_threshold

    carrier = build_raw_news(12, news_text_with_extra_fact())
    decision = await deduplicator.deduplicate(session.as_session(), carrier)

    assert decision.reason == REASON_L3_NEW_FACTS
    assert decision.status is DedupStatus.UPDATE
    assert decision.level == 3
    assert decision.cluster_id == cluster.id
    assert decision.tokens_spent is False


async def test_minor_edit_stays_a_level1_duplicate(
    deduplicator: Deduplicator, session: FakeSession
) -> None:
    similarity = jaccard(
        _require(compute_minhash(news_text())),
        _require(compute_minhash(news_text_minor_edit())),
    )
    assert similarity >= settings.lsh_duplicate_threshold, (
        f"one reworded sentence dropped the estimate to {similarity:.3f}, "
        f"below the level 1 duplicate threshold "
        f"{settings.lsh_duplicate_threshold}"
    )

    await deduplicator.deduplicate(session.as_session(), build_raw_news(1, news_text()))
    decision = await deduplicator.deduplicate(
        session.as_session(), build_raw_news(2, news_text_minor_edit())
    )

    assert decision.status is DedupStatus.SKIPPED_DUPLICATE
    assert decision.level == 3
    assert decision.similarity is not None
    assert decision.similarity >= settings.lsh_duplicate_threshold
    assert decision.tokens_spent is False


async def test_unrelated_texts_are_not_duplicates(
    deduplicator: Deduplicator, session: FakeSession
) -> None:
    similarity = jaccard(
        _require(compute_minhash(news_text())),
        _require(compute_minhash(news_text_unrelated())),
    )
    assert similarity < settings.lsh_ambiguous_threshold

    first = await deduplicator.deduplicate(
        session.as_session(), build_raw_news(1, news_text())
    )
    second = await deduplicator.deduplicate(
        session.as_session(), build_raw_news(2, news_text_unrelated())
    )

    assert second.status is DedupStatus.UNIQUE
    assert second.reason == REASON_L1_UNIQUE
    assert second.is_new_cluster is True
    assert second.cluster_id != first.cluster_id
    assert second.tokens_spent is False


async def test_rewritten_event_falls_into_the_ambiguous_band(
    fake_redis: FakeRedis,
) -> None:
    similarity = jaccard(
        _require(compute_minhash(news_text())),
        _require(compute_minhash(news_text_rewritten())),
    )
    assert settings.lsh_ambiguous_threshold <= similarity, (
        f"rewrite estimate {similarity:.3f} fell below the ambiguous floor "
        f"{settings.lsh_ambiguous_threshold}; it would skip level 2 entirely"
    )
    assert similarity < settings.lsh_duplicate_threshold, (
        f"rewrite estimate {similarity:.3f} reached the level 1 duplicate "
        f"threshold {settings.lsh_duplicate_threshold}"
    )


async def test_ambiguous_band_escalates_to_level2_instead_of_deciding(
    fake_redis: FakeRedis,
) -> None:
    ai = StubAIClient()
    engine = Deduplicator(redis=fake_redis, ai_client=ai)
    session = FakeSession()
    try:
        await engine.deduplicate(session.as_session(), build_raw_news(1, news_text()))
        assert ai.embed_calls == []

        decision = await engine.deduplicate(
            session.as_session(), build_raw_news(2, news_text_rewritten())
        )
    finally:
        await engine.close()

    # Escalation is the observable behaviour: one embedding was requested, the
    # verdict carries level 2 and the run is no longer free.
    assert len(ai.embed_calls) == 1
    assert decision.level == 2
    assert decision.reason == REASON_L2_NO_CANDIDATES
    assert decision.tokens_spent is True
    assert decision.reason != REASON_L1_DUPLICATE
    assert len(session.executed) == 1


async def test_ambiguous_band_attaches_when_cosine_is_above_upper(
    fake_redis: FakeRedis,
) -> None:
    existing = build_cluster(facts=sorted(extract_facts(news_text())))
    above_upper = min(settings.similarity_upper_threshold + 0.02, 1.0)
    session = FakeSession([semantic_row(existing, above_upper)])
    ai = StubAIClient()
    engine = Deduplicator(redis=fake_redis, ai_client=ai)
    try:
        await engine.deduplicate(session.as_session(), build_raw_news(1, news_text()))
        decision = await engine.deduplicate(
            session.as_session(), build_raw_news(2, news_text_rewritten())
        )
    finally:
        await engine.close()

    assert decision.cluster_id == existing.id
    assert decision.status is DedupStatus.SKIPPED_DUPLICATE
    assert decision.similarity == pytest.approx(above_upper)
    assert decision.tokens_spent is True


async def test_arbiter_no_opens_a_new_cluster(fake_redis: FakeRedis) -> None:
    existing = build_cluster()
    disputed = (
        settings.similarity_lower_threshold + settings.similarity_upper_threshold
    ) / 2
    session = FakeSession([semantic_row(existing, disputed)])
    ai = StubAIClient(answers=[False])
    engine = Deduplicator(redis=fake_redis, ai_client=ai)
    try:
        await engine.deduplicate(session.as_session(), build_raw_news(1, news_text()))
        decision = await engine.deduplicate(
            session.as_session(), build_raw_news(2, news_text_rewritten())
        )
    finally:
        await engine.close()

    assert len(ai.arbiter_prompts) == 1
    assert decision.status is DedupStatus.UNIQUE
    assert decision.is_new_cluster is True
    assert decision.cluster_id != existing.id


# ------------------------------------------------------------------- collisions


async def test_banding_finds_the_near_duplicate_and_prunes_the_corpus(
    deduplicator: Deduplicator, session: FakeSession
) -> None:
    corpus = news_corpus(COLLISION_CORPUS_SIZE)
    target_index = 42
    for offset, document in enumerate(corpus):
        signature = _require(compute_minhash(document))
        await deduplicator.index(offset + 1, uuid.uuid4(), signature)

    query = _require(compute_minhash(near_duplicate_of(corpus[target_index])))
    candidates = await deduplicator.find_candidates(query)
    found = {candidate.raw_news_id for candidate in candidates}

    # (a) the true near duplicate survives banding
    assert target_index + 1 in found
    best = next(
        candidate
        for candidate in candidates
        if candidate.raw_news_id == target_index + 1
    )
    assert best.jaccard >= settings.lsh_duplicate_threshold
    assert candidates[0].raw_news_id == target_index + 1

    # (b) banding prunes: the candidate set is a small fraction of the corpus
    assert len(candidates) <= COLLISION_CORPUS_SIZE // 4, (
        f"banding returned {len(candidates)} of {COLLISION_CORPUS_SIZE} indexed "
        "documents, which is no better than a full scan"
    )
    others = [
        candidate.jaccard
        for candidate in candidates
        if candidate.raw_news_id != target_index + 1
    ]
    assert all(value < settings.lsh_duplicate_threshold for value in others)


async def test_unrelated_query_against_a_corpus_yields_no_false_duplicate(
    deduplicator: Deduplicator, session: FakeSession
) -> None:
    for offset, document in enumerate(news_corpus(COLLISION_CORPUS_SIZE)):
        await deduplicator.index(
            offset + 1, uuid.uuid4(), _require(compute_minhash(document))
        )

    unrelated = build_raw_news(COLLISION_CORPUS_SIZE + 1, news_text_unrelated())
    decision = await deduplicator.deduplicate(session.as_session(), unrelated)

    assert decision.status is DedupStatus.UNIQUE
    assert decision.reason == REASON_L1_UNIQUE
    assert decision.is_new_cluster is True
    assert decision.tokens_spent is False

    candidates = await deduplicator.find_candidates(
        _require(compute_minhash(news_text_unrelated())),
        exclude_id=COLLISION_CORPUS_SIZE + 1,
    )
    assert all(
        candidate.jaccard < settings.lsh_ambiguous_threshold
        for candidate in candidates
    ), "an unrelated story reached the ambiguous band against filler documents"


@pytest.mark.slow
async def test_level1_query_median_latency_is_within_budget(
    deduplicator: Deduplicator,
) -> None:
    # Budget check against fakeredis, not a production benchmark: it holds the
    # in-process work (band hashing, candidate parsing, Jaccard estimation)
    # inside the documented < 5 ms level 1 target and fails loudly if that path
    # regresses. A real Redis adds network latency this cannot measure.
    corpus = news_corpus(LATENCY_CORPUS_SIZE)
    signatures = [_require(compute_minhash(document)) for document in corpus]
    for offset, signature in enumerate(signatures):
        await deduplicator.index(offset + 1, uuid.uuid4(), signature)

    hot = _require(compute_minhash(near_duplicate_of(corpus[42])))
    samples: list[float] = []
    for index in range(LATENCY_SAMPLES):
        query = hot if index % 3 == 0 else signatures[index % len(signatures)]
        started = time.perf_counter()
        await deduplicator.find_candidates(query)
        samples.append((time.perf_counter() - started) * 1000.0)

    median = statistics.median(samples)
    assert median < LEVEL1_BUDGET_MS, (
        f"level 1 median query latency {median:.3f} ms over {LATENCY_SAMPLES} "
        f"samples against {LATENCY_CORPUS_SIZE} indexed documents exceeds the "
        f"{LEVEL1_BUDGET_MS} ms budget "
        f"(min {min(samples):.3f} ms, max {max(samples):.3f} ms)"
    )


# ------------------------------------------------------------ signature storage


def test_signature_round_trip_preserves_estimate_and_band_keys() -> None:
    signature = _require(compute_minhash(news_text()))
    stored = signature_to_list(signature)

    # `raw_news.minhash_signature` is ARRAY(BIGINT), a *signed* type: a value
    # above the signed maximum could not be stored and would come back wrapped.
    assert all(0 <= value <= SIGNED_BIGINT_MAX for value in stored)
    assert len(stored) == settings.minhash_num_perm

    # Simulate the database round trip through JSON, as Redis does verbatim.
    restored = signature_from_list(json.loads(json.dumps(stored)))

    assert signature_to_list(restored) == stored
    assert jaccard(signature, restored) == 1.0
    assert bucket_keys(restored) == bucket_keys(signature)

    other = _require(compute_minhash(news_text_minor_edit()))
    assert jaccard(restored, other) == jaccard(signature, other)


def test_band_hashing_tolerates_values_beyond_the_signed_boundary() -> None:
    # The engine masks each value into 8 unsigned bytes before hashing, so a
    # value that crosses the signed BIGINT boundary (as an unsigned driver read
    # would report it) still yields a stable band key instead of raising.
    stored = signature_to_list(_require(compute_minhash(news_text())))
    beyond = [SIGNED_BIGINT_MAX + 1, *stored[1:]]

    keys = bucket_keys(signature_from_list(beyond))

    assert len(keys) == settings.lsh_bands
    assert keys != bucket_keys(signature_from_list(stored))
    assert keys[1:] == bucket_keys(signature_from_list(stored))[1:]


def test_bucket_keys_rejects_a_signature_that_does_not_split_into_bands() -> None:
    stub = signature_from_list([1] * (settings.lsh_bands + 1))
    with pytest.raises(ValueError, match="equal LSH bands"):
        bucket_keys(stub)


# --------------------------------------------------------------- Redis layout


async def test_index_writes_the_documented_redis_layout(
    deduplicator: Deduplicator, fake_redis: FakeRedis
) -> None:
    cluster_id = uuid.uuid4()
    signature = _require(compute_minhash(news_text()))

    await deduplicator.index(77, cluster_id, signature)

    expected_buckets = bucket_keys(signature)
    assert len(expected_buckets) == settings.lsh_bands
    assert {key.rsplit(":", 2)[0] for key in expected_buckets} == {LSH_BUCKET_PREFIX}
    assert {int(key.split(":")[2]) for key in expected_buckets} == set(
        range(settings.lsh_bands)
    )

    assert await redis_keys(fake_redis, "lsh:*") == {
        *expected_buckets,
        signature_key(77),
    }
    assert signature_key(77) == f"{LSH_SIGNATURE_PREFIX}:77"

    ttl = index_ttl_seconds()
    assert ttl == settings.dedup_lookback_hours * 3600
    for key in (*expected_buckets, signature_key(77)):
        assert await redis_ttl(fake_redis, key) == ttl

    for key in expected_buckets:
        assert await redis_members(fake_redis, key) == {f"77:{cluster_id}"}

    cached = await redis_get(fake_redis, signature_key(77))
    assert cached is not None
    assert json.loads(cached) == signature_to_list(signature)


async def test_index_of_a_clusterless_post_uses_an_empty_cluster_field(
    deduplicator: Deduplicator, fake_redis: FakeRedis
) -> None:
    signature = _require(compute_minhash(news_text()))
    await deduplicator.index(5, None, signature)

    assert await redis_members(fake_redis, bucket_keys(signature)[0]) == {"5:"}

    candidates = await deduplicator.find_candidates(signature)
    assert [(item.raw_news_id, item.cluster_id) for item in candidates] == [(5, None)]


async def test_reindexing_the_same_post_does_not_duplicate_bucket_members(
    deduplicator: Deduplicator, fake_redis: FakeRedis
) -> None:
    cluster_id = uuid.uuid4()
    signature = _require(compute_minhash(news_text()))

    await deduplicator.index(9, cluster_id, signature)
    first_keys = await redis_keys(fake_redis, "lsh:*")
    await deduplicator.index(9, cluster_id, signature)

    assert await redis_keys(fake_redis, "lsh:*") == first_keys
    for key in bucket_keys(signature):
        assert await redis_set_size(fake_redis, key) == 1

    candidates = await deduplicator.find_candidates(signature)
    assert len(candidates) == 1


async def test_replayed_row_is_not_reindexed(
    deduplicator: Deduplicator, session: FakeSession, fake_redis: FakeRedis
) -> None:
    decided = build_raw_news(
        3,
        news_text(),
        dedup_status=DedupStatus.UNIQUE,
        entities={"dedup_matched_cluster_id": str(uuid.uuid4())},
    )
    decision = await deduplicator.deduplicate(session.as_session(), decided)

    assert decision.status is DedupStatus.UNIQUE
    assert decision.is_new_cluster is False
    assert decision.tokens_spent is False
    assert session.added == []
    assert await redis_size(fake_redis) == 0


# ============================================================ heuristic filter

_NUMBERED_DIGEST: Final[str] = (
    "Главные события утра.\n"
    "1. Совет директоров банка утвердил новую стратегию развития до 2030 года.\n"
    "2. Аэропорт Пулково возобновил приём рейсов после ночного снегопада.\n"
    "3. Правительство продлило программу субсидирования ипотеки для семей.\n"
    "4. Курс рубля укрепился к доллару после решения Центрального банка."
)

_BULLET_DIGEST: Final[str] = (
    "Коротко:\n"
    "• Минфин отчитался о профиците федерального бюджета за квартал.\n"
    "• Экспорт зерна вырос почти на четверть к прошлому сезону.\n"
    "• Строители сдали новый мост через Обь на два месяца раньше срока.\n"
    "• Учёные описали новый вид глубоководных рыб у Курильских островов."
)

_PROSE_WITH_STRAY_NUMBER: Final[str] = (
    "Суд огласил приговор по делу о хищении средств дорожного фонда региона.\n"
    "1. Как установило следствие, подрядчик завысил стоимость работ на"
    " 8 млн рублей.\n"
    "Обвиняемый признал вину частично и намерен обжаловать решение в"
    " апелляции."
)


def _assert_reasons_are_stable_identifiers(result: FilterResult) -> None:
    """Every reported reason must be a known, storable identifier.

    Also checks that the reason families agree with the boolean flags: a reason
    is the only audit trail for a rejection, so it must not contradict the
    column the dashboard filters on.
    """
    assert result.reasons == tuple(sorted(set(result.reasons)))
    for reason in result.reasons:
        assert reason in ALL_FILTER_REASONS, f"unknown reason {reason!r}"
        assert len(reason) <= FILTER_REASON_MAX_LENGTH, (
            f"reason {reason!r} is {len(reason)} chars and would be truncated by "
            f"filter_reasons ARRAY(String({FILTER_REASON_MAX_LENGTH}))"
        )
    reported = set(result.reasons)
    assert result.is_digest is bool(reported & DIGEST_REASONS)
    assert result.is_ad is bool(reported & AD_REASONS)


@pytest.mark.parametrize(
    ("text", "expected_reason"),
    [
        pytest.param(
            _NUMBERED_DIGEST,
            text_utils.REASON_DIGEST_NUMBERED_LIST,
            id="numbered_list",
        ),
        pytest.param(
            _BULLET_DIGEST, text_utils.REASON_DIGEST_BULLET_LIST, id="bullet_run"
        ),
    ],
)
def test_digest_structure_is_detected(text: str, expected_reason: str) -> None:
    result = analyze_post(text)

    assert result.is_digest is True
    assert result.is_ad is False
    assert expected_reason in result.reasons
    assert result.should_skip is True
    _assert_reasons_are_stable_identifiers(result)


@pytest.mark.parametrize(
    "headline",
    [
        "Главное к этому часу",
        "Главные новости",
        "Главное за неделю",
        "Новости к этому часу",
        "Итоги дня",
        "Итоги недели",
        "Дайджест",
        "Коротко о главном",
        "Что произошло за сутки",
    ],
)
def test_digest_headline_phrases_are_detected(headline: str) -> None:
    result = analyze_post(
        f"{headline}. Мэрия отчиталась о ремонте дорог: работы завершены на"
        " девяти улицах центрального района."
    )

    assert result.is_digest is True
    assert text_utils.REASON_DIGEST_HEADLINE in result.reasons
    _assert_reasons_are_stable_identifiers(result)


@pytest.mark.parametrize(
    ("text", "case"),
    [
        pytest.param(_PROSE_WITH_STRAY_NUMBER, "stray_numbered_line", id="stray_number"),
        pytest.param(news_text(), "plain_news", id="plain_news"),
        pytest.param(
            "Заседание перенесли.\n"
            "— Решение принято единогласно, — заявил председатель комиссии.\n"
            "Дата нового заседания станет известна позже.",
            "direct_speech_dash",
            id="direct_speech",
        ),
    ],
)
def test_ordinary_prose_is_not_flagged_as_a_digest(text: str, case: str) -> None:
    result = analyze_post(text)

    assert result.is_digest is False, f"{case} was misread as a digest"
    assert not DIGEST_REASONS & set(result.reasons)
    assert result.should_skip is False


@pytest.mark.parametrize(
    ("text", "expected_reason"),
    [
        pytest.param(
            "Оформите вклад под 18% годовых и получите бонус на карту.\n"
            "Реклама. ООО «Банк», erid: 2VtzqvJk9ZH",
            text_utils.REASON_AD_ERID,
            id="erid_marker",
        ),
        pytest.param(
            "Новый жилой комплекс на берегу реки открыл продажи квартир с"
            " отделкой.\nНа правах рекламы.",
            text_utils.REASON_AD_FOOTER,
            id="ad_footer",
        ),
        pytest.param(
            "Только три дня скидка 30% на все курсы английского языка.\n"
            "Используйте промокод: LETO2026 при оформлении заказа.",
            text_utils.REASON_AD_PROMO_CODE,
            id="promo_code",
        ),
        pytest.param(
            "Бот подберёт вакансию за минуту, попробуйте:"
            " https://t.me/jobsbot?start=ref12345",
            text_utils.REASON_AD_REFERRAL_LINK,
            id="referral_bot_link",
        ),
        pytest.param(
            "Подборка выгодных предложений этой недели: https://bit.ly/3xYzAbc",
            text_utils.REASON_AD_SHORTENER,
            id="url_shortener",
        ),
        pytest.param(
            "Смотрите:\nhttps://example.com/a\nhttps://example.com/b\n"
            "https://example.com/c\nhttps://example.com/d\n"
            "https://example.com/e",
            text_utils.REASON_AD_LINK_DENSITY,
            id="link_density",
        ),
    ],
)
def test_advertising_is_detected(text: str, expected_reason: str) -> None:
    result = analyze_post(text)

    assert result.is_ad is True
    assert expected_reason in result.reasons
    assert result.should_skip is True
    _assert_reasons_are_stable_identifiers(result)


def test_link_density_counts_hidden_telegram_text_links() -> None:
    text = "Подборка предложений недели, смотрите варианты по ссылкам ниже."
    entities = [
        {
            "type": "text_link",
            "offset": 0,
            "length": 8,
            "url": f"https://shop.example.com/offer{index}",
        }
        for index in range(text_utils.LINK_SPAM_MIN_LINKS)
    ]

    result = analyze_post(text, entities)

    assert result.link_count == text_utils.LINK_SPAM_MIN_LINKS
    assert text_utils.REASON_AD_LINK_DENSITY in result.reasons
    assert result.is_ad is True


@pytest.mark.parametrize(
    ("text", "case"),
    [
        pytest.param(
            "Реклама на телевидении подорожала на 12% за год, подсчитали в"
            " отраслевой ассоциации.\n"
            "По оценке аналитиков, основной рост пришёлся на федеральные каналы"
            " и вечерний прайм.\n"
            "При этом реклама в интернете растёт быстрее других носителей,"
            " говорится в отчёте.",
            "the word appears mid-sentence, not as a disclosure",
            id="reklama_mid_sentence",
        ),
        pytest.param(
            "Рынок рекламы вырос на 12% по итогам года, следует из отчёта"
            " ассоциации коммуникационных агентств.\n"
            "Эксперты связывают динамику с возвращением крупных брендов и"
            " ростом онлайн-видео.",
            "inflected form inside prose",
            id="reklamy_inflected",
        ),
        pytest.param(
            "Ретейлеры объявили скидку 20% на школьные товары в конце августа,"
            " сообщает отраслевая ассоциация.",
            "a discount reported as news, with no promo code",
            id="discount_as_news",
        ),
        pytest.param(
            "Бюджет NASA вырос на 20% в этом году, следует из документа"
            " агентства.",
            "an acronym next to a percentage is not a promo code",
            id="acronym_and_percent",
        ),
        pytest.param(
            news_text(),
            "an ordinary news story",
            id="plain_news",
        ),
    ],
)
def test_news_prose_is_not_flagged_as_advertising(text: str, case: str) -> None:
    result = analyze_post(text)

    assert result.is_ad is False, f"false positive: {case}"
    assert result.reasons == ()
    assert result.should_skip is False


def test_every_declared_reason_fits_the_filter_reasons_column() -> None:
    # The whole vocabulary, not just the ones a test happens to trigger: adding
    # a longer identifier later must fail here rather than truncate in Postgres.
    for reason in ALL_FILTER_REASONS:
        assert len(reason) <= FILTER_REASON_MAX_LENGTH, (
            f"reason {reason!r} is {len(reason)} chars, over the "
            f"ARRAY(String({FILTER_REASON_MAX_LENGTH})) width"
        )
        assert reason == reason.strip()
    assert DIGEST_REASONS < ALL_FILTER_REASONS
    assert AD_REASONS < ALL_FILTER_REASONS
    assert DIGEST_REASONS.isdisjoint(AD_REASONS)
    assert DIGEST_REASONS | AD_REASONS == ALL_FILTER_REASONS


def test_should_skip_tracks_both_rejection_flags() -> None:
    digest = analyze_post(_NUMBERED_DIGEST)
    advert = analyze_post(
        "Новый жилой комплекс открыл продажи квартир.\nНа правах рекламы."
    )
    clean = analyze_post(news_text())

    assert (digest.is_digest, digest.is_ad, digest.should_skip) == (True, False, True)
    assert (advert.is_digest, advert.is_ad, advert.should_skip) == (False, True, True)
    assert (clean.is_digest, clean.is_ad, clean.should_skip) == (False, False, False)
    assert clean.clean_text and clean.clean_html


async def test_digest_and_ad_rows_are_closed_at_level_zero(
    deduplicator: Deduplicator, session: FakeSession, fake_redis: FakeRedis
) -> None:
    digest_row = build_raw_news(1, _NUMBERED_DIGEST, is_digest=True)
    ad_row = build_raw_news(2, news_text(), is_ad=True)

    digest_decision = await deduplicator.deduplicate(session.as_session(), digest_row)
    ad_decision = await deduplicator.deduplicate(session.as_session(), ad_row)

    assert digest_decision.status is DedupStatus.FILTERED_DIGEST
    assert digest_decision.reason == REASON_FILTERED_DIGEST
    assert ad_decision.status is DedupStatus.FILTERED_AD
    assert ad_decision.reason == REASON_FILTERED_AD
    for decision in (digest_decision, ad_decision):
        assert decision.cluster_id is None
        assert decision.tokens_spent is False
        assert decision.level is None
    assert digest_row.is_processed is True
    assert ad_row.is_processed is True
    # Level 0 spends nothing at all: no cluster, no LSH entry.
    assert session.added == []
    assert await redis_size(fake_redis) == 0


# ============================================================ level 3 novelty


def test_extract_facts_finds_dates_numbers_money_percent_and_entities() -> None:
    facts = extract_facts(
        "Мэр Сергей Собянин открыл станцию метро «Пыхтино» 14 марта 2026 года в"
        " 12:30.\n"
        "Стройка обошлась бюджету в 23 млрд рублей, пассажиропоток оценивают в"
        " 45 тысяч человек.\n"
        "Сроки монтажа сократились на 15%, вчера объект принял первых"
        " пассажиров."
    )

    assert "date:2026-03-14" in facts
    assert "time:12:30" in facts
    assert "when:вчера" in facts
    assert "money:23000000000:RUB" in facts
    assert "num:45000" in facts
    assert "pct:15" in facts
    assert any(fact.startswith("ent:") and "собянин" in fact for fact in facts)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Ущерб составил 150 млн рублей.", "money:150000000:RUB", id="rub"),
        pytest.param("Контракт на $2,5 млн подписан.", "money:2500000:USD", id="usd"),
        pytest.param("Инфляция замедлилась до 7,4%.", "pct:7.4", id="percent"),
        pytest.param("Пострадали 23 человека.", "num:23", id="count"),
        pytest.param(
            "Совещание прошло 2026-03-14.", "date:2026-03-14", id="iso_date"
        ),
        pytest.param(
            "Заседание назначено на 09.05.2026.", "date:2026-05-09", id="dotted"
        ),
    ],
)
def test_extract_facts_canonicalises_figures(text: str, expected: str) -> None:
    assert expected in extract_facts(text)


def test_identical_text_produces_an_empty_fact_delta() -> None:
    assert extract_facts(news_text()) - extract_facts(news_text()) == set()
    assert extract_facts(news_text_minor_edit()) - extract_facts(news_text()) == set()


async def test_duplicate_carrying_a_new_figure_becomes_an_update(
    deduplicator: Deduplicator, session: FakeSession
) -> None:
    added_fact = next(
        iter(extract_facts(news_text_with_extra_fact()) - extract_facts(news_text()))
    )
    assert added_fact == "money:850000000:RUB"

    original = await deduplicator.deduplicate(
        session.as_session(), build_raw_news(1, news_text())
    )
    follow_up_row = build_raw_news(2, news_text_with_extra_fact())
    follow_up = await deduplicator.deduplicate(session.as_session(), follow_up_row)

    assert follow_up.status is DedupStatus.UPDATE
    assert follow_up.reason == REASON_L3_NEW_FACTS
    assert follow_up.new_facts == (added_fact,)
    assert follow_up.cluster_id == original.cluster_id
    assert follow_up_row.cluster_id == original.cluster_id
    assert follow_up_row.is_processed is False

    assert original.cluster_id is not None
    cluster = session.clusters[original.cluster_id]
    assert added_fact in cluster.enrichment_data[FACTS_KEY]
    delta: list[dict[str, Any]] = cluster.enrichment_data[FACTS_DELTA_KEY]
    assert [entry["raw_news_id"] for entry in delta] == [2]
    assert delta[0]["facts"] == [added_fact]


# ======================================================== graceful degradation


async def test_ambiguous_post_degrades_to_a_new_cluster_without_credentials(
    deduplicator: Deduplicator, session: FakeSession
) -> None:
    await deduplicator.deduplicate(session.as_session(), build_raw_news(1, news_text()))
    ambiguous = build_raw_news(2, news_text_rewritten())

    decision = await deduplicator.deduplicate(session.as_session(), ambiguous)

    assert decision.status is DedupStatus.UNIQUE
    assert decision.reason == REASON_L2_DEGRADED
    assert decision.level == 1
    assert decision.is_new_cluster is True
    assert decision.tokens_spent is False
    assert ambiguous.entities["dedup_degraded"] is True
    assert session.executed == []


async def test_ambiguous_post_degrades_when_the_ai_call_fails(
    fake_redis: FakeRedis,
) -> None:
    ai = FailingAIClient()
    engine = Deduplicator(redis=fake_redis, ai_client=ai)
    session = FakeSession()
    try:
        await engine.deduplicate(session.as_session(), build_raw_news(1, news_text()))
        ambiguous = build_raw_news(2, news_text_rewritten())
        decision = await engine.deduplicate(session.as_session(), ambiguous)
    finally:
        await engine.close()

    assert ai.embed_calls == 1
    assert decision.status is DedupStatus.UNIQUE
    assert decision.reason == REASON_L2_DEGRADED
    assert decision.tokens_spent is False
    assert ambiguous.entities["dedup_degraded"] is True
    assert session.executed == []


async def test_media_only_post_without_hashable_text_opens_a_cluster(
    deduplicator: Deduplicator, session: FakeSession, fake_redis: FakeRedis
) -> None:
    # An all-ones signature over an empty shingle set would report Jaccard 1.0
    # against every other empty post, so such rows must stay out of the index.
    assert compute_minhash("📷🎬") is None
    row = build_raw_news(1, "📷🎬")

    decision = await deduplicator.deduplicate(session.as_session(), row)

    assert decision.status is DedupStatus.UNIQUE
    assert decision.is_new_cluster is True
    assert decision.tokens_spent is False
    assert await redis_size(fake_redis) == 0


async def test_empty_text_post_is_skipped_without_cluster(
    deduplicator: Deduplicator, session: FakeSession, fake_redis: FakeRedis
) -> None:
    row = build_raw_news(1, "   ")

    decision = await deduplicator.deduplicate(session.as_session(), row)

    assert decision.status is DedupStatus.SKIPPED_DUPLICATE
    assert decision.is_new_cluster is False
    assert decision.cluster_id is None
    assert decision.reason == REASON_EMPTY_TEXT
    assert await redis_size(fake_redis) == 0


# ============================================================ AI client parsing


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("YES", True),
        ("yes", True),
        ("Yes.", True),
        ("**YES**", True),
        ("ДА", True),
        ("да!", True),
        ("no.", False),
        ("**NO**", False),
        ("NO", False),
        ("нет", False),
        ("Нет.", False),
        ("возможно", None),
        ("", None),
        ("   ", None),
        ("не могу ответить", None),
        ("42", None),
    ],
)
def test_parse_yes_no_handles_real_arbiter_replies(
    answer: str, expected: bool | None
) -> None:
    assert parse_yes_no(answer) is expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [("YES", True), ("no.", False), ("**NO**", False), ("ДА", True), ("нет", False)],
)
async def test_ask_yes_no_parses_a_mocked_openrouter_reply(
    content: str, expected: bool
) -> None:
    payload = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    with respx.mock(
        base_url=settings.openrouter_base_url, assert_all_called=True
    ) as mock:
        route = mock.post(CHAT_COMPLETIONS_PATH).mock(
            return_value=httpx.Response(200, json=payload)
        )
        client = AIClient(api_key="test-key")
        try:
            verdict = await client.ask_yes_no("prompt")
        finally:
            await client.close()

    assert verdict is expected
    request = route.calls.last.request
    body: dict[str, Any] = json.loads(request.content)
    assert body["model"] == settings.ai_model_arbiter
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 5


async def test_ask_yes_no_returns_none_for_an_unparseable_reply() -> None:
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "не уверен"}}]
    }
    with respx.mock(
        base_url=settings.openrouter_base_url, assert_all_called=True
    ) as mock:
        mock.post(CHAT_COMPLETIONS_PATH).mock(
            return_value=httpx.Response(200, json=payload)
        )
        client = AIClient(api_key="test-key")
        try:
            assert await client.ask_yes_no("prompt") is None
        finally:
            await client.close()
