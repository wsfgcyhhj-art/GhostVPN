from __future__ import annotations

import io
import qrcode
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.user import (
    back_to_menu_kb,
    reissue_confirm_kb,
    subscription_card_kb,
)
from app.bot.states import UserPromoState
from app.bot.utils import format_dt_msk
from app.core.config import settings
from app.db.models import PromoType, Subscription, User
from app.db.repositories import Repository
from app.services.vpn import VPNService

router = Router(name="profile")


def format_subscription_card(sub: Subscription | None) -> str:
    if sub is None:
        return (
            "🔑 <b>Моя подписка</b>\n\n"
            "У вас пока нет активной подписки.\n"
            "Оформите бесплатный пробный период или выберите подходящий тариф!"
        )

    status = "🟢 <b>Активна</b>" if sub.is_currently_valid else "🔴 <b>Истекла</b>"
    sub_type = "🎁 Пробный период" if sub.is_trial else "💎 Полный доступ"
    exp_str = format_dt_msk(sub.expires_at)
    sub_url = sub.subscription_url

    return (
        f"🔑 <b>Ваша VPN-подписка</b>\n\n"
        f"• Статус: {status} ({sub_type})\n"
        f"• Действует до: <b>{exp_str}</b>\n"
        f"• Лимит устройств: <b>{sub.limit_ip} устр.</b> (Безлимитный трафик)\n"
        f"• Локация: <b>{sub.server.name}</b>\n\n"
        f"📋 <b>Персональная ссылка подписки (нажмите для копирования):</b>\n"
        f"<code>{sub_url}</code>\n\n"
        f"<i>💡 При продлении подписки ссылка остаётся прежней — ничего переустанавливать не нужно!</i>"
    )


def generate_qr_bytes(data: str) -> bytes:
    """Генерирует PNG QR-код в оперативной памяти (BytesIO) без сохранения на диск."""
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@router.callback_query(F.data == "profile:sub")
async def cb_profile_sub(callback: CallbackQuery, db_user: User) -> None:
    sub = db_user.subscription
    text = format_subscription_card(sub)
    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=subscription_card_kb(sub),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            await callback.message.answer(
                text,
                reply_markup=subscription_card_kb(sub),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    await callback.answer()


@router.callback_query(F.data == "profile:qr")
async def cb_profile_qr(callback: CallbackQuery, db_user: User) -> None:
    sub = db_user.subscription
    if sub is None:
        await callback.answer("Сначала активируйте подписку.", show_alert=True)
        return

    qr_png = generate_qr_bytes(sub.subscription_url)
    photo = BufferedInputFile(qr_png, filename=f"ghostvpn_{sub.sub_id}.png")
    if callback.message:
        await callback.message.answer_photo(
            photo=photo,
            caption=(
                f"📷 <b>QR-код вашей подписки</b>\n\n"
                f"Отсканируйте его в приложении Happ / v2rayTun / Hiddify:\n"
                f"<code>{sub.subscription_url}</code>"
            ),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data == "profile:reissue_confirm")
async def cb_reissue_confirm(callback: CallbackQuery, db_user: User) -> None:
    if db_user.subscription is None:
        await callback.answer("У вас нет подписки.", show_alert=True)
        return

    text = (
        "🔄 <b>Перевыпуск (сброс) ключа доступа</b>\n\n"
        "Если ваша ссылка попала к посторонним и срабатывает защита по лимиту устройств, "
        "вы можете перевыпустить ключ.\n\n"
        "⚠️ <b>Внимание:</b> старая ссылка перестанет работать, и вам нужно будет "
        "добавить новую ссылку в своё приложение. Доступно не чаще 1 раза в 24 часа.\n\n"
        "Продолжить?"
    )
    if callback.message:
        await callback.message.edit_text(
            text, reply_markup=reissue_confirm_kb(), parse_mode="HTML"
        )
    await callback.answer()


@router.callback_query(F.data == "profile:reissue_do")
async def cb_reissue_do(
    callback: CallbackQuery,
    session: AsyncSession,
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
            "⚠️ Не удалось связаться с сервером 3X-UI. Попробуйте позже.",
            show_alert=True,
        )
        return

    if callback.message:
        await callback.message.edit_text(
            "✅ <b>Ключ и ссылка успешно обновлены!</b>\n\n" + format_subscription_card(sub),
            reply_markup=subscription_card_kb(sub),
            parse_mode="HTML",
            disable_web_page_preview=True,
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
        f"👥 <b>Реферальная программа GhostVPN</b>\n\n"
        f"Приглашайте друзей и получайте <b>{settings.REFERRAL_PERCENT}% от каждой их оплаты</b> "
        f"на свой внутренний баланс!\n"
        f"Накопленными рублями можно оплачивать до <b>100% стоимости</b> собственной подписки.\n\n"
        f"🔗 <b>Ваша персональная ссылка (нажмите для копирования):</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"📊 Приглашено друзей: <b>{refs_count}</b>\n"
        f"💰 Ваш доступный баланс: <b>{db_user.balance_rub} ₽</b>"
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=back_to_menu_kb(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    await callback.answer()


@router.callback_query(F.data == "promo:enter")
async def cb_promo_enter(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserPromoState.waiting_for_code)
    if callback.message:
        await callback.message.edit_text(
            "🎟 <b>Активация промокода</b>\n\n"
            "Отправьте промокод ответным сообщением:",
            reply_markup=back_to_menu_kb(),
            parse_mode="HTML",
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
        await message.answer("Пожалуйста, введите текстовый промокод.")
        return

    try:
        promo, status_msg = await repo.validate_and_redeem_promo(db_user, code_text)
    except ValueError as exc:
        await message.answer(
            f"❌ {exc}",
            reply_markup=back_to_menu_kb(),
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
            await message.answer(
                f"🎉 <b>Промокод активирован!</b>\n{status_msg}\n\n"
                + format_subscription_card(sub),
                reply_markup=subscription_card_kb(sub),
                parse_mode="HTML",
            )
            return
        except Exception:
            await message.answer(
                "⚠️ Промокод принят, но сервер 3X-UI временно недоступен. Обратитесь в поддержку.",
                reply_markup=back_to_menu_kb(),
            )
            return

    await message.answer(
        f"🎉 <b>Промокод активирован!</b>\n{status_msg}",
        reply_markup=back_to_menu_kb(),
        parse_mode="HTML",
    )
