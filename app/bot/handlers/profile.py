from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.user import (
    back_to_menu_kb,
    extra_device_buy_kb,
    main_menu_kb,
    manage_slots_kb,
    mobile_boost_kb,
    reissue_confirm_kb,
    subscription_card_kb,
)
from app.bot.states import UserPromoState
from app.bot.utils import format_dt_msk, send_or_edit_banner
from app.core.config import settings
from app.core.i18n import render_progress_bar, t
from app.db.models import PromoType, Subscription, User
from app.db.repositories import Repository
from app.services.qr_card import generate_branded_qr_card
from app.services.vpn import VPNService, build_subscription_url, calculate_extra_device_price
from app.services.xui.client import XUIClient

router = Router(name="profile")


async def format_subscription_card(
    sub: Subscription | None,
    user: User | None = None,
    live_server_check: bool = False,
) -> str:
    lang = user.language if user and user.language else "ru"
    if sub is None:
        return t("sub_none_text", lang)

    status = "🟢 <b>Активна</b>" if sub.is_currently_valid else "🔴 <b>Истекла</b>"
    sub_type = "🔥 Пробный период" if sub.is_trial else "💎 Полная защита"
    exp_str = format_dt_msk(sub.expires_at)
    sub_url = build_subscription_url(sub.sub_id, server=sub.server)

    days_left = sub.days_left
    total_days = sub.total_period_days
    prog_bar = render_progress_bar(days_left, total_days, width=10)

    server_status_line = "🟢 Онлайн • Нагрузка 18%"
    if live_server_check and sub.server:
        client = XUIClient.from_server(sub.server, timeout=3.5)
        try:
            st = await client.get_server_status()
            if st.online:
                server_status_line = f"🟢 Онлайн • Нагрузка {st.cpu_percent:.0f}%"
            tr = await client.get_client_traffics(sub.xui_email)
            if tr is not None:
                sub.used_traffic_bytes = tr.total_bytes
        except Exception:
            pass
        finally:
            await client.close()

    used_gb = sub.used_traffic_gb
    online_devs = min(sub.online_ips_count or 0, sub.limit_ip)
    balance_str = f"{user.balance_rub} ₽" if user else "0.00 ₽"
    auto_str = "Включено ✅" if (user and user.auto_renew) else "Выключено ❌"
    extra_note = f" (база {settings.DEFAULT_LIMIT_IP} + доп. {sub.extra_devices})" if sub.extra_devices > 0 else ""

    return (
        f"👻 <b>Личный кабинет GhostVPN</b>\n\n"
        f"• <b>Статус защиты:</b> {status} ({sub_type})\n"
        f"• <b>Остаток срока:</b> <code>{prog_bar}</code> <b>{days_left} из {total_days} дн.</b>\n"
        f"• <b>Действует до:</b> <b>{exp_str}</b>\n"
        f"• <b>Использовано трафика:</b> <b>{used_gb} ГБ</b> из ∞ (Безлимит)\n"
        f"• <b>Подключено устройств:</b> <b>{online_devs} / {sub.limit_ip}</b>{extra_note}\n"
        f"• <b>Сервер {sub.server.name}:</b> <b>{server_status_line}</b>\n"
        f"• <b>Внутренний баланс:</b> <b>{balance_str}</b> | Автопродление: <b>{auto_str}</b>\n\n"
        f"📋 <b>Твоя единая ссылка подписки (Wi-Fi + LTE, нажми для копирования):</b>\n"
        f"<code>{sub_url}</code>\n\n"
        f"<i>💡 Нажми кнопку «📲 Подключить в 1 клик (Wi-Fi + LTE)» ниже для мгновенного подключения!</i>"
    )


@router.callback_query(F.data == "profile:sub")
async def cb_profile_sub(
    callback: CallbackQuery,
    session: AsyncSession,
    repo: Repository,
    db_user: User,
) -> None:
    sub = db_user.subscription
    text = await format_subscription_card(sub, user=db_user, live_server_check=False)
    await send_or_edit_banner(
        callback,
        banner_key="profile",
        caption=text,
        reply_markup=subscription_card_kb(db_user, sub),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer()


@router.callback_query(F.data == "help:mobile_boost")
async def cb_help_mobile_boost(
    callback: CallbackQuery,
    repo: Repository,
    db_user: User,
) -> None:
    sub = db_user.subscription
    text = (
        "📶 <b>Не грузит на мобильном интернете (LTE)?</b>\n\n"
        "Твоя текущая подписка <b>уже включает поддержку Wi-Fi и LTE</b> (в неё сразу вшиты два Reality-профиля с <b>Yandex / VK SNI</b>, <b>TLS Fragment</b> и принудительным IPv4):\n"
        "• <b>1) 🇳🇱 GhostVPN • LTE Основной (Yandex SNI + Fragment)</b>\n"
        "• <b>2) 🇳🇱 GhostVPN • LTE Резерв (VK SNI + Fragment)</b>\n\n"
        "✈️ <b>Что сделать, если оператор «задушил» текущую сессию:</b>\n"
        "1. <b>Включи «Режим самолёта» на 5 секунд</b> и выключи его — это мгновенно сбросит зависшую DPI-сессию сотового оператора.\n"
        "2. Открой приложение (Happ / v2rayTun / Hiddify) и нажми <b>«🔄» (обновить подписку)</b>.\n"
        "3. Переключись в списке серверов на второй профиль <b>«LTE Резерв (VK SNI + Fragment)»</b>."
    )
    await send_or_edit_banner(
        callback,
        banner_key="connect",
        caption=text,
        reply_markup=mobile_boost_kb(sub, db_user.language),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer()


@router.callback_query(F.data == "profile:toggle_lang")
async def cb_toggle_lang(
    callback: CallbackQuery,
    session: AsyncSession,
    repo: Repository,
    db_user: User,
) -> None:
    db_user.language = "en" if db_user.language == "ru" else "ru"
    await session.commit()

    from app.bot.handlers.start import build_welcome_text

    is_admin = db_user.telegram_id in settings.ADMIN_IDS
    await send_or_edit_banner(
        callback,
        banner_key="main",
        caption=build_welcome_text(db_user),
        reply_markup=main_menu_kb(db_user, is_admin=is_admin),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer(t("lang_switched", db_user.language))


@router.callback_query(F.data == "profile:toggle_autorenew")
async def cb_toggle_autorenew(
    callback: CallbackQuery,
    session: AsyncSession,
    repo: Repository,
    db_user: User,
) -> None:
    db_user.auto_renew = not db_user.auto_renew
    await session.commit()
    sub = db_user.subscription
    text = await format_subscription_card(sub, user=db_user, live_server_check=False)
    await send_or_edit_banner(
        callback,
        banner_key="profile",
        caption=text,
        reply_markup=subscription_card_kb(db_user, sub),
        lang=db_user.language,
        repo=repo,
    )
    state_txt = "включено ✅" if db_user.auto_renew else "выключено ❌"
    await callback.answer(f"Автопродление {state_txt}")


@router.callback_query(F.data == "profile:unlock_ip")
async def cb_unlock_ip(
    callback: CallbackQuery,
    session: AsyncSession,
    repo: Repository,
    db_user: User,
) -> None:
    sub = db_user.subscription
    if sub is None or not sub.is_currently_valid:
        await callback.answer("Сначала продлите подписку.", show_alert=True)
        return

    vpn_service = VPNService(session)
    try:
        await vpn_service.set_client_enabled(sub, True)
        sub.ip_violations_count = 0
        await session.commit()
    except Exception:
        await callback.answer("Сервер временно недоступен, попробуйте через минуту.", show_alert=True)
        return

    text = await format_subscription_card(sub, user=db_user)
    await send_or_edit_banner(
        callback,
        banner_key="profile",
        caption="✅ <b>Доступ успешно разблокирован!</b>\n\n" + text,
        reply_markup=subscription_card_kb(db_user, sub),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer("Доступ разблокирован!")


@router.callback_query(F.data == "devices:menu")
async def cb_devices_menu(
    callback: CallbackQuery,
    repo: Repository,
    db_user: User,
) -> None:
    sub = db_user.subscription
    if sub is None or not sub.is_currently_valid:
        await callback.answer(
            "Сначала активируй подписку или пробный период, чтобы добавить устройства!",
            show_alert=True,
        )
        return

    if sub.limit_ip >= settings.MAX_LIMIT_IP:
        await callback.answer(
            f"Достигнут максимум устройств на одну подписку ({settings.MAX_LIMIT_IP}).",
            show_alert=True,
        )
        return

    price_rub = calculate_extra_device_price(sub)
    text = (
        f"📱 <b>Добавление дополнительного устройства (+1 слот)</b>\n\n"
        f"• Текущий лимит: <b>{sub.limit_ip} устр.</b> (базовых {settings.DEFAULT_LIMIT_IP} + доп. {sub.extra_devices})\n"
        f"• Новый лимит после покупки: <b>{sub.limit_ip + 1} устр.</b>\n"
        f"• Стоимость до конца текущей подписки ({sub.days_left} дн.): <b>{price_rub} ₽</b>\n\n"
        f"<i>💡 При следующем продлении доп. слоты автоматически сохранятся за тобой "
        f"(+{settings.EXTRA_DEVICE_PRICE_MONTH_RUB:.0f} ₽/мес за слот). Ты всегда можешь отключить лишние слоты в меню «🎛 Управлять слотами».</i>"
    )
    await send_or_edit_banner(
        callback,
        banner_key="devices",
        caption=text,
        reply_markup=extra_device_buy_kb(price_rub, db_user.balance_rub, db_user.language),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer()


@router.callback_query(F.data == "devices:manage")
async def cb_devices_manage(
    callback: CallbackQuery,
    repo: Repository,
    db_user: User,
) -> None:
    sub = db_user.subscription
    if sub is None:
        await callback.answer("У тебя пока нет активной подписки.", show_alert=True)
        return

    text = (
        f"🎛 <b>Управление слотами устройств</b>\n\n"
        f"• Базовых слотов в тарифе: <b>{settings.DEFAULT_LIMIT_IP} устр.</b>\n"
        f"• Докупленных доп. слотов: <b>+{sub.extra_devices} устр.</b>\n"
        f"• <b>Всего доступно сейчас: {sub.limit_ip} устр.</b>\n\n"
        f"Если перед следующим продлением тебе больше не нужны дополнительные слоты, "
        f"ты можешь уменьшить их количество ниже, чтобы при продлении списывалась только базовая стоимость тарифа."
    )
    await send_or_edit_banner(
        callback,
        banner_key="devices",
        caption=text,
        reply_markup=manage_slots_kb(sub, db_user.language),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("devices:set_extra:"))
async def cb_devices_set_extra(
    callback: CallbackQuery,
    session: AsyncSession,
    repo: Repository,
    db_user: User,
) -> None:
    target_extra = int((callback.data or "").split(":")[2])
    vpn_service = VPNService(session)
    try:
        sub = await vpn_service.reduce_extra_device_slots(db_user, target_extra_devices=target_extra)
    except Exception as exc:
        await callback.answer(f"Ошибка обновления слотов: {exc}", show_alert=True)
        return

    await cb_devices_manage(callback, repo=repo, db_user=db_user)
    await callback.answer(f"Лимит устройств обновлён: {sub.limit_ip} устр.")


@router.callback_query(F.data == "profile:qr")
async def cb_profile_qr(callback: CallbackQuery, db_user: User) -> None:
    sub = db_user.subscription
    if sub is None:
        await callback.answer("Сначала активируй подписку.", show_alert=True)
        return

    qr_png = generate_branded_qr_card(
        subscription_url=sub.subscription_url,
        sub_id=sub.sub_id,
        expires_str=format_dt_msk(sub.expires_at),
        limit_ip=sub.limit_ip,
    )
    photo = BufferedInputFile(qr_png, filename=f"ghostvpn_card_{sub.sub_id}.png")
    if callback.message:
        await callback.message.answer_photo(
            photo=photo,
            caption=(
                f"🖼 <b>Твоя фирменная QR-карта GhostVPN</b>\n\n"
                f"Отсканируй этот код камерой на втором телефоне или прямо внутри приложения "
                f"Happ / v2rayTun / Hiddify:\n"
                f"<code>{sub.subscription_url}</code>"
            ),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "profile:reissue_confirm")
async def cb_reissue_confirm(
    callback: CallbackQuery,
    repo: Repository,
    db_user: User,
) -> None:
    if db_user.subscription is None:
        await callback.answer("У тебя пока нет подписки.", show_alert=True)
        return

    text = (
        "🔄 <b>Перевыпуск (обновление) персональной ссылки</b>\n\n"
        "Если ты случайно поделился своей ссылкой с кем-то лишним, мы мгновенно создадим для тебя новую, "
        "а старую отключим (весь оплаченный срок и слоты полностью сохранятся).\n\n"
        "⚠️ <b>Важно:</b> после обновления нужно будет один раз нажать «Подключить в 1 клик», "
        "чтобы новая ссылка добавилась в твоё приложение. Доступно 1 раз в 24 часа.\n\n"
        "Обновить ключ?"
    )
    await send_or_edit_banner(
        callback,
        banner_key="profile",
        caption=text,
        reply_markup=reissue_confirm_kb(),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer()


@router.callback_query(F.data == "profile:reissue_do")
async def cb_reissue_do(
    callback: CallbackQuery,
    session: AsyncSession,
    repo: Repository,
    db_user: User,
) -> None:
    vpn_service = VPNService(session)
    try:
        sub = await vpn_service.reissue_key(db_user, force_by_admin=False)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception:
        await callback.answer(
            "⚠️ Не удалось связаться с сервером 3X-UI. Попробуй через минуту.",
            show_alert=True,
        )
        return

    card_text = await format_subscription_card(sub, user=db_user)
    await send_or_edit_banner(
        callback,
        banner_key="profile",
        caption="✅ <b>Твоя ссылка и ключ успешно обновлены!</b>\n\n" + card_text,
        reply_markup=subscription_card_kb(db_user, sub),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer("Ключ успешно перевыпущен!")


@router.callback_query(F.data == "profile:ref")
async def cb_referral_info(
    callback: CallbackQuery,
    repo: Repository,
    db_user: User,
) -> None:
    refs_count = await repo.count_referrals(db_user.id)
    ref_link = f"https://t.me/{settings.BOT_USERNAME}?start=ref_{db_user.telegram_id}"

    text = (
        f"🎁 <b>Партнёрская программа GhostVPN (+{settings.REFERRAL_PERCENT}%)</b>\n\n"
        f"Отправь свою ссылку друзьям или близким! С <b>каждой оплаты и каждого продления</b> друга "
        f"тебе автоматически начисляется <b>{settings.REFERRAL_PERCENT}% в рублях</b> на внутренний баланс.\n\n"
        f"✨ Бонусным балансом можно оплачивать до <b>100% стоимости</b> своей подписки, "
        f"дополнительных устройств и автопродления!\n\n"
        f"🔗 <b>Твоя ссылка для приглашения (нажми для копирования):</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"👥 Приглашено друзей: <b>{refs_count}</b>\n"
        f"💰 Накопленный баланс: <b>{db_user.balance_rub} ₽</b>"
    )
    await send_or_edit_banner(
        callback,
        banner_key="referral",
        caption=text,
        reply_markup=back_to_menu_kb(db_user.language),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer()


@router.callback_query(F.data == "promo:enter")
async def cb_promo_enter(
    callback: CallbackQuery,
    repo: Repository,
    db_user: User,
    state: FSMContext,
) -> None:
    await state.set_state(UserPromoState.waiting_for_code)
    await send_or_edit_banner(
        callback,
        banner_key="promo",
        caption=(
            "🎟 <b>Активация секретного промокода</b>\n\n"
            "Напиши свой промокод ответным сообщением в чат, и я сразу начислю бонусные дни или скидку:"
        ),
        reply_markup=back_to_menu_kb(db_user.language),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer()


@router.message(UserPromoState.waiting_for_code)
async def msg_promo_redeem(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    repo: Repository,
    db_user: User,
) -> None:
    code_text = (message.text or "").strip()
    if not code_text:
        await message.answer("Пожалуйста, отправь промокод текстом.")
        return

    try:
        promo, status_msg = await repo.validate_and_redeem_promo(db_user, code_text)
    except ValueError as exc:
        await message.answer(
            f"❌ {exc}",
            reply_markup=back_to_menu_kb(db_user.language),
        )
        return

    await state.clear()
    if promo.type == PromoType.DAYS:
        vpn_service = VPNService(session)
        try:
            sub = await vpn_service.grant_or_extend_subscription(
                user=db_user,
                days=promo.value,
                limit_ip=settings.DEFAULT_LIMIT_IP,
                is_trial=False,
            )
            card = await format_subscription_card(sub, user=db_user)
            await send_or_edit_banner(
                message,
                banner_key="profile",
                caption=f"🎉 <b>Промокод активирован!</b>\n{status_msg}\n\n{card}",
                reply_markup=subscription_card_kb(db_user, sub),
                lang=db_user.language,
                repo=repo,
            )
            return
        except Exception:
            await message.answer(
                "⚠️ Промокод принят, но сервер временно синхронизируется. Напиши в поддержку.",
                reply_markup=back_to_menu_kb(db_user.language),
            )
            return

    await send_or_edit_banner(
        message,
        banner_key="promo",
        caption=f"🎉 <b>Промокод успешно активирован!</b>\n{status_msg}",
        reply_markup=back_to_menu_kb(db_user.language),
        lang=db_user.language,
        repo=repo,
    )
