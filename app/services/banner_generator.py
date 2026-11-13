from __future__ import annotations

import hashlib
import io
import math
from functools import lru_cache
from pathlib import Path
from typing import Dict, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from app.core.config import settings

ROOT_DIR = Path(__file__).resolve().parents[2]
BANNER_BG_PATH = ROOT_DIR / "app" / "static" / "images" / "banner_bg.png"
MASCOT_PATH = ROOT_DIR / "app" / "static" / "images" / "ghostface_base.png"

FONT_BOLD_PATH = ROOT_DIR / "app" / "static" / "fonts" / "Montserrat-ExtraBold.ttf"
FONT_REG_PATH = ROOT_DIR / "app" / "static" / "fonts" / "Montserrat-Medium.ttf"
FALLBACK_BOLD_PATH = ROOT_DIR / "app" / "static" / "fonts" / "DejaVuSans-Bold.ttf"
FALLBACK_REG_PATH = ROOT_DIR / "app" / "static" / "fonts" / "DejaVuSans.ttf"

# Соль версии шаблона для гарантированного сброса старых file_id в media_cache
BANNER_TEMPLATE_VERSION = "v3_flow_clean"

# Кэш сгенерированных PNG-байтов и их SHA256 в оперативной памяти: (key, lang, bot_handle) -> (bytes, sha256)
_MEMORY_BANNER_BYTES: Dict[Tuple[str, str, str], Tuple[bytes, str]] = {}
# Кэш Telegram file_id в оперативной памяти: (key, lang, sha256) -> file_id
_MEMORY_FILE_IDS: Dict[Tuple[str, str, str], str] = {}

BANNER_SPECS: dict[str, dict[str, tuple[str, str]]] = {
    "main": {
        "ru": ("ТВОЯ ТЕНЬ В СЕТИ", "Быстрый доступ без ограничений и слежки"),
        "en": ("YOUR SHADOW ONLINE", "Fast access without limits or tracking"),
    },
    "trial": {
        "ru": ("ПРОБНЫЙ ДОСТУП", "Протестируй скорость и обход блокировок"),
        "en": ("TRIAL ACCESS", "Test the speed and bypass restrictions"),
    },
    "tariffs": {
        "ru": ("ТАРИФЫ И ДОСТУП", "Выбери подходящий период защиты"),
        "en": ("PLANS & ACCESS", "Choose the right protection period"),
    },
    "profile": {
        "ru": ("ЛИЧНЫЙ КАБИНЕТ", "Управление подпиской, ключом и устройствами"),
        "en": ("PERSONAL ACCOUNT", "Manage subscription, key, and devices"),
    },
    "connect": {
        "ru": ("ПОДКЛЮЧЕНИЕ", "Настройка в 1 клик для iOS, Android и ПК"),
        "en": ("CONNECTION", "1-click setup for iOS, Android, and PC"),
    },
    "devices": {
        "ru": ("УСТРОЙСТВА И LTE", "Стабильная работа на мобильном интернете"),
        "en": ("DEVICES & LTE", "Stable performance on mobile networks"),
    },
    "referral": {
        "ru": ("БОНУСЫ ЗА ДРУЗЕЙ", "Получай 15% с каждой оплаты приглашённых"),
        "en": ("INVITE & EARN", "Get 15% from every friend's payment"),
    },
    "promo": {
        "ru": ("ПРОМОКОД", "Активация бонусных дней и персональных скидок"),
        "en": ("PROMO CODE", "Activate bonus days and personal discounts"),
    },
    "support": {
        "ru": ("ПОДДЕРЖКА 24/7", "Поможем с настройкой за пару минут"),
        "en": ("24/7 SUPPORT", "We will help you set up in minutes"),
    },
}


@lru_cache(maxsize=32)
def load_font(bold: bool = True, size: int = 32) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Кэширует шрифты Montserrat (с фоллбэком на DejaVuSans) в оперативной памяти."""
    candidates = (
        [FONT_BOLD_PATH, FALLBACK_BOLD_PATH]
        if bold
        else [FONT_REG_PATH, FALLBACK_REG_PATH]
    )
    for font_path in candidates:
        try:
            if font_path.exists():
                return ImageFont.truetype(str(font_path), size=size)
        except Exception:
            continue
    return ImageFont.load_default()


@lru_cache(maxsize=8)
def _load_cached_mascot(size: int = 480) -> Image.Image:
    """Загружает и ресайзит маскот один раз на каждый размер (используется в QR-карте и как фоллбэк)."""
    if MASCOT_PATH.exists():
        try:
            with Image.open(MASCOT_PATH) as raw_img:
                rgba = raw_img.convert("RGBA")
                resized = rgba.resize((size, size), Image.Resampling.LANCZOS)
                rgba.close()
                return resized
        except Exception:
            pass

    fallback = Image.new("RGBA", (size, size), (7, 7, 9, 255))
    return fallback


def load_mascot_image(size: int = 480) -> Image.Image:
    return _load_cached_mascot(size).copy()


def _apply_left_soft_gradient(canvas: Image.Image, gradient_width: int = 780) -> None:
    """
    Накладывает мягкий затемняющий градиент только в левой части фона (x = 0..780),
    чтобы белый текст читался со 100% контрастом, а правая часть с маской оставалась нетронутой.
    """
    width, height = canvas.size
    gw = min(gradient_width, width)

    alpha_row = Image.new("L", (width, 1), 0)
    for x in range(gw):
        if x <= 320:
            # Мягкое равномерное притемнение в зоне начала текста
            alpha = 118
        else:
            # Плавный косинусный спад до 0 к x = 780
            progress = (x - 320) / float(gw - 320)
            alpha = int(118 * 0.5 * (1.0 + math.cos(math.pi * progress)))
        alpha_row.putpixel((x, 0), alpha)

    alpha_mask = alpha_row.resize((width, height), Image.Resampling.BILINEAR)
    alpha_row.close()

    dark_overlay = Image.new("RGBA", (width, height), (4, 5, 8, 255))
    dark_overlay.putalpha(alpha_mask)
    alpha_mask.close()

    canvas.alpha_composite(dark_overlay)
    dark_overlay.close()


@lru_cache(maxsize=2)
def _load_cached_banner_bg(width: int = 1280, height: int = 640) -> Image.Image:
    """
    Загружает полноразмерный фон app/static/images/banner_bg.png (ImageOps.fit 1280x640 без искажений)
    и накладывает мягкий левый градиент для читаемости текста.
    Если banner_bg.png отсутствует — использует фоллбэк на ghostface_base.png.
    """
    if BANNER_BG_PATH.exists():
        try:
            with Image.open(BANNER_BG_PATH) as raw_bg:
                rgba = raw_bg.convert("RGBA")
                fitted = ImageOps.fit(
                    rgba,
                    (width, height),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.45),
                )
                rgba.close()
                _apply_left_soft_gradient(fitted, gradient_width=780)
                return fitted
        except Exception:
            pass

    fallback_bg = Image.new("RGBA", (width, height), (8, 9, 12, 255))
    mascot_size = 540
    mascot = load_mascot_image(mascot_size)
    try:
        mascot_x = width - mascot_size - 36
        mascot_y = (height - mascot_size) // 2
        fallback_bg.paste(mascot, (mascot_x, mascot_y), mascot)
    finally:
        mascot.close()
    _apply_left_soft_gradient(fallback_bg, gradient_width=780)
    return fallback_bg


def _measure_text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> float:
    try:
        return float(draw.textlength(text, font=font))
    except Exception:
        bbox = draw.textbbox((0, 0), text, font=font)
        return float(bbox[2] - bbox[0])


def _draw_tracked_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
    tracking: int = 6,
) -> None:
    """Рисует текст с аккуратной разрядкой букв (letter-spacing) по метрике glyph advance."""
    curr_x, curr_y = float(xy[0]), float(xy[1])
    for ch in text:
        draw.text((curr_x, curr_y), ch, fill=fill, font=font)
        ch_w = _measure_text_width(draw, ch, font)
        curr_x += ch_w + tracking


def _wrap_text_pixels(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int = 660,
) -> list[str]:
    """Переносит строки строго по пиксельной ширине max_width (не более 660 px)."""
    words = text.split()
    if not words:
        return []

    lines: list[str] = []
    current_line = words[0]
    for word in words[1:]:
        candidate = f"{current_line} {word}"
        if _measure_text_width(draw, candidate, font) <= max_width:
            current_line = candidate
        else:
            lines.append(current_line)
            current_line = word
    lines.append(current_line)
    return lines


def render_banner_bytes(banner_key: str, lang: str = "ru") -> tuple[bytes, str]:
    """
    Генерирует широкоформатный баннер 1280x640 (2:1) на базе готового фона banner_bg.png:
    чистая минималистичная типографика Montserrat в левой безопасной зоне (x = 72..750),
    мягкая тень заголовка и реальный @bot_username из настроек.
    """
    norm_key = banner_key if banner_key in BANNER_SPECS else "main"
    norm_lang = "en" if lang == "en" else "ru"
    raw_bot_username = (
        getattr(settings, "BOT_USERNAME", None)
        or getattr(settings, "bot_username", None)
        or "GhostVPN_bot"
    )
    bot_handle = f"@{str(raw_bot_username).lstrip('@')}"
    cache_key = (norm_key, norm_lang, bot_handle)

    if cache_key in _MEMORY_BANNER_BYTES:
        return _MEMORY_BANNER_BYTES[cache_key]

    title, subtitle = BANNER_SPECS[norm_key][norm_lang]

    width, height = 1280, 640
    canvas = _load_cached_banner_bg(width, height).copy()
    shadow_layer: Image.Image | None = None
    blurred_shadow: Image.Image | None = None
    out_rgb: Image.Image | None = None

    try:
        draw = ImageDraw.Draw(canvas)

        safe_x = 72
        max_title_width = 678  # x = 72..750
        max_sub_width = 660    # строго внутри 660 px

        red_accent = (255, 30, 66, 255)      # #FF1E42
        pure_white = (255, 255, 255, 255)    # #FFFFFF
        sub_gray = (212, 212, 216, 255)      # #D4D4D8
        footer_gray = (156, 163, 175, 220)

        font_badge = load_font(bold=True, size=18)
        font_title = load_font(bold=True, size=56)
        if _measure_text_width(draw, title, font_title) > max_title_width:
            font_title = load_font(bold=True, size=52)

        font_sub = load_font(bold=False, size=28)
        font_footer = load_font(bold=False, size=20)

        badge_y = 162
        title_y = 204
        sub_y = 290
        footer_y = 565

        wrapped_sub_lines = _wrap_text_pixels(draw, subtitle, font_sub, max_width=max_sub_width)

        # Мягкая тёмная тень под заголовком и подзаголовком
        shadow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow_layer)
        shadow_draw.text((safe_x, title_y + 4), title, fill=(0, 0, 0, 185), font=font_title)
        curr_sub_y = sub_y
        for line in wrapped_sub_lines:
            shadow_draw.text((safe_x, curr_sub_y + 3), line, fill=(0, 0, 0, 150), font=font_sub)
            curr_sub_y += 38

        blurred_shadow = shadow_layer.filter(ImageFilter.GaussianBlur(radius=6))
        canvas.alpha_composite(blurred_shadow)
        draw = ImageDraw.Draw(canvas)

        # 1. Бейдж-акцент сверху слева с разрядкой букв: GHOSTVPN (без рамок)
        _draw_tracked_text(
            draw,
            (safe_x, badge_y),
            "GHOSTVPN",
            font=font_badge,
            fill=red_accent,
            tracking=6,
        )

        # 2. Крупный жирный белый заголовок раздела
        draw.text((safe_x, title_y), title, fill=pure_white, font=font_title)

        # 3. Лаконичный подзаголовок (#D4D4D8) с переносом строк внутри 660 px
        curr_sub_y = sub_y
        for line in wrapped_sub_lines:
            draw.text((safe_x, curr_sub_y), line, fill=sub_gray, font=font_sub)
            curr_sub_y += 38

        # 4. В самом низу слева (y = 565) — минималистичная подпись @{settings.bot_username}
        draw.text((safe_x, footer_y), bot_handle, fill=footer_gray, font=font_footer)

        out_rgb = canvas.convert("RGB")
        with io.BytesIO() as buf:
            out_rgb.save(buf, format="PNG", optimize=True)
            png_bytes = buf.getvalue()
    finally:
        if out_rgb is not None:
            out_rgb.close()
        if blurred_shadow is not None:
            blurred_shadow.close()
        if shadow_layer is not None:
            shadow_layer.close()
        canvas.close()

    sha256_hex = hashlib.sha256(
        f"{BANNER_TEMPLATE_VERSION}:{bot_handle}:".encode("utf-8") + png_bytes
    ).hexdigest()
    _MEMORY_BANNER_BYTES[cache_key] = (png_bytes, sha256_hex)
    return png_bytes, sha256_hex


def get_memory_cached_file_id(banner_key: str, lang: str, file_hash: str) -> str | None:
    return _MEMORY_FILE_IDS.get((banner_key, lang, file_hash))


def set_memory_cached_file_id(banner_key: str, lang: str, file_hash: str, file_id: str) -> None:
    _MEMORY_FILE_IDS[(banner_key, lang, file_hash)] = file_id


def invalidate_memory_cached_file_id(banner_key: str, lang: str) -> None:
    """Очищает закэшированный `file_id` в памяти, если Telegram признал его невалидным."""
    keys_to_remove = [k for k in _MEMORY_FILE_IDS if k[0] == banner_key and k[1] == lang]
    for k in keys_to_remove:
        _MEMORY_FILE_IDS.pop(k, None)
