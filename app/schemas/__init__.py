"""Pydantic v2 request/response contracts for the HTTP layer.

Kept separate from :mod:`app.models` on purpose: the ORM describes what is
stored, these schemas describe what crosses the network boundary. Services and
workers never import this package, so the pipeline stays usable without FastAPI.
"""

from __future__ import annotations

from app.schemas.ingestion import (
    CrawlPreviewResponse,
    CrawlRulesSchema,
    IngestionRunResponse,
    IngestTriggerRequest,
    SourceIngestReportResponse,
    SourceResponse,
    TelegramDialogResponse,
    TelegramSourceCreate,
    WebsiteSourceSave,
    WebsiteTestRequest,
)

__all__ = [
    "CrawlPreviewResponse",
    "CrawlRulesSchema",
    "IngestTriggerRequest",
    "IngestionRunResponse",
    "SourceIngestReportResponse",
    "SourceResponse",
    "TelegramDialogResponse",
    "TelegramSourceCreate",
    "WebsiteSourceSave",
    "WebsiteTestRequest",
]
