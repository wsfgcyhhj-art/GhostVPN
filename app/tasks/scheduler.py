from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import AsyncGenerator

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.profile import format_subscription_card
from app.bot.keyboards.user import subscription_card_kb
from app.bot.utils import format_dt_msk
from app.core.logging import logger
from app.db.models import (
    Subscription,
    Transaction,
    TransactionStatus,
    User,
    utcnow,
)
from app.db.session import async_session_factory
from app.services.vpn import VPNService

LOCK_ID_EXPIRY_CHECK = 900101
LOCK_ID_PENDING_PROVISION = 900102


@asynccontextmanager
async def pg_advisory_lock(session: AsyncSession, lock_id: int) -> AsyncGenerator[bool, None]:
    """
    Пытается захватить сессионный PostgreSQL Advisory Lock (`pg_try_advisory_lock`).
    На SQLite (в unit-тестах) всегда возвращает True.
    """
    dialect_name = session.bind.dialect.name if session.bind else "postgresql"
    if dialect_name != "postgresql":
        yield True
        return

    acquired = False
    try:
        res = await session.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": lock_id},
        )
        acquired = bool(res.scalar())
        yield acquired
    finally:
        if acquired:
            try:
                await session.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": lock_id},
                )
            except Exception as exc:
                logger.warning("Не удалось освободить advisory lock %s: %s", lock_id, exc)


def _renew_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Продлить подписку в 1 клик", callback_data="plans:list")]
        ]
    )


async def check_expiring_and_expired_subscriptions(bot: Bot | None = None) -> dict[str, int]:
    """
    Проверяет подписки:
    - Истекшие (`expires_at <= now`, `notified_expired = False`): выключает `is_active` и шлёт уведомление.
    - Менее 24 часов до конца (`notified_1d = False`): шлёт уведомление за 24ч.
    - Менее 3 дней до конца (`notified_3d = False`): шлёт уведомление за 3 дня.
    """
    counters = {"expired": 0, "1d": 0, "3d": 0}
    now = utcnow()
    in_24h = now + timedelta(hours=24)
    in_3d = now + timedelta(days=3)

    async with async_session_factory() as session:
        async with pg_advisory_lock(session, LOCK_ID_EXPIRY_CHECK) as locked:
            if not locked:
                logger.debug("Задача check_expiring пропущена: advisory lock занят другим инстансом")
                return counters

            vpn_service = VPNService(session)

            # 1. Истекшие подписки
            exp_res = await session.execute(
                select(Subscription)
                .join(User, User.id == Subscription.user_id)
                .where(
                    Subscription.expires_at <= now,
                    Subscription.notified_expired.is_(False),
                )
            )
            expired_subs = exp_res.scalars().all()
            for sub in expired_subs:
                user = await session.get(User, sub.user_id)
                try:
                    await vpn_service.set_client_enabled(sub, False)
                except Exception as exc:
                    logger.warning("Не удалось отключить истекшего клиента %s в 3X-UI: %s", sub.xui_email, exc)
                    sub.is_active = False

                sub.notified_expired = True
                sub.notified_1d = True
                sub.notified_3d = True
                await session.commit()
                counters["expired"] += 1

                if bot and user:
                    try:
                        await bot.send_message(
                            chat_id=user.telegram_id,
                            text=(
                                "🔴 <b>Срок вашей VPN-подписки истёк!</b>\n\n"
                                "Доступ к серверу приостановлен.\n"
                                "Продлите подписку прямо сейчас — ваша текущая ссылка в приложении "
                                "продолжит работать автоматически без перенастройки!"
                            ),
                            reply_markup=_renew_kb(),
                            parse_mode="HTML",
                        )
                    except Exception as exc:
                        logger.debug("Не удалось отправить уведомление об истечении %s: %s", user.telegram_id, exc)

            # 2. Осталось менее 24 часов
            res_1d = await session.execute(
                select(Subscription).where(
                    Subscription.is_active.is_(True),
                    Subscription.expires_at > now,
                    Subscription.expires_at <= in_24h,
                    Subscription.notified_1d.is_(False),
                )
            )
            for sub in res_1d.scalars().all():
                user = await session.get(User, sub.user_id)
                sub.notified_1d = True
                sub.notified_3d = True
                await session.commit()
                counters["1d"] += 1

                if bot and user:
                    try:
                        await bot.send_message(
                            chat_id=user.telegram_id,
                            text=(
                                "⏳ <b>До окончания подписки осталось менее 24 часов!</b>\n\n"
                                f"Дата окончания: <b>{format_dt_msk(sub.expires_at)}</b>\n"
                                "Продлите доступ заранее, чтобы интернет не отключился в неподходящий момент."
                            ),
                            reply_markup=_renew_kb(),
                            parse_mode="HTML",
                        )
                    except Exception as exc:
                        logger.debug("Не удалось отправить 24h уведомление %s: %s", user.telegram_id, exc)

            # 3. Осталось менее 3 дней (но более 24 часов)
            res_3d = await session.execute(
                select(Subscription).where(
                    Subscription.is_active.is_(True),
                    Subscription.expires_at > in_24h,
                    Subscription.expires_at <= in_3d,
                    Subscription.notified_3d.is_(False),
                )
            )
            for sub in res_3d.scalars().all():
                user = await session.get(User, sub.user_id)
                sub.notified_3d = True
                await session.commit()
                counters["3d"] += 1

                if bot and user:
                    try:
                        await bot.send_message(
                            chat_id=user.telegram_id,
                            text=(
                                "🔔 <b>Напоминание: ваша подписка истекает через 3 дня</b>\n\n"
                                f"Действует до: <b>{format_dt_msk(sub.expires_at)}</b>\n"
                                "При продлении сейчас новые дни прибавятся к оставшемуся сроку."
                            ),
                            reply_markup=_renew_kb(),
                            parse_mode="HTML",
                        )
                    except Exception as exc:
                        logger.debug("Не удалось отправить 3d уведомление %s: %s", user.telegram_id, exc)

    return counters


async def retry_pending_provisions(bot: Bot | None = None) -> int:
    """
    Каждые 30 секунд проверяет транзакции в статусе PAID_PENDING_PROVISION
    и повторяет выдачу подписки в 3X-UI.
    """
    provisioned_count = 0
    async with async_session_factory() as session:
        async with pg_advisory_lock(session, LOCK_ID_PENDING_PROVISION) as locked:
            if not locked:
                return 0

            res = await session.execute(
                select(Transaction).where(
                    Transaction.status == TransactionStatus.PAID_PENDING_PROVISION
                )
            )
            pending_txs = res.scalars().all()
            if not pending_txs:
                return 0

            vpn_service = VPNService(session)
            for tx in pending_txs:
                ok, sub = await vpn_service.process_successful_payment(tx, bot=bot)
                if ok and sub:
                    provisioned_count += 1
                    user = await session.get(User, tx.user_id)
                    if bot and user:
                        try:
                            await bot.send_message(
                                chat_id=user.telegram_id,
                                text=(
                                    "🎉 <b>Ваш VPN-ключ успешно синхронизирован и активирован!</b>\n\n"
                                    + format_subscription_card(sub)
                                ),
                                reply_markup=subscription_card_kb(sub),
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                        except Exception as exc:
                            logger.warning("Не удалось уведомить пользователя после докачки ключа: %s", exc)

    return provisioned_count


def create_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        retry_pending_provisions,
        trigger="interval",
        seconds=30,
        kwargs={"bot": bot},
        id="retry_pending_provisions",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        check_expiring_and_expired_subscriptions,
        trigger="interval",
        minutes=5,
        kwargs={"bot": bot},
        id="check_expiring_subscriptions",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
