from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.user import instructions_kb, main_menu_kb
from app.core.config import settings
from app.db.models import User
from app.db.repositories import Repository

router = Router(name="start")


def build_welcome_text(user: User) -> str:
    sub = user.subscription
    if sub and sub.is_currently_valid:
        status_line = "🟢 <b>Активна</b>"
    elif sub:
        status_line = "🔴 <b>Истекла</b>"
    else:
        status_line = "⚪️ <b>Нет активной подписки</b>"

    return (
        f"👻 <b>Добро пожаловать в GhostVPN!</b>\n\n"
        f"Высокоскоростной VPN нового поколения на протоколе <b>VLESS + XTLS-Vision + Reality</b>:\n"
        f"• 🇳🇱 Локация: <b>Нидерланды (до 500 Мбит/с)</b>\n"
        f"• 🛡 Полная устойчивость к блокировкам ТСПУ/DPI\n"
        f"• 📱 До <b>{settings.DEFAULT_LIMIT_IP} устройств</b> одновременно, безлимитный трафик\n\n"
        f"📊 Статус подписки: {status_line}\n"
        f"💰 Ваш бонусный баланс: <b>{user.balance_rub} ₽</b>\n\n"
        f"<i>Продолжая использование бота, вы соглашаетесь с "
        f"<a href='{settings.TERMS_URL}'>правилами сервиса</a> (запрещены торренты, спам и DDoS).</i>"
    )


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    repo: Repository,
    state: FSMContext,
) -> None:
    await state.clear()
    tg_user = message.from_user
    if tg_user is None:
        return

    ref_tg_id: int | None = None
    args = (command.args or "").strip()
    if args.startswith("ref_"):
        raw_id = args.replace("ref_", "", 1)
        if raw_id.isdigit():
            ref_tg_id = int(raw_id)

    user, _ = await repo.get_or_create_user(
        telegram_id=tg_user.id,
        username=tg_user.username,
        full_name=tg_user.full_name,
        referrer_telegram_id=ref_tg_id,
    )
    is_admin = tg_user.id in settings.ADMIN_IDS
    await message.answer(
        build_welcome_text(user),
        reply_markup=main_menu_kb(user, is_admin=is_admin),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "menu:home")
async def cb_menu_home(
    callback: CallbackQuery,
    db_user: User,
    state: FSMContext,
) -> None:
    await state.clear()
    is_admin = db_user.telegram_id in settings.ADMIN_IDS
    text = build_welcome_text(db_user)
    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=main_menu_kb(db_user, is_admin=is_admin),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            await callback.message.answer(
                text,
                reply_markup=main_menu_kb(db_user, is_admin=is_admin),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    await callback.answer()


@router.callback_query(F.data == "help:instructions")
async def cb_instructions(callback: CallbackQuery) -> None:
    text = (
        "📖 <b>Инструкция по быстрому подключению</b>\n\n"
        "Мы используем единую <b>Subscription URL</b> (ссылку на подписку). "
        "Она автоматически подгружает настройки и обновляет срок действия прямо в приложении.\n\n"
        "<b>Шаг 1.</b> Установите бесплатное приложение для вашего устройства по кнопкам ниже:\n"
        "• <b>iOS / macOS:</b> Happ или v2rayTun\n"
        "• <b>Android:</b> v2rayTun или Hiddify\n"
        "• <b>Windows:</b> Hiddify\n\n"
        "<b>Шаг 2.</b> Откройте раздел «🔑 Моя подписка» и нажмите кнопку "
        "<b>«📲 Подключить в 1 клик»</b> (или скопируйте ссылку вручную и нажмите <code>+</code> в приложении)."
    )
    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=instructions_kb(),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            await callback.message.answer(
                text,
                reply_markup=instructions_kb(),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    await callback.answer()
