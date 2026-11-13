from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.utils import send_or_edit_banner
from app.core.config import settings
from app.core.i18n import render_progress_bar, t
from app.core.security import verify_telegram_webapp_init_data
from app.db.models import (
    PaymentProviderType,
    Transaction,
    TransactionStatus,
    TransactionType,
    utcnow,
)
from app.db.repositories import Repository
from app.db.session import get_db_session
from app.services.banner_generator import (
    BANNER_SPECS,
    render_banner_bytes,
    set_memory_cached_file_id,
)
from app.services.qr_card import generate_branded_qr_card
from app.services.vpn import (
    VPNService,
    calculate_extra_device_price,
    calculate_plan_renewal_price_with_slots,
)
from app.services.xui.client import XUIClient, XUIClientTraffic
from app.tasks.scheduler import (
    cleanup_stale_xui_clients,
    create_and_send_database_backup,
    run_retention_onboarding_and_winback,
    sync_xui_traffic_and_enforce_ip_limits,
)
from app.web.app import app


def _make_valid_init_data(
    user_dict: dict, bot_token: str, *, auth_date: int | None = None
) -> str:
    ts = auth_date if auth_date is not None else int(time.time())
    params = {
        "auth_date": str(ts),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user_dict, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    params["hash"] = sig
    return urlencode(params)


def test_banner_generator_all_9_sections_and_qr_card() -> None:
    for key in BANNER_SPECS:
        png_ru, hash_ru = render_banner_bytes(key, "ru")
        png_en, hash_en = render_banner_bytes(key, "en")
        assert png_ru.startswith(b"\x89PNG")
        assert png_en.startswith(b"\x89PNG")
        assert len(hash_ru) == 64
        assert hash_ru != hash_en

    card_bytes = generate_branded_qr_card(
        subscription_url="https://127.0.0.1:2096/sub/ghost1234567890",
        sub_id="ghost1234567890",
        expires_str="25.12.2026 12:00 МСК",
        limit_ip=4,
    )
    assert card_bytes.startswith(b"\x89PNG")

    bar = render_progress_bar(18, 30, width=10)
    assert bar == "▓▓▓▓▓▓░░░░"
    assert "GhostVPN" in t("welcome_title", "en", limit_ip=3, status_line="OK", balance=0, terms_url="#")


@pytest.mark.asyncio
async def test_extra_device_slots_and_renewal_pricing(db_session: AsyncSession) -> None:
    repo = Repository(db_session)
    user, _ = await repo.get_or_create_user(telegram_id=600101, username="slot_user")
    plans = await repo.get_active_plans()
    plan_1m = plans[0]  # 199 RUB, 30 days
    plan_3m = plans[1]  # 499 RUB, 90 days

    mock_xui = AsyncMock(spec=XUIClient)
    vpn = VPNService(db_session, xui_client_override=mock_xui)

    # 1. Выдача базовой подписки (3 устройства)
    sub = await vpn.grant_or_extend_subscription(user, days=30, plan_id=plan_1m.id)
    assert sub.limit_ip == 3
    assert sub.extra_devices == 0

    # 2. Докупка +2 слотов устройств через транзакцию EXTRA_DEVICE
    slot_price = calculate_extra_device_price(sub)
    assert slot_price >= Decimal("39.00")

    tx_slot = Transaction(
        user_id=user.id,
        plan_id=plan_1m.id,
        tx_type=TransactionType.EXTRA_DEVICE,
        provider=PaymentProviderType.RUB_GATEWAY,
        amount_rub=slot_price,
        extra_devices_qty=2,
        status=TransactionStatus.PENDING,
    )
    db_session.add(tx_slot)
    await db_session.commit()

    ok, sub = await vpn.process_successful_payment(tx_slot)
    assert ok is True
    assert sub is not None
    assert sub.limit_ip == 5
    assert sub.extra_devices == 2

    # 3. Проверка расчёта стоимости продления с учётом 2 доп. слотов:
    # На 1 месяц: 199 + 69 * 2 * 1 = 337.00 ₽
    assert calculate_plan_renewal_price_with_slots(plan_1m, extra_devices=sub.extra_devices) == Decimal("337.00")
    # На 3 месяца: 499 + 69 * 2 * 3 = 913.00 ₽
    assert calculate_plan_renewal_price_with_slots(plan_3m, extra_devices=sub.extra_devices) == Decimal("913.00")

    # 4. Пользователь в меню «Управлять слотами» уменьшает доп. слоты обратно до 0 (базовые 3)
    sub_reduced = await vpn.reduce_extra_device_slots(user, target_extra_devices=0)
    assert sub_reduced.extra_devices == 0
    assert sub_reduced.limit_ip == 3


@pytest.mark.asyncio
async def test_webapp_initdata_auth_freshness_and_mobile_fragment_security(
    db_session: AsyncSession,
) -> None:
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db_session] = override_get_db
    transport = ASGITransport(app=app)

    repo = Repository(db_session)
    user, _ = await repo.get_or_create_user(telegram_id=700202, username="webapp_hero")
    mock_xui = AsyncMock(spec=XUIClient)
    vpn = VPNService(db_session, xui_client_override=mock_xui)
    sub = await vpn.grant_or_extend_subscription(user, days=3, is_trial=True)

    user_payload = {"id": 700202, "first_name": "Neo", "username": "webapp_hero", "language_code": "ru"}
    valid_init_data = _make_valid_init_data(user_payload, settings.BOT_TOKEN)
    expired_init_data = _make_valid_init_data(
        user_payload,
        settings.BOT_TOKEN,
        auth_date=int(time.time()) - 90000,  # старше 24 часов
    )
    assert verify_telegram_webapp_init_data(expired_init_data, settings.BOT_TOKEN) is None

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. HTML страница /webapp (без небезопасного innerHTML для тарифов)
        resp_html = await client.get("/webapp")
        assert resp_html.status_code == 200
        assert "GHOSTVPN" in resp_html.text

        # 2. Невалидный и просроченный initData отклоняются с 401
        resp_unauth = await client.get("/api/webapp/me", headers={"X-Telegram-Init-Data": "fake=1&hash=bad"})
        assert resp_unauth.status_code == 401

        resp_expired = await client.get("/api/webapp/me", headers={"X-Telegram-Init-Data": expired_init_data})
        assert resp_expired.status_code == 401

        # 3. Валидный initData отдаёт полный дашборд
        resp_me = await client.get("/api/webapp/me", headers={"X-Telegram-Init-Data": valid_init_data})
        assert resp_me.status_code == 200
        me_json = resp_me.json()
        assert me_json["telegram_id"] == 700202
        assert me_json["sub_id"] == sub.sub_id
        assert len(me_json["plans"]) == 4

        # 4. Создание счёта из Mini App
        plan_id = me_json["plans"][0]["id"]
        resp_pay = await client.post(
            "/api/webapp/pay",
            headers={"X-Telegram-Init-Data": valid_init_data},
            json={"plan_id": plan_id, "provider": "RUB_GATEWAY"},
        )
        assert resp_pay.status_code == 200
        assert "payment_url" in resp_pay.json()

        # 5. Эндпоинт обхода мобильных глушилок /sub/mobile/{sub_id}
        resp_mob = await client.get(f"/sub/mobile/{sub.sub_id}")
        assert resp_mob.status_code == 200
        mob_data = resp_mob.json()
        assert mob_data["fragment"]["enabled"] is True
        assert "ya.ru" in mob_data["sni_whitelist"]

        # 6. Забаненный пользователь получает 403 на /sub/mobile/{sub_id} и /api/webapp/me
        user.is_banned = True
        await db_session.commit()
        resp_mob_banned = await client.get(f"/sub/mobile/{sub.sub_id}")
        assert resp_mob_banned.status_code == 403

        resp_me_banned = await client.get("/api/webapp/me", headers={"X-Telegram-Init-Data": valid_init_data})
        assert resp_me_banned.status_code == 403

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_trial_race_condition_and_stale_banner_file_id_recovery(
    db_session: AsyncSession,
) -> None:
    repo = Repository(db_session)
    user, _ = await repo.get_or_create_user(telegram_id=700999, username="trial_racer")
    mock_xui = AsyncMock(spec=XUIClient)
    vpn = VPNService(db_session, xui_client_override=mock_xui)

    # Два параллельно созданных счёта на триал: первый выдаёт 3 дня, второй не начисляет повторные 3 дня
    tx1 = Transaction(
        user_id=user.id,
        tx_type=TransactionType.TRIAL,
        provider=PaymentProviderType.RUB_GATEWAY,
        amount_rub=Decimal("1.00"),
        status=TransactionStatus.PENDING,
    )
    tx2 = Transaction(
        user_id=user.id,
        tx_type=TransactionType.TRIAL,
        provider=PaymentProviderType.STARS,
        amount_rub=Decimal("1.00"),
        status=TransactionStatus.PENDING,
    )
    db_session.add_all([tx1, tx2])
    await db_session.commit()

    ok1, sub1 = await vpn.process_successful_payment(tx1)
    assert ok1 is True and sub1 is not None
    exp_after_first = sub1.expires_at

    ok2, sub2 = await vpn.process_successful_payment(tx2)
    assert ok2 is True and sub2 is not None
    assert sub2.expires_at == exp_after_first
    assert mock_xui.add_client.await_count == 1
    assert mock_xui.update_client.await_count == 0

    # Проверка авто-восстановления send_or_edit_banner при протухшем telegram_file_id
    _, sha_hash = render_banner_bytes("main", "ru")
    await repo.save_cached_banner_file_id("main", "ru", sha_hash, "stale_bad_file_id")
    set_memory_cached_file_id("main", "ru", sha_hash, "stale_bad_file_id")

    fake_new_msg = AsyncMock(spec=Message)
    fake_new_msg.photo = [SimpleNamespace(file_id="fresh_valid_file_id_777")]

    mock_msg = AsyncMock(spec=Message)
    mock_msg.photo = [SimpleNamespace(file_id="old_msg_photo")]
    mock_msg.edit_media = AsyncMock(
        side_effect=[
            TelegramBadRequest(method=AsyncMock(), message="Bad Request: wrong file identifier"),
            fake_new_msg,
        ]
    )

    mock_cb = AsyncMock(spec=CallbackQuery)
    mock_cb.message = mock_msg

    await send_or_edit_banner(
        mock_cb,
        banner_key="main",
        caption="Тест восстановления баннера",
        lang="ru",
        repo=repo,
    )
    assert mock_msg.edit_media.await_count == 2
    cached_in_db = await repo.get_cached_banner_file_id("main", "ru", sha_hash)
    assert cached_in_db == "fresh_valid_file_id_777"


@pytest.mark.asyncio
async def test_workers_ip_violation_retention_cleanup_and_backup(
    db_session: AsyncSession,
) -> None:
    repo = Repository(db_session)
    u_violator, _ = await repo.get_or_create_user(telegram_id=800301, username="violator")
    u_stale, _ = await repo.get_or_create_user(telegram_id=800302, username="stale_user")

    mock_xui = AsyncMock(spec=XUIClient)
    mock_xui.get_online_clients.return_value = ["tg_800301"]
    mock_xui.get_client_ips.return_value = ["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4"]
    mock_xui.get_client_traffics.return_value = XUIClientTraffic(
        email="tg_800301", up_bytes=0, down_bytes=0, total_bytes=0
    )

    vpn = VPNService(db_session, xui_client_override=mock_xui)
    sub_v = await vpn.grant_or_extend_subscription(u_violator, days=10)
    sub_v.started_at = utcnow() - timedelta(hours=7)

    sub_s = await vpn.grant_or_extend_subscription(u_stale, days=3)
    sub_s.expires_at = utcnow() - timedelta(days=8)
    await db_session.commit()

    class DummySessionCtx:
        async def __aenter__(self) -> AsyncSession:
            return db_session

        async def __aexit__(self, *args: object) -> None:
            pass

    mock_bot = AsyncMock()
    with patch("app.tasks.scheduler.async_session_factory", return_value=DummySessionCtx()), patch(
        "app.tasks.scheduler.XUIClient.from_server", return_value=mock_xui
    ):
        # 1. Контроль превышения IP (1-е нарушение -> предупреждение)
        sync_stats = await sync_xui_traffic_and_enforce_ip_limits(bot=mock_bot)
        assert sync_stats["warned"] == 1
        assert sub_v.ip_violations_count == 1

        # 2. Retention: онбординг (6ч без трафика) + win-back (истекла >3 дней назад)
        ret_stats = await run_retention_onboarding_and_winback(bot=mock_bot)
        assert ret_stats["onboarding"] == 1
        assert ret_stats["winback"] == 1
        await db_session.refresh(u_stale)
        assert u_stale.pending_discount_percent == 25

        # 3. Автоочистка клиентов, истекших >7 дней назад
        cleaned = await cleanup_stale_xui_clients()
        assert cleaned == 1
        assert sub_s.deleted_from_xui is True

        # 4. Ежедневный ZIP-бэкап БД
        backup_ok = await create_and_send_database_backup(bot=mock_bot, target_chat_id=12345)
        assert backup_ok is True
        mock_bot.send_document.assert_awaited_once()
