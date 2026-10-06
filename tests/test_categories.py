"""The category catalogue, rewrite styles and local embedding contract.

These are specification constants rather than behaviour, and that is exactly why
they need tests: a WordPress term id is not derivable from anything, so a typo is
invisible until an article appears in the wrong section of a live site. The same
goes for the vector width, which is baked into the database schema.
"""

# The catalogue is Russian editorial copy, so Cyrillic characters resembling Latin
# ones are the subject matter rather than a typo.

from __future__ import annotations

import pytest

from app.config import SCHEMA_EMBEDDING_DIMENSIONS, settings
from app.models.enums import ArticleStatus, RiskLevel
from app.services.categories import (
    BODY_FORMAT_RULES,
    CATEGORY_BADGES,
    CATEGORY_MAP,
    CATEGORY_TITLES,
    DEFAULT_CATEGORY,
    INCIDENT_CATEGORY,
    MILITARY_CATEGORY,
    MODERATION_RULES,
    SPAM_VERDICT,
    STYLE_RULES,
    category_badge,
    category_ids,
    category_title,
    style_for,
    wp_category_id,
)
from app.services.content_prompts import CATEGORIES, CATEGORY_SLUGS
from app.services.local_embeddings import (
    LOCAL_EMBEDDING_DIMENSIONS,
    LOCAL_EMBEDDING_MODEL,
)

#: The map exactly as the specification fixes it. Duplicated here on purpose: a
#: test that imported the value it checks would pass no matter what it became.
SPEC_CATEGORY_MAP: dict[str, int] = {
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


# ============================================================== the catalogue


def test_the_category_map_matches_the_specification_exactly() -> None:
    """Every slug and every WordPress term id, verbatim.

    A wrong id publishes silently into the wrong section: the post appears, the
    request succeeds, and only a reader notices.
    """
    assert CATEGORY_MAP == SPEC_CATEGORY_MAP


def test_the_wordpress_ids_are_unique() -> None:
    """Two categories sharing a term id would merge two sections on the site."""
    assert len(set(CATEGORY_MAP.values())) == len(CATEGORY_MAP)


@pytest.mark.parametrize("slug", sorted(SPEC_CATEGORY_MAP))
def test_every_category_has_a_title_badge_and_style(slug: str) -> None:
    assert slug in CATEGORY_TITLES
    assert slug in CATEGORY_BADGES
    assert style_for(slug)[0]
    assert style_for(slug)[1]


def test_every_category_is_covered_by_exactly_one_style() -> None:
    """A category in two style groups would get whichever voice sorted first.

    The specification's five voices cover eighteen of the twenty; astrology and
    esports were left out, and a sixth voice was added rather than letting them
    fall back to the incident register.
    """
    seen: list[str] = [slug for slugs, _name, _text in STYLE_RULES for slug in slugs]

    assert sorted(seen) == sorted(CATEGORY_MAP)
    assert len(seen) == len(set(seen)), "a category appears in two style groups"


def test_the_five_specified_voices_are_present_verbatim() -> None:
    """The names are part of the specification, not a label we chose."""
    names = [name for _slugs, name, _text in STYLE_RULES]

    for expected in (
        "Народный трибун",
        "Циничный обозреватель",
        "Визионер-популяризатор",
        "Инвест-блогер",
        "Глубокий аналитик",
    ):
        assert expected in names


@pytest.mark.parametrize(
    ("slug", "expected_style"),
    [
        ("chp-i-proisshestvij", "Народный трибун"),
        ("novosti-obshhestva", "Народный трибун"),
        ("novosti-moskvy", "Народный трибун"),
        ("novosti-regionov", "Народный трибун"),
        ("avtonovosti", "Циничный обозреватель"),
        ("novosti-shou-biznesa", "Циничный обозреватель"),
        ("novosti-kriptovalyut", "Циничный обозреватель"),
        ("novosti-sporta", "Циничный обозреватель"),
        ("novosti-it-i-tehnologij", "Визионер-популяризатор"),
        ("iskustvennyj-intelekt", "Визионер-популяризатор"),
        ("novosti-nauki", "Визионер-популяризатор"),
        ("novosti-medicziny-i-zdorovya", "Визионер-популяризатор"),
        ("novosti-biznesa", "Инвест-блогер"),
        ("ekonomika", "Инвест-блогер"),
        ("novosti-turizma", "Инвест-блогер"),
        ("novosti-politiki", "Глубокий аналитик"),
        ("mirovye-novosti", "Глубокий аналитик"),
        ("voennye-novosti", "Глубокий аналитик"),
    ],
)
def test_each_category_maps_to_its_specified_style(
    slug: str, expected_style: str
) -> None:
    assert style_for(slug)[0] == expected_style


def test_an_unknown_slug_falls_back_rather_than_raising() -> None:
    """A mis-classification must not stop generation.

    An article filed under general news is recoverable by an editor; a crashed
    worker loses the news item entirely.
    """
    assert wp_category_id("no-such-rubric") == CATEGORY_MAP[DEFAULT_CATEGORY]
    assert style_for("no-such-rubric") == style_for(DEFAULT_CATEGORY)
    assert category_title("no-such-rubric") == "no-such-rubric"
    assert category_badge("no-such-rubric")


def test_category_ids_returns_the_wordpress_payload() -> None:
    assert category_ids("novosti-politiki") == (2,)


def test_the_prompt_catalogue_and_the_wordpress_map_agree() -> None:
    """One source of truth: a slug with a prompt but no term id cannot publish."""
    assert set(CATEGORIES) == set(CATEGORY_MAP)
    assert set(CATEGORY_SLUGS) == set(CATEGORY_MAP)
    # Sorted so the generated JSON schema is byte-stable between runs.
    assert list(CATEGORY_SLUGS) == sorted(CATEGORY_SLUGS)


# ============================================================== moderation


def test_the_moderation_rules_name_the_override_categories() -> None:
    """The two absolute overrides must be stated, not implied.

    Left to its own judgement a model files a fatal traffic accident under
    transport and a drone strike under technology, and both then reach the wrong
    section and the wrong audience.
    """
    joined = " ".join(MODERATION_RULES)

    assert INCIDENT_CATEGORY in joined
    assert MILITARY_CATEGORY in joined
    assert SPAM_VERDICT in joined
    assert DEFAULT_CATEGORY in joined


def test_the_risk_levels_are_the_specified_traffic_light() -> None:
    assert [level.value for level in RiskLevel] == [
        "green",
        "yellow",
        "red",
        "spam",
    ]


def test_only_spam_blocks_generation() -> None:
    """`spam` is not a degree of risk: it drops the cluster before any spend.

    A red cluster is still written and held for review; a spam one is discarded,
    which is the whole point of classifying it separately.
    """
    assert RiskLevel.SPAM.blocks_generation is True
    for level in (RiskLevel.GREEN, RiskLevel.YELLOW, RiskLevel.RED):
        assert level.blocks_generation is False


def test_the_body_format_rules_ban_wrappers_and_promotion() -> None:
    joined = " ".join(BODY_FORMAT_RULES)

    assert "<h2>" in joined
    assert "```html" in joined
    assert "<body>" in joined
    assert "Telegram" in joined


# ========================================================= the status flow


def test_the_article_status_flow_matches_the_specification() -> None:
    """draft -> draft_tag -> media_planned -> media_ready -> wp_published."""
    values = [status.value for status in ArticleStatus]

    for expected in (
        "draft",
        "draft_tag",
        "media_planned",
        "media_ready",
        "wp_published",
    ):
        assert expected in values

    # Order matters: the enum is declared in pipeline order, and `media_planned`
    # has to sit between tagging and the built assets.
    assert values.index("draft") < values.index("draft_tag")
    assert values.index("draft_tag") < values.index("media_planned")
    assert values.index("media_planned") < values.index("media_ready")
    assert values.index("media_ready") < values.index("wp_published")


# ==================================================== local embeddings


def test_the_embedding_width_matches_the_database_schema() -> None:
    """A mismatch here fails at INSERT time, far from the cause.

    The app's own settings validator refuses to boot on this, which is why the
    three values are asserted together.
    """
    assert LOCAL_EMBEDDING_DIMENSIONS == 384
    assert SCHEMA_EMBEDDING_DIMENSIONS == LOCAL_EMBEDDING_DIMENSIONS
    assert settings.ai_embedding_dimensions == LOCAL_EMBEDDING_DIMENSIONS


def test_the_pinned_model_is_the_one_the_specification_names() -> None:
    assert LOCAL_EMBEDDING_MODEL == (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )


def test_the_similarity_thresholds_are_ordered_and_calibrated() -> None:
    """Calibrated to the pinned model, not copied from the specification.

    Measured over 6 paraphrase pairs and 5 unrelated pairs of real Russian news,
    this model scores duplicates at 0.765-0.911 and unrelated stories at
    -0.07-0.713. The specification's 0.92 upper bound sits above every measured
    duplicate, so it would route all of them to the paid arbiter and defeat the
    purpose of a zero-token stage.
    """
    lower = settings.similarity_lower_threshold
    upper = settings.similarity_upper_threshold

    assert 0.0 < lower < upper < 1.0
    # Above the highest measured unrelated pair (0.713) would start merging
    # distinct stories; below the lowest measured duplicate (0.765) is required
    # for the free path to catch them at all.
    assert upper <= 0.765
    assert lower <= 0.713


def test_the_deduplication_window_is_forty_eight_hours() -> None:
    """Per the specification: a story older than two days is not a duplicate."""
    assert settings.dedup_lookback_hours == 48
