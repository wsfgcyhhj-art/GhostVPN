from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import AsyncGenerator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.core.logging import logger
from app.db.base import Base
from app.db.models import Plan, Server


def build_engine(database_url: str | None = None) -> AsyncEngine:
    url = database_url or settings.DATABASE_URL
    is_sqlite = url.startswith("sqlite")

    if is_sqlite:
        return create_async_engine(url, echo=False)

    # Совместимость с Neon / Supabase (PgBouncer transaction mode) и лимитом RAM Render Free
    return create_async_engine(
        url,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=5,
        connect_args={"statement_cache_size": 0},
    )


engine: AsyncEngine = build_engine()
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


DEFAULT_PLANS = [
    {"name": "1 месяц", "days": 30, "price_rub": Decimal("199.00"), "sort_order": 1},
    {"name": "3 месяца", "days": 90, "price_rub": Decimal("499.00"), "sort_order": 2},
    {"name": "6 месяцев", "days": 180, "price_rub": Decimal("899.00"), "sort_order": 3},
    {"name": "12 месяцев", "days": 365, "price_rub": Decimal("1590.00"), "sort_order": 4},
]


async def seed_initial_data(session: AsyncSession) -> None:
    """Создаёт стартовый сервер 3X-UI из .env и тарифную сетку, если таблицы пусты."""
    server_res = await session.execute(select(Server).limit(1))
    server = server_res.scalar_one_or_none()
    if server is None:
        default_server = Server(
            name=settings.XUI_SERVER_NAME,
            host=settings.XUI_HOST,
            web_base_path=settings.XUI_WEB_BASE_PATH,
            username=settings.XUI_USERNAME,
            password=settings.XUI_PASSWORD,
            inbound_id=settings.XUI_INBOUND_ID,
            sub_base_url=settings.XUI_SUB_BASE_URL,
            verify_ssl=settings.XUI_VERIFY_SSL,
            is_active=True,
        )
        session.add(default_server)
        logger.info("Создан стартовый сервер 3X-UI: %s", settings.XUI_SERVER_NAME)
    else:
        # Синхронизируем актуальные реквизиты из .env со стартовым сервером
        server.host = settings.XUI_HOST
        server.web_base_path = settings.XUI_WEB_BASE_PATH
        server.username = settings.XUI_USERNAME
        server.password = settings.XUI_PASSWORD
        server.inbound_id = settings.XUI_INBOUND_ID
        server.sub_base_url = settings.XUI_SUB_BASE_URL
        server.verify_ssl = settings.XUI_VERIFY_SSL

    plans_res = await session.execute(select(Plan).limit(1))
    if plans_res.scalar_one_or_none() is None:
        for p in DEFAULT_PLANS:
            session.add(
                Plan(
                    name=p["name"],
                    days=p["days"],
                    price_rub=p["price_rub"],
                    limit_ip=settings.DEFAULT_LIMIT_IP,
                    is_active=True,
                    sort_order=p["sort_order"],
                )
            )
        logger.info("Создана начальная тарифная сетка (1/3/6/12 мес.)")

    await session.commit()


async def init_db_with_retry(max_retries: int = 5, base_delay: float = 1.5) -> None:
    """Инициализирует схему БД и начальные данные с retry на случай холодного старта Neon/Supabase."""
    for attempt in range(1, max_retries + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with async_session_factory() as session:
                await seed_initial_data(session)
            logger.info("База данных успешно инициализирована (попытка %d)", attempt)
            return
        except Exception as exc:
            logger.warning(
                "Ошибка подключения к БД при старте (попытка %d/%d): %s",
                attempt,
                max_retries,
                exc,
            )
            if attempt == max_retries:
                raise
            await asyncio.sleep(base_delay * attempt)
