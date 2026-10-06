"""ORM model registry.

Importing this package registers every mapped class on ``Base.metadata``, which
Alembic autogeneration and ``create_all`` rely on.
"""

from __future__ import annotations

from app.database import Base
from app.models.ai_provider_key import AIProviderKey
from app.models.ai_provider_model import AIProviderModel
from app.models.ai_usage import AIUsage
from app.models.article import Article
from app.models.article_journal import ArticleJournal
from app.models.audience_metric import AudienceCategoryMetric
from app.models.category_prompt import CategoryPrompt
from app.models.cluster import NewsCluster
from app.models.enums import (
    ArticleStatus,
    MediaStrategy,
    Platform,
    PlatformStatus,
    RiskLevel,
    SourceType,
    TelegramPeerKind,
    default_platform_statuses,
)
from app.models.lab_attempt import LabAttemptStatus, LabRunItem, LabStage, LabStageAttempt
from app.models.lab_run import LabRun
from app.models.raw_news import RawNews
from app.models.seo_optimization import SeoOptimization
from app.models.setting import SystemSetting
from app.models.source import Source
from app.models.tag_cache import WPTagCache
from app.models.telegram_account import TelegramAccount
from app.models.types import MutableJSONB, MutableJSONBList
from app.models.user import User
from app.models.vector import EMBEDDING_DIMENSIONS, HNSW_OPTIONS, VECTOR_OPS

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "HNSW_OPTIONS",
    "VECTOR_OPS",
    "AIProviderKey",
    "AIProviderModel",
    "AIUsage",
    "Article",
    "ArticleJournal",
    "ArticleStatus",
    "AudienceCategoryMetric",
    "Base",
    "CategoryPrompt",
    "LabAttemptStatus",
    "LabRun",
    "LabRunItem",
    "LabStage",
    "LabStageAttempt",
    "MediaStrategy",
    "MutableJSONB",
    "MutableJSONBList",
    "NewsCluster",
    "Platform",
    "PlatformStatus",
    "RawNews",
    "RiskLevel",
    "SeoOptimization",
    "Source",
    "SourceType",
    "SystemSetting",
    "TelegramAccount",
    "TelegramPeerKind",
    "User",
    "WPTagCache",
    "default_platform_statuses",
]
