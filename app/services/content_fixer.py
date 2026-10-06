"""Fixer/QA sub-agent: the last gate before a draft becomes an article.

:mod:`app.services.content_generator` already rejects a draft whose *shape* is
wrong and whose *style* is machine-written. What it cannot see is whether the
draft is still about the same event as its sources — a model that invents a
plausible figure produces text that is well-formed, idiomatic and wrong. That is
what this module checks.

The review is two staged, and the split is the whole point:

* **Stage 1 is free.** Facts are extracted from the sources and from the draft
  with :func:`~app.services.deduplicator.extract_facts` and compared as sets.
  Structure and length limits are plain string checks. A draft with no defects
  costs **zero tokens** and is returned untouched, which is the common case and
  must not be taxed.
* **Stage 2 is paid, and only for what stage 1 found.** The model is asked to fix
  *specific, enumerated* defects, not to "improve the article". An open rewrite
  would reintroduce the clichés the generator was already forced to avoid and
  could drift further from the sources; a targeted repair cannot.

**Fact extraction is shared with the deduplicator on purpose.** Level 3 of dedup
decides whether a post carries *new* facts using :func:`extract_facts`; this
module decides whether a draft *dropped or invented* facts using the same
function. Two independent extractors would drift, and the pipeline would then
hold two different opinions about what a fact is — dedup merging on one
definition while QA rejects on another.

**Invented facts are the fatal class.** A dropped fact is often legitimate
editing: a twelve-sentence wire story does not become a worse article for
omitting one secondary figure, so a bounded amount of omission is tolerated. A
figure in the draft that appears in *no* source is a fabrication, and one
instance is enough to make the article untrustworthy. The two are therefore
scored separately rather than as a single "fidelity" number.

**A failed repair returns the original.** If the fixer's reply is malformed, or
its output fails the same review, the draft is returned unchanged with the report
attached. Returning a half-applied repair would be worse than returning a draft
whose defects are known and recorded.

The returned dictionary has the same shape the generator produces, plus QA
metadata, so a caller can pass a draft straight through. Nothing here writes to
the database.
"""

# The QA instructions are Russian editorial copy, so Cyrillic letters that look
# like Latin ones are intended.
# ruff: noqa: RUF001

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from app.config import settings
from app.services.ai_client import AIClient, get_ai_client
from app.services.content_generator import (
    SchemaViolationError,
    parse_generation,
    validate_generation,
)
from app.services.content_prompts import (
    MAX_SEO_DESCRIPTION_CHARS,
    MAX_TITLE_CHARS,
    get_category,
    response_format,
)
from app.services.deduplicator import extract_facts
from app.services.slop_filter import analyze_slop, clean_ai_slop
from app.services.text_utils import html_to_plain_text

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "FIXER_MAX_TOKENS",
    "FIXER_TEMPERATURE",
    "MAX_DROPPED_FACT_RATIO",
    "QAIssue",
    "QAReport",
    "review_and_fix_article",
    "review_article",
]

#: A repair returns the whole article, so the budget matches a generation.
FIXER_MAX_TOKENS: Final[int] = 4096

#: Near-deterministic: this is a correction, not a creative pass. Freedom here is
#: freedom to reintroduce the defects being fixed.
FIXER_TEMPERATURE: Final[float] = 0.2

#: Fraction of verifiable source facts a draft may omit before it is treated as
#: having lost the story rather than edited it. Condensing a wire story is what
#: editing *is*; dropping more than a third of its checkable content means the
#: article is about something thinner than the event.
MAX_DROPPED_FACT_RATIO: Final[float] = 0.34

#: Fact classes worth comparing. Entities are excluded deliberately: the
#: extractor's capitalised-bigram heuristic also picks up sentence openers and
#: agency names that an edited article legitimately drops, so requiring them
#: would fire constantly on sound copy. Numbers, money, percentages and dates are
#: what a reader can check against the source.
_VERIFIABLE_PREFIXES: Final[tuple[str, ...]] = ("date:", "money:", "num:", "pct:")

#: Bare integers at or below this are excluded from the *invention* check. Small
#: numbers occur in ordinary prose ("два района", "три станции"), so treating an
#: unmatched ``num:5`` as a fabricated statistic would reject good drafts. Money,
#: percentages and dates are checked at every magnitude, because those are always
#: claims rather than incidental counting.
_SMALL_NUMBER_CEILING: Final[float] = 100.0

#: Minimum length of a word considered for headline/body overlap. Russian
#: function words are short and would satisfy any overlap test on their own.
_SIGNIFICANT_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[^\W\d_]{5,}")

#: How much of the body is scanned for the coherence check. The headline's
#: subject appears early, or the draft has a different problem.
_COHERENCE_SCAN_CHARS: Final[int] = 4000

_H2_RE: Final[re.Pattern[str]] = re.compile(r"<h2[\s>]", re.IGNORECASE)
_P_RE: Final[re.Pattern[str]] = re.compile(r"<p[\s>]", re.IGNORECASE)
_LIST_RE: Final[re.Pattern[str]] = re.compile(r"<(?:ul|ol)[\s>]", re.IGNORECASE)

#: Facts quoted into a repair instruction. Enough to be actionable, few enough
#: that the instruction stays readable.
_MAX_QUOTED_FACTS: Final[int] = 8


@dataclass(frozen=True, slots=True)
class QAIssue:
    """One defect found by the free review stage.

    ``code`` is a stable identifier for logs and the dashboard. ``detail`` is
    written as an *instruction* rather than as a complaint, because it is handed
    verbatim to the fixer model as well as shown to an operator.
    """

    code: str
    detail: str
    fatal: bool = False


@dataclass(frozen=True, slots=True)
class QAReport:
    """Outcome of the free review stage."""

    issues: tuple[QAIssue, ...] = ()
    source_facts: frozenset[str] = frozenset()
    draft_facts: frozenset[str] = frozenset()
    dropped_facts: tuple[str, ...] = ()
    invented_facts: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the draft needs no repair at all — the zero-token fast path."""
        return not self.issues

    @property
    def fatal(self) -> tuple[QAIssue, ...]:
        """Issues that must be fixed rather than merely recorded."""
        return tuple(issue for issue in self.issues if issue.fatal)

    @property
    def codes(self) -> tuple[str, ...]:
        """Issue codes in order, for logging and for the dashboard."""
        return tuple(issue.code for issue in self.issues)

    def instructions(self) -> tuple[str, ...]:
        """Defect descriptions, in the form handed to the fixer model."""
        return tuple(issue.detail for issue in self.issues)


def _verifiable(facts: frozenset[str]) -> frozenset[str]:
    """Keep only the fact classes a reader could check against the source."""
    return frozenset(fact for fact in facts if fact.startswith(_VERIFIABLE_PREFIXES))


def _is_small_number(fact: str) -> bool:
    """Whether a fact is a bare small integer. See :data:`_SMALL_NUMBER_CEILING`."""
    if not fact.startswith("num:"):
        return False
    try:
        return abs(float(fact.removeprefix("num:"))) <= _SMALL_NUMBER_CEILING
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class _FactComparison:
    """Result of comparing a draft's facts against its sources'."""

    source_facts: frozenset[str]
    draft_facts: frozenset[str]
    dropped: tuple[str, ...]
    invented: tuple[str, ...]


def _compare_facts(source_text: str, draft_text: str) -> _FactComparison:
    """Extract and diff the verifiable facts of the sources and the draft."""
    source_facts = _verifiable(frozenset(extract_facts(source_text)))
    draft_facts = _verifiable(frozenset(extract_facts(draft_text)))
    invented = tuple(
        sorted(
            fact for fact in draft_facts - source_facts if not _is_small_number(fact)
        )
    )
    return _FactComparison(
        source_facts=source_facts,
        draft_facts=draft_facts,
        dropped=tuple(sorted(source_facts - draft_facts)),
        invented=invented,
    )


def _check_facts(comparison: _FactComparison, issues: list[QAIssue]) -> None:
    """Report fabricated figures and excessive omission.

    Fabrication is always fatal; omission only past
    :data:`MAX_DROPPED_FACT_RATIO`, because editing a story down is normal.
    """
    if comparison.invented:
        quoted = ", ".join(comparison.invented[:_MAX_QUOTED_FACTS])
        issues.append(
            QAIssue(
                code="qa:invented_facts",
                detail=(
                    "В тексте есть числа, суммы, проценты или даты, которых нет "
                    f"в источниках: {quoted}. Удалите их или замените значениями "
                    "из источников. Не выдумывайте данные."
                ),
                fatal=True,
            )
        )

    source_count = len(comparison.source_facts)
    if not source_count:
        return
    if len(comparison.dropped) / source_count > MAX_DROPPED_FACT_RATIO:
        quoted = ", ".join(comparison.dropped[:_MAX_QUOTED_FACTS])
        issues.append(
            QAIssue(
                code="qa:dropped_facts",
                detail=(
                    "В тексте потеряна значительная часть проверяемых данных из "
                    f"источников: {quoted}. Верните ключевые цифры и даты в "
                    "текст, не меняя их значений."
                ),
                fatal=True,
            )
        )


def _check_structure(body_html: str, issues: list[QAIssue]) -> None:
    """Check the markup contract a longread must satisfy."""
    if not body_html.strip():
        issues.append(
            QAIssue(
                code="qa:empty_body",
                detail="Тело материала пустое. Напишите текст заново.",
                fatal=True,
            )
        )
        return
    if _H2_RE.search(body_html) is None:
        issues.append(
            QAIssue(
                code="qa:missing_h2",
                detail=(
                    "В тексте нет ни одного подзаголовка <h2>. Разбейте материал "
                    "минимум на два смысловых блока с подзаголовками."
                ),
                fatal=True,
            )
        )
    if _P_RE.search(body_html) is None:
        issues.append(
            QAIssue(
                code="qa:missing_p",
                detail="Текст не разбит на абзацы <p>. Оформите его абзацами.",
                fatal=True,
            )
        )
    if _LIST_RE.search(body_html) is None:
        # Not fatal: a short incident report legitimately has nothing to list,
        # and forcing a list would produce filler bullets — which is slop itself.
        issues.append(
            QAIssue(
                code="qa:missing_list",
                detail=(
                    "В тексте нет перечисления <ul><li>. Если в материале есть "
                    "набор параметров или этапов, оформите их списком."
                ),
            )
        )


def _check_metadata(article: Mapping[str, Any], issues: list[QAIssue]) -> None:
    """Check the limits the database columns and Rank Math enforce."""
    checks = (
        ("title", "Заголовок", MAX_TITLE_CHARS, "qa:title_too_long"),
        ("seo_title", "SEO-заголовок", MAX_TITLE_CHARS, "qa:seo_title_too_long"),
        (
            "seo_description",
            "Meta description",
            MAX_SEO_DESCRIPTION_CHARS,
            "qa:description_too_long",
        ),
    )
    for field_name, label, limit, code in checks:
        value = str(article.get(field_name) or "").strip()
        if len(value) > limit:
            issues.append(
                QAIssue(
                    code=code,
                    detail=(
                        f"{label} длиннее {limit} символов ({len(value)}). "
                        "Сократите, сохранив главный факт."
                    ),
                    fatal=True,
                )
            )


def _stems(text: str) -> set[str]:
    """Reduce text to crude stems for the coherence check.

    Russian inflects, so comparing whole words reports a mismatch between a
    headline's "станция" and a body's "станцию" — a false positive on a perfectly
    coherent draft. Truncating each significant word to its first five characters
    is a deliberately crude stemmer: it collapses case and number endings without
    a morphology dependency, and this check only needs to distinguish "same
    story" from "different story", not to analyse grammar.
    """
    return {
        word[:5]
        for word in _SIGNIFICANT_WORD_RE.findall(text.lower())
    }


def _check_coherence(title: str, body_text: str, issues: list[QAIssue]) -> None:
    """Check that the headline and the body are about the same thing.

    A lexical overlap, not semantic analysis: it catches the failure that
    actually occurs — a headline written for one cluster attached to a body from
    another — without pretending to judge meaning. Only words of five or more
    letters count, because Russian function words would satisfy any overlap test
    on their own, and they are compared as stems so inflection does not read as a
    mismatch.
    """
    if not title or not body_text:
        return
    title_words = _stems(title)
    if not title_words:
        return
    body_words = _stems(body_text[:_COHERENCE_SCAN_CHARS])
    if not title_words & body_words:
        issues.append(
            QAIssue(
                code="qa:title_body_mismatch",
                detail=(
                    "Заголовок и текст не пересекаются ни одним значимым словом — "
                    "похоже, они о разных событиях. Приведите заголовок в "
                    "соответствие с текстом материала."
                ),
                fatal=True,
            )
        )


def _check_slop(body_html: str, issues: list[QAIssue]) -> None:
    """Report AI clichés that survived generation.

    The generator already filters these, so a hit here means the draft arrived by
    another route — a manual edit in the dashboard, or a reprocessed article.
    Reported as non-fatal because :func:`review_and_fix_article` strips the
    repairable ones for free before deciding whether to pay for a rewrite.
    """
    report = analyze_slop(body_html)
    if not report.findings:
        return
    quoted = ", ".join(report.reasons[:5])
    issues.append(
        QAIssue(
            code="qa:ai_slop",
            detail=(
                f"В тексте остались шаблонные ИИ-обороты: {quoted}. Замените их "
                "конкретными формулировками или удалите."
            ),
            fatal=bool(report.fatal),
        )
    )


def review_article(
    raw_sources_text: str, generated_article: Mapping[str, Any]
) -> QAReport:
    """Run the free QA review over one draft. Spends no tokens.

    Checks, in order: markup structure, verifiable-fact fidelity against the
    sources, metadata length limits, headline/body coherence, and residual AI
    clichés. Returns a :class:`QAReport` whose ``ok`` property is the caller's
    fast path.

    The title is prepended to the body before fact extraction because a headline
    routinely carries the story's headline figure ("23 млрд") that the body then
    states differently; counting it as absent would report a false omission.
    """
    issues: list[QAIssue] = []
    body_html = str(generated_article.get("content_html") or "")
    body_text = html_to_plain_text(body_html)
    title = str(generated_article.get("title") or "").strip()

    _check_structure(body_html, issues)
    comparison = _compare_facts(raw_sources_text, f"{title}\n{body_text}")
    _check_facts(comparison, issues)
    _check_metadata(generated_article, issues)
    _check_coherence(title, body_text, issues)
    _check_slop(body_html, issues)

    return QAReport(
        issues=tuple(issues),
        source_facts=comparison.source_facts,
        draft_facts=comparison.draft_facts,
        dropped_facts=comparison.dropped,
        invented_facts=comparison.invented,
    )


#: The fixer's system prompt. Framed as copy-editing rather than writing: the
#: draft already passed the generator's style gate, so the model's job is to
#: touch the named defects and nothing else. "Do not rewrite" is the load-bearing
#: instruction — an open rewrite reintroduces the clichés the generator was made
#: to avoid and can drift further from the sources.
_FIXER_SYSTEM_PROMPT: Final[str] = """\
Вы — выпускающий редактор. Вам передают готовый материал, исходные сообщения \
источников и список конкретных замечаний корректуры.

ВАША ЗАДАЧА: устранить перечисленные замечания и НИЧЕГО БОЛЬШЕ.

ПРАВИЛА:
- Не переписывайте материал заново. Сохраните структуру, стиль и формулировки \
всюду, где замечаний нет.
- Не добавляйте фактов, которых нет в источниках. Любая цифра, дата, сумма, \
должность и имя должны присутствовать в исходных сообщениях.
- Не меняйте значения цифр, если замечание не требует этого прямо.
- Не добавляйте вступлений-разогревов («В современном мире», «Не секрет, что»), \
обращений к читателю («Погрузимся в детали», «Давайте рассмотрим») и итоговых \
абзацев («Подводя итог», «В заключение», «Время покажет»).
- Не используйте лексику презентаций: «является ключевым фактором», «играет \
важную роль», «неотъемлемая часть», «комплексный подход».
- Сохраните язык материала (русский) и HTML-разметку: <h2>, <p>, <ul>, <li>, \
<blockquote>. Без <script>, <style>, <iframe>, без inline-стилей и атрибутов \
class/id.

ФОРМАТ ОТВЕТА: только один JSON-объект того же формата, что и входной материал, \
без markdown-обёртки и без пояснений."""


def _build_fixer_messages(
    raw_sources_text: str,
    article: Mapping[str, Any],
    report: QAReport,
    *,
    category_slug: str,
) -> list[dict[str, str]]:
    """Assemble the repair request.

    The sources are included in full because the fatal defects are *factual*: a
    model told to remove an invented figure needs to see what the real one was,
    or it will invent a replacement. The category is named so the beat's voice
    survives the edit.
    """
    editorial = get_category(category_slug)
    defects = "\n".join(
        f"{position}. {detail}"
        for position, detail in enumerate(report.instructions(), start=1)
    )
    payload = {
        "title": article.get("title", ""),
        "content_html": article.get("content_html", ""),
        "seo_title": article.get("seo_title", ""),
        "seo_description": article.get("seo_description", ""),
        "seo_keywords": article.get("seo_keywords", []),
        "category_slug": editorial.slug,
        "image_prompt": article.get("image_prompt", ""),
    }
    return [
        {"role": "system", "content": _FIXER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"# Рубрика\n{editorial.title_ru} (слаг: {editorial.slug})\n"
                f"Голос рубрики: {editorial.voice}\n\n"
                f"# Исходные сообщения источников\n{raw_sources_text.strip()}\n\n"
                f"# Текущий материал (JSON)\n"
                f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
                f"# Замечания корректуры\n{defects}\n\n"
                "Верните исправленный JSON-объект с теми же полями."
            ),
        },
    ]


def _finalise(
    article: Mapping[str, Any],
    *,
    report: QAReport,
    fixed: bool,
    attempts: int,
    residual: QAReport | None = None,
) -> dict[str, Any]:
    """Render the result dictionary, carrying the QA trail with it.

    The QA metadata travels with the article rather than being logged and
    discarded: the dashboard has to be able to show *why* an article was
    corrected, and an operator reviewing a questionable publication needs the
    same information after the fact.
    """
    outcome = report if residual is None else residual
    result = dict(article)
    result.update(
        {
            "qa_fixed": fixed,
            "qa_attempts": attempts,
            "qa_issues": list(report.codes),
            "qa_ok": outcome.ok,
            "qa_remaining_issues": list(outcome.codes),
            "qa_dropped_facts": list(report.dropped_facts),
            "qa_invented_facts": list(report.invented_facts),
        }
    )
    return result


async def review_and_fix_article(
    raw_sources_text: str,
    generated_article: Mapping[str, Any],
    model_name: str | None = None,
    *,
    client: AIClient | None = None,
) -> dict[str, Any]:
    """Review one draft and repair it if the free checks found defects.

    Returns the finalised article as a plain ``dict`` — the generator's shape plus
    a QA trail (``qa_fixed``, ``qa_attempts``, ``qa_issues``, ``qa_ok``,
    ``qa_remaining_issues``, ``qa_dropped_facts``, ``qa_invented_facts``).

    Three paths, cheapest first:

    1. **Clean draft** — returned unchanged, zero tokens.
    2. **Only repairable clichés** — stripped locally with
       :func:`~app.services.slop_filter.clean_ai_slop`; still zero tokens.
    3. **Real defects** — one call to the fast model
       (``AI_MODEL_SEO``, e.g. ``gpt-4o-mini``) with the defects enumerated. The
       repair is re-reviewed, and accepted only if it is genuinely better.

    Never raises for a failed repair: an unusable reply, a schema violation or a
    repair that did not help all return the original draft with its report
    attached. The caller decides whether to publish, queue for a human or discard,
    and it can only make that decision if it still has the draft.
    :class:`~app.services.ai_client.AIClientError` does propagate, because an
    unreachable API is an infrastructure problem rather than a content verdict.
    """
    report = review_article(raw_sources_text, generated_article)
    if report.ok:
        logger.debug("QA passed with no findings; no tokens spent")
        return _finalise(generated_article, report=report, fixed=False, attempts=0)

    # Free repair first: if the only complaint is repairable filler, stripping it
    # locally is both cheaper and more predictable than asking a model to.
    if not report.fatal:
        candidate = dict(generated_article)
        candidate["content_html"] = clean_ai_slop(
            str(candidate.get("content_html") or "")
        )
        local = review_article(raw_sources_text, candidate)
        if local.ok:
            logger.info(
                "QA repaired %s locally without an API call", list(report.codes)
            )
            return _finalise(candidate, report=report, fixed=True, attempts=0)

    category_slug = str(generated_article.get("category_slug") or "")
    engine = get_ai_client() if client is None else client
    messages = _build_fixer_messages(
        raw_sources_text, generated_article, report, category_slug=category_slug
    )

    logger.info(
        "QA found %d issue(s) %s; requesting a repair from %s",
        len(report.issues),
        list(report.codes),
        model_name or settings.ai_model_seo,
    )
    raw = await engine.complete_json(
        messages,
        model=model_name or settings.ai_model_seo,
        max_tokens=FIXER_MAX_TOKENS,
        temperature=FIXER_TEMPERATURE,
        response_format=response_format(),
    )

    try:
        repaired = validate_generation(
            parse_generation(raw), expected_category=category_slug
        )
    except SchemaViolationError as exc:
        # The repair is unusable. The original draft is a known quantity; a
        # malformed replacement is not.
        logger.warning(
            "QA repair returned an invalid article (%s); keeping the original", exc
        )
        return _finalise(generated_article, report=report, fixed=False, attempts=1)

    candidate = dict(generated_article)
    candidate.update(
        {
            "title": repaired.title,
            "content_html": repaired.content_html,
            "seo_title": repaired.seo_title,
            "seo_description": repaired.seo_description,
            "seo_keywords": list(repaired.seo_keywords),
            "category_slug": repaired.category_slug,
            "image_prompt": repaired.image_prompt,
        }
    )
    residual = review_article(raw_sources_text, candidate)

    # Accept only a genuine improvement. A repair that fixed one defect and
    # introduced another is not progress, and silently taking it would let a
    # draft degrade across pipeline stages.
    if len(residual.fatal) < len(report.fatal) or (
        not residual.fatal and len(residual.issues) <= len(report.issues)
    ):
        logger.info(
            "QA repair accepted: %s -> %s", list(report.codes), list(residual.codes)
        )
        return _finalise(
            candidate, report=report, fixed=True, attempts=1, residual=residual
        )

    logger.warning(
        "QA repair did not improve the draft (%s -> %s); keeping the original",
        list(report.codes),
        list(residual.codes),
    )
    return _finalise(generated_article, report=report, fixed=False, attempts=1)
