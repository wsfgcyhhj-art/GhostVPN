from __future__ import annotations

import io
import qrcode
from PIL import Image, ImageDraw

from app.services.banner_generator import load_font, load_mascot_image


def generate_branded_qr_card(
    subscription_url: str,
    sub_id: str,
    expires_str: str,
    limit_ip: int = 3,
) -> bytes:
    """
    Генерирует брендированную PNG-карточку GhostVPN (Ghostface Edition) в памяти (BytesIO):
    угольно-чёрный фон #070709, неоново-алая рамка #FF1E42, маскот Ghostface в шапке и QR-код.
    Гарантированно закрывает все временные объекты Image и BytesIO.
    """
    width, height = 680, 920
    bg = Image.new("RGBA", (width, height), color=(7, 7, 9, 255))
    mascot: Image.Image | None = None
    mask: Image.Image | None = None
    qr_raw: Image.Image | None = None
    qr_img: Image.Image | None = None
    out_rgb: Image.Image | None = None

    try:
        draw = ImageDraw.Draw(bg)

        red_neon = (255, 30, 66, 255)
        ghost_white = (248, 249, 250, 255)
        muted_gray = (160, 168, 184, 255)

        draw.rounded_rectangle(
            [(20, 20), (width - 20, height - 20)],
            radius=26,
            fill=(14, 14, 18, 255),
            outline=red_neon,
            width=3,
        )

        draw.rounded_rectangle(
            [(44, 44), (width - 44, 196)],
            radius=18,
            fill=(7, 7, 9, 255),
            outline=(90, 18, 34, 255),
            width=2,
        )

        mascot = load_mascot_image(116)
        mask = Image.new("L", (116, 116), 0)
        ImageDraw.Draw(mask).ellipse([(2, 2), (114, 114)], fill=255)
        bg.paste(mascot, (64, 62), mask)
        draw.ellipse([(64, 62), (180, 178)], outline=red_neon, width=3)

        font_title = load_font(bold=True, size=30)
        font_sub = load_font(bold=True, size=18)
        font_meta = load_font(bold=False, size=17)

        draw.text((202, 72), "GHOSTVPN // STEALTH KEY", fill=ghost_white, font=font_title)
        draw.text((202, 114), "VLESS REALITY • TLS FRAGMENT", fill=red_neon, font=font_sub)
        draw.text((202, 144), "Твоя цифровая тень в сети", fill=muted_gray, font=font_meta)

        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2,
        )
        qr.add_data(subscription_url)
        qr.make(fit=True)
        qr_made = qr.make_image(fill_color="#070709", back_color="#F8F9FA")
        qr_raw = qr_made.convert("RGBA")
        qr_size = 420
        qr_img = qr_raw.resize((qr_size, qr_size), Image.Resampling.NEAREST)

        qr_x = (width - qr_size) // 2
        qr_y = 232
        draw.rounded_rectangle(
            [(qr_x - 16, qr_y - 16), (qr_x + qr_size + 16, qr_y + qr_size + 16)],
            radius=20,
            fill=ghost_white,
            outline=red_neon,
            width=4,
        )
        bg.paste(qr_img, (qr_x, qr_y))

        info_y = qr_y + qr_size + 42
        draw.rounded_rectangle(
            [(44, info_y), (width - 44, height - 44)],
            radius=18,
            fill=(7, 7, 9, 255),
            outline=red_neon,
            width=2,
        )

        draw.text((72, info_y + 24), f"КЛЮЧ ДОСТУПА:  {sub_id}", fill=ghost_white, font=font_sub)
        draw.text(
            (72, info_y + 62),
            f"АКТИВЕН ДО:  {expires_str}",
            fill=red_neon,
            font=font_sub,
        )
        draw.text(
            (72, info_y + 100),
            f"ЛИМИТ УСТРОЙСТВ:  до {limit_ip} устр.  |  Happ • v2rayTun • Hiddify",
            fill=muted_gray,
            font=font_meta,
        )

        out_rgb = bg.convert("RGB")
        with io.BytesIO() as buf:
            out_rgb.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
    finally:
        if out_rgb is not None:
            out_rgb.close()
        if qr_img is not None:
            qr_img.close()
        if qr_raw is not None:
            qr_raw.close()
        if mask is not None:
            mask.close()
        if mascot is not None:
            mascot.close()
        bg.close()
