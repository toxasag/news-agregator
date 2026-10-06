"""Category catalogue and rewrite voices, fixed by the pipeline specification.

Three things live here, and each is a contract rather than a preference:

**``CATEGORY_MAP``** pairs every category slug with its WordPress term id. The ids
are not derivable — they are whatever that WordPress install assigned — so they are
pinned in code and asserted by tests. Publishing to the wrong term is silent: the
post appears, just in the wrong section.

**``STYLE_RULES``** groups the categories into the five rewrite voices. A group,
not a per-category voice, because the specification defines them that way: the same
register serves incidents, society, Moscow and the regions, and splitting it would
let four descriptions drift apart.

**``MODERATION_RULES``** are the classification overrides the arbiter must obey.
They exist because an LLM left to its own judgement files a fatal traffic accident
under transport and a drone strike under technology, which then reaches the wrong
WordPress section and the wrong audience.

Slugs are persisted to ``articles.category`` and ``news_clusters.category``, so
renaming one orphans every row already filed under it.
"""

from __future__ import annotations

# The catalogue and the voices are Russian editorial copy, so Cyrillic characters
# resembling Latin ones are the subject matter rather than a typo.
# ruff: noqa: RUF001
from typing import Final

__all__ = [
    "CATEGORY_MAP",
    "DEFAULT_CATEGORY",
    "INCIDENT_CATEGORY",
    "MILITARY_CATEGORY",
    "MODERATION_RULES",
    "SPAM_VERDICT",
    "STYLE_RULES",
    "category_ids",
    "style_for",
    "wp_category_id",
]

#: Category slug -> WordPress term id, exactly as the specification fixes them.
#: A slug missing from this map cannot be published, so the classifier is
#: constrained to these twenty values.
CATEGORY_MAP: Final[dict[str, int]] = {
    "avtonovosti": 661,
    "voennye-novosti": 5,
    "iskustvennyj-intelekt": 658,
    "mirovye-novosti": 3,
    "novosti-it-i-tehnologij": 8,
    "novosti-biznesa": 659,
    "novosti-kriptovalyut": 662,
    "novosti-medicziny-i-zdorovya": 663,
    "novosti-moskvy": 12,
    "novosti-nauki": 664,
    "novosti-obshhestva": 7,
    "novosti-politiki": 2,
    "novosti-regionov": 11,
    "novosti-sporta": 10,
    "novosti-turizma": 9,
    "novosti-shou-biznesa": 660,
    "chp-i-proisshestvij": 6,
    "ekonomika": 4,
    "astrologiya-numerologiya-i-ezoterika": 3485,
    "kibersport-i-igrovye-novosti": 3486,
}

#: Where an uncertain classification lands. Chosen by the specification because a
#: general-news miss is recoverable, while a wrong specialised section is not.
DEFAULT_CATEGORY: Final[str] = "novosti-obshhestva"

#: Crime, accidents and loss of life, regardless of the surrounding subject.
INCIDENT_CATEGORY: Final[str] = "chp-i-proisshestvij"

#: The military beat: SVO, army, drones, air defence.
MILITARY_CATEGORY: Final[str] = "voennye-novosti"

#: Verdict for advertising and self-promotion. Not a category: a spam post is
#: dropped before generation, so no article and no API spend follow it.
SPAM_VERDICT: Final[str] = "spam"

#: Human-readable titles, for the dashboard.
CATEGORY_TITLES: Final[dict[str, str]] = {
    "avtonovosti": "Автоновости",
    "voennye-novosti": "Военные новости",
    "iskustvennyj-intelekt": "Искусственный интеллект",
    "mirovye-novosti": "Мировые новости",
    "novosti-it-i-tehnologij": "Новости IT и технологий",
    "novosti-biznesa": "Новости бизнеса",
    "novosti-kriptovalyut": "Новости криптовалют",
    "novosti-medicziny-i-zdorovya": "Новости медицины и здоровья",
    "novosti-moskvy": "Новости Москвы",
    "novosti-nauki": "Новости науки",
    "novosti-obshhestva": "Новости общества",
    "novosti-politiki": "Новости политики",
    "novosti-regionov": "Новости регионов",
    "novosti-sporta": "Новости спорта",
    "novosti-turizma": "Новости туризма",
    "novosti-shou-biznesa": "Новости шоу-бизнеса",
    "chp-i-proisshestvij": "ЧП и происшествия",
    "ekonomika": "Экономика",
    "astrologiya-numerologiya-i-ezoterika": "Астрология и эзотерика",
    "kibersport-i-igrovye-novosti": "Киберспорт и игры",
}

#: Tailwind classes per category badge, so the articles table is scannable by
#: colour. Grouped by voice, which is what an operator actually distinguishes.
CATEGORY_BADGES: Final[dict[str, str]] = {
    "chp-i-proisshestvij": "bg-rose-950/60 text-rose-300",
    "novosti-obshhestva": "bg-slate-800 text-slate-300",
    "novosti-moskvy": "bg-slate-800 text-slate-300",
    "novosti-regionov": "bg-slate-800 text-slate-300",
    "avtonovosti": "bg-amber-950/60 text-amber-300",
    "novosti-shou-biznesa": "bg-amber-950/60 text-amber-300",
    "novosti-kriptovalyut": "bg-amber-950/60 text-amber-300",
    "novosti-sporta": "bg-amber-950/60 text-amber-300",
    "novosti-it-i-tehnologij": "bg-sky-950/60 text-sky-300",
    "iskustvennyj-intelekt": "bg-sky-950/60 text-sky-300",
    "novosti-nauki": "bg-sky-950/60 text-sky-300",
    "novosti-medicziny-i-zdorovya": "bg-sky-950/60 text-sky-300",
    "novosti-biznesa": "bg-emerald-950/60 text-emerald-300",
    "ekonomika": "bg-emerald-950/60 text-emerald-300",
    "novosti-turizma": "bg-emerald-950/60 text-emerald-300",
    "novosti-politiki": "bg-violet-950/60 text-violet-300",
    "mirovye-novosti": "bg-violet-950/60 text-violet-300",
    "voennye-novosti": "bg-violet-950/60 text-violet-300",
    "astrologiya-numerologiya-i-ezoterika": "bg-fuchsia-950/60 text-fuchsia-300",
    "kibersport-i-igrovye-novosti": "bg-fuchsia-950/60 text-fuchsia-300",
}

#: The five rewrite voices, each covering a group of categories.
#: ``(categories, name, instruction)``.
STYLE_RULES: Final[
    tuple[tuple[tuple[str, ...], str, str], ...]
] = (
    (
        (
            "chp-i-proisshestvij",
            "novosti-obshhestva",
            "novosti-moskvy",
            "novosti-regionov",
        ),
        "Народный трибун",
        (
            "Стиль «Народный трибун» (как Mash/Baza). Динамичный эмоциональный "
            "сторителлинг, разговорные литературные обороты, короткие рубленые "
            "фразы. 100% рерайт: ни одного предложения из источника дословно. "
            "Пишите о людях и конкретных обстоятельствах, а не о тенденциях."
        ),
    ),
    (
        (
            "avtonovosti",
            "novosti-shou-biznesa",
            "novosti-kriptovalyut",
            "novosti-sporta",
        ),
        "Циничный обозреватель",
        (
            "Стиль «Циничный обозреватель». Остро, с легким сарказмом и иронией, "
            "с авторской оценкой происходящего. Ирония направлена на события и "
            "решения, а не на пострадавших людей."
        ),
    ),
    (
        (
            "novosti-it-i-tehnologij",
            "iskustvennyj-intelekt",
            "novosti-nauki",
            "novosti-medicziny-i-zdorovya",
        ),
        "Визионер-популяризатор",
        (
            "Стиль «Визионер-популяризатор» (как Хабр). Просто о сложном: "
            "объясняйте механизм и показывайте влияние на будущее. Каждый термин "
            "раскрывается при первом упоминании."
        ),
    ),
    (
        ("novosti-biznesa", "ekonomika", "novosti-turizma"),
        "Инвест-блогер",
        (
            "Стиль «Инвест-блогер» (как Т—Ж). Разбор на пальцах: что это значит "
            "для денег и повседневной жизни читателя. Числа с единицами измерения "
            "и точкой отсчёта, без инвестиционных советов."
        ),
    ),
    (
        ("novosti-politiki", "mirovye-novosti", "voennye-novosti"),
        "Глубокий аналитик",
        (
            "Стиль «Глубокий аналитик». Фокус на причинах и следствиях: почему это "
            "произошло сейчас и что изменится дальше. Разделяйте установленные "
            "факты и заявления сторон."
        ),
    ),
    # The specification's five voices cover eighteen categories; these two were
    # left without one. Falling back to «Народный трибун» would apply an
    # emotional news-desk register to a horoscope, so they get their own voice.
    # Kept sixth and last so the five specified groups stay verbatim above.
    (
        ("astrologiya-numerologiya-i-ezoterika", "kibersport-i-igrovye-novosti"),
        "Лёгкий развлекательный",
        (
            "Стиль «Лёгкий развлекательный». Живо и с интересом к теме, без "
            "сарказма и без назидательности. Для астрологии и эзотерики: излагайте "
            "как культурное явление и не утверждайте предсказания как факты. Для "
            "киберспорта: называйте команды, игры и результаты точно."
        ),
    ),
)

#: Classification overrides for the moderation prompt, in priority order. The
#: first two are absolute: a fatal accident is an incident even when the subject
#: is transport, and a drone strike is military even when the subject is
#: technology. Without them the arbiter files both by topic.
MODERATION_RULES: Final[tuple[str, ...]] = (
    "Криминал, аварии, пожары, гибель или травмы людей — всегда "
    f"«{INCIDENT_CATEGORY}», независимо от остальной темы.",
    "СВО, армия, военная техника, дроны, ПВО, удары и обстрелы — всегда "
    f"«{MILITARY_CATEGORY}».",
    "Реклама, промокоды, розыгрыши, самопиар подкастов, курсов и каналов — "
    f"risk_level «{SPAM_VERDICT}»: такой пост отбрасывается без генерации.",
    f"Любые сомнения в рубрике — «{DEFAULT_CATEGORY}».",
)

#: Body-format rules shared by every voice. Kept next to the styles because a
#: drifting copy of these is what produces markdown fences in stored HTML.
BODY_FORMAT_RULES: Final[tuple[str, ...]] = (
    "Тело статьи — чистый семантический HTML: <h2>, <p>, <ul>/<li>, <strong>, <em>, "
    "<blockquote>, <figure>, <figcaption>.",
    "Запрещены <html>, <head>, <body>, <script>, <style> и блоки ```html.",
    "Удаляйте любые навязчивые призывы подписаться на чужие каналы, "
    "поставить лайк или рекламные спам-ссылки.",
    "В самом конце статьи обязательно сформируйте аккуратный блок источников "
    "информации: <p><strong>Источники информации:</strong> "
    '<a href="URL" target="_blank" rel="noopener noreferrer">'
    "Название СМИ или Telegram-канала</a>.</p> "
    "Указывайте ТОЛЬКО реальные издания, каналы и материалы с кликабельными ссылками. "
    "Категорически запрещено писать абстрактный текст без ссылок "
    "(например: «сообщения очевидцев», «данные госпиталей»).",
    "Под каждой иллюстрацией или фотографией обязательно указывайте активную подпись "
    "с кликабельной ссылкой на первоисточник: "
    '<figcaption>Фото: <a href="URL" target="_blank" '
    'rel="nofollow noopener noreferrer">Название источника</a></figcaption>.',
)

#: Reverse index from category to its style, built once at import.
_STYLE_BY_CATEGORY: Final[dict[str, tuple[str, str]]] = {
    slug: (name, instruction)
    for slugs, name, instruction in STYLE_RULES
    for slug in slugs
}


def wp_category_id(slug: str) -> int:
    """Return the WordPress term id for ``slug``.

    Falls back to the default category rather than raising: a classifier that
    invented a slug should still produce a publishable article in general news,
    which an editor can refile, instead of stalling the pipeline.
    """
    return CATEGORY_MAP.get(slug, CATEGORY_MAP[DEFAULT_CATEGORY])


def category_ids(slug: str) -> tuple[int, ...]:
    """Return the WordPress ``categories`` payload for ``slug``."""
    return (wp_category_id(slug),)


def style_for(slug: str) -> tuple[str, str]:
    """Return ``(style name, instruction)`` for ``slug``.

    Unknown slugs get the default category's voice, for the same reason
    :func:`wp_category_id` falls back: a mis-classification must not stop
    generation.
    """
    return _STYLE_BY_CATEGORY.get(
        slug, _STYLE_BY_CATEGORY[DEFAULT_CATEGORY]
    )


def category_title(slug: str) -> str:
    """Operator-facing title for ``slug``."""
    return CATEGORY_TITLES.get(slug, slug)


def category_badge(slug: str) -> str:
    """Tailwind classes for this category's badge."""
    return CATEGORY_BADGES.get(slug, "bg-slate-800 text-slate-400")
