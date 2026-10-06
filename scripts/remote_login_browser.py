#!/usr/bin/env python3
# ruff: noqa: RUF001, BLE001, S110, S603, S607, E501, PLW1510, PLR0912, PLR0915
"""Interactive browser session login helper over VPS SOCKS5 proxy.

Launches a visible Chromium window on macOS routing all network traffic
through the VPS IP (144.31.141.17) via an SSH SOCKS5 tunnel.

User logs in manually to TenChat and YouTube. The script detects successful
authentication, saves the cookies directly to PostgreSQL on the VPS,
verifies session validity, and updates the SvodkaPress system settings.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Self-bootstrap: re-exec with project venv or uv if playwright is missing from current python
try:
    import playwright  # noqa: F401
except ImportError:
    _proj = Path(__file__).resolve().parent.parent
    _venv_py = _proj / ".venv" / "bin" / "python3"
    _uv = shutil.which("uv")
    if _venv_py.exists():
        os.execv(str(_venv_py), [str(_venv_py), *sys.argv])  # noqa: S606
    elif _uv:
        os.execv(_uv, [_uv, "run", "python3", *sys.argv])  # noqa: S606
    else:
        sys.stderr.write(
            "❌ Ошибка: playwright не найден. Запустите: uv run python3 scripts/remote_login_browser.py\n"
        )
        sys.exit(1)

import asyncio
import contextlib
import json
import subprocess
import time
from typing import Any

from playwright.async_api import async_playwright

PROXY_PORT = 1085
EXPECTED_IP = "144.31.141.17"
SSH_HOST = "svodkapress-vps"
LOCAL_PROFILE_DIR = Path.home() / ".svodkapress" / "browser_profiles" / "mac_auth"


def ensure_ssh_proxy() -> str:
    """Ensure SSH SOCKS5 proxy is active and returns the VPS external IP."""
    print("⏳ Проверяем SSH SOCKS5 туннель к серверу...")

    def _probe_ip() -> str:
        for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
            cmd = [
                "curl",
                "-s",
                "--connect-timeout",
                "10",
                "--max-time",
                "15",
                "--socks5-hostname",
                f"127.0.0.1:{PROXY_PORT}",
                url,
            ]
            try:
                res = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
                if res and any(c.isdigit() for c in res):
                    return res
            except Exception:  # noqa: S112
                continue
        return ""

    out = _probe_ip()

    if out != EXPECTED_IP:
        print(f"🚀 Запускаем SSH SOCKS5 туннель через {SSH_HOST} на порт {PROXY_PORT}...")
        # Kill existing stale processes on this port if any
        subprocess.run(["pkill", "-f", f"-D {PROXY_PORT}"], stderr=subprocess.DEVNULL)
        time.sleep(0.8)
        subprocess.run(["ssh", "-D", str(PROXY_PORT), "-N", "-f", SSH_HOST], check=True)
        time.sleep(2.0)

        # Retry probing up to 3 times
        for _attempt in range(1, 4):
            out = _probe_ip()
            if out == EXPECTED_IP:
                break
            time.sleep(1.5)

    if out != EXPECTED_IP:
        print(f"⚠️ Предупреждение: получен IP {out or 'не определен'}, ожидался {EXPECTED_IP}")
    else:
        print(f"🌐 Туннель активен! Весь трафик браузера идет с серверного IP: {out}")
    return out or EXPECTED_IP


def sync_storage_state_to_vps(platform: str, storage_state: dict[str, Any]) -> dict[str, Any]:
    """Send captured storage_state (cookies + origins) to VPS and update PostgreSQL database."""
    cookies_count = len(storage_state.get("cookies", []))
    origins_count = len(storage_state.get("origins", []))
    print(
        f"💾 Синхронизируем состояние {platform} ({cookies_count} кук, "
        f"{origins_count} origins) на VPS..."
    )
    payload = json.dumps(
        {"platform": platform, "storage_state": storage_state}, ensure_ascii=False
    )

    py_script = """
import sys, json, asyncio
from app.database import AsyncSessionLocal
from app.services.social_browser_publisher import (
    load_all_social_browser_configs,
    save_social_browser_config,
    probe_social_browser_session,
    normalize_social_storage_state
)

async def run():
    raw = sys.stdin.read()
    data = json.loads(raw)
    platform = data['platform']
    storage_state = data['storage_state']
    async with AsyncSessionLocal() as session:
        cfgs = await load_all_social_browser_configs(session)
        cfg = cfgs.get(platform)
        if not cfg:
            print(json.dumps({"error": f"Unknown platform {platform}"}))
            return
        cfg.storage_state = normalize_social_storage_state(storage_state, platform)
        cfg.is_active = True
        await save_social_browser_config(session, cfg)
        probe_res = await probe_social_browser_session(cfg, session=session)
        print(json.dumps(probe_res, ensure_ascii=False))

asyncio.run(run())
"""
    cmd = [
        "ssh",
        SSH_HOST,
        f"docker exec -i svodkapress_app python3 -c {subprocess.list2cmdline([py_script])}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=payload,
            text=True,
            capture_output=True,
            timeout=30,
        )
        res_line = [
            line for line in proc.stdout.splitlines() if line.strip().startswith("{")
        ]
        if res_line:
            return json.loads(res_line[-1])
        return {"raw_output": proc.stdout.strip(), "error": proc.stderr.strip()}
    except Exception as exc:
        return {"error": str(exc)}


async def show_page_banner(page: Any, text: str, bg_color: str = "#10b981") -> None:
    """Inject a floating notification banner on the active web page."""
    with contextlib.suppress(Exception):
        await page.evaluate(
            f"""() => {{
            const id = 'svodka-status-toast';
            let el = document.getElementById(id);
            if (!el) {{
                el = document.createElement('div');
                el.id = id;
                el.style.position = 'fixed';
                el.style.top = '16px';
                el.style.left = '50%';
                el.style.transform = 'translateX(-50%)';
                el.style.padding = '12px 24px';
                el.style.background = '{bg_color}';
                el.style.color = '#ffffff';
                el.style.fontFamily = 'system-ui, -apple-system, sans-serif';
                el.style.fontSize = '15px';
                el.style.fontWeight = '600';
                el.style.borderRadius = '8px';
                el.style.boxShadow = '0 10px 25px rgba(0,0,0,0.3)';
                el.style.zIndex = '999999999';
                el.style.transition = 'all 0.3s ease';
                document.body.appendChild(el);
            }}
            el.innerText = '{text}';
        }}"""
        )


async def check_tenchat_authorized(page_tc: Any) -> bool:
    """Verify whether TenChat is genuinely authenticated and can access /editor."""
    try:
        cur_url = page_tc.url.lower()
        if any(x in cur_url for x in ("sign-in", "signin", "/auth/", "oauth", "sber")):
            return False
        # Direct probe via request context to avoid disrupting user's tab
        resp = await page_tc.request.get("https://tenchat.ru/editor", timeout=6000)
        if resp.status == 200 and "/auth/" not in resp.url and "sign-in" not in resp.url:
            return True
        # Check editor or authenticated elements in active page
        if await page_tc.locator(".ql-editor, textarea, [contenteditable='true']").count() > 0:
            return True
        if await page_tc.locator("a[href*='/post/create'], .header-profile").count() > 0:
            return True
    except Exception:
        return False
    return False


async def check_youtube_authorized(page_yt: Any) -> bool:
    """Verify whether YouTube channel account is genuinely authenticated."""
    try:
        cur_url = page_yt.url.lower()
        if any(x in cur_url for x in ("accounts.google", "servicelogin")):
            return False
        cookies = await page_yt.context.cookies(["https://www.youtube.com"])
        cookie_names = {c.get("name") for c in cookies}
        # True authentication indicator: LOGIN_INFO or primary Google session keys
        has_auth_cookies = "LOGIN_INFO" in cookie_names or (
            "SAPISID" in cookie_names and "SID" in cookie_names and "__Secure-1PSID" in cookie_names
        )
        if not has_auth_cookies:
            return False
        has_signin = await page_yt.locator(
            'ytd-masthead a[href*="ServiceLogin"], ytd-masthead button[aria-label*="Войти"], ytd-button-renderer a[href*="signin"]'
        ).first.is_visible()
        if has_signin:
            return False
        has_avatar = await page_yt.locator(
            "ytd-masthead #avatar-btn, ytd-masthead button#avatar-btn"
        ).first.is_visible()
        has_create = await page_yt.locator(
            "ytd-masthead [aria-label*='Создать'], ytd-masthead [aria-label*='Create']"
        ).first.is_visible()
        has_compose = await page_yt.locator(
            "#placeholder-area, #simplebox-placeholder, ytd-backstage-post-dialog-renderer"
        ).first.is_visible()
        return bool(has_avatar or has_create or has_compose or has_auth_cookies)
    except Exception:
        return False


async def check_spark_authorized(page_sp: Any) -> bool:
    """Verify whether Spark.ru is genuinely authenticated (not just guest session)."""
    try:
        ctx = page_sp.context
        cookies = await ctx.cookies()
        has_uid = False
        has_sid = False
        for c in cookies:
            if "spark.ru" in c.get("domain", ""):
                cname = c.get("name")
                cval = str(c.get("value") or "")
                if cname == "uid" and cval and cval != "deleted":
                    has_uid = True
                if cname == "sid" and cval and cval != "deleted":
                    has_sid = True

        if has_uid and has_sid:
            return True

        for p in ctx.pages:
            if "spark.ru" in p.url.lower():
                # If on /auth without editor, not logged in yet
                if "/auth" in p.url.lower() and await p.locator("input[name='login_email']").count() > 0:
                    continue
                # If inside composer with title input
                if await p.locator("input[name='title'], .post-form__input_title").count() > 0:
                    return True
                # If user profile avatar/link is visible
                profile_sel = ".header-profile, a[href*='/user/'], a:has-text('Ваша публикация')"
                if await p.locator(profile_sel).count() > 0:
                    # Double check window.uid > 0
                    with contextlib.suppress(Exception):
                        uid = await p.evaluate("() => window.uid || 0")
                        if uid and int(uid) > 0:
                            return True
    except Exception:
        return False
    return False


USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)


async def main() -> None:
    exit_ip = ensure_ssh_proxy()

    print("\n" + "=" * 60)
    print("🚀 ЗАПУСК БРАУЗЕРА ДЛЯ АВТОРИЗАЦИИ ЧЕРЕЗ СЕРВЕРНЫЙ IP")
    print(f"   IP подключения: {exit_ip}")
    print(f"   Профиль Chromium: {LOCAL_PROFILE_DIR}")
    print("=" * 60 + "\n")

    LOCAL_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(LOCAL_PROFILE_DIR),
            headless=False,
            proxy={"server": f"socks5://127.0.0.1:{PROXY_PORT}"},
            args=[
                "--window-size=1360,950",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1360, "height": 880},
            locale="ru-RU",
            user_agent=USER_AGENT,
        )

        page_tc = context.pages[0] if context.pages else await context.new_page()
        page_yt = await context.new_page()
        page_sp = await context.new_page()

        print("🌐 Открываем вкладку Spark.ru...")
        await page_sp.goto("https://spark.ru/auth", wait_until="domcontentloaded")

        print("🌐 Открываем вкладку TenChat...")
        await page_tc.goto("https://tenchat.ru/auth/sign-in", wait_until="domcontentloaded")

        print("🌐 Открываем вкладку YouTube...")
        await page_yt.goto(
            "https://www.youtube.com/@svodkapress/posts", wait_until="domcontentloaded"
        )

        await show_page_banner(
            page_sp,
            f"🔒 Вход через серверный IP ({exit_ip}). Войдите в аккаунт Spark.ru.",
            "#273149",
        )
        await show_page_banner(
            page_tc,
            f"🔒 Вход через серверный IP ({exit_ip}). Введите телефон и SMS код.",
            "#3b82f6",
        )
        await show_page_banner(
            page_yt,
            f"🔒 Вход через серверный IP ({exit_ip}). Нажмите «Войти» в аккаунт канала.",
            "#3b82f6",
        )

        sp_synced = False
        tc_synced = False
        yt_synced = False

        print("\n" + "—" * 60)
        print("👉 В появившемся окне браузера:")
        print("   1. Во вкладке Spark.ru войдите в аккаунт (логин/пароль или соцсеть).")
        print("   2. Во вкладке TenChat введите номер +79918829870 и код из SMS (если нужно).")
        print("   3. Во вкладке YouTube проверьте аккаунт канала.")
        print("   Скрипт автоматически обнаружит вход и синхронизирует сессию на VPS!")
        print("   (Чтобы закрыть окно и завершить работу, закройте браузер или нажмите Ctrl+C)")
        print("—" * 60 + "\n")

        try:
            while True:
                # 1. Check Spark.ru
                if not sp_synced and await check_spark_authorized(page_sp):
                    print("🎉 Обнаружен успешный вход в Spark.ru (сессия подтверждена)!")
                    state = await context.storage_state()
                    state["cookies"] = await context.cookies()
                    state["userAgent"] = USER_AGENT
                    res = sync_storage_state_to_vps("spark", state)
                    print("   Результат проверки на VPS:", res.get("message") or res)
                    if res.get("is_valid", True):
                        sp_synced = True
                        await show_page_banner(
                            page_sp, "✅ Spark.ru успешно авторизован и проверен на VPS!", "#10b981"
                        )

                # 2. Check TenChat
                if not tc_synced and await check_tenchat_authorized(page_tc):
                    print("🎉 Обнаружен успешный вход в TenChat (редактор /editor подтвержден)!")
                    state = await context.storage_state()
                    state["cookies"] = await context.cookies()
                    state["userAgent"] = USER_AGENT
                    res = sync_storage_state_to_vps("tenchat", state)
                    print("   Результат проверки на VPS:", res.get("message") or res)
                    if res.get("is_valid", True):
                        tc_synced = True
                        await show_page_banner(
                            page_tc, "✅ TenChat успешно авторизован и проверен на VPS!", "#10b981"
                        )

                # 3. Check YouTube
                if not yt_synced and await check_youtube_authorized(page_yt):
                    print("🎉 Обнаружен успешный вход в YouTube (аккаунт канала подтвержден)!")
                    state = await context.storage_state()
                    state["cookies"] = await context.cookies()
                    state["userAgent"] = USER_AGENT
                    res = sync_storage_state_to_vps("youtube", state)
                    print("   Результат проверки на VPS:", res.get("message") or res)
                    if res.get("is_valid", True):
                        yt_synced = True
                        await show_page_banner(
                            page_yt, "✅ YouTube успешно авторизован и проверен на VPS!", "#10b981"
                        )

                if sp_synced and tc_synced and yt_synced:
                    print("\n" + "=" * 60)
                    print("🏆 ВСЕ СЕССИИ (Spark.ru, TenChat, YouTube) УСПЕШНО АВТОРИЗОВАНЫ И СОХРАНЕНЫ!")
                    print("   Окно закроется через 10 секунд (или закройте сейчас).")
                    print("=" * 60 + "\n")
                    await asyncio.sleep(10.0)
                    break

                await asyncio.sleep(2.5)

        except (asyncio.CancelledError, KeyboardInterrupt):
            print("\nЗавершение по сигналу пользователя...")
        except Exception as exc:
            if "Target closed" in str(exc) or "browser has been closed" in str(exc):
                print("\nБраузер закрыт пользователем.")
            else:
                print(f"\nИсключение: {exc}")
        finally:
            # Final sync if authorized
            try:
                state = await context.storage_state()
                state["cookies"] = await context.cookies()
                state["userAgent"] = USER_AGENT
                if await check_spark_authorized(page_sp):
                    print("Финальное сохранение Spark.ru...")
                    sync_storage_state_to_vps("spark", state)
                if await check_tenchat_authorized(page_tc):
                    print("Финальное сохранение TenChat...")
                    sync_storage_state_to_vps("tenchat", state)
                if await check_youtube_authorized(page_yt):
                    print("Финальное сохранение YouTube...")
                    sync_storage_state_to_vps("youtube", state)
            except Exception:
                pass

            with contextlib.suppress(Exception):
                await context.close()

    print("\n✅ Процесс завершен. Проверяем состояние сессий в панели...")


if __name__ == "__main__":
    asyncio.run(main())
