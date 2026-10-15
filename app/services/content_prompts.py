"""Editorial prompt matrix: 20 category voices plus the shared output contract.

The generator asks the model for a *single JSON object* rather than prose, and
this module owns both halves of that request:

* :data:`CATEGORIES` — 20 editorial categories, each with its own voice, lead
  style and forbidden habits. A road-accident report and a market analysis are
  different crafts; one generic "write an article" prompt produces the same
  flat register for both, which is exactly the machine-written texture the
  slop blocklist exists to reject.
* :func:`build_messages` — assembles the ``system``/``user`` pair, embedding the
  cluster's facts and the response schema.

Two decisions worth stating, because both are easy to get wrong:

**The schema is enforced twice.** It is sent to the API as a
``json_schema`` response format *and* validated on the way back
(:func:`validate_generation`). Structured-output support varies by model and by
provider, and a model that silently ignores the schema would otherwise put a
malformed article into the database. The schema is the contract; the request
hint is an optimisation.

**Length limits are stated in characters, not tokens.** ``title`` must fit
``articles.title``, ``seo_title`` and ``seo_description`` feed Rank Math meta
fields with hard display limits (70 / 160), and ``seo_keywords`` maps onto
``ARRAY(String(128))``. The prompt states each limit and the validator enforces
it, so a verbose model produces a rejected draft rather than a truncated row.

The anti-slop rules are restated inside every system prompt. Filtering after
generation catches clichés; instructing against them up front means fewer
retries, and a retry costs a full article's worth of tokens.
"""

# The prompts are Russian editorial instructions, so Cyrillic letters that look
# like Latin ones are the subject matter; RUF001 would flag every line.
# ruff: noqa: RUF001, E501

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.models.promoted_project import PromotedProject
from app.services.categories import (
    BODY_FORMAT_RULES,
    CATEGORY_MAP,
    INCIDENT_CATEGORY,
    MILITARY_CATEGORY,
    category_title,
    style_for,
)
from app.services.categories import (
    DEFAULT_CATEGORY as _DEFAULT_CATEGORY,
)

__all__ = [
    "BRAND_SAFETY_STOP_WORDS",
    "CATEGORIES",
    "CATEGORY_SLUGS",
    "DEFAULT_CATEGORY",
    "MAX_SEO_DESCRIPTION_CHARS",
    "MAX_SEO_KEYWORDS",
    "MAX_TITLE_CHARS",
    "MIN_SEO_KEYWORDS",
    "NON_COMMERCIAL_CATEGORIES",
    "REQUIRED_FIELDS",
    "RESPONSE_JSON_SCHEMA",
    "EditorialCategory",
    "build_messages",
    "build_native_advertorial_prompt",
    "build_system_prompt",
    "build_user_prompt",
    "get_category",
    "is_brand_safe_for_commercial_integration",
    "response_format",
]

#: ``articles.title`` is ``String(512)``, but a headline that survives a search
#: result and a Telegram card is far shorter. 70 is the practical SERP limit.
MAX_TITLE_CHARS: Final[int] = 70
#: Google truncates meta descriptions past roughly this width.
MAX_SEO_DESCRIPTION_CHARS: Final[int] = 160
MIN_SEO_KEYWORDS: Final[int] = 3
MAX_SEO_KEYWORDS: Final[int] = 6
#: One keyword must fit ``articles.seo_keywords`` (``ARRAY(String(128))``).
MAX_KEYWORD_CHARS: Final[int] = 128
#: Lower bound on a publishable longread, in characters of HTML.
MIN_CONTENT_HTML_CHARS: Final[int] = 900

#: Fields every generation must carry. Ordered as they appear in the schema so
#: an error message reads in the same order as the prompt.
REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "title",
    "content_html",
    "seo_title",
    "seo_description",
    "seo_keywords",
    "category_slug",
    "image_prompt",
)


@dataclass(frozen=True, slots=True)
class EditorialCategory:
    """One editorial voice.

    ``voice`` is the register, ``lead`` is how the first paragraph must open,
    and ``avoid`` names the failure mode specific to this beat — the thing this
    category's drafts get wrong when left unsupervised.
    """

    slug: str
    title_ru: str
    voice: str
    lead: str
    avoid: str
    structure: str


#: Where an unknown or uncertain slug lands. Re-exported from
#: :mod:`app.services.categories` so callers that already import it from here keep
#: working, while the value itself is defined once.
DEFAULT_CATEGORY: Final[str] = _DEFAULT_CATEGORY

#: Every valid category slug, for the classifier's enum constraint. Sorted so the
#: generated JSON schema is byte-stable between runs, which keeps a prompt cache
#: from missing on key order alone.
CATEGORY_SLUGS: Final[tuple[str, ...]] = tuple(sorted(CATEGORY_MAP))

#: Lead, structure and failure mode per rewrite style. Keyed by style rather than
#: by category, because the specification defines the voices as groups: four
#: separate copies of the incident lead would eventually disagree with each other.
_LEAD_BY_STYLE: Final[Mapping[str, str]] = {
    "Народный трибун": (
        "Первый абзац бьёт по факту: что произошло, где, с кем. Без разгона."
    ),
    "Циничный обозреватель": (
        "Первый абзац — суть события и сразу авторский угол зрения на него."
    ),
    "Визионер-популяризатор": (
        "Первый абзац отвечает: что изобрели или выяснили и почему это важно."
    ),
    "Инвест-блогер": (
        "Первый абзац отвечает: что изменилось и как это касается денег читателя."
    ),
    "Глубокий аналитик": (
        "Первый абзац: что произошло и почему именно сейчас."
    ),
    "Лёгкий развлекательный": (
        "Первый абзац вводит в тему без предисловий и объясняет, о чём речь."
    ),
}

_STRUCTURE_BY_STYLE: Final[Mapping[str, str]] = {
    "Народный трибун": (
        "Событие → детали и хронология → официальные данные → что дальше."
    ),
    "Циничный обозреватель": "Событие → контекст → авторская оценка → чем это кончится.",
    "Визионер-популяризатор": (
        "Суть → как это работает → ограничения → влияние на будущее."
    ),
    "Инвест-блогер": "Что случилось → цифры → что это значит для кошелька → что делать.",
    "Глубокий аналитик": "Событие → предпосылки → расстановка сил → последствия.",
    "Лёгкий развлекательный": "Что произошло → детали → контекст сцены или сезона.",
}

_AVOID_BY_STYLE: Final[Mapping[str, str]] = {
    "Народный трибун": (
        "Натуралистичные подробности, догадки о виновных до официальных выводов, "
        "морализаторство о «нашем обществе»."
    ),
    "Циничный обозреватель": (
        "Ирония в адрес пострадавших людей, переход на личности, издёвка вместо "
        "аргумента."
    ),
    "Визионер-популяризатор": (
        "Нераскрытые термины, обещания революции и путаница между результатом "
        "исследования и его интерпретацией."
    ),
    "Инвест-блогер": (
        "Прямые инвестиционные советы, прогнозы курсов и числа без единиц "
        "измерения или точки отсчёта."
    ),
    "Глубокий аналитик": (
        "Смешивание установленных фактов с заявлениями сторон, безымянные "
        "«эксперты», предсказания исхода."
    ),
    "Лёгкий развлекательный": (
        "Подача предсказаний как фактов, снисходительный тон к интересам "
        "читателя, путаница в названиях команд и игр."
    ),
}

#: The 20-category matrix, derived from :mod:`app.services.categories` so the
#: editorial voice, the WordPress term id and the rewrite style cannot drift
#: apart. Slugs are persisted to ``articles.category``, so they are a stable
#: contract: renaming one orphans every article already filed under it.
CATEGORIES: Final[Mapping[str, EditorialCategory]] = {
    slug: EditorialCategory(
        slug=slug,
        title_ru=category_title(slug),
        voice=style_for(slug)[1],
        lead=_LEAD_BY_STYLE[style_for(slug)[0]],
        avoid=_AVOID_BY_STYLE[style_for(slug)[0]],
        structure=_STRUCTURE_BY_STYLE[style_for(slug)[0]],
    )
    for slug in CATEGORY_MAP
}


#: JSON schema for a generation reply, sent as ``response_format`` so a provider
#: that supports structured output enforces the shape server-side.
#:
#: The limits mirror what the database and the validator enforce, so a reply that
#: satisfies the schema does not then fail local validation on length. ``category_slug``
#: is an enum over :data:`CATEGORY_SLUGS`: free text there is how an article ends up
#: filed under a slug that has no WordPress term.
RESPONSE_JSON_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    # Without this a strict provider rejects a reply carrying extra keys, and a
    # lenient one lets a chatty `explanation` field through into the parser.
    "additionalProperties": False,
    "required": list(REQUIRED_FIELDS),
    "properties": {
        "title": {"type": "string", "maxLength": MAX_TITLE_CHARS},
        "content_html": {"type": "string", "minLength": MIN_CONTENT_HTML_CHARS},
        "seo_title": {"type": "string", "maxLength": MAX_TITLE_CHARS},
        "seo_description": {
            "type": "string",
            "maxLength": MAX_SEO_DESCRIPTION_CHARS,
        },
        "seo_keywords": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": MIN_SEO_KEYWORDS,
            "maxItems": MAX_SEO_KEYWORDS,
        },
        "category_slug": {"type": "string", "enum": list(CATEGORY_SLUGS)},
        "image_prompt": {"type": "string"},
    },
}


def response_format() -> dict[str, Any]:
    """Return the OpenAI/OpenRouter ``response_format`` block for a generation.

    ``strict: true`` asks the provider to enforce the schema server-side. Not
    every model honours it, which is why the response is validated locally as
    well; when it *is* honoured the retry budget is spent on quality rather than
    on shape.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "svodkapress_article",
            "strict": True,
            "schema": RESPONSE_JSON_SCHEMA,
        },
    }


def get_category(
    slug: str, overrides: Mapping[str, Any] | None = None
) -> EditorialCategory:
    """Resolve a category slug, falling back to :data:`DEFAULT_CATEGORY`.

    An unknown slug is a configuration or clustering artefact, not a reason to
    abandon a news item: the generic society voice is a safe default and the
    caller can see the substitution in the returned object's ``slug``.

    ``overrides`` carries dashboard-edited rows from ``category_prompts`` (raw
    ORM rows; passed loosely to keep this module DB-import-free). A stored row
    replaces the built-in fields wholesale.
    """
    key = slug.strip().lower()
    stored = (overrides or {}).get(key)
    if stored is not None:
        return EditorialCategory(
            slug=stored.slug,
            title_ru=stored.title,
            voice=stored.voice,
            lead=stored.lead,
            avoid=stored.avoid,
            structure=stored.structure,
        )
    category = CATEGORIES.get(key)
    if category is not None:
        return category
    return CATEGORIES[DEFAULT_CATEGORY]


#: Restated in every system prompt. Mirrors ``app.services.slop_filter``: the
#: model is told the rule, and the filter enforces it after the fact.
_ANTI_SLOP_RULES: Final[str] = """\
ЗАПРЕЩЕНО (текст с этим будет отклонён автоматически):
- Вступления-разогревы: «В современном мире», «В наши дни», «Не секрет, что», \
«Трудно переоценить».
- Обращения к читателю о структуре текста: «Погрузимся в детали», \
«Давайте рассмотрим», «В этой статье мы расскажем».
- Итоговые абзацы: «Подводя итог», «В заключение», «Время покажет», \
«Таким образом, мы видим».
- Лексика презентаций: «является ключевым фактором», «играет важную роль», \
«неотъемлемая часть», «вывести на новый уровень», «комплексный подход».
- Хеджи-заполнители: «стоит отметить, что», «важно понимать, что», \
«очевидно, что», «как известно».
- Английские клише: delve into, testament to, ever-changing landscape, \
in conclusion.

ТРЕБОВАНИЯ К ТЕКСТУ:
- Начинайте с факта. Первое предложение — суть новости, а не контекст.
- Заканчивайте фактом или конкретным следствием, а не резюме статьи.
- Каждая цифра, дата и цитата — только из переданных данных. Не выдумывайте \
имён, сумм, должностей и дат.
- Если в исходных данных чего-то нет, не пишите об этом вовсе.
- Активный залог, конкретные подлежащие. Без канцелярита.
- Не повторяйте заголовок в первом абзаце дословно."""

#: The DNA of Winners editorial standard based on analysis of 51,000+ reader sessions.
_DNA_OF_WINNERS_RULES: Final[str] = """\
СТАНДАРТ КАЧЕСТВА И ДНК ПОБЕДИТЕЛЕЙ (на основе анализа самых читаемых статей издания):
1. ЗАГОЛОВОК С ЭМОЦИОНАЛЬНЫМ АКЦЕНТОМ:
   - Формула: Субъект/Персона/Бренд + Конкретное действие/поворот + Интрига или выгода + 1-2 эмодзи в конце (например: 🍔❤️, 🤖⚡, 🚇👑, 🍫❤️, 📈🔥).
   - Длина: до 75 символов. Без внешних кавычек вокруг всего заголовка. Без кликбейтного обмана, но с сильным живым крючком.
2. ЛИД (ПЕРВЫЙ АБЗАЦ):
   - Строго 2–3 емких предложения (150–250 знаков).
   - Сразу суть: Кто, Что сделал, Где и Почему это важно прямо сейчас.
   - Никаких долгих предысторий и разогревов.
3. ЧЕЛОВЕКОПОНЯТНОСТЬ, ПРЕДЫСТОРИЯ И ПРАКТИЧЕСКАЯ ПОЛЬЗА (АНТИ-СКУКА):
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕН сухой академический тон, канцелярский официоз и заумные абстракции!
   - Пишите живым, энергичным и понятным языком для умных людей. Сложные термины и процессы объясняйте простыми аналогиями.
   - ПРЕДЫСТОРИЯ И ХРОНОЛОГИЯ: обязательно выделите отдельный смысловой блок с подзаголовком <h2> (например, «Предыстория событий», «Как развивался конфликт / расследование», «Хроника событий»), где раскройте корни ситуации, как к этому пришли и почему это произошло, опираясь на факты веб-ресерча.
   - ПРАКТИЧЕСКАЯ ПОЛЬЗА: обязательно сделайте блок с подзаголовком <h2> (например, «Что это значит для рынка / пользователей / общества» или «В чём реальная выгода и риски»), где четко объясните, чем эта информация важна читателю на практике и к чему приведет.
4. ТИПОГРАФИКА И СТРУКТУРИРОВАНИЕ ТЕКСТА:
   - ОБЯЗАТЕЛЬНО используйте теги <h2> для смысловых подзаголовков (минимум 2–3 подзаголовка). Подзаголовки должны быть цепляющими и информативными. ЗАПРЕЩЕНО оформлять подзаголовки тегами <p>!
   - КОРОТКИЕ АБЗАЦЫ: строго по 2–3 предложения на абзац <p>. Никаких сплошных простыней текста.
   - СПИСКИ: обязательно используйте маркированные списки <ul><li> с выделением сути жирным шрифтом <strong>Тезис:</strong> пояснение... (минимум 1 список из 3–5 пунктов).
   - ЦИТАТЫ ИЛИ КЛЮЧЕВОЙ ИНСАЙТ: используйте <blockquote>...</blockquote> для сильной цитаты спикера или главной мысли статьи.
5. СТРОЖАЙШИЙ ЗАПРЕТ ВЫДУМЫВАНИЯ (ZERO HALLUCINATION):
   - Каждая цифра, дата, цитата, имя, должность и название компании берутся ТОЛЬКО из переданных фактов и веб-ресерча.
   - Запрещено додумывать вымышленные факты! Если в данных чего-то нет — не пишите об этом.
   - Обогащение текста строится исключительно на контексте профессионального веб-ресерча (предыстория, официальные заявления, реальные цифры рынка).
6. ОБЪЕМ: 2200–3800 символов HTML (глубокий, содержательный материал с фактурой, предысторией и деталями, удерживающий читателя)."""

NON_COMMERCIAL_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "voennye-novosti",
        "chp-i-proisshestvij",
    }
)

BRAND_SAFETY_STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        "погиб", "погибли", "погибших", "погибшие", "смерть", "убит", "убиты",
        "жертв", "жертвы", "теракт", "террорист", "атака бпла", "атака дронов",
        "дрон", "бпла", "дрон-камикадзе", "обстрел", "ранение", "ранены",
        "катастроф", "крушение", "взрыв", "сбиты", "ракета", "бомб", "трагеди", "траур",
    }
)


def is_brand_safe_for_commercial_integration(
    category: str,
    title: str = "",
    text: str = "",
    override_force: bool = False,
) -> tuple[bool, str]:
    """Check whether a story is suitable for commercial/native advertising.

    Strict Brand Safety enforcement: war, accidents, attacks, and human tragedies
    must NEVER contain commercial ads or promoted product advertorials.
    Can be overridden explicitly by editor for retrospective/cultural stories.
    """
    if override_force:
        return True, "Реклама включена принудительно редактором (Brand Safety override)."

    cat_lower = category.strip().lower()
    if cat_lower in NON_COMMERCIAL_CATEGORIES:
        return False, f"Категория «{cat_lower}» исключена из рекламных интеграций (Brand Safety)."

    haystack = f"{title} {text}".lower()

    # Check if this is an entertainment / culture context mentioning retrospective words (e.g. actor tribute)
    is_entertainment_context = any(
        kw in cat_lower for kw in ("kultur", "shou", "kino", "serial", "teatr", "iskuss")
    ) or any(
        kw in haystack for kw in ("трейлер", "сериал", "фильм", "актер", "актриса", "съемк", "премьер", "режиссер")
    )

    for stop_word in BRAND_SAFETY_STOP_WORDS:
        if stop_word in haystack:
            if stop_word in ("смерть", "убит", "убиты") and is_entertainment_context:
                # Retrospective entertainment context: do not hard-block if it's movie/series news
                # unless war/terrorism/disaster words are also present
                has_hard_disaster = any(
                    hard_word in haystack
                    for hard_word in ("теракт", "дрон", "бпла", "обстрел", "ракета", "бомб", "катастроф", "крушение", "взрыв")
                )
                if not has_hard_disaster:
                    return True, f"Упоминание «{stop_word}» определено в культурно-ретроспективном контексте кино/сериалов."

            return False, f"Обнаружен стоп-маркер Brand Safety: «{stop_word}»."

    return True, "Тематика безопасна для нативной интеграции."


def build_native_advertorial_prompt(
    project: PromotedProject,
    cluster_id: str = "",
) -> str:
    """Build editorial prompt section instructing the model to write a native product story."""
    tracked_url = project.build_tracked_url(cluster_id)
    cta = project.cta_text or f"Узнайте подробнее о {project.brand_name} по ссылке"

    return f"""\
# РЕЖИМ ГЕНЕРАЦИИ: НАТИВНАЯ НОВОСТЬ О ПРОДУКТЕ (BRAND JOURNALISM)
Напишите материал как ПОЛНОЦЕННУЮ УВЛЕКАТЕЛЬНУЮ НОВОСТЬ по стандартам ДНК ПОБЕДИТЕЛЕЙ, где органичным центром сюжета или практическим решением выступает продукт:
- Продукт / Бренд: «{project.brand_name}» ({project.name})
- Уникальное преимущество (УТП): {project.usp}
- Решаемая проблема аудитории: {project.problem_solved}
- Ссылка для перехода: {tracked_url}
- CTA-призыв: {cta}

ПРАВИЛА ИНТЕГРАЦИИ ПРОДУКТА:
1. Статья должна читаться как авторитетная, увлекательная редакционная новость или аналитический разбор, а не как рекламный спам.
2. Продукт «{project.brand_name}» должен быть естественной частью инфоповода (пример передового решения, запуск сервиса, ответ на вызов рынка, личный опыт или коллаборация).
3. В тело статьи (ближе к середине или финалу разбора) органично встройте ссылку:
   <p>... подробнее можно узнать по <a href="{tracked_url}" target="_blank" rel="noopener">{cta}</a>.</p>
4. Не используйте штампы «купите прямо сейчас» или «уникальное предложение». Подавайте как интересную технологическую или потребительскую новость."""


#: Extra instruction when the moderator flagged the story as `yellow`.
#: The material is still published, but the sharp edges are filed down — the
#: alternative an editor would otherwise do by hand.
YELLOW_SOFTENING: Final[str] = (
    "УРОВЕНЬ РИСКА — YELLOW. Обязательно сгладь острые углы: убери нецензурную "
    "лексику, натуралистичные и шокирующие подробности, избыточную жестокость. "
    "Факты сохрани, тон сделай сдержанным."
)

#: Extra instruction for emergency events. A drone strike or an air-raid warning
#: is read for safety information, not for analysis, so the usual longread shape
#: is actively harmful here.
EMERGENCY_RULE: Final[str] = (
    "ЭКСТРЕННОЕ СОБЫТИЕ (атака дронов, воздушная тревога, угроза жизни). Пиши "
    "коротко и строго по делу: что произошло, где, какие меры приняты и что "
    "делать читателю. Без аналитики, предысторий и рассуждений."
)


def risk_instructions(risk_level: str, category: str) -> str:
    """Return the extra prompt lines dictated by risk and category.

    Kept separate from the voice so the two compose: a `yellow` incident needs
    softening *and* the emergency shape, and hard-coding the pair into each style
    would duplicate both rules five times over.
    """
    lines: list[str] = []
    if risk_level.strip().lower() == "yellow":
        lines.append(YELLOW_SOFTENING)
    if category in {MILITARY_CATEGORY, INCIDENT_CATEGORY}:
        lines.append(EMERGENCY_RULE)
    return "\n\n".join(lines)


def build_system_prompt(
    category: str,
    overrides: Mapping[str, Any] | None = None,
    *,
    custom_style_prompt: str = "",
) -> str:
    """Build the system prompt for one editorial category.

    The category voice comes first and the shared rules second: the beat-specific
    instruction is what distinguishes this prompt from the other nineteen, and
    models weight early instructions more heavily.
    """
    editorial = get_category(category, overrides)
    style_section = ""
    if custom_style_prompt.strip():
        style_section = (
            f"ФОРМАТ И РЕДАКЦИОННЫЙ СТИЛЬ:\n{custom_style_prompt.strip()}\n\n"
        )
    return (
        f"Вы — редактор рубрики «{editorial.title_ru}» российского новостного "
        f"издания. Вы переписываете сырые сообщения источников в оригинальный "
        f"материал для публикации.\n\n"
        f"{style_section}"
        f"ГОЛОС РУБРИКИ: {editorial.voice}\n"
        f"ЛИД: {editorial.lead}\n"
        f"СТРУКТУРА: {editorial.structure}\n"
        f"НЕ ДОПУСКАЕТСЯ В ЭТОЙ РУБРИКЕ: {editorial.avoid}\n\n"
        f"{_ANTI_SLOP_RULES}\n\n"
        f"{_DNA_OF_WINNERS_RULES}\n\n"
        # Stated in the prompt as well as enforced by the validator: a model told
        # the rule usually complies, and a rejection costs a whole extra call.
        "ФОРМАТ ТЕЛА СТАТЬИ:\n"
        + "\n".join(f"- {rule}" for rule in BODY_FORMAT_RULES)
        + "\n\n"
        "ФОРМАТ ОТВЕТА: только один JSON-объект, без markdown-обёртки, без "
        "пояснений до или после. Все текстовые поля — на русском языке, кроме "
        "image_prompt, который пишется по-английски."
    )


def _format_facts(facts: Sequence[str]) -> str:
    """Render the cluster's accumulated facts as a checklist.

    These come from the deduplicator's level 3 extraction
    (``enrichment_data["facts"]``), already canonicalised, so they are the
    highest-confidence data available and are listed separately from the free
    text to discourage the model from inventing figures.
    """
    if not facts:
        return "(структурированные факты не выделены)"
    return "\n".join(f"- {fact}" for fact in facts)


def _format_sources(sources: Sequence[Mapping[str, Any]]) -> str:
    """Render source posts with their index, so the model can cross-check."""
    if not sources:
        return "(нет исходных сообщений)"
    blocks: list[str] = []
    for position, item in enumerate(sources, start=1):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        url = str(item.get("url") or "").strip()
        header = f"### Источник {position}"
        if url:
            header = f"{header} ({url})"
        blocks.append(f"{header}\n{text}")
    return "\n\n".join(blocks) if blocks else "(нет исходных сообщений)"


def build_user_prompt(
    cluster_data: Mapping[str, Any],
    category: str,
    overrides: Mapping[str, Any] | None = None,
    *,
    custom_style_prompt: str = "",
    promoted_project: PromotedProject | None = None,
    cluster_id: str = "",
) -> str:
    """Build the user message carrying the cluster's facts and the field spec.

    ``cluster_data`` is read defensively: it arrives from the database
    (``news_clusters.enrichment_data``) where keys may be absent on a cluster the
    enrichment stage has not touched yet, and a missing key must degrade the
    prompt rather than raise.
    """
    editorial = get_category(category, overrides)
    title = str(cluster_data.get("title") or "").strip()
    raw_facts = cluster_data.get("facts")
    facts: Sequence[str] = (
        tuple(str(item) for item in raw_facts if isinstance(item, str))
        if isinstance(raw_facts, Sequence) and not isinstance(raw_facts, str | bytes)
        else ()
    )
    raw_sources = cluster_data.get("sources")
    sources: Sequence[Mapping[str, Any]] = (
        tuple(item for item in raw_sources if isinstance(item, Mapping))
        if isinstance(raw_sources, Sequence) and not isinstance(raw_sources, str | bytes)
        else ()
    )
    body = str(cluster_data.get("text") or "").strip()
    focus_kw = str(cluster_data.get("focus_keyword") or "").strip()

    seo_instructions = ""
    if focus_kw:
        seo_instructions = (
            "\n\n# Требования SEO и стандарты Excalibur:\n"
            f"- Главный фокусный поисковый запрос: «{focus_kw}».\n"
            f"- Органично внедрите «{focus_kw}» в title, в первые 150 знаков лида "
            "и в один из заголовков <h2> (без искусственного переспама).\n"
            "- Лид (первый абзац): строго 150–250 знаков, сразу суть, факты и цифры, "
            "без шаблонных фраз вроде 'как стало известно' или 'ни для кого не секрет'.\n"
        )

    research_directive = ""
    if "=== КОНТЕКСТ" in body or facts:
        research_directive = (
            "\n\n# ТРЕБОВАНИЕ К ИСПОЛЬЗОВАНИЮ РЕСЕРЧА И ГЛУБИНЕ МАТЕРИАЛА (ZERO HALLUCINATION):\n"
            "- Статья НЕ должна быть поверхностным пересказом одного сообщения!\n"
            "- ОБЯЗАТЕЛЬНО используйте факты, цифры, цитаты спикеров и предысторию "
            "из внешнего веб-ресерча (раздел «Сводный текст» выше).\n"
            "- КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выдумывать вымышленные факты, цифры, даты, имена "
            "или несуществующие цитаты. Если факта нет в исходниках или ресерче — не пишите о нем!\n"
            "- Органично интегрируйте реальный контекст событий в повествование, "
            "раскрывая причины, расстановку сил и последствия.\n"
            "- Сделайте статью содержательной, увлекательной и авторитетной."
        )

    style_reminder = ""
    if custom_style_prompt.strip():
        style_reminder = (
            f"\n\n# ВЫБРАННЫЙ СТИЛИСТИЧЕСКИЙ ФОРМАТ:\n{custom_style_prompt.strip()}\n"
            "Строго следуйте указанному формату, тональности и структуре."
        )

    followup_directive = ""
    parent_article = cluster_data.get("parent_article")
    if isinstance(parent_article, Mapping) and parent_article.get("title"):
        p_title = str(parent_article.get("title", "")).strip()
        followup_directive = (
            "\n\n# РАЗВИТИЕ СЮЖЕТА (UPDATE / FOLLOW-UP МАТЕРИАЛ):\n"
            f"- Данный материал является прямым развитием ранее опубликованной новости: «{p_title}».\n"
            "- Сфокусируйтесь на НОВЫХ подробностях, развитии ситуации, официальных заявлениях и последствиях.\n"
            "- В лиде обязательно кратко свяжите новое событие с контекстом («В продолжение утренних сообщений...», "
            "«После первых сообщений о происшествии стало известно...» и т.п.), не пересказывая старый текст целиком.\n"
            "- Заголовок должен четко отражать именно новый поворот сюжета или свежие детали."
        )

    native_ad_section = ""
    if promoted_project is not None:
        is_safe, reason = is_brand_safe_for_commercial_integration(
            category=editorial.slug,
            title=title,
            text=f"{body} {' '.join(facts)}",
        )
        if is_safe:
            native_ad_section = f"\n\n{build_native_advertorial_prompt(promoted_project, cluster_id)}\n"
        else:
            native_ad_section = (
                f"\n\n# ВНИМАНИЕ: БЛОКИРОВКА РЕКЛАМЫ (BRAND SAFETY)\n"
                f"{reason} Нативная реклама продукта отключена для этой статьи."
            )

    return (
        f"# Инфоповод\n{title or '(заголовок не задан)'}\n\n"
        f"# Установленные факты\n{_format_facts(facts)}\n\n"
        f"# Исходные сообщения\n{_format_sources(sources)}\n\n"
        f"# Сводный текст\n{body or '(нет)'}"
        f"{seo_instructions}"
        f"{research_directive}"
        f"{style_reminder}"
        f"{followup_directive}"
        f"{native_ad_section}\n\n"
        f"# Задание\n"
        f"Напишите оригинальный материал для рубрики «{editorial.title_ru}» "
        f"(слаг: {editorial.slug}) строго по стандарту «ДНК ПОБЕДИТЕЛЕЙ».\n"
        f"Верните JSON со следующими полями:\n"
        f"- title: заголовок, до {MAX_TITLE_CHARS} символов включительно, "
        f"по формуле ДНК Победителей (яркий хук + 1-2 тематических эмодзи в конце, "
        f"например: 🍔❤️, 🤖⚡, 🚇👑, 🍫❤️, 📈🔥), без кликбейта и без кавычек вокруг всего заголовка.\n"
        f"- content_html: развернутая статья 2200–3800 символов в HTML. Лид (первый абзац) — 150–250 знаков, "
        f"сразу факты (кто, что, где, почему важно сейчас). Минимум два-три подзаголовка <h2> с содержательными "
        f"названиями, абзацы <p>, хотя бы один список <ul><li>. Ноль выдумки (только исходники и ресерч). "
        f"В самом конце content_html обязательно блок: <p><strong>Источники информации:</strong> <a href=\"URL\" target=\"_blank\" rel=\"noopener noreferrer\">Название СМИ или Telegram-канала</a>.</p> "
        f"с реальными кликабельными ссылками на первоисточники. Категорически запрещено выдумывать общие фразы без ссылок (вроде «данные госпиталей», «сообщения туристов»). "
        f"Без вступления-разогрева и без итогового абзаца.\n"
        f"- seo_title: до {MAX_TITLE_CHARS} символов, главный запрос в начале.\n"
        f"- seo_description: до {MAX_SEO_DESCRIPTION_CHARS} символов, законченное "
        f"предложение с конкретным фактом.\n"
        f"- seo_keywords: массив из {MIN_SEO_KEYWORDS}–{MAX_SEO_KEYWORDS} "
        f"поисковых фраз в нижнем регистре.\n"
        f"- category_slug: ровно «{editorial.slug}».\n"
        f"- image_prompt: английский промпт для Flux Schnell — сцена, "
        f"композиция, свет, оптика, фотореализм; без текста и логотипов на "
        f"изображении, без узнаваемых лиц реальных людей."
    )


def build_messages(
    cluster_data: Mapping[str, Any],
    category: str,
    *,
    risk_level: str = "green",
    overrides: Mapping[str, Any] | None = None,
    custom_style_prompt: str = "",
    promoted_project: PromotedProject | None = None,
    cluster_id: str = "",
) -> list[dict[str, str]]:
    """Assemble the ``[system, user]`` message pair for one generation.

    ``risk_level`` comes from the moderator. It appends the softening and
    emergency rules to the *user* turn rather than the system prompt, because it
    is a property of this story, not of the editorial voice.
    """
    extra = risk_instructions(risk_level, category)
    user = build_user_prompt(
        cluster_data,
        category,
        overrides,
        custom_style_prompt=custom_style_prompt,
        promoted_project=promoted_project,
        cluster_id=cluster_id,
    )
    if extra:
        user = f"{extra}\n\n{user}"
    return [
        {
            "role": "system",
            "content": build_system_prompt(
                category, overrides, custom_style_prompt=custom_style_prompt
            ),
        },
        {"role": "user", "content": user},
    ]
