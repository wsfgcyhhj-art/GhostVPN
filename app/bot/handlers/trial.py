from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.profile import format_subscription_card
from app.bot.keyboards.user import channel_check_kb, subscription_card_kb
from app.core.config import settings
from app.core.logging import logger, send_admin_alert
from app.db.models import User
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
async def cb_trial_activate(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    db_user: User,
) -> None:
    if db_user.has_used_trial:
        await callback.answer("Вы уже использовали пробный период ранее.", show_alert=True)
        return

    subscribed = await is_user_subscribed(bot, db_user.telegram_id)
    if not subscribed:
        if callback.data == "trial:verify":
            await callback.answer(
                "❌ Подписка не обнаружена! Подпишитесь на канал и нажмите кнопку ещё раз.",
                show_alert=True,
            )
            return

        text = (
            f"🎁 <b>Бесплатный тест на {settings.TRIAL_DAYS} дня</b>\n\n"
            f"Для защиты от ботов и получения новостей о работе серверов "
            f"подпишитесь на наш официальный Telegram-канал, а затем нажмите "
            f"<b>«✅ Я подписался (Проверить)»</b>."
        )
        if callback.message:
            await callback.message.edit_text(
                text, reply_markup=channel_check_kb(), parse_mode="HTML"
            )
        await callback.answer()
        return

    vpn_service = VPNService(session)
    try:
        sub = await vpn_service.grant_or_extend_subscription(
            user=db_user,
            days=settings.TRIAL_DAYS,
            limit_ip=settings.DEFAULT_LIMIT_IP,
            is_trial=True,
        )
    except Exception as exc:
        logger.error("Ошибка выдачи триала для %s: %s", db_user.telegram_id, exc)
        await send_admin_alert(
            bot,
            f"Сбой выдачи триала для <code>{db_user.telegram_id}</code>:\n<code>{exc}</code>",
            critical=True,
        )
        await callback.answer(
            "⚠️ Сервер временно перегружен, попробуйте через минуту.",
            show_alert=True,
        )
        return

    await send_admin_alert(
        bot,
        (
            f"🎁 <b>Активирован пробный период ({settings.TRIAL_DAYS} дн.)</b>\n"
            f"👤 Пользователь: <code>{db_user.telegram_id}</code> (@{db_user.username or 'нет'})\n"
            f"🔑 Sub ID: <code>{sub.sub_id}</code>"
        ),
    )

    card_text = (
        f"🎉 <b>Пробный доступ на {settings.TRIAL_DAYS} дня успешно активирован!</b>\n\n"
        + format_subscription_card(sub)
    )
    if callback.message:
        await callback.message.edit_text(
            card_text,
            reply_markup=subscription_card_kb(sub),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    await callback.answer("Пробный период активирован!")
