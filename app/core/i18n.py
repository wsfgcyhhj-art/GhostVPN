from __future__ import annotations

from typing import Any

TRANSLATIONS: dict[str, dict[str, str]] = {
    "ru": {
        "welcome_title": "👻 <b>Привет! Это GhostVPN — твой быстрый и заботливый интернет без границ!</b>\n\n"
        "Мы сделали всё максимально просто: нажал одну кнопку — и всё летает (YouTube в 4K, Instagram, звонки и сайты без тормозов).\n\n"
        "✨ <b>Почему с нами уютно:</b>\n"
        "• 🇳🇱 Быстрый сервер в Нидерландах (до 500 Мбит/с)\n"
        "• 📱 До <b>{limit_ip} устройств</b> одновременно (телефон, ноутбук, планшет)\n"
        "• 🛡 Обход глушилок на мобильном интернете (TLS Fragment)\n\n"
        "📊 <b>Твой статус:</b> {status_line}\n"
        "💰 <b>Бонусный баланс:</b> <b>{balance} ₽</b>\n\n"
        "<i>Продолжая пользоваться ботом, ты принимаешь <a href='{terms_url}'>правила сервиса</a> (без торрентов и спама).</i>",
        "status_active": "🟢 Подписка активна",
        "status_expired": "🔴 Подписка закончилась",
        "status_none": "⚪️ Пока нет подписки",
        "btn_trial": "🔥 Тест на {days} дня за 1 ₽ / {stars} ⭐️",
        "btn_buy": "💳 Тарифы и продление",
        "btn_my_sub": "🔑 Моя подписка и ключ",
        "btn_webapp": "📱 Открыть Mini App (Личный кабинет)",
        "btn_ref": "🎁 Друзьям (+{percent}%)",
        "btn_promo": "🎟 Промокод",
        "btn_faq": "❓ Помощь и частые вопросы",
        "btn_support": "💬 Написать в поддержку",
        "btn_lang": "🌐 Язык: 🇷🇺 RU → 🇬🇧 EN",
        "btn_admin": "🛠 Админ-панель",
        "btn_back_menu": "⬅️ В главное меню",
        "lang_switched": "🇷🇺 Язык переключён на русский!",
        "rate_limit_msg": "⏳ Пожалуйста, не нажимай кнопки так быстро, дай мне секунду :)",
        "sub_none_text": "🔑 <b>Твой личный кабинет</b>\n\n"
        "У тебя пока нет активной подписки.\n"
        "Попробуй наш <b>3-дневный тест всего за 1 ₽</b> или выбери удобный тариф со скидкой!",
        "btn_add_device": "➕ Добавить устройство (+1 слот)",
        "btn_auto_renew_on": "🔄 Автопродление: ВКЛ ✅",
        "btn_auto_renew_off": "🔄 Автопродление: ВЫКЛ ❌",
        "btn_qr_card": "🖼 Красивая QR-карточка",
        "btn_reissue": "🔄 Обновить ссылку",
        "btn_mobile_boost": "📶 Не грузит на мобильном? (Решение)",
    },
    "en": {
        "welcome_title": "👻 <b>Welcome to GhostVPN — your fast, friendly, borderless internet!</b>\n\n"
        "We made everything effortless: tap one button and enjoy 4K streaming, calls, and websites without limits.\n\n"
        "✨ <b>Why you'll love GhostVPN:</b>\n"
        "• 🇳🇱 Ultra-fast Netherlands location (up to 500 Mbps)\n"
        "• 📱 Up to <b>{limit_ip} devices</b> at the same time\n"
        "• 🛡 Mobile ISP anti-throttling (TLS Fragment ready)\n\n"
        "📊 <b>Your status:</b> {status_line}\n"
        "💰 <b>Bonus balance:</b> <b>{balance} ₽</b>\n\n"
        "<i>By using this bot, you agree to our <a href='{terms_url}'>Terms of Service</a> (no torrents or spam).</i>",
        "status_active": "🟢 Active subscription",
        "status_expired": "🔴 Expired",
        "status_none": "⚪️ No active subscription",
        "btn_trial": "🔥 {days}-Day Trial for 1 ₽ / {stars} ⭐️",
        "btn_buy": "💳 Plans & Renewal",
        "btn_my_sub": "🔑 My Subscription & Key",
        "btn_webapp": "📱 Open Mini App (Dashboard)",
        "btn_ref": "🎁 Invite Friends (+{percent}%)",
        "btn_promo": "🎟 Promo Code",
        "btn_faq": "❓ Help & FAQ",
        "btn_support": "💬 Contact Support",
        "btn_lang": "🌐 Language: 🇬🇧 EN → 🇷🇺 RU",
        "btn_admin": "🛠 Admin Panel",
        "btn_back_menu": "⬅️ Back to Menu",
        "lang_switched": "🇬🇧 Language switched to English!",
        "rate_limit_msg": "⏳ Please slow down a bit, give me a second :)",
        "sub_none_text": "🔑 <b>Your Personal Dashboard</b>\n\n"
        "You don't have an active subscription yet.\n"
        "Try our <b>3-day trial for just 1 ₽</b> or pick a discounted plan!",
        "btn_add_device": "➕ Add Device (+1 slot)",
        "btn_auto_renew_on": "🔄 Auto-renew: ON ✅",
        "btn_auto_renew_off": "🔄 Auto-renew: OFF ❌",
        "btn_qr_card": "🖼 Branded QR Card",
        "btn_reissue": "🔄 Re-issue Link",
        "btn_mobile_boost": "📶 Mobile Network Fix (Fragment)",
    },
}


def t(key: str, lang: str = "ru", **kwargs: Any) -> str:
    norm_lang = "en" if lang == "en" else "ru"
    catalog = TRANSLATIONS.get(norm_lang, TRANSLATIONS["ru"])
    template = catalog.get(key) or TRANSLATIONS["ru"].get(key, key)
    if kwargs:
        return template.format(**kwargs)
    return template


def render_progress_bar(days_left: int, total_days: int, width: int = 10) -> str:
    """Формирует визуальный текстовый прогресс-бар: ▓▓▓▓▓▓░░░░ 18 из 30 дн."""
    safe_total = max(1, total_days)
    safe_left = max(0, min(days_left, safe_total))
    filled = round((safe_left / safe_total) * width)
    filled = max(0, min(width, filled))
    bar = ("▓" * filled) + ("░" * (width - filled))
    return bar
