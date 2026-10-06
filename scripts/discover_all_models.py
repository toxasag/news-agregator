"""Mass-discover models for every active AI provider key."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.ai_provider_key import AIProviderKey
from app.services.ai_provider_keys import ProviderKeyError, discover_models

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def discover_all_models() -> None:
    """Discover models for every active key."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AIProviderKey).where(AIProviderKey.is_active.is_(True))
        )
        keys = result.scalars().all()

        logger.info("Found %d active provider key(s)", len(keys))

        for key in keys:
            logger.info(
                "Discovering models for key #%d (%s)",
                key.id,
                key.provider_name,
            )
            try:
                models = await discover_models(session, key.id)
                logger.info("Discovered %d model(s)", len(models))
            except (ProviderKeyError, Exception) as error:  # noqa: BLE001
                logger.error("Model discovery failed for key #%d: %s", key.id, error)

        await session.commit()
        logger.info("Model discovery completed")


if __name__ == "__main__":
    asyncio.run(discover_all_models())
