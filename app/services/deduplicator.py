"""Three-level, zero-cost-first deduplication engine (docs/04_FILTER_AND_DEDUP.md).

The threshold ladder, in the order a post walks it:

* **Level 1 — MinHash LSH, 0 tokens, target < 5 ms.** Word 3-gram shingles of the
  normalised text are reduced to a `minhash_num_perm`-wide signature, split into
  `lsh_bands` bands and looked up in Redis. `Jaccard >= lsh_duplicate_threshold`
  (0.85) is a duplicate; `< lsh_ambiguous_threshold` (0.60) is a unique event.
  Both verdicts are final and cost nothing.

  Level 1 deliberately never touches PostgreSQL. Candidate *signatures* are
  cached in Redis next to the bucket sets (`lsh:sig:{raw_news_id}`), so
  recomputing the true Jaccard estimate for every candidate stays inside two
  Redis round trips (one pipelined multi-band `SMEMBERS`, one `MGET`) and the
  latency budget holds even when a bucket is crowded. Signatures are still
  persisted to `raw_news.minhash_signature`, which is what lets the index be
  rebuilt from the database after a Redis flush.

* **Level 2 — semantic pgvector search.** Runs only inside the ambiguous band
  0.60 <= Jaccard < 0.85. Costs one embedding. Cosine similarity against
  `news_clusters.embedding` over the last `dedup_lookback_hours`:
  `< similarity_lower_threshold` (0.75) opens a new cluster,
  `>= similarity_upper_threshold` (0.92) attaches to the match, and the band in
  between asks the LLM arbiter one YES/NO question.

* **Level 3 — novelty check, 0 tokens.** Runs after levels 1-2 confirm a
  duplicate. Dates, quantities, money, percentages and multi-word named
  entities are extracted and compared against the cluster's accumulated
  `enrichment_data["facts"]`. No new facts -> `SKIPPED_DUPLICATE`; new facts ->
  `UPDATE` plus a `facts_delta` entry merged into the cluster.

**Conservative arbiter default.** An unparseable, disabled or failed arbiter is
resolved as *new cluster*, never as a merge. A spurious near-duplicate in the
feed is cheap; silently folding two distinct events into one cluster destroys a
news item and cannot be recovered downstream.

**Graceful degradation.** Without `OPENROUTER_API_KEY`, or when any AI call
raises, levels 2 and 3 are skipped instead of failing: the level 1 verdict
stands and an ambiguous post becomes a new cluster. The row is flagged with
`entities["dedup_degraded"] = True` so it can be re-checked later. The pipeline
therefore runs end to end at zero AI spend.

**No-commit contract.** :func:`deduplicate` mutates the ORM objects and may
`flush()` to obtain the new cluster's identity, but it never commits and never
rolls back. The calling worker owns the transaction boundary, so one failed post
cannot leave a half-applied dedup decision behind.

**Idempotency.** A row whose `dedup_status` is no longer `PENDING` is replayed
from its persisted state: no second cluster, no second Redis write, no second
`facts_delta` entry. Redis `SADD`/`SET` are idempotent by construction, and a
post excludes its own id from its candidate set.

`DedupStatus.DUPLICATE` is intentionally never emitted here: the spec's decision
table resolves every confirmed duplicate through level 3 into either
`SKIPPED_DUPLICATE` or `UPDATE`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import struct
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Final

from datasketch import MinHash
from redis.asyncio import Redis
from sqlalchemy import Select, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.config import settings
from app.models.cluster import NewsCluster
from app.models.enums import DedupStatus
from app.models.raw_news import RawNews
from app.redis_client import get_redis
from app.services.ai_client import AIClient, AIClientError, get_ai_client
from app.services.categories import DEFAULT_CATEGORY
from app.services.embedding_service import get_embedding_service
from app.services.text_utils import normalize_for_hashing

logger: Final[logging.Logger] = logging.getLogger(__name__)

#: Fixed MinHash seed. Signatures are persisted in `raw_news.minhash_signature`
#: and cached in Redis, so the seed is part of the stored format: changing it
#: invalidates every stored signature and must be treated as a migration.
MINHASH_SEED: Final[int] = 1

LSH_BUCKET_PREFIX: Final[str] = "lsh:bucket"
LSH_SIGNATURE_PREFIX: Final[str] = "lsh:sig"

#: Mask applied before packing a signature value into 8 bytes, so a value read
#: back from `ARRAY(BIGINT)` (a *signed* type) can never break band hashing.
_UINT64_MASK: Final[int] = 0xFFFF_FFFF_FFFF_FFFF

#: Clusters pulled from the HNSW index per semantic query. Only the closest one
#: decides the outcome; the rest are fetched for logging and future tie-breaks.
SEMANTIC_CANDIDATE_LIMIT: Final[int] = 5

#: Category assigned to a freshly opened cluster, before the moderator has
#: classified it.
#:
#: Must be a real rubric from `CATEGORY_MAP`, not a placeholder like "general":
#: deduplication opens the cluster, and until the moderator stage runs this value
#: is what the writer and the WordPress publisher see. A placeholder reaches
#: `wp_category_id` as the fallback term and files the article in the wrong
#: section — observed on a live run, where a cluster carried `general` all the way
#: into generation.
DEFAULT_CLUSTER_CATEGORY: Final[str] = DEFAULT_CATEGORY

MAX_TITLE_LENGTH: Final[int] = 200
#: Per-side budget for the arbiter prompt. The question is one word wide, so
#: keeping the context short is what makes level 2 cheap.
MAX_ARBITER_CHARS: Final[int] = 700

ENTITY_KEY_DEGRADED: Final[str] = "dedup_degraded"
ENTITY_KEY_MATCHED_CLUSTER: Final[str] = "dedup_matched_cluster_id"

FACTS_KEY: Final[str] = "facts"
FACTS_DELTA_KEY: Final[str] = "facts_delta"

REASON_ALREADY_DECIDED: Final[str] = "already_decided"
REASON_FILTERED_DIGEST: Final[str] = "filtered_digest"
REASON_FILTERED_AD: Final[str] = "filtered_ad"
REASON_EMPTY_TEXT: Final[str] = "empty_text"
REASON_L1_UNIQUE: Final[str] = "l1_below_ambiguous"
REASON_L1_DUPLICATE: Final[str] = "l1_jaccard_duplicate"
REASON_L1_CLUSTER_MISSING: Final[str] = "l1_matched_cluster_missing"
REASON_L2_NO_CANDIDATES: Final[str] = "l2_no_semantic_candidates"
REASON_L2_BELOW_LOWER: Final[str] = "l2_cosine_below_lower"
REASON_L2_ABOVE_UPPER: Final[str] = "l2_cosine_above_upper"
REASON_L2_ARBITER_SAME: Final[str] = "l2_arbiter_yes"
REASON_L2_ARBITER_DIFFERENT: Final[str] = "l2_arbiter_no"
REASON_L2_ARBITER_UNPARSEABLE: Final[str] = "l2_arbiter_unparseable"
REASON_L2_ARBITER_DISABLED: Final[str] = "l2_arbiter_disabled"
REASON_L2_DEGRADED: Final[str] = "l2_degraded_no_ai"
REASON_L3_NEW_FACTS: Final[str] = "l3_new_facts"
REASON_L3_NO_NEW_FACTS: Final[str] = "l3_no_new_facts"

FACT_DATE: Final[str] = "date"
FACT_TIME: Final[str] = "time"
FACT_WHEN: Final[str] = "when"
FACT_NUMBER: Final[str] = "num"
FACT_MONEY: Final[str] = "money"
FACT_PERCENT: Final[str] = "pct"
FACT_ENTITY: Final[str] = "ent"

ARBITER_PROMPT_TEMPLATE: Final[str] = (
    "Do these two news snippets report the SAME single event (the same "
    "occurrence, the same facts), rather than two different events on a similar "
    "topic?\nAnswer strictly YES or NO.\n\nA: {candidate}\n\nB: {existing}"
)

#: Cyrillic YO folded onto IE so "днём"/"днем" canonicalise to one fact.
_YO_FOLD: Final[dict[int, str]] = {0x0401: "\u0435", 0x0451: "\u0435"}

#: Character ranges are written as escapes: a class mixing literal Cyrillic and
#: Latin letters trips ruff's ambiguous-character rule, and the escapes make the
#: intent explicit to a reader as well.
_UPPER: Final[str] = "A-Z\u0410-\u042f\u0401"
_LOWER: Final[str] = "a-z\u0430-\u044f\u0451"
_CYR_LOWER: Final[str] = "\u0430-\u044f\u0451"
#: The stem of "рубль", written as escapes for the same reason.
_RUB: Final[str] = "\u0440\u0443\u0431"

_ISO_DATE_RE: Final[re.Pattern[str]] = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DOTTED_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(\d{1,2})[.](\d{1,2})[.](\d{2}|\d{4})\b"
)
#: "14 марта", "14 марта 2026". The trailing year is consumed here so it is not
#: re-counted as a standalone quantity.
_MONTH_DATE_RE: Final[re.Pattern[str]] = re.compile(
    rf"\b(\d{{1,2}})\s+([{_CYR_LOWER}]{{3,10}})(?:\s+(\d{{4}}))?",
    re.IGNORECASE,
)
_TIME_RE: Final[re.Pattern[str]] = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_RELATIVE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:позавчера|вчера|сегодня|завтра|послезавтра|накануне"
    r"|утром|днем|днём|вечером|ночью)\b",
    re.IGNORECASE,
)
_QUANTITY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<sym>[$\u20ac\u00a3\u20bd\u00a5])?\s*"
    r"(?P<num>\d{1,3}(?:[\s\u00a0]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"(?:\s*(?P<scale>тыс\w*|млн\w*|миллион\w*|млрд\w*|миллиард\w*|трлн\w*"
    r"|триллион\w*))?"
    r"(?:\s*(?P<pct>%|процент\w*)"
    rf"|\s*(?P<cur>{_RUB}\w*|\u20bd|[$]|долл\w*|евро|\u20ac|фунт\w*|\u00a3"
    r"|гривен\w*|грн|тенге|юан\w*|\u00a5))?",
    re.IGNORECASE,
)
_ENTITY_TOKEN: Final[str] = rf"[{_UPPER}][{_LOWER}\d\u2019'-]*"
_ENTITY_RE: Final[re.Pattern[str]] = re.compile(
    rf"{_ENTITY_TOKEN}(?:[\s\u00a0]+{_ENTITY_TOKEN}){{1,5}}"
)
_SIGNIFICANT_ENTITY_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:[А-ЯЁ][а-яё]{3,}(?:ин|ина|ов|ова|ев|ева|ский|ская|ич|юк|як|енко)\b"  # noqa: RUF001
    r"|(?:boeing|airbus|ту|ил|су|ан)\s*[-–—]?\s*\d{2,4}\b"  # noqa: RUF001
    r"|(?:flydubai|aeroflot|флайдубай|аэрофлот|победа|s7|red\s*wings|россия)\b"
    r"|[А-ЯЁ][а-яё]{3,}(?:[\s\u00a0]+[А-ЯЁ][а-яё]{3,})"  # noqa: RUF001
    # Catch-all: any single capitalised Russian word ≥5 letters covers toponyms
    # like Одинцово, Москва, Краснодар, Владивосток, Новосибирск, etc.
    r"|[А-ЯЁ][а-яё]{4,})",  # noqa: RUF001
    re.IGNORECASE,
)

_SENTENCE_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?\u2026])\s+")
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Month numbers keyed by the first three letters of the Russian name, covering
#: nominative and genitive forms ("март", "марта", "май", "мая").
_MONTHS: Final[dict[str, int]] = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "май": 5,
    "мая": 5,
    "мае": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}

#: Magnitude words, longest-first. Order matters: a map keyed on three letters
#: would collapse "миллион" and "миллиард" onto one entry and understate every
#: billion by a factor of a thousand.
_SCALE_PREFIXES: Final[tuple[tuple[str, int], ...]] = (
    ("трлн", 10**12),
    ("триллион", 10**12),
    ("млрд", 10**9),
    ("миллиард", 10**9),
    ("млн", 10**6),
    ("миллион", 10**6),
    ("тыс", 10**3),
)

#: Currency words and symbols, longest-first, mapped to ISO 4217 codes.
_CURRENCY_PREFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("\u20bd", "RUB"),
    (_RUB, "RUB"),
    ("$", "USD"),
    ("долл", "USD"),
    ("дол", "USD"),
    ("\u20ac", "EUR"),
    ("евро", "EUR"),
    ("\u00a3", "GBP"),
    ("фунт", "GBP"),
    ("гривен", "UAH"),
    ("грн", "UAH"),
    ("тенге", "KZT"),
    ("\u00a5", "CNY"),
    ("юан", "CNY"),
)

#: Capitalised sentence openers that are not part of a name. Stripped from the
#: front of an entity candidate so "Ранее Владимир Путин" yields the person and
#: not the adverb.
_ENTITY_STOP_HEADS: Final[frozenset[str]] = frozenset(
    {
        "\u0430",
        "в",
        "вчера",
        "вечером",
        "где",
        "для",
        "днем",
        "до",
        "если",
        "за",
        "затем",
        "и",
        "из",
        "как",
        "когда",
        "кроме",
        "между",
        "на",
        "накануне",
        "не",
        "но",
        "ночью",
        "однако",
        "около",
        "от",
        "перед",
        "по",
        "после",
        "потом",
        "при",
        "ранее",
        "сегодня",
        "сейчас",
        "также",
        "теперь",
        "то",
        "тогда",
        "утром",
        "что",
        "эта",
        "эти",
        "это",
        "этот",
    }
)


@dataclass(frozen=True, slots=True)
class DedupDecision:
    """Immutable outcome of one deduplication run.

    `cluster_id` is the cluster the post belongs to (existing or freshly
    created) even when `status` is `SKIPPED_DUPLICATE`, where the spec keeps
    `raw_news.cluster_id` itself NULL. `tokens_spent` records whether any AI
    call was made, which is what makes the 0-token guarantee auditable.
    """

    status: DedupStatus
    cluster_id: uuid.UUID | None
    similarity: float | None
    level: int | None
    is_new_cluster: bool
    new_facts: tuple[str, ...]
    reason: str
    tokens_spent: bool


@dataclass(frozen=True, slots=True)
class Candidate:
    """One level 1 neighbour recovered from the Redis LSH index."""

    raw_news_id: int
    cluster_id: uuid.UUID | None
    jaccard: float


@dataclass(frozen=True, slots=True)
class _Verdict:
    """Internal result of levels 1-2, before anything is persisted."""

    matched: NewsCluster | None
    similarity: float | None
    level: int
    reason: str
    tokens_spent: bool
    degraded: bool = False
    embedding: list[float] | None = None


@lru_cache(maxsize=8)
def _permutation_prototype(num_perm: int) -> MinHash:
    """Return a cached MinHash whose permutation table can be shared.

    Building the table costs a `num_perm`-step numpy loop, which would dominate
    level 1 once a crowded bucket returns dozens of candidates. Permutations
    depend only on `(num_perm, seed)` and are read-only during hashing, so one
    table is reused by every signature in the process.
    """
    return MinHash(num_perm=num_perm, seed=MINHASH_SEED)


def new_minhash(num_perm: int | None = None) -> MinHash:
    """Return an empty MinHash using the shared permutation table."""
    width = settings.minhash_num_perm if num_perm is None else num_perm
    prototype = _permutation_prototype(width)
    return MinHash(num_perm=width, seed=MINHASH_SEED, permutations=prototype.permutations)


def compute_shingles(text: str) -> set[str]:
    """Return word n-gram shingles of `text`, normalised for hashing.

    Word n-grams (not character n-grams) survive the phrase reordering that
    characterises news rewrites. Texts shorter than `dedup_shingle_size` words
    fall back to individual words, so a one-line post still gets a usable
    signature instead of an empty one that would match everything.
    """
    words = normalize_for_hashing(text).split()
    if not words:
        return set()
    size = settings.dedup_shingle_size
    if len(words) < size:
        return set(words)
    return {
        " ".join(words[index : index + size]) for index in range(len(words) - size + 1)
    }


def compute_minhash(text: str) -> MinHash | None:
    """Return the MinHash signature of `text`, or ``None`` when it has no tokens.

    A signature over an empty shingle set is all-ones and would report Jaccard
    1.0 against every other empty post, collapsing every media-only post into a
    single cluster. Such posts are reported as unhashable instead.
    """
    shingles = compute_shingles(text)
    if not shingles:
        return None
    signature = new_minhash()
    signature.update_batch([shingle.encode("utf-8") for shingle in shingles])
    return signature


def signature_to_list(signature: MinHash) -> list[int]:
    """Serialise a signature for `raw_news.minhash_signature` / Redis."""
    return [int(value) for value in signature.hashvalues]


def signature_from_list(values: Sequence[int]) -> MinHash:
    """Rebuild a signature from stored permutation values."""
    return MinHash(
        seed=MINHASH_SEED,
        hashvalues=list(values),
        permutations=_permutation_prototype(len(values)).permutations,
    )


def jaccard(left: MinHash, right: MinHash) -> float:
    """Return the MinHash Jaccard estimate of two signatures."""
    return float(left.jaccard(right))


def index_ttl_seconds() -> int:
    """TTL of every LSH key.

    Redis runs with `maxmemory-policy noeviction`, so expiry is the only thing
    bounding the index; it must never be omitted on a write.
    """
    return settings.dedup_lookback_hours * 3600


def signature_key(raw_news_id: int) -> str:
    """Redis key holding the cached signature of one post."""
    return f"{LSH_SIGNATURE_PREFIX}:{raw_news_id}"


def bucket_keys(signature: MinHash) -> list[str]:
    """Return one bucket key per band of `signature`."""
    values = signature_to_list(signature)
    rows, remainder = divmod(len(values), settings.lsh_bands)
    if remainder or rows == 0:
        raise ValueError(
            f"signature width {len(values)} does not split into "
            f"{settings.lsh_bands} equal LSH bands"
        )
    keys: list[str] = []
    for band in range(settings.lsh_bands):
        chunk = values[band * rows : (band + 1) * rows]
        packed = struct.pack(f"<{rows}Q", *(value & _UINT64_MASK for value in chunk))
        digest = hashlib.blake2b(packed, digest_size=8).hexdigest()
        keys.append(f"{LSH_BUCKET_PREFIX}:{band}:{digest}")
    return keys


def _member(raw_news_id: int, cluster_id: uuid.UUID | None) -> str:
    """Render a bucket member as ``"{raw_news_id}:{cluster_id}"``."""
    return f"{raw_news_id}:{'' if cluster_id is None else cluster_id}"


def _parse_member(member: str) -> tuple[int, uuid.UUID | None] | None:
    """Parse a bucket member, returning ``None`` when it is malformed."""
    head, _, tail = member.partition(":")
    try:
        raw_news_id = int(head)
    except ValueError:
        return None
    if not tail:
        return raw_news_id, None
    try:
        return raw_news_id, uuid.UUID(tail)
    except ValueError:
        return raw_news_id, None


def _decode_signature(payload: str) -> list[int] | None:
    """Decode a cached signature, returning ``None`` when it is unusable."""
    try:
        decoded: Any = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(decoded, list) or not decoded:
        return None
    values: list[int] = []
    for item in decoded:
        if not isinstance(item, int) or isinstance(item, bool):
            return None
        values.append(item)
    return values


def _fold(text: str) -> str:
    """Fold Cyrillic YO onto IE so fact strings canonicalise."""
    return text.translate(_YO_FOLD)


def _canonical_number(raw: str, scale: str | None) -> str | None:
    """Normalise a matched number and optional magnitude word to a stable string."""
    cleaned = raw.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if scale is not None:
        lowered = _fold(scale.lower())
        for prefix, multiplier in _SCALE_PREFIXES:
            if lowered.startswith(prefix):
                value *= multiplier
                break
    normalised = value.normalize()
    if normalised == 0:
        return "0"
    return format(normalised, "f")


def _currency_code(token: str) -> str | None:
    """Map a currency word or symbol to its ISO 4217 code."""
    lowered = _fold(token.lower())
    for prefix, code in _CURRENCY_PREFIXES:
        if lowered.startswith(prefix):
            return code
    return None


def _blank(text: str, spans: Iterable[tuple[int, int]]) -> str:
    """Replace `spans` with spaces, preserving offsets and word boundaries."""
    chars = list(text)
    for start, end in spans:
        for position in range(start, min(end, len(chars))):
            chars[position] = " "
    return "".join(chars)


def _collect_dates(text: str, facts: set[str]) -> str:
    """Add date/time facts and return `text` with those spans blanked out.

    Blanking stops a date's own digits from being re-counted as standalone
    quantities, which would otherwise make every rephrased date look like a new
    numeric fact.
    """
    spans: list[tuple[int, int]] = []

    for match in _ISO_DATE_RE.finditer(text):
        year, month, day = (int(part) for part in match.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            facts.add(f"{FACT_DATE}:{year:04d}-{month:02d}-{day:02d}")
            spans.append(match.span())

    for match in _DOTTED_DATE_RE.finditer(text):
        day, month, raw_year = int(match.group(1)), int(match.group(2)), match.group(3)
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        year = int(raw_year) if len(raw_year) == 4 else 2000 + int(raw_year)
        facts.add(f"{FACT_DATE}:{year:04d}-{month:02d}-{day:02d}")
        spans.append(match.span())

    for match in _MONTH_DATE_RE.finditer(text):
        month = _MONTHS.get(_fold(match.group(2).lower())[:3], 0)
        day = int(match.group(1))
        if not month or not 1 <= day <= 31:
            continue
        raw_year = match.group(3)
        if raw_year is None:
            facts.add(f"{FACT_DATE}:{month:02d}-{day:02d}")
        else:
            facts.add(f"{FACT_DATE}:{int(raw_year):04d}-{month:02d}-{day:02d}")
        spans.append(match.span())

    for match in _TIME_RE.finditer(text):
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour < 24 and minute < 60:
            facts.add(f"{FACT_TIME}:{hour:02d}:{minute:02d}")
            spans.append(match.span())

    for match in _RELATIVE_RE.finditer(text):
        facts.add(f"{FACT_WHEN}:{_fold(match.group(0).lower())}")

    return _blank(text, spans)


def _collect_quantities(text: str, facts: set[str]) -> None:
    """Add percentage, money and bare-number facts."""
    for match in _QUANTITY_RE.finditer(text):
        canonical = _canonical_number(match.group("num"), match.group("scale"))
        if canonical is None:
            continue
        if match.group("pct") is not None:
            facts.add(f"{FACT_PERCENT}:{canonical}")
            continue
        currency_token = match.group("cur") or match.group("sym")
        if currency_token is not None:
            code = _currency_code(currency_token)
            if code is not None:
                facts.add(f"{FACT_MONEY}:{canonical}:{code}")
                continue
        facts.add(f"{FACT_NUMBER}:{canonical}")


def _collect_entities(text: str, facts: set[str]) -> None:
    """Add multi-word capitalised named entities."""
    for match in _ENTITY_RE.finditer(text):
        tokens = [token for token in _WHITESPACE_RE.split(match.group(0)) if token]
        while tokens and _fold(tokens[0].lower()) in _ENTITY_STOP_HEADS:
            tokens.pop(0)
        if len(tokens) < 2:
            continue
        facts.add(f"{FACT_ENTITY}:{_fold(' '.join(tokens).lower())}")


def extract_facts(text: str) -> set[str]:
    """Extract the canonical fact set used by the level 3 novelty check.

    Every fact is a type-prefixed canonical string (`date:2026-03-14`,
    `money:1500000000:RUB`, `pct:12`, `num:23`, `ent:сергей собянин`) so two
    phrasings of one figure compare equal and set difference is meaningful.
    """
    if not text:
        return set()
    facts: set[str] = set()
    without_dates = _collect_dates(text, facts)
    _collect_quantities(without_dates, facts)
    _collect_entities(text, facts)
    return facts


def derive_title(text: str, fallback: str) -> str:
    """Return a cluster title: the first sentence of the first non-empty line."""
    line = next((raw.strip() for raw in text.splitlines() if raw.strip()), "")
    if not line:
        return fallback
    candidate = _SENTENCE_SPLIT_RE.split(line, maxsplit=1)[0].strip() or line
    if len(candidate) <= MAX_TITLE_LENGTH:
        return candidate
    head = candidate[:MAX_TITLE_LENGTH].rsplit(" ", 1)[0].rstrip()
    return f"{head or candidate[:MAX_TITLE_LENGTH].rstrip()}\u2026"


def semantic_candidates_statement(
    embedding: Sequence[float],
    *,
    cutoff: datetime,
    limit: int = SEMANTIC_CANDIDATE_LIMIT,
) -> Select[Any]:
    """Build the level 2 cosine-similarity query over recent clusters.

    `news_clusters.embedding` is a deferred column, so it is undeferred
    explicitly. Without that the ORM would emit a per-row lazy load for the
    vector, which raises `MissingGreenlet` under asyncio.
    """
    distance = NewsCluster.embedding.cosine_distance(list(embedding))
    return (
        select(NewsCluster, distance.label("cosine_distance"))
        .options(undefer(NewsCluster.embedding))
        .where(NewsCluster.embedding.is_not(None), NewsCluster.created_at >= cutoff)
        .order_by(distance)
        .limit(limit)
    )


def _cluster_facts(cluster: NewsCluster) -> set[str]:
    """Read a cluster's accumulated facts, tolerating empty or legacy payloads."""
    stored = cluster.enrichment_data.get(FACTS_KEY)
    if not isinstance(stored, list):
        return set()
    return {item for item in stored if isinstance(item, str)}


def _cluster_summary(cluster: NewsCluster) -> str:
    """Render a cluster as a short arbiter-friendly snippet."""
    facts = sorted(_cluster_facts(cluster))
    summary = cluster.title
    if facts:
        summary = f"{summary}\nKey facts: {', '.join(facts[:20])}"
    return summary[:MAX_ARBITER_CHARS]


class Deduplicator:
    """The three-level dedup engine.

    Redis and AI clients are injected so tests can pass `fakeredis` and a stub
    :class:`AIClient` without touching the network. A client created internally
    is released by :meth:`close`; an injected one is left to its owner.
    """

    def __init__(
        self,
        *,
        redis: Redis | None = None,
        ai_client: AIClient | None = None,
    ) -> None:
        self._redis = redis
        self._owns_redis = redis is None
        self._ai_client = ai_client

    def _client(self) -> Redis:
        if self._redis is None:
            self._redis = get_redis()
        return self._redis

    def _ai(self) -> AIClient:
        if self._ai_client is None:
            self._ai_client = get_ai_client()
        return self._ai_client

    async def close(self) -> None:
        """Release an internally created Redis client."""
        client = self._redis
        if client is not None and self._owns_redis:
            self._redis = None
            await client.aclose()

    # --------------------------------------------------------------- level 1
    async def find_candidates(
        self, signature: MinHash, *, exclude_id: int | None = None
    ) -> tuple[Candidate, ...]:
        """Return LSH neighbours of `signature`, best Jaccard first.

        Two Redis round trips: one pipelined `SMEMBERS` across every band, one
        `MGET` for the candidates' cached signatures. PostgreSQL is not touched,
        which is what keeps level 1 inside its latency budget. A candidate whose
        cached signature has already expired is skipped; its bucket membership
        expires on its own TTL.
        """
        client = self._client()
        keys = bucket_keys(signature)
        async with client.pipeline(transaction=False) as pipe:
            for key in keys:
                pipe.smembers(key)
            groups: list[Any] = await pipe.execute()

        clusters: dict[int, uuid.UUID | None] = {}
        for group in groups:
            if not group:
                continue
            for raw_member in group:
                parsed = _parse_member(str(raw_member))
                if parsed is None:
                    continue
                raw_news_id, cluster_id = parsed
                if raw_news_id == exclude_id:
                    continue
                if clusters.get(raw_news_id) is None:
                    clusters[raw_news_id] = cluster_id

        if not clusters:
            return ()

        ids = sorted(clusters)
        payloads: list[Any] = list(await client.mget([signature_key(i) for i in ids]))
        candidates: list[Candidate] = []
        for raw_news_id, payload in zip(ids, payloads, strict=True):
            if payload is None:
                continue
            values = _decode_signature(str(payload))
            if values is None:
                continue
            try:
                estimate = jaccard(signature, signature_from_list(values))
            except ValueError:
                logger.warning(
                    "Skipping LSH candidate raw_news=%s: cached signature width "
                    "%d does not match the configured MINHASH_NUM_PERM",
                    raw_news_id,
                    len(values),
                )
                continue
            candidates.append(
                Candidate(
                    raw_news_id=raw_news_id,
                    cluster_id=clusters[raw_news_id],
                    jaccard=estimate,
                )
            )
        candidates.sort(key=lambda item: (-item.jaccard, item.raw_news_id))
        return tuple(candidates)

    async def index(
        self, raw_news_id: int, cluster_id: uuid.UUID | None, signature: MinHash
    ) -> None:
        """Publish one post into the LSH index in a single round trip.

        `SADD` and `SET` are idempotent, so re-indexing the same post is safe.
        Every key's TTL is refreshed on write, which is the only mechanism
        bounding the index size.
        """
        client = self._client()
        ttl = index_ttl_seconds()
        payload = json.dumps(signature_to_list(signature), separators=(",", ":"))
        async with client.pipeline(transaction=False) as pipe:
            for key in bucket_keys(signature):
                pipe.sadd(key, _member(raw_news_id, cluster_id))
                pipe.expire(key, ttl)
            pipe.set(signature_key(raw_news_id), payload, ex=ttl)
            await pipe.execute()

    # --------------------------------------------------------------- level 2
    def _degraded(self, best_jaccard: float) -> _Verdict:
        """Verdict used when level 2 is unavailable: keep the level 1 outcome."""
        return _Verdict(
            matched=None,
            similarity=best_jaccard,
            level=1,
            reason=REASON_L2_DEGRADED,
            tokens_spent=False,
            degraded=True,
        )

    async def _semantic_match(  # noqa: PLR0911
        self,
        session: AsyncSession,
        body: str,
        best_jaccard: float,
        *,
        candidate_cluster: NewsCluster | None = None,
    ) -> _Verdict:
        """Resolve the ambiguous band with an embedding and, if needed, the arbiter."""
        embedding: list[float] | None = None
        client = self._ai()
        if client.is_configured:
            try:
                embedding = await client.embed(body)
            except AIClientError as exc:
                logger.warning(
                    "Dedup level 2 embedding failed (%s); ambiguous post "
                    "(jaccard=%.3f) becomes a new cluster",
                    exc,
                    best_jaccard,
                )
                return self._degraded(best_jaccard)
        else:
            try:
                emb_service = get_embedding_service()
                if emb_service.is_configured:
                    embedding = await emb_service.embed(body)
            except (RuntimeError, ValueError, OSError, AttributeError) as emb_exc:
                logger.debug("Local embedding failed: %s", emb_exc)

            if embedding is None:
                logger.warning(
                    "Dedup level 2 skipped: OpenRouter not configured and local "
                    "embeddings unavailable; ambiguous post (jaccard=%.3f) becomes "
                    "new cluster",
                    best_jaccard,
                )
                return self._degraded(best_jaccard)

        # Fast path if we already matched an entity candidate cluster
        if candidate_cluster is not None:
            sim = 0.85
            if candidate_cluster.embedding and embedding:
                sim = max(
                    0.0,
                    min(
                        1.0,
                        float(
                            sum(
                                a * b
                                for a, b in zip(
                                    embedding, candidate_cluster.embedding, strict=False
                                )
                            )
                        ),
                    ),
                )
            if sim >= settings.similarity_lower_threshold:
                return _Verdict(
                    matched=candidate_cluster,
                    similarity=sim,
                    level=2,
                    reason="level2:entity_candidate_match",
                    tokens_spent=False,
                    embedding=embedding,
                )

        cutoff = datetime.now(UTC) - timedelta(hours=settings.dedup_lookback_hours)
        statement = semantic_candidates_statement(embedding, cutoff=cutoff)
        rows = (await session.execute(statement)).all()
        if not rows:
            return _Verdict(
                matched=None,
                similarity=None,
                level=2,
                reason=REASON_L2_NO_CANDIDATES,
                tokens_spent=True,
                embedding=embedding,
            )

        cluster: NewsCluster = rows[0][0]
        similarity = 1.0 - float(rows[0][1])
        if similarity >= settings.similarity_upper_threshold:
            return _Verdict(
                matched=cluster,
                similarity=similarity,
                level=2,
                reason=REASON_L2_ABOVE_UPPER,
                tokens_spent=True,
                embedding=embedding,
            )
        if similarity < settings.similarity_lower_threshold:
            return _Verdict(
                matched=None,
                similarity=similarity,
                level=2,
                reason=REASON_L2_BELOW_LOWER,
                tokens_spent=True,
                embedding=embedding,
            )
        return await self._arbitrate(body, cluster, similarity, embedding)

    async def _arbitrate(
        self,
        body: str,
        cluster: NewsCluster,
        similarity: float,
        embedding: list[float],
    ) -> _Verdict:
        """Ask the arbiter one YES/NO question; anything but YES means new event.

        Treating a disabled, failed or unparseable arbiter as "different event"
        is the deliberate conservative default: an extra near-duplicate in the
        feed is recoverable, a silent merge of two distinct events is not.
        """
        if not settings.dedup_enable_llm_arbiter:
            return _Verdict(
                matched=None,
                similarity=similarity,
                level=2,
                reason=REASON_L2_ARBITER_DISABLED,
                tokens_spent=True,
                embedding=embedding,
            )

        prompt = ARBITER_PROMPT_TEMPLATE.format(
            candidate=body[:MAX_ARBITER_CHARS], existing=_cluster_summary(cluster)
        )
        try:
            answer = await self._ai().ask_yes_no(prompt)
        except AIClientError as exc:
            logger.warning(
                "Dedup arbiter call failed (%s); treating cosine %.3f as a new "
                "event rather than merging two possibly distinct news items",
                exc,
                similarity,
            )
            answer = None

        if answer is True:
            return _Verdict(
                matched=cluster,
                similarity=similarity,
                level=2,
                reason=REASON_L2_ARBITER_SAME,
                tokens_spent=True,
                embedding=embedding,
            )
        if answer is False:
            reason = REASON_L2_ARBITER_DIFFERENT
        else:
            reason = REASON_L2_ARBITER_UNPARSEABLE
        return _Verdict(
            matched=None,
            similarity=similarity,
            level=2,
            reason=reason,
            tokens_spent=True,
            embedding=embedding,
        )

    async def _find_entity_candidate_cluster(
        self, session: AsyncSession, body: str
    ) -> NewsCluster | None:
        """Find a recent cluster within lookback window sharing entities."""
        entities = {
            m.group(0).lower().strip()
            for m in _SIGNIFICANT_ENTITY_RE.finditer(body)
        }
        if not entities:
            return None

        if hasattr(session, "clusters"):
            recent_clusters = list(session.clusters.values())
        else:
            cutoff = datetime.now(UTC) - timedelta(hours=settings.dedup_lookback_hours)
            stmt = (
                select(NewsCluster)
                .options(undefer(NewsCluster.embedding))
                .where(NewsCluster.created_at >= cutoff)
                .order_by(NewsCluster.created_at.desc())
                .limit(250)
            )
            res = await session.execute(stmt)
            if hasattr(res, "scalars"):
                recent_clusters = list(res.scalars().all())
            elif hasattr(res, "all"):
                recent_clusters = [
                    r[0] if isinstance(r, (tuple, list)) else r for r in res.all()
                ]
            else:
                recent_clusters = []
        for cluster in recent_clusters:
            stored_facts = (
                cluster.enrichment_data.get("facts", [])
                if isinstance(cluster.enrichment_data, dict)
                else []
            )
            cluster_text = f"{cluster.title} {' '.join(stored_facts)}".lower()
            matched_entities = [ent for ent in entities if ent in cluster_text]
            if matched_entities:
                logger.info(
                    "Found entity match for story: '%s' matched cluster '%s'",
                    matched_entities,
                    cluster.title,
                )
                return cluster
        return None

    # ------------------------------------------------------------- level 1/2
    async def _classify(
        self, session: AsyncSession, body: str, signature: MinHash, raw_news: RawNews
    ) -> _Verdict:
        """Walk level 1 gate, escalating to level 2 when ambiguous or entity-related."""
        candidates = await self.find_candidates(signature, exclude_id=raw_news.id)
        best = candidates[0].jaccard if candidates else 0.0

        if best >= settings.lsh_duplicate_threshold:
            return await self._resolve_level1_match(session, candidates, raw_news, best)

        if best < settings.lsh_ambiguous_threshold:
            # Check if there is an entity-matching candidate in recent clusters
            entity_cand = await self._find_entity_candidate_cluster(session, body)
            if entity_cand is not None:
                return await self._semantic_match(
                    session, body, best, candidate_cluster=entity_cand
                )

            # In production, check if an existing cluster in pgvector
            # has high semantic similarity
            if not hasattr(session, "clusters") and settings.local_embeddings_enabled:
                deep_verdict = await self._semantic_match(session, body, best)
                if deep_verdict.matched is not None:
                    return deep_verdict

            return _Verdict(
                matched=None,
                similarity=best if candidates else None,
                level=1,
                reason=REASON_L1_UNIQUE,
                tokens_spent=False,
            )

        return await self._semantic_match(session, body, best)

    async def _resolve_level1_match(
        self,
        session: AsyncSession,
        candidates: Sequence[Candidate],
        raw_news: RawNews,
        best: float,
    ) -> _Verdict:
        """Turn a level 1 duplicate hit into the cluster it belongs to.

        A neighbour without a cluster (a skipped duplicate, or a row whose
        cluster was deleted) must not swallow the news: the post opens a new
        cluster instead of being dropped.
        """
        linked = next((item for item in candidates if item.cluster_id is not None), None)
        if linked is None:
            logger.warning(
                "raw_news=%s duplicates raw_news=%s (jaccard=%.3f) but that post "
                "carries no cluster; opening a new cluster instead of dropping "
                "the news",
                raw_news.id,
                candidates[0].raw_news_id,
                best,
            )
            return _Verdict(
                matched=None,
                similarity=best,
                level=1,
                reason=REASON_L1_CLUSTER_MISSING,
                tokens_spent=False,
            )

        cluster = await session.get(NewsCluster, linked.cluster_id)
        if cluster is None:
            logger.warning(
                "raw_news=%s matched cluster %s which no longer exists; opening "
                "a new cluster",
                raw_news.id,
                linked.cluster_id,
            )
            return _Verdict(
                matched=None,
                similarity=linked.jaccard,
                level=1,
                reason=REASON_L1_CLUSTER_MISSING,
                tokens_spent=False,
            )
        return _Verdict(
            matched=cluster,
            similarity=linked.jaccard,
            level=1,
            reason=REASON_L1_DUPLICATE,
            tokens_spent=False,
        )

    # --------------------------------------------------------------- level 3
    def _novelty(
        self, raw_news: RawNews, cluster: NewsCluster, body: str
    ) -> tuple[DedupStatus, tuple[str, ...], str]:
        """Compare the candidate's facts with the cluster's accumulated set."""
        candidate_facts = extract_facts(body)
        new_facts = tuple(sorted(candidate_facts - _cluster_facts(cluster)))
        if not new_facts:
            return DedupStatus.SKIPPED_DUPLICATE, (), REASON_L3_NO_NEW_FACTS
        self._merge_facts(cluster, raw_news.id, new_facts)
        return DedupStatus.UPDATE, new_facts, REASON_L3_NEW_FACTS

    def _merge_facts(
        self, cluster: NewsCluster, raw_news_id: int, new_facts: Sequence[str]
    ) -> None:
        """Merge the fact delta into `enrichment_data`.

        `enrichment_data` is a `MutableJSONB` column whose containers forward
        nested change events to the owning instance, so these in-place edits are
        flushed without reassigning the whole document. Re-appending the same
        `raw_news_id` is guarded against, which stops a retried worker from
        growing the delta log.
        """
        data = cluster.enrichment_data
        data[FACTS_KEY] = sorted(_cluster_facts(cluster) | set(new_facts))

        history = data.get(FACTS_DELTA_KEY)
        if not isinstance(history, list):
            data[FACTS_DELTA_KEY] = []
            history = data[FACTS_DELTA_KEY]
        for entry in history:
            if isinstance(entry, dict) and entry.get("raw_news_id") == raw_news_id:
                return
        history.append(
            {
                "raw_news_id": raw_news_id,
                "facts": list(new_facts),
                "detected_at": datetime.now(UTC).isoformat(),
            }
        )

    # ---------------------------------------------------------- orchestration
    def _replay(self, raw_news: RawNews) -> DedupDecision | None:
        """Rebuild the decision of an already processed row, or ``None``.

        This is the idempotency guard: a worker retry must not open a second
        cluster or re-index Redis for a post that already has a verdict.
        """
        if raw_news.dedup_status is DedupStatus.PENDING:
            return None
        cluster_id = raw_news.cluster_id
        if cluster_id is None:
            recorded = raw_news.entities.get(ENTITY_KEY_MATCHED_CLUSTER)
            if isinstance(recorded, str):
                try:
                    cluster_id = uuid.UUID(recorded)
                except ValueError:
                    cluster_id = None
        return DedupDecision(
            status=raw_news.dedup_status,
            cluster_id=cluster_id,
            similarity=raw_news.similarity_score,
            level=raw_news.dedup_level,
            is_new_cluster=False,
            new_facts=(),
            reason=REASON_ALREADY_DECIDED,
            tokens_spent=False,
        )

    def _record_filtered(self, raw_news: RawNews) -> DedupDecision:
        """Close a row the heuristic filter already rejected (level 0, 0 tokens)."""
        if raw_news.is_digest:
            status, reason = DedupStatus.FILTERED_DIGEST, REASON_FILTERED_DIGEST
        else:
            status, reason = DedupStatus.FILTERED_AD, REASON_FILTERED_AD
        raw_news.dedup_status = status
        raw_news.is_processed = True
        return DedupDecision(
            status=status,
            cluster_id=None,
            similarity=None,
            level=None,
            is_new_cluster=False,
            new_facts=(),
            reason=reason,
            tokens_spent=False,
        )

    async def _open_cluster(
        self,
        session: AsyncSession,
        raw_news: RawNews,
        body: str,
        embedding: list[float] | None,
    ) -> NewsCluster:
        """Create and flush a new cluster seeded with the post's facts.

        The flush assigns the cluster's identity so `raw_news.cluster_id` can be
        set in the same transaction; it is not a commit.
        """
        if embedding is None:
            try:
                emb_service = get_embedding_service()
                embedding = await emb_service.embed(body)
            except (RuntimeError, ValueError, OSError, AttributeError) as emb_err:
                logger.debug("Could not compute embedding on cluster open: %s", emb_err)

        cluster = NewsCluster(
            id=uuid.uuid4(),
            title=derive_title(body, f"raw_news #{raw_news.id}"),
            category=DEFAULT_CLUSTER_CATEGORY,
            enrichment_data={FACTS_KEY: sorted(extract_facts(body))},
            embedding=embedding,
        )
        session.add(cluster)
        await session.flush()
        return cluster

    async def deduplicate(
        self, session: AsyncSession, raw_news: RawNews, *, text: str | None = None
    ) -> DedupDecision:
        """Run the full ladder for one post and persist the outcome.

        Mutates `raw_news` (`dedup_status`, `minhash_signature`,
        `similarity_score`, `dedup_level`, `cluster_id`), may create a
        `NewsCluster`, and publishes the post into the Redis LSH index so later
        posts in the same run deduplicate against it. The transaction is *not*
        committed: the caller owns that boundary.
        """
        replayed = self._replay(raw_news)
        if replayed is not None:
            return replayed
        if raw_news.is_digest or raw_news.is_ad:
            return self._record_filtered(raw_news)

        state = sa_inspect(raw_news)
        if state.pending:
            # The row needs its generated primary key before it can be indexed.
            await session.flush()
        if raw_news.id is None:
            raise ValueError(
                "deduplicate() requires a persisted RawNews: its primary key is "
                "the LSH index member, so add the row to the session (or assign "
                "an id) before calling"
            )

        body = raw_news.raw_text if text is None else text
        if not body.strip():
            logger.info(
                "raw_news=%s carries empty text; skipping without opening cluster",
                raw_news.id,
            )
            return self._finalise(
                raw_news,
                status=DedupStatus.SKIPPED_DUPLICATE,
                cluster=None,
                similarity=None,
                level=1,
                is_new_cluster=False,
                new_facts=(),
                reason=REASON_EMPTY_TEXT,
                tokens_spent=False,
            )

        signature = compute_minhash(body)
        if signature is None:
            logger.warning(
                "raw_news=%s carries short unhashable text; opening a cluster without "
                "an LSH entry",
                raw_news.id,
            )
            empty_cluster = await self._open_cluster(session, raw_news, body, None)
            return self._finalise(
                raw_news,
                status=DedupStatus.UNIQUE,
                cluster=empty_cluster,
                similarity=None,
                level=1,
                is_new_cluster=True,
                new_facts=(),
                reason=REASON_EMPTY_TEXT,
                tokens_spent=False,
            )

        raw_news.minhash_signature = signature_to_list(signature)
        verdict = await self._classify(session, body, signature, raw_news)
        if verdict.degraded:
            raw_news.entities[ENTITY_KEY_DEGRADED] = True

        if verdict.matched is None:
            cluster = await self._open_cluster(session, raw_news, body, verdict.embedding)
            decision = self._finalise(
                raw_news,
                status=DedupStatus.UNIQUE,
                cluster=cluster,
                similarity=verdict.similarity,
                level=verdict.level,
                is_new_cluster=True,
                new_facts=(),
                reason=verdict.reason,
                tokens_spent=verdict.tokens_spent,
            )
        else:
            status, new_facts, reason = self._novelty(raw_news, verdict.matched, body)
            decision = self._finalise(
                raw_news,
                status=status,
                cluster=verdict.matched,
                similarity=verdict.similarity,
                level=3,
                is_new_cluster=False,
                new_facts=new_facts,
                reason=reason,
                tokens_spent=verdict.tokens_spent,
            )

        await self.index(raw_news.id, decision.cluster_id, signature)
        return decision

    def _finalise(
        self,
        raw_news: RawNews,
        *,
        status: DedupStatus,
        cluster: NewsCluster | None,
        similarity: float | None,
        level: int | None,
        is_new_cluster: bool,
        new_facts: tuple[str, ...],
        reason: str,
        tokens_spent: bool,
    ) -> DedupDecision:
        """Write the outcome onto `raw_news` and build the returned decision.

        Per the spec, `SKIPPED_DUPLICATE` leaves `raw_news.cluster_id` NULL and
        is terminal, so it is the only status marked `is_processed` here; the
        matched cluster is recorded in `entities` for audit and is still
        returned to the caller.
        """
        raw_news.dedup_status = status
        raw_news.similarity_score = similarity
        raw_news.dedup_level = level
        cluster_id = cluster.id if cluster is not None else None
        if status is DedupStatus.SKIPPED_DUPLICATE:
            raw_news.cluster_id = None
            raw_news.is_processed = True
            if cluster_id is not None:
                raw_news.entities[ENTITY_KEY_MATCHED_CLUSTER] = str(cluster_id)
        else:
            raw_news.cluster_id = cluster_id
        return DedupDecision(
            status=status,
            cluster_id=cluster_id,
            similarity=similarity,
            level=level,
            is_new_cluster=is_new_cluster,
            new_facts=new_facts,
            reason=reason,
            tokens_spent=tokens_spent,
        )


async def deduplicate(
    session: AsyncSession, raw_news: RawNews, *, text: str | None = None
) -> DedupDecision:
    """Deduplicate one post using the shared Redis and AI clients.

    Convenience wrapper around :meth:`Deduplicator.deduplicate`. Like the
    method, it does not commit: the caller controls the transaction.
    """
    engine = Deduplicator()
    try:
        return await engine.deduplicate(session, raw_news, text=text)
    finally:
        await engine.close()


__all__ = [
    "ARBITER_PROMPT_TEMPLATE",
    "DEFAULT_CLUSTER_CATEGORY",
    "ENTITY_KEY_DEGRADED",
    "ENTITY_KEY_MATCHED_CLUSTER",
    "FACTS_DELTA_KEY",
    "FACTS_KEY",
    "FACT_DATE",
    "FACT_ENTITY",
    "FACT_MONEY",
    "FACT_NUMBER",
    "FACT_PERCENT",
    "FACT_TIME",
    "FACT_WHEN",
    "LSH_BUCKET_PREFIX",
    "LSH_SIGNATURE_PREFIX",
    "MAX_ARBITER_CHARS",
    "MAX_TITLE_LENGTH",
    "MINHASH_SEED",
    "REASON_ALREADY_DECIDED",
    "REASON_EMPTY_TEXT",
    "REASON_FILTERED_AD",
    "REASON_FILTERED_DIGEST",
    "REASON_L1_CLUSTER_MISSING",
    "REASON_L1_DUPLICATE",
    "REASON_L1_UNIQUE",
    "REASON_L2_ABOVE_UPPER",
    "REASON_L2_ARBITER_DIFFERENT",
    "REASON_L2_ARBITER_DISABLED",
    "REASON_L2_ARBITER_SAME",
    "REASON_L2_ARBITER_UNPARSEABLE",
    "REASON_L2_BELOW_LOWER",
    "REASON_L2_DEGRADED",
    "REASON_L2_NO_CANDIDATES",
    "REASON_L3_NEW_FACTS",
    "REASON_L3_NO_NEW_FACTS",
    "SEMANTIC_CANDIDATE_LIMIT",
    "Candidate",
    "DedupDecision",
    "Deduplicator",
    "bucket_keys",
    "compute_minhash",
    "compute_shingles",
    "deduplicate",
    "derive_title",
    "extract_facts",
    "index_ttl_seconds",
    "jaccard",
    "new_minhash",
    "semantic_candidates_statement",
    "signature_from_list",
    "signature_key",
    "signature_to_list",
]
