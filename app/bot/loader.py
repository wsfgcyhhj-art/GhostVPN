from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import admin, billing, profile, start, trial
from app.bot.middlewares import DatabaseAndUserMiddleware, RateLimitMiddleware
from app.core.config import settings


def create_bot() -> Bot:
    return Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.callback_query.outer_middleware(RateLimitMiddleware())
    dp.update.outer_middleware(DatabaseAndUserMiddleware())

    dp.include_router(start.router)
    dp.include_router(trial.router)
    dp.include_router(profile.router)
    dp.include_router(billing.router)
    dp.include_router(admin.router)
    return dp


bot = create_bot()
dp = create_dispatcher()
