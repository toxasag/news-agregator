"""Anti-slop validator: the gate that keeps AI clichés out of published copy.

``AGENTS.md`` states the rule plainly — every generated text must satisfy the
``ai-slop-blocklist`` *before* it reaches the database. This module is that
check, and it is deliberately strict: a reader recognises machine-written filler
instantly, and one "В современном мире" in a lead destroys the credibility of an
otherwise correct article.

Three design decisions carry most of the weight:

* **Patterns, not literals.** Russian inflects, so a blocklist of exact strings
  is trivially evaded by the model's own grammar: "является ключевым фактором",
  "является ключевым фактором развития", "являются ключевыми факторами" are one
  cliché in three shapes. Every entry is therefore a regex over a normalised
  form of the text, with the inflecting tail expressed as a character class.

* **Severity, not a flat list.** A phrase is either *banned* (``FATAL``) or
  *repairable* (``SOFT``). ``в современном мире`` is pure filler and can be cut
  without touching the facts; ``стоит отметить, что`` is a hedge whose removal
  leaves a valid sentence. Only ``SOFT`` findings are rewritten by
  :func:`clean_ai_slop`; a ``FATAL`` finding means the draft is rejected and
  regenerated, because silently deleting a phrase that carried structure would
  leave a mangled paragraph behind.

* **HTML-aware but markup-preserving.** Detection runs on text extracted from
  the markup, so a cliché split across ``<b>`` tags is still caught. Cleaning,
  by contrast, edits the original string: rebuilding HTML from extracted text
  would discard the article's structure.

Scoring is reported per 1000 words rather than as a raw count, so a 300-word
note and a 2000-word longread are held to the same density. The caller decides
what to do with it; :func:`validate_ai_text` only reports.
"""

# The blocklist is Russian editorial copy, so Cyrillic letters that look like
# Latin ones are the subject matter, not a mistake; RUF001 would flag nearly
# every pattern in this module.
# ruff: noqa: RUF001, RUF002

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "BLOCKLIST",
    "FATAL_PATTERN_COUNT",
    "MAX_SLOP_PER_1000_WORDS",
    "SOFT_PATTERN_COUNT",
    "SlopFinding",
    "SlopReport",
    "SlopSeverity",
    "analyze_slop",
    "clean_ai_slop",
    "slop_density",
    "validate_ai_text",
]


class SlopSeverity(StrEnum):
    """How a blocklist hit must be handled.

    ``FATAL`` — reject the draft and regenerate. The phrase is either pure
    filler occupying a structural position (an opening or closing sentence) or a
    tell that the model is padding rather than reporting.

    ``SOFT`` — repairable in place. Removing it leaves a grammatical sentence
    that says the same thing, so :func:`clean_ai_slop` can strip it instead of
    paying for a second generation.
    """

    FATAL = "fatal"
    SOFT = "soft"


@dataclass(frozen=True, slots=True)
class _Rule:
    """One blocklist entry.

    ``replacement`` is the text substituted for a ``SOFT`` match. It is almost
    always ``""``; a few rules keep a word so the sentence retains its subject.
    """

    name: str
    pattern: re.Pattern[str]
    severity: SlopSeverity
    replacement: str = ""


@dataclass(frozen=True, slots=True)
class SlopFinding:
    """One detected cliché, with enough context to be actionable.

    ``matched`` is the exact substring found, so an operator reading the
    dashboard sees the offending words rather than a rule id.
    """

    rule: str
    severity: SlopSeverity
    matched: str
    position: int


@dataclass(frozen=True, slots=True)
class SlopReport:
    """Full outcome of one validation pass."""

    findings: tuple[SlopFinding, ...]
    word_count: int

    @property
    def fatal(self) -> tuple[SlopFinding, ...]:
        """Findings that force a regeneration."""
        return tuple(
            item for item in self.findings if item.severity is SlopSeverity.FATAL
        )

    @property
    def soft(self) -> tuple[SlopFinding, ...]:
        """Findings :func:`clean_ai_slop` can repair in place."""
        return tuple(
            item for item in self.findings if item.severity is SlopSeverity.SOFT
        )

    @property
    def density(self) -> float:
        """Findings per 1000 words.

        Normalised by length so the threshold means the same thing for a short
        news note and a longread.
        """
        if self.word_count <= 0:
            return 0.0 if not self.findings else float("inf")
        return len(self.findings) * 1000 / self.word_count

    @property
    def is_valid(self) -> bool:
        """Whether the text may be stored as-is.

        A single ``FATAL`` hit invalidates the draft outright. ``SOFT`` hits are
        tolerated up to :data:`MAX_SLOP_PER_1000_WORDS`, above which the text is
        padding held together by hedges and is cheaper to regenerate than to
        repair.
        """
        if self.fatal:
            return False
        return self.density <= MAX_SLOP_PER_1000_WORDS

    @property
    def reasons(self) -> list[str]:
        """Detected phrases, de-duplicated, in order of first appearance.

        This is the ``list[str]`` half of the :func:`validate_ai_text` contract:
        the matched text, not the rule name, because that is what a human needs
        in order to judge the verdict.
        """
        return list(dict.fromkeys(item.matched for item in self.findings))


#: Soft-hit budget per 1000 words. Two hedges in a 1000-word article is a
#: stylistic tic; five is a draft made of connective tissue.
MAX_SLOP_PER_1000_WORDS: Final[float] = 4.0

# --------------------------------------------------------------- normalisation

#: Cyrillic YO folded onto IE: models emit both spellings interchangeably, and a
#: blocklist that distinguishes them would miss half the hits.
_YO_FOLD: Final[dict[int, str]] = {0x0401: "\u0415", 0x0451: "\u0435"}

#: Typographic variants a model produces freely. Normalised so one pattern
#: covers every dash and quote style.
_PUNCT_FOLD: Final[dict[int, str]] = {
    0x2010: "-",
    0x2011: "-",
    0x2012: "-",
    0x2013: "-",
    0x2014: "-",
    0x2015: "-",
    0x00AB: '"',
    0x00BB: '"',
    0x201C: '"',
    0x201D: '"',
    0x201E: '"',
    0x2018: "'",
    0x2019: "'",
    0x00A0: " ",
}

_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[^\W\d_]+", re.UNICODE)

#: Cyrillic inflecting tail, written as an escape range. A literal class mixing
#: Cyrillic and Latin letters trips ruff's ambiguous-character rule and is
#: genuinely hard to read.
_T: Final[str] = r"[\u0430-\u044f\u0451]*"


def _normalize(text: str) -> str:
    """Lowercase, fold letters and punctuation, collapse whitespace.

    Offsets in the result do **not** map back to the input, so findings report
    positions in normalised space. That is sufficient for reporting; cleaning
    works on the original string with its own case-insensitive patterns.
    """
    folded = text.translate(_YO_FOLD).translate(_PUNCT_FOLD).lower()
    return _WHITESPACE_RE.sub(" ", folded).strip()


def _strip_markup(text: str) -> str:
    """Replace tags with spaces so words never fuse across a tag boundary.

    ``<p>слово</p><p>другое</p>`` must not become ``словодругое``: the fused
    token would break word counting and could fabricate a false match.
    """
    return _TAG_RE.sub(" ", text)


def _count_words(text: str) -> int:
    """Count alphabetic words, ignoring digits and punctuation."""
    return len(_WORD_RE.findall(text))


def _rule(
    name: str,
    pattern: str,
    severity: SlopSeverity = SlopSeverity.FATAL,
    replacement: str = "",
) -> _Rule:
    """Compile one blocklist entry against the normalised text form."""
    return _Rule(
        name=name,
        pattern=re.compile(pattern, re.UNICODE),
        severity=severity,
        replacement=replacement,
    )


#: The blocklist itself.
#:
#: Grouped by the failure mode each entry represents, because that is what makes
#: the list maintainable: a new cliché belongs in an existing group or reveals a
#: group that is missing.
BLOCKLIST: Final[tuple[_Rule, ...]] = (
    # -- Openers that say nothing. A news article starts with the news; any
    # -- scene-setting preamble is the model warming up in public.
    _rule("slop:modern_world", rf"в (?:современном|нынешнем) мир{_T}"),
    _rule("slop:modern_realities", rf"в современн{_T} реали{_T}"),
    _rule("slop:era_of", rf"в эпох{_T} (?:цифров|тотальн|глобальн){_T}"),
    _rule("slop:no_secret", rf"не секрет,? что|всем известн{_T},? что"),
    _rule("slop:hard_to_overestimate", rf"(?:трудно|сложно) переоцен{_T}"),
    _rule("slop:increasingly_often", r"все чаще (?:можно|мы|говорят|звучат)"),
    # -- Tour-guide narration. The model addressing the reader about the
    # -- article's own structure instead of reporting.
    _rule("slop:lets_dive", rf"погруз{_T} в (?:детали|подробност|мир|тем){_T}"),
    _rule("slop:lets_look", rf"давайте (?:рассмотр|разбер|погруз|взглян){_T}"),
    _rule("slop:in_this_article", rf"в (?:эт{_T}|данн{_T}) (?:статье|материале)"),
    _rule("slop:we_will_tell", rf"(?:расскаж|разбер|поговор){_T} о том,? (?:как|что)"),
    # -- Closers. A news item ends on its last fact, not on a summary of itself.
    _rule("slop:summing_up", rf"подвод{_T} (?:итог|черт){_T}"),
    _rule("slop:in_conclusion", r"в заключение|в качестве заключения"),
    _rule("slop:time_will_tell", rf"врем{_T} покажет|остается (?:лишь )?ждать"),
    _rule("slop:one_thing_certain", r"одно (?:можно сказать )?(?:точно|наверняка)"),
    _rule("slop:thus_we_see", rf"так{_T} образом,? мы (?:вид|наблюда){_T}"),
    # -- Consultant register. Vocabulary from a slide deck, not a newsroom.
    _rule("slop:key_factor", rf"(?:явля{_T}|станов{_T}) ключев{_T} фактор{_T}"),
    _rule("slop:plays_key_role", rf"игра{_T} (?:ключев|важн|значим){_T} роль"),
    _rule("slop:integral_part", rf"неотъемлем{_T} часть{_T}"),
    _rule("slop:in_todays_pace", rf"в (?:современн|быстр){_T} темпе жизни"),
    _rule("slop:rapidly_changing", rf"быстро (?:меня{_T}|развива{_T})ся мир{_T}"),
    _rule("slop:digital_transformation", r"цифровая трансформация (?:всех|всего)"),
    # "вывело отрасль на новый уровень": the verb and the idiom's tail are
    # separated by the object, so a contiguous pattern would miss every real
    # sentence. Up to four intervening words are allowed — enough for a noun
    # phrase, short enough that two unrelated clauses do not match across a
    # comma. The verb stem changes across tenses (вывод-/вывел-/вывед-), hence
    # the alternation rather than one shared tail.
    _rule(
        "slop:new_level",
        rf"(?:вывод|вывел|вывед){_T}(?:\s+[^\s,.;:!?]+){{0,4}}\s+на нов{_T} уровень",
    ),
    _rule("slop:comprehensive", rf"комплексн{_T} подход{_T} к решению"),
    # -- English-language slop, for models that leak their training register.
    _rule("slop:en_delve", r"\bdelve into\b|\blet'?s dive\b"),
    _rule("slop:en_landscape", r"\b(?:ever[- ]changing|rapidly evolving) landscape\b"),
    _rule("slop:en_testament", r"\bis a testament to\b"),
    _rule("slop:en_in_conclusion", r"\bin conclusion\b|\bto sum up\b"),
    _rule("slop:en_it_is_worth", r"\bit is worth noting\b|\bit'?s important to note\b"),
    _rule("slop:en_navigate", r"\bnavigating the (?:complex|complexities)\b"),
    # -- Hedges. Repairable: cutting them leaves the sentence intact and the
    # -- claim unchanged, which is why these are SOFT. Declared longest-first so
    # -- the overlap guard in `_iter_findings` keeps the most specific match.
    _rule(
        "slop:worth_noting",
        r"стоит (?:отметить|заметить|подчеркнуть),? что ",
        SlopSeverity.SOFT,
    ),
    _rule(
        "slop:worth_noting_bare",
        r"стоит (?:отметить|заметить|подчеркнуть),? ",
        SlopSeverity.SOFT,
    ),
    _rule(
        "slop:it_should_be_said",
        r"следует (?:отметить|сказать|упомянуть),? ",
        SlopSeverity.SOFT,
    ),
    _rule(
        "slop:important_to_understand",
        r"важно (?:понимать|отметить),? что ",
        SlopSeverity.SOFT,
    ),
    _rule("slop:as_known", r"как (?:известно|мы знаем),? ", SlopSeverity.SOFT),
    _rule("slop:it_is_no_coincidence", r"неудивительно,? что ", SlopSeverity.SOFT),
    _rule("slop:in_fact", r"по (?:сути|факту) (?:говоря )?, ", SlopSeverity.SOFT),
    _rule(
        "slop:that_said",
        r"тем не менее,? стоит ",
        SlopSeverity.SOFT,
        "тем не менее, ",
    ),
    _rule("slop:obviously", r"очевидно,? что ", SlopSeverity.SOFT),
    _rule("slop:undoubtedly", r"(?:несомненно|безусловно),? что ", SlopSeverity.SOFT),
)

#: Split counts, exported so a test can assert the list did not silently shrink.
FATAL_PATTERN_COUNT: Final[int] = sum(
    1 for rule in BLOCKLIST if rule.severity is SlopSeverity.FATAL
)
SOFT_PATTERN_COUNT: Final[int] = sum(
    1 for rule in BLOCKLIST if rule.severity is SlopSeverity.SOFT
)


def _iter_findings(normalised: str) -> Iterator[SlopFinding]:
    """Yield every blocklist hit in ``normalised``, rule order preserved.

    Several rules are deliberately nested: ``стоит отметить, что`` and the bare
    ``стоит отметить,`` are separate entries because the longer form must be
    preferred when both apply (it consumes the subordinating ``что``, so the
    repair leaves a clean sentence). Reporting both would count one cliché
    twice and inflate the density that decides validity, so an occurrence whose
    span is already covered by an accepted finding is suppressed.

    Rules are declared longest-first within a group, which is what makes the
    first accepted match the most specific one.
    """
    claimed: list[tuple[int, int]] = []
    for rule in BLOCKLIST:
        for match in rule.pattern.finditer(normalised):
            matched = match.group(0).strip()
            if not matched:
                continue
            start, end = match.span()
            overlaps = any(
                start < prev_end and prev_start < end
                for prev_start, prev_end in claimed
            )
            if overlaps:
                continue
            claimed.append((start, end))
            yield SlopFinding(
                rule=rule.name,
                severity=rule.severity,
                matched=matched,
                position=start,
            )


def analyze_slop(text: str) -> SlopReport:
    """Run the full blocklist over ``text`` and return the detailed report.

    Accepts plain text or HTML: markup is stripped for detection so a cliché
    broken up by inline tags is still found. Findings are ordered by position so
    the first thing an operator sees is the first thing a reader would.
    """
    if not text or not text.strip():
        return SlopReport(findings=(), word_count=0)
    plain = _strip_markup(text)
    normalised = _normalize(plain)
    findings = sorted(_iter_findings(normalised), key=lambda item: item.position)
    return SlopReport(findings=tuple(findings), word_count=_count_words(plain))


def validate_ai_text(text: str) -> tuple[bool, list[str]]:
    """Validate one generated text against the blocklist.

    Returns ``(is_valid, detected)`` where ``detected`` lists the offending
    phrases as they appear in the text, de-duplicated and ordered by first
    appearance. ``is_valid`` is ``False`` when any fatal cliché is present or
    when the soft-hit density exceeds :data:`MAX_SLOP_PER_1000_WORDS`.

    An empty text is **not** valid: an empty article is a generation failure,
    and reporting it as clean would let it through to the database.
    """
    if not text or not text.strip():
        return False, ["empty_text"]
    report = analyze_slop(text)
    return report.is_valid, report.reasons


def slop_density(text: str) -> float:
    """Return the blocklist-hit density per 1000 words.

    Exposed for the generator's retry decision and for the dashboard, so
    "how bad is it" does not require re-deriving the report.
    """
    return analyze_slop(text).density


#: Punctuation left stranded when a hedge is cut from mid-sentence.
_ORPHAN_PUNCT_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[>\s])\s*[,;:]\s*")
#: Whitespace runs introduced by removal, inside a line.
_INLINE_WS_RE: Final[re.Pattern[str]] = re.compile(r"[ \t]{2,}")
#: A space before closing punctuation, e.g. ``слово .``
_SPACE_BEFORE_PUNCT_RE: Final[re.Pattern[str]] = re.compile(r"\s+([.,;:!?])")
#: An opening tag followed by punctuation, e.g. ``<p>, что`` after a cut.
_TAG_THEN_PUNCT_RE: Final[re.Pattern[str]] = re.compile(r"(>)\s*[,;:]\s*")


def _recase_after_cut(text: str) -> str:
    """Capitalise a sentence whose opening hedge was removed.

    Cutting "Стоит отметить, что " from the front of a sentence leaves it
    starting lowercase. Restoring the capital is what makes the repair
    invisible; leaving it would look like a different defect.
    """

    def fix(match: re.Match[str]) -> str:
        lead, letter = match.group(1), match.group(2)
        return f"{lead}{letter.upper()}"

    # After a block tag, or after sentence-final punctuation plus a space.
    text = re.sub(r"(<(?:p|li|h[1-6]|blockquote)>\s*)([^\W\d_])", fix, text)
    return re.sub(r"([.!?]\s+)([^\W\d_])", fix, text)


def clean_ai_slop(text: str) -> str:
    """Remove repairable AI filler, leaving the facts and markup untouched.

    Only ``SOFT`` rules are applied. A ``FATAL`` cliché is deliberately left in
    place: those phrases occupy structural positions (an opening line, a closing
    paragraph), so deleting one would leave a stub sentence or an empty
    paragraph, and the honest response is to regenerate the draft — which is
    what :func:`validate_ai_text` tells the caller to do.

    The edit is applied to the original string, not to the normalised form, so
    HTML structure, capitalisation and the author's punctuation survive. After
    removal the function tidies the seam: orphaned commas, doubled spaces, a
    space before a full stop, and the lowercase letter left at the start of a
    sentence whose hedge was cut.

    Idempotent: running it twice changes nothing the second time.
    """
    if not text or not text.strip():
        return ""

    cleaned = text
    for rule in BLOCKLIST:
        if rule.severity is not SlopSeverity.SOFT:
            continue
        # The stored patterns match the *normalised* form; for editing we need
        # them to match the original, so the same source is recompiled
        # case-insensitively. The patterns contain no case-sensitive constructs.
        editable = re.compile(rule.pattern.pattern, re.IGNORECASE | re.UNICODE)
        cleaned = editable.sub(rule.replacement, cleaned)

    cleaned = _TAG_THEN_PUNCT_RE.sub(r"\1", cleaned)
    cleaned = _ORPHAN_PUNCT_RE.sub(" ", cleaned)
    cleaned = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", cleaned)
    cleaned = _INLINE_WS_RE.sub(" ", cleaned)
    cleaned = _recase_after_cut(cleaned)
    return cleaned.strip()


def describe_blocklist() -> Sequence[tuple[str, str]]:
    """Return ``(rule_name, severity)`` pairs for the dashboard's rules panel."""
    return tuple((rule.name, rule.severity.value) for rule in BLOCKLIST)
