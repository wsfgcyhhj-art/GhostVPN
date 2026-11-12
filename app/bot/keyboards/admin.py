from __future__ import annotations

from typing import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.models import Plan, PromoCode, User


def admin_home_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🔄 Обновить дашборд", callback_data="admin:home"),
        InlineKeyboardButton(text="🔍 Поиск пользователя", callback_data="admin:user_search"),
    )
    b.row(
        InlineKeyboardButton(text="📦 Тарифы", callback_data="admin:plans"),
        InlineKeyboardButton(text="🎟 Промокоды", callback_data="admin:promos"),
    )
    b.row(InlineKeyboardButton(text="📢 Рассылка (Broadcast)", callback_data="admin:broadcast"))
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
            text="🔄 Сбросить ключ (3X-UI)", callback_data=f"admin:u_reissue:{target_user.id}"
        ),
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
