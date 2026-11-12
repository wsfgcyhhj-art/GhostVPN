from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    verify_cryptobot_signature,
    verify_rub_gateway_signature,
    verify_telegram_secret,
)
from app.db.models import PaymentProviderType, Transaction, TransactionStatus
from app.db.repositories import Repository
from app.db.session import get_db_session
from app.services.payments.stars import rub_to_stars
from app.services.xui.client import XUIClient
from app.web.app import app


def test_security_signatures_and_stars_conversion() -> None:
    assert verify_telegram_secret("my_secret", "my_secret") is True
    assert verify_telegram_secret("wrong", "my_secret") is False

    token = "99999:AA_TEST_CRYPTO_TOKEN"
    body = b'{"update_type":"invoice_paid","payload":{"invoice_id":123,"status":"paid"}}'
    secret = hashlib.sha256(token.encode()).digest()
    valid_sig = hmac.new(secret, body, hashlib.sha256).hexdigest()

    assert verify_cryptobot_signature(body, valid_sig, token) is True
    assert verify_cryptobot_signature(body, "invalid_sig", token) is False

    rub_secret = "rub_secret_123"
    rub_sig = hmac.new(rub_secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_rub_gateway_signature(body, rub_sig, rub_secret) is True

    stars = rub_to_stars(Decimal("199.00"), rate=2.0)
    assert stars == 100


@pytest.mark.asyncio
async def test_web_endpoints_health_deep_links_and_cryptobot_webhook(
    db_session: AsyncSession,
) -> None:
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db_session] = override_get_db
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Проверка /health и /ping
        resp_health = await client.get("/health")
        assert resp_health.status_code == 200
        assert resp_health.json()["status"] == "ok"

        resp_ping = await client.get("/ping")
        assert resp_ping.status_code == 200

        # 2. Проверка Deep Link редирект-страницы /connect/{app}/{sub_id}
        resp_connect = await client.get("/connect/happ/abc123def4567890")
        assert resp_connect.status_code == 200
        assert "happ://add/" in resp_connect.text

        resp_invalid_app = await client.get("/connect/unknownapp/abc123def4567890")
        assert resp_invalid_app.status_code == 404

        # 3. Проверка защиты вебхука Telegram
        resp_tg_bad = await client.post("/webhook/telegram", json={})
        assert resp_tg_bad.status_code == 403

        # 4. Проверка полного цикла вебхука CryptoBot с подписью HMAC-SHA256
        repo = Repository(db_session)
        user, _ = await repo.get_or_create_user(telegram_id=888111, username="crypto_buyer")
        plans = await repo.get_active_plans()
        tx = Transaction(
            user_id=user.id,
            plan_id=plans[0].id,
            provider=PaymentProviderType.CRYPTOBOT,
            amount_rub=plans[0].price_rub,
            balance_used_rub=Decimal("0.00"),
            external_id="777888",
            status=TransactionStatus.PENDING,
        )
        db_session.add(tx)
        await db_session.commit()
        await db_session.refresh(tx)

        test_token = "12345:CRYPTO_TEST_TOKEN"
        object.__setattr__(settings, "CRYPTOBOT_API_TOKEN", test_token)

        webhook_payload = {
            "update_type": "invoice_paid",
            "payload": {
                "invoice_id": 777888,
                "status": "paid",
                "payload": str(tx.id),
            },
        }
        raw_bytes = json.dumps(webhook_payload).encode("utf-8")
        secret_key = hashlib.sha256(test_token.encode("utf-8")).digest()
        sig = hmac.new(secret_key, raw_bytes, hashlib.sha256).hexdigest()

        mock_xui = AsyncMock(spec=XUIClient)
        with patch("app.services.vpn.XUIClient.from_server", return_value=mock_xui), patch(
            "app.web.app.bot.send_message", new_callable=AsyncMock
        ):
            resp_cb = await client.post(
                "/webhooks/cryptobot",
                content=raw_bytes,
                headers={
                    "Content-Type": "application/json",
                    "crypto-pay-api-signature": sig,
                },
            )
            assert resp_cb.status_code == 200
            assert resp_cb.json() == {"ok": True}

        await db_session.refresh(tx)
        assert tx.status == TransactionStatus.COMPLETED

    app.dependency_overrides.clear()
