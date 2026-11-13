from __future__ import annotations

import io
import json
import secrets
import zipfile
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from typing import Any, AsyncGenerator

from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.profile import format_subscription_card
from app.bot.keyboards.user import invoice_check_kb, subscription_card_kb
from app.bot.utils import format_dt_msk, send_banner_to_chat
from app.core.config import settings
from app.core.logging import logger, send_admin_alert
from app.db.models import (
    PaymentProviderType,
    Plan,
    PromoCode,
    PromoType,
    Subscription,
    Transaction,
    TransactionStatus,
    TransactionType,
    User,
    utcnow,
)
from app.db.repositories import Repository
from app.db.session import async_session_factory
from app.services.payments.rub_gateway import PlategaProvider
from app.services.vpn import VPNService, calculate_plan_renewal_price_with_slots
from app.services.xui.client import XUIClient

LOCK_ID_EXPIRY_CHECK = 900101
LOCK_ID_PENDING_PROVISION = 900102
LOCK_ID_XUI_SYNC = 900103
LOCK_ID_RETENTION = 900104
LOCK_ID_CLEANUP = 900105
LOCK_ID_BACKUP = 900106


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
    - Автопродление за 12 часов до конца (`auto_renew = True`, `auto_renew_attempted = False`):
      списывает баланс и продлевает, либо формирует готовый инвойс в 1 клик.
    - Истекшие (`expires_at <= now`, `notified_expired = False`): выключает `is_active` и шлёт уведомление.
    - Менее 24 часов до конца (`notified_1d = False`): шлёт уведомление за 24ч.
    - Менее 3 дней до конца (`notified_3d = False`): шлёт уведомление за 3 дня.
    """
    counters = {"expired": 0, "1d": 0, "3d": 0}
    now = utcnow()
    in_12h = now + timedelta(hours=12)
    in_24h = now + timedelta(hours=24)
    in_3d = now + timedelta(days=3)

    async with async_session_factory() as session:
        async with pg_advisory_lock(session, LOCK_ID_EXPIRY_CHECK) as locked:
            if not locked:
                return counters

            vpn_service = VPNService(session)
            repo = Repository(session)

            # 0. Автопродление за 12 часов до истечения
            ar_res = await session.execute(
                select(Subscription)
                .join(User, User.id == Subscription.user_id)
                .where(
                    Subscription.is_active.is_(True),
                    Subscription.expires_at > now,
                    Subscription.expires_at <= in_12h,
                    Subscription.auto_renew_attempted.is_(False),
                    User.auto_renew.is_(True),
                )
            )
            for sub in ar_res.scalars().all():
                user = await session.get(User, sub.user_id)
                if user is None:
                    continue
                sub.auto_renew_attempted = True
                await session.commit()

                plan = (
                    await session.get(Plan, sub.current_plan_id)
                    if sub.current_plan_id
                    else None
                )
                if plan is None:
                    plans = await repo.get_active_plans()
                    plan = plans[0] if plans else None
                if plan is None:
                    continue

                full_price = calculate_plan_renewal_price_with_slots(
                    plan, extra_devices=sub.extra_devices
                )
                if user.balance_rub >= full_price:
                    tx = Transaction(
                        user_id=user.id,
                        plan_id=plan.id,
                        tx_type=TransactionType.AUTO_RENEW,
                        provider=PaymentProviderType.BALANCE,
                        currency="RUB",
                        amount_currency=full_price,
                        amount_rub=Decimal("0.00"),
                        balance_used_rub=full_price,
                        status=TransactionStatus.PENDING,
                    )
                    session.add(tx)
                    await session.commit()
                    await session.refresh(tx)
                    ok, renewed_sub = await vpn_service.process_successful_payment(tx, bot=bot)
                    if ok and renewed_sub and bot:
                        try:
                            await send_banner_to_chat(
                                bot,
                                user.telegram_id,
                                banner_key="profile",
                                caption=(
                                    f"🔄 <b>Автопродление выполнено успешно!</b>\n\n"
                                    f"С вашего бонусного баланса списано <b>{full_price} ₽</b>, "
                                    f"подписка продлена на <b>+{plan.days} дн.</b> без разрыва соединения!"
                                ),
                                reply_markup=subscription_card_kb(user, renewed_sub),
                                lang=user.language,
                                repo=repo,
                            )
                        except Exception:
                            pass
                elif bot:
                    # Если баланса не хватает — выставляем готовый инвойс в 1 клик
                    balance_used = min(user.balance_rub, full_price)
                    to_pay = max(Decimal("1.00"), (full_price - balance_used).quantize(Decimal("0.01")))
                    tx = Transaction(
                        user_id=user.id,
                        plan_id=plan.id,
                        tx_type=TransactionType.AUTO_RENEW,
                        provider=PaymentProviderType.RUB_GATEWAY,
                        currency="RUB",
                        amount_currency=to_pay,
                        amount_rub=to_pay,
                        balance_used_rub=balance_used,
                        status=TransactionStatus.PENDING,
                    )
                    session.add(tx)
                    await session.commit()
                    await session.refresh(tx)
                    inv = await PlategaProvider().create_invoice(
                        transaction_id=tx.id,
                        amount_rub=to_pay,
                        description=f"GhostVPN: Автопродление {plan.name}",
                        user_telegram_id=user.telegram_id,
                    )
                    tx.external_id = inv.external_id
                    tx.payment_url = inv.payment_url
                    await session.commit()
                    if inv.payment_url:
                        try:
                            await send_banner_to_chat(
                                bot,
                                user.telegram_id,
                                banner_key="tariffs",
                                caption=(
                                    f"⚡ <b>До конца подписки осталось менее 12 часов!</b>\n\n"
                                    f"На внутреннем балансе пока недостаточно средств для автоматического продления.\n"
                                    f"Мы подготовили для тебя быстрый счёт на <b>{plan.name}</b> к оплате в 1 клик (<b>{to_pay} ₽</b>):"
                                ),
                                reply_markup=invoice_check_kb(tx.id, inv.payment_url),
                                lang=user.language,
                                repo=repo,
                            )
                        except Exception:
                            pass

            # 1. Истекшие подписки
            exp_res = await session.execute(
                select(Subscription)
                .join(User, User.id == Subscription.user_id)
                .where(
                    Subscription.expires_at <= now,
                    Subscription.notified_expired.is_(False),
                )
            )
            for sub in exp_res.scalars().all():
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
                                "🔴 <b>Срок твоей VPN-подписки подошёл к концу!</b>\n\n"
                                "Защита временно приостановлена.\n"
                                "Нажми кнопку ниже, чтобы продлить доступ в 1 клик — твоя текущая ссылка в приложении "
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
                                "⏳ <b>До окончания подписки осталось меньше 24 часов!</b>\n\n"
                                f"Действует до: <b>{format_dt_msk(sub.expires_at)}</b>\n"
                                "Продли доступ заранее, чтобы интернет не остановился в важный момент."
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
                                "🔔 <b>Напоминание: подписка закончится через 3 дня</b>\n\n"
                                f"Действует до: <b>{format_dt_msk(sub.expires_at)}</b>\n"
                                "При продлении сейчас новые дни прибавятся к оставшемуся сроку."
                            ),
                            reply_markup=_renew_kb(),
                            parse_mode="HTML",
                        )
                    except Exception as exc:
                        logger.debug("Не удалось отправить 3d уведомление %s: %s", user.telegram_id, exc)

    return counters


async def sync_xui_traffic_and_enforce_ip_limits(bot: Bot | None = None) -> dict[str, int]:
    """
    Каждые 15 минут:
    1) Синхронизирует потреблённый трафик (`used_traffic_bytes`) активных подписок из 3X-UI.
    2) Проверяет список онлайн-клиентов и количество активных IP (`get_client_ips`).
    3) При превышении `limit_ip`:
       - 1-2 раз: отправляет дружелюбное предупреждение с кнопкой «➕ Добавить устройство».
       - 3 раз подряд: временно приостанавливает клиента в 3X-UI и предлагает разблокировать в 1 клик.
    """
    stats = {"synced": 0, "warned": 0, "suspended": 0}
    now = utcnow()

    async with async_session_factory() as session:
        async with pg_advisory_lock(session, LOCK_ID_XUI_SYNC) as locked:
            if not locked:
                return stats

            repo = Repository(session)
            server = await repo.get_active_server()
            if server is None:
                return stats

            active_subs = await repo.get_all_active_subscriptions()
            if not active_subs:
                return stats

            client = XUIClient.from_server(server, timeout=8.0)
            try:
                online_emails = set(await client.get_online_clients())
                for sub in active_subs:
                    tr = await client.get_client_traffics(sub.xui_email)
                    if tr is not None:
                        sub.used_traffic_bytes = tr.total_bytes
                    stats["synced"] += 1

                    if sub.xui_email in online_emails:
                        ips = await client.get_client_ips(sub.xui_email)
                        active_ips_count = max(1, len(ips))
                    else:
                        active_ips_count = 0

                    sub.online_ips_count = active_ips_count

                    if active_ips_count > sub.limit_ip:
                        sub.ip_violations_count += 1
                        user = await session.get(User, sub.user_id)
                        if sub.ip_violations_count >= 3:
                            await client.update_client(
                                inbound_id=server.inbound_id,
                                client_uuid=sub.xui_client_uuid,
                                email=sub.xui_email,
                                sub_id=sub.sub_id,
                                expiry_time_ms=int(now.timestamp() * 1000)
                                if sub.expires_at <= now
                                else int(sub.expires_at.timestamp() * 1000),
                                limit_ip=sub.limit_ip,
                                total_gb=0,
                                enable=False,
                                tg_id=str(user.telegram_id if user else ""),
                            )
                            sub.is_active = False
                            stats["suspended"] += 1
                            if bot and user:
                                kb = InlineKeyboardMarkup(
                                    inline_keyboard=[
                                        [
                                            InlineKeyboardButton(
                                                text="➕ Добавить устройство (+1 слот)",
                                                callback_data="devices:menu",
                                            )
                                        ],
                                        [
                                            InlineKeyboardButton(
                                                text="🔓 Я отключил лишние устройства (Разблокировать)",
                                                callback_data="profile:unlock_ip",
                                            )
                                        ],
                                    ]
                                )
                                try:
                                    await send_banner_to_chat(
                                        bot,
                                        user.telegram_id,
                                        banner_key="devices",
                                        caption=(
                                            f"⚠️ <b>Временная защита ключа от перегрузки</b>\n\n"
                                            f"К твоему ключу одновременно подключилось <b>{active_ips_count} устр.</b> "
                                            f"при лимите <b>{sub.limit_ip} устр.</b>\n\n"
                                            f"Отключи лишнее устройство и нажми кнопку <b>«🔓 Разблокировать»</b> ниже, "
                                            f"или добавь +1 дополнительный слот!"
                                        ),
                                        reply_markup=kb,
                                        lang=user.language,
                                        repo=repo,
                                    )
                                except Exception:
                                    pass
                        else:
                            stats["warned"] += 1
                            if bot and user:
                                kb = InlineKeyboardMarkup(
                                    inline_keyboard=[
                                        [
                                            InlineKeyboardButton(
                                                text="➕ Добавить устройство (+1 слот)",
                                                callback_data="devices:menu",
                                            )
                                        ],
                                        [
                                            InlineKeyboardButton(
                                                text="🔄 Обновить ссылку (если кто-то чужой)",
                                                callback_data="profile:reissue_confirm",
                                            )
                                        ],
                                    ]
                                )
                                try:
                                    await send_banner_to_chat(
                                        bot,
                                        user.telegram_id,
                                        banner_key="devices",
                                        caption=(
                                            f"👻 <b>Заботливое уведомление о лимите устройств</b>\n\n"
                                            f"Сейчас к твоему ключу подключено <b>{active_ips_count} из {sub.limit_ip} устр.</b>\n"
                                            f"Чтобы интернет работал стабильно на всех гаджетах, добавь дополнительный слот "
                                            f"или отключи лишнее устройство."
                                        ),
                                        reply_markup=kb,
                                        lang=user.language,
                                        repo=repo,
                                    )
                                except Exception:
                                    pass
                    else:
                        sub.ip_violations_count = 0

                await session.commit()
            finally:
                await client.close()

    return stats


async def run_retention_onboarding_and_winback(bot: Bot | None = None) -> dict[str, int]:
    """
    Воронка удержания (Retention):
    1) Онбординг-помощник: прошло >= 6 часов с `started_at`, подписка активна, но `used_traffic_bytes == 0`.
    2) Win-back: подписка истекла >= 3 дня назад, не продлена -> генерирует персональный промокод -25% на 48 часов.
    """
    res_counts = {"onboarding": 0, "winback": 0}
    now = utcnow()
    six_hours_ago = now - timedelta(hours=6)
    three_days_ago = now - timedelta(days=3)

    async with async_session_factory() as session:
        async with pg_advisory_lock(session, LOCK_ID_RETENTION) as locked:
            if not locked:
                return res_counts

            repo = Repository(session)

            # 1. Онбординг-помощник (0 байт трафика спустя 6 часов)
            onb_res = await session.execute(
                select(Subscription).where(
                    Subscription.is_active.is_(True),
                    Subscription.expires_at > now,
                    Subscription.started_at <= six_hours_ago,
                    Subscription.used_traffic_bytes == 0,
                    Subscription.notified_onboarding.is_(False),
                )
            )
            for sub in onb_res.scalars().all():
                user = await session.get(User, sub.user_id)
                sub.notified_onboarding = True
                await session.commit()
                res_counts["onboarding"] += 1

                if bot and user:
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="📲 Инструкция за 30 секунд",
                                    callback_data="help:instructions",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="📶 Не грузит на мобильном?",
                                    callback_data="help:mobile_boost",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="💬 Написать помощнику",
                                    url=settings.SUPPORT_URL,
                                )
                            ],
                        ]
                    )
                    try:
                        await send_banner_to_chat(
                            bot,
                            user.telegram_id,
                            banner_key="connect",
                            caption=(
                                "👻 <b>Привет! Заметил, что ты ещё не подключился к VPN</b>\n\n"
                                "Твой ключ уже активен, но по нему пока 0 МБ трафика.\n"
                                "Если возникли сложности с установкой приложения на телефон или ноутбук — "
                                "нажми на кнопку инструкции ниже или напиши нам в поддержку, мы настроим всё вместе за 1 минуту!"
                            ),
                            reply_markup=kb,
                            lang=user.language,
                            repo=repo,
                        )
                    except Exception:
                        pass

            # 2. Win-back: подписка истекла >= 3 дней назад
            wb_res = await session.execute(
                select(Subscription).where(
                    Subscription.expires_at <= three_days_ago,
                    Subscription.notified_winback.is_(False),
                )
            )
            for sub in wb_res.scalars().all():
                user = await session.get(User, sub.user_id)
                if user is None:
                    continue
                sub.notified_winback = True
                code_str = f"GHOST25-{secrets.token_hex(2).upper()}"
                promo = PromoCode(
                    code=code_str,
                    type=PromoType.PERCENT,
                    value=25,
                    max_uses=1,
                    only_new_users=False,
                    expires_at=now + timedelta(hours=48),
                    is_active=True,
                )
                session.add(promo)
                # Также сразу активируем скидку на аккаунте для удобства
                user.pending_discount_percent = max(user.pending_discount_percent, 25)
                await session.commit()
                res_counts["winback"] += 1

                if bot:
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="🔥 Забрать тариф со скидкой -25%",
                                    callback_data="plans:list",
                                )
                            ]
                        ]
                    )
                    try:
                        await send_banner_to_chat(
                            bot,
                            user.telegram_id,
                            banner_key="promo",
                            caption=(
                                f"👻 <b>Мы скучали! Возвращайся в тень со скидкой -25%</b>\n\n"
                                f"Твоя подписка закончилась несколько дней назад. Мы приготовили для тебя "
                                f"персональный секретный промокод на <b>скидку 25%</b> (действует 48 часов):\n\n"
                                f"🎟 Код: <code>{code_str}</code> <i>(уже применён к твоему аккаунту!)</i>\n\n"
                                f"Нажми кнопку ниже — твоя старая ссылка в приложении сразу заработает снова!"
                            ),
                            reply_markup=kb,
                            lang=user.language,
                            repo=repo,
                        )
                    except Exception:
                        pass

    return res_counts


async def cleanup_stale_xui_clients() -> int:
    """
    Раз в сутки удаляет из панели 3X-UI клиентов, чья подписка истекла более 7 дней назад,
    выставляя `deleted_from_xui = True` (при будущем продлении клиент пересоздастся с тем же `sub_id`).
    """
    deleted_count = 0
    cutoff = utcnow() - timedelta(days=7)

    async with async_session_factory() as session:
        async with pg_advisory_lock(session, LOCK_ID_CLEANUP) as locked:
            if not locked:
                return 0

            repo = Repository(session)
            server = await repo.get_active_server()
            if server is None:
                return 0

            res = await session.execute(
                select(Subscription).where(
                    Subscription.expires_at <= cutoff,
                    Subscription.deleted_from_xui.is_(False),
                )
            )
            stale_subs = res.scalars().all()
            if not stale_subs:
                return 0

            client = XUIClient.from_server(server, timeout=8.0)
            try:
                for sub in stale_subs:
                    try:
                        await client.delete_client_by_uuid(
                            inbound_id=server.inbound_id,
                            client_uuid=sub.xui_client_uuid,
                        )
                    except Exception as exc:
                        logger.debug("Удаление просроченного клиента %s: %s", sub.xui_email, exc)
                    sub.deleted_from_xui = True
                    sub.is_active = False
                    deleted_count += 1
                await session.commit()
            finally:
                await client.close()

    return deleted_count


async def create_and_send_database_backup(
    bot: Bot | None = None, target_chat_id: int | None = None
) -> bool:
    """
    Формирует в памяти ZIP-архив (`ghostvpn_backup_YYYYMMDD.zip`) с `dump.json` и `dump.sql`
    ключевых таблиц БД и отправляет его в `ADMIN_CHAT_ID` (или `target_chat_id`).
    """
    now = utcnow()
    async with async_session_factory() as session:
        async with pg_advisory_lock(session, LOCK_ID_BACKUP) as locked:
            if not locked and target_chat_id is None:
                return False

            users = (await session.execute(select(User))).scalars().all()
            plans = (await session.execute(select(Plan))).scalars().all()
            subs = (await session.execute(select(Subscription))).scalars().all()
            txs = (await session.execute(select(Transaction))).scalars().all()
            promos = (await session.execute(select(PromoCode))).scalars().all()

            dump_data: dict[str, list[dict[str, Any]]] = {
                "users": [
                    {
                        "id": u.id,
                        "telegram_id": u.telegram_id,
                        "username": u.username,
                        "language": u.language,
                        "balance_rub": str(u.balance_rub),
                        "has_used_trial": u.has_used_trial,
                        "auto_renew": u.auto_renew,
                        "is_banned": u.is_banned,
                    }
                    for u in users
                ],
                "plans": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "days": p.days,
                        "price_rub": str(p.price_rub),
                        "limit_ip": p.limit_ip,
                    }
                    for p in plans
                ],
                "subscriptions": [
                    {
                        "id": s.id,
                        "user_id": s.user_id,
                        "xui_client_uuid": s.xui_client_uuid,
                        "xui_email": s.xui_email,
                        "sub_id": s.sub_id,
                        "limit_ip": s.limit_ip,
                        "extra_devices": s.extra_devices,
                        "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                        "is_active": s.is_active,
                    }
                    for s in subs
                ],
                "transactions": [
                    {
                        "id": t.id,
                        "user_id": t.user_id,
                        "plan_id": t.plan_id,
                        "tx_type": t.tx_type.value,
                        "provider": t.provider.value,
                        "amount_rub": str(t.amount_rub),
                        "status": t.status.value,
                    }
                    for t in txs
                ],
                "promocodes": [
                    {
                        "id": pr.id,
                        "code": pr.code,
                        "type": pr.type.value,
                        "value": pr.value,
                        "used_count": pr.used_count,
                        "max_uses": pr.max_uses,
                    }
                    for pr in promos
                ],
            }

            sql_lines = [
                f"-- GhostVPN SQL Backup generated at {now.isoformat()}",
                f"-- Users: {len(users)}, Subscriptions: {len(subs)}, Transactions: {len(txs)}",
            ]
            for u in users:
                uname = (u.username or "").replace("'", "''")
                sql_lines.append(
                    f"INSERT INTO users (id, telegram_id, username, language, balance_rub, has_used_trial, auto_renew, is_banned) "
                    f"VALUES ({u.id}, {u.telegram_id}, '{uname}', '{u.language}', {u.balance_rub}, "
                    f"{str(u.has_used_trial).upper()}, {str(u.auto_renew).upper()}, {str(u.is_banned).upper()}) "
                    f"ON CONFLICT (telegram_id) DO NOTHING;"
                )

            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(
                    "dump.json",
                    json.dumps(dump_data, ensure_ascii=False, indent=2),
                )
                zf.writestr("dump.sql", "\n".join(sql_lines))

            zip_bytes = zip_buf.getvalue()

    if bot is None:
        return True

    destinations: list[int] = []
    if target_chat_id:
        destinations.append(target_chat_id)
    elif settings.ADMIN_CHAT_ID:
        destinations.append(settings.ADMIN_CHAT_ID)
    elif settings.ADMIN_IDS:
        destinations.append(settings.ADMIN_IDS[0])

    filename = f"ghostvpn_backup_{now.strftime('%Y%m%d_%H%M')}.zip"
    for chat_id in destinations:
        try:
            doc = BufferedInputFile(zip_bytes, filename=filename)
            await bot.send_document(
                chat_id=chat_id,
                document=doc,
                caption=(
                    f"💾 <b>Ежедневный автобэкап БД GhostVPN</b>\n"
                    f"• Пользователей: <b>{len(users)}</b>\n"
                    f"• Подписок: <b>{len(subs)}</b>\n"
                    f"• Транзакций: <b>{len(txs)}</b>"
                ),
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Не удалось отправить бэкап БД в чат %s: %s", chat_id, exc)

    return True


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
                            card = await format_subscription_card(sub, user=user)
                            await bot.send_message(
                                chat_id=user.telegram_id,
                                text=(
                                    "🎉 <b>Твой VPN-ключ успешно синхронизирован и активирован!</b>\n\n"
                                    + card
                                ),
                                reply_markup=subscription_card_kb(user, sub),
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
    scheduler.add_job(
        sync_xui_traffic_and_enforce_ip_limits,
        trigger="interval",
        minutes=15,
        kwargs={"bot": bot},
        id="sync_xui_traffic_and_ip_limits",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_retention_onboarding_and_winback,
        trigger="interval",
        minutes=30,
        kwargs={"bot": bot},
        id="retention_onboarding_and_winback",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        cleanup_stale_xui_clients,
        trigger="interval",
        hours=24,
        id="cleanup_stale_xui_clients",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        create_and_send_database_backup,
        trigger="interval",
        hours=24,
        kwargs={"bot": bot},
        id="daily_database_backup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
