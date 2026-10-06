"""Article generation: one LLM call, validated, with a single slop-driven retry.

The flow is deliberately small and explicit:

1. Build the category's ``[system, user]`` messages
   (:mod:`app.services.content_prompts`).
2. Ask the model for a JSON object matching the schema.
3. Validate the *shape* (:func:`validate_generation`) — required fields, length
   limits, HTML structure.
4. Validate the *style* (:func:`~app.services.slop_filter.validate_ai_text`).
5. On failure, retry **once** with the specific complaint appended, then give up.

Why exactly one retry: a generation costs a full article's worth of tokens, and
a model that produced slop twice under an explicit prohibition will not be
argued into compliance by a third attempt. Escalating retries turn one bad
cluster into an unbounded bill. The second attempt is not a blind repeat either
— the failure reasons are fed back as a correction, which is what makes it worth
paying for at all.

**Soft slop is repaired, not regenerated.** When the only findings are
repairable hedges, :func:`~app.services.slop_filter.clean_ai_slop` strips them
and the draft is accepted. Spending a second call to remove "стоит отметить, что"
would be indefensible.

**Nothing here writes to the database.** The function returns a plain ``dict``
matching the ``articles`` columns; the worker owns the transaction. Keeping the
generator I/O-free at the database boundary is what lets the whole test suite run
without PostgreSQL.
"""

# The corrective prompt is Russian editorial copy, so Cyrillic letters that look
# like Latin ones are intended.
# ruff: noqa: RUF001, RUF002

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.config import settings
from app.models.promoted_project import PromotedProject
from app.services.ai_client import AIClient, get_ai_client
from app.services.content_prompts import (
    MAX_KEYWORD_CHARS,
    MAX_SEO_DESCRIPTION_CHARS,
    MAX_SEO_KEYWORDS,
    MAX_TITLE_CHARS,
    MIN_CONTENT_HTML_CHARS,
    MIN_SEO_KEYWORDS,
    REQUIRED_FIELDS,
    build_messages,
    get_category,
    response_format,
)
from app.services.slop_filter import (
    SlopReport,
    analyze_slop,
    clean_ai_slop,
)

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "GENERATION_MAX_TOKENS",
    "GENERATION_TEMPERATURE",
    "MAX_GENERATION_ATTEMPTS",
    "ContentGenerationError",
    "GeneratedArticle",
    "GenerationRejectedError",
    "SchemaViolationError",
    "generate_article",
    "parse_generation",
    "validate_generation",
]

#: One initial attempt plus one corrective retry. See the module docstring.
MAX_GENERATION_ATTEMPTS: Final[int] = 2

#: A longread plus its metadata. Generous enough that a legitimate article is
#: never truncated mid-tag, which would fail HTML validation for the wrong reason.
GENERATION_MAX_TOKENS: Final[int] = 4096

#: Some variation is wanted — twenty articles a day in one voice must not read
#: like a template — but not so much that the model starts inventing facts.
GENERATION_TEMPERATURE: Final[float] = 0.7

#: Block-level tags a longread must actually contain, per the prompt contract.
_H2_RE: Final[re.Pattern[str]] = re.compile(r"<h2[\s>]", re.IGNORECASE)
_P_RE: Final[re.Pattern[str]] = re.compile(r"<p[\s>]", re.IGNORECASE)

#: Tags that must never appear in generated markup. ``content_html`` is stored
#: and later rendered by WordPress and the dashboard, so a model echoing a
#: ``<script>`` from its input must fail validation rather than be sanitised
#: silently — silent repair would hide a prompt-injection attempt in a source.
_FORBIDDEN_TAG_RE: Final[re.Pattern[str]] = re.compile(
    r"<\s*/?\s*(?:script|style|iframe|object|embed|form|input|link|meta|html|body|head)\b",
    re.IGNORECASE,
)
#: Inline event handlers and styles, same reasoning.
_INLINE_ATTR_RE: Final[re.Pattern[str]] = re.compile(
    r"\son[a-z]+\s*=|\sstyle\s*=", re.IGNORECASE
)

#: Response keys leaking *into* ``content_html``.
#:
#: Observed in production: a model closed the `content_html` string with the wrong
#: quote, so the remaining keys became part of the body. The result is still valid
#: JSON — `json.loads` succeeds and every other field is present — which is why
#: nothing upstream caught it. Four of eleven articles in the first control run
#: carried an English image prompt at the end of the Russian text, and would have
#: been published that way.
#:
#: Treated as a violation rather than trimmed: the boundary between article and
#: scaffolding cannot be recovered reliably, so a retry with a clean reply is
#: worth far more than a silently truncated body.
_LEAKED_FIELD_RE: Final[re.Pattern[str]] = re.compile(
    r"""['"]\s*,\s*['"]?(?P<field>"""
    r"image_prompt|seo_title|seo_description|seo_keywords|title|category"
    r""")['"]?\s*:""",
    re.IGNORECASE,
)

#: Markdown fences a model wraps JSON in despite being told not to. Stripped
#: before parsing because this is a formatting quirk, not a content failure.
_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL
)


class ContentGenerationError(RuntimeError):
    """Base class for content-engine failures."""


class SchemaViolationError(ContentGenerationError):
    """The model's reply did not match the article schema.

    Carries the individual violations so the retry can quote them back.
    """

    def __init__(self, violations: Sequence[str]) -> None:
        self.violations = tuple(violations)
        super().__init__("; ".join(self.violations) or "unspecified schema violation")


class GenerationRejectedError(ContentGenerationError):
    """Every attempt was exhausted without a publishable draft."""

    def __init__(self, message: str, *, attempts: int, reasons: Sequence[str]) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.reasons = tuple(reasons)


@dataclass(frozen=True, slots=True)
class GeneratedArticle:
    """A validated draft, shaped for the ``articles`` table."""

    title: str
    content_html: str
    seo_title: str
    seo_description: str
    seo_keywords: tuple[str, ...]
    category_slug: str
    image_prompt: str
    #: Attempts consumed, so the worker can log the cost of a stubborn cluster.
    attempts: int = 1
    #: Whether soft slop was stripped from ``content_html`` before acceptance.
    slop_cleaned: bool = False
    #: Phrases tolerated in the accepted draft, for the dashboard's QA panel.
    slop_notes: tuple[str, ...] = field(default_factory=tuple)

    def to_article_fields(self) -> dict[str, Any]:
        """Render the column values for an ``articles`` insert.

        ``seo_keywords`` becomes a ``list`` because the column is
        ``ARRAY(String(128))`` and SQLAlchemy binds a list, not a tuple.
        """
        return {
            "title": self.title,
            "content_html": self.content_html,
            "seo_title": self.seo_title,
            "seo_description": self.seo_description,
            "seo_keywords": list(self.seo_keywords),
            "category": self.category_slug,
        }


def _strip_fence(raw: str) -> str:
    """Remove a markdown code fence around a JSON payload, if present."""
    match = _FENCE_RE.match(raw)
    return match.group("body") if match is not None else raw.strip()


def parse_generation(raw: str) -> dict[str, Any]:
    """Parse the model's reply into a JSON object.

    Tolerates a markdown fence and leading/trailing prose, because those are
    formatting noise rather than content failures. Anything that is not a JSON
    object raises :class:`SchemaViolationError`, which the retry can act on.
    """
    candidate = _strip_fence(raw)
    try:
        payload: Any = json.loads(candidate)
    except ValueError:
        # A model that prefixed the object with a sentence still produced usable
        # JSON; recover the outermost braces before giving up.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise SchemaViolationError(
                ["reply is not JSON and contains no JSON object"]
            ) from None
        try:
            payload = json.loads(candidate[start : end + 1])
        except ValueError:
            raise SchemaViolationError(["reply is not valid JSON"]) from None
    if not isinstance(payload, dict):
        raise SchemaViolationError(
            [f"reply is a JSON {type(payload).__name__}, expected an object"]
        )
    return payload


def _as_text(value: Any) -> str:
    """Coerce a scalar field to a stripped string."""
    return value.strip() if isinstance(value, str) else ""


def _as_keywords(value: Any) -> tuple[str, ...]:
    """Normalise ``seo_keywords`` from either a list or a comma-separated string.

    Both shapes are accepted because the schema asks for an array while the
    prompt's wording ("comma-separated") invites a string, and a draft is too
    expensive to discard over that ambiguity. Entries are lowercased,
    de-duplicated and stripped of leading hashes.
    """
    items: list[str]
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, Sequence) and not isinstance(value, bytes):
        items = [item for item in value if isinstance(item, str)]
    else:
        return ()
    cleaned = (item.strip().lstrip("#").strip().lower() for item in items)
    return tuple(dict.fromkeys(item for item in cleaned if item))


def _check_length(name: str, value: str, limit: int, violations: list[str]) -> None:
    """Record a violation when ``value`` exceeds ``limit`` characters."""
    if len(value) > limit:
        violations.append(f"{name} is {len(value)} chars, limit is {limit}")


def _body_violations(body: str) -> list[str]:
    """Check ``content_html`` for length, required structure and unsafe markup.

    Unsafe markup is a *rejection*, not something to sanitise away: a
    ``<script>`` in a generated article means the model echoed markup out of a
    scraped source, and quietly stripping it would hide an attempted injection
    that an operator should see.
    """
    violations: list[str] = []
    if len(body) < MIN_CONTENT_HTML_CHARS:
        violations.append(
            f"content_html is {len(body)} chars, minimum is {MIN_CONTENT_HTML_CHARS}"
        )
    if _H2_RE.search(body) is None:
        violations.append("content_html carries no <h2> subheading")
    if _P_RE.search(body) is None:
        violations.append("content_html carries no <p> paragraph")
    forbidden = _FORBIDDEN_TAG_RE.search(body)
    if forbidden is not None:
        violations.append(
            f"content_html carries the forbidden tag {forbidden.group(0).strip()!r}"
        )
    if _INLINE_ATTR_RE.search(body) is not None:
        violations.append("content_html carries an inline style or event handler")
    leaked = _LEAKED_FIELD_RE.search(body)
    if leaked is not None:
        violations.append(
            f"content_html carries the leaked response field "
            f"{leaked.group('field')!r}"
        )
    return violations


def validate_generation(
    payload: Mapping[str, Any],
    *,
    expected_category: str,
    overrides: Mapping[str, Any] | None = None,
) -> GeneratedArticle:
    """Validate one parsed reply against the article contract.

    Enforces what the database and the publishers require, in this order:
    required fields present and non-empty, character limits (``title`` and
    ``seo_title`` 70, ``seo_description`` 160), keyword count within
    3–6, ``content_html`` long enough and structured with ``<h2>`` and ``<p>``,
    no forbidden tags or inline handlers, and a ``category_slug`` that resolves.

    ``category_slug`` is **corrected rather than rejected** when the model
    returns an unknown value: the article itself may be perfectly good, and the
    caller already knows which category it asked for. Every other violation
    raises :class:`SchemaViolationError`.

    ``overrides`` carries dashboard-edited rubric prompts so custom rubrics are
    accepted as valid rather than falling back to the default.
    """
    violations: list[str] = []

    values = {name: _as_text(payload.get(name)) for name in REQUIRED_FIELDS}
    keywords = _as_keywords(payload.get("seo_keywords"))

    for name in REQUIRED_FIELDS:
        if name == "seo_keywords":
            continue
        if not values[name]:
            violations.append(f"{name} is missing or empty")

    _check_length("title", values["title"], MAX_TITLE_CHARS, violations)
    _check_length("seo_title", values["seo_title"], MAX_TITLE_CHARS, violations)
    _check_length(
        "seo_description",
        values["seo_description"],
        MAX_SEO_DESCRIPTION_CHARS,
        violations,
    )

    if not MIN_SEO_KEYWORDS <= len(keywords) <= MAX_SEO_KEYWORDS:
        violations.append(
            f"seo_keywords carries {len(keywords)} item(s), expected "
            f"{MIN_SEO_KEYWORDS}-{MAX_SEO_KEYWORDS}"
        )
    for keyword in keywords:
        if len(keyword) > MAX_KEYWORD_CHARS:
            violations.append(
                f"keyword {keyword[:32]!r} is {len(keyword)} chars, limit is "
                f"{MAX_KEYWORD_CHARS}"
            )

    body = values["content_html"]
    if body:
        violations.extend(_body_violations(body))

    if violations:
        raise SchemaViolationError(violations)

    resolved = get_category(values["category_slug"], overrides)
    if resolved.slug != values["category_slug"]:
        logger.info(
            "Model returned unknown category_slug %r; filing under %r",
            values["category_slug"][:64],
            expected_category,
        )
        resolved = get_category(expected_category, overrides)

    return GeneratedArticle(
        title=values["title"],
        content_html=body,
        seo_title=values["seo_title"],
        seo_description=values["seo_description"],
        seo_keywords=keywords,
        category_slug=resolved.slug,
        image_prompt=values["image_prompt"],
    )


def _correction_message(reasons: Sequence[str]) -> dict[str, str]:
    """Build the retry's corrective user message.

    Naming the exact defects is what makes the second attempt worth its tokens;
    a bare "try again" reproduces the same draft.
    """
    listed = "\n".join(f"- {reason}" for reason in reasons)
    return {
        "role": "user",
        "content": (
            "Предыдущий вариант отклонён автоматической проверкой. "
            f"Причины:\n{listed}\n\n"
            "Перепишите материал заново, устранив каждую причину. Не объясняйте "
            "исправления — верните только исправленный JSON-объект того же "
            "формата."
        ),
    }


def _resolve_slop(report: SlopReport, body: str) -> tuple[str, bool, tuple[str, ...]]:
    """Decide what to do with a draft's slop findings.

    Returns ``(body, was_cleaned, notes)``. A draft with no fatal cliché is
    repaired in place when it needs it; a fatal finding is left for the caller to
    reject, because deleting a phrase that opens or closes a paragraph would
    leave mangled prose behind.
    """
    if report.is_valid and not report.soft:
        return body, False, ()
    if not report.fatal:
        cleaned = clean_ai_slop(body)
        recheck = analyze_slop(cleaned)
        if recheck.is_valid:
            return cleaned, True, tuple(recheck.reasons)
    return body, False, tuple(report.reasons)


async def _request_generation(
    client: AIClient,
    messages: Sequence[Mapping[str, str]],
    *,
    model: str,
) -> tuple[str, dict[str, Any] | None]:
    """Issue one chat completion for a full article.

    Uses :meth:`AIClient.complete_json_with_usage` rather than
    :meth:`AIClient.complete_json` because the caller needs the usage block to
    account spend. The shared client's retry, backoff and error typing still
    apply.
    """
    return await client.complete_json_with_usage(
        messages,
        model=model,
        max_tokens=GENERATION_MAX_TOKENS,
        temperature=GENERATION_TEMPERATURE,
        response_format=response_format(),
    )


async def generate_article(
    cluster_data: Mapping[str, Any],
    category: str,
    model_name: str | None = None,
    *,
    risk_level: str = "green",
    client: AIClient | None = None,
    overrides: Mapping[str, Any] | None = None,
    custom_style_prompt: str = "",
    promoted_project: PromotedProject | None = None,
    cluster_id: str = "",
) -> dict[str, Any]:
    """Generate one publishable article from a news cluster.

    ``cluster_data`` carries the cluster's ``title``, ``text``, extracted
    ``facts`` and source posts; ``category`` selects the editorial voice. The
    returned ``dict`` is the validated draft plus generation metadata
    (``attempts``, ``slop_cleaned``, ``slop_notes``), ready for the worker to
    persist.

    Raises :class:`GenerationRejectedError` when both attempts fail validation,
    and :class:`~app.services.ai_client.AIClientError` when the API itself is
    unreachable or unconfigured — the caller distinguishes "the model wrote
    badly" from "we could not reach the model", because only the first is worth
    reporting as a content problem.

    ``client`` is injectable so tests exercise the full retry logic against a
    scripted transport without a network. ``overrides`` carries dashboard-edited
    rubric prompts (``category_prompts`` rows) so a tuned voice applies without
    a redeploy.
    """
    engine = get_ai_client() if client is None else client
    model = model_name or settings.ai_model_writer
    editorial = get_category(category, overrides)
    messages: list[dict[str, str]] = build_messages(
        cluster_data,
        editorial.slug,
        risk_level=risk_level,
        overrides=overrides,
        custom_style_prompt=custom_style_prompt,
        promoted_project=promoted_project,
        cluster_id=cluster_id,
    )
    reasons: list[str] = []

    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        raw, usage = await _request_generation(engine, messages, model=model)
        try:
            draft = validate_generation(
                parse_generation(raw),
                expected_category=editorial.slug,
                overrides=overrides,
            )
        except SchemaViolationError as exc:
            reasons = list(exc.violations)
            logger.warning(
                "Generation attempt %d/%d violated the schema: %s",
                attempt,
                MAX_GENERATION_ATTEMPTS,
                exc,
            )
        else:
            report = analyze_slop(draft.content_html)
            body, cleaned, notes = _resolve_slop(report, draft.content_html)
            if report.is_valid or cleaned:
                if cleaned:
                    logger.info(
                        "Stripped %d repairable cliché(s) from a draft for %r",
                        len(report.soft),
                        editorial.slug,
                    )
                accepted = GeneratedArticle(
                    title=draft.title,
                    content_html=body,
                    seo_title=draft.seo_title,
                    seo_description=draft.seo_description,
                    seo_keywords=draft.seo_keywords,
                    category_slug=draft.category_slug,
                    image_prompt=draft.image_prompt,
                    attempts=attempt,
                    slop_cleaned=cleaned,
                    slop_notes=notes,
                )
                return _as_result(
                    accepted,
                    usage=usage,
                    promoted_project=promoted_project,
                    cluster_id=cluster_id,
                )
            reasons = [f"AI-клише в тексте: {phrase}" for phrase in report.reasons]
            logger.warning(
                "Generation attempt %d/%d rejected by the slop filter "
                "(density %.1f per 1000 words): %s",
                attempt,
                MAX_GENERATION_ATTEMPTS,
                report.density,
                ", ".join(report.reasons[:5]),
            )

        if attempt < MAX_GENERATION_ATTEMPTS:
            messages = [*messages, {"role": "assistant", "content": raw}]
            messages.append(_correction_message(reasons))

    raise GenerationRejectedError(
        f"No publishable draft after {MAX_GENERATION_ATTEMPTS} attempt(s) for "
        f"category {editorial.slug!r}",
        attempts=MAX_GENERATION_ATTEMPTS,
        reasons=reasons,
    )


def _as_result(
    article: GeneratedArticle,
    *,
    usage: dict[str, Any] | None = None,
    promoted_project: PromotedProject | None = None,
    cluster_id: str = "",
) -> dict[str, Any]:
    """Render the accepted draft as the plain dict the worker consumes."""
    result = {
        "title": article.title,
        "content_html": article.content_html,
        "seo_title": article.seo_title,
        "seo_description": article.seo_description,
        "seo_keywords": list(article.seo_keywords),
        "category_slug": article.category_slug,
        "image_prompt": article.image_prompt,
        "attempts": article.attempts,
        "slop_cleaned": article.slop_cleaned,
        "slop_notes": list(article.slop_notes),
    }
    if usage is not None:
        result["usage"] = usage
    if promoted_project is not None:
        result["promoted_project_id"] = promoted_project.id
        result["promoted_brand_name"] = promoted_project.brand_name
        result["tracked_url"] = promoted_project.build_tracked_url(cluster_id)
    return result
