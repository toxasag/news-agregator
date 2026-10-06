"""Dzen Web Publisher: authenticated Playwright automation with storage_state.

Implements browser-based publication to Yandex Dzen Studio:
- Encrypted storage of Playwright session (`storage_state.json`) via Fernet
- Live session health verification (`probe_dzen_session`)
- Isolated Dzen promotion/banner injection (`inject_dzen_promo`)
- Native browser DOM automation for article creation in Dzen Studio
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    Page,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import SecretBox, get_secret_box
from app.models.article import Article
from app.models.enums import Platform
from app.models.setting import SystemSetting
from app.services.social_base import (
    PublishResult,
    SocialAuthError,
    SocialNotConfiguredError,
    SocialResponseError,
)
from app.services.social_dzen import build_dzen_html
from app.services.web_scraper import get_browser_pool

logger: Final[logging.Logger] = logging.getLogger(__name__)

DZEN_SESSION_KEY: Final[str] = "dzen_browser_session"
DZEN_EDITOR_URL: Final[str] = "https://dzen.ru/profile/editor"
DZEN_CREATE_ARTICLE_URL: Final[str] = "https://dzen.ru/profile/editor/create/article"
_DISMISS_POPUPS_JS: Final[str] = (
    "() => { document.querySelectorAll('.ReactModalPortal, "
    "[class*=\"help-popup\"], [data-testid=\"modal-overlay\"]')"
    ".forEach(el => el.remove()); }"
)

__all__ = [
    "DZEN_CREATE_ARTICLE_URL",
    "DZEN_EDITOR_URL",
    "DZEN_SESSION_KEY",
    "DzenBrowserPublisher",
    "DzenSessionConfig",
    "DzenSessionProbeResult",
    "inject_dzen_promo",
    "linkify_dzen_promo",
    "load_dzen_session_config",
    "normalize_dzen_storage_state",
    "probe_dzen_session",
    "save_dzen_session_config",
]


def _get_box() -> SecretBox | None:
    try:
        return get_secret_box()
    except (ValueError, TypeError):
        return None


def _encrypt_val(value: str) -> str:
    if not value:
        return ""
    box = _get_box()
    if box is not None:
        return box.encrypt(value)
    return value


def _decrypt_val(value: str) -> str:
    if not value:
        return ""
    box = _get_box()
    if box is not None:
        try:
            return box.decrypt(value)
        except (ValueError, TypeError):
            return value
    return value


def _clean_single_cookie(c: dict[str, Any]) -> dict[str, Any] | None:
    """Validate and format a single cookie dict for Playwright compatibility."""
    name = str(c.get("name", "")).strip()
    if not name:
        return None

    raw_domain = str(c.get("domain", "")).strip() or ".dzen.ru"
    cookie: dict[str, Any] = {
        "name": name,
        "value": str(c.get("value", "")),
        "domain": raw_domain,
        "path": str(c.get("path", "/")),
    }

    # Expiration (expirationDate in extensions -> expires in Playwright)
    exp = c.get("expires")
    if exp is None:
        exp = c.get("expirationDate")
    if exp is not None:
        with contextlib.suppress(ValueError, TypeError):
            cookie["expires"] = float(exp)

    if "httpOnly" in c:
        cookie["httpOnly"] = bool(c["httpOnly"])
    if "secure" in c:
        cookie["secure"] = bool(c["secure"])

    # Strict Playwright sameSite: 'Strict' | 'Lax' | 'None'
    ss = str(c.get("sameSite", "")).lower().strip()
    if ss in ("strict",):
        cookie["sameSite"] = "Strict"
    elif ss in ("lax",):
        cookie["sameSite"] = "Lax"
    else:
        cookie["sameSite"] = "None"

    return cookie


def normalize_dzen_storage_state(raw_state: Any) -> dict[str, Any]:
    """Normalize raw cookie dump from Chrome extensions or Playwright."""
    if isinstance(raw_state, list):
        cookies_list = raw_state
        origins_list: list[Any] = []
    elif isinstance(raw_state, dict):
        cookies_list = raw_state.get("cookies", [])
        origins_list = raw_state.get("origins", [])
    else:
        return {"cookies": [], "origins": []}

    cleaned_cookies: list[dict[str, Any]] = []
    for c in cookies_list:
        if isinstance(c, dict):
            cleaned = _clean_single_cookie(c)
            if cleaned is not None:
                cleaned_cookies.append(cleaned)

    return {
        "cookies": cleaned_cookies,
        "origins": origins_list if isinstance(origins_list, list) else [],
    }


@dataclass
class DzenSessionConfig:
    """Configuration and credentials for browser-based Dzen publishing."""

    storage_state: dict[str, Any] | None = None
    channel_url: str = ""
    channel_name: str = ""
    is_active: bool = False
    last_checked_at: str | None = None
    # Options: not_configured, valid, expired, captcha, error
    status: str = "not_configured"
    error_message: str | None = None
    promo_mode: str = "none"  # none, native_banner, promo_box, custom_html
    promo_snippet: str = ""
    publication_mode: str = "publish"  # "publish", "draft"

    @property
    def has_session(self) -> bool:
        """Whether a storage_state payload is present."""
        return bool(self.storage_state and isinstance(self.storage_state, dict))

    def to_db_dict(self) -> dict[str, Any]:
        """Serialize configuration for database storage with encrypted session."""
        norm_state = (
            normalize_dzen_storage_state(self.storage_state)
            if self.storage_state
            else None
        )
        raw_state = json.dumps(norm_state) if norm_state else ""
        return {
            "encrypted_storage_state": _encrypt_val(raw_state),
            "channel_url": self.channel_url,
            "channel_name": self.channel_name,
            "is_active": self.is_active,
            "last_checked_at": self.last_checked_at,
            "status": self.status,
            "error_message": self.error_message,
            "promo_mode": self.promo_mode,
            "promo_snippet": self.promo_snippet,
            "publication_mode": self.publication_mode,
        }

    @classmethod
    def from_db_dict(cls, data: dict[str, Any]) -> DzenSessionConfig:
        """Deserialize configuration, decrypting storage_state."""
        encrypted_state = data.get("encrypted_storage_state", "")
        decrypted_state_str = _decrypt_val(encrypted_state)
        storage_state: dict[str, Any] | None = None
        if decrypted_state_str:
            try:
                parsed = json.loads(decrypted_state_str)
                if isinstance(parsed, (dict, list)):
                    storage_state = normalize_dzen_storage_state(parsed)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.warning("Failed to parse decrypted Dzen storage_state: %s", exc)

        return cls(
            storage_state=storage_state,
            channel_url=str(data.get("channel_url", "") or ""),
            channel_name=str(data.get("channel_name", "") or ""),
            is_active=bool(data.get("is_active", False)),
            last_checked_at=data.get("last_checked_at"),
            status=str(data.get("status", "not_configured")),
            error_message=data.get("error_message"),
            promo_mode=str(data.get("promo_mode", "none")),
            promo_snippet=str(data.get("promo_snippet", "") or ""),
            publication_mode=str(data.get("publication_mode", "publish")),
        )


@dataclass(slots=True)
class DzenSessionProbeResult:
    """Outcome of probing a Dzen browser session."""

    is_valid: bool
    status: str
    message: str
    channel_name: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    )


async def load_dzen_session_config(session: AsyncSession) -> DzenSessionConfig:
    """Load Dzen browser session configuration from system_settings."""
    try:
        stmt = select(SystemSetting.value).where(SystemSetting.key == DZEN_SESSION_KEY)
        result = await session.execute(stmt)
        raw = result.scalar_one_or_none()
        if isinstance(raw, dict):
            return DzenSessionConfig.from_db_dict(raw)
    except SQLAlchemyError as exc:
        logger.error("Failed to load Dzen session config from DB: %s", exc)
    return DzenSessionConfig()


async def save_dzen_session_config(
    session: AsyncSession, config: DzenSessionConfig
) -> None:
    """Save Dzen browser session configuration to system_settings."""
    doc = config.to_db_dict()
    stmt = (
        pg_insert(SystemSetting)
        .values(
            key=DZEN_SESSION_KEY,
            value=doc,
            description="Dzen browser publication session and promo settings",
        )
        .on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={"value": doc, "updated_at": datetime.now(UTC)},
        )
    )
    await session.execute(stmt)
    await session.commit()


def linkify_dzen_promo(
    text: str, wp_post_url: str = "https://svodka.press/"
) -> str:
    """Ensure mentions of website and Telegram channel have clickable HTML links."""
    if not text:
        return text

    target_site_url = "https://svodka.press/"
    target_tg_url = "https://t.me/svodka_press"

    res = text
    # Check if links already exist
    has_site_link = bool(
        re.search(
            r'<a\s+[^>]*href=["\'][^"\']*(?:svodka|http)[^"\']*["\'][^>]*>',
            res,
            re.IGNORECASE,
        )
    )
    has_tg_link = bool(
        re.search(
            r'<a\s+[^>]*href=["\'][^"\']*(?:t\.me|telegram)[^"\']*["\'][^>]*>',
            res,
            re.IGNORECASE,
        )
    )

    if not has_site_link:
        # Linkify "сайте", "сайте SvodkaPress" while preserving leading "на "
        site_link = (
            f'<a href="{target_site_url}" target="_blank" '
            'rel="noopener noreferrer">сайте SvodkaPress</a>'
        )
        res = re.sub(
            r"(?i)\bсайте(?:\s+SvodkaPress|\s+svodka\.press)?\b",  # noqa: RUF001
            site_link,
            res,
            count=1,
        )

    if not has_tg_link:
        # Linkify "телеграм канале", "Telegram-канале" while preserving leading "в "
        tg_link = (
            f'<a href="{target_tg_url}" target="_blank" '
            'rel="noopener noreferrer">Telegram-канале</a>'
        )
        res = re.sub(
            r"(?i)\b(?:телеграм|telegram)(?:-|\s+)?канале\b",
            tg_link,
            res,
            count=1,
        )

    return res


def inject_dzen_promo(
    body_html: str,
    promo_mode: str,
    promo_snippet: str,
    wp_post_url: str = "https://svodka.press/",
) -> str:
    """Inject Dzen-specific advertisement or promotion into the article HTML.

    Allows running campaigns on Dzen that differ from the main website.
    Mentions of site or Telegram are automatically enriched with live links.
    """
    clean_snippet = promo_snippet.strip()
    if promo_mode == "none" or not clean_snippet:
        return body_html

    # Ensure any text snippets have live clickable links
    clean_snippet = linkify_dzen_promo(clean_snippet, wp_post_url=wp_post_url)

    # Avoid injecting a duplicate if the article body already contains a promo block
    # or partner link
    if (
        "dzen-native-banner" in body_html
        or "dzen-promo-box" in body_html
        or "svodka-promo-card" in body_html
        or "FreeAPI" in body_html
        or "freeapiai.ru" in body_html
        or "Партнерский материал" in body_html
        or "▫️ ▫️ ▫️" in body_html
    ):
        return body_html

    formatted_ad = ""
    if promo_mode == "native_banner":
        formatted_ad = (
            "\n<p>▫️ ▫️ ▫️</p>\n"
            '<p class="dzen-native-banner">'
            "<b>🚀 Возможности для авторов | Партнерский материал</b></p>\n"
            f"{clean_snippet}\n"
            "<p>▫️ ▫️ ▫️</p>\n"
        )
    elif promo_mode == "promo_box":
        formatted_ad = (
            "\n<p>▫️ ▫️ ▫️</p>\n"
            f'<blockquote class="dzen-promo-box">\n{clean_snippet}\n</blockquote>\n'
            "<p>▫️ ▫️ ▫️</p>\n"
        )
    elif promo_mode == "custom_html":
        formatted_ad = clean_snippet
    else:
        return body_html

    # Insert after the 2nd paragraph if >= 3 paragraphs, else before last or at the end
    p_splits = re.split(r"(</p>)", body_html, flags=re.IGNORECASE)
    if len(p_splits) >= 5:  # At least 2 full <p>...</p> blocks
        return (
            "".join(p_splits[:4])
            + "\n"
            + formatted_ad
            + "\n"
            + "".join(p_splits[4:])
        )

    return f"{body_html}\n{formatted_ad}"


async def _inspect_dzen_page(
    page: Page,
    current_url: str,
    config: DzenSessionConfig,
    timestamp: str,
) -> DzenSessionProbeResult:
    """Analyze loaded Dzen page DOM to determine authentication status."""
    login_btn = await page.query_selector(
        "button:has-text('Войти'), a:has-text('Войти'), "
        "[data-testid='login-button']"
    )

    if "passport.yandex." in current_url or "/auth" in current_url:
        return DzenSessionProbeResult(
            is_valid=False,
            status="expired",
            message=(
                "Сессия не авторизована: произошло перенаправление на страницу входа "
                "(passport.yandex.ru). Сессия завершена или недействительна."
            ),
            timestamp=timestamp,
        )

    if login_btn is not None:
        return DzenSessionProbeResult(
            is_valid=False,
            status="expired",
            message=(
                "Сессия не авторизована: страница открылась в режиме гостя "
                "(отображается кнопка «Войти»). В экспортированных куках "  # noqa: RUF001
                "отсутствуют активные сессионные ключи Яндекса (Session_id) "
                "или сессия завершена. Войдите в Студию Дзена в браузере и "
                "скопируйте сессию заново."
            ),
            timestamp=timestamp,
        )

    if "smartcaptcha" in current_url or "showcaptcha" in current_url:
        return DzenSessionProbeResult(
            is_valid=False,
            status="captcha",
            message="Обнаружена SmartCaptcha Яндекса. Требуется ручной вход.",
            timestamp=timestamp,
        )

    title = ""
    with contextlib.suppress(PlaywrightError):
        title = await page.title()

    if "страница не найдена" in title.lower():
        return DzenSessionProbeResult(
            is_valid=False,
            status="error",
            message=(
                f"Страница редактора не найдена ({current_url}). "
                "Проверьте ссылку на канал."
            ),
            timestamp=timestamp,
        )

    raw_title = title.replace("Студия", "").strip(" —-|")
    channel_name = config.channel_name or raw_title
    return DzenSessionProbeResult(
        is_valid=True,
        status="valid",
        message=(
            f"Сессия активна! Доступ к Студии подтвержден "
            f"({channel_name or 'Канал авторизован'})."
        ),
        channel_name=channel_name,
        timestamp=timestamp,
    )


async def probe_dzen_session(
    config: DzenSessionConfig | None = None,
    session: AsyncSession | None = None,
) -> DzenSessionProbeResult:
    """Verify live status of Dzen browser session using Playwright stealth."""
    if config is None and session is not None:
        config = await load_dzen_session_config(session)

    if config is None or not config.has_session:
        return DzenSessionProbeResult(
            is_valid=False,
            status="not_configured",
            message="storage_state не настроен. Загрузите файл сессии или куки.",
        )

    pool = get_browser_pool()
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Determine channel-specific studio URL
    channel_clean = config.channel_name.strip()
    if not channel_clean and config.channel_url:
        channel_clean = config.channel_url.rstrip("/").split("/")[-1]
    target_url = (
        f"https://dzen.ru/profile/editor/{channel_clean}"
        if channel_clean
        else "https://dzen.ru/studio"
    )

    clean_state = normalize_dzen_storage_state(config.storage_state)

    try:
        async with pool.page(
            storage_state=clean_state, block_resources=False
        ) as page:
            await page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=25000,
            )
            await page.wait_for_timeout(3000)
            result = await _inspect_dzen_page(
                page, page.url, config, timestamp
            )

    except PlaywrightTimeoutError:
        result = DzenSessionProbeResult(
            is_valid=False,
            status="error",
            message="Таймаут подключения к dzen.ru (превышено время ожидания 25 сек).",
            timestamp=timestamp,
        )
    except PlaywrightError as exc:
        result = DzenSessionProbeResult(
            is_valid=False,
            status="error",
            message=f"Ошибка браузера при проверке сессии: {exc}",
            timestamp=timestamp,
        )

    if session is not None and config is not None:
        config.status = result.status
        config.last_checked_at = result.timestamp
        config.error_message = None if result.is_valid else result.message
        if result.channel_name:
            config.channel_name = result.channel_name
        try:
            await save_dzen_session_config(session, config)
        except SQLAlchemyError as exc:
            logger.warning("Failed to update Dzen probe result in DB: %s", exc)

    return result


class DzenBrowserPublisher:
    """Handles article publishing to Dzen Studio via Playwright Stealth."""

    __slots__ = ("_config",)

    def __init__(self, config: DzenSessionConfig) -> None:
        self._config = config

    @property
    def is_ready(self) -> bool:
        """Whether publication is ready to be attempted."""
        return self._config.is_active and self._config.has_session

    async def _fill_editor(  # noqa: PLR0915
        self, page: Page, title: str, html_content: str, *, cover_url: str = ""
    ) -> None:
        """Populate Draft.js title, body and cover image in Dzen Studio editor."""
        # Dismiss any initial onboarding modal / help popup
        with contextlib.suppress(PlaywrightError):
            await page.evaluate(_DISMISS_POPUPS_JS)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)

        editables = await page.query_selector_all("[contenteditable='true']")
        if len(editables) < 2:
            raise SocialResponseError(
                "Поля заголовка или текста не найдены в редакторе статьи Дзена",
                retryable=True,
            )

        title_el = editables[0]
        body_el = editables[1]

        # 1. Fill title
        await title_el.click()
        await page.evaluate("""(el) => {
            el.focus();
            document.execCommand('selectAll', false, null);
            document.execCommand('delete', false, null);
        }""", title_el)
        await page.keyboard.type(title)
        await page.wait_for_timeout(500)

        # 2. Fill body via native ClipboardEvent('paste') with text/html + text/plain
        # This triggers Draft.js native HTML parser, preserving H2/H3, bold/italic,
        # active clickable <a> links, and promo callouts.
        await body_el.click()
        await page.wait_for_timeout(300)
        await page.evaluate(
            """(html) => {
                const els = document.querySelectorAll('[contenteditable="true"]');
                const target = els.length > 1 ? els[1] : els[0];
                if (target) {
                    target.focus();
                    document.execCommand('selectAll', false, null);
                    document.execCommand('delete', false, null);
                    const dt = new DataTransfer();
                    dt.setData('text/html', html);
                    dt.setData('text/plain', html.replace(/<[^>]+>/g, ' '));
                    const pasteEvent = new ClipboardEvent('paste', {
                        bubbles: true,
                        cancelable: true,
                        clipboardData: dt
                    });
                    target.dispatchEvent(pasteEvent);
                }
            }""",
            html_content,
        )
        await page.wait_for_timeout(1500)

        # 3. Insert lead cover image at the very top of the body (above paragraph 1)
        if cover_url:
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(300)
            first_block = await body_el.query_selector("[data-block='true']")
            if first_block:
                with contextlib.suppress(PlaywrightError):
                    await first_block.scroll_into_view_if_needed()
                    await first_block.click(position={"x": 5, "y": 5})
                    await page.wait_for_timeout(200)
                    await page.keyboard.press("Home")
                    await page.keyboard.press("Enter")
                    await page.keyboard.press("ArrowUp")
                    await page.wait_for_timeout(300)

            # Download cover image to a temp file and upload via Dzen's native file input
            tmp_cover_path = ""
            try:
                timeout = httpx.Timeout(20.0)
                async with httpx.AsyncClient(
                    timeout=timeout, follow_redirects=True
                ) as client:
                    resp = await client.get(cover_url)
                    if resp.status_code == 200 and len(resp.content) > 500:
                        with tempfile.NamedTemporaryFile(
                            suffix=".jpg", delete=False
                        ) as tf:
                            tf.write(resp.content)
                            tmp_cover_path = tf.name

                if tmp_cover_path:
                    side_btn = await page.query_selector('[class*="side-button"]')
                    if side_btn:
                        await side_btn.click()
                        await page.wait_for_timeout(500)
                        file_sel = 'input[type="file"][accept*="image"]'
                        file_input = await page.query_selector(file_sel)
                        if file_input:
                            await file_input.set_input_files(tmp_cover_path)
                            logger.info("Uploaded cover image to Dzen editor")
                            await page.wait_for_timeout(4000)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to upload cover image in Dzen editor: %s", exc)
            finally:
                if tmp_cover_path:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_cover_path)

        # 4. Clean any popups or overlays that may have spawned during paste
        with contextlib.suppress(PlaywrightError):
            await page.evaluate(_DISMISS_POPUPS_JS)

        # 5. Wait for Dzen autosave to persist to cloud
        for _ in range(15):
            await page.wait_for_timeout(500)
            status_text = await page.evaluate("""() => {
                const header = document.querySelector('header, [class*="header"]');
                return header ? header.innerText : '';
            }""")
            if "Сохранено" in status_text:
                break

    async def _trigger_publish(self, page: Page) -> str:
        """Click Publish button in Dzen Studio and resolve VK SmartCaptcha."""
        with contextlib.suppress(PlaywrightError):
            await page.evaluate("""() => {
                document.querySelectorAll(
                    '.article-editor-desktop--help-popup__overlay-3q, ' +
                    '[class*="help-popup__overlay"], ' +
                    '[data-testid="modal-overlay"]'
                ).forEach(el => el.remove());
            }""")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)

        # 1. Click header publish button
        header_btn = await page.query_selector(
            "button[data-testid='article-publish-btn'], "
            "button.article-editor-desktop--editor-header__editBtn-44, "
            "button:has-text('Опубликовать'), "
            "[class*='editBtn']"
        )
        if not header_btn:
            logger.warning("Dzen header publish button not found: %s", page.url)
            return page.url

        with contextlib.suppress(PlaywrightError):
            await header_btn.click(timeout=5000, force=True)
            await page.wait_for_timeout(2000)

        # 2. Check if one-time channel agreement / terms dialog appears
        terms_input = await page.query_selector(
            "input.article-editor-desktop--checkbox-v2__input-1y, input[type='checkbox']"
        )
        if terms_input and not await terms_input.is_checked():
            with contextlib.suppress(PlaywrightError):
                await terms_input.check(force=True)
                await page.wait_for_timeout(500)
                cont_btn = await page.query_selector(
                    "button[type='submit']:has-text('Продолжить'), "
                    "button:has-text('Продолжить')"
                )
                if cont_btn:
                    await cont_btn.click(timeout=5000, force=True)
                    await page.wait_for_timeout(2500)

        # 3. Ensure 'Опубликовать позже' (Schedule publish) checkbox is NOT checked
        with contextlib.suppress(PlaywrightError):
            await page.evaluate("""() => {
                const labels = Array.from(
                    document.querySelectorAll('label, [class*="checkbox"]')
                );
                for (const l of labels) {
                    if (l.innerText && l.innerText.toLowerCase().includes('позже')) {
                        const svg = l.querySelector('svg');
                        const isChecked = svg && (
                            svg.innerHTML.includes('checked') ||
                            svg.getAttribute('class')?.includes('checked')
                        );
                        if (isChecked) {
                            l.click();
                        }
                    }
                }
            }""")
            await page.wait_for_timeout(500)

        # 4. Click final publish button in publication modal
        modal_btn = await page.query_selector(
            "button[data-testid='publish-btn'], "
            "[role='dialog'] button:has-text('Опубликовать'), "
            "[class*='modal'] button:has-text('Опубликовать'), "
            "[class*='popup'] button:has-text('Опубликовать'), "
            "button[type='submit']:has-text('Опубликовать'), "
            "button.article-editor-desktop--base-button__primary-1Y, "
            "button.editor-confirm-publish"
        )
        if modal_btn:
            with contextlib.suppress(PlaywrightError):
                await modal_btn.click(timeout=5000, force=True)
                await page.wait_for_timeout(2500)

        # 5. Check if VK ID SmartCaptcha iframe appears
        for _ in range(5):
            captcha_frame = None
            for frame in page.frames:
                if "id.vk.ru" in frame.url or "not_robot_captcha" in frame.url:
                    captcha_frame = frame
                    break
            if captcha_frame is not None:
                logger.info("VK SmartCaptcha frame: %s", captcha_frame.url[:80])
                with contextlib.suppress(PlaywrightError):
                    cb = await captcha_frame.query_selector(
                        "#not-robot-captcha-checkbox, input[type='checkbox']"
                    )
                    if cb:
                        await cb.click(force=True)
                        logger.info("Clicked VK SmartCaptcha checkbox")
                        await page.wait_for_timeout(3000)
                break
            await page.wait_for_timeout(1000)

        # 6. Wait for editor to exit and transition to live published URL
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_url(
                lambda u: "/edit" not in u and (
                    "/a/" in u or "/media/" in u or "publications" in u
                ),
                timeout=15000,
            )
        await page.wait_for_timeout(2500)
        return page.url

    async def publish_article(
        self,
        article: Article,
        wp_post_url: str,
        *,
        cover_url: str = "",
        custom_promo: str | None = None,
        publish_now: bool | None = None,
    ) -> PublishResult:
        """Publish article to Dzen Studio via browser automation."""
        if not self._config.has_session:
            raise SocialNotConfiguredError(
                "Dzen browser session (storage_state) is not configured"
            )

        if not wp_post_url:
            raise SocialResponseError(
                "Refusing to publish to Dzen without canonical URL",
                retryable=False,
            )

        base_html = build_dzen_html(article.content_html)
        if not base_html:
            raise SocialResponseError(
                "Article body reduced to empty after Dzen sanitization",
                retryable=False,
            )

        promo_snippet = (
            custom_promo
            if custom_promo is not None
            else self._config.promo_snippet
        )
        content_with_promo = inject_dzen_promo(
            base_html,
            self._config.promo_mode,
            promo_snippet,
            wp_post_url=wp_post_url or "https://svodka.press/",
        )

        if not cover_url:
            wp_status = (article.platform_statuses or {}).get("wp", {})
            if isinstance(wp_status, dict) and wp_status.get("cover_url"):
                cover_url = str(wp_status["cover_url"]).strip()

        target_site_url = "https://svodka.press/"
        target_article_link = wp_post_url.strip() or target_site_url
        target_tg_link = "https://t.me/svodka_press"
        canonical_attribution = (
            "<p>▫️ ▫️ ▫️</p>\n"
            "<p>⚡ <b>Читайте главные новости на "
            f'<a href="{target_site_url}">сайте SvodkaPress</a> или в нашем '
            f'<a href="{target_tg_link}">Telegram-канале</a></b></p>\n'
            f"<p><small>Оригинал и полная версия: "
            f'<a href="{target_article_link}">{target_article_link}</a></small></p>'
        )
        final_content = f"{content_with_promo}\n{canonical_attribution}"

        should_publish = (
            publish_now
            if publish_now is not None
            else (self._config.publication_mode == "publish")
        )

        pool = get_browser_pool()
        logger.info(
            "Starting Dzen browser publication for article %s (mode=%s)",
            article.id,
            "publish" if should_publish else "draft",
        )

        clean_state = normalize_dzen_storage_state(self._config.storage_state)
        channel_clean = self._config.channel_name.strip()
        if not channel_clean and self._config.channel_url:
            channel_clean = self._config.channel_url.rstrip("/").split("/")[-1]
        studio_url = (
            f"https://dzen.ru/profile/editor/{channel_clean}"
            if channel_clean
            else "https://dzen.ru/studio"
        )

        try:
            async with pool.page(
                storage_state=clean_state, block_resources=False
            ) as page:
                await self._open_editor(page, studio_url)
                await self._fill_editor(
                    page, article.title, final_content, cover_url=cover_url
                )

                post_url = page.url
                match = re.search(
                    r"/editor/(?:id/[^/]+/)?([a-zA-Z0-9_-]+)(?:/edit)?", post_url
                )
                pub_id = (
                    match.group(1)
                    if match
                    else f"dzen_{int(datetime.now().timestamp())}"
                )

                if should_publish:
                    post_url = await self._trigger_publish(page)
                    if pub_id and not pub_id.startswith("dzen_") and "/edit" in post_url:
                        post_url = f"https://dzen.ru/a/{pub_id}"


                logger.info(
                    "Dzen article placed via browser: %s (id=%s)",
                    post_url,
                    pub_id,
                )
                return PublishResult.published(
                    Platform.DZEN.value,
                    external_id=pub_id,
                    url=post_url,
                )
        except (SocialAuthError, SocialNotConfiguredError, SocialResponseError):
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            logger.error("Dzen browser publication error: %s", exc)
            raise SocialResponseError(
                f"Dzen browser automation failed: {exc}",
                retryable=True,
            ) from exc

    async def _open_editor(self, page: Page, studio_url: str) -> None:
        """Navigate to Dzen studio, handle modals, and open article editor."""
        await page.goto(studio_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        login_btn = await page.query_selector(
            "button:has-text('Войти'), a:has-text('Войти'), [data-testid='login-button']"
        )
        if "passport.yandex." in page.url or login_btn is not None:
            raise SocialAuthError(
                "Dzen browser session expired (not authenticated, login required)"
            )

        # Dismiss modal overlay or onboarding banner if present
        with contextlib.suppress(PlaywrightError):
            await page.evaluate(_DISMISS_POPUPS_JS)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)

        close_btn = await page.query_selector(
            "[data-testid='close-button'], button[aria-label*='Закрыть']"
        )
        if close_btn:
            with contextlib.suppress(PlaywrightError):
                await close_btn.click(timeout=3000)
                await page.wait_for_timeout(1000)

        # Click [+] Add publication
        add_btn = await page.query_selector("[data-testid='add-publication-button']")
        if not add_btn:
            raise SocialResponseError(
                "Кнопка добавления публикации [+] не найдена в Студии Дзена",
                retryable=True,
            )
        await add_btn.click()
        await page.wait_for_timeout(1500)

        # Click «Написать статью» in dropdown
        article_item = await page.query_selector(
            "label[aria-label*='Написать статью'], "
            "span:has-text('Написать статью'), "
            "[class*='new-publication-dropdown'] *:has-text('Написать статью')"
        )
        if not article_item:
            raise SocialResponseError(
                "Пункт меню «Написать статью» не найден в выпадающем списке Дзена",
                retryable=True,
            )
        await article_item.click()

        # Wait for navigation to /edit
        try:
            await page.wait_for_url(lambda u: "/edit" in u, timeout=15000)
        except PlaywrightTimeoutError as exc:
            raise SocialResponseError(
                f"Таймаут открытия редактора статьи Дзена ({page.url})",
                retryable=True,
            ) from exc

        await page.wait_for_timeout(2000)

        # Dismiss any tutorial / help popup in editor
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
        close_cross = await page.query_selector(
            "[class*='closeCross'], [aria-label='Закрыть']"
        )
        if close_cross:
            with contextlib.suppress(PlaywrightError):
                await close_cross.click(timeout=2000)
                await page.wait_for_timeout(500)


