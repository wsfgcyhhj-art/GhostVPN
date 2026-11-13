from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl


def format_fk_amount(amount: Decimal | float | int | str) -> str:
    """
    Гарантирует идентичное форматирование суммы (например, '199.00', '1.00')
    как в параметре `oa` ссылки FreeKassa SCI, так и при расчёте MD5-подписи.
    """
    try:
        dec = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Некорректная сумма платежа: {amount}") from exc
    return f"{dec:.2f}"


def build_freekassa_sci_sign(
    merchant_id: str,
    amount_str: str,
    secret_1: str,
    currency: str,
    order_id: str,
) -> str:
    """
    Формирует MD5-подпись для ссылки оплаты FreeKassa SCI:
    md5("{merchant_id}:{amount_str}:{secret_1}:{currency}:{order_id}")
    """
    raw = f"{merchant_id}:{amount_str}:{secret_1}:{currency}:{order_id}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def verify_freekassa_signature(
    merchant_id: str,
    amount_str: str,
    order_id: str,
    signature: str | None,
    secret_2: str,
) -> bool:
    """
    Криптографическая проверка MD5-подписи вебхука FreeKassa SCI (Секретное слово №2):
    md5("{MERCHANT_ID}:{AMOUNT}:{SECRET_2}:{MERCHANT_ORDER_ID}")
    """
    if not signature or not secret_2 or not merchant_id or not amount_str or not order_id:
        return False
    raw = f"{merchant_id}:{amount_str}:{secret_2}:{order_id}"
    expected = hashlib.md5(raw.encode("utf-8")).hexdigest().lower()
    return hmac.compare_digest(expected, signature.strip().lower())


def verify_telegram_secret(header_secret: str | None, expected_secret: str) -> bool:
    """Проверяет заголовок X-Telegram-Bot-Api-Secret-Token."""
    if not header_secret or not expected_secret:
        return False
    return hmac.compare_digest(header_secret, expected_secret)


def verify_cryptobot_signature(raw_body: bytes, signature: str | None, api_token: str) -> bool:
    """
    Проверяет подпись Crypto Pay API (заголовок crypto-pay-api-signature).
    Алгоритм CryptoBot: HMAC-SHA256 от raw_body, где секретный ключ = SHA256(api_token).
    """
    if not signature or not api_token:
        return False
    secret_key = hashlib.sha256(api_token.encode("utf-8")).digest()
    calculated = hmac.new(secret_key, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(calculated, signature)


def verify_telegram_webapp_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86400,
    now_ts: int | None = None,
) -> dict[str, Any] | None:
    """
    Криптографическая проверка подлинности `Telegram.WebApp.initData` (HMAC-SHA256)
    и проверка свежести `auth_date` (по умолчанию не старше 24 часов = 86400 сек).
    Возвращает словарь распарсенных данных (включая `user`), либо None при подделке/истечении.
    """
    if not init_data or not bot_token:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    auth_date_raw = parsed.get("auth_date")
    if not auth_date_raw or not str(auth_date_raw).isdigit():
        return None

    auth_date = int(auth_date_raw)
    current_ts = now_ts if now_ts is not None else int(time.time())
    if current_ts - auth_date > max_age_seconds or auth_date > current_ts + 300:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    result: dict[str, Any] = dict(parsed)
    if "user" in result and isinstance(result["user"], str):
        try:
            result["user"] = json.loads(result["user"])
        except Exception:
            return None
    return result
