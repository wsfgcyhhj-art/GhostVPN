from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Dict, Tuple

from PIL import Image, ImageDraw, ImageFont

ROOT_DIR = Path(__file__).resolve().parents[2]
MASCOT_PATH = ROOT_DIR / "app" / "static" / "images" / "ghostface_base.png"
FONT_BOLD_PATH = ROOT_DIR / "app" / "static" / "fonts" / "DejaVuSans-Bold.ttf"
FONT_REG_PATH = ROOT_DIR / "app" / "static" / "fonts" / "DejaVuSans.ttf"

# Кэш сгенерированных PNG-байтов и их SHA256 в оперативной памяти: (key, lang) -> (bytes, sha256)
_MEMORY_BANNER_BYTES: Dict[Tuple[str, str], Tuple[bytes, str]] = {}
# Кэш Telegram file_id в оперативной памяти: (key, lang, sha256) -> file_id
_MEMORY_FILE_IDS: Dict[Tuple[str, str, str], str] = {}

BANNER_SPECS: dict[str, dict[str, tuple[str, str, str]]] = {
    "main": {
        "ru": ("GHOSTVPN", "ТВОЯ ТЕНЬ В СЕТИ", "VLESS REALITY • НИДЕРЛАНДЫ 500 МБИТ/С • БЕЗ ЛИМИТОВ"),
        "en": ("GHOSTVPN", "YOUR DIGITAL SHADOW", "VLESS REALITY • NETHERLANDS 500 MBPS • NO LIMITS"),
    },
    "trial": {
        "ru": ("3 ДНЯ ЗА 1 ₽", "ПОЛНЫЙ ТЕСТ-ДРАЙВ", "МГНОВЕННЫЙ ЗАПУСК • ДО 3 УСТРОЙСТВ • ОБХОД ГЛУШИЛОК"),
        "en": ("3 DAYS FOR 1 ₽", "FULL TEST DRIVE", "INSTANT ACCESS • UP TO 3 DEVICES • ANTI-DPI READY"),
    },
    "tariffs": {
        "ru": ("МАГАЗИН ТАРИФОВ", "ВЫБЕРИ СВОЙ ПЕРИОД", "СКИДКА ДО 33% НА ГОД • СБП / STARS / CRYPTOBOT"),
        "en": ("TARIFF STORE", "CHOOSE YOUR PLAN", "UP TO 33% OFF YEARLY • CARDS / STARS / CRYPTOBOT"),
    },
    "profile": {
        "ru": ("ТВОЙ ПРОФИЛЬ", "СТАТУС ЗАЩИТЫ АКТИВЕН", "КОНТРОЛЬ ДНЕЙ • ТРАФИК • АВТОПРОДЛЕНИЕ • QR-КАРТА"),
        "en": ("YOUR PROFILE", "PROTECTION STATUS", "DAYS TRACKER • TRAFFIC • AUTO-RENEW • QR CARD"),
    },
    "connect": {
        "ru": ("ПОДКЛЮЧЕНИЕ В 1 КЛИК", "ОБХОД МОБИЛЬНЫХ СЕТЕЙ", "HAPP • V2RAYTUN • HIDDIFY • TLS FRAGMENT"),
        "en": ("1-CLICK CONNECT", "MOBILE ANTI-THROTTLING", "HAPP • V2RAYTUN • HIDDIFY • TLS FRAGMENT"),
    },
    "devices": {
        "ru": ("БОЛЬШЕ УСТРОЙСТВ", "ДО +10 АКТИВНЫХ СЛОТОВ", "СМАРТФОН • НОУТБУК • ПЛАНШЕТ • TV ПРИСТАВКА"),
        "en": ("MORE DEVICES", "UP TO +10 ACTIVE SLOTS", "PHONE • LAPTOP • TABLET • SMART TV"),
    },
    "referral": {
        "ru": ("ПАРТНЁРСКАЯ СЕТЬ", "+15% С КАЖДОЙ ОПЛАТЫ", "ПРИГЛАШАЙ ДРУЗЕЙ И ОПЛАЧИВАЙ VPN БОНУСАМИ ДО 100%"),
        "en": ("REFERRAL NETWORK", "+15% FROM EVERY PAYMENT", "INVITE FRIENDS & PAY UP TO 100% WITH BONUS BALANCE"),
    },
    "support": {
        "ru": ("ЦЕНТР ПОДДЕРЖКИ", "ПОМОЖЕМ ЗА 1 МИНУТУ", "ЧАСТЫЕ ВОПРОСЫ • НАСТРОЙКА МОБИЛЬНЫХ СЕТЕЙ • ЧАТ 24/7"),
        "en": ("SUPPORT CENTER", "FAST HELP IN 1 MINUTE", "FAQ SOLUTIONS • MOBILE ISP TUNING • 24/7 LIVE CHAT"),
    },
    "promo": {
        "ru": ("СЕКРЕТНЫЙ КОД", "ПЕРСОНАЛЬНЫЙ БОНУС", "АКТИВИРУЙ ПРОМОКОД НА БОНУСНЫЕ ДНИ ИЛИ СКИДКУ"),
        "en": ("SECRET CODE", "PERSONAL BONUS OFFER", "REDEEM PROMO CODE FOR BONUS DAYS OR DISCOUNT"),
    },
}


def load_font(bold: bool = True, size: int = 32) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_path = FONT_BOLD_PATH if bold else FONT_REG_PATH
    try:
        if font_path.exists():
            return ImageFont.truetype(str(font_path), size=size)
    except Exception:
        pass
    return ImageFont.load_default()


def load_mascot_image(size: int = 480) -> Image.Image:
    """Загружает `app/static/images/ghostface_base.png` или рисует стилизованный фоллбэк."""
    if MASCOT_PATH.exists():
        try:
            img = Image.open(MASCOT_PATH).convert("RGBA")
            img = img.resize((size, size), Image.Resampling.LANCZOS)
            return img
        except Exception:
            pass

    fallback = Image.new("RGBA", (size, size), (7, 7, 9, 255))
    d = ImageDraw.Draw(fallback)
    d.ellipse([(20, 20), (size - 20, size - 20)], outline=(255, 30, 66, 255), width=6)
    d.text((size // 4, size // 2 - 20), "GHOSTFACE", fill=(248, 249, 250, 255), font=load_font(True, 36))
    return fallback


def render_banner_bytes(banner_key: str, lang: str = "ru") -> tuple[bytes, str]:
    """
    Генерирует широкоформатный баннер 1280x640 (2:1) в стиле Ghostface Edition:
    угольно-чёрный фон #070709, неоново-алый #FF1E42, HUD-прицел, глитч-полосы и маскот.
    Возвращает кортеж (png_bytes, sha256_hex).
    """
    norm_key = banner_key if banner_key in BANNER_SPECS else "main"
    norm_lang = "en" if lang == "en" else "ru"
    cache_key = (norm_key, norm_lang)

    if cache_key in _MEMORY_BANNER_BYTES:
        return _MEMORY_BANNER_BYTES[cache_key]

    title, subtitle, tag_line = BANNER_SPECS[norm_key][norm_lang]

    width, height = 1280, 640
    canvas = Image.new("RGBA", (width, height), (7, 7, 9, 255))
    draw = ImageDraw.Draw(canvas)

    # 1. Киберпанк-сетка и HUD-линии на фоне
    grid_color = (22, 22, 30, 255)
    for x in range(0, width, 64):
        draw.line([(x, 0), (x, height)], fill=grid_color, width=1)
    for y in range(0, height, 64):
        draw.line([(0, y), (width, y)], fill=grid_color, width=1)

    # 2. Неоново-алая внешняя HUD-рамка и угловые скобы (#FF1E42)
    red_neon = (255, 30, 66, 255)
    red_dim = (140, 18, 42, 255)
    ghost_white = (248, 249, 250, 255)
    muted_gray = (160, 168, 184, 255)

    draw.rounded_rectangle(
        [(24, 24), (width - 24, height - 24)],
        radius=20,
        outline=red_dim,
        width=2,
    )
    # Угловые кибер-скобы
    corner_len = 56
    for cx, cy, dx, dy in [
        (24, 24, 1, 1),
        (width - 24, 24, -1, 1),
        (24, height - 24, 1, -1),
        (width - 24, height - 24, -1, -1),
    ]:
        draw.line([(cx, cy), (cx + dx * corner_len, cy)], fill=red_neon, width=5)
        draw.line([(cx, cy), (cx, cy + dy * corner_len)], fill=red_neon, width=5)

    # 3. Размещение маскота Ghostface справа с круговым HUD-прицелом
    mascot_size = 520
    mascot = load_mascot_image(mascot_size)
    mascot_x = width - mascot_size - 48
    mascot_y = (height - mascot_size) // 2

    # Круглая маска и неоновое кольцо вокруг маскота
    mask = Image.new("L", (mascot_size, mascot_size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse([(8, 8), (mascot_size - 8, mascot_size - 8)], fill=255)
    canvas.paste(mascot, (mascot_x, mascot_y), mask)

    draw.ellipse(
        [(mascot_x + 4, mascot_y + 4), (mascot_x + mascot_size - 4, mascot_y + mascot_size - 4)],
        outline=red_neon,
        width=4,
    )

    # 4. Цифровые глитч-полосы слева
    draw.rectangle([(64, 88), (240, 94)], fill=red_neon)
    draw.rectangle([(252, 88), (300, 94)], fill=ghost_white)

    # 5. Текстовый блок раздела
    font_badge = load_font(bold=True, size=20)
    font_title = load_font(bold=True, size=56)
    font_sub = load_font(bold=True, size=38)
    font_tag = load_font(bold=False, size=21)

    draw.text((64, 114), f"// GHOSTFACE EDITION • {norm_key.upper()}", fill=red_neon, font=font_badge)

    # Эффект хроматического/алого сдвига (glitch shadow) для главного заголовка
    draw.text((67, 178), title, fill=red_neon, font=font_title)
    draw.text((64, 175), title, fill=ghost_white, font=font_title)

    # Подзаголовок в неоновой плашке
    sub_box_y = 265
    draw.rounded_rectangle(
        [(64, sub_box_y), (710, sub_box_y + 74)],
        radius=12,
        fill=(24, 10, 16, 255),
        outline=red_neon,
        width=2,
    )
    draw.text((86, sub_box_y + 15), subtitle, fill=red_neon, font=font_sub)

    # Нижняя строка преимуществ
    draw.text((64, 385), tag_line, fill=muted_gray, font=font_tag)

    # Нижний статус-бар HUD
    draw.rounded_rectangle(
        [(64, 475), (650, 540)],
        radius=10,
        fill=(14, 16, 22, 255),
        outline=(45, 52, 68, 255),
        width=2,
    )
    draw.ellipse([(88, 498), (106, 516)], fill=(255, 30, 66, 255))
    draw.text(
        (122, 495),
        "@GhostFVPN_bot  •  STEALTH VLESS REALITY PROTOCOL",
        fill=ghost_white,
        font=font_badge,
    )

    out_rgb = canvas.convert("RGB")
    buf = io.BytesIO()
    out_rgb.save(buf, format="PNG", optimize=True)
    png_bytes = buf.getvalue()
    sha256_hex = hashlib.sha256(png_bytes).hexdigest()

    _MEMORY_BANNER_BYTES[cache_key] = (png_bytes, sha256_hex)
    return png_bytes, sha256_hex


def get_memory_cached_file_id(banner_key: str, lang: str, file_hash: str) -> str | None:
    return _MEMORY_FILE_IDS.get((banner_key, lang, file_hash))


def set_memory_cached_file_id(banner_key: str, lang: str, file_hash: str, file_id: str) -> None:
    _MEMORY_FILE_IDS[(banner_key, lang, file_hash)] = file_id
