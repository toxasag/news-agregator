"""Moderation: classification, the two absolute rules, and the spam gate.

The moderator is the only stage that can stop a cluster before money is spent, so
two properties matter more than the rest:

* **Spam blocks generation.** Everything else — a red verdict included — still
  produces an article for review; only `spam` is discarded, and the flag that
  decides it must not drift.
* **The two priority rules hold in code, not only in the prompt.** A model left to
  its own judgement files a fatal accident under transport and a drone strike under
  technology, and both then reach the wrong section and the wrong audience. A
  prompt is guidance; the pipeline needs a guarantee.
"""

# The prompt and verdict reasons under test are Russian.
# ruff: noqa: RUF001

from __future__ import annotations

import json
from typing import Any

import pytest

from app.models.enums import RiskLevel
from app.services.categories import (
    CATEGORY_MAP,
    DEFAULT_CATEGORY,
    INCIDENT_CATEGORY,
    MILITARY_CATEGORY,
)
from app.services.moderator import (
    MODERATION_SCHEMA,
    ModerationError,
    ModerationVerdict,
    build_moderation_prompt,
    moderate_cluster,
    parse_verdict,
)


def _reply(**fields: Any) -> str:
    payload = {
        "category": "novosti-obshhestva",
        "risk_level": "green",
        "reason": "Обычная новость.",
    }
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False)


# ================================================================== prompt


def test_the_prompt_lists_every_rubric() -> None:
    """A rubric missing from the prompt is one the model cannot choose.

    Generated from the catalogue rather than written out, so adding a rubric
    cannot leave the prompt behind — the model would invent a slug with no
    WordPress term.
    """
    prompt = build_moderation_prompt()

    for slug in CATEGORY_MAP:
        assert slug in prompt


def test_the_prompt_states_the_priority_rules_and_the_spam_verdict() -> None:
    prompt = build_moderation_prompt()

    assert INCIDENT_CATEGORY in prompt
    assert MILITARY_CATEGORY in prompt
    assert "spam" in prompt
    # The reasoning models this gateway routes to will otherwise think out loud in
    # the content field and run out of budget before the JSON.
    assert "Не рассуждай" in prompt


def test_the_schema_constrains_the_rubric_to_the_catalogue() -> None:
    enum = MODERATION_SCHEMA["properties"]["category"]["enum"]

    assert set(enum) == set(CATEGORY_MAP)
    assert MODERATION_SCHEMA["properties"]["risk_level"]["enum"] == [
        level.value for level in RiskLevel
    ]


# ================================================================= parsing


def test_a_clean_verdict_parses() -> None:
    verdict = parse_verdict(_reply(category="ekonomika", risk_level="yellow"))

    assert verdict.category == "ekonomika"
    assert verdict.risk_level is RiskLevel.YELLOW
    assert verdict.blocks_generation is False


def test_only_spam_blocks_generation() -> None:
    """A red cluster is still written and held; a spam one is discarded."""
    assert parse_verdict(_reply(risk_level="spam")).blocks_generation is True
    for level in ("green", "yellow", "red"):
        assert parse_verdict(_reply(risk_level=level)).blocks_generation is False


def test_a_fenced_reply_is_accepted() -> None:
    """Models wrap JSON in a markdown fence often enough to handle it.

    Refusing such a reply would waste a call on a document that is right there.
    """
    verdict = parse_verdict("```json\n" + _reply(category="novosti-sporta") + "\n```")

    assert verdict.category == "novosti-sporta"


def test_json_embedded_in_prose_is_recovered() -> None:
    raw = "Вот мой ответ:\n" + _reply(category="novosti-nauki") + "\nГотово."

    assert parse_verdict(raw).category == "novosti-nauki"


def test_an_unknown_rubric_falls_back_to_the_default() -> None:
    """A mis-classification must not stop the pipeline.

    An article filed under general news is recoverable by an editor; a crashed
    worker loses the news item.
    """
    verdict = parse_verdict(_reply(category="какая-то-выдумка"))

    assert verdict.category == DEFAULT_CATEGORY
    # The model's own answer is kept, so the timeline can show what it proposed.
    assert verdict.model_category == "какая-то-выдумка"


def test_an_unreadable_risk_level_becomes_yellow_not_green() -> None:
    """`green` is the value that lets a post through untouched.

    Defaulting an unparseable risk to `green` would publish an unreviewed story;
    `yellow` still publishes but asks the writer to soften, which is the honest
    reading of "we do not know".
    """
    verdict = parse_verdict(_reply(risk_level="совершенно непонятно"))

    assert verdict.risk_level is RiskLevel.YELLOW


def test_a_missing_reason_gets_a_placeholder() -> None:
    verdict = parse_verdict('{"category": "ekonomika", "risk_level": "green"}')

    assert verdict.reason


@pytest.mark.parametrize("raw", ["не json вообще", "", "[1, 2, 3]", "{сломано"])
def test_an_unusable_reply_raises(raw: str) -> None:
    with pytest.raises(ModerationError):
        parse_verdict(raw)


# ========================================================= priority rules


def test_a_fatal_accident_is_filed_as_an_incident_whatever_the_model_says() -> None:
    """The rule that matters most: a death is an incident, not transport news.

    Observed reasoning in the original n8n workflow too — a model sees a car and
    files the story under motoring.
    """
    verdict = parse_verdict(
        _reply(category="avtonovosti"),
        text="На МКАД произошло ДТП, один человек погиб.",
    )

    assert verdict.category == INCIDENT_CATEGORY
    assert verdict.overridden is True
    assert verdict.model_category == "avtonovosti"
    assert "приоритета" in verdict.override_note


def test_a_drone_story_is_filed_as_military_whatever_the_model_says() -> None:
    verdict = parse_verdict(
        _reply(category="novosti-it-i-tehnologij"),
        text="Средства ПВО сбили 12 беспилотников над регионом.",
    )

    assert verdict.category == MILITARY_CATEGORY
    assert verdict.overridden is True


def test_the_military_rule_wins_over_the_incident_rule() -> None:
    """Both patterns match a shelling report; the beat is military.

    Filing it as an incident would bury a front-line story in the crime section.
    """
    verdict = parse_verdict(
        _reply(category="mirovye-novosti"),
        text="При обстреле погиб один человек, работала ПВО.",
    )

    assert verdict.category == MILITARY_CATEGORY


def test_a_correct_rubric_is_not_overridden() -> None:
    verdict = parse_verdict(
        _reply(category=INCIDENT_CATEGORY), text="В центре города произошёл пожар."
    )

    assert verdict.overridden is False
    assert verdict.override_note == ""


def test_spam_is_never_reclassified() -> None:
    """An advertisement mentioning a drone is still an advertisement.

    Overriding its rubric would move it out of the drop path and publish an ad.
    """
    verdict = parse_verdict(
        _reply(risk_level="spam", category="novosti-biznesa"),
        text="Промокод на курс по сборке дронов, только сегодня!",
    )

    assert verdict.risk_level is RiskLevel.SPAM
    assert verdict.overridden is False
    assert verdict.blocks_generation is True


def test_an_ordinary_story_without_text_is_left_alone() -> None:
    """Without text the code-side rules cannot fire, and must not guess."""
    verdict = parse_verdict(_reply(category="novosti-turizma"), text="")

    assert verdict.category == "novosti-turizma"
    assert verdict.overridden is False


# ================================================================ the call


class _FakeClient:
    """AI client double returning one scripted reply."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self,
        messages: Any,
        *,
        model: str | None = None,
        max_tokens: int,
        temperature: float,
        response_format: Any = None,
    ) -> str:
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return self.reply


async def test_moderate_cluster_sends_the_prompt_and_the_text() -> None:
    client = _FakeClient(_reply(category="novosti-moskvy"))

    verdict = await moderate_cluster(
        "В Москве открыли станцию метро.",
        model_name="auto/fast",
        client=client,  # type: ignore[arg-type]
    )

    assert verdict.category == "novosti-moskvy"
    call = client.calls[0]
    assert call["model"] == "auto/fast"
    # Deterministic: the same story must not land in two rubrics on two runs,
    # because the rubric decides the WordPress section.
    assert call["temperature"] == 0.0
    # The budget has to cover a reasoning preamble as well as the answer: 400
    # tokens was consumed entirely by "thinking" on this gateway.
    assert call["max_tokens"] >= 1000
    assert call["messages"][0]["role"] == "system"
    assert "В Москве открыли станцию метро." in call["messages"][1]["content"]


async def test_an_empty_cluster_is_refused_before_the_network() -> None:
    client = _FakeClient(_reply())

    with pytest.raises(ModerationError, match="пустой текст"):
        await moderate_cluster("   ", client=client)  # type: ignore[arg-type]

    assert client.calls == []


async def test_the_verdict_serialises_for_the_timeline() -> None:
    verdict = ModerationVerdict(
        category=INCIDENT_CATEGORY,
        risk_level=RiskLevel.YELLOW,
        reason="Пострадали люди.",
        overridden=True,
        override_note="Правило приоритета.",
        model_category="avtonovosti",
    )

    payload = verdict.to_dict()

    assert payload["category_title"]
    assert payload["blocks_generation"] is False
    assert payload["model_category"] == "avtonovosti"
