#!/usr/bin/env python3
# ruff: noqa: RUF001, ASYNC250
"""CLI utility to authenticate TenChat via Playwright and Sber ID SMS.

Usage:
    docker exec -it svodkapress_app python3 scripts/auth_tenchat.py [+79918829870]
"""

from __future__ import annotations

import asyncio
import sys

from app.database import AsyncSessionLocal
from app.services.tenchat_auth import tenchat_auth_manager


async def main() -> None:
    phone = sys.argv[1] if len(sys.argv) > 1 else "+79918829870"
    print(f"Запуск авторизации TenChat для номера {phone}...")

    res = await tenchat_auth_manager.start_sms_flow(phone=phone)
    status = res.get("status")

    if status == "success":
        print(f"Готово: {res.get('message')}")
        return

    if status != "sms_required":
        print(f"Ошибка: {res.get('message')}")
        return

    print(f"\n{res.get('message')}")
    try:
        code = input("Введите 5-значный СМС-код: ").strip()
    except (EOFError, KeyboardInterrupt):
        await tenchat_auth_manager.cancel()
        print("\nОтменено.")
        return

    async with AsyncSessionLocal() as session:
        v_res = await tenchat_auth_manager.verify_code(code, session=session)

    if v_res.get("status") == "success":
        print(f"\nУспех: {v_res.get('message')}")
    else:
        print(f"\nОшибка: {v_res.get('message')}")


if __name__ == "__main__":
    asyncio.run(main())
