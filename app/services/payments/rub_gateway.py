from __future__ import annotations

from decimal import Decimal

import httpx

from app.core.config import settings
from app.core.logging import logger
from app.services.payments.base import BasePaymentProvider, InvoiceResult


class PlategaGatewayError(Exception):
    """Ошибка взаимодействия с рублёвым P2P/СБП шлюзом (Platega / CrystalPay)."""


class PlategaProvider(BasePaymentProvider):
    """
    Полноценная реализация рублёвого шлюза без ИП/самозанятости (Platega API / СБП и карты РФ).
    Если ключи `PLATEGA_MERCHANT_ID` и `PLATEGA_API_KEY` заданы в `.env`, выполняет реальный
    HTTP-запрос к Platega API для генерации СБП/карточной ссылки.
    Если ключи ещё не заполнены — возвращает безопасную локальную ссылку `/pay/rub/{external_id}`,
    позволяя тестировать воронку и принимать вебхуки на `POST /webhooks/rub_gateway`.
    """

    def __init__(
        self,
        merchant_id: str | None = None,
        api_key: str | None = None,
        api_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.merchant_id = (
            merchant_id
            if merchant_id is not None
            else (settings.PLATEGA_MERCHANT_ID or settings.RUB_GATEWAY_SHOP_ID)
        )
        self.api_key = (
            api_key
            if api_key is not None
            else (settings.PLATEGA_API_KEY or settings.RUB_GATEWAY_SECRET_KEY)
        )
        self.api_url = (api_url or settings.PLATEGA_API_URL).rstrip("/")
        self._http_client = http_client

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

        if not self.merchant_id or not self.api_key:
            fallback_url = f"{base}/pay/rub/{external_id}?amount={amount_rub:.2f}"
            return InvoiceResult(external_id=external_id, payment_url=fallback_url)

        payload = {
            "merchantId": self.merchant_id,
            "orderId": str(transaction_id),
            "amount": float(amount_rub),
            "currency": "RUB",
            "description": description,
            "webhookUrl": f"{base}/webhooks/rub_gateway",
            "returnUrl": f"https://t.me/{settings.BOT_USERNAME}",
            "metadata": {"telegram_id": user_telegram_id, "transaction_id": transaction_id},
        }
        headers = {
            "X-Merchant-Id": self.merchant_id,
            "X-Secret": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=10.0)
        try:
            resp = await client.post(
                f"{self.api_url}/transaction/process",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            ext_id = str(data.get("transactionId") or data.get("id") or external_id)
            pay_url = (
                data.get("redirect")
                or data.get("paymentUrl")
                or data.get("url")
                or f"{base}/pay/rub/{ext_id}?amount={amount_rub:.2f}"
            )
            return InvoiceResult(external_id=ext_id, payment_url=str(pay_url))
        except Exception as exc:
            logger.warning("Platega API fallback для tx #%s: %s", transaction_id, exc)
            fallback_url = f"{base}/pay/rub/{external_id}?amount={amount_rub:.2f}"
            return InvoiceResult(external_id=external_id, payment_url=fallback_url)
        finally:
            if owns_client:
                await client.aclose()

    async def check_payment_status(self, external_id: str) -> bool:
        if not self.merchant_id or not self.api_key:
            return False

        headers = {
            "X-Merchant-Id": self.merchant_id,
            "X-Secret": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
        }
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=10.0)
        try:
            resp = await client.get(
                f"{self.api_url}/transaction/{external_id}",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            st = str(data.get("status", "")).lower()
            return st in ("paid", "succeeded", "confirmed", "completed")
        except Exception:
            return False
        finally:
            if owns_client:
                await client.aclose()


# Сохраняем алиас RubGatewayStub для обратной совместимости импортов
RubGatewayStub = PlategaProvider
