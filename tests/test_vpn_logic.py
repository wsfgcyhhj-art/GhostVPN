from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.billing import calculate_checkout_amounts
from app.services.qr_card import generate_branded_qr_card
from app.db.models import (
    PaymentProviderType,
    PromoCode,
    PromoType,
    Transaction,
    TransactionStatus,
    utcnow,
)
from app.db.repositories import Repository
from app.services.vpn import VPNService, calculate_new_expiry
from app.services.xui.client import XUIClient, XUIError


def test_calculate_new_expiry_active_vs_expired() -> None:
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)

    # 1. Активная подписка (истекает через 5 дней) + 30 дней = now + 35 дней
    active_exp = now + timedelta(days=5)
    res_active = calculate_new_expiry(active_exp, 30, now=now)
    assert res_active == now + timedelta(days=35)

    # 2. Истёкшая подписка (истекла 10 дней назад) + 30 дней = now + 30 дней
    expired_exp = now - timedelta(days=10)
    res_expired = calculate_new_expiry(expired_exp, 30, now=now)
    assert res_expired == now + timedelta(days=30)


@pytest.mark.asyncio
async def test_grant_extend_and_reissue_cooldown(db_session: AsyncSession) -> None:
    repo = Repository(db_session)
    user, _ = await repo.get_or_create_user(telegram_id=555001, username="tester")

    mock_xui = AsyncMock(spec=XUIClient)
    vpn = VPNService(db_session, xui_client_override=mock_xui)

    # 1. Выдача триала на 3 дня
    sub = await vpn.grant_or_extend_subscription(user, days=3, is_trial=True)
    assert sub.xui_email == "tg_555001"
    assert len(sub.sub_id) == 16
    assert user.has_used_trial is True
    first_sub_id = sub.sub_id
    first_exp = sub.expires_at
    mock_xui.add_client.assert_awaited_once()

    # 2. Продление на 30 дней: sub_id ОБЯЗАН сохраниться, а дни прибавиться к first_exp
    sub_extended = await vpn.grant_or_extend_subscription(user, days=30, is_trial=False)
    assert sub_extended.sub_id == first_sub_id
    assert sub_extended.is_trial is False
    assert sub_extended.expires_at > first_exp + timedelta(days=29)
    mock_xui.update_client.assert_awaited_once()

    # 3. Сброс ключа (первый раз успешно)
    sub_reissued = await vpn.reissue_key(user, force_by_admin=False)
    second_sub_id = sub_reissued.sub_id
    assert second_sub_id != first_sub_id

    # 4. Повторный сброс ключа раньше 24 часов блокируется
    with pytest.raises(ValueError, match="не чаще 1 раза"):
        await vpn.reissue_key(user, force_by_admin=False)

    # 5. Админ может сбросить принудительно без кулдауна
    sub_admin_reset = await vpn.reissue_key(user, force_by_admin=True)
    assert sub_admin_reset.sub_id != second_sub_id


@pytest.mark.asyncio
async def test_promocodes_validation_and_redeem(db_session: AsyncSession) -> None:
    repo = Repository(db_session)
    user, _ = await repo.get_or_create_user(telegram_id=777001, username="promo_user")

    promo = PromoCode(
        code="WELCOME20",
        type=PromoType.PERCENT,
        value=20,
        max_uses=1,
        only_new_users=True,
        expires_at=utcnow() + timedelta(days=5),
        is_active=True,
    )
    db_session.add(promo)
    await db_session.commit()

    redeemed, _ = await repo.validate_and_redeem_promo(user, "welcome20")
    assert redeemed.used_count == 1
    assert user.pending_discount_percent == 20

    # Повторная активация/исчерпанный лимит должен выдать ошибку
    with pytest.raises(ValueError):
        await repo.validate_and_redeem_promo(user, "WELCOME20")


@pytest.mark.asyncio
async def test_referral_reward_and_paid_pending_provision_recovery(
    db_session: AsyncSession,
) -> None:
    repo = Repository(db_session)
    referrer, _ = await repo.get_or_create_user(telegram_id=10001, username="referrer")
    buyer, _ = await repo.get_or_create_user(
        telegram_id=10002, username="buyer", referrer_telegram_id=10001
    )
    assert buyer.referrer_id == referrer.id

    plans = await repo.get_active_plans()
    plan_1m = plans[0]  # 199 RUB

    tx = Transaction(
        user_id=buyer.id,
        plan_id=plan_1m.id,
        provider=PaymentProviderType.CRYPTOBOT,
        amount_rub=plan_1m.price_rub,
        balance_used_rub=Decimal("0.00"),
        status=TransactionStatus.PENDING,
    )
    db_session.add(tx)
    await db_session.commit()

    # Сценарий 1: Сервер 3X-UI упал в момент прихода вебхука оплаты
    failing_xui = AsyncMock(spec=XUIClient)
    failing_xui.add_client.side_effect = XUIError("Connection refused")

    vpn_fail = VPNService(db_session, xui_client_override=failing_xui)
    ok, sub = await vpn_fail.process_successful_payment(tx, bot=None)

    assert ok is False
    assert sub is None
    assert tx.status == TransactionStatus.PAID_PENDING_PROVISION
    # Реферер уже получил свои 15% (199 * 0.15 = 29.85 ₽)
    await db_session.refresh(referrer)
    assert referrer.balance_rub == Decimal("29.85")

    # Сценарий 2: Сервер 3X-UI поднялся, фоновый воркер повторяет выдачу
    working_xui = AsyncMock(spec=XUIClient)
    vpn_ok = VPNService(db_session, xui_client_override=working_xui)
    ok2, sub2 = await vpn_ok.process_successful_payment(tx, bot=None)

    assert ok2 is True
    assert sub2 is not None
    assert tx.status == TransactionStatus.COMPLETED
    # Реферальный бонус НЕ начислился повторно (идемпотентность!)
    await db_session.refresh(referrer)
    assert referrer.balance_rub == Decimal("29.85")


def test_checkout_amounts_and_in_memory_qr() -> None:
    to_pay, used = calculate_checkout_amounts(
        plan_price_rub=Decimal("200.00"),
        discount_percent=10,
        user_balance_rub=Decimal("50.00"),
        use_balance=True,
    )
    assert to_pay == Decimal("130.00")
    assert used == Decimal("50.00")

    qr_bytes = generate_branded_qr_card(
        subscription_url="https://127.0.0.1:2096/sub/abcd1234efgh5678",
        sub_id="abcd1234efgh5678",
        expires_str="25.12.2026 12:00 МСК",
        limit_ip=3,
    )
    assert qr_bytes.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_scheduler_notifications_3d_1d_and_expired(
    db_session: AsyncSession,
) -> None:
    from unittest.mock import patch
    from app.tasks.scheduler import check_expiring_and_expired_subscriptions

    repo = Repository(db_session)
    u1, _ = await repo.get_or_create_user(telegram_id=90001, username="exp_3d")
    u2, _ = await repo.get_or_create_user(telegram_id=90002, username="exp_1d")
    u3, _ = await repo.get_or_create_user(telegram_id=90003, username="exp_now")

    mock_xui = AsyncMock(spec=XUIClient)
    vpn = VPNService(db_session, xui_client_override=mock_xui)

    sub1 = await vpn.grant_or_extend_subscription(u1, days=2)
    sub2 = await vpn.grant_or_extend_subscription(u2, days=1)
    sub2.expires_at = utcnow() + timedelta(hours=12)
    sub3 = await vpn.grant_or_extend_subscription(u3, days=1)
    sub3.expires_at = utcnow() - timedelta(minutes=10)
    await db_session.commit()

    class DummySessionCtx:
        async def __aenter__(self) -> AsyncSession:
            return db_session

        async def __aexit__(self, *args: object) -> None:
            pass

    mock_bot = AsyncMock()
    with patch("app.tasks.scheduler.async_session_factory", return_value=DummySessionCtx()), patch(
        "app.services.vpn.XUIClient.from_server", return_value=mock_xui
    ):
        counts = await check_expiring_and_expired_subscriptions(bot=mock_bot)

    assert counts == {"expired": 1, "1d": 1, "3d": 1}
    assert sub1.notified_3d is True
    assert sub2.notified_1d is True
    assert sub3.notified_expired is True
    assert sub3.is_active is False
    assert mock_bot.send_message.await_count == 3

