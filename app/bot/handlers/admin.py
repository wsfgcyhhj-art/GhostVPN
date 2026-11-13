from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.admin import (
    admin_broadcast_confirm_kb,
    admin_broadcast_segment_kb,
    admin_home_kb,
    admin_plans_kb,
    admin_promos_kb,
    admin_user_manage_kb,
)
from app.bot.states import (
    AdminBroadcastState,
    AdminMassCompensateState,
    AdminPlanState,
    AdminPromoCreateState,
    AdminSearchState,
)
from app.bot.utils import format_dt_msk
from app.core.config import settings
from app.core.logging import logger
from app.db.models import (
    PromoCode,
    PromoType,
    Subscription,
    User,
    utcnow,
)
from app.db.repositories import Repository
from app.db.session import async_session_factory
from app.services.banner_generator import MASCOT_PATH
from app.services.vpn import VPNService, datetime_to_ms
from app.services.xui.client import XUIClient

router = Router(name="admin")


def is_admin_user(telegram_id: int) -> bool:
    return telegram_id in settings.ADMIN_IDS


async def build_dashboard_text(repo: Repository) -> str:
    stats = await repo.get_dashboard_stats()
    server = await repo.get_active_server()

    xui_line = "🔴 Не настроен"
    if server is not None:
        client = XUIClient.from_server(server, timeout=4.0)
        try:
            st = await client.get_server_status()
            if st.online:
                uptime_h = round(st.uptime_seconds / 3600, 1)
                xui_line = (
                    f"🟢 Онлайн (Xray: <b>{st.xray_state}</b>, аптайм: <b>{uptime_h} ч.</b>)\n"
                    f"• Нагрузка: CPU <b>{st.cpu_percent}%</b> | "
                    f"RAM <b>{st.mem_used_mb:.0f} / {st.mem_total_mb:.0f} MB</b>\n"
                    f"• Сейчас онлайн клиентов: <b>{st.online_clients_count}</b>"
                )
            else:
                xui_line = "🟡 Панель доступна, метрики скрыты или офлайн"
        except Exception as exc:
            xui_line = f"🔴 Ошибка связи ({exc})"
        finally:
            await client.close()

    usdt_est = (
        (stats["rev_crypto_rub_30d"] / Decimal(str(settings.USDT_RUB_RATE))).quantize(
            Decimal("0.01")
        )
        if settings.USDT_RUB_RATE > 0
        else Decimal("0.00")
    )
    stars_est = (
        int(float(stats["rev_stars_rub_30d"]) / settings.STARS_RUB_RATE)
        if settings.STARS_RUB_RATE > 0
        else 0
    )

    return (
        f"🛠 <b>Админ-панель GhostVPN (Ghostface Edition)</b>\n\n"
        f"👥 <b>Аудитория и Воронка:</b>\n"
        f"• Всего пользователей: <b>{stats['total_users']}</b> "
        f"(+<b>{stats['new_users_today']}</b> сегодня / +<b>{stats['new_users_7d']}</b> за 7 дн.)\n"
        f"• Платных активных подписок: <b>{stats['active_subs']}</b>\n"
        f"• На триале за 1 ₽ сейчас: <b>{stats['trial_subs']}</b>\n"
        f"• Конверсия из триала в покупку: <b>{stats['conversion_rate']}%</b> "
        f"({stats['converted_from_trial']} из {stats['total_trial_users']})\n\n"
        f"💰 <b>Выручка (всего):</b>\n"
        f"• За сегодня: <b>{stats['rev_today']} ₽</b>\n"
        f"• За 7 дней: <b>{stats['rev_7d']} ₽</b>\n"
        f"• За 30 дней: <b>{stats['rev_month']} ₽</b>\n"
        f"• За всё время: <b>{stats['rev_total']} ₽</b>\n\n"
        f"💳 <b>Разбивка по шлюзам за 30 дней:</b>\n"
        f"• СБП / Карты (FreeKassa): <b>{stats['rev_rub_gw_30d']} ₽</b>\n"
        f"• CryptoBot: <b>{stats['rev_crypto_rub_30d']} ₽</b> (~<b>{usdt_est} USDT</b>)\n"
        f"• Telegram Stars: <b>{stats['rev_stars_rub_30d']} ₽</b> (~<b>{stars_est} ⭐️</b>)\n\n"
        f"🖥 <b>VPS 3X-UI ({server.name if server else 'N/A'}):</b>\n"
        f"• {xui_line}"
    )


async def format_admin_user_profile(repo: Repository, u: User) -> str:
    sub = u.subscription
    if sub:
        sub_info = (
            f"• Статус: {'🟢 Активна' if sub.is_currently_valid else '🔴 Истекла/Откл.'}\n"
            f"• До: <b>{format_dt_msk(sub.expires_at)}</b>\n"
            f"• Лимит устройств: <b>{sub.limit_ip} устр.</b> (доп. +{sub.extra_devices})\n"
            f"• Трафик: <b>{sub.used_traffic_gb} ГБ</b> | Нарушений IP: <b>{sub.ip_violations_count}</b>\n"
            f"• Sub ID: <code>{sub.sub_id}</code>\n"
            f"• Email 3X-UI: <code>{sub.xui_email}</code>\n"
            f"• UUID: <code>{sub.xui_client_uuid}</code>\n"
            f"• Ссылка: <code>{sub.subscription_url}</code>"
        )
    else:
        sub_info = "• Подписка: отсутствует"

    txs = await repo.get_user_transactions(u.id, limit=5)
    if txs:
        tx_lines = [
            f"  #{tx.id} [{tx.status.value}] {tx.provider.value}: {tx.amount_rub} ₽ ({tx.tx_type.value})"
            for tx in txs
        ]
        tx_block = "\n".join(tx_lines)
    else:
        tx_block = "  Нет платежей"

    promos = await repo.get_user_promo_usages(u.id)
    if promos:
        promo_block = ", ".join(
            f"<code>{pu.promocode.code}</code>" for pu in promos if pu.promocode
        )
    else:
        promo_block = "Нет"

    return (
        f"👤 <b>CRM-карточка клиента #{u.id}</b>\n\n"
        f"• Telegram ID: <code>{u.telegram_id}</code> (Язык: <b>{u.language.upper()}</b>)\n"
        f"• Username: @{u.username or 'нет'} ({u.full_name or ''})\n"
        f"• Внутренний баланс: <b>{u.balance_rub} ₽</b>\n"
        f"• Автопродление: <b>{'ВКЛ' if u.auto_renew else 'ВЫКЛ'}</b> | Триал: <b>{'Да' if u.has_used_trial else 'Нет'}</b>\n"
        f"• Бан: <b>{'⛔ ЗАБЛОКИРОВАН' if u.is_banned else 'Нет'}</b>\n\n"
        f"🔑 <b>Подписка 3X-UI:</b>\n{sub_info}\n\n"
        f"💳 <b>Последние платежи:</b>\n{tx_block}\n\n"
        f"🎟 <b>Активированные промокоды:</b> {promo_block}"
    )


@router.message(Command("admin"))
async def cmd_admin(
    message: Message,
    repo: Repository,
    state: FSMContext,
) -> None:
    if not message.from_user or not is_admin_user(message.from_user.id):
        return
    await state.clear()
    text = await build_dashboard_text(repo)
    await message.answer(text, reply_markup=admin_home_kb(), parse_mode="HTML")


@router.callback_query(F.data == "admin:home")
async def cb_admin_home(
    callback: CallbackQuery,
    repo: Repository,
    state: FSMContext,
) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await state.clear()
    text = await build_dashboard_text(repo)
    if callback.message:
        try:
            await callback.message.edit_text(
                text, reply_markup=admin_home_kb(), parse_mode="HTML"
            )
        except Exception:
            await callback.message.answer(
                text, reply_markup=admin_home_kb(), parse_mode="HTML"
            )
    await callback.answer()


# --- Синхронизация описания, команд и напоминание об аватаре Ghostface ---
@router.callback_query(F.data == "admin:sync_bot_profile")
async def cb_admin_sync_bot_profile(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    try:
        await bot.set_my_commands(
            commands=[
                BotCommand(command="start", description="👻 Главное меню и личный кабинет"),
                BotCommand(command="admin", description="🛠 Панель управления (для админов)"),
            ]
        )
        await bot.set_my_short_description(
            short_description="👻 GhostVPN — твоя цифровая тень в сети. VLESS Reality 500 Мбит/с, YouTube 4K и обход мобильных глушилок."
        )
        await bot.set_my_description(
            description=(
                "👻 Добро пожаловать в GhostVPN (Ghostface Edition)!\n\n"
                "• 🇳🇱 Скоростной сервер в Нидерландах (до 500 Мбит/с)\n"
                "• 🔥 Полный тест-драйв на 3 дня за 1 ₽\n"
                "• 🛡 Обход блокировок и мобильных глушилок (TLS Fragment)\n"
                "• 📱 Подключение в 1 клик на iPhone, Android, Windows и Mac\n\n"
                "Нажми «Начать» (/start), чтобы забрать свой ключ!"
            )
        )
    except Exception as exc:
        logger.warning("Не удалось обновить описание бота через API: %s", exc)

    mascot_status = "✅ Файл найден" if MASCOT_PATH.exists() else "⚠️ Используется встроенный фоллбэк"
    if callback.message and MASCOT_PATH.exists():
        photo = BufferedInputFile(MASCOT_PATH.read_bytes(), filename="ghostface_avatar.png")
        await callback.message.answer_photo(
            photo=photo,
            caption=(
                "👻 <b>Команды и описание бота успешно обновлены через Bot API!</b>\n\n"
                f"• Исходник <code>app/static/images/ghostface_base.png</code>: <b>{mascot_status}</b>\n"
                "• Напоминание: если аватарка самого бота в Telegram ещё не установлена, "
                "сохрани эту картинку выше и отправь её в <code>@BotFather</code> → <code>/setuserpic</code>."
            ),
            parse_mode="HTML",
        )
    await callback.answer("Профиль бота синхронизирован!")


# --- Ручной запуск бэкапа БД ---
@router.callback_query(F.data == "admin:backup_now")
async def cb_admin_backup_now(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    from app.tasks.scheduler import create_and_send_database_backup

    ok = await create_and_send_database_backup(bot=bot, target_chat_id=callback.from_user.id)
    if ok:
        await callback.answer("📦 Архив бэкапа БД отправлен вам в личные сообщения!", show_alert=True)
    else:
        await callback.answer("Ошибка создания бэкапа.", show_alert=True)


# --- Массовая компенсация бонусных дней всем активным ---
@router.callback_query(F.data == "admin:mass_compensate")
async def cb_admin_mass_compensate(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    await state.set_state(AdminMassCompensateState.waiting_for_days)
    if callback.message:
        await callback.message.edit_text(
            "🎁 <b>Массовое начисление бонусных дней всем активным клиентам</b>\n\n"
            "Введите положительное количество дней (например, <code>3</code>), которое нужно добавить "
            "каждой активной подписке в БД и в панели 3X-UI (с отправкой уведомления пользователям):",
            reply_markup=admin_home_kb(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(AdminMassCompensateState.waiting_for_days)
async def msg_admin_mass_compensate(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    repo: Repository,
    state: FSMContext,
) -> None:
    if not message.from_user or not is_admin_user(message.from_user.id):
        return
    try:
        days_to_add = int((message.text or "").strip())
        if days_to_add <= 0 or days_to_add > 365:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое число от 1 до 365.")
        return

    await state.clear()
    active_subs = await repo.get_all_active_subscriptions()
    vpn_service = VPNService(session)
    updated_count = 0

    for sub in active_subs:
        user = await session.get(User, sub.user_id)
        if user is None:
            continue
        try:
            await vpn_service.grant_or_extend_subscription(user, days=days_to_add)
            updated_count += 1
            try:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=(
                        f"🎁 <b>Подарок от команды GhostVPN!</b>\n\n"
                        f"Мы начислили вам <b>+{days_to_add} бонусных дн.</b> к вашей текущей подписке.\n"
                        f"Спасибо, что остаётесь с нами!"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass
            await asyncio.sleep(0.05)
        except Exception as exc:
            logger.warning("Ошибка массовой компенсации для user_id=%s: %s", user.id, exc)

    await message.answer(
        f"✅ <b>Массовая компенсация завершена!</b>\n"
        f"Начислено <b>+{days_to_add} дн.</b> для <b>{updated_count}</b> активных подписок.",
        reply_markup=admin_home_kb(),
        parse_mode="HTML",
    )


# --- CRM User Management ---
@router.callback_query(F.data == "admin:user_search")
async def cb_admin_user_search(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    await state.set_state(AdminSearchState.waiting_for_query)
    if callback.message:
        await callback.message.edit_text(
            "🔍 <b>CRM Поиск клиента</b>\n\n"
            "Отправьте <code>telegram_id</code>, <code>@username</code>, <code>sub_id</code> или <code>email</code> из 3X-UI (например, <code>tg_123456</code>):",
            reply_markup=admin_home_kb(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(AdminSearchState.waiting_for_query)
async def msg_admin_user_search(
    message: Message,
    repo: Repository,
    state: FSMContext,
) -> None:
    if not message.from_user or not is_admin_user(message.from_user.id):
        return
    target = await repo.find_user_by_query(message.text or "")
    if target is None:
        await message.answer(
            "❌ Пользователь не найден. Попробуйте другой запрос:",
            reply_markup=admin_home_kb(),
        )
        return

    await state.clear()
    await message.answer(
        await format_admin_user_profile(repo, target),
        reply_markup=admin_user_manage_kb(target),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("admin:u_days:"))
async def cb_admin_u_days(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    uid = int((callback.data or "").split(":")[2])
    await state.set_state(AdminSearchState.waiting_for_days_delta)
    await state.update_data(target_user_id=uid)
    if callback.message:
        await callback.message.edit_text(
            "📅 Введите количество дней для добавления (например, <code>30</code>) "
            "или списания (например, <code>-5</code>):",
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(AdminSearchState.waiting_for_days_delta)
async def msg_admin_u_days(
    message: Message,
    session: AsyncSession,
    repo: Repository,
    state: FSMContext,
) -> None:
    if not message.from_user or not is_admin_user(message.from_user.id):
        return
    raw = (message.text or "").strip()
    try:
        days_delta = int(raw)
    except ValueError:
        await message.answer("Введите целое число (например, 15 или -5).")
        return

    data = await state.get_data()
    target = await repo.get_user_by_id(int(data["target_user_id"]))
    await state.clear()
    if target is None:
        await message.answer("Пользователь не найден.", reply_markup=admin_home_kb())
        return

    vpn_service = VPNService(session)
    try:
        await vpn_service.grant_or_extend_subscription(target, days=days_delta)
        await session.refresh(target)
        await message.answer(
            f"✅ Изменение срока подписки ({days_delta:+} дн.) синхронизировано с 3X-UI!\n\n"
            + await format_admin_user_profile(repo, target),
            reply_markup=admin_user_manage_kb(target),
            parse_mode="HTML",
        )
    except Exception as exc:
        await message.answer(
            f"⚠️ Ошибка синхронизации с 3X-UI: {exc}",
            reply_markup=admin_user_manage_kb(target),
        )


@router.callback_query(F.data.startswith("admin:u_limit:"))
async def cb_admin_u_limit(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    uid = int((callback.data or "").split(":")[2])
    await state.set_state(AdminSearchState.waiting_for_limit_ip)
    await state.update_data(target_user_id=uid)
    if callback.message:
        await callback.message.edit_text(
            "📱 Введите новый общий лимит устройств для клиента (от <code>1</code> до <code>15</code>):",
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(AdminSearchState.waiting_for_limit_ip)
async def msg_admin_u_limit(
    message: Message,
    session: AsyncSession,
    repo: Repository,
    state: FSMContext,
) -> None:
    if not message.from_user or not is_admin_user(message.from_user.id):
        return
    try:
        new_limit = int((message.text or "").strip())
        if new_limit < 1 or new_limit > 20:
            raise ValueError
    except ValueError:
        await message.answer("Введите число от 1 до 20.")
        return

    data = await state.get_data()
    target = await repo.get_user_by_id(int(data["target_user_id"]))
    await state.clear()
    if target is None or target.subscription is None:
        await message.answer("У пользователя ещё нет подписки.", reply_markup=admin_home_kb())
        return

    sub = target.subscription
    server = sub.server or await repo.get_active_server()
    if server:
        client = XUIClient.from_server(server)
        try:
            await VPNService._sync_update_client_all_inbounds(
                client,
                server,
                client_uuid=sub.xui_client_uuid,
                email=sub.xui_email,
                sub_id=sub.sub_id,
                expiry_time_ms=datetime_to_ms(sub.expires_at),
                limit_ip=new_limit,
                enable=sub.is_currently_valid,
                tg_id=str(target.telegram_id),
            )
        finally:
            await client.close()

    sub.limit_ip = new_limit
    sub.extra_devices = max(0, new_limit - settings.DEFAULT_LIMIT_IP)
    await session.commit()
    await session.refresh(target)

    await message.answer(
        f"✅ Лимит устройств изменён на {new_limit}!\n\n"
        + await format_admin_user_profile(repo, target),
        reply_markup=admin_user_manage_kb(target),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("admin:u_bal:"))
async def cb_admin_u_bal(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    uid = int((callback.data or "").split(":")[2])
    await state.set_state(AdminSearchState.waiting_for_balance_delta)
    await state.update_data(target_user_id=uid)
    if callback.message:
        await callback.message.edit_text(
            "💰 Введите сумму изменения баланса в рублях (например, <code>150</code> или <code>-50</code>):",
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(AdminSearchState.waiting_for_balance_delta)
async def msg_admin_u_bal(
    message: Message,
    session: AsyncSession,
    repo: Repository,
    state: FSMContext,
) -> None:
    if not message.from_user or not is_admin_user(message.from_user.id):
        return
    try:
        delta = Decimal((message.text or "").strip().replace(",", "."))
    except Exception:
        await message.answer("Введите корректное число.")
        return

    data = await state.get_data()
    target = await repo.get_user_by_id(int(data["target_user_id"]))
    await state.clear()
    if target is None:
        return

    target.balance_rub = max(Decimal("0.00"), (target.balance_rub + delta).quantize(Decimal("0.01")))
    await session.commit()
    await message.answer(
        f"✅ Баланс обновлён ({delta:+} ₽)!\n\n" + await format_admin_user_profile(repo, target),
        reply_markup=admin_user_manage_kb(target),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("admin:u_reissue:"))
async def cb_admin_u_reissue(
    callback: CallbackQuery,
    session: AsyncSession,
    repo: Repository,
) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    uid = int((callback.data or "").split(":")[2])
    target = await repo.get_user_by_id(uid)
    if target is None:
        return

    vpn_service = VPNService(session)
    try:
        await vpn_service.reissue_key(target, force_by_admin=True)
        await session.refresh(target)
        if callback.message:
            await callback.message.edit_text(
                "✅ Ключ пользователя принудительно перевыпущен!\n\n"
                + await format_admin_user_profile(repo, target),
                reply_markup=admin_user_manage_kb(target),
                parse_mode="HTML",
            )
        await callback.answer("Ключ сброшен!")
    except Exception as exc:
        await callback.answer(f"Ошибка: {exc}", show_alert=True)


@router.callback_query(F.data.startswith("admin:u_ban:"))
async def cb_admin_u_ban(
    callback: CallbackQuery,
    session: AsyncSession,
    repo: Repository,
) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    uid = int((callback.data or "").split(":")[2])
    target = await repo.get_user_by_id(uid)
    if target is None:
        return

    target.is_banned = not target.is_banned
    await session.commit()
    if callback.message:
        await callback.message.edit_text(
            await format_admin_user_profile(repo, target),
            reply_markup=admin_user_manage_kb(target),
            parse_mode="HTML",
        )
    await callback.answer("Статус бана изменён.")


@router.callback_query(F.data.startswith("admin:u_toggle_sub:"))
async def cb_admin_u_toggle_sub(
    callback: CallbackQuery,
    session: AsyncSession,
    repo: Repository,
) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    uid = int((callback.data or "").split(":")[2])
    target = await repo.get_user_by_id(uid)
    if target is None or target.subscription is None:
        return

    vpn_service = VPNService(session)
    try:
        await vpn_service.set_client_enabled(
            target.subscription, not target.subscription.is_active
        )
        await session.refresh(target)
        if callback.message:
            await callback.message.edit_text(
                await format_admin_user_profile(repo, target),
                reply_markup=admin_user_manage_kb(target),
                parse_mode="HTML",
            )
        await callback.answer("Статус клиента в 3X-UI изменён!")
    except Exception as exc:
        await callback.answer(f"Ошибка 3X-UI: {exc}", show_alert=True)


# --- Plans Management ---
@router.callback_query(F.data == "admin:plans")
async def cb_admin_plans(callback: CallbackQuery, repo: Repository) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    plans = await repo.get_all_plans()
    if callback.message:
        await callback.message.edit_text(
            "📦 <b>Управление тарифами</b>\n"
            "Нажмите на тариф для изменения цены или переключите его видимость:",
            reply_markup=admin_plans_kb(plans),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:plan_toggle:"))
async def cb_admin_plan_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    repo: Repository,
) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    plan_id = int((callback.data or "").split(":")[2])
    plan = await repo.get_plan_by_id(plan_id)
    if plan:
        plan.is_active = not plan.is_active
        await session.commit()
    plans = await repo.get_all_plans()
    if callback.message:
        await callback.message.edit_text(
            "📦 <b>Управление тарифами</b>",
            reply_markup=admin_plans_kb(plans),
            parse_mode="HTML",
        )
    await callback.answer("Обновлено!")


@router.callback_query(F.data.startswith("admin:plan_edit:"))
async def cb_admin_plan_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    plan_id = int((callback.data or "").split(":")[2])
    await state.set_state(AdminPlanState.waiting_for_new_price)
    await state.update_data(edit_plan_id=plan_id)
    if callback.message:
        await callback.message.edit_text(
            "💵 Введите новую цену тарифа в рублях (например, <code>249</code>):",
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(AdminPlanState.waiting_for_new_price)
async def msg_admin_plan_price(
    message: Message,
    session: AsyncSession,
    repo: Repository,
    state: FSMContext,
) -> None:
    if not message.from_user or not is_admin_user(message.from_user.id):
        return
    try:
        new_price = Decimal((message.text or "").strip().replace(",", ".")).quantize(
            Decimal("0.01")
        )
        if new_price <= Decimal("0"):
            raise ValueError
    except Exception:
        await message.answer("Введите положительное число.")
        return

    data = await state.get_data()
    plan = await repo.get_plan_by_id(int(data["edit_plan_id"]))
    await state.clear()
    if plan:
        plan.price_rub = new_price
        await session.commit()

    plans = await repo.get_all_plans()
    await message.answer(
        "✅ Цена тарифа обновлена!\n\n📦 <b>Управление тарифами</b>",
        reply_markup=admin_plans_kb(plans),
        parse_mode="HTML",
    )


# --- Promocodes Management ---
@router.callback_query(F.data == "admin:promos")
async def cb_admin_promos(callback: CallbackQuery, session: AsyncSession) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    res = await session.execute(
        select(PromoCode).where(PromoCode.is_active.is_(True)).order_by(PromoCode.id.desc())
    )
    promos = res.scalars().all()
    if callback.message:
        await callback.message.edit_text(
            "🎟 <b>Активные промокоды</b>\n"
            "Нажмите на промокод, чтобы деактивировать/удалить его, или создайте новый:",
            reply_markup=admin_promos_kb(promos),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:promo_del:"))
async def cb_admin_promo_del(callback: CallbackQuery, session: AsyncSession) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    pid = int((callback.data or "").split(":")[2])
    promo = await session.get(PromoCode, pid)
    if promo:
        promo.is_active = False
        await session.commit()
    res = await session.execute(
        select(PromoCode).where(PromoCode.is_active.is_(True)).order_by(PromoCode.id.desc())
    )
    promos = res.scalars().all()
    if callback.message:
        await callback.message.edit_text(
            "🎟 <b>Активные промокоды</b>",
            reply_markup=admin_promos_kb(promos),
            parse_mode="HTML",
        )
    await callback.answer("Промокод удалён.")


@router.callback_query(F.data == "admin:promo_create")
async def cb_admin_promo_create(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    await state.set_state(AdminPromoCreateState.waiting_for_params)
    if callback.message:
        await callback.message.edit_text(
            "➕ <b>Создание промокода</b>\n\n"
            "Отправьте параметры одной строкой через пробел:\n"
            "<code>КОД ТИП(DAYS|PERCENT) ЗНАЧЕНИЕ ЛИМИТ_АКТИВАЦИЙ ДНЕЙ_СРОКА НОВЫЕ(0|1)</code>\n\n"
            "Примеры:\n"
            "• <code>GHOST7 DAYS 7 100 30 1</code> (+7 дней, 100 шт, на 30 дней, только новым)\n"
            "• <code>SALE25 PERCENT 25 50 14 0</code> (скидка 25%, 50 шт, на 14 дней, всем)",
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(AdminPromoCreateState.waiting_for_params)
async def msg_admin_promo_create(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if not message.from_user or not is_admin_user(message.from_user.id):
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 4:
        await message.answer("Неверный формат. Минимум 4 параметра: КОД ТИП ЗНАЧЕНИЕ ЛИМИТ")
        return

    code = parts[0].upper()
    p_type_raw = parts[1].upper()
    if p_type_raw not in ("DAYS", "PERCENT"):
        await message.answer("Тип должен быть DAYS или PERCENT.")
        return

    try:
        value = int(parts[2])
        max_uses = int(parts[3])
        valid_days = int(parts[4]) if len(parts) > 4 else 30
        only_new = bool(int(parts[5])) if len(parts) > 5 else False
    except ValueError:
        await message.answer("Числовые параметры указаны неверно.")
        return

    promo = PromoCode(
        code=code,
        type=PromoType(p_type_raw),
        value=value,
        max_uses=max_uses,
        expires_at=utcnow() + timedelta(days=valid_days),
        only_new_users=only_new,
        is_active=True,
    )
    session.add(promo)
    await session.commit()
    await state.clear()

    res = await session.execute(
        select(PromoCode).where(PromoCode.is_active.is_(True)).order_by(PromoCode.id.desc())
    )
    promos = res.scalars().all()
    await message.answer(
        f"✅ Промокод <code>{code}</code> успешно создан!",
        reply_markup=admin_promos_kb(promos),
        parse_mode="HTML",
    )


# --- Broadcast Constructor (текст, фото, видео, кружочки video_note + URL/callback кнопки) ---
@router.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    await state.set_state(AdminBroadcastState.waiting_for_segment)
    if callback.message:
        await callback.message.edit_text(
            "📢 <b>Конструктор рассылок (Broadcast)</b>\n\nВыберите сегмент получателей:",
            reply_markup=admin_broadcast_segment_kb(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:bc_seg:"))
async def cb_admin_bc_seg(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    segment = (callback.data or "").split(":")[2]
    await state.update_data(segment=segment)
    await state.set_state(AdminBroadcastState.waiting_for_content)
    if callback.message:
        await callback.message.edit_text(
            "📝 Отправьте контент для рассылки:\n"
            "• Обычный текст с HTML-разметкой\n"
            "• Фото с подписью\n"
            "• Видео с подписью\n"
            "• Видеокружочек (Video Note)",
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(AdminBroadcastState.waiting_for_content)
async def msg_admin_bc_content(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin_user(message.from_user.id):
        return
    photo_id = message.photo[-1].file_id if message.photo else None
    video_id = message.video.file_id if message.video else None
    video_note_id = message.video_note.file_id if message.video_note else None
    text = message.html_text or message.caption or ""
    await state.update_data(
        photo_id=photo_id,
        video_id=video_id,
        video_note_id=video_note_id,
        text=text,
    )
    await state.set_state(AdminBroadcastState.waiting_for_button)
    await message.answer(
        "🔗 Хотите прикрепить кнопку к сообщению?\n"
        "• Для внешней ссылки: <code>Текст кнопки | https://...</code>\n"
        "• Для перехода к тарифам: <code>🔥 Выбрать тариф | plans:list</code>\n"
        "• Для триала за 1 ₽: <code>🎁 Тест за 1 ₽ | trial:start</code>\n"
        "• Или отправьте <code>-</code> без кнопки:",
        parse_mode="HTML",
    )


def _build_broadcast_button(
    btn_text: str | None, btn_target: str | None
) -> InlineKeyboardMarkup | None:
    if not btn_text or not btn_target:
        return None
    if btn_target.startswith(("http://", "https://", "tg://")):
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=btn_text, url=btn_target)]]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=btn_text, callback_data=btn_target)]]
    )


@router.message(AdminBroadcastState.waiting_for_button)
async def msg_admin_bc_button(message: Message, bot: Bot, state: FSMContext) -> None:
    if not message.from_user or not is_admin_user(message.from_user.id):
        return
    raw = (message.text or "").strip()
    btn_text: str | None = None
    btn_target: str | None = None
    if raw != "-" and "|" in raw:
        parts = [p.strip() for p in raw.split("|", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            btn_text, btn_target = parts[0], parts[1]

    await state.update_data(btn_text=btn_text, btn_target=btn_target)
    await state.set_state(AdminBroadcastState.waiting_for_confirm)
    data = await state.get_data()

    preview_kb = _build_broadcast_button(btn_text, btn_target)
    try:
        if data.get("video_note_id"):
            await bot.send_video_note(
                chat_id=message.chat.id,
                video_note=data["video_note_id"],
                reply_markup=preview_kb,
            )
        elif data.get("video_id"):
            await bot.send_video(
                chat_id=message.chat.id,
                video=data["video_id"],
                caption=data.get("text", ""),
                reply_markup=preview_kb,
                parse_mode="HTML",
            )
        elif data.get("photo_id"):
            await bot.send_photo(
                chat_id=message.chat.id,
                photo=data["photo_id"],
                caption=data.get("text", ""),
                reply_markup=preview_kb,
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                chat_id=message.chat.id,
                text=data.get("text", "") or "<i>(пустой текст)</i>",
                reply_markup=preview_kb,
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.warning("Ошибка предпросмотра рассылки: %s", exc)

    await message.answer(
        f"👀 <b>Выше — предпросмотр вашего сообщения!</b>\n"
        f"• Сегмент: <b>{data['segment']}</b>\n"
        f"• Кнопка: <b>{btn_text or 'нет'}</b>\n\n"
        f"Запустить рассылку в фоновом режиме (20 сообщ./сек)?",
        reply_markup=admin_broadcast_confirm_kb(),
        parse_mode="HTML",
    )


async def _run_broadcast_task(
    bot: Bot,
    admin_chat_id: int,
    segment: str,
    text: str,
    photo_id: str | None,
    video_id: str | None,
    video_note_id: str | None,
    btn_text: str | None,
    btn_target: str | None,
) -> None:
    now = utcnow()
    async with async_session_factory() as session:
        if segment == "active":
            stmt = (
                select(User.telegram_id)
                .join(Subscription, Subscription.user_id == User.id)
                .where(
                    User.is_banned.is_(False),
                    Subscription.is_active.is_(True),
                    Subscription.expires_at > now,
                )
            )
        elif segment == "inactive":
            stmt = (
                select(User.telegram_id)
                .outerjoin(Subscription, Subscription.user_id == User.id)
                .where(
                    User.is_banned.is_(False),
                    (Subscription.id.is_(None)) | (Subscription.expires_at <= now),
                )
            )
        else:
            stmt = select(User.telegram_id).where(User.is_banned.is_(False))

        res = await session.execute(stmt)
        tg_ids = list(res.scalars().all())

    kb = _build_broadcast_button(btn_text, btn_target)

    sent = 0
    failed = 0
    for tg_id in tg_ids:
        try:
            if video_note_id:
                await bot.send_video_note(
                    chat_id=tg_id,
                    video_note=video_note_id,
                    reply_markup=kb,
                )
            elif video_id:
                await bot.send_video(
                    chat_id=tg_id,
                    video=video_id,
                    caption=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            elif photo_id:
                await bot.send_photo(
                    chat_id=tg_id,
                    photo=photo_id,
                    caption=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    chat_id=tg_id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            sent += 1
        except Exception as exc:
            logger.debug("Ошибка рассылки пользователю %s: %s", tg_id, exc)
            failed += 1
        await asyncio.sleep(0.05)

    try:
        await bot.send_message(
            chat_id=admin_chat_id,
            text=(
                f"📊 <b>Рассылка завершена!</b>\n\n"
                f"• Сегмент: <b>{segment}</b>\n"
                f"• Успешно доставлено: <b>{sent}</b>\n"
                f"• Ошибок (заблокировали бота): <b>{failed}</b>"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(AdminBroadcastState.waiting_for_confirm, F.data == "admin:bc_run")
async def cb_admin_bc_run(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
) -> None:
    if not is_admin_user(callback.from_user.id):
        return
    data = await state.get_data()
    await state.clear()

    asyncio.create_task(
        _run_broadcast_task(
            bot=bot,
            admin_chat_id=callback.from_user.id,
            segment=data.get("segment", "all"),
            text=data.get("text", ""),
            photo_id=data.get("photo_id"),
            video_id=data.get("video_id"),
            video_note_id=data.get("video_note_id"),
            btn_text=data.get("btn_text"),
            btn_target=data.get("btn_target"),
        )
    )
    if callback.message:
        await callback.message.edit_text(
            "🚀 <b>Рассылка запущена в фоновом режиме!</b>\n"
            "По завершении вы получите подробный отчёт.",
            reply_markup=admin_home_kb(),
            parse_mode="HTML",
        )
    await callback.answer()
