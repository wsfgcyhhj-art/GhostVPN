from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.core.config import settings
from app.db.repositories import Repository
from app.db.session import async_session_factory


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
                )
                if user.is_banned and tg_user.id not in settings.ADMIN_IDS:
                    if isinstance(event, Message):
                        await event.answer("⛔ Ваш аккаунт заблокирован администратором.")
                    elif isinstance(event, CallbackQuery):
                        await event.answer("⛔ Ваш аккаунт заблокирован.", show_alert=True)
                    return None
                data["db_user"] = user

            return await handler(event, data)
