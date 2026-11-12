from __future__ import annotations

import math
from decimal import Decimal

from app.core.config import settings
from app.services.payments.base import BasePaymentProvider, InvoiceResult


def rub_to_stars(amount_rub: Decimal, rate: float | None = None) -> int:
    """Конвертирует сумму в рублях в целое число Telegram Stars (XTR), минимум 1 Star."""
    eff_rate = rate if rate and rate > 0 else settings.STARS_RUB_RATE
    stars = math.ceil(float(amount_rub) / eff_rate)
    return max(1, stars)


class TelegramStarsProvider(BasePaymentProvider):
    """Нативная оплата через Telegram Stars (валюта XTR)."""

    async def create_invoice(
        self,
        *,
        transaction_id: int,
        amount_rub: Decimal,
        description: str,
        user_telegram_id: int,
    ) -> InvoiceResult:
        stars_count = rub_to_stars(amount_rub)
        return InvoiceResult(
            external_id=f"stars_tx_{transaction_id}",
            payment_url=None,
            stars_amount=stars_count,
        )

    async def check_payment_status(self, external_id: str) -> bool:
        # Для Telegram Stars подтверждение приходит мгновенно через апдейт successful_payment
        return False
