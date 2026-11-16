from __future__ import annotations

from typing import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.keyboards.user import build_gift_voucher_share_url
from app.db.models import Plan, PromoCode, User


def admin_home_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🔄 Обновить аналитику", callback_data="admin:home"),
        InlineKeyboardButton(text="🔍 CRM Поиск клиента", callback_data="admin:user_search"),
    )
    b.row(
        InlineKeyboardButton(text="📦 Тарифы", callback_data="admin:plans"),
        InlineKeyboardButton(text="🎟 Промокоды", callback_data="admin:promos"),
    )
    b.row(
        InlineKeyboardButton(text="🎁 Подарить подписку", callback_data="admin:gift_menu"),
        InlineKeyboardButton(
            text="🎁 Начислить дни всем активным", callback_data="admin:mass_compensate"
        ),
    )
    b.row(
        InlineKeyboardButton(text="📢 Конструктор рассылок", callback_data="admin:broadcast"),
        InlineKeyboardButton(text="💾 Бэкап БД сейчас", callback_data="admin:backup_now"),
    )
    b.row(
        InlineKeyboardButton(
            text="👻 Синхронизировать профиль бота", callback_data="admin:sync_bot_profile"
        )
    )
    b.row(InlineKeyboardButton(text="⬅️ В пользовательское меню", callback_data="menu:home"))
    return b.as_markup()


def admin_user_manage_kb(target_user: User) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="➕/➖ Дни подписки", callback_data=f"admin:u_days:{target_user.id}"
        ),
        InlineKeyboardButton(
            text="💰 Изменить баланс", callback_data=f"admin:u_bal:{target_user.id}"
        ),
    )
    b.row(
        InlineKeyboardButton(
            text="📱 Лимит устройств", callback_data=f"admin:u_limit:{target_user.id}"
        ),
        InlineKeyboardButton(
            text="🔄 Перевыпустить UUID", callback_data=f"admin:u_reissue:{target_user.id}"
        ),
    )
    b.row(
        InlineKeyboardButton(
            text="✅ Разбанить" if target_user.is_banned else "⛔ Забанить",
            callback_data=f"admin:u_ban:{target_user.id}",
        ),
    )
    if target_user.subscription is not None:
        sub_active = target_user.subscription.is_active
        b.row(
            InlineKeyboardButton(
                text="⏸ Выключить в 3X-UI" if sub_active else "▶️ Включить в 3X-UI",
                callback_data=f"admin:u_toggle_sub:{target_user.id}",
            )
        )
    b.row(InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:home"))
    return b.as_markup()


def admin_plans_kb(plans: Sequence[Plan]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in plans:
        status_icon = "🟢" if p.is_active else "🔴"
        b.row(
            InlineKeyboardButton(
                text=f"{status_icon} {p.name} — {p.price_rub} ₽ ({p.days} дн.)",
                callback_data=f"admin:plan_edit:{p.id}",
            ),
            InlineKeyboardButton(
                text="Скрыть" if p.is_active else "Показать",
                callback_data=f"admin:plan_toggle:{p.id}",
            ),
        )
    b.row(InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:home"))
    return b.as_markup()


def admin_promos_kb(promos: Sequence[PromoCode]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin:promo_create"))
    for pr in promos:
        b.row(
            InlineKeyboardButton(
                text=f"🎟 {pr.code} ({pr.type.value}: {pr.value}) [{pr.used_count}/{pr.max_uses}]",
                callback_data=f"admin:promo_del:{pr.id}",
            )
        )
    b.row(InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:home"))
    return b.as_markup()


def admin_broadcast_segment_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="👥 Всем пользователям", callback_data="admin:bc_seg:all"))
    b.row(
        InlineKeyboardButton(
            text="🟢 Только с активной подпиской", callback_data="admin:bc_seg:active"
        )
    )
    b.row(
        InlineKeyboardButton(
            text="🔴 Без активной подписки", callback_data="admin:bc_seg:inactive"
        )
    )
    b.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:home"))
    return b.as_markup()


def admin_broadcast_confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🚀 Запустить рассылку", callback_data="admin:bc_run"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="admin:home"),
    )
    return b.as_markup()


def admin_gift_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="🎯 Выдать напрямую (@username / ID / переслать)",
            callback_data="admin:gift_direct",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="🔗 Создать подарочную ссылку",
            callback_data="admin:gift_link",
        )
    )
    b.row(InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:home"))
    return b.as_markup()


def admin_gift_direct_days_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="7 дн.", callback_data="admin:gift_dir_days:7:3"),
        InlineKeyboardButton(text="30 дн.", callback_data="admin:gift_dir_days:30:3"),
        InlineKeyboardButton(text="90 дн.", callback_data="admin:gift_dir_days:90:3"),
    )
    b.row(
        InlineKeyboardButton(text="180 дн.", callback_data="admin:gift_dir_days:180:3"),
        InlineKeyboardButton(text="365 дн.", callback_data="admin:gift_dir_days:365:3"),
    )
    b.row(
        InlineKeyboardButton(
            text="♾ Безлимит навсегда (0 устройств лимит)",
            callback_data="admin:gift_dir_days:0:0",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="✏️ Ввести своё число дней",
            callback_data="admin:gift_dir_custom",
        )
    )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:gift_menu"))
    return b.as_markup()


def admin_gift_link_days_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="7 дн.", callback_data="admin:gift_lnk_days:7"),
        InlineKeyboardButton(text="30 дн.", callback_data="admin:gift_lnk_days:30"),
        InlineKeyboardButton(text="90 дн.", callback_data="admin:gift_lnk_days:90"),
    )
    b.row(
        InlineKeyboardButton(text="180 дн.", callback_data="admin:gift_lnk_days:180"),
        InlineKeyboardButton(text="365 дн.", callback_data="admin:gift_lnk_days:365"),
    )
    b.row(
        InlineKeyboardButton(
            text="♾ Безлимит навсегда (0 устр.)",
            callback_data="admin:gift_lnk_make:0:0",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="♾ Безлимит навсегда (выбрать лимит устр.)",
            callback_data="admin:gift_lnk_days:0",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="✏️ Своё число дней",
            callback_data="admin:gift_lnk_custom",
        )
    )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:gift_menu"))
    return b.as_markup()


def admin_gift_link_limit_kb(days: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="📱 3 устройства (стандарт)",
            callback_data=f"admin:gift_lnk_make:{days}:3",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="♾ Безлимит устройств (0)",
            callback_data=f"admin:gift_lnk_make:{days}:0",
        )
    )
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:gift_link"))
    return b.as_markup()


def admin_gift_link_created_kb(code: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="📨 Переслать подарок",
            url=build_gift_voucher_share_url(code),
        )
    )
    b.row(
        InlineKeyboardButton(text="➕ Ещё подарок", callback_data="admin:gift_menu"),
        InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:home"),
    )
    return b.as_markup()

