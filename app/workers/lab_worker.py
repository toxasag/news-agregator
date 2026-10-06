"""ARQ worker for operator-triggered laboratory stage attempts."""

from __future__ import annotations

import logging
from typing import Any, Final

from arq import cron
from arq.connections import RedisSettings
from arq.cron import CronJob
from arq.typing import StartupShutdown, WorkerCoroutine

from app.config import settings
from app.redis_client import close_redis_pool
from app.services.lab_runner import (
    LAB_QUEUE_NAME,
    execute_stage_attempt,
    recover_unenqueued_attempts,
)

logger: Final[logging.Logger] = logging.getLogger(__name__)


async def lab_stage_task(ctx: dict[str, Any], *, attempt_id: int) -> dict[str, Any]:
    """Execute one claimed attempt and return a serialisable acknowledgement."""
    await execute_stage_attempt(attempt_id)
    return {"attempt_id": attempt_id, "completed": True}


async def recover_lab_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    """Recover database-committed attempts whose enqueue call was interrupted."""
    recovered = await recover_unenqueued_attempts()
    return {"recovered": recovered}


async def startup(ctx: dict[str, Any]) -> None:
    from app.api.logs_sse import install_log_handler  # noqa: PLC0415

    logger.info("Laboratory worker starting (env=%s)", settings.app_env)
    install_log_handler()


async def shutdown(ctx: dict[str, Any]) -> None:
    await close_redis_pool()
    logger.info("Laboratory worker stopped")


class WorkerSettings:
    """ARQ settings for the isolated manual-run queue."""

    redis_settings: Final[RedisSettings] = RedisSettings.from_dsn(str(settings.redis_url))
    queue_name: Final[str] = LAB_QUEUE_NAME
    functions: Final[tuple[WorkerCoroutine, ...]] = (lab_stage_task,)
    cron_jobs: Final[tuple[CronJob, ...]] = (
        cron(
            recover_lab_jobs,
            name="recover_lab_jobs",
            minute={0, 10, 20, 30, 40, 50},
            second=15,
            run_at_startup=False,
            max_tries=1,
            timeout=120,
        ),
    )
    on_startup: Final[StartupShutdown] = startup
    on_shutdown: Final[StartupShutdown] = shutdown
    max_jobs: Final[int] = 2
    job_timeout: Final[int] = 900
    keep_result: Final[int] = 3600
    health_check_interval: Final[int] = 30
