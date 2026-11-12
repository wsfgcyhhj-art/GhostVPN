from __future__ import annotations

from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.profile import format_subscription_card
from app.bot.keyboards.user import (
    back_to_menu_kb,
    invoice_check_kb,
    payment_methods_kb,
    plans_list_kb,
    subscription_card_kb,
)
from app.db.models import (
    PaymentProviderType,
    Plan,
    Transaction,
    TransactionStatus,
    User,
)
from app.db.repositories import Repository
from app.services.payments.cryptobot import CryptoBotError, CryptoBotProvider
from app.services.payments.rub_gateway import RubGatewayStub
from app.services.payments.stars import TelegramStarsProvider
from app.services.vpn import VPNService

router = Router(name="billing")


def calculate_checkout_amounts(
    plan_price_rub: Decimal,
    discount_percent: int,
    user_balance_rub: Decimal,
    use_balance: bool,
) -> tuple[Decimal, Decimal]:
    """
    Возвращает кортеж (to_pay_rub, balance_used_rub).
    1) Применяется процентная скидка по промокоду (если есть).
    2) Если use_balance=True, списывается доступный баланс (вплоть до полной стоимости).
    """
    price_after_discount = plan_price_rub
    if discount_percent > 0:
        price_after_discount = (
            plan_price_rub * Decimal(100 - discount_percent) / Decimal(100)
        ).quantize(Decimal("0.01"))

    price_after_discount = max(Decimal("1.00"), price_after_discount)

    if not use_balance or user_balance_rub <= Decimal("0.00"):
        return price_after_discount, Decimal("0.00")

    balance_used = min(user_balance_rub, price_after_discount)
    to_pay = (price_after_discount - balance_used).quantize(Decimal("0.01"))
    return to_pay, balance_used


@router.callback_query(F.data == "plans:list")
async def cb_plans_list(
    callback: CallbackQuery,
    repo: Repository,
    db_user: User,
) -> None:
    plans = await repo.get_active_plans()
    discount = db_user.pending_discount_percent
    discount_line = (
        f"\n🎟 <b>Активна скидка {discount}% на следующую покупку!</b>\n"
        if discount > 0
        else ""
    )
    text = (
        f"💳 <b>Выберите тарифный план GhostVPN</b>\n"
        f"{discount_line}\n"
        f"Во все тарифы входит:\n"
        f"• 🇳🇱 Высокоскоростной сервер в Нидерландах (VLESS + Reality)\n"
        f"• 📱 До <b>3 устройств</b> одновременно\n"
        f"• ♾ Безлимитный трафик без ограничений скорости\n"
        f"• 🔄 Автоматическое продление вашей текущей ссылки"
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=plans_list_kb(plans, discount_percent=discount),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("plan:select:"))
async def cb_plan_select(
    callback: CallbackQuery,
    repo: Repository,
    db_user: User,
) -> None:
    parts = (callback.data or "").split(":")
    plan_id = int(parts[2])
    use_balance = len(parts) > 3 and parts[3] == "1"

    plan = await repo.get_plan_by_id(plan_id)
    if plan is None or not plan.is_active:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    to_pay_rub, balance_used_rub = calculate_checkout_amounts(
        plan_price_rub=plan.price_rub,
        discount_percent=db_user.pending_discount_percent,
        user_balance_rub=db_user.balance_rub,
        use_balance=use_balance,
    )
    price_after_promo, _ = calculate_checkout_amounts(
        plan_price_rub=plan.price_rub,
        discount_percent=db_user.pending_discount_percent,
        user_balance_rub=db_user.balance_rub,
        use_balance=False,
    )

    text = (
        f"📦 <b>Оформление подписки: {plan.name} ({plan.days} дн.)</b>\n\n"
        f"• Базовая цена: <b>{plan.price_rub} ₽</b>\n"
    )
    if db_user.pending_discount_percent > 0:
        text += f"• Промо-скидка: <b>-{db_user.pending_discount_percent}%</b>\n"
    if use_balance and balance_used_rub > Decimal("0.00"):
        text += f"• Списание с баланса: <b>-{balance_used_rub} ₽</b>\n"

    text += f"\n💵 <b>Итого к оплате: {to_pay_rub} ₽</b>\nВыберите удобный способ оплаты:"

    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=payment_methods_kb(
                plan_id=plan.id,
                final_price_rub=price_after_promo,
                user_balance_rub=db_user.balance_rub,
                use_balance=use_balance,
            ),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("pay:balance_full:"))
async def cb_pay_balance_full(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    repo: Repository,
    db_user: User,
) -> None:
    plan_id = int((callback.data or "").split(":")[2])
    plan = await repo.get_plan_by_id(plan_id)
    if plan is None or not plan.is_active:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    _, balance_needed = calculate_checkout_amounts(
        plan_price_rub=plan.price_rub,
        discount_percent=db_user.pending_discount_percent,
        user_balance_rub=db_user.balance_rub,
        use_balance=True,
    )
    price_after_promo, _ = calculate_checkout_amounts(
        plan_price_rub=plan.price_rub,
        discount_percent=db_user.pending_discount_percent,
        user_balance_rub=db_user.balance_rub,
        use_balance=False,
    )
    if db_user.balance_rub < price_after_promo:
        await callback.answer("Недостаточно средств на балансе.", show_alert=True)
        return

    tx = Transaction(
        user_id=db_user.id,
        plan_id=plan.id,
        provider=PaymentProviderType.BALANCE,
        amount_rub=Decimal("0.00"),
        balance_used_rub=balance_needed,
        discount_percent=db_user.pending_discount_percent,
        status=TransactionStatus.PENDING,
    )
    session.add(tx)
    await session.commit()
    await session.refresh(tx)

    vpn_service = VPNService(session)
    ok, sub = await vpn_service.process_successful_payment(tx, bot=bot)
    if ok and sub:
        if callback.message:
            await callback.message.edit_text(
                f"✅ <b>Тариф «{plan.name}» успешно оплачен с баланса!</b>\n\n"
                + format_subscription_card(sub),
                reply_markup=subscription_card_kb(sub),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    else:
        if callback.message:
            await callback.message.edit_text(
                "✅ <b>Оплата с баланса зафиксирована!</b>\n"
                "Сервер синхронизирует ваш ключ, доступ активируется автоматически в течение пары минут.",
                reply_markup=back_to_menu_kb(),
                parse_mode="HTML",
            )
    await callback.answer()


@router.callback_query(F.data.startswith("pay:create:"))
async def cb_pay_create(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    repo: Repository,
    db_user: User,
) -> None:
    parts = (callback.data or "").split(":")
    provider_str = parts[2]
    plan_id = int(parts[3])
    use_balance = len(parts) > 4 and parts[4] == "1"

    plan = await repo.get_plan_by_id(plan_id)
    if plan is None or not plan.is_active:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    to_pay_rub, balance_used_rub = calculate_checkout_amounts(
        plan_price_rub=plan.price_rub,
        discount_percent=db_user.pending_discount_percent,
        user_balance_rub=db_user.balance_rub,
        use_balance=use_balance,
    )
    if to_pay_rub <= Decimal("0.00"):
        await callback.answer("Используйте кнопку полной оплаты с баланса.", show_alert=True)
        return

    provider_enum = PaymentProviderType(provider_str)
    tx = Transaction(
        user_id=db_user.id,
        plan_id=plan.id,
        provider=provider_enum,
        amount_rub=to_pay_rub,
        balance_used_rub=balance_used_rub,
        discount_percent=db_user.pending_discount_percent,
        status=TransactionStatus.PENDING,
    )
    session.add(tx)
    await session.commit()
    await session.refresh(tx)

    description = f"GhostVPN: подписка {plan.name} ({plan.days} дн.)"

    if provider_enum == PaymentProviderType.STARS:
        stars_provider = TelegramStarsProvider()
        inv = await stars_provider.create_invoice(
            transaction_id=tx.id,
            amount_rub=to_pay_rub,
            description=description,
            user_telegram_id=db_user.telegram_id,
        )
        tx.external_id = inv.external_id
        await session.commit()

        await bot.send_invoice(
            chat_id=db_user.telegram_id,
            title=f"GhostVPN — {plan.name}",
            description=description,
            payload=f"tx:{tx.id}",
            provider_token="",  # Для Telegram Stars (XTR) пустая строка
            currency="XTR",
            prices=[LabeledPrice(label=plan.name, amount=inv.stars_amount or 1)],
        )
        await callback.answer("Счёт в Telegram Stars выставлен!")
        return

    if provider_enum == PaymentProviderType.CRYPTOBOT:
        crypto_provider = CryptoBotProvider()
        try:
            inv = await crypto_provider.create_invoice(
                transaction_id=tx.id,
                amount_rub=to_pay_rub,
                description=description,
                user_telegram_id=db_user.telegram_id,
            )
        except CryptoBotError as exc:
            tx.status = TransactionStatus.CANCELED
            await session.commit()
            await callback.answer(f"Ошибка CryptoBot: {exc}", show_alert=True)
            return

        tx.external_id = inv.external_id
        tx.payment_url = inv.payment_url
        await session.commit()

        if callback.message and inv.payment_url:
            await callback.message.edit_text(
                f"💎 <b>Счёт CryptoBot #{inv.external_id}</b>\n\n"
                f"• Тариф: <b>{plan.name}</b>\n"
                f"• Сумма к оплате: <b>{to_pay_rub} ₽</b>\n\n"
                f"Нажмите «Перейти к оплате». После оплаты подписка активируется автоматически "
                f"(либо нажмите «Проверить оплату»).",
                reply_markup=invoice_check_kb(tx.id, inv.payment_url),
                parse_mode="HTML",
            )
        await callback.answer()
        return

    if provider_enum == PaymentProviderType.RUB_GATEWAY:
        rub_provider = RubGatewayStub()
        inv = await rub_provider.create_invoice(
            transaction_id=tx.id,
            amount_rub=to_pay_rub,
            description=description,
            user_telegram_id=db_user.telegram_id,
        )
        tx.external_id = inv.external_id
        tx.payment_url = inv.payment_url
        await session.commit()

        if callback.message and inv.payment_url:
            await callback.message.edit_text(
                f"💳 <b>Оплата картой РФ / СБП (Заказ #{tx.id})</b>\n\n"
                f"• Тариф: <b>{plan.name}</b>\n"
                f"• Сумма к оплате: <b>{to_pay_rub} ₽</b>\n\n"
                f"Перейдите по ссылке для оплаты. Подписка активируется автоматически после подтверждения шлюзом.",
                reply_markup=invoice_check_kb(tx.id, inv.payment_url),
                parse_mode="HTML",
            )
        await callback.answer()


@router.callback_query(F.data.startswith("pay:check:"))
async def cb_pay_check(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
) -> None:
    tx_id = int((callback.data or "").split(":")[2])
    res = await session.execute(select(Transaction).where(Transaction.id == tx_id))
    tx = res.scalar_one_or_none()
    if tx is None:
        await callback.answer("Счёт не найден.", show_alert=True)
        return

    if tx.status == TransactionStatus.COMPLETED:
        sub = tx.user.subscription
        if callback.message:
            await callback.message.edit_text(
                "✅ <b>Счёт уже оплачен и подписка активна!</b>\n\n"
                + format_subscription_card(sub),
                reply_markup=subscription_card_kb(sub),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        await callback.answer("Оплата подтверждена!")
        return

    if tx.status == TransactionStatus.PAID_PENDING_PROVISION:
        vpn_service = VPNService(session)
        ok, sub = await vpn_service.process_successful_payment(tx, bot=bot)
        if ok and sub and callback.message:
            await callback.message.edit_text(
                "✅ <b>Подписка успешно активирована!</b>\n\n"
                + format_subscription_card(sub),
                reply_markup=subscription_card_kb(sub),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await callback.answer("Готово!")
            return
        await callback.answer(
            "Оплата получена! Ожидаем ответа VPN-сервера (повтор через 30 сек).",
            show_alert=True,
        )
        return

    is_paid = False
    if tx.provider == PaymentProviderType.CRYPTOBOT and tx.external_id:
        is_paid = await CryptoBotProvider().check_payment_status(tx.external_id)
    elif tx.provider == PaymentProviderType.RUB_GATEWAY and tx.external_id:
        is_paid = await RubGatewayStub().check_payment_status(tx.external_id)

    if not is_paid:
        await callback.answer(
            "⏳ Оплата ещё не поступила. Завершите платёж и попробуйте снова.",
            show_alert=True,
        )
        return

    vpn_service = VPNService(session)
    ok, sub = await vpn_service.process_successful_payment(tx, bot=bot)
    if ok and sub and callback.message:
        await callback.message.edit_text(
            "✅ <b>Оплата получена, подписка активирована!</b>\n\n"
            + format_subscription_card(sub),
            reply_markup=subscription_card_kb(sub),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    elif callback.message:
        await callback.message.edit_text(
            "✅ <b>Оплата получена!</b> Активируем ваш доступ на сервере...",
            reply_markup=back_to_menu_kb(),
            parse_mode="HTML",
        )
    await callback.answer("Оплата подтверждена!")


@router.pre_checkout_query()
async def on_pre_checkout_query(
    pre_checkout_query: PreCheckoutQuery,
    session: AsyncSession,
) -> None:
    payload = pre_checkout_query.invoice_payload or ""
    if not payload.startswith("tx:"):
        await pre_checkout_query.answer(ok=False, error_message="Некорректный счёт.")
        return

    tx_id = int(payload.split(":")[1])
    tx = await session.get(Transaction, tx_id)
    if tx is None or tx.status != TransactionStatus.PENDING:
        await pre_checkout_query.answer(
            ok=False, error_message="Счёт уже оплачен или отменён."
        )
        return

    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(
    message: Message,
    bot: Bot,
    session: AsyncSession,
) -> None:
    sp = message.successful_payment
    if sp is None:
        return

    payload = sp.invoice_payload or ""
    if not payload.startswith("tx:"):
        return

    tx_id = int(payload.split(":")[1])
    res = await session.execute(select(Transaction).where(Transaction.id == tx_id))
    tx = res.scalar_one_or_none()
    if tx is None:
        return

    if sp.telegram_payment_charge_id:
        tx.external_id = sp.telegram_payment_charge_id

    vpn_service = VPNService(session)
    ok, sub = await vpn_service.process_successful_payment(tx, bot=bot)
    if ok and sub:
        await message.answer(
            "🎉 <b>Оплата через Telegram Stars прошла успешно!</b>\n\n"
            + format_subscription_card(sub),
            reply_markup=subscription_card_kb(sub),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    else:
        await message.answer(
            "✅ <b>Оплата получена!</b> Активируем ваш ключ на VPN-сервере, "
            "мы пришлём уведомление в течение пары минут.",
            reply_markup=back_to_menu_kb(),
            parse_mode="HTML",
        )
