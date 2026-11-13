from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlencode

from app.core.config import settings
from app.core.security import build_freekassa_sci_sign, format_fk_amount
from app.services.payments.base import BasePaymentProvider, InvoiceResult


class FreeKassaProvider(BasePaymentProvider):
    """
    Чистая реализация рублёвого платёжного шлюза FreeKassa SCI (СБП и карты РФ).
    Генерирует подписанную MD5 ссылку `https://pay.fk.money/?m=...&oa=...&currency=RUB&o=...&s=...`
    со строгим совпадением формата суммы `oa` (`199.00`) и строки расчёта MD5-подписи.
    Вебхук подтверждения принимается на `POST /webhooks/rub_gateway` с ответом `PlainTextResponse("YES")`.
    """

    def __init__(
        self,
        merchant_id: str | None = None,
        secret_1: str | None = None,
        sci_url: str | None = None,
    ) -> None:
        self.merchant_id = (
            merchant_id if merchant_id is not None else settings.FREEKASSA_MERCHANT_ID
        )
        self.secret_1 = secret_1 if secret_1 is not None else settings.FREEKASSA_SECRET_1
        self.sci_url = (sci_url or settings.FREEKASSA_SCI_URL).rstrip("/")

    async def create_invoice(
        self,
        *,
        transaction_id: int,
        amount_rub: Decimal,
        description: str,
        user_telegram_id: int,
    ) -> InvoiceResult:
        order_id = str(transaction_id)
        amount_str = format_fk_amount(amount_rub)
        currency = "RUB"

        eff_merchant = self.merchant_id or "0"
        eff_secret = self.secret_1 or "dev_secret_1"
        sign = build_freekassa_sci_sign(
            merchant_id=eff_merchant,
            amount_str=amount_str,
            secret_1=eff_secret,
            currency=currency,
            order_id=order_id,
        )

        params = {
            "m": eff_merchant,
            "oa": amount_str,
            "currency": currency,
            "o": order_id,
            "s": sign,
            "us_tg_id": str(user_telegram_id),
        }
        payment_url = f"{self.sci_url}/?{urlencode(params)}"
        return InvoiceResult(
            external_id=f"fk_order_{order_id}",
            payment_url=payment_url,
        )

    async def check_payment_status(self, external_id: str) -> bool:
        """
        В протоколе FreeKassa SCI статус подтверждается асинхронным вебхуком
        `POST /webhooks/rub_gateway` (с проверкой MD5 Секретного слова №2).
        """
        return False
