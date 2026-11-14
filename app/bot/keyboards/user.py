from __future__ import annotations

from decimal import Decimal
from typing import Sequence
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core.config import settings
from app.core.i18n import t
from app.db.models import Plan, Subscription, User
from app.services.vpn import calculate_plan_renewal_price_with_slots

SHARE_FRIEND_TEXT = (
    "Держи бесплатный доступ к быстрому интернету без рекламы — "
    "работает и на домашней сети, и на мобильной связи, "
    "российские банки открываются без отключения"
)


def build_friend_share_url(user_telegram_id: int) -> str:
    """Формирует ссылку t.me/share/url для пересылки приглашения другу в 1 клик."""
    bot_username = (settings.BOT_USERNAME or "GhostVPN_Bot").lstrip("@")
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_telegram_id}"
    return (
        f"https://t.me/share/url?url={quote(ref_link, safe='')}"
        f"&text={quote(SHARE_FRIEND_TEXT, safe='')}"
    )


def main_menu_kb(user: User, is_admin: bool = False) -> InlineKeyboardMarkup:
    lang = user.language or "ru"
    b = InlineKeyboardBuilder()
    webapp_url = f"{settings.WEBHOOK_BASE_URL.rstrip('/')}/webapp"

    if webapp_url.startswith("https://"):
        b.row(
            InlineKeyboardButton(
                text=t("btn_webapp", lang),
                web_app=WebAppInfo(url=webapp_url),
            )
        )

    if not user.has_used_trial:
        b.row(
            InlineKeyboardButton(
                text=t(
                    "btn_trial",
                    lang,
                    days=settings.TRIAL_DAYS,
                    stars=settings.TRIAL_PRICE_STARS,
                ),
                callback_data="trial:start",
            )
        )
    b.row(
        InlineKeyboardButton(text=t("btn_buy", lang), callback_data="plans:list"),
        InlineKeyboardButton(text=t("btn_my_sub", lang), callback_data="profile:sub"),
    )
    b.row(
        InlineKeyboardButton(
            text=t("btn_ref", lang, percent=settings.REFERRAL_PERCENT),
            callback_data="profile:ref",
        ),
        InlineKeyboardButton(text=t("btn_promo", lang), callback_data="promo:enter"),
    )
    b.row(
        InlineKeyboardButton(text=t("btn_faq", lang), callback_data="help:faq"),
        InlineKeyboardButton(text=t("btn_support", lang), url=settings.SUPPORT_URL),
    )
    b.row(InlineKeyboardButton(text=t("btn_lang", lang), callback_data="profile:toggle_lang"))
    if is_admin:
        b.row(InlineKeyboardButton(text=t("btn_admin", lang), callback_data="admin:home"))
    return b.as_markup()


def channel_check_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📢 Подписаться на канал", url=settings.REQUIRED_CHANNEL_URL))
    b.row(InlineKeyboardButton(text="✅ Я подписался (Проверить)", callback_data="trial:verify"))
    b.row(InlineKeyboardButton(text=t("btn_back_menu", lang), callback_data="menu:home"))
    return b.as_markup()


def trial_payment_kb(user_balance: Decimal, lang: str = "ru") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    trial_rub = Decimal(str(settings.TRIAL_PRICE_RUB)).quantize(Decimal("0.01"))
    if user_balance >= trial_rub:
        b.row(
            InlineKeyboardButton(
                text=f"💰 Активировать с баланса ({trial_rub} ₽)",
                callback_data="trial:pay:BALANCE",
            )
        )
    b.row(
        InlineKeyboardButton(
            text=f"💳 СБП / Карта РФ — {trial_rub} ₽",
            callback_data="trial:pay:RUB_GATEWAY",
        )
    )
    b.row(
        InlineKeyboardButton(
            text=f"⭐️ Telegram Stars — {settings.TRIAL_PRICE_STARS} ⭐️",
            callback_data="trial:pay:STARS",
        )
    )
    b.row(InlineKeyboardButton(text=t("btn_back_menu", lang), callback_data="menu:home"))
    return b.as_markup()


def subscription_card_kb(user: User, sub: Subscription | None) -> InlineKeyboardMarkup:
    lang = user.language or "ru"
    b = InlineKeyboardBuilder()
    if sub is not None:
        base_web = settings.WEBHOOK_BASE_URL.rstrip("/")
        b.row(
            InlineKeyboardButton(
                text=t("btn_connect_oneclick", lang),
                url=f"{base_web}/connect/happ/{sub.sub_id}",
            )
        )
        b.row(
            InlineKeyboardButton(
                text="📲 v2rayTun",
                url=f"{base_web}/connect/v2raytun/{sub.sub_id}",
            ),
            InlineKeyboardButton(
                text="📲 Hiddify",
                url=f"{base_web}/connect/hiddify/{sub.sub_id}",
            ),
        )
        b.row(
            InlineKeyboardButton(text=t("btn_qr_card", lang), callback_data="profile:qr"),
            InlineKeyboardButton(text=t("btn_mobile_boost", lang), callback_data="help:mobile_boost"),
        )
        b.row(
            InlineKeyboardButton(text=t("btn_add_device", lang), callback_data="devices:menu"),
            InlineKeyboardButton(text="🎛 Управлять слотами", callback_data="devices:manage"),
        )
        b.row(
            InlineKeyboardButton(
                text=t("btn_auto_renew_on" if user.auto_renew else "btn_auto_renew_off", lang),
                callback_data="profile:toggle_autorenew",
            )
        )
        b.row(
            InlineKeyboardButton(text=t("btn_reissue", lang), callback_data="profile:reissue_confirm"),
            InlineKeyboardButton(text="📖 Приложения", callback_data="help:instructions"),
        )
        if not sub.is_active and sub.ip_violations_count >= 3:
            b.row(
                InlineKeyboardButton(
                    text="🔓 Я отключил лишние устройства (Разблокировать)",
                    callback_data="profile:unlock_ip",
                )
            )
    b.row(
        InlineKeyboardButton(
            text=t("btn_gift_friend", lang),
            url=build_friend_share_url(user.telegram_id),
        )
    )
    b.row(
        InlineKeyboardButton(text=t("btn_buy", lang), callback_data="plans:list"),
        InlineKeyboardButton(text=t("btn_support", lang), url=settings.SUPPORT_URL),
    )
    b.row(InlineKeyboardButton(text=t("btn_back_menu", lang), callback_data="menu:home"))
    return b.as_markup()


def extra_device_buy_kb(
    price_rub: Decimal, user_balance_rub: Decimal, lang: str = "ru"
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if user_balance_rub >= price_rub:
        b.row(
            InlineKeyboardButton(
                text=f"💰 Оплатить с баланса ({price_rub} ₽)",
                callback_data="devices:pay:BALANCE",
            )
        )
    b.row(
        InlineKeyboardButton(
            text=f"💳 СБП / Карта РФ ({price_rub} ₽)",
            callback_data="devices:pay:RUB_GATEWAY",
        )
    )
    b.row(
        InlineKeyboardButton(
            text="⭐️ Telegram Stars",
            callback_data="devices:pay:STARS",
        ),
        InlineKeyboardButton(
            text="💎 CryptoBot",
            callback_data="devices:pay:CRYPTOBOT",
        ),
    )
    b.row(
        InlineKeyboardButton(text="🎛 Управлять слотами", callback_data="devices:manage"),
        InlineKeyboardButton(text="⬅️ К подписке", callback_data="profile:sub"),
    )
    return b.as_markup()


def manage_slots_kb(sub: Subscription, lang: str = "ru") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if sub.extra_devices > 0:
        b.row(
            InlineKeyboardButton(
                text="➖ Убрать 1 доп. слот",
                callback_data=f"devices:set_extra:{sub.extra_devices - 1}",
            )
        )
        b.row(
            InlineKeyboardButton(
                text=f"🔄 Сбросить до базовых {settings.DEFAULT_LIMIT_IP} устройств",
                callback_data="devices:set_extra:0",
            )
        )
    b.row(InlineKeyboardButton(text=t("btn_add_device", lang), callback_data="devices:menu"))
    b.row(InlineKeyboardButton(text="⬅️ К подписке", callback_data="profile:sub"))
    return b.as_markup()


def reissue_confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Да, перевыпустить ключ", callback_data="profile:reissue_do"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="profile:sub"),
    )
    return b.as_markup()


def plans_list_kb(
    plans: Sequence[Plan],
    discount_percent: int = 0,
    extra_devices: int = 0,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for plan in plans:
        full_price = calculate_plan_renewal_price_with_slots(plan, extra_devices=extra_devices)
        if discount_percent > 0:
            discounted = (full_price * Decimal(100 - discount_percent) / Decimal(100)).quantize(
                Decimal("0.01")
            )
            label = f"🔥 {plan.name} — {discounted} ₽ (-{discount_percent}%)"
        else:
            slot_note = f" (+{extra_devices} сл.)" if extra_devices > 0 else ""
            label = f"⚡️ {plan.name}{slot_note} — {full_price} ₽ ({plan.days} дн.)"
        b.row(InlineKeyboardButton(text=label, callback_data=f"plan:select:{plan.id}"))
    if extra_devices > 0:
        b.row(InlineKeyboardButton(text="🎛 Управлять слотами", callback_data="devices:manage"))
    b.row(InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="promo:enter"))
    b.row(InlineKeyboardButton(text=t("btn_back_menu", lang), callback_data="menu:home"))
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
            text="💳 СБП / Карта РФ (Мгновенно)",
            callback_data=f"pay:create:RUB_GATEWAY:{plan_id}:{flag}",
        )
    )
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
    b.row(InlineKeyboardButton(text="⬅️ К выбору тарифа", callback_data="plans:list"))
    return b.as_markup()


def invoice_check_kb(tx_id: int, payment_url: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔗 Перейти к оплате", url=payment_url))
    b.row(InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"pay:check:{tx_id}"))
    b.row(InlineKeyboardButton(text="⬅️ К тарифам", callback_data="plans:list"))
    return b.as_markup()


def instructions_kb(lang: str = "ru") -> InlineKeyboardMarkup:
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
        InlineKeyboardButton(text=t("btn_mobile_boost", lang), callback_data="help:mobile_boost")
    )
    b.row(
        InlineKeyboardButton(text="🔑 Моя подписка", callback_data="profile:sub"),
        InlineKeyboardButton(text=t("btn_back_menu", lang), callback_data="menu:home"),
    )
    return b.as_markup()


def faq_menu_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📶 Не грузит на мобильном интернете", callback_data="help:mobile_boost"))
    b.row(InlineKeyboardButton(text="📲 Как подключить на iPhone / Android / ПК", callback_data="help:instructions"))
    b.row(InlineKeyboardButton(text="💳 Вопросы по оплате и автопродлению", callback_data="help:faq_billing"))
    b.row(InlineKeyboardButton(text="💬 Связаться со специалистом 24/7", url=settings.SUPPORT_URL))
    b.row(InlineKeyboardButton(text=t("btn_back_menu", lang), callback_data="menu:home"))
    return b.as_markup()


def mobile_boost_kb(sub: Subscription | None, lang: str = "ru") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if sub is not None:
        base_web = settings.WEBHOOK_BASE_URL.rstrip("/")
        b.row(
            InlineKeyboardButton(
                text=t("btn_connect_oneclick", lang),
                url=f"{base_web}/connect/happ/{sub.sub_id}",
            )
        )
    b.row(
        InlineKeyboardButton(text="🔑 К подписке", callback_data="profile:sub"),
        InlineKeyboardButton(text="❓ В раздел Помощи", callback_data="help:faq"),
    )
    return b.as_markup()


def referral_kb(user: User) -> InlineKeyboardMarkup:
    lang = user.language or "ru"
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(
            text=t("btn_gift_friend", lang),
            url=build_friend_share_url(user.telegram_id),
        )
    )
    b.row(InlineKeyboardButton(text=t("btn_back_menu", lang), callback_data="menu:home"))
    return b.as_markup()


def back_to_menu_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=t("btn_back_menu", lang), callback_data="menu:home"))
    return b.as_markup()
