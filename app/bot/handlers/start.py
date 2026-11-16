from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.profile import format_subscription_card
from app.bot.keyboards.user import (
    about_service_kb,
    admin_granted_subscription_kb,
    faq_menu_kb,
    instructions_kb,
    main_menu_kb,
    subscription_card_kb,
)
from app.bot.utils import format_dt_msk, send_or_edit_banner
from app.core.config import settings
from app.core.i18n import t
from app.db.models import GiftVoucherStatus, User
from app.db.repositories import Repository
from app.services.vpn import VPNService

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
    bot: Bot,
    session: AsyncSession,
    repo: Repository,
    state: FSMContext,
) -> None:
    await state.clear()
    tg_user = message.from_user
    if tg_user is None:
        return

    ref_tg_id: int | None = None
    ref_user_id: int | None = None
    gift_code: str | None = None

    args = (command.args or "").strip()
    if args.startswith("ref_"):
        raw_id = args.replace("ref_", "", 1)
        if raw_id.isdigit():
            ref_tg_id = int(raw_id)
    elif args.startswith("gift_"):
        gift_code = args.replace("gift_", "", 1).strip()
        if gift_code:
            voucher_preview = await repo.get_gift_voucher_by_code(gift_code)
            if (
                voucher_preview is not None
                and voucher_preview.status == GiftVoucherStatus.ACTIVE
                and not voucher_preview.is_admin_gift
                and voucher_preview.creator_user_id
            ):
                ref_user_id = voucher_preview.creator_user_id

    user, created_now = await repo.get_or_create_user(
        telegram_id=tg_user.id,
        username=tg_user.username,
        full_name=tg_user.full_name,
        referrer_telegram_id=ref_tg_id,
        language_code=tg_user.language_code,
        referrer_user_id=ref_user_id,
    )
    is_admin = tg_user.id in settings.ADMIN_IDS

    # 1. Проверяем наличие отложенных подписок, выданных администратором по @username
    if tg_user.username:
        clean_username = tg_user.username.lstrip("@").strip().lower()
        pending_grants = await repo.get_unclaimed_pending_gifts_by_username(clean_username)
        if pending_grants:
            vpn_service = VPNService(session)
            last_sub = None
            total_days = 0
            for grant in pending_grants:
                total_days += grant.days
                last_sub = await vpn_service.grant_or_extend_subscription(
                    user=user,
                    days=grant.days,
                    limit_ip=grant.limit_ip,
                    is_trial=False,
                )
                await repo.mark_pending_gift_claimed(grant.id)

            if last_sub is not None:
                is_unlimited = (last_sub.limit_ip == 0 or total_days >= 18250 or last_sub.days_left >= 18000)
                if is_unlimited:
                    grant_msg = (
                        "👑 <b>Вам выдана безлимитная подписка GhostFaceVPN!</b>\n\n"
                        "Администратор активировал для вас максимальный доступ без ограничений по времени, скорости и количеству устройств.\n\n"
                        f"📋 <b>Ваша ссылка подписки:</b>\n<code>{last_sub.subscription_url}</code>\n\n"
                        "Нажмите кнопку ниже, чтобы подключиться в 1 клик 👇"
                    )
                else:
                    expiry_date_str = format_dt_msk(last_sub.expires_at)
                    limit_text = "♾ Без ограничений" if last_sub.limit_ip == 0 else f"{last_sub.limit_ip} устр."
                    grant_msg = (
                        "🎉 <b>Вам выдана полноценная подписка GhostFaceVPN!</b>\n\n"
                        f"Администратор активировал для вас доступ на <b>{total_days} дн.</b> (до <b>{expiry_date_str} МСК</b>).\n"
                        f"📱 Лимит устройств: <b>{limit_text}</b>\n\n"
                        f"📋 <b>Ваша ссылка подписки:</b>\n<code>{last_sub.subscription_url}</code>\n\n"
                        "Нажмите кнопку ниже, чтобы подключиться в 1 клик 👇"
                    )
                await send_or_edit_banner(
                    message,
                    banner_key="profile",
                    caption=grant_msg,
                    reply_markup=admin_granted_subscription_kb(last_sub, user.language),
                    lang=user.language,
                    repo=repo,
                )
                return

    # 2. Если пользователь уже был создан админом заранее и у него есть активная подписка
    if user.subscription and user.subscription.is_active and not user.subscription.is_trial:
        sub = user.subscription
        is_unlimited = (sub.limit_ip == 0 or sub.days_left >= 18000)
        if is_unlimited:
            admin_sub_msg = (
                "👑 <b>Вам выдана безлимитная подписка GhostFaceVPN!</b>\n\n"
                "Администратор активировал для вас максимальный доступ без ограничений по времени, скорости и количеству устройств.\n\n"
                f"📋 <b>Ваша ссылка подписки:</b>\n<code>{sub.subscription_url}</code>\n\n"
                "Нажмите кнопку ниже, чтобы подключиться в 1 клик 👇"
            )
        else:
            expiry_date_str = format_dt_msk(sub.expires_at)
            limit_text = "♾ Без ограничений" if sub.limit_ip == 0 else f"{sub.limit_ip} устр."
            admin_sub_msg = (
                "🎉 <b>Вам выдана полноценная подписка GhostFaceVPN!</b>\n\n"
                f"Администратор активировал для вас доступ (до <b>{expiry_date_str} МСК</b>).\n"
                f"📱 Лимит устройств: <b>{limit_text}</b>\n\n"
                f"📋 <b>Ваша ссылка подписки:</b>\n<code>{sub.subscription_url}</code>\n\n"
                "Нажмите кнопку ниже, чтобы подключиться в 1 клик 👇"
            )
        await send_or_edit_banner(
            message,
            banner_key="profile",
            caption=admin_sub_msg,
            reply_markup=admin_granted_subscription_kb(sub, user.language),
            lang=user.language,
            repo=repo,
        )
        return

    if gift_code:
        vpn_service = VPNService(session)
        try:
            redeemed_voucher, sub = await vpn_service.redeem_gift_voucher(
                user=user,
                raw_code=gift_code,
                bot=bot,
            )
            card = await format_subscription_card(sub, user=user)
            await send_or_edit_banner(
                message,
                banner_key="profile",
                caption=(
                    f"🎁 <b>Подарочная подписка ({redeemed_voucher.days_label}) успешно активирована!</b>\n\n"
                    f"{card}"
                ),
                reply_markup=subscription_card_kb(user, sub),
                lang=user.language,
                repo=repo,
            )
            return
        except ValueError as exc:
            await send_or_edit_banner(
                message,
                banner_key="main",
                caption=f"⚠️ <b>{exc}</b>\n\n{build_welcome_text(user)}",
                reply_markup=main_menu_kb(user, is_admin=is_admin),
                lang=user.language,
                repo=repo,
            )
            return
        except Exception:
            await send_or_edit_banner(
                message,
                banner_key="main",
                caption=(
                    "⚠️ <b>Сервер 3X-UI временно обновляется. Нажмите на подарочную ссылку ещё раз через минуту!</b>\n\n"
                    f"{build_welcome_text(user)}"
                ),
                reply_markup=main_menu_kb(user, is_admin=is_admin),
                lang=user.language,
                repo=repo,
            )
            return

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
        "❓ <b>Центр помощи и заботы GhostFaceVPN</b>\n\n"
        "Мы собрали готовые ответы на самые частые вопросы для комфортной работы:\n\n"
        "• ⚙️ <b>Как выбрать оптимальный профиль подключения?</b>\n"
        "  Открой памятку ниже — в твоей подписке доступны основной и резервный профили для стабильного интернета без ограничений по скорости.\n\n"
        "• 📲 <b>Как установить на iPhone, Android или ноутбук?</b>\n"
        "  Всего 2 клика: скачай приложение и нажми «Подключить в 1 клик» в разделе подписки.\n\n"
        "• 💬 Если что-то не получается — напиши нашему живому специалисту @imran_wxz, мы поможем за пару минут!"
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


@router.callback_query(F.data == "help:about")
async def cb_about_service(
    callback: CallbackQuery,
    repo: Repository,
    db_user: User,
) -> None:
    doc_date = settings.DOCS_UPDATED_DATE
    text = (
        "ℹ️ <b>О сервисе GhostFaceVPN</b>\n\n"
        "GhostFaceVPN — сервис стабильного высокоскоростного сетевого подключения, "
        "доступный из любой точки мира. Стабильный интернет без ограничений по скорости и трафику.\n\n"
        f"📄 <b>Политика конфиденциальности (от {doc_date}):</b>\n"
        f"{settings.PRIVACY_URL}\n\n"
        f"📜 <b>Пользовательское соглашение (от {doc_date}):</b>\n"
        f"{settings.TERMS_URL}\n\n"
        f"📅 Дата редакции документов: <b>{doc_date}</b>\n"
        "💬 Поддержка: @imran_wxz"
    )
    await send_or_edit_banner(
        callback,
        banner_key="support",
        caption=text,
        reply_markup=about_service_kb(db_user.language),
        lang=db_user.language,
        repo=repo,
    )
    await callback.answer()

