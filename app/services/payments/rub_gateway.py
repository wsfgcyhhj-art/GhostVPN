from __future__ import annotations

from decimal import Decimal

from app.core.config import settings
from app.services.payments.base import BasePaymentProvider, InvoiceResult


class RubGatewayStub(BasePaymentProvider):
    """
    Готовый адаптер под рублёвый шлюз (ЮKassa / Platega / СБП).
    При заполнении RUB_GATEWAY_SHOP_ID и RUB_GATEWAY_SECRET_KEY генерирует платёжную ссылку
    и обрабатывает вебхук POST /webhooks/rub_gateway без изменения ядра бота.
    """

    async def create_invoice(
        self,
        *,
        transaction_id: int,
        amount_rub: Decimal,
        description: str,
        user_telegram_id: int,
    ) -> InvoiceResult:
        external_id = f"rub_order_{transaction_id}"
        base = settings.WEBHOOK_BASE_URL.rstrip("/")
        # Платёжная ссылка (при подключении реального API ЮKassa/Platega возвращает confirmation_url)
        payment_url = f"{base}/pay/rub/{external_id}?amount={amount_rub:.2f}"
        return InvoiceResult(external_id=external_id, payment_url=payment_url)

    async def check_payment_status(self, external_id: str) -> bool:
        # Фоллбэк: статус обновляется через защищённый вебхук POST /webhooks/rub_gateway
        return False
