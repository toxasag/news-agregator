"""Promoted project data model for native brand journalism and ad tracking.

Represents a commercial product or service to be natively integrated into
editorial stories that match the project's target categories, with full
attribution through UTM tags and client Yandex Metrika goals.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


@dataclass(slots=True)
class ConversionGoal:
    """Target action tracked on the advertiser's website."""

    goal_id: str
    name: str
    target_cr_pct: float = 5.0
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "name": self.name,
            "target_cr_pct": self.target_cr_pct,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversionGoal:
        return cls(
            goal_id=str(data.get("goal_id", "")).strip(),
            name=str(data.get("name", "Целевое действие")).strip(),
            target_cr_pct=float(data.get("target_cr_pct", 5.0)),
            weight=float(data.get("weight", 1.0)),
        )


@dataclass(slots=True)
class PromotedProject:
    """Configured advertiser project or product for native advertorials."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    brand_name: str = ""
    target_url: str = ""
    utm_source: str = "svodkapress"
    utm_medium: str = "native_news"
    utm_campaign: str = ""
    usp: str = ""
    problem_solved: str = ""
    target_categories: list[str] = field(default_factory=list)
    target_segments: list[str] = field(default_factory=list)
    cta_text: str = ""
    client_metrika_counter_id: str = ""
    client_metrika_token: str = ""
    conversion_goals: list[ConversionGoal] = field(default_factory=list)
    is_active: bool = True
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    )

    def build_tracked_url(
        self,
        cluster_id_or_source: str = "",
        campaign_override: str = "",
    ) -> str:
        """Generate target URL with standardized UTM parameters for tracking."""
        if not self.target_url:
            return ""

        parsed = urlparse(self.target_url)
        params = parse_qs(parsed.query)

        is_platform = cluster_id_or_source in ("web", "vk", "tg", "dzen", "social")
        source = cluster_id_or_source if is_platform else self.utm_source
        clean_cid = (
            cluster_id_or_source.removeprefix("cluster_")
            if cluster_id_or_source
            else ""
        )
        default_camp = campaign_override or (
            f"cluster_{clean_cid[:8]}" if clean_cid else "direct"
        )
        campaign = campaign_override or self.utm_campaign or default_camp

        params["utm_source"] = [source or self.utm_source or "svodkapress"]
        params["utm_medium"] = [self.utm_medium or "referral"]
        params["utm_campaign"] = [campaign]
        if cluster_id_or_source and not is_platform and not campaign_override:
            params["utm_content"] = [cluster_id_or_source]

        new_query = urlencode(params, doseq=True)
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                new_query,
                parsed.fragment,
            )
        )

    @property
    def product_name(self) -> str:
        return self.name or self.brand_name

    @property
    def target_audience_segment(self) -> str:
        if self.target_segments:
            return self.target_segments[0]
        return self.usp or self.problem_solved or "Общая аудитория"

    @property
    def has_client_metrika_token(self) -> bool:
        return bool(self.client_metrika_token.strip())

    @property
    def client_metrika_token_masked(self) -> str:
        val = self.client_metrika_token.strip()
        if not val:
            return ""
        if len(val) <= 6:
            return "••••••"
        return f"••••••••{val[-4:]}"

    def to_dict(self) -> dict[str, Any]:
        """Serialize project to dictionary for storage in SystemSettings."""
        return {
            "id": self.id,
            "name": self.name,
            "product_name": self.product_name,
            "brand_name": self.brand_name,
            "target_url": self.target_url,
            "utm_source": self.utm_source,
            "utm_medium": self.utm_medium,
            "utm_campaign": self.utm_campaign,
            "usp": self.usp,
            "problem_solved": self.problem_solved,
            "target_categories": self.target_categories,
            "target_segments": self.target_segments,
            "target_audience_segment": self.target_audience_segment,
            "cta_text": self.cta_text,
            "client_metrika_counter_id": self.client_metrika_counter_id,
            "client_metrika_token": self.client_metrika_token,
            "has_client_metrika_token": self.has_client_metrika_token,
            "client_metrika_token_masked": self.client_metrika_token_masked,
            "conversion_goals": [g.to_dict() for g in self.conversion_goals],
            "is_active": self.is_active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PromotedProject:
        """Reconstruct PromotedProject from stored dictionary."""
        goals_raw = data.get("conversion_goals", [])
        goals = [
            ConversionGoal.from_dict(g) if isinstance(g, dict) else g
            for g in goals_raw
            if isinstance(g, (dict, ConversionGoal))
        ]
        counter_id = str(data.get("client_metrika_counter_id", "")).strip()
        return cls(
            id=str(data.get("id") or str(uuid.uuid4())),
            name=str(data.get("name", "")).strip(),
            brand_name=str(data.get("brand_name", "")).strip(),
            target_url=str(data.get("target_url", "")).strip(),
            utm_source=str(data.get("utm_source", "svodkapress")).strip(),
            utm_medium=str(data.get("utm_medium", "native_news")).strip(),
            utm_campaign=str(data.get("utm_campaign", "")).strip(),
            usp=str(data.get("usp", "")).strip(),
            problem_solved=str(data.get("problem_solved", "")).strip(),
            target_categories=list(data.get("target_categories", [])),
            target_segments=list(data.get("target_segments", [])),
            cta_text=str(data.get("cta_text", "")).strip(),
            client_metrika_counter_id=counter_id,
            client_metrika_token=str(data.get("client_metrika_token", "")).strip(),
            conversion_goals=goals,
            is_active=bool(data.get("is_active", True)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )


__all__ = [
    "ConversionGoal",
    "PromotedProject",
]
