# ruff: noqa: RUF001, BLE001, E501, PLR0912, PLR0915
"""Service for managing promoted advertiser projects and tracking client conversions.

Stores project profiles in SystemSetting('promoted_projects') and queries
the client's Yandex Metrika Reporting API for UTM-tagged visits and goal conversions.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import SecretBox, get_secret_box
from app.models.promoted_project import PromotedProject
from app.models.setting import SystemSetting

logger: Final[logging.Logger] = logging.getLogger(__name__)

PROMOTED_PROJECTS_SETTING_KEY: Final[str] = "promoted_projects_registry"
METRIKA_API_URL: Final[str] = "https://api-metrika.yandex.net/stat/v1/data"


def _get_box() -> SecretBox | None:
    try:
        return get_secret_box()
    except (ValueError, TypeError):
        return None


def _encrypt_val(value: str) -> str:
    if not value:
        return ""
    box = _get_box()
    return box.encrypt(value) if box is not None else value


def _decrypt_val(value: str) -> str:
    if not value:
        return ""
    box = _get_box()
    return box.decrypt(value) if box is not None else value


class PromotedProjectsService:
    """Manages advertiser projects and retrieves client-side conversion metrics."""

    @classmethod
    async def get_all_projects(cls, session: AsyncSession) -> list[PromotedProject]:
        """Fetch all configured promoted projects from the database."""
        try:
            stmt = select(SystemSetting).where(
                SystemSetting.key == PROMOTED_PROJECTS_SETTING_KEY
            )
            res = await session.execute(stmt)
            setting = res.scalar_one_or_none()
            if not setting or not isinstance(setting.value, list):
                return []

            projects: list[PromotedProject] = []
            for item in setting.value:
                if isinstance(item, dict):
                    # Decrypt client metrika token if stored
                    token_enc = item.get("client_metrika_token", "")
                    if token_enc:
                        item["client_metrika_token"] = _decrypt_val(token_enc)
                    projects.append(PromotedProject.from_dict(item))
            return projects
        except SQLAlchemyError as exc:
            logger.warning("Failed to load promoted projects: %s", exc)
            return []

    @classmethod
    async def get_project_by_id(
        cls, session: AsyncSession, project_id: str
    ) -> PromotedProject | None:
        """Find a single promoted project by its ID."""
        projects = await cls.get_all_projects(session)
        for p in projects:
            if p.id == project_id:
                return p
        return None

    @classmethod
    async def save_project(
        cls, session: AsyncSession, project: PromotedProject
    ) -> PromotedProject:
        """Upsert a promoted project into system settings."""
        projects = await cls.get_all_projects(session)
        project.updated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

        found = False
        updated_list: list[dict[str, Any]] = []
        for p in projects:
            if p.id == project.id:
                found = True
                p_dict = project.to_dict()
                if p_dict.get("client_metrika_token"):
                    p_dict["client_metrika_token"] = _encrypt_val(
                        p_dict["client_metrika_token"]
                    )
                updated_list.append(p_dict)
            else:
                p_dict = p.to_dict()
                if p_dict.get("client_metrika_token"):
                    p_dict["client_metrika_token"] = _encrypt_val(
                        p_dict["client_metrika_token"]
                    )
                updated_list.append(p_dict)

        if not found:
            p_dict = project.to_dict()
            if p_dict.get("client_metrika_token"):
                p_dict["client_metrika_token"] = _encrypt_val(
                    p_dict["client_metrika_token"]
                )
            updated_list.append(p_dict)

        try:
            stmt = (
                pg_insert(SystemSetting)
                .values(
                    key=PROMOTED_PROJECTS_SETTING_KEY,
                    value=updated_list,
                    description="Каталог рекламируемых проектов и счетчиков конверсий",
                )
                .on_conflict_do_update(
                    index_elements=[SystemSetting.key],
                    set_={"value": updated_list, "updated_at": datetime.now(UTC)},
                )
            )
            await session.execute(stmt)
            await session.commit()
        except SQLAlchemyError as exc:
            logger.error("Failed to persist promoted project: %s", exc)
            if hasattr(session, "rollback"):
                await session.rollback()
            raise

        await cls.sync_to_wordpress(session)
        return project

    @classmethod
    async def delete_project(cls, session: AsyncSession, project_id: str) -> bool:
        """Remove a project from the catalog."""
        projects = await cls.get_all_projects(session)
        remaining = [p for p in projects if p.id != project_id]
        if len(remaining) == len(projects):
            return False

        serialized = []
        for p in remaining:
            d = p.to_dict()
            if d.get("client_metrika_token"):
                d["client_metrika_token"] = _encrypt_val(d["client_metrika_token"])
            serialized.append(d)

        try:
            stmt = (
                pg_insert(SystemSetting)
                .values(
                    key=PROMOTED_PROJECTS_SETTING_KEY,
                    value=serialized,
                    description="Каталог рекламируемых проектов и счетчиков конверсий",
                )
                .on_conflict_do_update(
                    index_elements=[SystemSetting.key],
                    set_={"value": serialized, "updated_at": datetime.now(UTC)},
                )
            )
            await session.execute(stmt)
            await session.commit()
            await cls.sync_to_wordpress(session)
            return True
        except SQLAlchemyError as exc:
            logger.error("Failed to delete promoted project: %s", exc)
            if hasattr(session, "rollback"):
                await session.rollback()
            return False

    @classmethod
    async def sync_to_wordpress(cls, session: AsyncSession) -> bool:
        """Push active promoted projects to WordPress option `svodka_promoted_projects` over SSH."""
        import json  # noqa: PLC0415
        import shlex  # noqa: PLC0415

        import asyncssh  # noqa: PLC0415

        from app.services.wp_connection import load_wp_connection  # noqa: PLC0415

        try:
            projects = await cls.get_all_projects(session)
            conn = await load_wp_connection(session)
            if not conn.has_ssh_credentials:
                return False

            payload = [
                {
                    "id": p.id,
                    "name": p.name,
                    "brand_name": p.brand_name or p.name,
                    "target_url": p.target_url,
                    "tracked_url": p.build_tracked_url("web", "feed_rotation"),
                    "usp": p.usp,
                    "problem_solved": p.problem_solved,
                    "cta_text": p.cta_text,
                    "is_active": p.is_active,
                }
                for p in projects
            ]
            json_str = json.dumps(payload, ensure_ascii=False)
            wp_root = shlex.quote(conn.wp_document_root)
            json_arg = shlex.quote(json_str)
            cmd = f"wp --allow-root --path={wp_root} option update svodka_promoted_projects {json_arg}"

            client_keys: list[Any] = []
            if conn.ssh_private_key.strip():
                client_keys = [asyncssh.import_private_key(conn.ssh_private_key)]
            elif conn.ssh_key_path.strip():
                client_keys = [conn.ssh_key_path.strip()]

            async with asyncssh.connect(
                conn.ssh_host,
                port=conn.ssh_port or 22,
                username=conn.ssh_user or "root",
                client_keys=client_keys,
                known_hosts=None,
                connect_timeout=10.0,
            ) as ssh_conn:
                await ssh_conn.run(cmd, timeout=15.0)
            return True
        except Exception as exc:
            logger.warning("Failed to sync promoted projects to WordPress: %s", exc)
            return False

    @classmethod
    def get_matching_projects(
        cls,
        projects: Sequence[PromotedProject],
        category_slug: str,
        *,
        is_brand_safe: bool = True,
    ) -> list[PromotedProject]:
        """Filter projects eligible for this category and enforce Brand Safety."""
        if not is_brand_safe:
            return []

        matched = []
        for p in projects:
            if not p.is_active:
                continue
            # If no target categories set, project fits any brand-safe category
            if not p.target_categories or category_slug in p.target_categories:
                matched.append(p)
        return matched

    @classmethod
    def build_promo_card(
        cls,
        project: PromotedProject,
        platform: str = "web",
        campaign: str = "",
    ) -> str:
        """Generate high-CTR promotional HTML markup tailored for the platform."""
        target_url = project.build_tracked_url(platform, campaign or "article_callout")

        if platform == "dzen":
            return (
                '<blockquote class="dzen-native-banner">\n'
                f'<p><b>🚀 Возможности для авторов | Партнерский материал</b></p>\n'
                f'<p><b>{project.name}: масштабирование контента с помощью нейросетей</b></p>\n'
                f'<p>Создатели шопсов и клипов все чаще автоматизируют производство видео с помощью искусственного интеллекта. '
                f'В клубе <b><a href="{target_url}">{project.name}</a></b> авторы получают проверенные шаблоны вирусных сценариев, '
                f'промпты для видео и бесплатный доступ к передовым нейросетям без ограничений и зарубежных карт.</p>\n'
                f'<p>👉 <b><a href="{target_url}">Подключиться к {project.name} и получить бесплатный доступ →</a></b></p>\n'
                '</blockquote>'
            )

        headline_text = (
            f"{project.name}: бесплатный доступ к лучшим нейросетям для создателей контента"
            if "freeapi" in (project.id or "").lower() or "freeapi" in (project.name or "").lower()
            else f"{project.name} — {project.problem_solved or 'специальное предложение для читателей Сводка Пресс'}"
        )
        body_text = (
            project.usp
            or (
                f"Участники закрытого клуба <strong>{project.name}</strong> получают проверенные алгоритмы создания вирусных сценариев, "
                "готовые промпты для клипов и шопсов, а также доступ к пулам бесплатных токенов для запуска передовых нейросетей прямо из России без зарубежных карт и VPN."
            )
        )
        btn_label = project.cta_text if (project.cta_text and len(project.cta_text) <= 42) else f"Подключиться к {project.brand_name or project.name}"

        # Default: Modern dark gradient Apple-style card for WordPress and Web
        return (
            '<div class="svodka-promo-card" style="margin: 28px 0; padding: 24px; '
            'background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); '
            'border-radius: 16px; border: 1px solid rgba(255,255,255,0.12); '
            'box-shadow: 0 12px 28px -6px rgba(0,0,0,0.25); color: #ffffff;">\n'
            '  <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; flex-wrap: wrap; gap: 8px;">\n'
            '    <span style="display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; background: rgba(0, 113, 227, 0.25); color: #38bdf8; padding: 5px 12px; border-radius: 20px; border: 1px solid rgba(56, 189, 248, 0.35);">\n'
            '      🚀 Рекомендация редакции\n'
            '    </span>\n'
            '    <span style="font-size: 11px; color: #94a3b8; font-weight: 500;">Партнерский материал</span>\n'
            '  </div>\n'
            f'  <h3 style="margin: 0 0 10px 0; font-size: 19px; font-weight: 700; color: #ffffff; line-height: 1.35;">\n'
            f'    {headline_text}\n'
            '  </h3>\n'
            '  <p style="margin: 0 0 18px 0; font-size: 14px; line-height: 1.6; color: #cbd5e1;">\n'
            f'    {body_text}\n'
            '  </p>\n'
            '  <div style="display: flex; align-items: center; gap: 14px; flex-wrap: wrap;">\n'
            f'    <a href="{target_url}" target="_blank" rel="noopener" style="display: inline-flex; align-items: center; gap: 8px; background: #0071e3; color: #ffffff; text-decoration: none; font-weight: 600; font-size: 14px; padding: 11px 22px; border-radius: 10px; transition: background 0.2s ease; box-shadow: 0 4px 14px rgba(0,113,227,0.35);">\n'
            f'      <span>{btn_label}</span>\n'
            '      <span style="font-size: 16px;">→</span>\n'
            '    </a>\n'
            '    <span style="font-size: 12px; color: #94a3b8;">100% бесплатно для резидентов РФ</span>\n'
            '  </div>\n'
            '</div>'
        )

    @classmethod
    async def upsert_project(
        cls, session: AsyncSession, project: PromotedProject
    ) -> PromotedProject:
        """Alias for save_project used by dashboard endpoints."""
        return await cls.save_project(session, project)

    @classmethod
    async def discover_client_metrika_goals(
        cls,
        counter_id: str,
        token: str,
    ) -> dict[str, Any]:
        """Fetch counter details and configured conversion goals from Yandex Metrika Management API."""
        cid = counter_id.strip()
        tok = token.strip()
        if not cid or not tok:
            return {
                "ok": False,
                "message": "Укажите номер счётчика и OAuth-токен Метрики рекламодателя.",
                "counter_name": "",
                "site_domain": "",
                "goals": [],
            }

        headers = {
            "Authorization": f"OAuth {tok}",
            "Accept": "application/json",
        }
        counter_url = f"https://api-metrika.yandex.net/management/v1/counter/{cid}"
        goals_url = f"https://api-metrika.yandex.net/management/v1/counter/{cid}/goals"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                c_res = await client.get(counter_url, headers=headers)
                g_res = await client.get(goals_url, headers=headers)

                if c_res.status_code in (401, 403) or g_res.status_code in (401, 403):
                    code = c_res.status_code if c_res.status_code in (401, 403) else g_res.status_code
                    return {
                        "ok": False,
                        "message": (
                            f"Ошибка доступа ({code}): токен не имеет прав на чтение счётчика #{cid}. "
                            "Выдайте гостевой доступ к счётчику или укажите OAuth-токен аккаунта рекламодателя."
                        ),
                        "counter_name": "",
                        "site_domain": "",
                        "goals": [],
                    }

                counter_name = ""
                site_domain = ""
                if c_res.status_code == 200:
                    c_data = c_res.json().get("counter", {})
                    counter_name = str(c_data.get("name") or "")
                    site_domain = str(c_data.get("site") or "")

                goals_list: list[dict[str, Any]] = []
                if g_res.status_code == 200:
                    raw_goals = g_res.json().get("goals", [])
                    for g in raw_goals:
                        if not isinstance(g, dict):
                            continue
                        gid = str(g.get("id") or "").strip()
                        gname = str(g.get("name") or "Целевое действие").strip()
                        gtype = str(g.get("type") or "action").strip()
                        if gid:
                            goals_list.append(
                                {
                                    "goal_id": gid,
                                    "name": gname,
                                    "type": gtype,
                                    "target_cr_pct": 5.0,
                                    "weight": 1.0,
                                }
                            )

                if c_res.status_code != 200 and g_res.status_code != 200:
                    return {
                        "ok": False,
                        "message": f"API Метрики вернул статус {c_res.status_code} для счётчика #{cid}.",
                        "counter_name": "",
                        "site_domain": "",
                        "goals": [],
                    }

                site_info = f" ({site_domain})" if site_domain else ""
                return {
                    "ok": True,
                    "message": (
                        f"Счётчик #{cid}{site_info} подключён! "
                        f"Обнаружено целей конверсий: {len(goals_list)}."
                    ),
                    "counter_name": counter_name,
                    "site_domain": site_domain,
                    "goals": goals_list,
                }
        except Exception as exc:
            logger.warning("Failed to discover client Metrika goals for %s: %s", cid, exc)
            return {
                "ok": False,
                "message": f"Сетевая ошибка при обращении к API Метрики: {exc}",
                "counter_name": "",
                "site_domain": "",
                "goals": [],
            }

    @staticmethod
    def extract_attribution_tokens(project: PromotedProject) -> list[str]:
        """Extract referral slugs or unique tokens from project target_url for Metrika attribution."""
        tokens: list[str] = []
        if not project.target_url:
            return tokens

        try:
            parsed = urlparse(project.target_url)
            # 1. Query parameters (e.g. ?ref=tdCxTE7k, ?r=tdCxTE7k, ?code=...)
            qs = parse_qs(parsed.query)
            for key in (
                "ref",
                "r",
                "code",
                "partner",
                "aff",
                "partner_id",
                "inviter",
                "start",
            ):
                for val in qs.get(key, []):
                    val_clean = val.strip()
                    if val_clean and len(val_clean) >= 3 and val_clean not in tokens:
                        tokens.append(val_clean)

            # 2. Path segments like /r/<slug> or /ref/<slug> or /invite/<slug>
            path_parts = [p.strip() for p in parsed.path.split("/") if p.strip()]
            for idx, part in enumerate(path_parts):
                if (
                    part in ("r", "ref", "invite", "partner", "p", "go")
                    and idx + 1 < len(path_parts)
                ):
                    slug = path_parts[idx + 1]
                    if slug and len(slug) >= 3 and slug not in tokens:
                        tokens.append(slug)
                elif (
                    idx == len(path_parts) - 1
                    and len(part) >= 4
                    and "." not in part
                    and part not in tokens
                ):
                    tokens.append(part)
        except Exception as exc:
            logger.debug("Failed parsing target_url attribution tokens: %s", exc)
        return tokens

    @classmethod
    async def fetch_client_metrika_stats(
        cls,
        project: PromotedProject,
        cluster_id: str = "",
        *,
        default_token: str = "",
        days: int = 28,
        include_counter_totals: bool = False,
    ) -> dict[str, Any]:
        """Fetch actual visits, bounce rate, and conversions on the client website.

        Queries the client's Yandex Metrika Reporting API filtered by
        ``utm_source`` (and optionally ``utm_campaign`` if ``cluster_id`` is provided).
        When ``include_counter_totals=True``, also fetches counter-wide visits and goal
        reaches to verify that the counter and its goals are firing properly.
        """
        counter_id = project.client_metrika_counter_id.strip()
        token = project.client_metrika_token.strip() or default_token.strip()
        token_source = (
            "client"
            if project.client_metrika_token.strip()
            else ("shared_yandex" if default_token.strip() else "none")
        )

        if not counter_id or not token:
            reason = (
                "Не указан номер счётчика Яндекс.Метрики рекламодателя"
                if not counter_id
                else "Не указан OAuth-токен Метрики (задайте токен рекламодателя в окне «Метрика и цели» или общий токен в Настройках)"
            )
            return {
                "available": False,
                "counter_id": counter_id,
                "token_source": token_source,
                "reason": reason,
                "visits": 0,
                "users": 0,
                "bounce_rate": 0.0,
                "bounce_rate_pct": 0.0,
                "avg_duration_sec": 0.0,
                "conversions_count": 0,
                "cr_pct": 0.0,
                "goals_breakdown": [
                    {
                        "goal_id": g.goal_id,
                        "name": g.name,
                        "reaches": 0,
                        "all_site_reaches": 0,
                        "cr_pct": 0.0,
                        "target_cr_pct": g.target_cr_pct,
                        "weight": g.weight,
                    }
                    for g in project.conversion_goals
                    if g.goal_id
                ],
                "all_site_visits": 0,
                "all_site_conversions": 0,
            }

        end_date = date.today()
        start_date = end_date - timedelta(days=max(days, 1))

        campaign_pattern = f"cluster_{cluster_id[:8]}" if cluster_id else ""
        source_name = project.utm_source or "svodkapress"

        # Build filter condition for visits from our media (UTM, referral slug/link, and referer)
        sources = [
            source_name,
            "svodkapress",
            "web",
            "dzen",
            "tg",
            "vk",
            "ok",
            "tenchat",
            "social",
        ]
        seen_src: set[str] = set()
        u_sources = [s for s in sources if s and not (s in seen_src or seen_src.add(s))]
        utm_conditions = [f"ym:s:UTMSource=='{s}'" for s in u_sources]

        # Extract referral tokens from target_url (e.g. tdCxTE7k from https://freeapiai.ru/r/tdCxTE7k)
        tokens = cls.extract_attribution_tokens(project)
        token_conditions = [f"ym:s:startURLPathFull=@'{t}'" for t in tokens]

        # Referers from our network domains
        referer_conditions = [
            "ym:s:referer=@'svodka.press'",
            "ym:s:referer=@'svodkanews'",
        ]

        combined_conditions = utm_conditions + token_conditions + referer_conditions
        filter_expr = "(" + " OR ".join(combined_conditions) + ")"

        if campaign_pattern:
            filter_expr = f"({filter_expr} AND ym:s:UTMCampaign=='{campaign_pattern}')"

        params = {
            "ids": counter_id,
            "date1": start_date.isoformat(),
            "date2": end_date.isoformat(),
            "metrics": "ym:s:visits,ym:s:users,ym:s:bounceRate,ym:s:avgVisitDurationSeconds",
            "filters": filter_expr,
        }
        headers = {
            "Authorization": f"OAuth {token}",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(METRIKA_API_URL, params=params, headers=headers)
                if res.status_code != 200:
                    logger.warning(
                        "Client Metrika API returned HTTP %d for counter %s",
                        res.status_code,
                        counter_id,
                    )
                    reason_msg = (
                        f"Нет доступа к счётчику #{counter_id} (HTTP {res.status_code}). "
                        "Выдайте гостевой доступ на чтение или укажите персональный OAuth-токен рекламодателя."
                        if res.status_code in (401, 403)
                        else f"Ошибка Метрики рекламодателя: HTTP {res.status_code}"
                    )
                    return {
                        "available": False,
                        "counter_id": counter_id,
                        "token_source": token_source,
                        "status_code": res.status_code,
                        "reason": reason_msg,
                        "visits": 0,
                        "users": 0,
                        "bounce_rate": 0.0,
                        "bounce_rate_pct": 0.0,
                        "avg_duration_sec": 0.0,
                        "conversions_count": 0,
                        "cr_pct": 0.0,
                        "goals_breakdown": [
                            {
                                "goal_id": g.goal_id,
                                "name": g.name,
                                "reaches": 0,
                                "all_site_reaches": 0,
                                "cr_pct": 0.0,
                                "target_cr_pct": g.target_cr_pct,
                                "weight": g.weight,
                            }
                            for g in project.conversion_goals
                            if g.goal_id
                        ],
                        "all_site_visits": 0,
                        "all_site_conversions": 0,
                    }

                data = res.json()
                totals = data.get("totals", [])
                visits = int(totals[0]) if len(totals) > 0 else 0
                users = int(totals[1]) if len(totals) > 1 else 0
                bounce_pct = float(totals[2]) if len(totals) > 2 else 0.0
                duration_sec = float(totals[3]) if len(totals) > 3 else 0.0

                all_site_visits = 0
                if include_counter_totals:
                    all_params = {
                        "ids": counter_id,
                        "date1": start_date.isoformat(),
                        "date2": end_date.isoformat(),
                        "metrics": "ym:s:visits,ym:s:users",
                    }
                    all_res = await client.get(
                        METRIKA_API_URL, params=all_params, headers=headers
                    )
                    if all_res.status_code == 200:
                        all_t = all_res.json().get("totals", [])
                        all_site_visits = int(all_t[0]) if all_t else 0

                # Also query conversion goals if defined
                total_conversions = 0
                all_site_conversions = 0
                goals_breakdown: list[dict[str, Any]] = []
                if project.conversion_goals:
                    for goal in project.conversion_goals:
                        if not goal.goal_id:
                            continue
                        reaches = 0
                        all_reaches = 0
                        if visits > 0:
                            g_params = {
                                "ids": counter_id,
                                "date1": start_date.isoformat(),
                                "date2": end_date.isoformat(),
                                "metrics": f"ym:s:goal{goal.goal_id}reaches",
                                "filters": filter_expr,
                            }
                            g_res = await client.get(
                                METRIKA_API_URL, params=g_params, headers=headers
                            )
                            if g_res.status_code == 200:
                                g_totals = g_res.json().get("totals", [0])
                                reaches = int(g_totals[0]) if g_totals else 0
                                total_conversions += reaches

                        if include_counter_totals:
                            ga_params = {
                                "ids": counter_id,
                                "date1": start_date.isoformat(),
                                "date2": end_date.isoformat(),
                                "metrics": f"ym:s:goal{goal.goal_id}reaches",
                            }
                            ga_res = await client.get(
                                METRIKA_API_URL, params=ga_params, headers=headers
                            )
                            if ga_res.status_code == 200:
                                ga_totals = ga_res.json().get("totals", [0])
                                all_reaches = int(ga_totals[0]) if ga_totals else 0
                                all_site_conversions += all_reaches

                        goal_cr = (
                            round((reaches / visits) * 100.0, 1) if visits > 0 else 0.0
                        )
                        goals_breakdown.append(
                            {
                                "goal_id": goal.goal_id,
                                "name": goal.name,
                                "reaches": reaches,
                                "all_site_reaches": all_reaches,
                                "cr_pct": goal_cr,
                                "target_cr_pct": goal.target_cr_pct,
                                "weight": goal.weight,
                            }
                        )

                # Query platform performance breakdown via ym:s:UTMSource
                platform_stats: dict[str, dict[str, Any]] = {
                    "web": {
                        "visits": 0,
                        "users": 0,
                        "bounce_rate_pct": 0.0,
                        "conversions": 0,
                        "cr_pct": 0.0,
                    },
                    "dzen": {
                        "visits": 0,
                        "users": 0,
                        "bounce_rate_pct": 0.0,
                        "conversions": 0,
                        "cr_pct": 0.0,
                    },
                    "tg": {
                        "visits": 0,
                        "users": 0,
                        "bounce_rate_pct": 0.0,
                        "conversions": 0,
                        "cr_pct": 0.0,
                    },
                    "vk": {
                        "visits": 0,
                        "users": 0,
                        "bounce_rate_pct": 0.0,
                        "conversions": 0,
                        "cr_pct": 0.0,
                    },
                    "ok": {
                        "visits": 0,
                        "users": 0,
                        "bounce_rate_pct": 0.0,
                        "conversions": 0,
                        "cr_pct": 0.0,
                    },
                    "tenchat": {
                        "visits": 0,
                        "users": 0,
                        "bounce_rate_pct": 0.0,
                        "conversions": 0,
                        "cr_pct": 0.0,
                    },
                }

                try:
                    src_params = {
                        "ids": counter_id,
                        "date1": start_date.isoformat(),
                        "date2": end_date.isoformat(),
                        "metrics": "ym:s:visits,ym:s:users,ym:s:bounceRate",
                        "dimensions": "ym:s:UTMSource",
                        "filters": filter_expr,
                        "limit": "20",
                    }
                    src_res = await client.get(
                        METRIKA_API_URL, params=src_params, headers=headers
                    )
                    if src_res.status_code == 200:
                        for row in src_res.json().get("data", []):
                            dims = row.get("dimensions", [])
                            mets = row.get("metrics", [])
                            if not dims or not mets:
                                continue
                            raw_src = str(dims[0].get("name", "")).lower().strip()
                            key = (
                                "web"
                                if raw_src in ("svodkapress", "web", "website")
                                else raw_src
                            )
                            if key in platform_stats:
                                p_v = int(float(mets[0]))
                                p_u = int(float(mets[1]))
                                p_b = round(float(mets[2]), 1)
                                platform_stats[key]["visits"] += p_v
                                platform_stats[key]["users"] += p_u
                                platform_stats[key]["bounce_rate_pct"] = p_b

                    # If some or all visits lack UTMSource (e.g. came via referral link / direct landing),
                    # inspect referer dimension to break down by platform (Dzen, VK, TG, OK, TenChat)
                    tot_plat_visits = sum(p["visits"] for p in platform_stats.values())
                    if tot_plat_visits < visits:
                        try:
                            ref_params = {
                                "ids": counter_id,
                                "date1": start_date.isoformat(),
                                "date2": end_date.isoformat(),
                                "metrics": "ym:s:visits,ym:s:users,ym:s:bounceRate",
                                "dimensions": "ym:s:referer",
                                "filters": filter_expr,
                                "limit": "20",
                            }
                            ref_res = await client.get(
                                METRIKA_API_URL, params=ref_params, headers=headers
                            )
                            if ref_res.status_code == 200:
                                for row in ref_res.json().get("data", []):
                                    dims = row.get("dimensions", [])
                                    mets = row.get("metrics", [])
                                    if not dims or not mets:
                                        continue
                                    raw_ref = str(dims[0].get("name", "") or "").lower()
                                    r_v = int(float(mets[0]))
                                    r_u = int(float(mets[1]))
                                    r_b = round(float(mets[2]), 1)
                                    assigned_key = ""
                                    if "dzen" in raw_ref:
                                        assigned_key = "dzen"
                                    elif "vk.com" in raw_ref:
                                        assigned_key = "vk"
                                    elif "t.me" in raw_ref or "telegram" in raw_ref:
                                        assigned_key = "tg"
                                    elif "ok.ru" in raw_ref:
                                        assigned_key = "ok"
                                    elif "tenchat" in raw_ref:
                                        assigned_key = "tenchat"

                                    if (
                                        assigned_key
                                        and platform_stats[assigned_key]["visits"] == 0
                                    ):
                                        platform_stats[assigned_key]["visits"] += r_v
                                        platform_stats[assigned_key]["users"] += r_u
                                        platform_stats[assigned_key]["bounce_rate_pct"] = (
                                            r_b
                                        )
                        except Exception as ref_err:
                            logger.debug("Failed to query referer breakdown: %s", ref_err)

                    # Any remaining unattributed visits from our referral link/domain are assigned to web
                    tot_plat_visits = sum(p["visits"] for p in platform_stats.values())
                    if tot_plat_visits < visits:
                        rem_v = visits - tot_plat_visits
                        platform_stats["web"]["visits"] += rem_v
                        rem_u = max(
                            0,
                            users
                            - sum(
                                p["users"]
                                for k, p in platform_stats.items()
                                if k != "web"
                            ),
                        )
                        platform_stats["web"]["users"] += rem_u
                        if platform_stats["web"]["bounce_rate_pct"] == 0.0:
                            platform_stats["web"]["bounce_rate_pct"] = round(bounce_pct, 1)

                    # If conversions exist, query goal reaches per platform
                    if project.conversion_goals and any(
                        p["visits"] > 0 for p in platform_stats.values()
                    ):
                        goal_metrics_str = ",".join(
                            f"ym:s:goal{g.goal_id}reaches"
                            for g in project.conversion_goals
                            if g.goal_id
                        )
                        if goal_metrics_str:
                            g_src_params = {
                                "ids": counter_id,
                                "date1": start_date.isoformat(),
                                "date2": end_date.isoformat(),
                                "metrics": goal_metrics_str,
                                "dimensions": "ym:s:UTMSource",
                                "filters": filter_expr,
                                "limit": "20",
                            }
                            g_src_res = await client.get(
                                METRIKA_API_URL, params=g_src_params, headers=headers
                            )
                            if g_src_res.status_code == 200:
                                for row in g_src_res.json().get("data", []):
                                    dims = row.get("dimensions", [])
                                    mets = row.get("metrics", [])
                                    if not dims or not mets:
                                        continue
                                    raw_src = (
                                        str(dims[0].get("name", "")).lower().strip()
                                    )
                                    key = (
                                        "web"
                                        if raw_src in ("svodkapress", "web", "website")
                                        else raw_src
                                    )
                                    if key in platform_stats:
                                        tot_g_reaches = sum(
                                            int(float(m)) for m in mets
                                        )
                                        platform_stats[key]["conversions"] += (
                                            tot_g_reaches
                                        )
                                        v = platform_stats[key]["visits"]
                                        if v > 0:
                                            platform_stats[key]["cr_pct"] = round(
                                                (tot_g_reaches / v) * 100.0, 1
                                            )

                            # If some conversions were not attributed to UTM source, assign remaining to web
                            tot_plat_conv = sum(
                                p["conversions"] for p in platform_stats.values()
                            )
                            if tot_plat_conv < total_conversions:
                                rem_conv = total_conversions - tot_plat_conv
                                platform_stats["web"]["conversions"] += rem_conv
                                if platform_stats["web"]["visits"] > 0:
                                    platform_stats["web"]["cr_pct"] = round(
                                        (
                                            platform_stats["web"]["conversions"]
                                            / platform_stats["web"]["visits"]
                                        )
                                        * 100.0,
                                        1,
                                    )
                except Exception as src_err:
                    logger.debug("Failed to query platform stats: %s", src_err)

                cr_pct = (
                    round((total_conversions / visits) * 100.0, 1) if visits > 0 else 0.0
                )

                return {
                    "available": True,
                    "counter_id": counter_id,
                    "token_source": token_source,
                    "visits": visits,
                    "users": users,
                    "bounce_rate": round(bounce_pct / 100.0, 3),
                    "bounce_rate_pct": round(bounce_pct, 1),
                    "avg_duration_sec": round(duration_sec, 0),
                    "conversions_count": total_conversions,
                    "cr_pct": cr_pct,
                    "goals_breakdown": goals_breakdown,
                    "all_site_visits": all_site_visits,
                    "all_site_conversions": all_site_conversions,
                    "platform_stats": platform_stats,
                }
        except Exception as exc:
            logger.warning("Error fetching client Metrika stats: %s", exc)
            return {
                "available": False,
                "counter_id": counter_id,
                "token_source": token_source,
                "reason": str(exc),
                "visits": 0,
                "users": 0,
                "bounce_rate": 0.0,
                "bounce_rate_pct": 0.0,
                "avg_duration_sec": 0.0,
                "conversions_count": 0,
                "cr_pct": 0.0,
                "goals_breakdown": [],
                "all_site_visits": 0,
                "all_site_conversions": 0,
            }

    @classmethod
    def parse_goals_from_form(cls, form_data: Any) -> list[Any]:
        """Parse ConversionGoal list submitted from HTMX form arrays."""
        from app.models.promoted_project import ConversionGoal  # noqa: PLC0415

        goal_ids = form_data.getlist("goal_ids") if hasattr(form_data, "getlist") else []
        goal_names = (
            form_data.getlist("goal_names") if hasattr(form_data, "getlist") else []
        )
        goal_crs = (
            form_data.getlist("goal_target_crs") if hasattr(form_data, "getlist") else []
        )

        goals: list[ConversionGoal] = []
        seen_ids: set[str] = set()
        for idx, raw_id in enumerate(goal_ids):
            gid = str(raw_id or "").strip()
            if not gid or gid in seen_ids:
                continue
            seen_ids.add(gid)
            gname = (
                str(goal_names[idx] or "").strip()
                if idx < len(goal_names)
                else "Целевое действие"
            )
            if not gname:
                gname = f"Цель #{gid}"
            raw_cr = goal_crs[idx] if idx < len(goal_crs) else "5.0"
            try:
                t_cr = float(str(raw_cr).replace(",", ".").strip() or 5.0)
            except ValueError:
                t_cr = 5.0
            goals.append(
                ConversionGoal(
                    goal_id=gid,
                    name=gname,
                    target_cr_pct=max(0.1, min(t_cr, 100.0)),
                    weight=1.0,
                )
            )
        return goals

    @classmethod
    async def collect_advertiser_campaigns_report(
        cls,
        session: AsyncSession,
        *,
        days: int = 28,
        audience_categories: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        """Build Closed-Loop Advertiser & Metrika Goals report for /ads and /seo.

        Connects each advertiser's Yandex Metrika counter, conversion goals,
        integrated articles in PostgreSQL, and Campaign Quality Index (CQI)
        so /seo can learn which categories and topics drive target visits and
        goal completions.
        """
        from app.models.article import Article  # noqa: PLC0415
        from app.services.aevi_scoring import evaluate_campaign_quality  # noqa: PLC0415
        from app.services.categories import CATEGORY_TITLES  # noqa: PLC0415
        from app.services.yandex_connection import load_yandex_connection  # noqa: PLC0415

        projects = await cls.get_all_projects(session)
        yandex_conn = await load_yandex_connection(session)
        shared_token = yandex_conn.metrika_token.strip()

        # Map audience category metrics by slug if available
        cat_metrics_by_slug: dict[str, dict[str, Any]] = {}
        if audience_categories:
            for c in audience_categories:
                slug = (
                    c.get("category_slug")
                    if isinstance(c, dict)
                    else getattr(c, "category_slug", "")
                )
                if slug:
                    cat_metrics_by_slug[str(slug)] = (
                        c if isinstance(c, dict) else c.to_dict()
                    )

        # Fetch recent articles to count native ad placements per project & category
        recent_articles: list[Article] = []
        try:
            stmt_art = (
                select(Article)
                .where(Article.content_html.is_not(None))
                .order_by(Article.id.desc())
                .limit(250)
            )
            recent_articles = list((await session.execute(stmt_art)).scalars().all())
        except Exception as exc:
            logger.debug("Skipping article scan for advertiser report: %s", exc)

        campaigns: list[dict[str, Any]] = []
        total_target_visits = 0
        total_goal_conversions = 0
        total_integrated_articles = 0
        connected_counters_count = 0
        learning_directives: list[str] = []

        for proj in projects:
            # Match articles containing this advertiser's link or brand
            brand_needle = (proj.brand_name or proj.name or "").strip().lower()
            domain_needle = (
                proj.target_url.replace("https://", "")
                .replace("http://", "")
                .split("/")[0]
                .strip()
                .lower()
                if proj.target_url
                else ""
            )

            matched_articles: list[dict[str, Any]] = []
            cat_counts: dict[str, int] = {}
            platform_article_counts: dict[str, int] = {
                "web": 0,
                "dzen": 0,
                "tg": 0,
                "vk": 0,
                "ok": 0,
                "tenchat": 0,
            }
            for art in recent_articles:
                html_lower = (art.content_html or "").lower()
                if (domain_needle and domain_needle in html_lower) or (
                    brand_needle and len(brand_needle) >= 3 and brand_needle in html_lower
                ):
                    cat_slug = art.category or "general"
                    cat_counts[cat_slug] = cat_counts.get(cat_slug, 0) + 1

                    # Count placements across platforms
                    p_statuses = art.platform_statuses or {}
                    if art.wp_post_id or art.status == "published":
                        platform_article_counts["web"] += 1
                    for plat in ("dzen", "tg", "vk", "ok", "tenchat"):
                        st = p_statuses.get(plat)
                        if (
                            isinstance(st, dict) and st.get("status") == "published"
                        ) or st == "published":
                            platform_article_counts[plat] += 1

                    if len(matched_articles) < 5:
                        matched_articles.append(
                            {
                                "id": art.id,
                                "title": art.title,
                                "category": cat_slug,
                                "category_title": CATEGORY_TITLES.get(
                                    cat_slug, cat_slug
                                ),
                                "url_slug": art.url_slug or "",
                            }
                        )

            integrated_count = sum(cat_counts.values())
            total_integrated_articles += integrated_count

            # Fetch live Metrika stats for the advertiser
            stats = await cls.fetch_client_metrika_stats(
                proj,
                default_token=shared_token,
                days=days,
                include_counter_totals=True,
            )
            if stats.get("available"):
                connected_counters_count += 1
                total_target_visits += int(stats.get("visits") or 0)
                total_goal_conversions += int(stats.get("conversions_count") or 0)

            # Compile per-platform performance, stage analysis and action suggestions
            p_stats_map = stats.get("platform_stats", {})
            platform_configs = [
                ("web", "Сайт svodka.press", "🌐"),
                ("dzen", "Яндекс.Дзен", "⚡"),
                ("tg", "Telegram-канал", "✈️"),
                ("vk", "ВКонтакте", "💬"),
                ("ok", "Одноклассники", "👥"),
                ("tenchat", "TenChat", "💼"),
            ]
            platform_performance: list[dict[str, Any]] = []
            for plat_key, plat_title, plat_icon in platform_configs:
                art_count = platform_article_counts.get(plat_key, 0)
                ps = p_stats_map.get(plat_key, {})
                clicks = int(ps.get("visits", 0))
                convs = int(ps.get("conversions", 0))
                cr = float(ps.get("cr_pct", 0.0))
                bounce = float(ps.get("bounce_rate_pct", 0.0))

                # Closed-loop evaluation and action at each stage
                if not proj.is_active:
                    st_badge = "⏸️ На паузе"
                    st_analysis = "Интеграция приостановлена. Новые статьи не содержат объявлений."
                    st_rec = "Включить интеграцию"
                elif not stats.get("available"):
                    st_badge = "⚠️ Нет счётчика" if not proj.client_metrika_counter_id else "🔒 Требуется токен"
                    st_analysis = (
                        f"Размещено статей: {art_count}. Для фиксации переходов и конверсий привяжите счётчик Яндекс.Метрики."
                        if art_count > 0
                        else "Размещений ещё не было. Подключите Метрику и активируйте дистрибуцию."
                    )
                    st_rec = "Настроить Метрику"
                elif clicks > 0 and convs > 0:
                    st_badge = "🟢 Конверсии подтверждены"
                    st_analysis = f"Успешная воронка: {clicks} визитов → {convs} конверсий (CR {cr}%). Площадка окупается!"
                    st_rec = "Масштабировать охват"
                elif clicks > 0:
                    st_badge = "🔵 Трафик идёт"
                    st_analysis = f"Получено переходов: {clicks} (отказы {bounce}%). Целевые действия пока не совершены. Проверьте соответствие посадочной страницы ожиданиям читателей."
                    st_rec = "Снизить отказы"
                elif art_count == 0:
                    st_badge = "⚪ Не размещено"
                    st_analysis = "Объявления еще не выходили на этой площадке. Включите дистрибуцию в настройках публикаций."
                    st_rec = "Запустить публикации"
                else:
                    st_badge = "🟡 Статьи активны"
                    st_analysis = f"Опубликовано статей: {art_count}. Переходов по ссылке пока нет. Рекомендуется протестировать более цепляющий CTA и яркую врезку."
                    st_rec = "Оптимизировать CTA"

                platform_performance.append({
                    "platform": plat_key,
                    "title": plat_title,
                    "icon": plat_icon,
                    "articles_count": art_count,
                    "clicks": clicks,
                    "conversions": convs,
                    "cr_pct": cr,
                    "bounce_pct": bounce,
                    "status_badge": st_badge,
                    "stage_analysis": st_analysis,
                    "action_recommendation": st_rec,
                })

            # Estimate reading depth and bounce on our media for the project's target categories
            target_cats = proj.target_categories or list(cat_counts.keys())
            avg_read_sec = 82.0
            avg_bounce = 0.22
            total_cat_views = 0
            best_cat_title = "Технологии и ИИ"
            best_cat_score = 85
            if target_cats and cat_metrics_by_slug:
                read_vals = []
                bounce_vals = []
                best_cat = None
                for slug in target_cats:
                    cm = cat_metrics_by_slug.get(slug)
                    if cm:
                        read_vals.append(float(cm.get("avg_time_on_page") or 75.0))
                        bounce_vals.append(float(cm.get("bounce_rate") or 0.24))
                        total_cat_views += int(cm.get("pageviews") or 0)
                        if best_cat is None or float(
                            cm.get("ad_potential_score") or 0
                        ) > float(best_cat.get("ad_potential_score") or 0):
                            best_cat = cm
                if read_vals:
                    avg_read_sec = sum(read_vals) / len(read_vals)
                if bounce_vals:
                    avg_bounce = sum(bounce_vals) / len(bounce_vals)
                if best_cat:
                    best_cat_title = str(
                        best_cat.get("category_title") or best_cat_title
                    )
                    best_cat_score = int(best_cat.get("ad_potential_score") or 85)

            benchmark_cr = (
                proj.conversion_goals[0].target_cr_pct
                if proj.conversion_goals
                else 5.0
            )
            target_visits = int(stats.get("visits") or 0)
            target_convs = int(stats.get("conversions_count") or 0)
            target_bounce = float(stats.get("bounce_rate") or 0.20)

            cqi_report = evaluate_campaign_quality(
                is_brand_safe=True,
                media_reading_sec=avg_read_sec,
                media_bounce_rate=avg_bounce,
                media_reads=max(total_cat_views, integrated_count * 120, 100),
                link_clicks=max(target_visits, integrated_count * 5),
                target_visits=target_visits,
                target_conversions=target_convs,
                target_bounce_rate=target_bounce,
                target_benchmark_cr=benchmark_cr,
            )

            # Formulate Closed-Loop learning hypothesis for /seo
            if not proj.client_metrika_counter_id:
                hypothesis = (
                    f"Для «{proj.brand_name or proj.name}» не указан счётчик Метрики. "
                    "Подключите счётчик и цели в /ads, чтобы система отслеживала целевые визиты."
                )
            elif not stats.get("available"):
                hypothesis = (
                    f"Счётчик #{proj.client_metrika_counter_id} («{proj.brand_name}»): {stats.get('reason')}. "
                    "Откройте «📊 Метрика и цели» в /ads для привязки токена и загрузки целей."
                )
            elif not proj.conversion_goals:
                hypothesis = (
                    f"Счётчик #{proj.client_metrika_counter_id} активен (целевых визитов: {target_visits}). "
                    "Нажмите «🔄 Загрузить цели из Метрики» в карточке рекламодателя, чтобы ИИ обучался на конверсиях."
                )
            elif target_visits > 0 and target_convs > 0:
                cr_actual = stats.get("cr_pct", 0.0)
                hypothesis = (
                    f"Подтверждена конверсия «{proj.brand_name}»: {target_visits} целевых визитов → "
                    f"{target_convs} достижений целей (CR {cr_actual}% при плане {benchmark_cr}%). "
                    f"SEO-оптимизатор повышает приоритет сниппетов в рубрике «{best_cat_title}» (Ad Score {best_cat_score}/100)."
                )
            else:
                goal_names = ", ".join(g.name for g in proj.conversion_goals[:3])
                hypothesis = (
                    f"Цели «{proj.brand_name}» ({goal_names}) подключены к контуру /seo. "
                    f"Для роста целевых визитов рекомендуется генерация статей в рубрике «{best_cat_title}» "
                    f"и 1-Click дожим сниппетов на позициях 4–15 под запросы ЦА: {proj.problem_solved or proj.usp[:80]}."
                )

            if proj.is_active:
                learning_directives.append(hypothesis)

            campaigns.append(
                {
                    "project": proj,
                    "project_dict": proj.to_dict(),
                    "stats": stats,
                    "cqi": cqi_report.to_dict(),
                    "integrated_articles_count": integrated_count,
                    "matched_articles": matched_articles,
                    "platform_performance": platform_performance,
                    "category_Breakdown": [
                        {
                            "slug": k,
                            "title": CATEGORY_TITLES.get(k, k),
                            "count": v,
                        }
                        for k, v in sorted(
                            cat_counts.items(), key=lambda item: item[1], reverse=True
                        )
                    ],
                    "best_category_title": best_cat_title,
                    "best_category_score": best_cat_score,
                    "learning_hypothesis": hypothesis,
                }
            )

        return {
            "campaigns": campaigns,
            "stats_by_project_id": {c["project"].id: c["stats"] for c in campaigns},
            "cqi_by_project_id": {c["project"].id: c["cqi"] for c in campaigns},
            "total_projects": len(projects),
            "active_projects": sum(1 for p in projects if p.is_active),
            "connected_counters_count": connected_counters_count,
            "total_target_visits": total_target_visits,
            "total_goal_conversions": total_goal_conversions,
            "total_integrated_articles": total_integrated_articles,
            "shared_token_available": bool(shared_token),
            "shared_counter_id": yandex_conn.metrika_counter_id,
            "learning_directives": learning_directives,
        }


__all__ = [
    "PROMOTED_PROJECTS_SETTING_KEY",
    "PromotedProjectsService",
]

