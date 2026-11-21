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
    BANNER_BG_PATH,
    BANNER_SPECS,
    BANNER_TEMPLATE_VERSION,
    FONT_BOLD_PATH,
    FONT_REG_PATH,
    render_banner_bytes,
    set_memory_cached_file_id,
)
from app.services.qr_card import generate_branded_qr_card
from app.services.vpn import (
    VPNService,
    build_subscription_url,
    calculate_extra_device_price,
    calculate_plan_renewal_price_with_slots,
    get_target_inbound_ids,
)
from app.services.xui.client import XUIClient, XUIClientTraffic, XUIInboundRealityInfo
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
    import io
    from PIL import Image

    assert BANNER_TEMPLATE_VERSION == "v3_flow_clean"
    assert BANNER_BG_PATH.exists()
    assert FONT_BOLD_PATH.exists()
    assert FONT_REG_PATH.exists()
    assert len(BANNER_SPECS) == 9

    for key in BANNER_SPECS:
        png_ru, hash_ru = render_banner_bytes(key, "ru")
        png_en, hash_en = render_banner_bytes(key, "en")
        assert png_ru.startswith(b"\x89PNG")
        assert png_en.startswith(b"\x89PNG")
        assert len(hash_ru) == 64
        assert hash_ru != hash_en

        with Image.open(io.BytesIO(png_ru)) as im:
            assert im.size == (1280, 640)

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

        # 5. Эндпоинт мобильного подключения /sub/mobile/{sub_id}
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "XUI_INBOUND_ID", 1)
    monkeypatch.setattr(settings, "XUI_MOBILE_INBOUND_ID", None)
    monkeypatch.setattr(settings, "XUI_EXTRA_INBOUND_IDS", "")

    repo = Repository(db_session)
    server = await repo.get_active_server()
    if server is not None:
        server.inbound_id = 1
        await db_session.commit()

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


@pytest.mark.asyncio
async def test_dual_reality_inbounds_sync_and_mobile_multiprofile_formats(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64
    from urllib.parse import unquote

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db_session] = override_get_db
    transport = ASGITransport(app=app)

    monkeypatch.setattr(settings, "XUI_INBOUND_ID", 1)
    monkeypatch.setattr(settings, "XUI_MOBILE_INBOUND_ID", 2)
    monkeypatch.setattr(settings, "XUI_EXTRA_INBOUND_IDS", "2,3,4,5")

    try:
        repo = Repository(db_session)
        server = await repo.get_active_server()
        assert server is not None
        server.inbound_id = 1
        await db_session.commit()
        assert get_target_inbound_ids(server) == [1, 2, 3, 4, 5]

        user, _ = await repo.get_or_create_user(telegram_id=910500, username="lte_dual_user")

        mock_xui = AsyncMock(spec=XUIClient)

        inbound_specs = {
            1: (443, ["ya.ru"], "PBK_NODE_1", "aa11", "chrome"),
            2: (8443, ["vk.com"], "PBK_NODE_2", "bb22", "ios"),
            3: (2053, ["dl.google.com"], "PBK_NODE_3", "cc33", "chrome"),
            4: (2083, ["cloudflare.com"], "PBK_NODE_4", "dd44", "ios"),
            5: (2087, ["microsoft.com"], "PBK_NODE_5", "ee55", "chrome"),
        }

        sub = None

        async def fake_get_inbound_info(inbound_id: int) -> XUIInboundRealityInfo:
            port, snis, pbk, sid, fp = inbound_specs[inbound_id]
            # Имитируем ситуацию: в инбаунде #1 клиент уже есть, а инбаунды #2..5 созданы позже и ещё пустые
            existing_uuids = {sub.xui_client_uuid} if (inbound_id == 1 and sub is not None) else set()
            return XUIInboundRealityInfo(
                inbound_id=inbound_id,
                port=port,
                server_names=snis,
                public_key=pbk,
                short_id=sid,
                fingerprint=fp,
                client_uuids=existing_uuids,
            )

        mock_xui.get_inbound_reality_info.side_effect = fake_get_inbound_info
        vpn = VPNService(db_session, xui_client_override=mock_xui)

        # 1. Создание подписки синхронно вызывает add_client во всех 5 инбаундах (1..5) с одинаковыми uuid/subId/email
        sub = await vpn.grant_or_extend_subscription(user, days=30)
        assert mock_xui.add_client.await_count == 5
        for idx, expected_inb_id in enumerate([1, 2, 3, 4, 5]):
            call_kw = mock_xui.add_client.await_args_list[idx].kwargs
            assert call_kw["inbound_id"] == expected_inb_id
            assert call_kw["client_uuid"] == sub.xui_client_uuid
            assert call_kw["sub_id"] == sub.sub_id
            assert call_kw["email"] == sub.xui_email

        # 2. Докупка слота устройства обновляет limit_ip во всех 5 инбаундах
        mock_xui.update_client.reset_mock()
        await vpn.add_extra_device_slot(user, slots=1)
        assert mock_xui.update_client.await_count == 5
        assert [c.kwargs["inbound_id"] for c in mock_xui.update_client.await_args_list] == [1, 2, 3, 4, 5]
        assert mock_xui.update_client.await_args_list[4].kwargs["limit_ip"] == 4

        # 3. Блокировка/разблокировка и перевыпуск ключа синхронизируют все 5 инбаундов
        mock_xui.update_client.reset_mock()
        await vpn.set_client_enabled(sub, False)
        assert mock_xui.update_client.await_count == 5
        assert mock_xui.update_client.await_args_list[4].kwargs["enable"] is False
        await vpn.set_client_enabled(sub, True)

        mock_xui.add_client.reset_mock()
        mock_xui.delete_client_by_uuid.reset_mock()
        sub = await vpn.reissue_key(user, force_by_admin=True)
        assert mock_xui.delete_client_by_uuid.await_count == 5
        assert mock_xui.add_client.await_count == 5

        # 4. Проверка смарт-эндпоинта GET /sub/mobile/{sub_id} (5 узлов «⚡️ Узел 1..5» в JSON, YAML, Base64)
        # и автоматического добавления клиента в новые инбаунды #2..5, где его ещё не было
        mock_xui.add_client.reset_mock()
        expected_titles = [
            "🇳🇱 GhostFaceVPN • Основной",
            "🇳🇱 GhostFaceVPN • Резервный",
            "⚡️ Узел 3",
            "⚡️ Узел 4",
            "⚡️ Узел 5",
        ]
        with patch("app.services.vpn.XUIClient.from_server", return_value=mock_xui):
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                # 4a. JSON (Happ / v2rayTun / Hiddify)
                resp_json = await client.get(
                    f"/sub/mobile/{sub.sub_id}",
                    headers={"User-Agent": "Happ/1.4.0 iOS"},
                )
                assert resp_json.status_code == 200
                # В inbound #1 клиент уже был, а в #2, #3, #4, #5 он автоматически добавлен с текущими параметрами
                assert mock_xui.add_client.await_count == 4
                auto_added_inbounds = [c.kwargs["inbound_id"] for c in mock_xui.add_client.await_args_list]
                assert auto_added_inbounds == [2, 3, 4, 5]
                for c in mock_xui.add_client.await_args_list:
                    assert c.kwargs["client_uuid"] == sub.xui_client_uuid
                    assert c.kwargs["sub_id"] == sub.sub_id
                    assert c.kwargs["email"] == sub.xui_email
                    assert c.kwargs["limit_ip"] == sub.limit_ip
                data = resp_json.json()
                assert len(data["profiles"]) == 5
                assert [p["name"] for p in data["profiles"]] == expected_titles
                p1, p2, p5 = data["profiles"][0], data["profiles"][1], data["profiles"][4]
                assert p1["port"] == 443 and p1["publicKey"] == "PBK_NODE_1" and p1["shortId"] == "aa11" and p1["sni"] == "ya.ru"
                assert p2["port"] == 8443 and p2["publicKey"] == "PBK_NODE_2" and p2["shortId"] == "bb22" and p2["sni"] == "vk.com"
                assert p5["port"] == 2087 and p5["publicKey"] == "PBK_NODE_5" and p5["shortId"] == "ee55" and p5["sni"] == "microsoft.com"
                assert p1["fragment"] == {
                    "enabled": True,
                    "packets": "tlshello",
                    "length": "10-20",
                    "interval": "10-15",
                }
                assert p1["ip_strategy"] == "UseIPv4" and p1["prefer_ipv4"] is True

                # Проверка правил маршрутизации (блокировка рекламы + прямой доступ к РФ банкам/сайтам)
                routing = data.get("routing", {})
                rules = routing.get("rules", [])
                assert any(
                    r.get("outboundTag") == "block" and "geosite:category-ads-all" in r.get("domain", [])
                    for r in rules
                )
                assert any(
                    r.get("outboundTag") == "direct"
                    and "geosite:category-ru" in r.get("domain", [])
                    and "domain:ru" in r.get("domain", [])
                    and "domain:рф" in r.get("domain", [])
                    and "domain:su" in r.get("domain", [])
                    for r in rules
                )
                assert any(
                    r.get("outboundTag") == "direct"
                    and "geoip:ru" in r.get("ip", [])
                    and "geoip:private" in r.get("ip", [])
                    for r in rules
                )
                assert rules[-1].get("outboundTag") == "proxy"

                # 4b. YAML (Clash / Mihomo User-Agent)
                resp_yaml = await client.get(
                    f"/sub/mobile/{sub.sub_id}",
                    headers={"User-Agent": "ClashMetaForAndroid/2.10"},
                )
                assert resp_yaml.status_code == 200
                assert "text/yaml" in resp_yaml.headers["content-type"]
                for title in expected_titles:
                    assert title in resp_yaml.text
                assert 'packets: "tlshello"' in resp_yaml.text
                assert "PBK_NODE_5" in resp_yaml.text
                assert "GEOSITE,category-ads-all,REJECT" in resp_yaml.text
                assert "GEOSITE,category-ru,DIRECT" in resp_yaml.text
                assert "DOMAIN-SUFFIX,ru,DIRECT" in resp_yaml.text
                assert "DOMAIN-SUFFIX,рф,DIRECT" in resp_yaml.text
                assert "DOMAIN-SUFFIX,su,DIRECT" in resp_yaml.text
                assert "GEOIP,RU,DIRECT" in resp_yaml.text
                assert "GEOIP,private,DIRECT,no-resolve" in resp_yaml.text

                # 4c. Base64 (Shadowrocket / v2rayN User-Agent) + проверка VPN_PUBLIC_DOMAIN
                old_pub_dom = settings.VPN_PUBLIC_DOMAIN
                try:
                    object.__setattr__(settings, "VPN_PUBLIC_DOMAIN", "eu.ghostvpn-custom.org")
                    resp_b64 = await client.get(
                        f"/sub/mobile/{sub.sub_id}",
                        headers={"User-Agent": "Shadowrocket/2.2.40"},
                    )
                    assert resp_b64.status_code == 200
                    decoded_uris = unquote(base64.b64decode(resp_b64.text).decode("utf-8"))
                    for title in expected_titles:
                        assert title in decoded_uris
                    assert "@eu.ghostvpn-custom.org:443" in decoded_uris
                    assert "pbk=PBK_NODE_1" in decoded_uris
                    assert "pbk=PBK_NODE_5" in decoded_uris
                finally:
                    object.__setattr__(settings, "VPN_PUBLIC_DOMAIN", old_pub_dom)

                # 4d. Проверка кнопки «🎁 Подарить 3 дня другу» и текстов преимуществ
                from app.bot.keyboards.user import SHARE_FRIEND_TEXT, build_friend_share_url, referral_kb

                share_url = build_friend_share_url(user.telegram_id)
                assert share_url.startswith("https://t.me/share/url?url=")
                assert unquote(share_url).endswith(SHARE_FRIEND_TEXT)
                ref_markup = referral_kb(user)
                assert ref_markup.inline_keyboard[0][0].text == "🎁 Подарить 3 дня другу"
                assert ref_markup.inline_keyboard[0][0].url == share_url
                welcome_ru = t("welcome_title", "ru", limit_ip=3, status_line="OK", balance=0, terms_url="#")
                assert "Российские банки и Госуслуги работают без отключения" in welcome_ru
                assert "Встроенная блокировка рекламы" in welcome_ru
                assert "Стабильный интернет без ограничений по скорости" in welcome_ru
                assert "Доступно из любой точки мира" in welcome_ru

                # 4e. Проверка раздела «ℹ️ О сервисе» и соблюдения правил модерации FreeKassa
                from app.bot.keyboards.user import about_service_kb

                assert settings.DOCS_UPDATED_DATE == "16.11.2026"
                assert settings.PRIVACY_URL.endswith("/privacy") or "telegra.ph" in settings.PRIVACY_URL
                assert settings.TERMS_URL.endswith("/terms") or "telegra.ph" in settings.TERMS_URL
                about_kb = about_service_kb("ru")
                btn_texts = [row[0].text for row in about_kb.inline_keyboard]
                assert "📄 Политика конфиденциальности (от 16.11.2026)" in btn_texts
                assert "📜 Пользовательское соглашение (от 16.11.2026)" in btn_texts
                assert "💬 Поддержка (@imran_wxz)" in btn_texts

                for doc_path in ("/privacy", "/terms"):
                    resp_doc = await client.get(doc_path)
                    assert resp_doc.status_code == 200
                    assert "16 ноября 2026 г." in resp_doc.text
                    assert "@imran_wxz" in resp_doc.text
                    lower_doc = resp_doc.text.lower()
                    for forbidden in (
                        "шифрован",
                        "глушил",
                        "сокрыти",
                        "невидим",
                        "обход блокиров",
                        "обход ограничен",
                        "белые списки",
                        "lte",
                    ):
                        assert forbidden not in lower_doc

                # 5. Единая ссылка подписки {WEBHOOK_BASE_URL}/sub/mobile/{sub_id} везде по умолчанию
                expected_unified = f"{settings.WEBHOOK_BASE_URL.rstrip('/')}/sub/mobile/{sub.sub_id}"
                assert build_subscription_url(sub.sub_id, server=sub.server) == expected_unified
                assert sub.subscription_url == expected_unified

                for app_name in ("happ", "v2raytun", "hiddify"):
                    resp_conn = await client.get(f"/connect/{app_name}/{sub.sub_id}")
                    assert resp_conn.status_code == 200
                    assert expected_unified in resp_conn.text

                # Фоллбэк на XUI_SUB_BASE_URL, если WEBHOOK_BASE_URL пустой
                old_wh = settings.WEBHOOK_BASE_URL
                try:
                    object.__setattr__(settings, "WEBHOOK_BASE_URL", "")
                    assert build_subscription_url(sub.sub_id, server=sub.server).endswith(f"/{sub.sub_id}")
                    assert "/sub/mobile/" not in build_subscription_url(sub.sub_id, server=sub.server)
                finally:
                    object.__setattr__(settings, "WEBHOOK_BASE_URL", old_wh)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_gift_subscriptions_admin_and_user_flows(
    db_session: AsyncSession,
) -> None:
    from app.bot.handlers.admin import _execute_admin_grant
    from app.bot.keyboards.user import (
        admin_granted_subscription_kb,
        main_menu_kb,
        plans_list_kb,
        referral_kb,
        subscription_card_kb,
    )

    repo = Repository(db_session)
    mock_xui = AsyncMock(spec=XUIClient)
    vpn = VPNService(db_session, xui_client_override=mock_xui)
    mock_bot = AsyncMock()

    # 1. Проверяем, что в интерфейсе обычных пользователей нет кнопок подарков
    user_test, _ = await repo.get_or_create_user(telegram_id=900010, username="regular_user")
    plans = await repo.get_active_plans()

    # main_menu_kb
    main_kb = main_menu_kb(user_test, is_admin=False)
    for row in main_kb.inline_keyboard:
        for btn in row:
            assert btn.callback_data != "gift:plans"
            assert "🎁 Подарить подписку" not in (btn.text or "")
            assert "🎁 Мои подарки" not in (btn.text or "")

    # referral_kb
    ref_kb = referral_kb(user_test)
    for row in ref_kb.inline_keyboard:
        for btn in row:
            assert btn.callback_data != "gift:plans"
            assert btn.callback_data != "gift:my"
            assert "🎁 Подарить подписку" not in (btn.text or "")
            assert "🎁 Мои подарки" not in (btn.text or "")

    # plans_list_kb
    plans_kb = plans_list_kb(plans)
    for row in plans_kb.inline_keyboard:
        for btn in row:
            assert btn.callback_data != "gift:plans"

    # subscription_card_kb
    sub_dummy = await vpn.grant_or_extend_subscription(user=user_test, days=7, is_trial=True)
    card_kb = subscription_card_kb(user_test, sub_dummy)
    for row in card_kb.inline_keyboard:
        for btn in row:
            assert btn.callback_data != "gift:plans"
            assert btn.callback_data != "gift:my"

    # 2. Выдача подписки существующему пользователю по @username
    admin_u, _ = await repo.get_or_create_user(telegram_id=900001, username="admin_master")
    target_existing, _ = await repo.get_or_create_user(telegram_id=900002, username="durov_friend")

    with patch("app.bot.handlers.admin.VPNService", return_value=vpn):
        report = await _execute_admin_grant(
            bot=mock_bot,
            session=db_session,
            repo=repo,
            admin_tg_id=admin_u.telegram_id,
            target_user_id=target_existing.id,
            target_username="durov_friend",
            target_tg_id=target_existing.telegram_id,
            days=30,
            limit_ip=3,
        )

    assert "Полноценная подписка успешно активирована" in report
    assert "Уведомление с кнопкой подключения успешно доставлено" in report

    # Проверяем отправленное сообщение в ЛС
    mock_bot.send_message.assert_awaited()
    sent_call = mock_bot.send_message.await_args_list[-1]
    assert sent_call.kwargs["chat_id"] == target_existing.telegram_id
    assert "Вам выдана полноценная подписка GhostFaceVPN!" in sent_call.kwargs["text"]
    assert "30 дн." in sent_call.kwargs["text"]
    # Проверяем кнопку подключения
    kb_sent = sent_call.kwargs["reply_markup"]
    assert kb_sent.inline_keyboard[0][0].text == "📲 Подключиться в 1 клик"

    # Проверяем подписку в БД
    await db_session.refresh(target_existing, attribute_names=["subscription"])
    sub_ext = target_existing.subscription
    assert sub_ext is not None
    assert sub_ext.is_trial is False
    assert sub_ext.is_active is True
    assert sub_ext.limit_ip == 3
    assert 29 <= sub_ext.days_left <= 31

    # 3. Выдача подписки новому пользователю, которого ещё нет в базе (отложенная выдача)
    mock_bot.send_message.reset_mock()
    pending_username = "future_vip_customer"
    report_pending = await _execute_admin_grant(
        bot=mock_bot,
        session=db_session,
        repo=repo,
        admin_tg_id=admin_u.telegram_id,
        target_user_id=None,
        target_username=pending_username,
        target_tg_id=None,
        days=90,
        limit_ip=5,
    )
    assert "Подписка успешно подготовлена для @future_vip_customer" in report_pending
    assert "В базу добавлена отложенная выдача" in report_pending

    # Проверяем запись в pending_username_gifts
    unclaimed = await repo.get_unclaimed_pending_gifts_by_username(pending_username)
    assert len(unclaimed) == 1
    p_gift = unclaimed[0]
    assert p_gift.username == pending_username
    assert p_gift.days == 90
    assert p_gift.limit_ip == 5
    assert p_gift.is_claimed is False

    # Пользователь впервые нажимает /start
    new_vip_user, _ = await repo.get_or_create_user(telegram_id=900003, username=pending_username)
    # Имитация логики /start:
    unclaimed_start = await repo.get_unclaimed_pending_gifts_by_username(new_vip_user.username)
    assert len(unclaimed_start) == 1
    activated_sub = await vpn.grant_or_extend_subscription(
        user=new_vip_user,
        days=unclaimed_start[0].days,
        limit_ip=unclaimed_start[0].limit_ip,
        is_trial=False,
    )
    await repo.mark_pending_gift_claimed(unclaimed_start[0].id)

    assert activated_sub.is_trial is False
    assert activated_sub.is_active is True
    assert activated_sub.limit_ip == 5
    assert 89 <= activated_sub.days_left <= 91

    # Проверяем, что подарок теперь помечен claimed
    unclaimed_after = await repo.get_unclaimed_pending_gifts_by_username(pending_username)
    assert len(unclaimed_after) == 0

    # 4. Выдача безлимитной подписки навсегда (days=18250, limit_ip=0)
    mock_bot.send_message.reset_mock()
    with patch("app.bot.handlers.admin.VPNService", return_value=vpn):
        report_unlim = await _execute_admin_grant(
            bot=mock_bot,
            session=db_session,
            repo=repo,
            admin_tg_id=admin_u.telegram_id,
            target_user_id=target_existing.id,
            target_username=target_existing.username,
            target_tg_id=target_existing.telegram_id,
            days=18250,
            limit_ip=0,
        )
    assert "Безлимит навсегда" in report_unlim
    unlim_msg = mock_bot.send_message.await_args_list[-1]
    assert "Вам выдана безлимитная подписка GhostFaceVPN!" in unlim_msg.kwargs["text"]
    assert "без ограничений по времени, скорости и количеству устройств" in unlim_msg.kwargs["text"]

    await db_session.refresh(target_existing, attribute_names=["subscription"])
    assert target_existing.subscription.limit_ip == 0
    assert target_existing.subscription.days_left >= 18000


@pytest.mark.asyncio
async def test_start_handler_pending_and_precreated_grants(
    db_session: AsyncSession,
) -> None:
    from aiogram.filters.command import CommandObject
    from app.bot.handlers.start import cmd_start

    repo = Repository(db_session)
    mock_xui = AsyncMock(spec=XUIClient)
    vpn = VPNService(db_session, xui_client_override=mock_xui)
    mock_bot = AsyncMock()

    # 1. Тест: у пользователя есть отложенная подписка (PendingUsernameGift). Он нажимает /start
    target_uname = "alice_crypto"
    await repo.create_pending_username_gift(
        username=target_uname,
        days=60,
        limit_ip=3,
        granted_by_admin_id=900001,
    )

    fake_msg = AsyncMock()
    fake_msg.from_user = AsyncMock()
    fake_msg.from_user.id = 900088
    fake_msg.from_user.username = "Alice_Crypto"
    fake_msg.from_user.full_name = "Alice In Crypto"
    fake_msg.from_user.language_code = "ru"
    fake_msg.bot = mock_bot
    fake_msg.answer = AsyncMock()
    fake_command = CommandObject(prefix="/", command="start", args="")
    mock_state = AsyncMock()

    with patch("app.bot.handlers.start.VPNService", return_value=vpn), \
         patch("app.bot.handlers.start.send_or_edit_banner", new_callable=AsyncMock) as mock_send_banner:
        await cmd_start(
            message=fake_msg,
            command=fake_command,
            bot=mock_bot,
            session=db_session,
            repo=repo,
            state=mock_state,
        )

        mock_send_banner.assert_awaited_once()
        caption = mock_send_banner.await_args.kwargs["caption"]
        assert "Вам выдана полноценная подписка GhostFaceVPN!" in caption
        assert "60 дн." in caption

    # Проверяем, что подписка активирована в БД и помечена как claimed
    unclaimed = await repo.get_unclaimed_pending_gifts_by_username(target_uname)
    assert len(unclaimed) == 0

    user_alice = await repo.get_user_by_tg_id(900088)
    assert user_alice is not None
    assert user_alice.subscription is not None
    assert user_alice.subscription.is_active is True
    assert user_alice.subscription.is_trial is False
    assert 59 <= user_alice.subscription.days_left <= 61

    # 2. Тест: пользователь уже создан заранее с активной подпиской и нажимает /start
    fake_msg.from_user.id = 900099
    fake_msg.from_user.username = "bob_precreated"
    fake_msg.from_user.full_name = "Bob Precreated"
    user_bob, _ = await repo.get_or_create_user(telegram_id=900099, username="bob_precreated")
    await vpn.grant_or_extend_subscription(user=user_bob, days=18250, limit_ip=0, is_trial=False)

    with patch("app.bot.handlers.start.VPNService", return_value=vpn), \
         patch("app.bot.handlers.start.send_or_edit_banner", new_callable=AsyncMock) as mock_send_banner:
        await cmd_start(
            message=fake_msg,
            command=fake_command,
            bot=mock_bot,
            session=db_session,
            repo=repo,
            state=mock_state,
        )

        mock_send_banner.assert_awaited_once()
        caption = mock_send_banner.await_args.kwargs["caption"]
        assert "Вам выдана безлимитная подписка GhostFaceVPN!" in caption
        assert "без ограничений по времени, скорости и количеству устройств" in caption


