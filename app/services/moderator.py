"""Moderation: classify a cluster, and drop what must never be written.

The first stage of the content pipeline, and the only one that can stop a cluster
before any money is spent. It answers four things in one call:

* which of the 20 rubrics the story belongs to,
* how risky it is (``green``/``yellow``/``red``),
* whether it is advertising or self-promotion at all (``spam``),
* and why, in one sentence an operator can read.

**Spam is dropped before generation, not after.** An advertising post costs a
writer call, an SEO pass, a cover and a WordPress upload before anyone notices it
was an ad. Classifying first turns that into one cheap call.

**Two classification rules are absolute**, because a model left to its own
judgement files a fatal traffic accident under transport and a drone strike under
technology — both then reach the wrong section and the wrong audience. Crime,
accidents and loss of life are always incidents; military subjects are always
military. Both rules are stated in the prompt and re-applied to the model's answer
in code, because a prompt is guidance and the pipeline needs a guarantee.
"""

from __future__ import annotations

# The prompt and the operator-facing reasons are Russian.
# ruff: noqa: RUF001
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.models.enums import RiskLevel
from app.services.ai_client import (
    AIClient,
    AIClientError,
    AIResponseError,
    get_ai_client,
)
from app.services.categories import (
    CATEGORY_MAP,
    DEFAULT_CATEGORY,
    INCIDENT_CATEGORY,
    MILITARY_CATEGORY,
    MODERATION_RULES,
    SPAM_VERDICT,
    category_title,
)
from app.services.model_pool import PoolResult, run_with_fallback

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "MODERATION_SCHEMA",
    "ModerationError",
    "ModerationVerdict",
    "build_moderation_prompt",
    "moderate_cluster",
    "moderate_with_fallback",
    "parse_verdict",
]

#: Token budget.
#:
#: The verdict itself is ~60 tokens, but `auto/fast` routes to reasoning models
#: that "think" in the visible content field. Measured on this gateway: a 400-token
#: budget was consumed almost entirely by a reasoning preamble
#: (393 completion tokens, `finish_reason: stop`) and the JSON never arrived —
#: which surfaced as "the moderator returned invalid JSON" on three of five test
#: cases. The budget has to cover the thinking as well as the answer.
MAX_TOKENS: Final[int] = 1200

#: Deterministic classification: the same story must not land in two rubrics on
#: two runs, because the rubric decides the WordPress section.
TEMPERATURE: Final[float] = 0.0

#: How much of the cluster text the moderator sees. The rubric and the risk are
#: decided by the lead; sending the whole cluster would multiply cost for no
#: accuracy.
MAX_INPUT_CHARS: Final[int] = 2500

def moderation_schema(
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the verdict schema with the current rubric catalogue.

    The enum grows with every custom rubric the operator creates; a fixed schema
    would make the model invent a slug the database does not know.
    """
    from app.services.category_prompts import catalog_slugs  # noqa: PLC0415

    slugs = catalog_slugs(overrides or {})
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["category", "risk_level", "reason"],
        "properties": {
            "category": {"type": "string", "enum": sorted(slugs)},
            "risk_level": {
                "type": "string",
                "enum": [level.value for level in RiskLevel],
            },
            "reason": {"type": "string", "maxLength": 300},
        },
    }


#: Static fallback used when no overrides are in scope — the built-in catalogue
#: alone. Kept for tests and for code paths that have not yet loaded overrides.
MODERATION_SCHEMA: Final[dict[str, Any]] = moderation_schema()

#: Keyword guards applied to the *text*, not to the model's answer. They exist
#: because the two absolute rules must hold even when the model disagrees, and a
#: misfiled drone strike is not a cosmetic error.
_MILITARY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:бпла|дрон\w*|беспилотник\w*|пво|сво|минобороны|"
    r"обстрел\w*|ракет\w*|всу|фронт\w*|мобилизац\w*)\b",
    re.IGNORECASE,
)
_INCIDENT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:погиб\w*|поги?б|убит\w*|застрел\w*|дтп|пожар\w*|"
    r"взрыв\w*|задержан\w*|уголовн\w*|пострадав\w*|тело|труп)\b",
    re.IGNORECASE,
)


class ModerationError(AIResponseError):
    """The moderator could not produce a usable verdict.

    An `AIResponseError` on purpose: to the fallback pool an unusable verdict is
    the same signal as a malformed completion — this model cannot do the job right
    now, so try the next one and cool this one down.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ModerationVerdict:
    """One moderation outcome."""

    category: str
    risk_level: RiskLevel
    reason: str
    #: True when a code-side rule overrode the model's rubric, with the note in
    #: :attr:`override_note`. Surfaced so the lab timeline can show that the
    #: guarantee fired rather than hiding a silent correction.
    overridden: bool = False
    override_note: str = ""
    #: The rubric the model actually returned, kept for the timeline.
    model_category: str = ""

    @property
    def blocks_generation(self) -> bool:
        """Whether this cluster must not be written at all."""
        return self.risk_level.blocks_generation

    @property
    def category_title(self) -> str:
        return category_title(self.category)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "category_title": self.category_title,
            "risk_level": self.risk_level.value,
            "reason": self.reason,
            "blocks_generation": self.blocks_generation,
            "overridden": self.overridden,
            "override_note": self.override_note,
            "model_category": self.model_category,
        }


def build_moderation_prompt(
    overrides: Mapping[str, Any] | None = None,
) -> str:
    """Assemble the moderator's system prompt.

    The rubric list is generated from the catalogue — built-in plus any custom
    rows — so a rubric added from the dashboard cannot be missing from the
    prompt, which would make the model invent a slug that has no WordPress term.
    """
    from app.services.category_prompts import catalog_slugs  # noqa: PLC0415

    slugs = catalog_slugs(overrides or {})
    rubrics = "\n".join(
        f"- {slug} ({category_title(slug) if slug in CATEGORY_MAP else slug})"
        for slug in sorted(slugs)
    )
    rules = "\n".join(f"- {rule}" for rule in MODERATION_RULES)
    return (
        "Ты главный редактор новостного агентства и юрист. Прочитай новость и "
        "верни СТРОГИЙ JSON без markdown-обёртки и без пояснений вокруг.\n\n"
        "ЗАДАЧА 1 — РУБРИКА. Определи главную суть инфоповода и выбери ровно "
        "один ярлык из списка:\n"
        f"{rubrics}\n\n"
        "ПРАВИЛА ВЫБОРА (обязательны):\n"
        f"{rules}\n\n"
        "ЗАДАЧА 2 — УРОВЕНЬ РИСКА:\n"
        "- green — обычная новость, публикуется как есть;\n"
        "- yellow — есть шокирующие подробности, жестокость или резкая лексика: "
        "материал пишется, но острые углы сглаживаются;\n"
        "- red — публиковать нельзя: призывы к насилию, дискредитация, "
        "запрещённые темы;\n"
        f"- {SPAM_VERDICT} — реклама, промокоды, розыгрыши, самопиар каналов, "
        "курсов и подкастов. Такой пост отбрасывается без генерации.\n\n"
        "ЗАДАЧА 3 — ПРИЧИНА. Одно короткое предложение по-русски: почему "
        "выбрана эта рубрика и этот уровень риска.\n\n"
        "ФОРМАТ ОТВЕТА. Не рассуждай в ответе и не пиши ничего до или после "
        "JSON. Первым символом ответа должна быть открывающая фигурная скобка:\n"
        '{"category": "...", "risk_level": "...", "reason": "..."}'
    )


def parse_verdict(
    raw: str, *, text: str = "", overrides: Mapping[str, Any] | None = None
) -> ModerationVerdict:
    """Parse and normalise the model's answer.

    Every field degrades to a safe default rather than raising: a malformed answer
    should cost one retry at most, and a cluster stuck forever because the model
    returned a stray comma is worse than a cluster filed under general news.

    ``text`` enables the code-side rules. They are applied after parsing, so the
    two absolute classifications hold even when the model disagrees.

    ``overrides`` carries custom rubrics so a dashboard-created slug is accepted
    as valid rather than falling back to the default.
    """
    from app.services.category_prompts import catalog_slugs  # noqa: PLC0415

    payload = _extract_json(raw)

    category = str(payload.get("category") or "").strip().lower()
    model_category = category
    valid_slugs = set(catalog_slugs(overrides or {}))
    if category not in valid_slugs:
        logger.info("Moderator returned unknown rubric %r; using default", category)
        category = DEFAULT_CATEGORY

    raw_risk = str(payload.get("risk_level") or "").strip().lower()
    try:
        risk = RiskLevel(raw_risk)
    except ValueError:
        # An unreadable risk level must not become `green` by accident: that is the
        # value that lets a post through. `yellow` still publishes but asks the
        # writer to soften, which is the safe reading of "we do not know".
        logger.info("Moderator returned unknown risk %r; using yellow", raw_risk)
        risk = RiskLevel.YELLOW

    reason = str(payload.get("reason") or "").strip()[:300] or "Причина не указана."

    # --- The two absolute rules, re-applied in code.
    overridden = False
    note = ""
    if risk is not RiskLevel.SPAM and text:
        if _MILITARY_PATTERN.search(text) and category != MILITARY_CATEGORY:
            overridden, note = True, (
                f"Правило приоритета: в тексте военная тематика — рубрика "
                f"изменена с «{category}» на «{MILITARY_CATEGORY}»."
            )
            category = MILITARY_CATEGORY
        elif _INCIDENT_PATTERN.search(text) and category != INCIDENT_CATEGORY:
            overridden, note = True, (
                f"Правило приоритета: в тексте криминал или пострадавшие — "
                f"рубрика изменена с «{category}» на «{INCIDENT_CATEGORY}»."
            )
            category = INCIDENT_CATEGORY

    return ModerationVerdict(
        category=category,
        risk_level=risk,
        reason=reason,
        overridden=overridden,
        override_note=note,
        model_category=model_category,
    )


def _extract_json(raw: str) -> dict[str, Any]:
    """Pull the JSON object out of a model reply.

    Models wrap JSON in prose or a markdown fence often enough that refusing such
    a reply would waste a call on a document that is right there.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match is None:
            raise ModerationError(
                f"модератор вернул не JSON: {text[:120]!r}"
            ) from None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as error:
            raise ModerationError(
                f"модератор вернул повреждённый JSON: {error}"
            ) from error
    if not isinstance(parsed, dict):
        raise ModerationError("модератор вернул не объект JSON")
    return parsed


async def moderate_cluster(
    text: str,
    *,
    model_name: str | None = None,
    client: AIClient | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> ModerationVerdict:
    """Classify one cluster and decide whether it may be written.

    Raises :class:`ModerationError` when the model is unreachable or its answer is
    unusable. The caller decides what that means: the production worker treats it
    as "leave the cluster for the next tick", which is safer than guessing a rubric
    and publishing into the wrong section.

    ``overrides`` carries dashboard-edited rubric prompts so custom rubrics
    appear in the moderator's classification enum.
    """
    body = (text or "").strip()
    if not body:
        raise ModerationError("пустой текст кластера")

    ai = client or get_ai_client()
    prompt = build_moderation_prompt(overrides=overrides)
    schema = moderation_schema(overrides=overrides)
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": body[:MAX_INPUT_CHARS]},
    ]
    try:
        raw = await ai.complete_json(
            messages,
            model=model_name,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "moderation_verdict",
                    "strict": True,
                    "schema": schema,
                },
            },
        )
    except AIClientError as error:
        raise ModerationError(f"модератор недоступен: {error}") from error

    verdict = parse_verdict(raw, text=body, overrides=overrides)
    logger.info(
        "Moderation: %s / %s%s",
        verdict.category,
        verdict.risk_level.value,
        " (override)" if verdict.overridden else "",
    )
    return verdict


async def moderate_with_fallback(
    text: str,
    chain: Sequence[str],
    *,
    client: AIClient | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> tuple[ModerationVerdict, PoolResult[ModerationVerdict]]:
    """Moderate through a fallback chain, returning the verdict and the route.

    A model that returns an unusable verdict counts as a failed model, not as a
    failed cluster: `ModerationError` is raised inside the call, the pool records a
    cooldown and moves to the next name. That is the difference between "this
    gateway is having a bad minute" and "this news item cannot be classified".
    """

    async def attempt(model: str) -> ModerationVerdict:
        return await moderate_cluster(
            text, model_name=model, client=client, overrides=overrides
        )

    result = await run_with_fallback("arbiter", chain, attempt)
    return result.value, result
