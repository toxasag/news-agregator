"""Pydantic v2 contracts for the ingestion API (Package 2, `app/api/ingestion.py`).

Three groups of models live here:

* **Requests** — what the admin UI and the CLI may send. They are deliberately
  strict (``extra="forbid"``, stripped strings, bounded lengths): a typo in a
  selector name has to fail loudly at the edge instead of being persisted into
  ``sources.crawl_rules`` and silently doing nothing at crawl time.
* **Responses** — the persisted source, the dry-run crawl preview and the
  ingestion run report. Each carries ``description=`` metadata so ``/docs`` is
  usable as the reference for the dashboard's HTMX calls.
* **Adapters** — ``from_preview`` / ``from_dialog`` / ``from_report`` convert the
  frozen dataclasses returned by the services and the worker into schemas. The
  services stay free of Pydantic (they are used by the ARQ worker too), so the
  conversion has to live on this side of the boundary.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Final, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
)

from app.models.enums import MediaStrategy, SourceType
from app.services.telegram_ingestor import public_username

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module import-light
    from app.services.telegram_ingestor import TelegramDialog
    from app.services.web_scraper import CrawlPreview
    from app.workers.ingestion_worker import IngestionRunReport, SourceIngestReport

#: Numeric Telegram peer ids, e.g. ``-1001234567890``. Accepted alongside public
#: usernames because private channels have no username at all.
_NUMERIC_PEER_ID: Final[re.Pattern[str]] = re.compile(r"^-?[0-9]{5,20}$")

#: Prefixes that betray a pasted link rather than an identifier. ``://`` is
#: checked separately, so these are the schemeless variants people actually
#: paste out of a browser's address bar.
_URL_LIKE_PREFIXES: Final[tuple[str, ...]] = (
    "t.me/",
    "telegram.me/",
    "telegram.dog/",
    "www.",
)

#: Control characters have no meaning in a CSS or XPath selector and usually
#: arrive from a copy-paste out of a formatted document.
_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")

#: Upper bound on one selector. ``sources.crawl_rules`` is JSONB, so nothing
#: enforces a length in the database; a 10 kB "selector" is always a mistake.
_SELECTOR_MAX_LENGTH: Final[int] = 512

#: Matches ``sources.name`` (``String(255)``).
_NAME_MAX_LENGTH: Final[int] = 255

#: Matches ``sources.identifier`` (``String(512)``).
_IDENTIFIER_MAX_LENGTH: Final[int] = 512

#: Hard cap on a manual trigger's source list: a longer list is a mistake, and
#: an unbounded one would let a single request pin the worker for hours.
_MAX_TRIGGER_SOURCES: Final[int] = 200

SourceName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=_NAME_MAX_LENGTH),
]

Selector = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=_SELECTOR_MAX_LENGTH
    ),
]

SourceId = Annotated[int, Field(ge=1)]


def _reject_control_characters(value: str | None) -> str | None:
    """Reject a selector containing control characters."""
    if value is not None and _CONTROL_CHARS.search(value) is not None:
        raise ValueError(
            "selector contains control characters; paste it as plain text "
            "without newlines or tabs"
        )
    return value


class TelegramSourceCreate(BaseModel):
    """Add or edit a Telegram source.

    ``identifier`` is normalised to the **bare** channel username, without a
    leading ``@``: ``@svodka`` and ``svodka`` are the same channel, and storing
    both spellings would defeat the ``uq_sources_type_identifier`` unique
    constraint on ``(source_type, identifier)`` and ingest the channel twice.
    Numeric peer ids (``-1001234567890``) are kept verbatim, since a private
    channel has no username. A pasted URL is rejected rather than silently
    trimmed, because guessing which part of ``https://t.me/foo/1234`` is the
    channel is exactly the kind of guess that creates a duplicate source.
    """

    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(
        max_length=_IDENTIFIER_MAX_LENGTH,
        description=(
            "Channel username (`@name` or `name`) or numeric peer id "
            "(`-1001234567890`). A leading `@` is stripped before storage, so "
            "`@name` and `name` address the same source. URLs are rejected."
        ),
        examples=["@svodka_press", "-1001234567890"],
    )
    name: SourceName = Field(
        description="Human-readable label shown in the dashboard.",
        examples=["Svodka Press"],
    )
    is_active: bool = Field(
        default=True,
        description="Only active sources are collected by the ingestion worker.",
    )
    skip_media: bool = Field(
        default=False,
        description=(
            "Drop media metadata at ingestion time, making the source "
            "text-only: no downloads and no OCR further down the pipeline."
        ),
    )
    media_strategy: MediaStrategy = Field(
        default=MediaStrategy.OCR_CHECK,
        description="How Package 3 must treat media attached to this source.",
    )

    @field_validator("identifier", mode="after")
    @classmethod
    def _normalize_identifier(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise ValueError("identifier must not be empty")
        lowered = candidate.lower()
        if "://" in candidate or lowered.startswith(_URL_LIKE_PREFIXES):
            raise ValueError(
                "identifier must be a channel username (@name) or a numeric "
                "peer id, not a URL: send 'name' instead of "
                "'https://t.me/name'"
            )
        username = public_username(candidate)
        if username is not None:
            return username
        stripped = candidate.removeprefix("@").strip()
        if _NUMERIC_PEER_ID.match(stripped):
            return stripped
        raise ValueError(
            "identifier must be a Telegram username (a letter followed by 4-31 "
            "letters, digits or underscores, optionally prefixed with '@') or a "
            "numeric peer id of 5-20 digits"
        )


class CrawlRulesSchema(BaseModel):
    """Per-source Playwright selectors — the ``sources.crawl_rules`` contract.

    Mirrors the table in docs/03_INGESTION_MODULE.md. Every field is optional and
    an absent field means "fall back to Trafilatura", which is why this model
    doubles as the documented schema in OpenAPI: the selector builder in the
    dashboard reads it to render its form.

    A selector may be CSS or XPath; XPath is recognised by a leading ``/`` or
    ``(`` (``//article//h1``), everything else is treated as CSS.
    """

    model_config = ConfigDict(extra="forbid")

    list_selector: Selector | None = Field(
        default=None,
        description="Container of one item on the listing page.",
        examples=["div.news-feed article.news-item"],
    )
    link_selector: Selector | None = Field(
        default=None,
        description=(
            "Article link inside `list_selector`; relative hrefs are resolved "
            "against the listing page URL."
        ),
        examples=["a.news-item__link"],
    )
    title_selector: Selector | None = Field(
        default=None,
        description="Headline on the article page.",
        examples=["h1.article__title"],
    )
    content_selector: Selector | None = Field(
        default=None,
        description="Article body. An empty result falls back to Trafilatura.",
        examples=["div.article__body"],
    )
    date_selector: Selector | None = Field(
        default=None,
        description=(
            "Publication date; the node text and the `datetime`/`content` "
            "attributes are both tried."
        ),
        examples=["time.article__date"],
    )
    media_selector: Selector | None = Field(
        default=None,
        description=(
            "Image/video nodes; `src`, `data-src`, `srcset` and `content` are "
            "read from the DOM (the bytes are never downloaded)."
        ),
        examples=["div.article__body img"],
    )
    exclude_selectors: list[Selector] = Field(
        default_factory=list,
        description=(
            "Subtrees removed from the DOM before any text is read: banners, "
            "'read also' rails, subscribe blocks. Duplicates are collapsed."
        ),
        examples=[["div.article__banner", "aside.related-news"]],
    )
    max_items: int | None = Field(
        default=None,
        ge=1,
        le=500,
        description=(
            "Maximum articles taken from one listing pass. Defaults to "
            "INGEST_MAX_MESSAGES_PER_SOURCE when omitted."
        ),
        examples=[20],
    )
    wait_for_selector: Selector | None = Field(
        default=None,
        description=(
            "Selector awaited after navigation, for SPA pages that hydrate "
            "their content client-side."
        ),
        examples=["div.article__body"],
    )

    @field_validator(
        "list_selector",
        "link_selector",
        "title_selector",
        "content_selector",
        "date_selector",
        "media_selector",
        "wait_for_selector",
        mode="after",
    )
    @classmethod
    def _validate_selector(cls, value: str | None) -> str | None:
        return _reject_control_characters(value)

    @field_validator("exclude_selectors", mode="after")
    @classmethod
    def _validate_exclude_selectors(cls, value: list[str]) -> list[str]:
        """Reject blank/malformed entries and collapse duplicates.

        A blank entry is already refused by the ``min_length=1`` constraint on
        :data:`Selector`; this adds the control-character check and the
        de-duplication, so a list pasted twice does not make the crawler walk
        and delete the same subtree twice on every page.
        """
        for item in value:
            _reject_control_characters(item)
        return list(dict.fromkeys(value))

    def to_crawl_rules(self) -> dict[str, Any]:
        """Render the JSONB payload stored in ``sources.crawl_rules``.

        Unset selectors are omitted rather than written as ``null``: the crawler
        reads the mapping with ``_CrawlRules.from_mapping``, where a missing key
        and a null mean the same thing, and omitting them keeps the stored JSON
        readable for an operator.
        """
        return self.model_dump(mode="json", exclude_none=True)


class WebsiteTestRequest(BaseModel):
    """Dry-run request for the selector builder. Never writes to the database."""

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl = Field(
        description="Absolute http(s) URL of the page to open.",
        examples=["https://example.ru/news/2026/incident"],
    )
    crawl_rules: CrawlRulesSchema | None = Field(
        default=None,
        description=(
            "Selectors to try. Omit them to see what Trafilatura extracts on "
            "its own, which is the baseline the rules have to beat."
        ),
    )


class CrawlPreviewResponse(BaseModel):
    """Result of one dry run, mirroring `web_scraper.CrawlPreview`.

    ``warnings`` and ``strategy`` are the point of the endpoint: they tell the
    operator which selector matched nothing and whether the body came from the
    rules, from Trafilatura, or from both.
    """

    url: str = Field(
        description="Final URL after redirects, or the requested URL on failure."
    )
    title: str = Field(description="Extracted headline; empty when none was found.")
    text_preview: str = Field(
        description="Leading fragment of the extracted body, ellipsised."
    )
    text_length: int = Field(description="Length of the full extracted body, in chars.")
    published_at: datetime | None = Field(
        default=None, description="Parsed publication date, when one was found."
    )
    media_urls: list[str] = Field(
        default_factory=list, description="Absolute media URLs read from the DOM."
    )
    strategy: str = Field(
        description=(
            "Which stage produced the body: `rules`, `trafilatura`, `hybrid`, "
            "or `failed` when the page could not be read at all."
        )
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Operator-facing diagnostics: selectors that matched nothing, a "
            "suspiciously short body, a page that looks JavaScript-rendered."
        ),
    )
    duration_ms: int = Field(description="Wall-clock duration of the dry run.")
    content_html_length: int = Field(
        default=0,
        description=(
            "Length of the captured article-body HTML, in chars. Zero means the "
            "selectors produced text but no markup, so `raw_news.clean_html` "
            "would fall back to wrapped paragraphs."
        ),
    )

    @classmethod
    def from_preview(cls, preview: CrawlPreview) -> Self:
        """Adapt the frozen dataclass returned by `test_crawl_url`."""
        return cls(
            url=preview.url,
            title=preview.title,
            text_preview=preview.text_preview,
            text_length=preview.text_length,
            published_at=preview.published_at,
            media_urls=list(preview.media_urls),
            strategy=preview.strategy,
            warnings=list(preview.warnings),
            duration_ms=preview.duration_ms,
            content_html_length=preview.content_html_length,
        )


class WebsiteSourceSave(BaseModel):
    """Persist the selectors an operator confirmed in the selector builder."""

    model_config = ConfigDict(extra="forbid")

    identifier: HttpUrl = Field(
        description=(
            "Absolute listing/feed URL crawled on every pass. Stored "
            "normalised, so a bare host gains a trailing `/`."
        ),
        examples=["https://example.ru/news/"],
    )
    name: SourceName = Field(
        description="Human-readable label shown in the dashboard.",
        examples=["Example News"],
    )
    crawl_rules: CrawlRulesSchema = Field(
        description="Selectors validated with `POST /api/sources/website/test`."
    )
    is_active: bool = Field(
        default=True,
        description="Only active sources are collected by the ingestion worker.",
    )
    skip_media: bool = Field(
        default=False, description="Store no media metadata for this source."
    )
    media_strategy: MediaStrategy = Field(
        default=MediaStrategy.OCR_CHECK,
        description="How Package 3 must treat media attached to this source.",
    )


class SourceResponse(BaseModel):
    """A persisted row of ``sources``."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="Primary key.")
    source_type: SourceType = Field(description="`telegram` or `website`.")
    name: str = Field(description="Human-readable label.")
    identifier: str = Field(
        description=(
            "Channel username / peer id for Telegram sources, listing URL for "
            "website sources."
        )
    )
    is_active: bool = Field(description="Whether the worker collects this source.")
    skip_media: bool = Field(description="Whether media metadata is dropped.")
    media_strategy: MediaStrategy = Field(description="Media handling strategy.")
    crawl_rules: dict[str, Any] = Field(
        default_factory=dict,
        description="Playwright selectors; always `{}` for Telegram sources.",
    )
    last_scraped_at: datetime | None = Field(
        default=None,
        description=(
            "Set only after a successful pass, so a failed source is retried "
            "first on the next tick."
        ),
    )
    created_at: datetime = Field(description="Row creation timestamp.")


class TelegramDialogResponse(BaseModel):
    """One dialog visible to the reader account, for the admin channel picker."""

    id: str = Field(
        description="Peer id as reported by the bridge, always a string.",
    )
    title: str = Field(description="Channel or chat title.")
    username: str | None = Field(
        default=None,
        description="Public username without `@`; `None` for private channels.",
    )
    is_channel: bool = Field(
        description="Whether this is a broadcast channel rather than a group."
    )
    participants_count: int | None = Field(
        default=None,
        description="Subscriber count when the bridge reports one.",
    )

    @classmethod
    def from_dialog(cls, dialog: TelegramDialog) -> Self:
        """Adapt `telegram_ingestor.TelegramDialog`."""
        return cls(
            id=dialog.id,
            title=dialog.title,
            username=dialog.username,
            is_channel=dialog.is_channel,
            participants_count=dialog.participants_count,
        )


class IngestTriggerRequest(BaseModel):
    """Manual ingestion trigger."""

    model_config = ConfigDict(extra="forbid")

    source_ids: list[SourceId] | None = Field(
        default=None,
        max_length=_MAX_TRIGGER_SOURCES,
        description=(
            "Sources to collect. Omit or send `null` to collect every active "
            "source, which is what the scheduled run does."
        ),
        examples=[[7, 9]],
    )


class SourceIngestReportResponse(BaseModel):
    """Per-source outcome of one ingestion pass."""

    source_id: int = Field(description="`sources.id` of the collected source.")
    source_name: str = Field(description="Label of the collected source.")
    fetched: int = Field(
        description="Items the collector returned: messages, or discovered links."
    )
    inserted: int = Field(description="New `raw_news` rows created.")
    skipped_existing: int = Field(
        description="Candidates already stored, skipped by the ON CONFLICT guard."
    )
    filtered_digest: int = Field(description="Posts rejected as digests (0 tokens).")
    filtered_ad: int = Field(description="Posts rejected as advertising (0 tokens).")
    duplicates: int = Field(description="Posts closed as duplicates without new facts.")
    updates: int = Field(description="Duplicates that carried new facts.")
    new_clusters: int = Field(description="News clusters opened by this pass.")
    errors: list[str] = Field(
        default_factory=list,
        description="Non-fatal failures; a dead source never aborts the run.",
    )
    duration_ms: int = Field(description="Wall-clock duration of this source's pass.")
    ok: bool = Field(description="Whether the pass recorded no errors.")

    @classmethod
    def from_report(cls, report: SourceIngestReport) -> Self:
        """Adapt `ingestion_worker.SourceIngestReport`."""
        return cls(
            source_id=report.source_id,
            source_name=report.source_name,
            fetched=report.fetched,
            inserted=report.inserted,
            skipped_existing=report.skipped_existing,
            filtered_digest=report.filtered_digest,
            filtered_ad=report.filtered_ad,
            duplicates=report.duplicates,
            updates=report.updates,
            new_clusters=report.new_clusters,
            errors=list(report.errors),
            duration_ms=report.duration_ms,
            ok=report.ok,
        )


class IngestionRunResponse(BaseModel):
    """Aggregate report of one ingestion run."""

    summary: str = Field(
        description="One-line human summary, identical to the worker's log line."
    )
    lock_skipped: bool = Field(
        description=(
            "True when the global run lock was held, i.e. no work was done. "
            "The API surfaces this as HTTP 409 instead of a 200."
        )
    )
    started_at: datetime = Field(description="Run start, UTC.")
    finished_at: datetime = Field(description="Run end, UTC.")
    duration_ms: int = Field(description="Wall-clock duration of the whole run.")
    fetched: int = Field(description="Total items returned by the collectors.")
    inserted: int = Field(description="Total new `raw_news` rows.")
    skipped_existing: int = Field(description="Total candidates already stored.")
    filtered_digest: int = Field(description="Total digests rejected.")
    filtered_ad: int = Field(description="Total adverts rejected.")
    duplicates: int = Field(description="Total duplicates closed.")
    updates: int = Field(description="Total duplicates carrying new facts.")
    new_clusters: int = Field(description="Total clusters opened.")
    failed_sources: int = Field(description="Sources whose pass recorded an error.")
    sources: list[SourceIngestReportResponse] = Field(
        default_factory=list, description="Per-source reports, in collection order."
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Every per-source error, prefixed with its source id.",
    )
    ok: bool = Field(
        description="Whether every source was collected without an error."
    )

    @classmethod
    def from_report(cls, report: IngestionRunReport) -> Self:
        """Adapt `ingestion_worker.IngestionRunReport`."""
        return cls(
            summary=report.summary(),
            lock_skipped=report.lock_skipped,
            started_at=report.started_at,
            finished_at=report.finished_at,
            duration_ms=report.duration_ms,
            fetched=report.fetched,
            inserted=report.inserted,
            skipped_existing=report.skipped_existing,
            filtered_digest=report.filtered_digest,
            filtered_ad=report.filtered_ad,
            duplicates=report.duplicates,
            updates=report.updates,
            new_clusters=report.new_clusters,
            failed_sources=report.failed_sources,
            sources=[
                SourceIngestReportResponse.from_report(source)
                for source in report.sources
            ],
            errors=list(report.errors),
            ok=report.ok,
        )


__all__ = [
    "CrawlPreviewResponse",
    "CrawlRulesSchema",
    "IngestTriggerRequest",
    "IngestionRunResponse",
    "Selector",
    "SourceId",
    "SourceIngestReportResponse",
    "SourceName",
    "SourceResponse",
    "TelegramDialogResponse",
    "TelegramSourceCreate",
    "WebsiteSourceSave",
    "WebsiteTestRequest",
]
