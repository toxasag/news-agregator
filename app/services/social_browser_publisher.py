# ruff: noqa: RUF001
"""Browser-based social distribution adapters (Playwright Stealth + Cookie-Editor).

Connects and publishes news to 6 platforms using authenticated browser sessions
(``storage_state.json`` / ``Cookie-Editor`` JSON export) stored encrypted with
Fernet in ``system_settings`` (key ``social_browser_sessions``):

1. **Setka** (``https://setka.ru/``) — ``Platform.SETKA``
2. **TenChat** (``https://tenchat.ru/``) — ``Platform.TENCHAT``
3. **Threads** (``https://www.threads.com/``) — ``Platform.THREADS``
4. **Facebook** (``https://www.facebook.com/``) — ``Platform.FACEBOOK``
5. **Pinterest** (``https://ru.pinterest.com/``) — ``Platform.PINTEREST``
6. **YouTube Community** (``https://www.youtube.com/``) — ``Platform.YOUTUBE``

All adapters implement :class:`~app.services.social_base.BaseSocialAdapter` so
``publisher_worker`` fans out to them concurrently alongside Telegram, VK, OK,
and Yandex Dzen.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import re
import tempfile
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx
from PIL import Image
from playwright.async_api import Page, async_playwright
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_secret_box
from app.models.enums import Platform
from app.models.setting import SystemSetting
from app.services.social_base import (
    BaseSocialAdapter,
    PublishResult,
    SocialAuthError,
    SocialNotConfiguredError,
    SocialResponseError,
    build_hashtags,
    build_teaser,
)
from app.services.web_scraper import (
    _LAUNCH_ARGS,
    _STEALTH_INIT_SCRIPT,
    _USER_AGENT,
    get_browser_pool,
)

if TYPE_CHECKING:
    from app.models.article import Article

logger: Final[logging.Logger] = logging.getLogger(__name__)

_BROWSER_SESSION_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)

SOCIAL_BROWSER_SETTING_KEY: Final[str] = "social_browser_sessions"
SVODKA_SITE_URL: Final[str] = "https://svodka.press/"
YOUTUBE_SOCS_COOKIE: Final[str] = (
    "CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjQwMjE4LjA4X3AwGgJydSABGgYIgOm1rgY"
)
SVODKA_TG_URL: Final[str] = "https://t.me/svodka_press"
BROWSER_PROFILES_DIR: Final[Path] = Path(
    os.getenv("BROWSER_PROFILES_DIR", "/app/storage/browser_profiles")
)
_PLATFORM_LOCKS: Final[dict[str, asyncio.Lock]] = {}

SOCIAL_BROWSER_PLATFORMS: Final[dict[str, dict[str, Any]]] = {
    Platform.SETKA.value: {
        "code": Platform.SETKA.value,
        "title": "Сетка (setka.ru)",
        "short_title": "Сетка",
        "badge": "SK",
        "color": "#6366F1",
        "domain": ".setka.ru",
        "home_url": "https://setka.ru/",
        "compose_url": "https://setka.ru/",
        "target_label": "URL профиля или сообщества в Сетке",
        "target_placeholder": "https://setka.ru/",
        "max_chars": 1800,
        "auth_cookies": ("access_token", "session", "setka_session", "auth_token"),
        "description": (
            "Деловая соцсеть от hh.ru: публикация поста с обложкой "
            "и ссылками на сайт и Telegram."
        ),
    },
    Platform.TENCHAT.value: {
        "code": Platform.TENCHAT.value,
        "title": "TenChat (tenchat.ru)",
        "short_title": "TenChat",
        "badge": "TC",
        "color": "#EF4444",
        "domain": ".tenchat.ru",
        "home_url": "https://tenchat.ru/",
        "compose_url": "https://tenchat.ru/editor",
        "target_label": "URL вашего профиля в TenChat",
        "target_placeholder": "https://tenchat.ru/svodkapress",
        "max_chars": 2500,
        "auth_cookies": ("access_token", "refresh_token", "tc_session", "session_id"),
        "description": (
            "Публикация аналитических и новостных записей с обложкой "
            "и хештегами через веб-студию TenChat."
        ),
    },
    Platform.THREADS.value: {
        "code": Platform.THREADS.value,
        "title": "Threads (threads.com)",
        "short_title": "Threads",
        "badge": "TH",
        "color": "#111827",
        "domain": ".threads.com",
        "home_url": "https://www.threads.com/",
        "compose_url": "https://www.threads.com/",
        "target_label": "URL профиля в Threads",
        "target_placeholder": "https://www.threads.com/@svodka_press",
        "max_chars": 480,
        "auth_cookies": ("sessionid", "ds_user_id", "csrftoken"),
        "description": (
            "Публикация новостных веток (до 500 знаков) с фото-обложкой "
            "и ссылкой на полную статью."
        ),
    },
    Platform.FACEBOOK.value: {
        "code": Platform.FACEBOOK.value,
        "title": "Facebook (facebook.com)",
        "short_title": "Facebook",
        "badge": "FB",
        "color": "#1877F2",
        "domain": ".facebook.com",
        "home_url": "https://www.facebook.com/",
        "compose_url": "https://www.facebook.com/",
        "target_label": "URL Страницы / группы Facebook",
        "target_placeholder": "https://www.facebook.com/svodkapress",
        "max_chars": 1800,
        "auth_cookies": ("c_user", "xs"),
        "description": (
            "Публикация анонсов с фото и ссылкой на Страницу или в профиль "
            "через сессию браузера."
        ),
    },
    Platform.PINTEREST.value: {
        "code": Platform.PINTEREST.value,
        "title": "Pinterest (ru.pinterest.com)",
        "short_title": "Pinterest",
        "badge": "PI",
        "color": "#E60023",
        "domain": ".pinterest.com",
        "home_url": "https://ru.pinterest.com/",
        "compose_url": "https://ru.pinterest.com/pin-creation-tool/",
        "target_label": "Название доски (Board) для пинов",
        "target_placeholder": "Новости СводкаПресс",
        "max_chars": 480,
        "auth_cookies": ("_pinterest_sess", "_auth"),
        "description": (
            "Создание фото-пинов с заголовком, SEO-описанием и прямой "
            "целевой ссылкой на статью svodka.press."
        ),
    },
    Platform.YOUTUBE.value: {
        "code": Platform.YOUTUBE.value,
        "title": "YouTube Сообщество (youtube.com)",
        "short_title": "YouTube",
        "badge": "YT",
        "color": "#FF0000",
        "domain": ".youtube.com",
        "home_url": "https://www.youtube.com/",
        "compose_url": "https://www.youtube.com/",
        "target_label": "URL вкладки «Сообщество» (Posts) канала",
        "target_placeholder": "https://www.youtube.com/@svodkapress/posts",
        "max_chars": 1400,
        "auth_cookies": (
            "SAPISID",
            "__Secure-1PSID",
            "__Secure-3PSID",
            "SSID",
            "SID",
        ),
        "description": (
            "Текстовые посты с фотографией обложки и живыми ссылками "
            "во вкладке «Сообщество» (Записи) YouTube."
        ),
    },
    Platform.SPARK.value: {
        "code": Platform.SPARK.value,
        "title": "Spark.ru (spark.ru)",
        "short_title": "Spark",
        "badge": "SP",
        "color": "#273149",
        "domain": ".spark.ru",
        "home_url": "https://spark.ru/startup/svodka-press",
        "compose_url": "https://spark.ru/startup/svodka-press/blog/new",
        "target_label": "URL блога стартапа на Spark.ru",
        "target_placeholder": "https://spark.ru/startup/svodka-press",
        "max_chars": 8000,
        "auth_cookies": ("uid", "session", "sid"),
        "description": (
            "Публикация новостей и статей в блог проекта «Svodka Press» "
            "на платформе предпринимателей и технологий Spark.ru."
        ),
    },
}

_FIRST_IMG_RE: Final[re.Pattern[str]] = re.compile(
    r'<img[^>]+src=["\'](https?://[^"\']+)["\']', re.IGNORECASE
)


@dataclass(slots=True)
class SocialBrowserSessionConfig:
    """Configuration and encrypted browser state for one social network."""

    platform: str
    is_active: bool = True
    target_url: str = ""
    api_token: str = ""
    storage_state: dict[str, Any] | None = None
    status: str = "not_configured"
    error_message: str | None = None
    account_name: str | None = None
    last_checked_at: str | None = None

    @property
    def profile_dir(self) -> Path:
        """Physical directory for the persistent Chromium profile."""
        return BROWSER_PROFILES_DIR / self.platform

    @property
    def has_profile(self) -> bool:
        """Whether a populated persistent Chromium profile exists on disk."""
        pdir = self.profile_dir
        if not pdir.is_dir():
            return False
        # Profile is considered populated if it contains Default or preferences/cookies
        return any(
            (pdir / name).exists()
            for name in ("Default", "Cookies", "Preferences", "Network")
        )

    @property
    def has_session(self) -> bool:
        """Whether non-empty browser cookies or persistent profile exist."""
        if self.has_profile:
            return True
        if not self.storage_state or not isinstance(self.storage_state, dict):
            return False
        cookies = self.storage_state.get("cookies")
        return bool(isinstance(cookies, list) and len(cookies) > 0)

    @property
    def has_token(self) -> bool:
        """Whether an explicit API token is configured."""
        return bool(self.api_token.strip())

    @property
    def is_configured(self) -> bool:
        """Whether the platform is enabled and has credentials or profile."""
        return self.is_active and (self.has_session or self.has_token or self.has_profile)

    @property
    def cookies_count(self) -> int:
        if not self.has_session or not self.storage_state:
            return 0
        return len(self.storage_state.get("cookies", []))

    @property
    def masked_token(self) -> str:
        raw = self.api_token.strip()
        if not raw:
            return ""
        return f"••••••••{raw[-4:]}" if len(raw) > 8 else "••••••••"

    def to_storage_dict(self) -> dict[str, Any]:
        """Serialize with Fernet encryption for JSONB persistence."""
        box = get_secret_box()
        enc_state = ""
        if self.storage_state:
            enc_state = box.encrypt(
                json.dumps(self.storage_state, ensure_ascii=False)
            )
        enc_token = box.encrypt(self.api_token) if self.api_token else ""
        return {
            "platform": self.platform,
            "is_active": self.is_active,
            "target_url": self.target_url,
            "encrypted_api_token": enc_token,
            "encrypted_storage_state": enc_state,
            "status": self.status,
            "error_message": self.error_message,
            "account_name": self.account_name,
            "last_checked_at": self.last_checked_at,
        }

    @classmethod
    def from_storage_dict(
        cls, platform: str, data: Mapping[str, Any]
    ) -> SocialBrowserSessionConfig:
        """Deserialize and decrypt from JSONB."""
        box = get_secret_box()
        storage_state: dict[str, Any] | None = None
        enc_state = str(data.get("encrypted_storage_state") or "")
        if enc_state:
            try:
                storage_state = json.loads(box.decrypt(enc_state))
            except Exception:  # noqa: BLE001
                logger.warning("Could not decrypt %s browser session", platform)

        api_token = ""
        enc_tok = str(data.get("encrypted_api_token") or "")
        if enc_tok:
            try:
                api_token = box.decrypt(enc_tok)
            except Exception:  # noqa: BLE001
                logger.warning("Could not decrypt %s API token", platform)

        return cls(
            platform=platform,
            is_active=bool(data.get("is_active", True)),
            target_url=str(data.get("target_url") or ""),
            api_token=api_token,
            storage_state=storage_state,
            status=str(data.get("status") or "not_configured"),
            error_message=(
                str(data["error_message"]) if data.get("error_message") else None
            ),
            account_name=(
                str(data["account_name"]) if data.get("account_name") else None
            ),
            last_checked_at=(
                str(data["last_checked_at"]) if data.get("last_checked_at") else None
            ),
        )

    def to_public_dict(self) -> dict[str, Any]:
        """Safe dictionary for Jinja2 templates."""
        meta = SOCIAL_BROWSER_PLATFORMS.get(self.platform, {})
        return {
            "platform": self.platform,
            "title": meta.get("title", self.platform),
            "short_title": meta.get("short_title", self.platform),
            "badge": meta.get("badge", self.platform[:2].upper()),
            "color": meta.get("color", "#0071E3"),
            "home_url": meta.get("home_url", ""),
            "target_label": meta.get("target_label", "Целевой URL"),
            "target_placeholder": meta.get("target_placeholder", ""),
            "description": meta.get("description", ""),
            "is_active": self.is_active,
            "target_url": self.target_url,
            "has_profile": self.has_profile,
            "has_session": self.has_session,
            "has_token": self.has_token,
            "is_configured": self.is_configured,
            "cookies_count": self.cookies_count,
            "masked_token": self.masked_token,
            "status": self.status,
            "error_message": self.error_message,
            "account_name": self.account_name,
            "last_checked_at": self.last_checked_at,
        }


def _normalize_single_cookie(
    c: Mapping[str, Any], default_domain: str
) -> dict[str, Any] | None:
    name = str(c.get("name") or "").strip()
    value = str(c.get("value") or "")
    if not name:
        return None
    domain = str(c.get("domain") or default_domain).strip()
    path = str(c.get("path") or "/").strip()

    raw_same_site = str(c.get("sameSite") or "Lax").lower()
    if raw_same_site in ("no_restriction", "none", "unspecified"):
        same_site = "None"
    elif raw_same_site == "strict":
        same_site = "Strict"
    else:
        same_site = "Lax"

    secure = bool(c.get("secure", False))
    if same_site == "None":
        secure = True

    if name.startswith("__Host-"):
        # RFC 6265bis: __Host- cookies MUST be host-only, no leading dot allowed
        domain = domain.lstrip(".")
        path = "/"
        secure = True

    cookie: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "httpOnly": bool(c.get("httpOnly", False)),
        "secure": secure,
        "sameSite": same_site,
    }
    exp = c.get("expires") or c.get("expirationDate")
    if isinstance(exp, (int, float)) and exp > 0:
        cookie["expires"] = float(exp)
    return cookie


def normalize_social_storage_state(  # noqa: PLR0912
    raw_data: Any,
    platform: str,
    existing_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize Cookie-Editor JSON array or Playwright storage_state."""
    meta = SOCIAL_BROWSER_PLATFORMS.get(platform, {})
    default_domain = str(meta.get("domain") or f".{platform}.com")

    raw_cookies: list[Any] = []
    origins: list[Any] = []
    user_agent: str | None = None
    if isinstance(raw_data, list):
        raw_cookies = raw_data
    elif isinstance(raw_data, dict):
        if isinstance(raw_data.get("cookies"), list):
            raw_cookies = raw_data["cookies"]
        if isinstance(raw_data.get("origins"), list):
            origins = raw_data["origins"]
        if raw_data.get("userAgent"):
            user_agent = str(raw_data["userAgent"])

    if (
        not user_agent
        and isinstance(existing_state, Mapping)
        and existing_state.get("userAgent")
    ):
        user_agent = str(existing_state["userAgent"])
    if not user_agent:
        user_agent = _BROWSER_SESSION_USER_AGENT

    cleaned: list[dict[str, Any]] = []
    for item in raw_cookies:
        if not isinstance(item, Mapping):
            continue
        norm = _normalize_single_cookie(item, default_domain)
        if norm is None:
            continue
        cleaned.append(norm)
        if platform == Platform.THREADS.value:
            dom = norm["domain"]
            if "threads.net" in dom:
                cleaned.append(
                    {**norm, "domain": dom.replace("threads.net", "threads.com")}
                )
            elif "threads.com" in dom:
                cleaned.append(
                    {**norm, "domain": dom.replace("threads.com", "threads.net")}
                )
        elif platform == Platform.YOUTUBE.value:
            dom = norm["domain"]
            if "google.com" in dom:
                cleaned.append(
                    {**norm, "domain": dom.replace("google.com", "youtube.com")}
                )
            elif "youtube.com" in dom:
                cleaned.append(
                    {**norm, "domain": dom.replace("youtube.com", "google.com")}
                )

    # Deduplicate cookies by (name, normalized domain, path)
    unique_cookies: dict[tuple[str, str, str], dict[str, Any]] = {}
    for c in cleaned:
        c_dom = str(c.get("domain") or "").lstrip(".")
        c_path = str(c.get("path") or "/")
        key = (c["name"], c_dom, c_path)
        unique_cookies[key] = c

    return {
        "cookies": list(unique_cookies.values()),
        "origins": origins,
        "userAgent": user_agent,
    }


async def load_all_social_browser_configs(
    session: AsyncSession,
) -> dict[str, SocialBrowserSessionConfig]:
    """Load all 6 browser social platform configs from system_settings."""
    configs: dict[str, SocialBrowserSessionConfig] = {
        code: SocialBrowserSessionConfig(platform=code)
        for code in SOCIAL_BROWSER_PLATFORMS
    }
    try:
        stmt = select(SystemSetting).where(
            SystemSetting.key == SOCIAL_BROWSER_SETTING_KEY
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row and isinstance(row.value, Mapping):
            for code in SOCIAL_BROWSER_PLATFORMS:
                raw_entry = row.value.get(code)
                if isinstance(raw_entry, Mapping):
                    configs[code] = SocialBrowserSessionConfig.from_storage_dict(
                        code, raw_entry
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load social browser sessions: %s", exc)
    return configs


async def load_social_browser_config(
    session: AsyncSession, platform: str
) -> SocialBrowserSessionConfig:
    """Load config for a single browser social platform."""
    all_cfgs = await load_all_social_browser_configs(session)
    return all_cfgs.get(platform) or SocialBrowserSessionConfig(platform=platform)


async def save_social_browser_config(
    session: AsyncSession, config: SocialBrowserSessionConfig
) -> None:
    """Persist updated config for one platform into system_settings."""
    all_cfgs = await load_all_social_browser_configs(session)
    all_cfgs[config.platform] = config
    payload = {code: cfg.to_storage_dict() for code, cfg in all_cfgs.items()}

    stmt = (
        pg_insert(SystemSetting)
        .values(
            key=SOCIAL_BROWSER_SETTING_KEY,
            value=payload,
            description=(
                "Encrypted browser sessions for Setka, TenChat, Threads, "
                "Facebook, Pinterest, YouTube"
            ),
        )
        .on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={
                "value": payload,
                "updated_at": datetime.now(UTC),
            },
        )
    )
    await session.execute(stmt)
    await session.commit()


def extract_article_cover_url(article: Article) -> str:
    """Extract the best available HTTP image URL for an article."""
    wp_stat = (article.platform_statuses or {}).get("wp")
    if isinstance(wp_stat, Mapping):
        cover = str(wp_stat.get("cover_url") or "").strip()
        if cover.startswith(("http://", "https://")):
            return cover
    match = _FIRST_IMG_RE.search(article.content_html or "")
    if match:
        return match.group(1)
    return ""


def format_social_browser_post(
    article: Article,
    wp_post_url: str,
    platform: str,
) -> dict[str, Any]:
    """Build platform-tailored post text, title, hashtags, and links."""
    meta = SOCIAL_BROWSER_PLATFORMS.get(platform, {})
    max_chars = int(meta.get("max_chars") or 1000)

    title = " ".join((article.title or "Новость дня").split()).strip()
    tags = build_hashtags(article.seo_keywords or ())
    if not tags:
        tags = ["#новости", "#сводкапресс"]
    tags_line = " ".join(tags)

    canonical_url = wp_post_url.strip() or SVODKA_SITE_URL
    footer = (
        f"\n\n👉 Читать полностью на сайте: {canonical_url}"
        f"\n📲 Оперативные новости в Telegram: {SVODKA_TG_URL}"
        f"\n\n{tags_line}"
    )

    teaser_budget = max(120, max_chars - len(title) - len(footer) - 10)
    teaser = build_teaser(
        title=title,
        body_html=article.content_html or "",
        limit=teaser_budget,
        description=article.seo_description or "",
    )

    if platform == Platform.PINTEREST.value:
        pin_body = (
            f"{teaser}\n\nПодробности на {SVODKA_SITE_URL} "
            f"и в Telegram {SVODKA_TG_URL}\n{tags_line}"
        )
        full_text = pin_body[:max_chars]
    elif platform == Platform.SPARK.value:
        from app.services.text_utils import html_to_plain_text  # noqa: PLC0415
        plain_body = (
            html_to_plain_text(article.content_html or "")
            if article.content_html
            else teaser
        )
        full_text = f"{plain_body}\n\n{footer}"[:max_chars]
    else:
        full_text = f"⚡ {title}\n\n{teaser}{footer}"[:max_chars]

    return {
        "title": title[:100],
        "teaser": teaser,
        "text": full_text,
        "hashtags": tags,
        "canonical_url": canonical_url,
        "tg_url": SVODKA_TG_URL,
        "cover_url": extract_article_cover_url(article),
        "article_id": getattr(article, "id", None),
    }


def _has_platform_auth_cookies(
    cookies: list[dict[str, Any]], platform: str
) -> list[str]:
    """Return matched authentication cookie names for the given platform."""
    meta = SOCIAL_BROWSER_PLATFORMS.get(platform, {})
    expected = tuple(str(x).lower() for x in meta.get("auth_cookies", ()))
    found: list[str] = []
    for c in cookies:
        cname = str(c.get("name") or "")
        if cname.lower() in expected or any(
            tok in cname.lower() for tok in ("sess", "token", "sid", "auth", "user")
        ):
            found.append(cname)
    return found


async def _acquire_platform_lock(platform: str) -> asyncio.Lock:
    """Return an asyncio lock for the platform to serialize access to user_data_dir."""
    if platform not in _PLATFORM_LOCKS:
        _PLATFORM_LOCKS[platform] = asyncio.Lock()
    return _PLATFORM_LOCKS[platform]


@asynccontextmanager
async def persistent_platform_page(  # noqa: PLR0912, PLR0915
    config: SocialBrowserSessionConfig,
    *,
    timeout_seconds: float = 35.0,
) -> AsyncIterator[Page]:
    """Yield an authenticated Page, using persistent Chromium profile if available."""
    p_lock = await _acquire_platform_lock(config.platform)
    async with p_lock:
        timeout_ms = int(timeout_seconds * 1000)

        context = None
        pw = None
        page = None
        try:
            config.profile_dir.mkdir(parents=True, exist_ok=True)
            pw = await async_playwright().start()
            launch_args = list(_LAUNCH_ARGS)
            ua = None
            if config.storage_state and isinstance(config.storage_state, dict):
                ua = config.storage_state.get("userAgent")
            if not ua:
                ua = _USER_AGENT

            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(config.profile_dir),
                headless=True,
                viewport={"width": 1440, "height": 900},
                user_agent=ua,
                args=launch_args,
            )
            context.set_default_timeout(timeout_ms)
            context.set_default_navigation_timeout(timeout_ms)

            # Inject storage_state (cookies + localStorage) if provided in config
            if config.storage_state and isinstance(config.storage_state, dict):
                cookies = config.storage_state.get("cookies", [])
                if cookies and isinstance(cookies, list):
                    clean_cookies = []
                    for c in cookies:
                        if (
                            not isinstance(c, dict)
                            or not c.get("name")
                            or not c.get("value")
                        ):
                            continue
                        cookie_dict = dict(c)
                        exp = cookie_dict.get("expires")
                        if exp is not None and (
                            not isinstance(exp, (int, float)) or exp <= 0
                        ):
                            cookie_dict.pop("expires", None)
                        clean_cookies.append(cookie_dict)
                    if clean_cookies:
                        with contextlib.suppress(Exception):
                            await context.add_cookies(clean_cookies)
                origins = config.storage_state.get("origins", [])
                if origins and isinstance(origins, list):
                    for orig in origins:
                        if not isinstance(orig, dict):
                            continue
                        origin_url = orig.get("origin")
                        ls_entries = orig.get("localStorage", [])
                        if not origin_url or not ls_entries:
                            continue
                        js = (
                            f"if (window.location.origin === {json.dumps(origin_url)}) {{"
                            f"  const entries = {json.dumps(ls_entries)};"
                            f"  for (const item of entries) {{"
                            f"    if (localStorage.getItem(item.name) !== item.value) {{"
                            f"      localStorage.setItem(item.name, item.value);"
                            f"    }}"
                            f"  }}"
                            f"}}"
                        )
                        with contextlib.suppress(Exception):
                            await context.add_init_script(js)

            page = context.pages[0] if context.pages else await context.new_page()
            with contextlib.suppress(Exception):
                await page.add_init_script(_STEALTH_INIT_SCRIPT)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Persistent profile failed to launch for %s, "
                "falling back to BrowserPool: %s",
                config.platform,
                exc,
            )
            if context is not None:
                with contextlib.suppress(Exception):
                    await context.close()
                context = None
            if pw is not None:
                with contextlib.suppress(Exception):
                    await pw.stop()
                pw = None

        if page is not None:
            try:
                yield page
            finally:
                if context is not None:
                    with contextlib.suppress(Exception):
                        refreshed = await context.storage_state()
                        if refreshed and refreshed.get("cookies"):
                            config.storage_state = normalize_social_storage_state(
                                refreshed, config.platform, config.storage_state
                            )
                    with contextlib.suppress(Exception):
                        await context.close()
                if pw is not None:
                    with contextlib.suppress(Exception):
                        await pw.stop()
        else:
            pool = get_browser_pool()
            async with pool.page_with_storage(
                storage_state=config.storage_state,
                timeout_seconds=timeout_seconds,
            ) as fallback_page:
                yield fallback_page


async def probe_social_browser_session(  # noqa: PLR0912, PLR0915
    config: SocialBrowserSessionConfig,
    *,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """Verify the saved browser session or persistent profile via Playwright."""
    now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    meta = SOCIAL_BROWSER_PLATFORMS.get(config.platform, {})
    title = str(meta.get("title") or config.platform)

    if not config.has_session and not config.has_token and not config.has_profile:
        config.status = "not_configured"
        config.last_checked_at = now_str
        msg = (
            f"Сессия {title} не настроена. "
            "Авторизуйте профиль или загрузите куки."
        )
        config.error_message = msg
        if session is not None:
            await save_social_browser_config(session, config)
        return {
            "is_valid": False,
            "status": config.status,
            "message": msg,
            "timestamp": now_str,
        }

    cookies = (config.storage_state or {}).get("cookies", [])
    matched_auth = _has_platform_auth_cookies(cookies, config.platform)
    target = config.target_url.strip() or str(meta.get("home_url") or "")

    probe_url = target
    if config.platform == Platform.TENCHAT.value:
        probe_url = "https://tenchat.ru/editor"
    elif config.platform == Platform.SPARK.value:
        probe_url = "https://spark.ru/write"
    elif config.platform == Platform.YOUTUBE.value:
        yt_dest = target if "@" in target else "https://www.youtube.com/@svodkapress/posts"
        if not yt_dest.rstrip("/").endswith(("community", "posts")):
            yt_dest = f"{yt_dest.rstrip('/')}/posts"
        import urllib.parse  # noqa: PLC0415
        enc_dest = urllib.parse.quote(yt_dest, safe="")
        probe_url = (
            "https://accounts.google.com/ServiceLogin?service=youtube&uilel=3&passive=true"
            f"&continue=https%3A%2F%2Fwww.youtube.com%2Fsignin%3Faction_handle_signin%3Dtrue"
            f"%26app%3Ddesktop%26hl%3Dru%26next%3D{enc_dest}"
        )

    try:
        async with persistent_platform_page(config, timeout_seconds=25.0) as page:
            resp = await page.goto(
                probe_url, wait_until="domcontentloaded", timeout=20000
            )
            await asyncio.sleep(2.5)
            final_url = page.url.lower()
            page_title = await page.title()

            unauth_markers = (
                "/login",
                "/signin",
                "/sign-in",
                "/auth",
                "oauth.",
                "servicelogin",
                "accounts.google.com",
                "passport.yandex.",
            )
            is_auth_redirect = any(m in final_url for m in unauth_markers)

            if config.platform == Platform.TENCHAT.value:
                # Direct check for TenChat /editor
                if (
                    is_auth_redirect
                    or (resp and resp.status == 401)
                    or "необходима авторизация" in page_title.lower()
                ):
                    config.status = "expired"
                    msg = (
                        "⚠️ Сессия TenChat не авторизована: доступ к редактору "
                        "статей (/editor) заблокирован (401). Требуется вход."
                    )
                    config.error_message = msg
                    is_valid = False
                else:
                    config.status = "active"
                    config.error_message = None
                    config.account_name = "TenChat Редактор"
                    msg = "✅ Сессия TenChat активна и проверена на странице /editor."
                    is_valid = True

            elif config.platform == Platform.YOUTUBE.value:
                cookies = await page.context.cookies()
                yt_cookies = {
                    c.get("name")
                    for c in cookies
                    if "youtube.com" in c.get("domain", "")
                    or "google" in c.get("domain", "")
                }
                has_auth_cookie = (
                    "LOGIN_INFO" in yt_cookies
                    or ("SAPISID" in yt_cookies and "SID" in yt_cookies)
                    or ("__Secure-1PSID" in yt_cookies and "__Secure-3PSID" in yt_cookies)
                )
                has_avatar = False
                with contextlib.suppress(Exception):
                    has_avatar = await page.locator(
                        "ytd-masthead #avatar-btn, ytd-masthead button#avatar-btn"
                    ).first.is_visible()
                has_create = False
                with contextlib.suppress(Exception):
                    create_sel = (
                        "ytd-masthead [aria-label*='Создать'], "
                        "ytd-masthead [aria-label*='Create']"
                    )
                    has_create = await page.locator(create_sel).first.is_visible()
                has_post_box = False
                with contextlib.suppress(Exception):
                    post_sel = (
                        "#placeholder-area, #simplebox-placeholder, "
                        "ytd-backstage-post-dialog-renderer"
                    )
                    has_post_box = await page.locator(post_sel).first.is_visible()
                has_signin = False
                with contextlib.suppress(Exception):
                    has_signin = await page.locator(
                        'ytd-masthead a[href*="ServiceLogin"], '
                        'ytd-masthead button[aria-label*="Войти"], '
                        'ytd-button-renderer a[href*="signin"]'
                    ).first.is_visible()

                is_logged_in = (
                    (has_avatar or has_create or has_post_box or has_auth_cookie)
                    and not has_signin
                    and not is_auth_redirect
                )
                if not is_logged_in:
                    config.status = "expired"
                    msg = (
                        "⚠️ Сессия YouTube не авторизована: "
                        "отсутствует авторизация аккаунта (требуется вход в Google)."
                    )
                    config.error_message = msg
                    is_valid = False
                else:
                    config.status = "active"
                    config.error_message = None
                    fallback_name = "YouTube @svodkapress"
                    config.account_name = page_title[:60] if page_title else fallback_name
                    msg = (
                        f"✅ Сессия YouTube Сообщество активна. "
                        f"Канал: «{config.account_name}»."
                    )
                    is_valid = True

            elif config.platform == Platform.SPARK.value:
                spark_uid = 0
                with contextlib.suppress(Exception):
                    spark_uid = await page.evaluate("() => window.uid || 0")

                has_editor = (
                    await page.locator(
                        "input[name='title'], textarea[name='subtitle'], "
                        ".post-form__input_title, .ce-paragraph, "
                        ".post-form__submit, .simple-selector"
                    ).count()
                    > 0
                )

                if is_auth_redirect or not has_editor:
                    config.status = "expired"
                    msg = (
                        "⚠️ Сессия Spark.ru не авторизована: вход в аккаунт не выполнен. "
                        "Авторизуйтесь через браузер (Вход через IP сервера) или "
                        "загрузите куки."
                    )
                    config.error_message = msg
                    is_valid = False
                else:
                    config.status = "active"
                    config.error_message = None
                    config.account_name = (
                        f"Svodka Press (UID {spark_uid})"
                        if spark_uid
                        else "Svodka Press (Spark.ru)"
                    )
                    msg = (
                        "✅ Сессия Spark.ru активна: редактор https://spark.ru/write "
                        "проверен, блог Svodka Press доступен."
                    )
                    is_valid = True

            elif is_auth_redirect:
                config.status = "expired"
                msg = (
                    f"⚠️ Сессия {title} истекла (перенаправлена на авторизацию). "
                    "Авторизуйте профиль заново."
                )
                config.error_message = msg
                is_valid = False
            elif not matched_auth and not cookies and not config.has_profile:
                config.status = "expired"
                msg = f"⚠️ Сессия {title}: нет данных авторизации."
                config.error_message = msg
                is_valid = False
            else:
                config.status = "active"
                config.error_message = None
                config.account_name = page_title[:60] if page_title else title
                auth_info = (
                    "Persistent Profile"
                    if config.has_profile
                    else f"{len(cookies)} кук"
                )
                msg = (
                    f"✅ Сессия {title} активна ({auth_info}). "
                    f"Страница: «{config.account_name}»."
                )
                is_valid = True

    except Exception as exc:  # noqa: BLE001
        config.status = "error"
        is_valid = False
        msg = f"❌ Ошибка проверки {title}: {exc}"
        config.error_message = msg

    config.last_checked_at = now_str
    if session is not None:
        await save_social_browser_config(session, config)
    return {
        "is_valid": is_valid,
        "status": config.status,
        "message": msg,
        "timestamp": now_str,
    }


async def _download_temp_cover(cover_url: str) -> str | None:
    """Download article cover image to a temporary JPEG file for file input upload."""
    if not cover_url or not cover_url.startswith(("http://", "https://")):
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(cover_url)
            if resp.status_code != 200 or len(resp.content) < 512:
                return None
            raw_bytes = resp.content
            with (
                contextlib.suppress(Exception),
                Image.open(io.BytesIO(raw_bytes)) as img,
            ):
                rgb = img.convert("RGB")
                buf = io.BytesIO()
                rgb.save(buf, format="JPEG", quality=90)
                raw_bytes = buf.getvalue()
            with tempfile.NamedTemporaryFile(
                prefix="svodka_social_", suffix=".jpg", delete=False
            ) as tmp:
                tmp.write(raw_bytes)
                return tmp.name
    except Exception as exc:  # noqa: BLE001
        logger.debug("Cover download for social browser skipped: %s", exc)
        return None


async def _try_upload_file_on_page(page: Any, image_path: str | None) -> bool:
    """Attach image file to the first file input on the page if available."""
    if not image_path:
        return False
    try:
        file_inputs = page.locator("input[type='file']")
        if await file_inputs.count() > 0:
            await file_inputs.first.set_input_files(image_path)
            await asyncio.sleep(2.5)
            return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("File input attachment skipped: %s", exc)
    return False


async def _fill_first_matching(
    page: Any, selectors: tuple[str, ...], text: str
) -> bool:
    """Click and fill/paste text into the first visible matching selector."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=3000)
                await loc.fill(text, timeout=5000)
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Selector fill skipped (%s): %s", sel, exc)
    return False


async def _click_first_matching(page: Any, selectors: tuple[str, ...]) -> bool:
    """Click the first visible matching selector."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=4000)
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Selector click skipped (%s): %s", sel, exc)
    return False


async def _publish_pinterest_pin(
    page: Any, post_data: dict[str, Any], cover_path: str | None
) -> tuple[str, str]:
    """Automate Pinterest Pin Builder (ru.pinterest.com/pin-creation-tool/)."""
    await page.goto(
        "https://ru.pinterest.com/pin-creation-tool/",
        wait_until="domcontentloaded",
        timeout=25000,
    )
    await asyncio.sleep(3.0)
    await _try_upload_file_on_page(page, cover_path)
    await _fill_first_matching(
        page,
        (
            "input[placeholder*='заголовок' i]",
            "input[placeholder*='title' i]",
            "textarea[placeholder*='заголовок' i]",
            "[data-test-id='pin-draft-title'] input",
            "[data-test-id='pin-draft-title'] textarea",
        ),
        post_data["title"],
    )
    await _fill_first_matching(
        page,
        (
            "[data-test-id='pin-draft-description'] [contenteditable='true']",
            "div[role='textbox']",
            "textarea[placeholder*='описание' i]",
        ),
        post_data["text"],
    )
    await _fill_first_matching(
        page,
        (
            "input[placeholder*='ссылк' i]",
            "input[placeholder*='link' i]",
            "input[type='url']",
            "[data-test-id='pin-draft-link'] input",
        ),
        post_data["canonical_url"],
    )
    clicked = await _click_first_matching(
        page,
        (
            "[data-test-id='board-dropdown-save-button']",
            "button:has-text('Опубликовать')",
            "button:has-text('Сохранить')",
            "button:has-text('Publish')",
        ),
    )
    if not clicked:
        raise SocialResponseError(
            "Pinterest: не найдена кнопка публикации пина "
            "(проверьте сессию и наличие обложки)",
            retryable=True,
        )
    await asyncio.sleep(4.0)
    return f"pin-{uuid.uuid4().hex[:10]}", page.url


async def _publish_generic_composer(
    page: Any,
    config: SocialBrowserSessionConfig,
    post_data: dict[str, Any],
    cover_path: str | None,
) -> tuple[str, str]:
    """Automate composer flow for Setka, TenChat, Threads, Facebook, and YouTube."""
    meta = SOCIAL_BROWSER_PLATFORMS.get(config.platform, {})
    target_url = config.target_url.strip() or str(
        meta.get("compose_url") or meta.get("home_url") or ""
    )
    if (
        config.platform == Platform.YOUTUBE.value
        and target_url
        and not target_url.rstrip("/").endswith(("community", "posts"))
        and "@" in target_url
    ):
        target_url = f"{target_url.rstrip('/')}/community"

    await page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
    await asyncio.sleep(3.0)

    await _click_first_matching(
        page,
        (
            "#simplebox-placeholder",
            "button:has-text('Создать запись')",
            "button:has-text('Написать')",
            "button:has-text('Создать')",
            "[aria-label*='Создать' i]",
            "[aria-label*='Что у вас нового' i]",
            "[role='button']:has-text('Что нового')",
            "[role='button']:has-text('Что у вас нового')",
            "div:has-text('Поделитесь мыслями')",
        ),
    )
    await asyncio.sleep(1.5)

    await _try_upload_file_on_page(page, cover_path)

    filled = await _fill_first_matching(
        page,
        (
            "#contenteditable-root",
            "div[contenteditable='true'][role='textbox']",
            "div[contenteditable='true']",
            "textarea[placeholder]",
            "textarea",
        ),
        post_data["text"],
    )
    if not filled:
        short_name = meta.get("short_title", config.platform)
        raise SocialAuthError(
            f"{short_name}: поле ввода поста не найдено "
            "(возможно, истекла браузерная сессия)."
        )

    await asyncio.sleep(1.5)
    published = await _click_first_matching(
        page,
        (
            "#submit-button button",
            "ytd-button-renderer#submit-button button",
            "button[type='submit']:has-text('Опубликовать')",
            "button:has-text('Опубликовать')",
            "div[role='button']:has-text('Опубликовать')",
            "button:has-text('Отправить')",
            "button:has-text('Post')",
            "div[role='button']:has-text('Post')",
        ),
    )
    if not published:
        short_name = meta.get("short_title", config.platform)
        raise SocialResponseError(
            f"{short_name}: кнопка «Опубликовать» не сработала.",
            retryable=True,
        )

    await asyncio.sleep(3.5)
    ext_id = f"{config.platform}-{uuid.uuid4().hex[:10]}"
    return ext_id, page.url


async def _dismiss_tenchat_tutorial(page: Any) -> None:
    """Dismiss onboarding modals (Далее -> Далее -> Приступить)."""
    for _ in range(4):
        btn = page.locator(
            "button:has-text('Далее'), button:has-text('Приступить'), "
            "button:has-text('Понятно'), button:has-text('Закрыть')"
        ).first
        if await btn.count() > 0 and await btn.is_visible():
            with contextlib.suppress(Exception):
                await btn.click(timeout=3000)
                await asyncio.sleep(0.8)
        else:
            break


async def _select_tenchat_topic(page: Any) -> None:
    """Select the first suggested topic tag in TenChat editor."""
    topic_btn = page.locator("button:has-text('Добавить тематику')").first
    if not (await topic_btn.count() > 0 and await topic_btn.is_visible()):
        return
    with contextlib.suppress(Exception):
        await topic_btn.click(timeout=3000)
        await asyncio.sleep(1.2)
        topic_tag = page.locator(
            "[role='dialog'] button, [class*='modal'] button, "
            "[class*='tag'], [class*='chip']"
        ).first
        if await topic_tag.count() > 0 and await topic_tag.is_visible():
            await topic_tag.click(timeout=3000)
            await asyncio.sleep(0.8)
        confirm = page.locator(
            "[role='dialog'] button:has-text('Сохранить'), "
            "[role='dialog'] button:has-text('Выбрать'), "
            "[role='dialog'] button:has-text('Применить')"
        ).first
        if await confirm.count() > 0 and await confirm.is_visible():
            await confirm.click(timeout=3000)
            await asyncio.sleep(0.8)


async def _publish_tenchat_post(
    page: Any,
    config: SocialBrowserSessionConfig,
    post_data: dict[str, Any],
    cover_path: str | None,
) -> tuple[str, str]:
    """Automate post composition in TenChat Web Studio (tenchat.ru/editor)."""
    meta = SOCIAL_BROWSER_PLATFORMS.get(config.platform, {})
    compose_url = str(meta.get("compose_url") or "https://tenchat.ru/editor")

    await page.goto(compose_url, wait_until="domcontentloaded", timeout=25000)
    await asyncio.sleep(2.0)

    final_url = page.url.lower()
    if any(m in final_url for m in ("/login", "/signin", "/sign-in", "/auth/", "oauth.")):
        raise SocialAuthError(
            "TenChat: сессия истекла (перенаправлено на авторизацию). "
            "Обновите куки в настройках."
        )

    await _dismiss_tenchat_tutorial(page)

    # Fill Title
    title_text = str(post_data.get("title") or "").strip()
    title_el = page.locator(
        "div[contenteditable='true']:not(.ql-editor), div.text-3xl"
    ).first
    if await title_el.count() > 0 and await title_el.is_visible():
        await title_el.click(force=True)
        try:
            await title_el.fill(title_text)
        except Exception:  # noqa: BLE001
            await page.keyboard.type(title_text)
        await asyncio.sleep(0.5)

    # Fill Body
    body_text = str(post_data.get("text") or post_data.get("teaser") or "").strip()
    body_el = page.locator("div.ql-editor").first
    if not (await body_el.count() > 0 and await body_el.is_visible()):
        raise SocialAuthError("TenChat: поле ввода статьи не найдено.")
    await body_el.click(force=True)
    try:
        await body_el.fill(body_text)
    except Exception:  # noqa: BLE001
        await page.keyboard.type(body_text)
    await asyncio.sleep(0.8)

    if cover_path:
        await _try_upload_file_on_page(page, cover_path)

    await _select_tenchat_topic(page)

    pub_btn = page.locator("button:has-text('Опубликовать')").first
    if not (await pub_btn.count() > 0 and await pub_btn.is_visible()):
        raise SocialResponseError("TenChat: кнопка «Опубликовать» не найдена.")

    await pub_btn.click(timeout=5000)
    await asyncio.sleep(4.0)

    ext_id = f"tenchat-{uuid.uuid4().hex[:10]}"
    ret_url = config.target_url.strip() if config.target_url else page.url
    return ext_id, ret_url


async def _publish_youtube_post(  # noqa: PLR0912, PLR0915
    page: Any,
    config: SocialBrowserSessionConfig,
    post_data: dict[str, Any],
    cover_path: str | None,
) -> tuple[str, str]:
    """Publish a post with photo attachment to YouTube Community tab."""
    target_url = config.target_url.strip() or "https://www.youtube.com/@svodkapress/posts"
    if "@" in target_url and not target_url.rstrip("/").endswith(("community", "posts")):
        target_url = f"{target_url.rstrip('/')}/posts"
    if "?" not in target_url and target_url.rstrip("/").endswith("posts"):
        target_url = f"{target_url}?show_create_dialog=1"

    # 0. Navigate to YouTube target URL first (so session cookies are loaded)
    await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2.5)

    # Check for Google Cookie Consent wall (consent.youtube.com)
    page_title = ""
    with contextlib.suppress(Exception):
        page_title = await page.title()
    if "consent.youtube" in page.url or "Прежде чем перейти" in page_title:
        logger.info("YouTube: dismissing Google cookie consent dialog")
        await _click_first_matching(
            page,
            (
                "button[aria-label*='Принять все']",
                "button:has-text('Принять все')",
                "button[aria-label*='Accept all']",
                "button:has-text('Accept all')",
            ),
        )
        await asyncio.sleep(4.0)

    # 0.1 If unauthenticated (Войти button visible or redirected to accounts.google.com),
    # attempt SSO hand-off
    avatar_btn = page.locator(
        "ytd-masthead #avatar-btn, ytd-masthead button#avatar-btn"
    ).first
    signin_loc = page.locator(
        "ytd-masthead a[href*='signin'], ytd-masthead button:has-text('Войти')"
    )
    if "accounts.google.com" in page.url or (
        await signin_loc.count() > 0
        and not (await avatar_btn.count() > 0 and await avatar_btn.is_visible())
    ):
        import urllib.parse  # noqa: PLC0415
        enc_dest = urllib.parse.quote(target_url, safe="")
        sso_url = (
            "https://accounts.google.com/ServiceLogin?service=youtube&uilel=3&passive=true"
            f"&continue=https%3A%2F%2Fwww.youtube.com%2Fsignin%3Faction_handle_signin%3Dtrue"
            f"%26app%3Ddesktop%26hl%3Dru%26next%3D{enc_dest}"
        )
        await page.goto(sso_url, wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(2.5)

    # 1. Channel top post deduplication check (prevent duplicate Community posts)
    post_title = str(post_data.get("title") or "")
    article_id = str(post_data.get("article_id") or "")
    if post_title:
        title_prefix = post_title[:35].strip().lower()
        title_words = [w for w in re.split(r"\W+", post_title.lower()) if len(w) >= 4][:4]
        top_posts = page.locator("ytd-backstage-post-thread-renderer #content-text")
        count = await top_posts.count()
        for idx in range(min(count, 3)):
            top_text = (await top_posts.nth(idx).text_content() or "").lower()
            matched = bool(
                (title_prefix and title_prefix in top_text)
                or (len(title_words) >= 3 and all(w in top_text for w in title_words))
            )
            if matched:
                logger.info(
                    "Article %s already posted on YouTube Community; skipping duplicate",
                    article_id or post_title[:30],
                )
                return f"youtube-dup-{article_id or uuid.uuid4().hex[:8]}", page.url

    # 2. Open Community post dialog if textarea is not immediately visible
    textarea = page.locator(
        "#contenteditable-root, div[contenteditable='true'][role='textbox']"
    ).first
    if not await textarea.is_visible():
        # First try clicking the unopened post placeholder directly on the community tab
        placeholder = page.locator(
            "#placeholder-area, #commentbox-placeholder, #unopened-dialog, "
            "ytd-backstage-post-dialog-renderer #placeholder-area"
        ).first
        if await placeholder.count() > 0 and await placeholder.is_visible():
            with contextlib.suppress(Exception):
                await placeholder.click()
                await asyncio.sleep(1.5)

    if not await textarea.is_visible():
        create_btn = page.locator(
            "button[aria-label*='Создать'], ytd-topbar-menu-button-renderer"
        ).first
        if await create_btn.count() > 0 and await create_btn.is_visible():
            await create_btn.click()
            await asyncio.sleep(1.0)
            post_item = page.locator(
                "tp-yt-paper-item:has-text('Создать запись'), "
                "ytd-compact-link-renderer:has-text('Создать запись')"
            ).first
            if await post_item.count() > 0 and await post_item.is_visible():
                await post_item.click()
                await asyncio.sleep(2.5)

    # 3. Attach cover image if available (click 'Изображение' button)
    if cover_path and os.path.exists(cover_path):  # noqa: ASYNC240
        img_btn = page.locator(
            "ytd-commentbox button:has-text('Изображение'), "
            "ytd-commentbox button[aria-label*='Добавить изображение'], "
            "button:has-text('Изображение'):visible, "
            "[aria-label*='Добавить изображение']:visible, "
            "button:has-text('Image'):visible, "
            "[aria-label*='Image' i]:visible"
        ).first
        if await img_btn.count() > 0 and await img_btn.is_visible():
            with contextlib.suppress(Exception):
                await img_btn.click()
                await asyncio.sleep(1.2)
                multi_input = page.locator(
                    "ytd-backstage-multi-image-select-renderer "
                    "#dropzone input[type='file'], "
                    "ytd-backstage-multi-image-select-renderer input[type='file']"
                ).first
                if await multi_input.count() > 0:
                    await multi_input.set_input_files(cover_path)
                    # Wait for image upload thumbnail to render in preview
                    image_rendered = False
                    for _ in range(20):
                        clipper = page.locator(
                            "img#image-in-preview-clipper, "
                            "img.thumbnail-image, "
                            "ytd-backstage-image-preview-renderer img:visible"
                        )
                        if await clipper.count() > 0 and await clipper.first.is_visible():
                            image_rendered = True
                            break
                        await asyncio.sleep(0.5)
                    if not image_rendered:
                        logger.warning(
                            "YouTube: cover image upload timed out or preview "
                            "did not render; cancelling backstage image selector"
                        )
                        cancel_btn = page.locator(
                            "ytd-backstage-multi-image-select-renderer #cancel-button, "
                            "ytd-backstage-multi-image-select-renderer "
                            "button[aria-label*='Отмена' i], "
                            "ytd-backstage-multi-image-select-renderer "
                            "button:has-text('Отмена'), "
                            "ytd-backstage-multi-image-select-renderer "
                            "button:has-text('Cancel')"
                        ).first
                        if await cancel_btn.count() > 0 and await cancel_btn.is_visible():
                            with contextlib.suppress(Exception):
                                await cancel_btn.click()
                                await asyncio.sleep(1.0)

    # 4. Fill text content
    textarea = page.locator(
        "#contenteditable-root, div[contenteditable='true'][role='textbox']"
    ).first
    if not (await textarea.count() > 0 and await textarea.is_visible()):
        placeholder = page.locator(
            "#placeholder-area, #commentbox-placeholder, #unopened-dialog"
        ).first
        if await placeholder.count() > 0 and await placeholder.is_visible():
            with contextlib.suppress(Exception):
                await placeholder.click()
                await asyncio.sleep(1.5)

    if not (await textarea.count() > 0 and await textarea.is_visible()):
        raise SocialAuthError(
            f"YouTube: поле ввода поста не найдено на странице {target_url} "
            "(проверьте авторизацию аккаунта владельца канала)."
        )

    await textarea.click()
    text_content = str(post_data["text"]).strip()
    await page.keyboard.insert_text(text_content)
    await asyncio.sleep(1.5)

    # 5. Click Publish button (target enabled visible button)
    pub_btn = page.locator(
        "ytd-commentbox button:has-text('Опубликовать'):visible"
    ).first
    if not (await pub_btn.count() > 0 and await pub_btn.is_visible()):
        raise SocialResponseError(
            "YouTube: кнопка «Опубликовать» не найдена.",
            retryable=True,
        )

    # Wait up to 15s for the button to become enabled
    for _ in range(30):
        cls = await pub_btn.get_attribute("class") or ""
        dis = await pub_btn.get_attribute("disabled")
        aria_dis = await pub_btn.get_attribute("aria-disabled")
        if "Disabled" not in cls and dis is None and aria_dis != "true":
            break
        await asyncio.sleep(0.5)

    await pub_btn.click(timeout=10000)
    await asyncio.sleep(3.0)

    # 6. Check if YouTube blocked publication with daily limit / feature gate modal
    limit_dialog = page.locator(
        "tp-yt-paper-dialog:visible:has-text('Развивайте свой канал'), "
        "tp-yt-paper-dialog:visible:has-text('правила сообщества'), "
        "ytd-modal-with-bubble-and-content-renderer:visible"
    ).first
    if await limit_dialog.count() > 0 and await limit_dialog.is_visible():
        dialog_text = (await limit_dialog.inner_text()).replace("\n", " ").strip()
        ok_btn = limit_dialog.locator(
            "button:has-text('OK'), button:has-text('ОК')"
        ).first
        if await ok_btn.count() > 0:
            with contextlib.suppress(Exception):
                await ok_btn.click()
        logger.warning(
            "YouTube: publication blocked by channel feature limit: %s", dialog_text
        )
        raise SocialResponseError(
            f"YouTube: исчерпан суточный лимит постов («{dialog_text[:90]}»). "
            "Требуется подтвердить продвинутые функции канала в YouTube Studio.",
            retryable=False,
        )

    ext_id = f"youtube-{uuid.uuid4().hex[:10]}"
    return ext_id, page.url


async def _publish_setka_post(  # noqa: PLR0912, PLR0915
    page: Any,
    config: SocialBrowserSessionConfig,
    post_data: dict[str, Any],
    cover_path: str | None,
) -> tuple[str, str]:
    """Publish a post to Setka (setka.ru) via /posts/regular/new composer.

    Post URL resolution strategy (ordered by reliability):
    1. Intercept ``POST /posts/regular/create`` (or ``/api/``) headers/body
       and fail fast on 429.
    2. Poll ``page.url`` for navigation to ``/posts/{uuid}``.
    3. Check author profile for a UUIDv7 post created after ``publish_started_ms``.
    """
    compose_url = "https://setka.ru/posts/regular/new"
    await page.goto(compose_url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3.0)

    # Dismiss any cookie banner or onboarding modals
    await _click_first_matching(
        page,
        (
            "#cookie-consent-accept",
            "button:has-text('окей')",
            "button:has-text('закрыть')",
            "button:has-text('супер, всё понятно')",
        ),
    )
    await asyncio.sleep(0.8)

    # 1. Fill title (Setka regular editor enforces a strict 60-grapheme limit)
    title_text = str(post_data.get("title") or "").strip()[:60]
    title_el = await page.query_selector(
        'textarea[name="title"], textarea[placeholder*="заголовок" i]'
    )
    if title_el and title_text:
        await title_el.fill(title_text)
        await asyncio.sleep(0.5)

    # 2. Fill post body in tiptap editor
    body_text = str(post_data.get("text") or "")
    body_el = await page.query_selector(
        'div.tiptap[role="textbox"], div[contenteditable="true"]'
    )
    if not body_el and not title_el:
        raise SocialAuthError(
            "Сетка: поле ввода поста не найдено на /posts/regular/new "
            "(возможно, истекла браузерная сессия)."
        )

    if body_el:
        await body_el.click()
        await asyncio.sleep(0.3)
        await page.evaluate(
            """(text) => {
                const el = (
                    document.querySelector('div.tiptap[role="textbox"]') ||
                    document.activeElement
                );
                if (!el) return;
                const dt = new DataTransfer();
                dt.setData('text/plain', text);
                const pasteEvent = new ClipboardEvent('paste', {
                    bubbles: true,
                    cancelable: true,
                    clipboardData: dt
                });
                el.dispatchEvent(pasteEvent);
            }""",
            body_text,
        )
        await asyncio.sleep(0.5)
        current_text = await body_el.inner_text()
        if not current_text.strip():
            await body_el.type(body_text[:500])

    await asyncio.sleep(1.0)

    # 3. Attach cover image if available
    if cover_path and Path(cover_path).is_file():  # noqa: ASYNC240
        file_input = await page.query_selector('input[type="file"]')
        if file_input:
            try:
                await page.set_input_files('input[type="file"]', cover_path)
                logger.info("Setka: attached cover image from %s", cover_path)
                await asyncio.sleep(3.5)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Setka: failed to attach image: %s", exc)

    # Dismiss #error-modal if image upload triggered an error dialog
    await _click_first_matching(
        page,
        (
            "#error-modal:not(.hidden) #error-modal-close-button",
            "#error-modal:not(.hidden) #error-modal-x-close-button",
            "#error-modal:not(.hidden) button",
        ),
    )

    # 4. Click publish button (Setka uses accent submit button)
    publish_btn = None
    for selector in (
        "button#publish-button",
        "button[type='submit'][class*='bg-component-button-background-accent']",
        "button[class*='bg-component-button-background-accent']",
        "button:has-text('Опубликовать')",
        "button:has-text('Готово')",
    ):
        el = await page.query_selector(selector)
        if el:
            for _ in range(12):
                if not await el.is_disabled():
                    publish_btn = el
                    break
                await asyncio.sleep(0.3)
            if publish_btn:
                break

    if not publish_btn:
        raise SocialResponseError(
            "Сетка: кнопка публикации не найдена или осталась неактивной.",
            retryable=True,
        )

    publish_started_ms = int(datetime.now(UTC).timestamp() * 1000)
    captured_post_id: str = ""
    create_error_msg: str = ""

    async def _on_create_response(response: Any) -> None:
        nonlocal captured_post_id, create_error_msg
        url = response.url
        if "/posts/regular/create" not in url and "/api/" not in url:
            return
        if "/posts/regular/create" in url and response.status >= 400:
            err_detail = f"HTTP {response.status}"
            with contextlib.suppress(Exception):
                err_json = await response.json()
                if isinstance(err_json, dict) and err_json.get("message"):
                    err_detail = str(err_json["message"])
            create_error_msg = f"Сетка ({response.status}): {err_detail}"
            logger.warning("Setka create rejected: %s", create_error_msg)
            return

        if captured_post_id or response.status < 200 or response.status >= 400:
            return

        # Check HTMX redirect/push headers on /posts/regular/create
        headers = {k.lower(): v for k, v in response.headers.items()}
        for h_name in ("hx-redirect", "hx-push-url", "hx-location", "location"):
            h_val = headers.get(h_name, "")
            m_hdr = re.search(r"/posts/([0-9a-fA-F-]{20,})", h_val)
            if m_hdr:
                captured_post_id = m_hdr.group(1)
                logger.info(
                    "Setka: captured post ID from %s: %s",
                    h_name,
                    captured_post_id,
                )
                return

        try:
            body_text_resp = await response.text()
        except Exception:  # noqa: BLE001
            return

        m_body = re.search(r"/posts/([0-9a-fA-F-]{32,36})", body_text_resp)
        if m_body and "regular" not in m_body.group(1):
            captured_post_id = m_body.group(1)
            logger.info(
                "Setka: captured post ID from response body: %s",
                captured_post_id,
            )

    page.on("response", _on_create_response)

    try:
        await publish_btn.click()
        logger.info("Setka: clicked publish button, awaiting post resolution...")

        final_post_url = ""
        post_id = ""

        for _ in range(20):
            await asyncio.sleep(0.5)
            if create_error_msg:
                is_rate_limit = "429" in create_error_msg
                raise SocialResponseError(create_error_msg, retryable=not is_rate_limit)
            if captured_post_id:
                post_id = captured_post_id
                final_post_url = f"https://setka.ru/posts/{post_id}"
                break
            cur_url = page.url
            if "/posts/" in cur_url and "/new" not in cur_url and "/edit" not in cur_url:
                m = re.search(r"/posts/([0-9a-fA-F-]+)", cur_url)
                if m:
                    post_id = m.group(1)
                    final_post_url = f"https://setka.ru/posts/{post_id}"
                    break
    finally:
        page.remove_listener("response", _on_create_response)

    if create_error_msg:
        is_rate_limit = "429" in create_error_msg
        raise SocialResponseError(create_error_msg, retryable=not is_rate_limit)

    # --- Strategy 3: check author profile for a post created during this call ---
    if not final_post_url and config.target_url:
        logger.info(
            "Setka: composer URL remained %s, checking profile %s",
            page.url,
            config.target_url,
        )
        try:
            await page.goto(
                config.target_url, wait_until="domcontentloaded", timeout=20000
            )
            await asyncio.sleep(2.5)

            profile_ids = await page.evaluate(
                """() => {
                    const links = document.querySelectorAll('a[href*="/posts/"]');
                    const ids = new Set();
                    for (const a of links) {
                        const m = a.href.match(/\\/posts\\/([0-9a-fA-F-]{20,})/);
                        if (m) ids.add(m[1]);
                    }
                    return [...ids];
                }"""
            )

            if profile_ids:
                newest = max(profile_ids)
                # Setka uses UUIDv7 where the first 12 hex digits encode epoch ms.
                # Verify the post was created after publish_started_ms - 15s.
                hex_clean = newest.replace("-", "")
                is_fresh = False
                if len(hex_clean) >= 12:
                    with contextlib.suppress(ValueError):
                        uuid_ms = int(hex_clean[:12], 16)
                        if uuid_ms >= publish_started_ms - 15_000:
                            is_fresh = True
                if is_fresh:
                    post_id = newest
                    final_post_url = f"https://setka.ru/posts/{post_id}"
                    logger.info(
                        "Setka: verified fresh UUIDv7 post from profile: %s",
                        final_post_url,
                    )
                else:
                    logger.warning(
                        "Setka: latest profile post %s is stale (predates publish click)",
                        newest,
                    )
        except Exception as prof_err:  # noqa: BLE001
            logger.warning(
                "Setka: failed to resolve post from author profile: %s", prof_err
            )

    if not final_post_url or "/new" in final_post_url:
        raise SocialResponseError(
            f"Сетка: публикация не подтверждена, URL остался {page.url}. "
            "Проверьте сессию аккаунта и лимиты публикаций.",
            retryable=True,
        )

    ext_id = post_id or f"setka-{uuid.uuid4().hex[:10]}"
    logger.info(
        "Setka: successfully published post: %s (id: %s)", final_post_url, ext_id
    )
    return ext_id, final_post_url


async def _publish_spark_post(  # noqa: PLR0912, PLR0915
    page: Any,
    config: SocialBrowserSessionConfig,
    post_data: dict[str, Any],
    cover_path: str | None,
) -> tuple[str, str]:
    """Automate publishing article/news to Spark.ru startup blog."""
    target_url = config.target_url.strip() or "https://spark.ru/startup/svodka-press"
    compose_url = "https://spark.ru/write"

    await page.goto(compose_url, wait_until="domcontentloaded", timeout=25000)
    await asyncio.sleep(2.0)

    final_url = page.url.lower()
    has_auth_btn = await page.locator("a[href='/auth'].auth-block").count() > 0
    if "/auth" in final_url or has_auth_btn:
        uid = 0
        with contextlib.suppress(Exception):
            uid = await page.evaluate("() => window.uid || 0")
        editor_sel = ".post-form__input_title, .ce-paragraph, .post-form__submit"
        has_editor = await page.locator(editor_sel).count() > 0
        if uid == 0 and not has_editor:
            raise SocialAuthError(
                "Spark.ru: сессия не авторизована (перенаправлено на /auth). "
                "Выполните вход через IP сервера."
            )

    # Step 1: Ensure Svodka Press blog is selected in target dropdown
    with contextlib.suppress(Exception):
        await page.evaluate("""() => {
            const sel = ".simple-selector__item[data-target='44324']";
            const item = document.querySelector(sel);
            if (item) item.click();
        }""")
        await asyncio.sleep(0.5)

    title_text = str(post_data.get("title") or "").strip()

    # Step 2: Check top post deduplication on startup blog
    with contextlib.suppress(Exception):
        post_sel = ".posts-list-item h2, .posts-list-item a, .article h2"
        top_post = page.locator(post_sel).first
        if await top_post.count() > 0:
            top_title = (await top_post.text_content() or "").strip()
            if title_text[:35].lower() in top_title.lower():
                logger.info(
                    "Spark.ru: пост '%s' уже опубликован в блоге; пропускаем дубликат",
                    title_text[:40],
                )
                return f"spark-dup-{uuid.uuid4().hex[:8]}", target_url

    # Step 3: Fill Title
    title_el = page.locator(
        "input[name='title'], .post-form__input_title, textarea[name='title']"
    ).first
    if await title_el.count() > 0 and await title_el.is_visible():
        await title_el.click(force=True)
        try:
            await title_el.fill(title_text)
        except Exception:  # noqa: BLE001
            await page.keyboard.insert_text(title_text)
        await asyncio.sleep(0.5)

    # Step 4: Fill Subtitle (Spark has subtitle field for teaser, max 320 chars)
    sub_text = str(post_data.get("lead") or post_data.get("teaser") or "").strip()
    if not sub_text and post_data.get("text"):
        sub_text = str(post_data.get("text"))[:150].strip()
    sub_el = page.locator("textarea[name='subtitle'], .post-form__input_subtitle").first
    if await sub_el.count() > 0 and await sub_el.is_visible() and sub_text:
        with contextlib.suppress(Exception):
            await sub_el.fill(sub_text[:300])
        await asyncio.sleep(0.3)

    # Step 5: Fill Body (Editor.js)
    body_text = str(
        post_data.get("text")
        or post_data.get("full_text")
        or post_data.get("content")
        or ""
    ).strip()
    article_url = str(post_data.get("canonical_url") or "").strip()
    if article_url and article_url not in body_text:
        body_text += f"\n\nПодробнее на SvodkaPress: {article_url}"

    ce_editable = page.locator(
        ".ce-paragraph[contenteditable='true'], div[contenteditable='true']"
    ).first
    if await ce_editable.count() > 0 and await ce_editable.is_visible():
        await ce_editable.click(force=True)
        await page.keyboard.insert_text(body_text)
        await asyncio.sleep(0.8)

    # Step 6: Tags
    tags_list = post_data.get("tags") or []
    tags_text = (
        ", ".join(tags_list[:5])
        if isinstance(tags_list, list)
        else str(tags_list)
    )
    if not tags_text:
        tags_text = "новости, события, россия"
    tags_el = page.locator("input[name='tags'], .post-form__input_tags").first
    if await tags_el.count() > 0 and await tags_el.is_visible():
        with contextlib.suppress(Exception):
            await tags_el.fill(tags_text[:100])
        await asyncio.sleep(0.3)

    # Step 7: Upload Cover Image if available
    if cover_path and os.path.exists(cover_path):  # noqa: ASYNC240
        file_input = page.locator(
            "#transportInput, input[type='file'][name='files'], "
            "input[type='file'][name='cover'], input[type='file'][accept*='image']"
        ).first
        if await file_input.count() > 0:
            with contextlib.suppress(Exception):
                await file_input.set_input_files(cover_path)
                await asyncio.sleep(2.0)

    # Step 8: Submit / Publish
    pub_btn = page.locator(
        ".post-form__submit, div:has-text('Опубликовать').button, "
        "div.post-form__submit, input[type='submit'][value*='Опубликовать']"
    ).first

    if not (await pub_btn.count() > 0 and await pub_btn.is_visible()):
        raise SocialResponseError(
            "Spark.ru: кнопка публикации поста не найдена на странице."
        )

    await pub_btn.click(timeout=8000)
    await asyncio.sleep(4.0)

    curr_url = page.url
    post_id = uuid.uuid4().hex[:10]
    import re  # noqa: PLC0415
    m = re.search(r"/(?:blog|post)/([0-9]+)", curr_url)
    if m:
        post_id = m.group(1)
        res_url = curr_url
    else:
        res_url = target_url

    logger.info("Spark.ru post published successfully: ID %s -> %s", post_id, res_url)
    return f"spark-{post_id}", res_url


async def publish_via_social_browser(
    config: SocialBrowserSessionConfig,
    article: Article,
    wp_post_url: str,
    *,
    cover_url: str = "",
    session: AsyncSession | None = None,
) -> PublishResult:
    """Publish an article to a browser-authenticated social network."""
    if not config.is_configured:
        raise SocialNotConfiguredError(
            f"{config.platform} is not configured; skipping"
        )

    post_data = format_social_browser_post(article, wp_post_url, config.platform)
    resolved_cover = cover_url.strip() or str(post_data.get("cover_url") or "")
    if not resolved_cover and getattr(article, "wp_image_id", None):
        with contextlib.suppress(Exception):
            from app.workers.publisher_worker import _resolve_cover_url  # noqa: PLC0415
            resolved_cover = await _resolve_cover_url(article)
    post_data["cover_url"] = resolved_cover
    cover_path = await _download_temp_cover(resolved_cover)

    try:
        async with persistent_platform_page(config, timeout_seconds=40.0) as page:
            if config.platform == Platform.PINTEREST.value:
                ext_id, final_url = await _publish_pinterest_pin(
                    page, post_data, cover_path
                )
            elif config.platform == Platform.TENCHAT.value:
                ext_id, final_url = await _publish_tenchat_post(
                    page, config, post_data, cover_path
                )
            elif config.platform == Platform.YOUTUBE.value:
                ext_id, final_url = await _publish_youtube_post(
                    page, config, post_data, cover_path
                )
            elif config.platform == Platform.SETKA.value:
                ext_id, final_url = await _publish_setka_post(
                    page, config, post_data, cover_path
                )
            elif config.platform == Platform.SPARK.value:
                ext_id, final_url = await _publish_spark_post(
                    page, config, post_data, cover_path
                )
            else:
                ext_id, final_url = await _publish_generic_composer(
                    page, config, post_data, cover_path
                )

            # Auto-save refreshed cookies to keep rolling tokens (like *PSIDTS) alive
            with contextlib.suppress(Exception):
                refreshed = await page.context.cookies()
                if refreshed and session is not None:
                    config.storage_state = normalize_social_storage_state(
                        refreshed, config.platform, config.storage_state
                    )
                    config.last_checked_at = datetime.now(UTC).strftime(
                        "%Y-%m-%d %H:%M:%S UTC"
                    )
                    await save_social_browser_config(session, config)

            return PublishResult.published(
                config.platform,
                external_id=ext_id,
                url=final_url,
            )
    finally:
        if cover_path:
            with contextlib.suppress(OSError):
                os.unlink(cover_path)


class BrowserSocialAdapter(BaseSocialAdapter):
    """Base adapter wrapping :class:`SocialBrowserSessionConfig`."""

    def __init__(
        self,
        platform: Platform,
        config: SocialBrowserSessionConfig | None = None,
    ) -> None:
        self.platform = platform
        self._config = config or SocialBrowserSessionConfig(
            platform=platform.value, is_active=False
        )

    @property
    def is_configured(self) -> bool:
        return self._config.is_configured

    async def publish(
        self, article: Article, wp_post_url: str, **kwargs: Any
    ) -> PublishResult:
        kw_cover = str(kwargs.get("cover_url") or kwargs.get("photo_url") or "")
        session = kwargs.get("session")
        return await publish_via_social_browser(
            self._config,
            article,
            wp_post_url,
            cover_url=kw_cover,
            session=session if isinstance(session, AsyncSession) else None,
        )


class SetkaBrowserAdapter(BrowserSocialAdapter):
    """Adapter for Setka (https://setka.ru/)."""

    def __init__(
        self, config: SocialBrowserSessionConfig | None = None
    ) -> None:
        super().__init__(Platform.SETKA, config)


class TenChatBrowserAdapter(BrowserSocialAdapter):
    """Adapter for TenChat (https://tenchat.ru/)."""

    def __init__(
        self, config: SocialBrowserSessionConfig | None = None
    ) -> None:
        super().__init__(Platform.TENCHAT, config)


class ThreadsBrowserAdapter(BrowserSocialAdapter):
    """Adapter for Threads (https://www.threads.com/)."""

    def __init__(
        self, config: SocialBrowserSessionConfig | None = None
    ) -> None:
        super().__init__(Platform.THREADS, config)


class FacebookBrowserAdapter(BrowserSocialAdapter):
    """Adapter for Facebook (https://www.facebook.com/)."""

    def __init__(
        self, config: SocialBrowserSessionConfig | None = None
    ) -> None:
        super().__init__(Platform.FACEBOOK, config)


class PinterestBrowserAdapter(BrowserSocialAdapter):
    """Adapter for Pinterest (https://ru.pinterest.com/)."""

    def __init__(
        self, config: SocialBrowserSessionConfig | None = None
    ) -> None:
        super().__init__(Platform.PINTEREST, config)


class YouTubeCommunityBrowserAdapter(BrowserSocialAdapter):
    """Adapter for YouTube Community posts (https://www.youtube.com/)."""

    def __init__(
        self, config: SocialBrowserSessionConfig | None = None
    ) -> None:
        super().__init__(Platform.YOUTUBE, config)


class SparkBrowserAdapter(BrowserSocialAdapter):
    """Adapter for Spark.ru startup blog (https://spark.ru/)."""

    def __init__(
        self, config: SocialBrowserSessionConfig | None = None
    ) -> None:
        super().__init__(Platform.SPARK, config)


def build_social_browser_adapters(
    configs: Mapping[str, SocialBrowserSessionConfig] | None = None,
) -> list[BaseSocialAdapter]:
    """Instantiate all 7 browser social adapters."""
    cfg_map = configs or {}
    return [
        SetkaBrowserAdapter(cfg_map.get(Platform.SETKA.value)),
        TenChatBrowserAdapter(cfg_map.get(Platform.TENCHAT.value)),
        ThreadsBrowserAdapter(cfg_map.get(Platform.THREADS.value)),
        FacebookBrowserAdapter(cfg_map.get(Platform.FACEBOOK.value)),
        PinterestBrowserAdapter(cfg_map.get(Platform.PINTEREST.value)),
        YouTubeCommunityBrowserAdapter(cfg_map.get(Platform.YOUTUBE.value)),
        SparkBrowserAdapter(cfg_map.get(Platform.SPARK.value)),
    ]


async def keep_active_browser_sessions_alive(
    session: AsyncSession | None = None,
    *,
    max_idle_hours: float = 3.0,
) -> dict[str, str]:
    """Touch active browser sessions periodically to keep rolling tokens fresh.

    Google / YouTube issues rolling __Secure-1PSIDTS cookies that expire after
    ~24h if idle. A lightweight navigation refreshes these tokens and persists
    them back to PostgreSQL so the session never dies.
    """
    from app.database import AsyncSessionLocal  # noqa: PLC0415

    results: dict[str, str] = {}
    owns_session = session is None
    sess = AsyncSessionLocal() if owns_session else session

    try:
        all_cfgs = await load_all_social_browser_configs(sess)
        for platform, cfg in all_cfgs.items():
            if not cfg.is_active or not cfg.has_session:
                continue

            # TenChat tokens expire very quickly without active use; enforce a 5-min ping
            plat_max_idle = 0.08 if platform == Platform.TENCHAT.value else max_idle_hours

            needs_ping = True
            if cfg.last_checked_at:
                with contextlib.suppress(Exception):
                    raw_ts = cfg.last_checked_at.replace(" UTC", "")
                    last_dt = datetime.strptime(
                        raw_ts, "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=UTC)
                    elapsed_hours = (
                        datetime.now(UTC) - last_dt
                    ).total_seconds() / 3600.0
                    if elapsed_hours < plat_max_idle:
                        needs_ping = False

            if not needs_ping:
                continue

            logger.info("Keep-alive probe starting for %s...", platform)
            probe_res = await probe_social_browser_session(cfg, session=sess)
            results[platform] = str(probe_res.get("message") or "")
            logger.info("Keep-alive finished for %s", platform)
    finally:
        if owns_session and sess is not None:
            await sess.close()

    return results

