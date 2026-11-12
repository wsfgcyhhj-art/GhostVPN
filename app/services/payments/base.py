from __future__ import annotations

import abc
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class InvoiceResult:
    external_id: str
    payment_url: str | None = None
    stars_amount: int | None = None


class BasePaymentProvider(abc.ABC):
    """Абстрактный базовый класс платёжного шлюза (Strategy Pattern)."""

    @abc.abstractmethod
    async def create_invoice(
        self,
        *,
        transaction_id: int,
        amount_rub: Decimal,
        description: str,
        user_telegram_id: int,
    ) -> InvoiceResult:
        raise NotImplementedError

    @abc.abstractmethod
    async def check_payment_status(self, external_id: str) -> bool:
        """Возвращает True, если счёт оплачен во внешней системе."""
        raise NotImplementedError
