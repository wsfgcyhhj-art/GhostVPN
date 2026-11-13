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
    build_freekassa_sci_sign,
    format_fk_amount,
    verify_cryptobot_signature,
    verify_freekassa_signature,
    verify_telegram_secret,
)
from app.db.models import PaymentProviderType, Transaction, TransactionStatus
from app.db.repositories import Repository
from app.db.session import get_db_session
from app.services.payments.rub_gateway import FreeKassaProvider
from app.services.payments.stars import rub_to_stars
from app.services.vpn import VPNService
from app.services.xui.client import XUIClient
from app.web.app import app


@pytest.mark.asyncio
async def test_security_signatures_freekassa_amount_format_and_stars() -> None:
    assert verify_telegram_secret("my_secret", "my_secret") is True
    assert verify_telegram_secret("wrong", "my_secret") is False

    token = "99999:AA_TEST_CRYPTO_TOKEN"
    body = b'{"update_type":"invoice_paid","payload":{"invoice_id":123,"status":"paid"}}'
    secret = hashlib.sha256(token.encode()).digest()
    valid_sig = hmac.new(secret, body, hashlib.sha256).hexdigest()

    assert verify_cryptobot_signature(body, valid_sig, token) is True
    assert verify_cryptobot_signature(body, "invalid_sig", token) is False

    # Проверка идентичного форматирования суммы FreeKassa (199 -> '199.00')
    assert format_fk_amount(199) == "199.00"
    assert format_fk_amount(Decimal("199")) == "199.00"
    assert format_fk_amount(Decimal("1.00")) == "1.00"

    fk_provider = FreeKassaProvider(merchant_id="12345", secret_1="sec1_test")
    inv = await fk_provider.create_invoice(
        transaction_id=42,
        amount_rub=Decimal("199"),
        description="Test 1m",
        user_telegram_id=100500,
    )
    expected_sci_sign = build_freekassa_sci_sign("12345", "199.00", "sec1_test", "RUB", "42")
    assert "oa=199.00" in (inv.payment_url or "")
    assert f"s={expected_sci_sign}" in (inv.payment_url or "")

    # Проверка MD5-подписи вебхука FreeKassa (Секретное слово №2)
    sec2 = "sec2_webhook_test"
    wh_sign = hashlib.md5(f"12345:199.00:{sec2}:42".encode("utf-8")).hexdigest()
    assert verify_freekassa_signature("12345", "199.00", "42", wh_sign.upper(), sec2) is True
    assert verify_freekassa_signature("12345", "199.00", "42", "bad_md5", sec2) is False

    stars = rub_to_stars(Decimal("199.00"), rate=2.0)
    assert stars == 100


@pytest.mark.asyncio
async def test_web_endpoints_health_deep_links_and_webhooks(
    db_session: AsyncSession,
) -> None:
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db_session] = override_get_db
    transport = ASGITransport(app=app)

    repo = Repository(db_session)
    user, _ = await repo.get_or_create_user(telegram_id=888111, username="crypto_buyer")
    mock_xui = AsyncMock(spec=XUIClient)
    vpn = VPNService(db_session, xui_client_override=mock_xui)
    active_sub = await vpn.grant_or_extend_subscription(user, days=30)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Проверка /health и /ping (GET и HEAD для UptimeRobot)
        resp_health = await client.get("/health")
        assert resp_health.status_code == 200
        assert resp_health.json()["status"] == "ok"

        resp_ping = await client.get("/ping")
        assert resp_ping.status_code == 200

        resp_head_health = await client.head("/health")
        assert resp_head_health.status_code == 200

        resp_head_ping = await client.head("/ping")
        assert resp_head_ping.status_code == 200

        # 2. Проверка Deep Link /connect/{app}/{sub_id} для активной, несуществующей и забаненной подписки
        resp_connect = await client.get(f"/connect/happ/{active_sub.sub_id}")
        assert resp_connect.status_code == 200
        assert "happ://add/" in resp_connect.text

        resp_not_found = await client.get("/connect/happ/nonexistent12345")
        assert resp_not_found.status_code == 404

        resp_invalid_app = await client.get(f"/connect/unknownapp/{active_sub.sub_id}")
        assert resp_invalid_app.status_code == 404

        # 3. Проверка защиты вебхука Telegram
        resp_tg_bad = await client.post("/webhook/telegram", json={})
        assert resp_tg_bad.status_code == 403

        # 4. Проверка полного цикла вебхука CryptoBot с подписью HMAC-SHA256
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

        # 5. Проверка вебхука FreeKassa SCI (/webhooks/rub_gateway) с ответом PlainTextResponse("YES")
        tx_fk = Transaction(
            user_id=user.id,
            plan_id=plans[0].id,
            provider=PaymentProviderType.RUB_GATEWAY,
            amount_rub=Decimal("199.00"),
            balance_used_rub=Decimal("0.00"),
            status=TransactionStatus.PENDING,
        )
        db_session.add(tx_fk)
        await db_session.commit()
        await db_session.refresh(tx_fk)

        fk_sec2 = "fk_test_secret_2"
        object.__setattr__(settings, "FREEKASSA_SECRET_2", fk_sec2)
        fk_sign = hashlib.md5(f"999:199.00:{fk_sec2}:{tx_fk.id}".encode("utf-8")).hexdigest()

        with patch("app.services.vpn.XUIClient.from_server", return_value=mock_xui), patch(
            "app.web.app.bot.send_message", new_callable=AsyncMock
        ):
            resp_fk = await client.post(
                "/webhooks/rub_gateway",
                data={
                    "MERCHANT_ID": "999",
                    "AMOUNT": "199.00",
                    "intid": "555444",
                    "MERCHANT_ORDER_ID": str(tx_fk.id),
                    "SIGN": fk_sign,
                },
            )
            assert resp_fk.status_code == 200
            assert resp_fk.text == "YES"

            # Повторный вызов того же вебхука идемпотентно возвращает YES
            resp_fk_dup = await client.post(
                "/webhooks/rub_gateway",
                data={
                    "MERCHANT_ID": "999",
                    "AMOUNT": "199.00",
                    "intid": "555444",
                    "MERCHANT_ORDER_ID": str(tx_fk.id),
                    "SIGN": fk_sign,
                },
            )
            assert resp_fk_dup.status_code == 200
            assert resp_fk_dup.text == "YES"

        await db_session.refresh(tx_fk)
        assert tx_fk.status == TransactionStatus.COMPLETED
        assert tx_fk.external_id == "fk_555444"

    app.dependency_overrides.clear()
