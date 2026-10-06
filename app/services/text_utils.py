"""Zero-token text utilities: Telegram markup, normalisation and heuristics.

This module is stage 0 of the deduplication pipeline
(``docs/04_FILTER_AND_DEDUP.md``). It runs on *every* ingested post, is pure
CPU, performs no I/O and spends no AI tokens. Three responsibilities:

* turn Telegram ``entities`` offsets into safe, correctly nested HTML;
* normalise text for the MinHash shingler and for human-readable storage;
* reject digests and advertising with auditable, machine-readable rule names.

The ``REASON_*`` identifiers are a persisted contract: they are written to
``raw_news.filter_reasons`` (``ARRAY(String(64))``) and are the only way to
explain after the fact why a post left the pipeline, so they are stable
identifiers rather than free-form prose and must not be renamed casually.
"""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Final

# --------------------------------------------------------------------- reasons

#: Three or more consecutive line-leading numbered items ("1.", "2)", "3 -").
REASON_DIGEST_NUMBERED_LIST: Final[str] = "digest:numbered_list"
#: A summary headline such as "Главное к этому часу" / "Итоги дня".
REASON_DIGEST_HEADLINE: Final[str] = "digest:headline_phrase"
#: Four or more consecutive bullet lines sharing one marker.
REASON_DIGEST_BULLET_LIST: Final[str] = "digest:bullet_list"

#: Russian legal advertising marker ("erid:", "erid=", "ERID <token>").
REASON_AD_ERID: Final[str] = "ad:erid"
#: Explicit advertising footer / disclosure label.
REASON_AD_FOOTER: Final[str] = "ad:footer"
#: Promo code, either spelled out or an ALL-CAPS code next to a discount.
REASON_AD_PROMO_CODE: Final[str] = "ad:promo_code"
#: Referral or bot deep link ("t.me/bot?start=", "t.me/+invite").
REASON_AD_REFERRAL_LINK: Final[str] = "ad:referral_link"
#: Link built with a URL shortener, used to hide the real ad destination.
REASON_AD_SHORTENER: Final[str] = "ad:shortener"
#: Short post consisting mostly of links.
REASON_AD_LINK_DENSITY: Final[str] = "ad:link_density"

#: Human-readable descriptions for dashboard inspection and operator review.
FILTER_REASON_LABELS: Final[dict[str, str]] = {
    REASON_AD_ERID: "Маркировка рекламы (erid)",
    REASON_AD_FOOTER: "Рекламный блок / метка рекламы",
    REASON_AD_PROMO_CODE: "Промокод / рекламная скидка",
    REASON_AD_REFERRAL_LINK: "Реферальная ссылка / инвайт",
    REASON_AD_SHORTENER: "Сокращатель ссылок (редирект)",
    REASON_AD_LINK_DENSITY: "Много ссылок в коротком тексте",
    REASON_DIGEST_NUMBERED_LIST: "Нумерованный дайджест",
    REASON_DIGEST_BULLET_LIST: "Список пунктов / дайджест",
    REASON_DIGEST_HEADLINE: "Заголовок дайджеста («Главное за день»)",
}


def describe_filter_reason(reason: str) -> str:
    """Return an operator-facing label for a filter rejection reason."""
    return FILTER_REASON_LABELS.get(reason, reason)


# ------------------------------------------------------------------ thresholds

#: Consecutive numbered items required to call a post a digest.
DIGEST_MIN_NUMBERED_ITEMS: Final[int] = 3
#: Consecutive bullet items required to call a post a digest. Higher than the
#: numbered threshold because a dash also opens Russian direct speech.
DIGEST_MIN_BULLET_ITEMS: Final[int] = 4
#: Link count from which a *short* post is treated as link spam.
LINK_SPAM_MIN_LINKS: Final[int] = 5
#: Upper text length for the link-density rule; long articles legitimately
#: carry many links, only short link dumps are advertising.
LINK_SPAM_MAX_CHARS: Final[int] = 400
#: Characters scanned around a discount word for an ALL-CAPS promo code.
PROMO_CODE_WINDOW: Final[int] = 120

# ------------------------------------------------------------ letter folding

#: Cyrillic YO is used interchangeably with IE in Russian copy, so the two are
#: folded together before matching and hashing. Written as escapes because a
#: bare single Cyrillic letter is visually ambiguous with its latin lookalike.
_YO_FOLD: Final[Mapping[int, str]] = str.maketrans(
    {"\u0451": "\u0435", "\u0401": "\u0415"}
)

# --------------------------------------------------------------- UTF-16 layer

#: Telegram offsets are UTF-16 code unit indexes, so all splicing happens on
#: this encoding. Little endian is used because it has no BOM.
_UTF16: Final[str] = "utf-16-le"

# ------------------------------------------------------------------- entities

#: Entity types that map to a fixed tag pair.
_SIMPLE_TAGS: Final[Mapping[str, tuple[str, str]]] = {
    "bold": ("<b>", "</b>"),
    "italic": ("<i>", "</i>"),
    "underline": ("<u>", "</u>"),
    "strikethrough": ("<s>", "</s>"),
    "spoiler": ('<span class="tg-spoiler">', "</span>"),
    "code": ("<code>", "</code>"),
    "blockquote": ("<blockquote>", "</blockquote>"),
    "expandable_blockquote": ("<blockquote>", "</blockquote>"),
}

#: Entity types that carry meaning for Telegram but no markup for us: the text
#: is emitted verbatim (escaped) with no wrapping tag.
_PLAIN_TYPES: Final[frozenset[str]] = frozenset(
    {
        "mention",
        "text_mention",
        "hashtag",
        "cashtag",
        "bot_command",
        "email",
        "phone_number",
        "custom_emoji",
    }
)

#: Spelling differences between the Bot API (``text_link``) and the MTProto
#: class names GramJS emits (``MessageEntityTextUrl``).
_TYPE_ALIASES: Final[Mapping[str, str]] = {
    "text_url": "text_link",
    "strike": "strikethrough",
    "mention_name": "text_mention",
    "pre_code": "pre",
}

#: Only these schemes may reach an ``href``; anything else (``javascript:``,
#: ``data:``) is rendered as plain text instead of a link.
_SAFE_HREF_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:https?://|tg://|mailto:)", re.IGNORECASE
)
#: Scheme-less link text ("example.com", "www.example.com") that may be
#: upgraded to https before being used as an ``href``.
_BARE_DOMAIN_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:www\.)?[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?:[/?#].*)?$", re.IGNORECASE
)
#: Allowed characters in a ``<pre>`` language hint, to keep the class attribute
#: free of anything that could break out of the attribute.
_LANGUAGE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_+#.-]{1,32}$")
_CAMEL_BOUNDARY_RE: Final[re.Pattern[str]] = re.compile(r"(?<!^)(?=[A-Z])")

# ------------------------------------------------------------ html and text

_SCRIPT_STYLE_RE: Final[re.Pattern[str]] = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_BLOCK_BREAK_RE: Final[re.Pattern[str]] = re.compile(
    r"<\s*(?:br\s*/?|hr\s*/?|/\s*(?:p|div|li|tr|blockquote|pre|h[1-6]"
    r"|figure|figcaption|section|article|ul|ol|table)\s*)>",
    re.IGNORECASE,
)
_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]*>", re.DOTALL)
_HREF_RE: Final[re.Pattern[str]] = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.IGNORECASE
)
_HORIZONTAL_WS_RE: Final[re.Pattern[str]] = re.compile(r"[^\S\n]+")
_BLANK_RUN_RE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")

_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"https?://[^\s<>\"'`()\[\]{}«»]+", re.IGNORECASE
)
#: Trailing characters that belong to the sentence, not to the URL.
_URL_TRAILING: Final[str] = ".,;:!?…»)\"'*_-"
_HANDLE_RE: Final[re.Pattern[str]] = re.compile(r"[@#]\w+")
#: Everything that is not a letter or a digit: punctuation, emoji, symbols and
#: whitespace collapse to a single space for hashing.
_NON_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[\W_]+")

# ------------------------------------------------------------- signature lines

#: A line that is only decoration (emoji separators, dashes, arrows) carries no
#: text and is dropped before the signature test so "👉 Подписаться" and
#: "Подписаться" behave identically.
_DECOR_ONLY_RE: Final[re.Pattern[str]] = re.compile(r"^[^\w\s]+$")
#: Channel signatures and service footers. Anchored at both ends: only a line
#: that consists *entirely* of a footer is removed, so a sentence mentioning
#: "источник" mid-text survives.
_SIGNATURE_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[^\w\s]{0,8}\s*"
    r"(?:"
    r"@[a-z0-9_]{3,}"
    r"|(?:https?://)?t\.me/\S+"
    r"|(?:подписаться|подпишись|подпишитесь|подписывайся|подписывайтесь"
    r"|подписка)\b.*"
    r"|(?:наши|наше|наша|наш)\s+(?:канал|каналы|тг|телеграм|соцсети|сайт)\b.*"
    r"|(?:читать|читайте|подробнее)\s+(?:далее|полностью|здесь|тут"
    r"|в\s+источнике|на\s+сайте)\b.*"
    r"|источник\s*[:\-\u2013\u2014].*"
    r"|(?:фото|видео|иллюстрация|инфографика)\s*[:\-\u2013\u2014].*"
    r"|прислать\s+новость.*"
    r"|больше\s+новостей.*"
    r"|(?:наш|мой)\s+бот\b.*"
    r")"
    r"\s*[^\w\s]{0,8}$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------- digest rules

_NUMBERED_ITEM_RE: Final[re.Pattern[str]] = re.compile(
    r"^[\s>*•▪🔹\-\u2013\u2014]{0,4}(?P<num>\d{1,2})"
    r"(?:\ufe0f?\u20e3|\s*[.)\]:\-\u2013\u2014])\s+\S"
)
#: Bullet markers seen in Russian Telegram digests (excluding ⚡️ for breaking news).
_BULLET_ITEM_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s{0,4}(?P<mark>[•▪▫◦‣·○●◾◽\u2796➤►▶✅☑✔🔹🔸🔺🔻—\u2013-])\s+\S"
)
_DASH_MARKS: Final[frozenset[str]] = frozenset({"-", "\u2013", "\u2014"})
_DIGEST_HEADLINE_RE: Final[re.Pattern[str]] = re.compile(
    r"главное к этому часу"
    r"|главные новости"
    r"|главное за\b"
    r"|новости к этому часу"
    r"|итоги дня"
    r"|итоги недели"
    r"|дайджест"
    # The standalone preposition is escaped: a bare single Cyrillic letter is
    # indistinguishable from its latin lookalike in source.
    r"|коротко \u043e главном"
    r"|что произошло за\b"
)

# -------------------------------------------------------------------- ad rules

_AD_ERID_RE: Final[re.Pattern[str]] = re.compile(
    r"\berid\b\s*[:=]?\s*[A-Za-z0-9_-]{4,}", re.IGNORECASE
)
#: Unambiguous disclosure wording. The bare word "реклама" is deliberately
#: absent: see :func:`_has_standalone_ad_label`.
_AD_FOOTER_RE: Final[re.Pattern[str]] = re.compile(
    r"на правах рекламы"
    r"|рекламодател"
    r"|партн[её]рский материал"
    r"|партн[её]рский пост"
    r"|#промо\b"
    r"|#реклама\b"
    r"|спонсор"
    r"|промокод",
    re.IGNORECASE,
)
#: The bare word "реклама" as a *label*: at the start of its own line and
#: followed by end-of-line or punctuation. This is what a disclosure looks like
#: ("Реклама." or "Реклама." followed by the advertiser's legal details) and
#: it never matches a news sentence like "Реклама на телевидении подорожала",
#: where the word is followed by a space and more prose, nor its inflected
#: forms ("рекламе", "рекламы").
_AD_LABEL_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[^\w\s]{0,8}\s*реклама\s*(?:[.:,!|\-\u2013\u2014].*)?$", re.IGNORECASE
)
#: Same word appended to the very end of the text after a separator.
_AD_LABEL_TAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[\n|·•—\u2013\-])\s*реклама\s*[.!]?\s*$", re.IGNORECASE
)
_AD_PROMO_RE: Final[re.Pattern[str]] = re.compile(
    r"промокод\w*\s*[:\-\u2013\u2014]?\s*[«\"']?[A-Za-z0-9]{3,}"
    r"|по\s+(?:коду|кодом)\s+[«\"']?[A-Za-z0-9]{3,}",
    re.IGNORECASE,
)
#: Matches commercial discounts/sales/coupons without triggering on tax/rate news.
_DISCOUNT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:скидк\w*|распродаж\w*|дисконт\w*|купон\w*|выгод\w*|акци[яией]\b)",
    re.IGNORECASE,
)
#: ALL-CAPS latin token long enough to be a promo code rather than an acronym.
_CAPS_CODE_RE: Final[re.Pattern[str]] = re.compile(r"\b[A-Z][A-Z0-9]{3,19}\b")
_AD_REFERRAL_RE: Final[re.Pattern[str]] = re.compile(
    r"t\.me/\w+\?start=|t\.me/\+|\?(?:ref|refid|referral|utm_source=telegram_ad)=",
    re.IGNORECASE,
)
_AD_SHORTENER_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:bit\.ly|clck\.ru|vk\.cc|tinyurl\.com|goo\.gl|is\.gd|cutt\.ly"
    r"|tiny\.cc|t\.co|lnk\.to|surl\.li|u\.to)/",
    re.IGNORECASE,
)

#: Frequent latin acronyms and media/channel brands in Russian news copy.
#: Without this guard a sentence mentioning SHOT, TASS, or NASA would look
#: like an ALL-CAPS promo code next to a discount marker.
_CAPS_CODE_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "AUTO",
        "AVIA",
        "BASE",
        "BASH",
        "BRICS",
        "BULL",
        "COVID",
        "EBITDA",
        "FIFA",
        "GDPR",
        "HTML",
        "HTTP",
        "HTTPS",
        "IAEA",
        "INFO",
        "IPHONE",
        "IPTV",
        "LIFE",
        "LIVE",
        "MASH",
        "MOSCOW",
        "MSCI",
        "NASA",
        "NATO",
        "NEWS",
        "OPEC",
        "OSCE",
        "POST",
        "PRESS",
        "RTRS",
        "RTVI",
        "RUSSIA",
        "SHOT",
        "STAR",
        "SWIFT",
        "TASS",
        "TECH",
        "UEFA",
        "USSR",
        "WADA",
        "WHO",
        "WORLD",
    }
)


@dataclass(frozen=True, slots=True)
class FilterResult:
    """Immutable outcome of the 0-token heuristic filter.

    Consumed by the deduplication worker, which maps :attr:`should_skip` onto
    ``DedupStatus.FILTERED_DIGEST`` / ``DedupStatus.FILTERED_AD`` and persists
    :attr:`reasons` on ``raw_news.filter_reasons`` for auditing.
    """

    clean_text: str
    clean_html: str
    is_digest: bool
    is_ad: bool
    reasons: tuple[str, ...]
    link_count: int

    @property
    def should_skip(self) -> bool:
        """Whether the post must not enter the paid AI stages."""
        return self.is_digest or self.is_ad


@dataclass(frozen=True, slots=True)
class _Span:
    """A resolved entity: UTF-16 unit range plus the tags that wrap it."""

    start: int
    end: int
    open_tag: str
    close_tag: str
    verbatim: bool
    order: int


def _is_low_surrogate(data: bytes, unit: int) -> bool:
    """Whether UTF-16 unit ``unit`` of ``data`` is a trailing surrogate.

    A code point outside the BMP (emoji, flags, rare CJK) occupies two UTF-16
    units: a high surrogate followed by a low one. A boundary that lands on the
    low half would split the pair and produce an undecodable byte sequence, so
    callers snap such boundaries onto a whole character.
    """
    index = unit * 2
    if index + 1 >= len(data):
        return False
    code = data[index] | (data[index + 1] << 8)
    return 0xDC00 <= code <= 0xDFFF


def _slice_utf16(data: bytes, start: int, end: int) -> str:
    """Decode UTF-16 units ``[start, end)`` of ``data`` back to ``str``.

    Telegram reports ``offset``/``length`` in UTF-16 code units while Python
    indexes strings by code points, so slicing the ``str`` directly shifts every
    entity that follows an emoji. Splicing the ``utf-16-le`` encoding on 2-byte
    boundaries is therefore a correctness requirement, not an optimisation:
    Russian news channels put an emoji in nearly every post.

    ``errors="replace"`` is the last line of defence for a boundary that still
    splits a surrogate pair after snapping — a malformed source must degrade to
    a replacement character, never raise into the ingestion worker.
    """
    return data[start * 2 : end * 2].decode(_UTF16, errors="replace")


def _escape_segment(text: str, *, verbatim: bool) -> str:
    """Escape a text run, turning newlines into ``<br>`` outside code blocks.

    ``code``/``pre`` content keeps its literal newlines because whitespace is
    significant there and browsers already render it verbatim.
    """
    escaped = html_lib.escape(text, quote=False)
    if verbatim:
        return escaped
    return escaped.replace("\n", "<br>")


def _entity_type(entity: Mapping[str, Any]) -> str:
    """Return the normalised entity type name.

    The Bot API sends ``{"type": "text_link"}`` while GramJS forwards MTProto
    class names (``{"className": "MessageEntityTextUrl"}``); both reach this
    module depending on the ingestion path, so both spellings are folded onto
    the Bot API vocabulary.
    """
    raw = entity.get("type") or entity.get("className") or entity.get("_") or ""
    if not isinstance(raw, str):
        return ""
    name = raw.strip()
    if name.startswith("MessageEntity"):
        name = name[len("MessageEntity") :]
        name = _CAMEL_BOUNDARY_RE.sub("_", name)
    name = name.lower()
    return _TYPE_ALIASES.get(name, name)


def _entity_bounds(entity: Mapping[str, Any], total_units: int) -> tuple[int, int] | None:
    """Return the clamped ``(start, end)`` UTF-16 range, or ``None`` if unusable.

    Negative, non-numeric, zero-length and out-of-range values are dropped
    instead of raising: a single malformed entity must not cost the whole post.
    """
    try:
        offset = int(entity["offset"])
        length = int(entity["length"])
    except (KeyError, TypeError, ValueError):
        return None
    if offset < 0 or length <= 0 or offset >= total_units:
        return None
    return offset, min(offset + length, total_units)


def _safe_href(raw: str) -> str | None:
    """Return an escaped, scheme-checked ``href``, or ``None`` if unusable.

    Only http(s), ``tg://`` and ``mailto:`` links are emitted; a
    ``javascript:``/``data:`` payload from a hostile source is rejected so the
    text is rendered without an anchor instead of becoming an XSS vector.
    """
    candidate = raw.strip()
    if not candidate:
        return None
    if not _SAFE_HREF_RE.match(candidate):
        if not _BARE_DOMAIN_RE.match(candidate):
            return None
        candidate = f"https://{candidate}"
    return html_lib.escape(candidate, quote=True)


def _pre_tags(entity: Mapping[str, Any]) -> tuple[str, str]:
    """Build ``<pre>`` tags, honouring an optional syntax ``language`` hint."""
    language = entity.get("language")
    if isinstance(language, str) and _LANGUAGE_RE.match(language):
        return f'<pre><code class="language-{language}">', "</code></pre>"
    return "<pre>", "</pre>"


def _link_tags(
    entity: Mapping[str, Any], entity_type: str, data: bytes, start: int, end: int
) -> tuple[str, str] | None:
    """Build anchor tags for ``text_link``/``url``, or ``None`` when unsafe."""
    if entity_type == "url":
        raw_url = _slice_utf16(data, start, end)
    else:
        candidate = entity.get("url") or entity.get("href") or ""
        raw_url = candidate if isinstance(candidate, str) else ""
    href = _safe_href(raw_url)
    if href is None:
        return None
    return f'<a href="{href}">', "</a>"


def _build_span(
    entity: Mapping[str, Any], data: bytes, total_units: int, order: int
) -> _Span | None:
    """Resolve one entity into a :class:`_Span`, or ``None`` to leave it plain."""
    entity_type = _entity_type(entity)
    if not entity_type or entity_type in _PLAIN_TYPES:
        return None
    bounds = _entity_bounds(entity, total_units)
    if bounds is None:
        return None
    start, end = bounds

    if entity_type == "pre":
        tags: tuple[str, str] | None = _pre_tags(entity)
    elif entity_type in {"text_link", "url"}:
        tags = _link_tags(entity, entity_type, data, start, end)
    else:
        tags = _SIMPLE_TAGS.get(entity_type)
    if tags is None:
        # Unknown or unsafe entity: emit the text without markup.
        return None

    open_tag, close_tag = tags
    verbatim = entity_type in {"pre", "code"}
    return _Span(start, end, open_tag, close_tag, verbatim, order)


def _snap(data: bytes, span: _Span) -> _Span:
    """Move a span's edges off the interior of a surrogate pair."""
    start = span.start
    end = span.end
    if _is_low_surrogate(data, start):
        start -= 1
    if _is_low_surrogate(data, end):
        end += 1
    if (start, end) == (span.start, span.end):
        return span
    return _Span(
        max(start, 0),
        min(end, len(data) // 2),
        span.open_tag,
        span.close_tag,
        span.verbatim,
        span.order,
    )


def _render_spans(data: bytes, spans: Sequence[_Span]) -> str:
    """Emit escaped text with correctly nested tags.

    Telegram allows entities to nest *and* to partially overlap, which plain
    "wrap each range" logic turns into crossing tags. The text is therefore cut
    at every entity edge, and a stack of currently open spans is unwound to the
    longest common prefix at each cut: nested ranges produce a single nesting,
    overlapping ranges are re-opened after the split, and the output is valid
    HTML either way. Outer-to-inner order is start ascending, then the longer
    range, then declaration order, so the widest entity is always outermost.
    """
    total_units = len(data) // 2
    edges = {0, total_units}
    for span in spans:
        edges.add(span.start)
        edges.add(span.end)
    ordered_edges = sorted(edges)
    ranked = sorted(spans, key=lambda span: (span.start, -span.end, span.order))

    parts: list[str] = []
    stack: list[_Span] = []
    for begin, stop in pairwise(ordered_edges):
        active = [span for span in ranked if span.start <= begin < span.end]
        shared = 0
        while (
            shared < len(stack)
            and shared < len(active)
            and stack[shared] is active[shared]
        ):
            shared += 1
        for span in reversed(stack[shared:]):
            parts.append(span.close_tag)
        del stack[shared:]
        for span in active[shared:]:
            parts.append(span.open_tag)
            stack.append(span)
        verbatim = any(span.verbatim for span in stack)
        parts.append(_escape_segment(_slice_utf16(data, begin, stop), verbatim=verbatim))
    for span in reversed(stack):
        parts.append(span.close_tag)
    return "".join(parts)


def telegram_entities_to_html(text: str, entities: Sequence[Mapping[str, Any]]) -> str:
    """Convert a Telegram message plus its ``entities`` array into safe HTML.

    Offsets are interpreted as UTF-16 code units (see :func:`_slice_utf16`),
    text runs are HTML-escaped so a source cannot inject markup, and unknown or
    malformed entities are dropped rather than raised on. An empty entity list
    yields the escaped plain text with newlines as ``<br>``.
    """
    if not text:
        return ""
    data = text.encode(_UTF16)
    total_units = len(data) // 2
    spans: list[_Span] = []
    for order, entity in enumerate(entities):
        span = _build_span(entity, data, total_units, order)
        if span is not None and span.end > span.start:
            spans.append(_snap(data, span))
    if not spans:
        return _escape_segment(text, verbatim=False)
    return _render_spans(data, spans)


def html_to_plain_text(html: str) -> str:
    """Reduce HTML to readable plain text.

    ``<br>`` and closing block tags become newlines so paragraph structure
    survives, ``<script>``/``<style>`` bodies are dropped entirely, entities are
    unescaped *after* tag removal (unescaping first would resurrect an escaped
    ``&lt;script&gt;`` as a live tag), and runs of whitespace collapse.
    """
    if not html:
        return ""
    without_code = _SCRIPT_STYLE_RE.sub(" ", html)
    with_breaks = _BLOCK_BREAK_RE.sub("\n", without_code)
    stripped = _TAG_RE.sub("", with_breaks)
    return _collapse_plain(html_lib.unescape(stripped))


def _collapse_plain(text: str) -> str:
    """Trim each line, collapse horizontal whitespace and blank-line runs."""
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    normalised = normalised.replace("\u00a0", " ")
    lines = (_HORIZONTAL_WS_RE.sub(" ", line).strip() for line in normalised.split("\n"))
    return _BLANK_RUN_RE.sub("\n\n", "\n".join(lines)).strip()


def strip_signatures(text: str) -> str:
    """Drop channel signatures, subscribe calls and decoration-only lines.

    Only lines that consist *entirely* of a footer pattern are removed, so a
    sentence that merely contains "источник" or a handle keeps its content.
    """
    kept = [
        line
        for line in text.split("\n")
        if not _SIGNATURE_LINE_RE.match(line.strip())
        and not _DECOR_ONLY_RE.match(line.strip())
    ]
    return _collapse_plain("\n".join(kept))


def extract_urls(text: str) -> list[str]:
    """Return every http/https URL, de-duplicated with first-seen order kept."""
    seen: dict[str, None] = {}
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(_URL_TRAILING)
        if url:
            seen.setdefault(url, None)
    return list(seen)


def normalize_for_hashing(text: str) -> str:
    """Normalise text into the stable form the MinHash shingler consumes.

    Lowercases, folds Cyrillic YO onto IE, removes signatures, URLs and handles,
    then replaces every non-alphanumeric run with a single space. It must be
    deterministic: an unstable normalisation would change a post's shingles
    between runs and silently break duplicate detection.
    """
    if not text:
        return ""
    lowered = strip_signatures(text).lower().translate(_YO_FOLD)
    without_urls = _URL_RE.sub(" ", lowered)
    without_handles = _HANDLE_RE.sub(" ", without_urls)
    return _NON_WORD_RE.sub(" ", without_handles).strip()


def _leading_numbers(lines: Sequence[str]) -> list[int]:
    """Return the numbers of every line that opens a numbered list item."""
    numbers: list[int] = []
    for line in lines:
        match = _NUMBERED_ITEM_RE.match(line)
        if match is not None:
            numbers.append(int(match.group("num")))
    return numbers


def _longest_ascending_run(numbers: Sequence[int]) -> int:
    """Length of the longest ``n, n+1, n+2 …`` run in document order.

    Requiring consecutive numbering separates a real digest from prose that
    happens to start a line with a figure (a price list, a date, a quoted
    clause), which never counts up by one across several lines.
    """
    best = 0
    run = 0
    previous: int | None = None
    for number in numbers:
        run = run + 1 if previous is not None and number == previous + 1 else 1
        previous = number
        best = max(best, run)
    return best


def _bullet_class(mark: str) -> str:
    """Group bullet markers so a run is not broken by a cosmetic variation."""
    return "dash" if mark in _DASH_MARKS else "symbol"


def _longest_bullet_run(lines: Sequence[str]) -> int:
    """Length of the longest run of bullet lines sharing one marker class.

    Blank lines continue a run (digests are often double-spaced) while any line
    of prose ends it, so an article with one stray dash-led line cannot reach
    the threshold.
    """
    best = 0
    run = 0
    current = ""
    for line in lines:
        if not line.strip():
            continue
        match = _BULLET_ITEM_RE.match(line)
        if match is None:
            run = 0
            current = ""
            continue
        marker = _bullet_class(match.group("mark"))
        run = run + 1 if marker == current else 1
        current = marker
        best = max(best, run)
    return best


def detect_digest(text: str) -> tuple[bool, tuple[str, ...]]:
    """Detect a summary/digest post. Pure heuristics, 0 tokens.

    A digest bundles several unrelated events, so it is useless as a news hook
    and it poisons deduplication by matching many unrelated clusters.
    """
    if not text.strip():
        return False, ()
    lines = text.replace("\r\n", "\n").split("\n")
    reasons: list[str] = []
    if _longest_ascending_run(_leading_numbers(lines)) >= DIGEST_MIN_NUMBERED_ITEMS:
        reasons.append(REASON_DIGEST_NUMBERED_LIST)
    if _longest_bullet_run(lines) >= DIGEST_MIN_BULLET_ITEMS:
        reasons.append(REASON_DIGEST_BULLET_LIST)
    if _DIGEST_HEADLINE_RE.search(text.lower().translate(_YO_FOLD)):
        reasons.append(REASON_DIGEST_HEADLINE)
    return bool(reasons), tuple(reasons)


def _has_standalone_ad_label(text: str) -> bool:
    """Whether the bare word "реклама" appears as a disclosure label.

    The word itself is not evidence of advertising: Russian news writes about
    advertising all the time ("реклама на телевидении подорожала", "рынок
    рекламы").
    A disclosure, by contrast, is *structural* — it stands alone on its own
    line, or is appended after a separator at the very end of the post, or is
    written as the hashtag ``#реклама`` (handled by the footer pattern). Only
    those shapes are accepted here, which is what keeps the rule from
    swallowing ordinary news copy.
    """
    if _AD_LABEL_TAIL_RE.search(text):
        return True
    return any(_AD_LABEL_LINE_RE.match(line.strip()) for line in text.split("\n"))


def _has_caps_promo_code(text: str) -> bool:
    """Whether an ALL-CAPS latin code sits next to a discount marker.

    Neither signal alone is enough — acronyms and percentages are common in
    news — so both must appear within :data:`PROMO_CODE_WINDOW` characters.
    """
    for discount in _DISCOUNT_RE.finditer(text):
        begin = max(discount.start() - PROMO_CODE_WINDOW, 0)
        window = text[begin : discount.end() + PROMO_CODE_WINDOW]
        for code in _CAPS_CODE_RE.finditer(window):
            if code.group(0) not in _CAPS_CODE_STOPWORDS:
                return True
    return False


def detect_ad(text: str, *, link_count: int) -> tuple[bool, tuple[str, ...]]:
    """Detect advertising, PR and link spam. Pure heuristics, 0 tokens."""
    if not text.strip():
        return False, ()
    reasons: list[str] = []
    if _AD_ERID_RE.search(text):
        reasons.append(REASON_AD_ERID)
    if _AD_FOOTER_RE.search(text) or _has_standalone_ad_label(text):
        reasons.append(REASON_AD_FOOTER)
    if _AD_PROMO_RE.search(text) or _has_caps_promo_code(text):
        reasons.append(REASON_AD_PROMO_CODE)
    if _AD_REFERRAL_RE.search(text):
        reasons.append(REASON_AD_REFERRAL_LINK)
    if _AD_SHORTENER_RE.search(text):
        reasons.append(REASON_AD_SHORTENER)
    if link_count >= LINK_SPAM_MIN_LINKS and len(text) < LINK_SPAM_MAX_CHARS:
        reasons.append(REASON_AD_LINK_DENSITY)
    return bool(reasons), tuple(reasons)


def _html_hrefs(html: str) -> Iterable[str]:
    """Yield the ``href`` values of every anchor in ``html``."""
    for match in _HREF_RE.finditer(html):
        href = match.group(1) or match.group(2) or match.group(3) or ""
        unescaped = html_lib.unescape(href).strip()
        if unescaped:
            yield unescaped


def count_links(text: str, html: str) -> int:
    """Count distinct links across the plain text and the rendered HTML.

    Telegram hides the target of a ``text_link`` behind the anchor text, so a
    text-only count would report zero links for a post built entirely of them.
    """
    links: dict[str, None] = dict.fromkeys(extract_urls(text))
    for href in _html_hrefs(html):
        if href.lower().startswith(("http://", "https://")):
            links.setdefault(href, None)
    return len(links)


def analyze_post(
    text: str,
    entities: Sequence[Mapping[str, Any]] | None = None,
    *,
    html: str | None = None,
) -> FilterResult:
    """Run the full 0-token heuristic filter over one ingested post.

    ``entities`` is the Telegram path (markup is rebuilt from offsets), ``html``
    is the web-crawler path (markup already exists and plain text is derived
    from it), and with neither the text is escaped as-is. Detection always runs
    on the human-readable plain text *before* signatures are stripped, because
    disclosures such as a trailing "Реклама" line live exactly there.
    """
    source_text = text or ""
    if entities:
        clean_html = telegram_entities_to_html(source_text, entities)
        plain = _collapse_plain(source_text)
    elif html:
        clean_html = html
        plain = html_to_plain_text(html)
    else:
        clean_html = _escape_segment(source_text, verbatim=False)
        plain = _collapse_plain(source_text)

    link_count = count_links(plain, clean_html)
    is_digest, digest_reasons = detect_digest(plain)
    is_ad, ad_reasons = detect_ad(plain, link_count=link_count)
    reasons = tuple(sorted(set(digest_reasons) | set(ad_reasons)))

    return FilterResult(
        clean_text=strip_signatures(plain),
        clean_html=clean_html,
        is_digest=is_digest,
        is_ad=is_ad,
        reasons=reasons,
        link_count=link_count,
    )


__all__ = [
    "DIGEST_MIN_BULLET_ITEMS",
    "DIGEST_MIN_NUMBERED_ITEMS",
    "FILTER_REASON_LABELS",
    "LINK_SPAM_MAX_CHARS",
    "LINK_SPAM_MIN_LINKS",
    "REASON_AD_ERID",
    "REASON_AD_FOOTER",
    "REASON_AD_LINK_DENSITY",
    "REASON_AD_PROMO_CODE",
    "REASON_AD_REFERRAL_LINK",
    "REASON_AD_SHORTENER",
    "REASON_DIGEST_BULLET_LIST",
    "REASON_DIGEST_HEADLINE",
    "REASON_DIGEST_NUMBERED_LIST",
    "FilterResult",
    "analyze_post",
    "count_links",
    "describe_filter_reason",
    "detect_ad",
    "detect_digest",
    "extract_urls",
    "html_to_plain_text",
    "normalize_for_hashing",
    "strip_signatures",
    "telegram_entities_to_html",
]
