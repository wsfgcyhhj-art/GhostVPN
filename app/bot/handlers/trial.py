from __future__ import annotations

from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.types import CallbackQuery, LabeledPrice
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.profile import format_subscription_card
from app.bot.keyboards.user import (
    channel_check_kb,
    invoice_check_kb,
    subscription_card_kb,
    trial_payment_kb,
)
from app.bot.utils import send_or_edit_banner
from app.core.config import settings
from app.core.logging import logger
from app.db.models import (
    PaymentProviderType,
    Transaction,
    TransactionStatus,
    TransactionType,
    User,
)
from app.db.repositories import Repository
from app.services.payments.rub_gateway import FreeKassaProvider
from app.services.vpn import VPNService

router = Router(name="trial")

ALLOWED_MEMBER_STATUSES = {
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.RESTRICTED,
}


async def is_user_subscribed(bot: Bot, user_id: int) -> bool:
    """Проверяет подписку пользователя на REQUIRED_CHANNEL_ID, если он задан в .env."""
    if not settings.REQUIRED_CHANNEL_ID:
        return True
    try:
        member = await bot.get_chat_member(
            chat_id=settings.REQUIRED_CHANNEL_ID,
            user_id=user_id,
        )
        return member.status in ALLOWED_MEMBER_STATUSES
    except Exception as exc:
        logger.warning("Ошибка проверки подписки на канал %s: %s", settings.REQUIRED_CHANNEL_ID, exc)
        return False


@router.callback_query(F.data.in_({"trial:start", "trial:verify"}))
async def cb_trial_start(
    callback: CallbackQuery,
    bot: Bot,
    repo: Repository,
    db_user: User,
) -> None:
    if db_user.has_used_trial:
        await callback.answer("Ты уже использовал пробный период ранее :)", show_alert=True)
        return

    subscribed = await is_user_subscribed(bot, db_user.telegram_id)
    if not subscribed:
        if callback.data == "trial:verify":
            await callback.answer(
                "❌ Подписка на канал пока не найдена. Подпишись и нажми кнопку ещё раз!",
                show_alert=True,
            )
            return

        text = (
            f"🔥 <b>Тест-драйв GhostVPN на {settings.TRIAL_DAYS} дня</b>\n\n"
            f"Сначала подпишись на наш новостной канал (там мы публикуем важные обновления и бонусные промокоды), "
            f"а затем нажми <b>«✅ Я подписался (Проверить)»</b>."
        )
        await send_or_edit_banner(
            callback,
            banner_key="trial",
            caption=text,
            reply_markup=channel_check_kb(db_user.language),
            lang=db_user.language,
            repo=repo,
        )
        await callback.answer()
        return

    trial_rub = Decimal(str(settings.TRIAL_PRICE_RUB)).quantize(Decimal("0.01"))
    text = (
        f"🔥 <b>Полный доступ на {settings.TRIAL_DAYS} дня всего за {trial_rub} ₽!</b>\n\n"
        f"Чтобы защитить сервер от автоматических ботов и сохранить для тебя максимальную скорость 500 Мбит/с, "
        f"активация пробного периода стоит символический <b>{trial_rub} ₽</b> (или <b>{settings.TRIAL_PRICE_STARS} ⭐️ Telegram Stars</b>).\n\n"
        f"✨ <b>Что ты получишь мгновенно:</b>\n"
        f"• 🇳🇱 3 дня безлимитного трафика и максимальной скорости\n"
        f"• 📱 Подключение до <b>{settings.DEFAULT_LIMIT_IP} устройств</b> сразу\n"
        f"• 🛡 Работающий YouTube 4K, Instagram и звонки на любом интернете\n\n"
        f"Выбери удобный способ активации:"
    )
    await send_or_edit_banner(
        callback,
        banner_key="trial",
        caption=text,
        reply_markup=trial_payment_kb(db_user.balance_rub, db_user.language),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("trial:pay:"))
async def cb_trial_pay(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    repo: Repository,
    db_user: User,
) -> None:
    locked_res = await session.execute(
        select(User).where(User.id == db_user.id).with_for_update()
    )
    db_user = locked_res.scalar_one()

    if db_user.has_used_trial:
        await callback.answer("Пробный период доступен только 1 раз.", show_alert=True)
        return

    paid_trial_res = await session.execute(
        select(Transaction).where(
            Transaction.user_id == db_user.id,
            Transaction.tx_type == TransactionType.TRIAL,
            Transaction.status.in_(
                [TransactionStatus.COMPLETED, TransactionStatus.PAID_PENDING_PROVISION]
            ),
        )
    )
    if paid_trial_res.scalar_one_or_none() is not None:
        db_user.has_used_trial = True
        await session.commit()
        await callback.answer("Пробный период доступен только 1 раз.", show_alert=True)
        return

    recent_pending = await repo.count_recent_pending_transactions(db_user.id, minutes=10)
    if recent_pending >= 5:
        await callback.answer(
            "⏳ У тебя уже создано несколько активных счетов. Пожалуйста, оплати один из них или подожди пару минут.",
            show_alert=True,
        )
        return

    method_str = (callback.data or "").split(":")[2]
    trial_rub = Decimal(str(settings.TRIAL_PRICE_RUB)).quantize(Decimal("0.01"))

    if method_str == "BALANCE":
        if db_user.balance_rub < trial_rub:
            await callback.answer("Недостаточно средств на балансе.", show_alert=True)
            return

        tx = Transaction(
            user_id=db_user.id,
            plan_id=None,
            tx_type=TransactionType.TRIAL,
            provider=PaymentProviderType.BALANCE,
            currency="RUB",
            amount_currency=trial_rub,
            amount_rub=Decimal("0.00"),
            balance_used_rub=trial_rub,
            status=TransactionStatus.PENDING,
        )
        session.add(tx)
        await session.commit()
        await session.refresh(tx)

        vpn_service = VPNService(session)
        ok, sub = await vpn_service.process_successful_payment(tx, bot=bot)
        if ok and sub:
            await send_or_edit_banner(
                callback,
                banner_key="profile",
                caption=(
                    f"🎉 <b>Ура! Пробный период на {settings.TRIAL_DAYS} дня активирован!</b>\n\n"
                    + await format_subscription_card(sub, user=db_user)
                ),
                reply_markup=subscription_card_kb(db_user, sub),
                lang=db_user.language,
                repo=repo,
            )
        await callback.answer("Пробный период активирован!")
        return

    if method_str == "STARS":
        tx = Transaction(
            user_id=db_user.id,
            plan_id=None,
            tx_type=TransactionType.TRIAL,
            provider=PaymentProviderType.STARS,
            currency="XTR",
            amount_currency=Decimal(str(settings.TRIAL_PRICE_STARS)),
            amount_rub=trial_rub,
            balance_used_rub=Decimal("0.00"),
            status=TransactionStatus.PENDING,
        )
        session.add(tx)
        await session.commit()
        await session.refresh(tx)
        tx.external_id = f"stars_trial_{tx.id}"
        await session.commit()

        await bot.send_invoice(
            chat_id=db_user.telegram_id,
            title=f"GhostVPN — Тест на {settings.TRIAL_DAYS} дня",
            description=f"Активация полного пробного доступа GhostVPN на {settings.TRIAL_DAYS} дня (до 3 устройств)",
            payload=f"tx:{tx.id}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Пробный период 3 дня", amount=settings.TRIAL_PRICE_STARS)],
        )
        await callback.answer("Счёт в Telegram Stars отправлен!")
        return

    if method_str == "RUB_GATEWAY":
        tx = Transaction(
            user_id=db_user.id,
            plan_id=None,
            tx_type=TransactionType.TRIAL,
            provider=PaymentProviderType.RUB_GATEWAY,
            currency="RUB",
            amount_currency=trial_rub,
            amount_rub=trial_rub,
            balance_used_rub=Decimal("0.00"),
            status=TransactionStatus.PENDING,
        )
        session.add(tx)
        await session.commit()
        await session.refresh(tx)

        fk = FreeKassaProvider()
        inv = await fk.create_invoice(
            transaction_id=tx.id,
            amount_rub=trial_rub,
            description=f"GhostVPN: Пробный доступ на {settings.TRIAL_DAYS} дня",
            user_telegram_id=db_user.telegram_id,
        )
        tx.external_id = inv.external_id
        tx.payment_url = inv.payment_url
        await session.commit()

        if inv.payment_url:
            await send_or_edit_banner(
                callback,
                banner_key="trial",
                caption=(
                    f"💳 <b>Активация триала за {trial_rub} ₽ (Заказ #{tx.id})</b>\n\n"
                    f"Нажми кнопку <b>«🔗 Перейти к оплате»</b> (СБП или любая карта РФ).\n"
                    f"Сразу после оплаты бот мгновенно выдаст твой ключ доступа!"
                ),
                reply_markup=invoice_check_kb(tx.id, inv.payment_url),
                lang=db_user.language,
                repo=repo,
            )
        await callback.answer()
