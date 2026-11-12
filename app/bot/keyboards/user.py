from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core.config import settings
from app.db.models import Plan, Subscription, User


def main_menu_kb(user: User, is_admin: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if not user.has_used_trial:
        b.row(
            InlineKeyboardButton(
                text=f"🎁 Попробовать бесплатно ({settings.TRIAL_DAYS} дня)",
                callback_data="trial:start",
            )
        )
    b.row(
        InlineKeyboardButton(text="💳 Купить / Продлить VPN", callback_data="plans:list"),
        InlineKeyboardButton(text="🔑 Моя подписка", callback_data="profile:sub"),
    )
    b.row(
        InlineKeyboardButton(text="👥 Реферальная программа", callback_data="profile:ref"),
        InlineKeyboardButton(text="🎟 Промокод", callback_data="promo:enter"),
    )
    b.row(
        InlineKeyboardButton(text="📖 Инструкции", callback_data="help:instructions"),
        InlineKeyboardButton(text="💬 Поддержка", url=settings.SUPPORT_URL),
    )
    if is_admin:
        b.row(InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin:home"))
    return b.as_markup()


def channel_check_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📢 Подписаться на канал", url=settings.REQUIRED_CHANNEL_URL))
    b.row(InlineKeyboardButton(text="✅ Я подписался (Проверить)", callback_data="trial:verify"))
    b.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:home"))
    return b.as_markup()


def subscription_card_kb(sub: Subscription | None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if sub is not None:
        base_web = settings.WEBHOOK_BASE_URL.rstrip("/")
        b.row(
            InlineKeyboardButton(
                text="📲 Happ (1 клик)",
                url=f"{base_web}/connect/happ/{sub.sub_id}",
            ),
            InlineKeyboardButton(
                text="📲 v2rayTun (1 клик)",
                url=f"{base_web}/connect/v2raytun/{sub.sub_id}",
            ),
        )
        b.row(
            InlineKeyboardButton(
                text="📲 Hiddify (1 клик)",
                url=f"{base_web}/connect/hiddify/{sub.sub_id}",
            ),
            InlineKeyboardButton(text="📷 QR-код", callback_data="profile:qr"),
        )
        b.row(
            InlineKeyboardButton(text="🔄 Сбросить ключ", callback_data="profile:reissue_confirm"),
            InlineKeyboardButton(text="📖 Инструкция по установке", callback_data="help:instructions"),
        )
    b.row(
        InlineKeyboardButton(text="💳 Продлить подписку", callback_data="plans:list"),
        InlineKeyboardButton(text="💬 Поддержка", url=settings.SUPPORT_URL),
    )
    b.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:home"))
    return b.as_markup()


def reissue_confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Да, перевыпустить ключ", callback_data="profile:reissue_do"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="profile:sub"),
    )
    return b.as_markup()


def plans_list_kb(plans: Sequence[Plan], discount_percent: int = 0) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for plan in plans:
        price = plan.price_rub
        if discount_percent > 0:
            discounted = (price * Decimal(100 - discount_percent) / Decimal(100)).quantize(
                Decimal("0.01")
            )
            label = f"⚡️ {plan.name} — {discounted} ₽ (скидка -{discount_percent}%)"
        else:
            label = f"⚡️ {plan.name} — {price} ₽ ({plan.days} дн.)"
        b.row(InlineKeyboardButton(text=label, callback_data=f"plan:select:{plan.id}"))
    b.row(InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="promo:enter"))
    b.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:home"))
    return b.as_markup()


def payment_methods_kb(
    plan_id: int,
    final_price_rub: Decimal,
    user_balance_rub: Decimal,
    use_balance: bool = False,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()

    if user_balance_rub > Decimal("0.00"):
        if user_balance_rub >= final_price_rub:
            b.row(
                InlineKeyboardButton(
                    text=f"💰 Оплатить полностью с баланса ({final_price_rub} ₽)",
                    callback_data=f"pay:balance_full:{plan_id}",
                )
            )
        else:
            toggle_text = (
                f"✅ Списать баланс ({user_balance_rub} ₽) как скидку"
                if use_balance
                else f"☑️ Использовать баланс ({user_balance_rub} ₽) как скидку"
            )
            b.row(
                InlineKeyboardButton(
                    text=toggle_text,
                    callback_data=f"plan:select:{plan_id}:{'0' if use_balance else '1'}",
                )
            )

    flag = "1" if use_balance else "0"
    b.row(
        InlineKeyboardButton(
            text="⭐️ Telegram Stars",
            callback_data=f"pay:create:STARS:{plan_id}:{flag}",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="💎 CryptoBot (USDT / TON / BTC)",
            callback_data=f"pay:create:CRYPTOBOT:{plan_id}:{flag}",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="💳 Карта РФ / СБП",
            callback_data=f"pay:create:RUB_GATEWAY:{plan_id}:{flag}",
        )
    )
    b.row(InlineKeyboardButton(text="⬅️ К выбору тарифа", callback_data="plans:list"))
    return b.as_markup()


def invoice_check_kb(tx_id: int, payment_url: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔗 Перейти к оплате", url=payment_url))
    b.row(InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"pay:check:{tx_id}"))
    b.row(InlineKeyboardButton(text="⬅️ К тарифам", callback_data="plans:list"))
    return b.as_markup()


def instructions_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text="🍎 iOS (Happ)",
            url="https://apps.apple.com/app/happ-proxy-utility/id6504287215",
        ),
        InlineKeyboardButton(
            text="🍎 iOS (v2rayTun)",
            url="https://apps.apple.com/app/v2raytun/id6476628951",
        ),
    )
    b.row(
        InlineKeyboardButton(
            text="🤖 Android (v2rayTun)",
            url="https://play.google.com/store/apps/details?id=com.v2raytun.android",
        ),
        InlineKeyboardButton(
            text="🤖 Android (Hiddify)",
            url="https://play.google.com/store/apps/details?id=app.hiddify.com",
        ),
    )
    b.row(
        InlineKeyboardButton(
            text="💻 Windows / macOS (Hiddify)",
            url="https://github.com/hiddify/hiddify-next/releases/latest",
        )
    )
    b.row(
        InlineKeyboardButton(text="🔑 Моя подписка", callback_data="profile:sub"),
        InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:home"),
    )
    return b.as_markup()


def back_to_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:home"))
    return b.as_markup()
