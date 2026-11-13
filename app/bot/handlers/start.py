from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.user import (
    faq_menu_kb,
    instructions_kb,
    main_menu_kb,
    mobile_boost_kb,
)
from app.bot.utils import send_or_edit_banner
from app.core.config import settings
from app.core.i18n import t
from app.db.models import User
from app.db.repositories import Repository

router = Router(name="start")


def build_welcome_text(user: User) -> str:
    lang = user.language or "ru"
    sub = user.subscription
    if sub and sub.is_currently_valid:
        status_line = f"<b>{t('status_active', lang)}</b>"
    elif sub:
        status_line = f"<b>{t('status_expired', lang)}</b>"
    else:
        status_line = f"<b>{t('status_none', lang)}</b>"

    return t(
        "welcome_title",
        lang,
        limit_ip=settings.DEFAULT_LIMIT_IP,
        status_line=status_line,
        balance=user.balance_rub,
        terms_url=settings.TERMS_URL,
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
        language_code=tg_user.language_code,
    )
    is_admin = tg_user.id in settings.ADMIN_IDS
    await send_or_edit_banner(
        message,
        banner_key="main",
        caption=build_welcome_text(user),
        reply_markup=main_menu_kb(user, is_admin=is_admin),
        lang=user.language,
        repo=repo,
    )


@router.callback_query(F.data == "menu:home")
async def cb_menu_home(
    callback: CallbackQuery,
    repo: Repository,
    db_user: User,
    state: FSMContext,
) -> None:
    await state.clear()
    is_admin = db_user.telegram_id in settings.ADMIN_IDS
    await send_or_edit_banner(
        callback,
        banner_key="main",
        caption=build_welcome_text(db_user),
        reply_markup=main_menu_kb(db_user, is_admin=is_admin),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer()


@router.callback_query(F.data == "help:faq")
async def cb_help_faq(
    callback: CallbackQuery,
    repo: Repository,
    db_user: User,
) -> None:
    text = (
        "❓ <b>Центр помощи и заботы GhostVPN</b>\n\n"
        "Мы собрали готовые ответы на самые частые вопросы, чтобы у тебя всё работало с полуслова:\n\n"
        "• 📶 <b>Не открываются сайты на мобильном интернете (МТС / Билайн / МегаФон / Tele2)?</b>\n"
        "  Нажми кнопку ниже — мы включим специальный режим обхода глушилок (TLS Fragment).\n\n"
        "• 📲 <b>Как установить на iPhone, Android или ноутбук?</b>\n"
        "  Всего 2 клика: скачай приложение и нажми «Подключить в 1 клик» в разделе подписки.\n\n"
        "• 💬 Если что-то не получается — напиши нашему живому специалисту, мы поможем за пару минут!"
    )
    await send_or_edit_banner(
        callback,
        banner_key="support",
        caption=text,
        reply_markup=faq_menu_kb(db_user.language),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer()


@router.callback_query(F.data == "help:faq_billing")
async def cb_help_faq_billing(
    callback: CallbackQuery,
    repo: Repository,
    db_user: User,
) -> None:
    text = (
        "💳 <b>Вопросы по оплате, балансу и автопродлению</b>\n\n"
        "1. <b>Как работает тест за 1 ₽ / 10 ⭐️?</b>\n"
        "   Это символическая проверка от ботов. Сразу после оплаты тебе открывается полный безлимитный доступ на 3 дня.\n\n"
        "2. <b>Нужно ли менять ссылку в приложении при продлении?</b>\n"
        "   Нет! Твоя ссылка подписки остаётся навсегда. При оплате дни просто прибавляются автоматически.\n\n"
        "3. <b>Как работает Автопродление?</b>\n"
        "   За 12 часов до конца срока бот проверит твой бонусный баланс. Если средств достаточно — продлит подписку сам; если нет — пришлёт удобную кнопку оплаты в 1 клик."
    )
    await send_or_edit_banner(
        callback,
        banner_key="support",
        caption=text,
        reply_markup=faq_menu_kb(db_user.language),
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
    base_web = settings.WEBHOOK_BASE_URL.rstrip("/")
    mobile_sub_url = (
        f"{base_web}/sub/mobile/{sub.sub_id}"
        if sub
        else "Доступно после активации подписки"
    )
    text = (
        "📶 <b>Режим обхода мобильных глушилок (TLS Fragment)</b>\n\n"
        "Если на домашнем Wi-Fi всё летает, а на мобильном интернете (LTE/4G) соединение задумывается — мобильный оператор режет пакеты.\n\n"
        "✨ <b>Как решить за 15 секунд:</b>\n"
        "1. Нажми кнопку <b>«🚀 Включить мобильный профиль»</b> ниже (для Happ или v2rayTun).\n"
        "2. Или в настройках своего приложения включи два тумблера:\n"
        "   • <b>Фрагментация (TLS Fragment):</b> <code>Вкл (10-20 байт, задержка 10-15 мс)</code>\n"
        "   • <b>IPv6 маршрутизация:</b> <code>Отключить (Только IPv4)</code>\n\n"
        f"🔗 <b>Твоя умная ссылка с поддержкой мобильных сетей:</b>\n"
        f"<code>{mobile_sub_url}</code>"
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


@router.callback_query(F.data == "help:instructions")
async def cb_instructions(
    callback: CallbackQuery,
    repo: Repository,
    db_user: User,
) -> None:
    text = (
        "📖 <b>Простая инструкция: подключение за 30 секунд</b>\n\n"
        "Никаких сложных настроек — всё работает через одну умную ссылку:\n\n"
        "<b>Шаг 1.</b> Скачай бесплатное приложение для своего устройства по кнопкам ниже:\n"
        "• <b>iPhone / iPad / Mac:</b> Happ или v2rayTun\n"
        "• <b>Android:</b> v2rayTun или Hiddify\n"
        "• <b>Windows:</b> Hiddify\n\n"
        "<b>Шаг 2.</b> Открой раздел <b>«🔑 Моя подписка»</b> и нажми кнопку "
        "<b>«📲 Happ / v2rayTun (1 клик)»</b> — приложение откроется и всё настроит само!"
    )
    await send_or_edit_banner(
        callback,
        banner_key="connect",
        caption=text,
        reply_markup=instructions_kb(db_user.language),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer()
