# ruff: noqa: RUF001
"""Interactive SMS authorization helper for TenChat via Playwright.

Maintains an active Playwright session in memory for up to 180 seconds while
the user receives and enters the 5-digit SMS verification code.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import Platform
from app.services.social_browser_publisher import (
    load_social_browser_config,
    normalize_social_storage_state,
    probe_social_browser_session,
    save_social_browser_config,
)
from app.services.web_scraper import (
    _USER_AGENT,
    _VIEWPORT,
    _apply_stealth,
    get_browser_pool,
)

logger: Final[logging.Logger] = logging.getLogger(__name__)

SESSION_TIMEOUT_SECONDS: Final[float] = 180.0


class TenChatAuthManager:
    """Manages interactive Playwright login for TenChat via Sber ID SMS."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._context: Any | None = None
        self._page: Any | None = None
        self._created_at: float = 0.0
        self._phone: str | None = None
        self._pending: bool = False
        self._status_message: str | None = None

    @property
    def is_pending(self) -> bool:
        if not self._pending:
            return False
        if time.time() - self._created_at > SESSION_TIMEOUT_SECONDS:
            asyncio.create_task(self.cancel())  # noqa: RUF006
            return False
        return True

    @property
    def phone(self) -> str | None:
        return self._phone

    @property
    def status_message(self) -> str | None:
        return self._status_message

    async def start_sms_flow(  # noqa: PLR0915
        self, phone: str = "+79918829870"
    ) -> dict[str, Any]:
        """Navigate to tenchat.ru, enter phone number, wait for SMS prompt."""
        async with self._lock:
            await self._cleanup_locked()

            clean_digits = "".join(filter(str.isdigit, phone))
            if len(clean_digits) == 11 and clean_digits.startswith(("7", "8")):
                target_phone = clean_digits[1:]
            else:
                target_phone = clean_digits

            self._phone = f"+7{target_phone}"
            logger.info("TenChat login starting for phone +7%s", target_phone)

            pool = get_browser_pool()
            browser = await pool._ensure_browser()

            context = await browser.new_context(
                user_agent=_USER_AGENT,
                locale="ru-RU",
                viewport=_VIEWPORT,
                ignore_https_errors=True,
            )
            page = await context.new_page()
            await _apply_stealth(page)

            self._context = context
            self._page = page
            self._created_at = time.time()

            try:
                await page.goto(
                    "https://tenchat.ru/",
                    wait_until="domcontentloaded",
                    timeout=25000,
                )
                await asyncio.sleep(2.5)

                # Click Login / Войти
                btn_sel = "button:has-text('Войти'), a:has-text('Войти')"
                login_btn = page.locator(btn_sel).first
                if await login_btn.count() > 0:
                    await login_btn.click()
                    await asyncio.sleep(2.5)

                # Enter phone number
                phone_sel = "input[type='tel'], input[placeholder*='телефон']"
                phone_inp = page.locator(phone_sel).first
                if await phone_inp.count() == 0:
                    await self._cleanup_locked()
                    return {
                        "status": "error",
                        "message": "Поле ввода телефона не найдено на tenchat.ru",
                    }

                await phone_inp.click()
                await phone_inp.fill(target_phone)
                await asyncio.sleep(0.8)

                # Click Continue / Продолжить
                cont_sel = "button:has-text('Продолжить'), button[type='submit']"
                cont_btn = page.locator(cont_sel).first
                if await cont_btn.count() > 0:
                    await cont_btn.click()
                    await asyncio.sleep(4.0)

                # Check where we landed
                cur_url = page.url.lower()
                logger.info("TenChat login post-submit URL: %s", cur_url)

                # Check if SMS inputs are present (Sber ID: sms0..sms4)
                sms0 = page.locator("input[name='sms0']").first
                if await sms0.count() > 0:
                    self._pending = True
                    msg = (
                        f"СМС отправлено на номер +7{target_phone}. "
                        "Введите 5 цифр ниже:"
                    )
                    self._status_message = msg
                    return {
                        "status": "sms_required",
                        "message": msg,
                        "phone": f"+7{target_phone}",
                    }

                # Check if already logged in (redirected to feed/profile)
                cookies = await context.cookies()
                auth_cookies = [
                    c
                    for c in cookies
                    if c.get("name") in ("auth_token", "access_token", "tc_auth")
                ]
                if auth_cookies:
                    self._pending = False
                    await self._cleanup_locked()
                    return {
                        "status": "success",
                        "message": "Успешная авторизация без ввода СМС!",
                        "cookies": cookies,
                    }

                # Fallback: check if still on Sber ID
                if "id.sber.ru" in cur_url:
                    self._pending = True
                    msg = (
                        f"Страница Сбер ID ожидает ввода кода для +7{target_phone}."
                    )
                    self._status_message = msg
                    return {
                        "status": "sms_required",
                        "message": msg,
                        "phone": f"+7{target_phone}",
                    }

                await self._cleanup_locked()
                return {
                    "status": "error",
                    "message": f"Неизвестный экран (URL: {cur_url[:60]})",
                }

            except Exception as exc:
                logger.error("TenChat login failed: %s", exc, exc_info=True)
                await self._cleanup_locked()
                return {
                    "status": "error",
                    "message": f"Ошибка запуска браузера: {exc}",
                }

    async def verify_code(
        self,
        code: str,
        session: AsyncSession,
    ) -> dict[str, Any]:
        """Fill 5-digit SMS code into Sber ID inputs, capture cookies, save to DB."""
        async with self._lock:
            if not self._page or not self._context:
                return {
                    "status": "error",
                    "message": (
                        "Сессия ожидания СМС истекла. Нажмите «Отправить СМС» снова."
                    ),
                }

            digits = "".join(filter(str.isdigit, code))
            if len(digits) != 5:
                return {
                    "status": "error",
                    "message": (
                        f"Код должен содержать ровно 5 цифр (получено: {len(digits)})."
                    ),
                }

            page = self._page
            context = self._context

            try:
                logger.info("Entering 5-digit SMS code into Sber ID inputs...")
                for i in range(5):
                    inp = page.locator(f"input[name='sms{i}']").first
                    if await inp.count() > 0:
                        await inp.fill(digits[i])
                        await asyncio.sleep(0.15)

                await asyncio.sleep(3.0)

                submit_sel = (
                    "button:has-text('Войти'), "
                    "button:has-text('Подтвердить'), "
                    "button[type='submit']"
                )
                submit_btn = page.locator(submit_sel).first
                if await submit_btn.count() > 0 and await submit_btn.is_visible():
                    with contextlib.suppress(Exception):
                        await submit_btn.click()
                        await asyncio.sleep(3.0)

                for _ in range(8):
                    cur_url = page.url.lower()
                    if "tenchat.ru" in cur_url and "id.sber.ru" not in cur_url:
                        break
                    await asyncio.sleep(1.0)

                await asyncio.sleep(2.0)
                final_url = page.url
                logger.info("URL after SMS verification: %s", final_url)

                storage_state = await context.storage_state()
                if not storage_state or not storage_state.get("cookies"):
                    return {
                        "status": "error",
                        "message": "После ввода кода куки не получены (неверный код?).",
                    }

                norm = normalize_social_storage_state(
                    storage_state, Platform.TENCHAT.value
                )
                cfg = await load_social_browser_config(session, Platform.TENCHAT.value)
                cfg.storage_state = norm
                cfg.status = "active"
                cfg.is_active = True
                cfg.error_message = None
                cfg.last_checked_at = time.strftime(
                    "%Y-%m-%d %H:%M:%S UTC", time.gmtime()
                )
                await save_social_browser_config(session, cfg)

                # Live probe to confirm /editor access and set account name
                probe_res = await probe_social_browser_session(cfg, session=session)
                cookies_count = len(norm.get("cookies", []))
                await self._cleanup_locked()

                is_ok = bool(probe_res.get("is_valid", True))
                return {
                    "status": "success" if is_ok else "warning",
                    "message": (
                        probe_res.get("message")
                        or f"✅ Сессия TenChat сохранена! ({cookies_count} кук)."
                    ),
                    "cookies_count": cookies_count,
                    "is_valid": is_ok,
                }

            except Exception as exc:
                logger.error("SMS verification failed: %s", exc, exc_info=True)
                return {
                    "status": "error",
                    "message": f"Ошибка при вводе кода: {exc}",
                }

    async def cancel(self) -> None:
        """Cancel active login attempt and free browser resources."""
        async with self._lock:
            await self._cleanup_locked()

    async def _cleanup_locked(self) -> None:
        self._pending = False
        self._status_message = None
        self._phone = None
        self._created_at = 0.0

        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
            self._context = None
            self._page = None


tenchat_auth_manager = TenChatAuthManager()
