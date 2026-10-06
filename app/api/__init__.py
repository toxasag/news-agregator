"""HTTP routers mounted by :mod:`app.main`.

One module per bounded area of the pipeline; :mod:`app.api.ingestion` owns
source configuration and manual ingestion runs (Package 2).
"""

from __future__ import annotations

from app.api.ingestion import router as ingestion_router

__all__ = ["ingestion_router"]
