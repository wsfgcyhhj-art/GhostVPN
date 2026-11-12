from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from app.core.config import settings

if TYPE_CHECKING:
    from aiogram import Bot


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


logger = logging.getLogger("ghostvpn")


async def send_admin_alert(bot: "Bot | None", text: str, critical: bool = False) -> None:
    """Отправляет уведомление в ADMIN_CHAT_ID (или всем ADMIN_IDS, если чат не задан)."""
    prefix = "🚨 <b>КРИТИЧЕСКАЯ ОШИБКА</b>\n" if critical else "🔔 <b>Уведомление GhostVPN</b>\n"
    full_text = f"{prefix}{text}"

    if critical:
        logger.error("Admin Alert: %s", text)
    else:
        logger.info("Admin Notification: %s", text)

    if bot is None:
        return

    targets: list[int] = []
    if settings.ADMIN_CHAT_ID:
        targets.append(settings.ADMIN_CHAT_ID)
    elif settings.ADMIN_IDS:
        targets.extend(settings.ADMIN_IDS)

    for chat_id in targets:
        try:
            await bot.send_message(chat_id=chat_id, text=full_text, parse_mode="HTML")
        except Exception as exc:
            logger.warning("Не удалось отправить алерт админу %s: %s", chat_id, exc)
