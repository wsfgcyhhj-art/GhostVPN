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
from app.bot.utils import send_or_edit_banner
from app.core.config import settings
from app.db.models import (
    PaymentProviderType,
    Transaction,
    TransactionStatus,
    TransactionType,
    User,
)
from app.db.repositories import Repository
from app.services.payments.cryptobot import CryptoBotError, CryptoBotProvider
from app.services.payments.rub_gateway import PlategaProvider
from app.services.payments.stars import TelegramStarsProvider
from app.services.vpn import (
    VPNService,
    calculate_extra_device_price,
    calculate_plan_renewal_price_with_slots,
)

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
    extra_devs = db_user.subscription.extra_devices if db_user.subscription else 0

    discount_line = (
        f"\n🎟 <b>Активна персональная скидка -{discount}% на покупку!</b>\n"
        if discount > 0
        else ""
    )
    slots_line = (
        f"\n📱 <i>У тебя подключено +{extra_devs} доп. слотов (итого {settings.DEFAULT_LIMIT_IP + extra_devs} устр.) — "
        f"они уже учтены в цене продления. Изменить можно по кнопке «🎛 Управлять слотами».</i>\n"
        if extra_devs > 0
        else ""
    )
    text = (
        f"💳 <b>Выбери свой тариф GhostVPN</b>\n"
        f"{discount_line}{slots_line}\n"
        f"✨ <b>В каждый тариф включено:</b>\n"
        f"• 🇳🇱 Скоростной канал до 500 Мбит/с (Нидерланды, VLESS Reality)\n"
        f"• 🛡 Обход глушилок на мобильном интернете (TLS Fragment)\n"
        f"• ♾ Полный безлимит по трафику и скорости\n"
        f"• 🔄 Продление без смены ссылки и настроек в приложении"
    )
    await send_or_edit_banner(
        callback,
        banner_key="tariffs",
        caption=text,
        reply_markup=plans_list_kb(
            plans,
            discount_percent=discount,
            extra_devices=extra_devs,
            lang=db_user.language,
        ),
        lang=db_user.language,
        repo=repo,
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

    extra_devs = db_user.subscription.extra_devices if db_user.subscription else 0
    full_plan_price = calculate_plan_renewal_price_with_slots(plan, extra_devices=extra_devs)

    to_pay_rub, balance_used_rub = calculate_checkout_amounts(
        plan_price_rub=full_plan_price,
        discount_percent=db_user.pending_discount_percent,
        user_balance_rub=db_user.balance_rub,
        use_balance=use_balance,
    )
    price_after_promo, _ = calculate_checkout_amounts(
        plan_price_rub=full_plan_price,
        discount_percent=db_user.pending_discount_percent,
        user_balance_rub=db_user.balance_rub,
        use_balance=False,
    )

    text = (
        f"📦 <b>Оформление подписки: {plan.name} ({plan.days} дн.)</b>\n\n"
        f"• Тариф ({settings.DEFAULT_LIMIT_IP} устр.): <b>{plan.price_rub} ₽</b>\n"
    )
    if extra_devs > 0:
        extra_diff = full_plan_price - plan.price_rub
        text += f"• Доп. слоты (+{extra_devs} устр.): <b>+{extra_diff} ₽</b>\n"
    if db_user.pending_discount_percent > 0:
        text += f"• Скидка по промокоду: <b>-{db_user.pending_discount_percent}%</b>\n"
    if use_balance and balance_used_rub > Decimal("0.00"):
        text += f"• Списание с бонусного баланса: <b>-{balance_used_rub} ₽</b>\n"

    text += f"\n💵 <b>Итого к оплате: {to_pay_rub} ₽</b>\nВыбери удобный способ оплаты:"

    await send_or_edit_banner(
        callback,
        banner_key="tariffs",
        caption=text,
        reply_markup=payment_methods_kb(
            plan_id=plan.id,
            final_price_rub=price_after_promo,
            user_balance_rub=db_user.balance_rub,
            use_balance=use_balance,
        ),
        lang=db_user.language,
        repo=repo,
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

    extra_devs = db_user.subscription.extra_devices if db_user.subscription else 0
    full_plan_price = calculate_plan_renewal_price_with_slots(plan, extra_devices=extra_devs)

    _, balance_needed = calculate_checkout_amounts(
        plan_price_rub=full_plan_price,
        discount_percent=db_user.pending_discount_percent,
        user_balance_rub=db_user.balance_rub,
        use_balance=True,
    )
    price_after_promo, _ = calculate_checkout_amounts(
        plan_price_rub=full_plan_price,
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
        tx_type=TransactionType.SUBSCRIPTION,
        provider=PaymentProviderType.BALANCE,
        currency="RUB",
        amount_currency=balance_needed,
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
        card = await format_subscription_card(sub, user=db_user)
        await send_or_edit_banner(
            callback,
            banner_key="profile",
            caption=f"✅ <b>Тариф «{plan.name}» успешно оплачен с баланса!</b>\n\n{card}",
            reply_markup=subscription_card_kb(db_user, sub),
            lang=db_user.language,
            repo=repo,
        )
    else:
        await send_or_edit_banner(
            callback,
            banner_key="profile",
            caption=(
                "✅ <b>Оплата с баланса зафиксирована!</b>\n"
                "Сервер синхронизирует твой ключ, доступ активируется автоматически в течение пары минут."
            ),
            reply_markup=back_to_menu_kb(db_user.language),
            lang=db_user.language,
            repo=repo,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("devices:pay:"))
async def cb_devices_pay(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    repo: Repository,
    db_user: User,
) -> None:
    sub = db_user.subscription
    if sub is None or not sub.is_currently_valid:
        await callback.answer("Сначала активируй подписку.", show_alert=True)
        return
    if sub.limit_ip >= settings.MAX_LIMIT_IP:
        await callback.answer("Достигнут максимум слотов.", show_alert=True)
        return

    recent_pending = await repo.count_recent_pending_transactions(db_user.id, minutes=10)
    if recent_pending >= 5:
        await callback.answer("Подожди пару минут перед созданием нового счёта.", show_alert=True)
        return

    provider_str = (callback.data or "").split(":")[2]
    price_rub = calculate_extra_device_price(sub)
    description = f"GhostVPN: +1 устройство (до {sub.limit_ip + 1} устр.)"

    if provider_str == "BALANCE":
        if db_user.balance_rub < price_rub:
            await callback.answer("Недостаточно средств на балансе.", show_alert=True)
            return
        tx = Transaction(
            user_id=db_user.id,
            plan_id=sub.current_plan_id,
            tx_type=TransactionType.EXTRA_DEVICE,
            provider=PaymentProviderType.BALANCE,
            currency="RUB",
            amount_currency=price_rub,
            amount_rub=Decimal("0.00"),
            balance_used_rub=price_rub,
            extra_devices_qty=1,
            status=TransactionStatus.PENDING,
        )
        session.add(tx)
        await session.commit()
        await session.refresh(tx)

        vpn_service = VPNService(session)
        ok, updated_sub = await vpn_service.process_successful_payment(tx, bot=bot)
        if ok and updated_sub:
            card = await format_subscription_card(updated_sub, user=db_user)
            await send_or_edit_banner(
                callback,
                banner_key="profile",
                caption=f"🎉 <b>+1 устройство успешно добавлено! Теперь доступно {updated_sub.limit_ip} устр.</b>\n\n{card}",
                reply_markup=subscription_card_kb(db_user, updated_sub),
                lang=db_user.language,
                repo=repo,
            )
        await callback.answer("Слот добавлен!")
        return

    provider_enum = PaymentProviderType(provider_str)
    tx = Transaction(
        user_id=db_user.id,
        plan_id=sub.current_plan_id,
        tx_type=TransactionType.EXTRA_DEVICE,
        provider=provider_enum,
        currency="XTR" if provider_enum == PaymentProviderType.STARS else (
            "USDT" if provider_enum == PaymentProviderType.CRYPTOBOT else "RUB"
        ),
        amount_currency=price_rub,
        amount_rub=price_rub,
        balance_used_rub=Decimal("0.00"),
        extra_devices_qty=1,
        status=TransactionStatus.PENDING,
    )
    session.add(tx)
    await session.commit()
    await session.refresh(tx)

    if provider_enum == PaymentProviderType.STARS:
        stars_provider = TelegramStarsProvider()
        inv = await stars_provider.create_invoice(
            transaction_id=tx.id,
            amount_rub=price_rub,
            description=description,
            user_telegram_id=db_user.telegram_id,
        )
        tx.external_id = inv.external_id
        tx.amount_currency = Decimal(str(inv.stars_amount or 1))
        await session.commit()

        await bot.send_invoice(
            chat_id=db_user.telegram_id,
            title="GhostVPN — +1 устройство",
            description=description,
            payload=f"tx:{tx.id}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="+1 устройство", amount=inv.stars_amount or 1)],
        )
        await callback.answer("Счёт в Telegram Stars выставлен!")
        return

    if provider_enum == PaymentProviderType.CRYPTOBOT:
        try:
            inv = await CryptoBotProvider().create_invoice(
                transaction_id=tx.id,
                amount_rub=price_rub,
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
        if inv.payment_url:
            await send_or_edit_banner(
                callback,
                banner_key="devices",
                caption=f"💎 <b>Оплата +1 слота устройства ({price_rub} ₽)</b>\nПерейди по ссылке для оплаты:",
                reply_markup=invoice_check_kb(tx.id, inv.payment_url),
                lang=db_user.language,
                repo=repo,
            )
        await callback.answer()
        return

    inv = await PlategaProvider().create_invoice(
        transaction_id=tx.id,
        amount_rub=price_rub,
        description=description,
        user_telegram_id=db_user.telegram_id,
    )
    tx.external_id = inv.external_id
    tx.payment_url = inv.payment_url
    await session.commit()
    if inv.payment_url:
        await send_or_edit_banner(
            callback,
            banner_key="devices",
            caption=f"💳 <b>Добавление +1 устройства ({price_rub} ₽)</b>\nПерейди по ссылке для оплаты СБП / картой РФ:",
            reply_markup=invoice_check_kb(tx.id, inv.payment_url),
            lang=db_user.language,
            repo=repo,
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
    recent_pending = await repo.count_recent_pending_transactions(db_user.id, minutes=10)
    if recent_pending >= 5:
        await callback.answer(
            "⏳ У тебя уже создано несколько неоплаченных счетов. Оплати любой из них или подожди пару минут.",
            show_alert=True,
        )
        return

    parts = (callback.data or "").split(":")
    provider_str = parts[2]
    plan_id = int(parts[3])
    use_balance = len(parts) > 4 and parts[4] == "1"

    plan = await repo.get_plan_by_id(plan_id)
    if plan is None or not plan.is_active:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    extra_devs = db_user.subscription.extra_devices if db_user.subscription else 0
    full_plan_price = calculate_plan_renewal_price_with_slots(plan, extra_devices=extra_devs)

    to_pay_rub, balance_used_rub = calculate_checkout_amounts(
        plan_price_rub=full_plan_price,
        discount_percent=db_user.pending_discount_percent,
        user_balance_rub=db_user.balance_rub,
        use_balance=use_balance,
    )
    if to_pay_rub <= Decimal("0.00"):
        await callback.answer("Используй кнопку полной оплаты с баланса.", show_alert=True)
        return

    provider_enum = PaymentProviderType(provider_str)
    currency_code = (
        "XTR"
        if provider_enum == PaymentProviderType.STARS
        else ("USDT" if provider_enum == PaymentProviderType.CRYPTOBOT else "RUB")
    )
    tx = Transaction(
        user_id=db_user.id,
        plan_id=plan.id,
        tx_type=TransactionType.SUBSCRIPTION,
        provider=provider_enum,
        currency=currency_code,
        amount_currency=to_pay_rub,
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
        tx.amount_currency = Decimal(str(inv.stars_amount or 1))
        await session.commit()

        await bot.send_invoice(
            chat_id=db_user.telegram_id,
            title=f"GhostVPN — {plan.name}",
            description=description,
            payload=f"tx:{tx.id}",
            provider_token="",
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

        if inv.payment_url:
            await send_or_edit_banner(
                callback,
                banner_key="tariffs",
                caption=(
                    f"💎 <b>Счёт CryptoBot #{inv.external_id}</b>\n\n"
                    f"• Тариф: <b>{plan.name}</b>\n"
                    f"• Сумма к оплате: <b>{to_pay_rub} ₽</b>\n\n"
                    f"Нажми «🔗 Перейти к оплате». После оплаты подписка активируется автоматически!"
                ),
                reply_markup=invoice_check_kb(tx.id, inv.payment_url),
                lang=db_user.language,
                repo=repo,
            )
        await callback.answer()
        return

    if provider_enum == PaymentProviderType.RUB_GATEWAY:
        rub_provider = PlategaProvider()
        inv = await rub_provider.create_invoice(
            transaction_id=tx.id,
            amount_rub=to_pay_rub,
            description=description,
            user_telegram_id=db_user.telegram_id,
        )
        tx.external_id = inv.external_id
        tx.payment_url = inv.payment_url
        await session.commit()

        if inv.payment_url:
            await send_or_edit_banner(
                callback,
                banner_key="tariffs",
                caption=(
                    f"💳 <b>Оплата СБП / Картой РФ (Заказ #{tx.id})</b>\n\n"
                    f"• Тариф: <b>{plan.name} ({plan.days} дн.)</b>\n"
                    f"• Сумма к оплате: <b>{to_pay_rub} ₽</b>\n\n"
                    f"Нажми «🔗 Перейти к оплате». Сразу после подтверждения твой ключ активируется автоматически!"
                ),
                reply_markup=invoice_check_kb(tx.id, inv.payment_url),
                lang=db_user.language,
                repo=repo,
            )
        await callback.answer()


@router.callback_query(F.data.startswith("pay:check:"))
async def cb_pay_check(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    repo: Repository,
    db_user: User,
) -> None:
    tx_id = int((callback.data or "").split(":")[2])
    res = await session.execute(select(Transaction).where(Transaction.id == tx_id))
    tx = res.scalar_one_or_none()
    if tx is None:
        await callback.answer("Счёт не найден.", show_alert=True)
        return

    if tx.status == TransactionStatus.COMPLETED:
        sub = tx.user.subscription
        card = await format_subscription_card(sub, user=db_user)
        await send_or_edit_banner(
            callback,
            banner_key="profile",
            caption=f"✅ <b>Счёт уже оплачен и подписка активна!</b>\n\n{card}",
            reply_markup=subscription_card_kb(db_user, sub),
            lang=db_user.language,
            repo=repo,
        )
        await callback.answer("Оплата подтверждена!")
        return

    if tx.status == TransactionStatus.PAID_PENDING_PROVISION:
        vpn_service = VPNService(session)
        ok, sub = await vpn_service.process_successful_payment(tx, bot=bot)
        if ok and sub:
            card = await format_subscription_card(sub, user=db_user)
            await send_or_edit_banner(
                callback,
                banner_key="profile",
                caption=f"✅ <b>Подписка успешно активирована!</b>\n\n{card}",
                reply_markup=subscription_card_kb(db_user, sub),
                lang=db_user.language,
                repo=repo,
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
        is_paid = await PlategaProvider().check_payment_status(tx.external_id)

    if not is_paid:
        await callback.answer(
            "⏳ Оплата ещё не поступила. Заверши платёж по ссылке и нажми кнопку снова!",
            show_alert=True,
        )
        return

    vpn_service = VPNService(session)
    ok, sub = await vpn_service.process_successful_payment(tx, bot=bot)
    if ok and sub:
        card = await format_subscription_card(sub, user=db_user)
        await send_or_edit_banner(
            callback,
            banner_key="profile",
            caption=f"✅ <b>Оплата получена, защита активирована!</b>\n\n{card}",
            reply_markup=subscription_card_kb(db_user, sub),
            lang=db_user.language,
            repo=repo,
        )
    else:
        await send_or_edit_banner(
            callback,
            banner_key="profile",
            caption="✅ <b>Оплата получена!</b> Синхронизируем твой ключ на сервере...",
            reply_markup=back_to_menu_kb(db_user.language),
            lang=db_user.language,
            repo=repo,
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
    repo: Repository,
    db_user: User,
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
        card = await format_subscription_card(sub, user=db_user)
        await send_or_edit_banner(
            message,
            banner_key="profile",
            caption=f"🎉 <b>Оплата через Telegram Stars прошла успешно!</b>\n\n{card}",
            reply_markup=subscription_card_kb(db_user, sub),
            lang=db_user.language,
            repo=repo,
        )
    else:
        await message.answer(
            "✅ <b>Оплата получена!</b> Активируем твой ключ на VPN-сервере, "
            "пришлём уведомление в течение пары минут.",
            reply_markup=back_to_menu_kb(db_user.language),
            parse_mode="HTML",
        )
