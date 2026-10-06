"""Shared fixtures for the filter and deduplication suite.

The suite runs with **zero external services**: Redis is `fakeredis`, the AI
client is a stub (or is left unconfigured on purpose) and PostgreSQL is replaced
by :class:`FakeSession`, a minimal stand-in that implements only the four
`AsyncSession` operations the deduplicator actually calls (`add`, `flush`,
`get`, `execute`). `RawNews` and `NewsCluster` are plain ORM objects that are
never persisted, which is legal: SQLAlchemy models are ordinary Python classes
until a session flushes them.

The news fixtures are deliberately of real news length (140+ words). The 0.85
Jaccard duplicate threshold is calibrated for that: a 30-word snippet has so few
word 3-gram shingles that a single edited clause swings the estimate by tens of
points, so short fixtures would "prove" thresholds that do not hold in
production.
"""

# The fixtures below are Russian news copy, so Cyrillic letters that look like
# Latin ones are unavoidable and expected; RUF001 would flag every sentence.
# ruff: noqa: RUF001

from __future__ import annotations

import os

# Pinned before any `app` import, because `app.config.Settings` reads `.env` at
# first instantiation and is cached and frozen from then on. Without this the suite
# inherits the host's `APP_ENV=production` or developer's local `.env`.
os.environ["APP_ENV"] = "development"
os.environ["ALLOWED_HOSTS"] = ""

import random
import uuid
from collections.abc import AsyncIterator, Awaitable, Sequence
from typing import Any, Final, cast

import pytest
from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cluster import NewsCluster
from app.models.enums import DedupStatus
from app.models.raw_news import RawNews
from app.services.ai_client import AIClient, AIClientError
from app.services.deduplicator import Deduplicator

# --------------------------------------------------------------- news fixtures

#: One realistic Russian news story, one sentence per element. Kept as separate
#: sentences so a fixture can rewrite an individual sentence and the resulting
#: MinHash similarity lands in a known band.
_STORY: Final[tuple[str, ...]] = (
    "Власти Москвы открыли новую станцию метро «Пыхтино» на Солнцевской линии.",
    "Об этом сообщил мэр города Сергей Собянин в ходе торжественной церемонии"
    " 14 марта.",
    "По его словам, станция строилась четыре года и обошлась городскому бюджету"
    " в 23 млрд рублей.",
    "Ожидается, что новым пересадочным узлом ежедневно будут пользоваться около"
    " 45 тысяч пассажиров.",
    "Станция расположена рядом с жилым районом, где живут более 80 тысяч"
    " человек, и связана тёплым переходом с автобусным терминалом.",
    "Проектировщики применили безопорную конструкцию платформы, что позволило"
    " сократить сроки монтажа на 15%.",
    "На объекте установлены лифты для маломобильных пассажиров, тактильная"
    " плитка и система навигации с дублированием на английском языке.",
    "В департаменте транспорта уточнили, что интервал движения поездов в часы"
    " пик составит 90 секунд.",
    "Также в ведомстве добавили, что до конца года на этой же линии планируется"
    " открыть ещё две станции.",
    "Строительные работы велись круглосуточно, на площадке было занято до"
    " 1200 рабочих и 40 единиц техники.",
    "Первые пассажиры прошли через турникеты уже через час после окончания"
    " церемонии.",
    "Мэрия рассчитывает, что новая станция разгрузит соседний участок"
    " Калининско-Солнцевской линии на 12%.",
)

#: Sentences of :data:`_STORY` rewritten in different words while reporting the
#: same event, keyed by their index. Replacing three of them puts the MinHash
#: estimate inside the ambiguous band (0.60 <= sim < 0.85).
_REWRITTEN_SENTENCES: Final[dict[int, str]] = {
    0: "В столице заработала станция «Пыхтино» Солнцевской линии подземки.",
    1: "Открытие провёл мэр Сергей Собянин, церемония прошла 14 марта.",
    3: "Пассажиропоток нового узла оценивают примерно в 45 тысяч человек"
    " в сутки.",
}

_UNRELATED_STORY: Final[tuple[str, ...]] = (
    "Рослесхоз сообщил о крупном лесном пожаре в Красноярском крае, площадь"
    " которого превысила 1200 гектаров.",
    "По данным ведомства, огонь распространился из-за сухой грозы и сильного"
    " ветра в отдалённом районе тайги.",
    "На тушении задействованы 240 сотрудников лесной охраны, восемь вертолётов"
    " и два самолёта-амфибии.",
    "Угрозы населённым пунктам нет, ближайшая деревня находится в 35"
    " километрах от кромки огня.",
    "Региональные власти ввели режим чрезвычайной ситуации и ограничили"
    " посещение лесов до конца недели.",
    "Синоптики прогнозируют дожди только в начале следующей недели, поэтому"
    " пожарным приходится создавать минерализованные полосы.",
    "Ущерб от огня оценивается предварительно в 150 млн рублей, окончательные"
    " подсчёты продолжаются.",
    "Прокуратура начала проверку соблюдения правил пожарной безопасности"
    " лесозаготовительными предприятиями.",
    "Жителей просят сообщать о задымлении по горячей линии и воздерживаться от"
    " разведения костров.",
    "С начала сезона в регионе зарегистрировано 78 лесных пожаров, что на 12%"
    " меньше прошлогоднего показателя.",
)


def news_text() -> str:
    """Return the reference news story (167 words, 12 sentences)."""
    return "\n".join(_STORY)


def news_text_minor_edit() -> str:
    """Same story with a single sentence reworded, as a rewrite of a headline.

    Stays above `lsh_duplicate_threshold`: only one of twelve sentences moves,
    so the vast majority of word 3-grams are shared.
    """
    sentences = list(_STORY)
    sentences[10] = (
        "Первые пассажиры прошли через турникеты спустя час после завершения"
        " церемонии."
    )
    return "\n".join(sentences)


def news_text_rewritten() -> str:
    """Same event told in different words, aimed at the ambiguous band.

    Three of twelve sentences are rephrased, which is what a competing outlet's
    rewrite of the same press release looks like.
    """
    sentences = list(_STORY)
    for index, replacement in _REWRITTEN_SENTENCES.items():
        sentences[index] = replacement
    return "\n".join(sentences)


def news_text_unrelated() -> str:
    """A different event entirely: no shared figures, entities or phrasing."""
    return "\n".join(_UNRELATED_STORY)


def news_text_with_extra_fact() -> str:
    """The reference story plus one new money figure, for the level 3 check."""
    return (
        f"{news_text()}\nПозднее в мэрии уточнили, что подключение инженерных"
        " сетей стоило 850 млн рублей."
    )


# ------------------------------------------------------------- synthetic corpus

_CORPUS_SUBJECTS: Final[tuple[str, ...]] = (
    "мэрия",
    "областной минтранс",
    "городская дума",
    "краевой минздрав",
    "университет",
    "судостроительный завод",
    "агрохолдинг",
    "энергетическая компания",
    "театр драмы",
    "федерация плавания",
    "музей краеведения",
    "перевозчик",
    "стройтрест",
    "технопарк",
    "рыбколхоз",
    "филармония",
    "лесничество",
    "морской порт",
    "телеканал",
    "статистическое управление",
)

_CORPUS_PREDICATES: Final[tuple[str, ...]] = (
    "объявила о завершении капитального ремонта корпуса на улице Гагарина",
    "подписала соглашение о поставке оборудования для профильной лаборатории",
    "отчиталась о снижении аварийности на подведомственных участках",
    "представила программу обновления парка спецтехники",
    "открыла набор на бесплатные курсы повышения квалификации",
    "запустила пилотный проект раздельного сбора отходов",
    "получила разрешение на ввод второй очереди объекта",
    "утвердила регламент обслуживания жителей отдалённых поселений",
    "сообщила о росте числа обращений через электронную приёмную",
    "завершила инвентаризацию имущества после реорганизации",
    "договорилась о совместных исследованиях с академическим институтом",
    "перевела документооборот на отечественную платформу",
    "начала модернизацию системы вентиляции в производственном цехе",
    "объявила конкурс на разработку архитектурной концепции квартала",
    "расширила перечень услуг, доступных без предварительной записи",
)

_CORPUS_DETAILS: Final[tuple[str, ...]] = (
    "работы выполнили без остановки основного технологического процесса",
    "смету согласовали после повторной государственной экспертизы",
    "оборудование смонтировали специалисты подрядной организации из соседнего"
    " региона",
    "персонал прошёл обучение на площадке производителя",
    "проектную документацию доработали по замечаниям надзорного органа",
    "лицензию продлили сразу на пять лет",
    "поставку разбили на четыре партии с приёмкой по каждой",
    "испытания провели в круглосуточном режиме в течение недели",
    "результаты опубликовали в открытом реестре контрактов",
    "гарантийное обслуживание возьмёт на себя поставщик",
    "оплату привязали к фактическому объёму выполненных работ",
    "площадку огородили и оснастили системой видеонаблюдения",
    "график скорректировали из-за поздней поставки насосной группы",
    "часть материалов закупили у местных производителей",
    "склад переоборудовали под временное хранение конструкций",
)

_CORPUS_QUOTES: Final[tuple[str, ...]] = (
    "по словам руководителя направления, нагрузка распределится равномернее",
    "как отметил главный инженер, узел работает в проектном режиме",
    "представитель заказчика подчеркнул, что замечаний к качеству нет",
    "профильный эксперт назвал решение технически оправданным",
    "председатель комиссии добавил, что приёмка прошла без нарушений",
    "начальник участка пояснил, что резерв мощности сохраняется",
    "куратор проекта заявил о готовности к сезонной нагрузке",
    "главный технолог сообщил о выходе на плановую производительность",
)

#: Marker sentence of a corpus document, the one :func:`near_duplicate_of`
#: rewrites to build a near duplicate.
_CORPUS_REPORT_LINE: Final[str] = "отчёт направят до"


def corpus_document(index: int) -> str:
    """Build one deterministic, news-length filler story.

    Documents are assembled from disjoint sentence pools plus index-derived
    figures, so pairwise Jaccard across the corpus stays far below
    `lsh_ambiguous_threshold` and any duplicate verdict against this corpus is a
    genuine false positive rather than a fixture artefact.
    """
    rnd = random.Random(1000 + index)  # noqa: S311 - fixture text, not crypto
    subject = _CORPUS_SUBJECTS[index % len(_CORPUS_SUBJECTS)].capitalize()
    lines = (
        f"{subject} {rnd.choice(_CORPUS_PREDICATES)}.",
        f"Стоимость этапа составила {37 + index} млн рублей, площадь объекта —"
        f" {820 + 11 * index} квадратных метров.",
        f"{rnd.choice(_CORPUS_DETAILS).capitalize()},"
        f" {rnd.choice(_CORPUS_DETAILS)}.",
        f"{rnd.choice(_CORPUS_QUOTES).capitalize()}.",
        f"{rnd.choice(_CORPUS_DETAILS).capitalize()};"
        f" {rnd.choice(_CORPUS_QUOTES)}.",
        f"Заявку зарегистрировали под номером {24_000 + 13 * index},"
        f" {_CORPUS_REPORT_LINE} {1 + index % 27} числа следующего месяца.",
        f"{rnd.choice(_CORPUS_PREDICATES).capitalize()} — эта работа"
        " продолжится и в следующем квартале.",
        f"{rnd.choice(_CORPUS_DETAILS).capitalize()}, добавили в профильном"
        " ведомстве региона.",
    )
    return "\n".join(lines)


def news_corpus(size: int) -> list[str]:
    """Return `size` distinct filler stories."""
    return [corpus_document(index) for index in range(size)]


def near_duplicate_of(text: str) -> str:
    """Return `text` with one clause reworded: a true near duplicate."""
    replaced = text.replace(
        f"{_CORPUS_REPORT_LINE} ", "отчётность в итоге направят до "
    )
    if replaced == text:
        raise ValueError("near_duplicate_of expects a corpus_document() text")
    return replaced


# --------------------------------------------------------------- ORM factories

def build_raw_news(
    raw_news_id: int,
    text: str,
    *,
    is_digest: bool = False,
    is_ad: bool = False,
    entities: dict[str, Any] | None = None,
    dedup_status: DedupStatus = DedupStatus.PENDING,
) -> RawNews:
    """Build a transient :class:`RawNews` with no database involved.

    The primary key is assigned explicitly because it is the LSH bucket member:
    `Deduplicator.deduplicate` refuses to run without one, and providing it here
    is what lets the whole suite skip PostgreSQL.
    """
    return RawNews(
        id=raw_news_id,
        source_id=1,
        external_post_id=f"post-{raw_news_id}",
        raw_text=text,
        entities={} if entities is None else dict(entities),
        media_data=[],
        minhash_signature=None,
        is_digest=is_digest,
        is_ad=is_ad,
        is_processed=False,
        dedup_status=dedup_status,
        filter_reasons=[],
    )


def build_cluster(
    title: str = "Существующий инфоповод",
    *,
    facts: Sequence[str] = (),
) -> NewsCluster:
    """Build a transient :class:`NewsCluster` seeded with `facts`."""
    return NewsCluster(
        id=uuid.uuid4(),
        title=title,
        category="general",
        enrichment_data={"facts": list(facts)},
        embedding=None,
    )


# ------------------------------------------------------------- session double

class _ScalarResult:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def all(self) -> list[Any]:
        items: list[Any] = []
        for r in self._rows:
            if isinstance(r, (tuple, list)) and len(r) > 0:
                items.append(r[0])
            else:
                items.append(r)
        return items

    def first(self) -> Any:
        items = self.all()
        return items[0] if items else None


class _Result:
    """The slice of `sqlalchemy.Result` the deduplicator consumes."""

    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def all(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self.scalars().first()


class FakeSession:
    """In-memory stand-in for `AsyncSession`, no PostgreSQL required.

    Implements exactly what the deduplicator uses: `add`, `flush`, `get` (level
    1 cluster lookup) and `execute` (level 2 similarity search, whose rows are
    supplied by the test as `(NewsCluster, cosine_distance)` pairs).
    """

    def __init__(self, semantic_rows: Sequence[tuple[Any, ...]] = ()) -> None:
        self.semantic_rows: list[tuple[Any, ...]] = list(semantic_rows)
        self.clusters: dict[uuid.UUID, NewsCluster] = {}
        self.added: list[Any] = []
        self.flush_count = 0
        self.executed: list[Any] = []
        for row in self.semantic_rows:
            cluster = row[0]
            if isinstance(cluster, NewsCluster):
                self.clusters[cluster.id] = cluster

    def add(self, instance: Any) -> None:
        self.added.append(instance)
        if isinstance(instance, NewsCluster):
            self.clusters[instance.id] = instance

    async def flush(self) -> None:
        self.flush_count += 1

    async def get(self, entity: Any, ident: Any) -> Any:
        if entity is not NewsCluster:
            return None
        return self.clusters.get(ident)

    async def execute(self, statement: Any) -> _Result:
        self.executed.append(statement)
        return _Result(self.semantic_rows)

    def as_session(self) -> AsyncSession:
        """Return `self` typed as `AsyncSession` for the engine under test."""
        return cast(AsyncSession, self)


def semantic_row(cluster: NewsCluster, cosine_similarity: float) -> tuple[Any, ...]:
    """Build one level 2 result row from a *similarity* (not a distance)."""
    return (cluster, 1.0 - cosine_similarity)


# ------------------------------------------------------------------ AI doubles

class StubAIClient(AIClient):
    """Configured AI client that answers from a script, without any network."""

    def __init__(self, *, answers: Sequence[bool | None] = ()) -> None:
        super().__init__(api_key="stub-key")
        self.answers: list[bool | None] = list(answers)
        self.embed_calls: list[str] = []
        self.arbiter_prompts: list[str] = []

    @property
    def is_configured(self) -> bool:
        return True

    async def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return [0.01] * 1536

    async def ask_yes_no(self, prompt: str, *, model: str | None = None) -> bool | None:
        self.arbiter_prompts.append(prompt)
        return self.answers.pop(0) if self.answers else None


class FailingAIClient(AIClient):
    """Configured client whose embedding call always fails."""

    def __init__(self) -> None:
        super().__init__(api_key="stub-key")
        self.embed_calls = 0

    @property
    def is_configured(self) -> bool:
        return True

    async def embed(self, text: str) -> list[float]:
        self.embed_calls += 1
        raise AIClientError("simulated OpenRouter outage")

    async def ask_yes_no(self, prompt: str, *, model: str | None = None) -> bool | None:
        raise AIClientError("simulated OpenRouter outage")


def unconfigured_ai_client() -> AIClient:
    """Return a client with no credentials, i.e. the zero-token deployment."""
    return AIClient(api_key="")


# ------------------------------------------------------------- redis accessors

# `redis-py` shares one signature between its sync and async clients, so every
# command is annotated as `Awaitable[T] | T`. These helpers narrow the async
# half once, instead of casting at each of the dozens of call sites.

async def redis_members(client: FakeRedis, key: str) -> set[str]:
    """Return the members of set `key` as `str`."""
    members = await cast("Awaitable[set[Any]]", client.smembers(key))
    return {str(member) for member in members}


async def redis_set_size(client: FakeRedis, key: str) -> int:
    """Return the cardinality of set `key`."""
    return int(await cast("Awaitable[int]", client.scard(key)))


async def redis_ttl(client: FakeRedis, key: str) -> int:
    """Return the remaining TTL of `key` in seconds."""
    return int(await cast("Awaitable[int]", client.ttl(key)))


async def redis_keys(client: FakeRedis, pattern: str) -> set[str]:
    """Return every key matching `pattern`."""
    return {str(key) async for key in client.scan_iter(match=pattern)}


async def redis_size(client: FakeRedis) -> int:
    """Return the number of keys in the database."""
    return int(await cast("Awaitable[int]", client.dbsize()))


async def redis_get(client: FakeRedis, key: str) -> str | None:
    """Return the string value of `key`, or ``None`` when it is unset."""
    value = await cast("Awaitable[Any]", client.get(key))
    return None if value is None else str(value)


# --------------------------------------------------------------------- fixtures

@pytest.fixture
async def fake_redis() -> AsyncIterator[FakeRedis]:
    """A flushed, `decode_responses=True` fake Redis, closed after each test.

    `decode_responses=True` matches `app.redis_client.get_redis`: the production
    code compares bucket members and JSON payloads as `str`, so a byte-mode
    client would silently exercise a different code path.
    """
    client = FakeRedis(decode_responses=True)
    await client.flushall()
    try:
        yield client
    finally:
        await client.flushall()
        # The pool must go too, not just the client: a pooled connection left
        # behind is reclaimed by the garbage collector, which emits a
        # `ResourceWarning` the suite treats as an error.
        await client.aclose(close_connection_pool=True)


@pytest.fixture
async def deduplicator(fake_redis: FakeRedis) -> AsyncIterator[Deduplicator]:
    """A deduplicator on `fake_redis` with AI disabled: pure level 1 / level 3."""
    engine = Deduplicator(redis=fake_redis, ai_client=unconfigured_ai_client())
    try:
        yield engine
    finally:
        await engine.close()


@pytest.fixture
def session() -> FakeSession:
    """An empty session double with no semantic candidates."""
    return FakeSession()
