from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.core.config import settings
from app.core.i18n import t
from app.db.repositories import Repository
from app.db.session import async_session_factory


class RateLimitMiddleware(BaseMiddleware):
    """
    Антифрод защита от спама нажатиями кнопок (не чаще 1 клика в RATE_LIMIT_MIN_INTERVAL_SEC = 0.35–0.4с).
    """

    def __init__(self, min_interval_sec: float | None = None) -> None:
        self.min_interval = (
            min_interval_sec
            if min_interval_sec is not None
            else settings.RATE_LIMIT_MIN_INTERVAL_SEC
        )
        self._last_click: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery) and event.from_user:
            uid = event.from_user.id
            now = time.monotonic()
            prev = self._last_click.get(uid, 0.0)
            if now - prev < self.min_interval:
                await event.answer(t("rate_limit_msg", "ru"), show_alert=False)
                return None
            self._last_click[uid] = now
            if len(self._last_click) > 5000:
                self._last_click.clear()

        return await handler(event, data)


class DatabaseAndUserMiddleware(BaseMiddleware):
    """
    Инжектирует `session`, `repo` и `db_user` в каждый апдейт.
    Блокирует забаненных пользователей (кроме администраторов из ADMIN_IDS).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        async with async_session_factory() as session:
            repo = Repository(session)
            data["session"] = session
            data["repo"] = repo

            if tg_user is not None:
                user, _ = await repo.get_or_create_user(
                    telegram_id=tg_user.id,
                    username=tg_user.username,
                    full_name=tg_user.full_name,
                    language_code=getattr(tg_user, "language_code", "ru"),
                )
                if user.is_banned and tg_user.id not in settings.ADMIN_IDS:
                    if isinstance(event, Message):
                        await event.answer("⛔ Доступ к боту ограничен администратором.")
                    elif isinstance(event, CallbackQuery):
                        await event.answer("⛔ Доступ ограничен.", show_alert=True)
                    return None
                data["db_user"] = user

            return await handler(event, data)
