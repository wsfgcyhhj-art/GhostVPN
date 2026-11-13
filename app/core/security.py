from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from urllib.parse import parse_qsl


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


def verify_rub_gateway_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """Проверяет HMAC-SHA256 подпись вебхука рублёвого шлюза (Platega / CrystalPay)."""
    if not signature or not secret:
        return False
    calculated = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(calculated, signature.strip().lower())


def verify_telegram_webapp_init_data(init_data: str, bot_token: str) -> dict[str, Any] | None:
    """
    Криптографическая проверка подлинности `Telegram.WebApp.initData` (HMAC-SHA256).
    Возвращает словарь распарсенных данных (включая `user`), либо None при подделке.
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
