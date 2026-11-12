from __future__ import annotations

from decimal import Decimal

import httpx

from app.core.config import settings
from app.services.payments.base import BasePaymentProvider, InvoiceResult


class CryptoBotError(Exception):
    """Ошибка Crypto Pay API."""


class CryptoBotProvider(BasePaymentProvider):
    """Интеграция с CryptoBot (@send / Crypto Pay API)."""

    def __init__(
        self,
        api_token: str | None = None,
        api_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_token = api_token if api_token is not None else settings.CRYPTOBOT_API_TOKEN
        self.api_url = (api_url or settings.CRYPTOBOT_API_URL).rstrip("/")
        self._http_client = http_client

    async def create_invoice(
        self,
        *,
        transaction_id: int,
        amount_rub: Decimal,
        description: str,
        user_telegram_id: int,
    ) -> InvoiceResult:
        if not self.api_token:
            raise CryptoBotError("Токен CRYPTOBOT_API_TOKEN не задан в настройках.")

        payload = {
            "currency_type": "fiat",
            "fiat": settings.CRYPTOBOT_FIAT_CURRENCY,
            "amount": f"{amount_rub:.2f}",
            "description": description,
            "payload": str(transaction_id),
            "expires_in": 3600,
        }
        headers = {"Crypto-Pay-API-Token": self.api_token}

        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=10.0)
        try:
            resp = await client.post(
                f"{self.api_url}/createInvoice",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise CryptoBotError(f"Ошибка вызова createInvoice CryptoBot: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        if not data.get("ok") or "result" not in data:
            raise CryptoBotError(f"CryptoBot отклонил создание счёта: {data}")

        res = data["result"]
        invoice_id = str(res["invoice_id"])
        pay_url = res.get("bot_invoice_url") or res.get("pay_url")
        return InvoiceResult(external_id=invoice_id, payment_url=pay_url)

    async def check_payment_status(self, external_id: str) -> bool:
        if not self.api_token:
            return False

        headers = {"Crypto-Pay-API-Token": self.api_token}
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=10.0)
        try:
            resp = await client.get(
                f"{self.api_url}/getInvoices",
                params={"invoice_ids": external_id},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return False
        finally:
            if owns_client:
                await client.aclose()

        if not data.get("ok"):
            return False

        items = data.get("result", {}).get("items", [])
        for item in items:
            if str(item.get("invoice_id")) == str(external_id) and item.get("status") == "paid":
                return True
        return False
