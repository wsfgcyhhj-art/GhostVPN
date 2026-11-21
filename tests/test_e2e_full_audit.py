from __future__ import annotations

import asyncio
import base64
import gc
import hashlib
import hmac
import io
import json
import time
import tracemalloc
import zipfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote, urlencode

import pytest
from aiogram.types import Chat, Message, Update, User as TgUser
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.handlers.profile import format_subscription_card
from app.bot.handlers.trial import cb_trial_pay
from app.bot.middlewares import DatabaseAndUserMiddleware
from app.core.config import settings
from app.db.base import Base
from app.db.models import (
    PaymentProviderType,
    PromoCode,
    PromoType,
    Transaction,
    TransactionStatus,
    TransactionType,
    User,
    utcnow,
)
from app.db.repositories import Repository
from app.db.session import seed_initial_data
from app.services.qr_card import generate_branded_qr_card
from app.services.vpn import VPNService
from app.services.xui.client import XUIClient, XUIError, XUIInboundRealityInfo
from app.tasks.scheduler import create_and_send_database_backup, retry_pending_provisions
from app.web.app import app, get_db_session


def _make_valid_init_data(user_dict: dict, bot_token: str) -> str:
    params = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user_dict, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    params["hash"] = sig
    return urlencode(params)


@pytest.mark.asyncio
async def test_e2e_health_and_public_web_endpoints(db_session: AsyncSession) -> None:
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db_session] = override_get_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            for ep in ("/health", "/ping"):
                r_get = await client.get(ep)
                assert r_get.status_code == 200
                body = r_get.json()
                assert body["status"] == "ok"
                assert body["database"] == "connected"

                r_head = await client.head(ep)
                assert r_head.status_code == 200

            r_priv = await client.get("/privacy")
            assert r_priv.status_code == 200
            assert "Политика конфиденциальности" in r_priv.text
            assert "@imran_wxz" in r_priv.text

            r_terms = await client.get("/terms")
            assert r_terms.status_code == 200
            assert "Пользовательское соглашение" in r_terms.text
            assert "@imran_wxz" in r_terms.text

            r_webapp = await client.get("/webapp")
            assert r_webapp.status_code == 200
            assert "GHOSTVPN" in r_webapp.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_e2e_referral_funnel_trial_double_guard_and_promocodes(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "e2e_funnel.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=eng, class_=AsyncSession, expire_on_commit=False)

    try:
        async with factory() as seed_sess:
            await seed_initial_data(seed_sess)
            repo_seed = Repository(seed_sess)
            referrer, created = await repo_seed.get_or_create_user(
                telegram_id=501001,
                username="e2e_referrer",
                full_name="Referrer",
            )
            assert created is True
            referrer_id = referrer.id

        # 1. Проход нового пользователя с /start ref_<id> через DatabaseAndUserMiddleware
        mw = DatabaseAndUserMiddleware()
        tg_msg = Message(
            message_id=1,
            date=utcnow(),
            chat=Chat(id=502002, type="private"),
            from_user=TgUser(
                id=502002,
                is_bot=False,
                first_name="Invited",
                username="e2e_invited",
                language_code="ru",
            ),
            text="/start ref_501001",
        )
        upd = Update(update_id=101, message=tg_msg)
        captured_user_id: int | None = None

        async def dummy_handler(event, data):
            nonlocal captured_user_id
            u: User = data["db_user"]
            captured_user_id = u.id
            assert u.referrer_id == referrer_id
            return "ok"

        with patch("app.bot.middlewares.async_session_factory", factory):
            res_mw = await mw(dummy_handler, upd, {"event_from_user": tg_msg.from_user})
            assert res_mw == "ok"
            assert captured_user_id is not None

        # 2. Активация триала + проверка защиты от повторного взятия триала при двойном вызове
        async with factory() as sess:
            repo = Repository(sess)
            invited = await repo.get_user_by_tg_id(502002)
            assert invited is not None
            invited.balance_rub = Decimal("10.00")
            await sess.commit()

            mock_xui = AsyncMock(spec=XUIClient)
            mock_xui.add_client = AsyncMock(return_value=None)
            mock_xui.update_client = AsyncMock(return_value=None)

            mock_bot = AsyncMock()
            cb1 = AsyncMock()
            cb1.data = "trial:pay:BALANCE"
            cb1.answer = AsyncMock()

            with patch("app.bot.handlers.trial.VPNService", return_value=VPNService(sess, xui_client_override=mock_xui)), \
                 patch("app.bot.handlers.trial.send_or_edit_banner", new_callable=AsyncMock):
                await cb_trial_pay(cb1, mock_bot, sess, repo, invited)

            await sess.refresh(invited)
            assert invited.has_used_trial is True
            first_sub_id = invited.subscription.sub_id
            assert first_sub_id

            # Второй вызов cb_trial_pay должен быть заблокирован
            cb2 = AsyncMock()
            cb2.data = "trial:pay:BALANCE"
            cb2.answer = AsyncMock()
            await cb_trial_pay(cb2, mock_bot, sess, repo, invited)
            cb2.answer.assert_awaited_once()
            assert "1 раз" in cb2.answer.await_args.args[0]

            # 3. Промокоды DAYS (лимит 1 на весь промокод + начисление дней) и PERCENT (одноразовый на пользователя)
            promo_days = PromoCode(
                code="E2EDAYS7",
                type=PromoType.DAYS,
                value=7,
                max_uses=1,
                is_active=True,
            )
            promo_pct = PromoCode(
                code="E2ESALE20",
                type=PromoType.PERCENT,
                value=20,
                max_uses=5,
                is_active=True,
            )
            sess.add_all([promo_days, promo_pct])
            await sess.commit()

            exp_before_promo = invited.subscription.expires_at
            p_res, _ = await repo.validate_and_redeem_promo(invited, "e2edays7")
            assert p_res.value == 7
            vpn_promo = VPNService(sess, xui_client_override=mock_xui)
            extended_sub = await vpn_promo.grant_or_extend_subscription(
                user=invited, days=p_res.value, is_trial=False
            )
            assert round((extended_sub.expires_at - exp_before_promo).total_seconds() / 86400) == 7

            # Повторная активация (и другим пользователем) при max_uses=1 отклоняется
            with pytest.raises(ValueError, match="исчерпан"):
                await repo.validate_and_redeem_promo(invited, "E2EDAYS7")

            other_u, _ = await repo.get_or_create_user(telegram_id=503003, username="e2e_other")
            with pytest.raises(ValueError, match="исчерпан"):
                await repo.validate_and_redeem_promo(other_u, "E2EDAYS7")

            # Активация процентного промокода и проверка одноразовости для пользователя
            pct_res, _ = await repo.validate_and_redeem_promo(invited, "E2ESALE20")
            assert pct_res.value == 20
            assert invited.pending_discount_percent == 20
            with pytest.raises(ValueError, match="уже активировали"):
                await repo.validate_and_redeem_promo(invited, "E2ESALE20")
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_e2e_payment_webhook_referral_15pct_extension_and_pending_provision_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "FREEKASSA_SECRET_2", "e2e_fk_secret_2")
    monkeypatch.setattr(settings, "REFERRAL_PERCENT", 15)

    db_file = tmp_path / "e2e_payments.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=eng, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = override_get_db
    try:
        async with factory() as sess:
            await seed_initial_data(sess)
            repo = Repository(sess)
            referrer, _ = await repo.get_or_create_user(telegram_id=601001, username="pay_ref")
            buyer, _ = await repo.get_or_create_user(
                telegram_id=602002,
                username="pay_buyer",
                referrer_telegram_id=601001,
            )
            plans = await repo.get_active_plans()
            plan_30 = plans[0]

            tx1 = Transaction(
                user_id=buyer.id,
                plan_id=plan_30.id,
                tx_type=TransactionType.SUBSCRIPTION,
                provider=PaymentProviderType.RUB_GATEWAY,
                currency="RUB",
                amount_currency=Decimal("199.00"),
                amount_rub=Decimal("199.00"),
                balance_used_rub=Decimal("0.00"),
                status=TransactionStatus.PENDING,
            )
            sess.add(tx1)
            await sess.commit()
            await sess.refresh(tx1)
            tx1_id = tx1.id
            referrer_id = referrer.id
            buyer_id = buyer.id

        mock_xui = AsyncMock(spec=XUIClient)
        mock_xui.add_client = AsyncMock(return_value=None)
        mock_xui.update_client = AsyncMock(return_value=None)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. Битая подпись вебхука -> 403
            with patch("app.web.app.bot.send_message", new_callable=AsyncMock):
                r_bad = await client.post(
                    "/webhooks/rub_gateway",
                    data={
                        "MERCHANT_ID": "999",
                        "AMOUNT": "199.00",
                        "intid": "1001",
                        "MERCHANT_ORDER_ID": str(tx1_id),
                        "SIGN": "00000000000000000000000000000000",
                    },
                )
                assert r_bad.status_code == 403

            # 2. Валидный вебхук -> 200 YES, создание подписки и начисление 15% (29.85 RUB)
            valid_sign_1 = hashlib.md5(f"999:199.00:e2e_fk_secret_2:{tx1_id}".encode("utf-8")).hexdigest()
            with patch("app.services.vpn.XUIClient.from_server", return_value=mock_xui), \
                 patch("app.web.app.bot.send_message", new_callable=AsyncMock):
                r_ok = await client.post(
                    "/webhooks/rub_gateway",
                    data={
                        "MERCHANT_ID": "999",
                        "AMOUNT": "199.00",
                        "intid": "1001",
                        "MERCHANT_ORDER_ID": str(tx1_id),
                        "SIGN": valid_sign_1,
                    },
                )
                assert r_ok.status_code == 200
                assert r_ok.text == "YES"

                # 3. Дубль вебхука -> 200 YES, без повторного начисления рефереру и продления
                r_dup = await client.post(
                    "/webhooks/rub_gateway",
                    data={
                        "MERCHANT_ID": "999",
                        "AMOUNT": "199.00",
                        "intid": "1001",
                        "MERCHANT_ORDER_ID": str(tx1_id),
                        "SIGN": valid_sign_1,
                    },
                )
                assert r_dup.status_code == 200
                assert r_dup.text == "YES"

        async with factory() as sess:
            ref_after_1 = await sess.get(User, referrer_id)
            buyer_after_1 = await sess.get(User, buyer_id)
            assert ref_after_1 is not None and buyer_after_1 is not None
            assert ref_after_1.balance_rub == Decimal("29.85")
            sub1 = buyer_after_1.subscription
            assert sub1 is not None
            orig_uuid = sub1.xui_client_uuid
            orig_sub_id = sub1.sub_id
            exp_after_1 = sub1.expires_at

            # 4. Вторая оплата при сбое 3X-UI -> PAID_PENDING_PROVISION -> recovery воркером
            tx2 = Transaction(
                user_id=buyer_id,
                plan_id=buyer_after_1.subscription.current_plan_id,
                tx_type=TransactionType.SUBSCRIPTION,
                provider=PaymentProviderType.RUB_GATEWAY,
                currency="RUB",
                amount_currency=Decimal("199.00"),
                amount_rub=Decimal("199.00"),
                balance_used_rub=Decimal("0.00"),
                status=TransactionStatus.PENDING,
            )
            sess.add(tx2)
            await sess.commit()
            await sess.refresh(tx2)
            tx2_id = tx2.id

        failing_xui = AsyncMock(spec=XUIClient)
        failing_xui.update_client = AsyncMock(side_effect=XUIError("3X-UI temporary 502"))
        valid_sign_2 = hashlib.md5(f"999:199.00:e2e_fk_secret_2:{tx2_id}".encode("utf-8")).hexdigest()

        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch("app.services.vpn.XUIClient.from_server", return_value=failing_xui), \
                 patch("app.web.app.bot.send_message", new_callable=AsyncMock):
                r_pending = await client.post(
                    "/webhooks/rub_gateway",
                    data={
                        "MERCHANT_ID": "999",
                        "AMOUNT": "199.00",
                        "intid": "1002",
                        "MERCHANT_ORDER_ID": str(tx2_id),
                        "SIGN": valid_sign_2,
                    },
                )
                assert r_pending.status_code == 200
                assert r_pending.text == "YES"

        async with factory() as sess:
            tx2_db = await sess.get(Transaction, tx2_id)
            ref_after_2 = await sess.get(User, referrer_id)
            assert tx2_db is not None and tx2_db.status == TransactionStatus.PAID_PENDING_PROVISION
            assert ref_after_2 is not None and ref_after_2.balance_rub == Decimal("59.70")

        # 5. Фоновый воркер retry_pending_provisions восстанавливает выдачу, когда 3X-UI ожил
        mock_bot = AsyncMock()
        with patch("app.tasks.scheduler.async_session_factory", factory), \
             patch("app.services.vpn.XUIClient.from_server", return_value=mock_xui):
            provisioned = await retry_pending_provisions(bot=mock_bot)
            assert provisioned == 1

        async with factory() as sess:
            tx2_done = await sess.get(Transaction, tx2_id)
            ref_final = await sess.get(User, referrer_id)
            buyer_final = await sess.get(User, buyer_id)
            assert tx2_done is not None and tx2_done.status == TransactionStatus.COMPLETED
            # Рефералка не начислилась дважды при ретрае
            assert ref_final is not None and ref_final.balance_rub == Decimal("59.70")
            sub_final = buyer_final.subscription
            assert sub_final is not None
            assert sub_final.xui_client_uuid == orig_uuid
            assert sub_final.sub_id == orig_sub_id
            delta_days = round((sub_final.expires_at - exp_after_1).total_seconds() / 86400)
            assert delta_days == 30
    finally:
        app.dependency_overrides.clear()
        await eng.dispose()


@pytest.mark.asyncio
async def test_e2e_unified_subscription_formats_routing_and_access_control(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "XUI_MOBILE_INBOUND_ID", 2)
    monkeypatch.setattr(settings, "XUI_EXTRA_INBOUND_IDS", "")

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db_session] = override_get_db
    try:
        repo = Repository(db_session)
        user, _ = await repo.get_or_create_user(telegram_id=611001, username="sub_formats_u")
        mock_xui = AsyncMock(spec=XUIClient)
        mock_xui.add_client = AsyncMock(return_value=None)
        mock_xui.get_inbound_reality_info = AsyncMock(
            side_effect=lambda inbound_id: XUIInboundRealityInfo(
                inbound_id=inbound_id,
                port=2084 if inbound_id == 2 else 443,
                server_names=["ghostfacevpn.duckdns.org"],
                public_key="TEST_PBK_KEY_XYZ",
                short_id="abcd1234",
                fingerprint="chrome",
                spider_x="/",
                client_uuids=set(),
            )
        )

        vpn = VPNService(db_session, xui_client_override=mock_xui)
        sub = await vpn.grant_or_extend_subscription(user, days=30)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with patch("app.services.vpn.XUIClient.from_server", return_value=mock_xui):
                # 1. JSON (Happ / v2rayTun / Hiddify)
                r_json = await client.get(
                    f"/sub/mobile/{sub.sub_id}",
                    headers={"User-Agent": "Happ/1.0"},
                )
                assert r_json.status_code == 200
                jdata = r_json.json()
                assert len(jdata["profiles"]) >= 2
                rules = jdata["routing"]["rules"]
                assert any(
                    r.get("action") == "block" and "geosite:category-ads-all" in r.get("domain", [])
                    for r in rules
                )
                assert any(
                    r.get("action") == "direct"
                    and "geosite:category-ru" in r.get("domain", [])
                    and "domain:ru" in r.get("domain", [])
                    for r in rules
                )
                assert any(
                    r.get("action") == "direct" and "geoip:ru" in r.get("ip", [])
                    for r in rules
                )
                assert any(r.get("action") == "proxy" for r in rules)

                # 2. YAML (Clash / Mihomo)
                r_yaml = await client.get(f"/sub/mobile/{sub.sub_id}?format=yaml")
                assert r_yaml.status_code == 200
                assert "GEOSITE,category-ads-all,REJECT" in r_yaml.text
                assert "GEOSITE,category-ru,DIRECT" in r_yaml.text
                assert "DOMAIN-SUFFIX,ru,DIRECT" in r_yaml.text
                assert "GEOIP,RU,DIRECT" in r_yaml.text
                assert "MATCH,👻 GhostFaceVPN" in r_yaml.text

                # 3. Base64 VLESS (Shadowrocket / v2rayN)
                r_b64 = await client.get(f"/sub/mobile/{sub.sub_id}?format=base64")
                assert r_b64.status_code == 200
                decoded = base64.b64decode(r_b64.text).decode("utf-8")
                lines = [ln for ln in decoded.splitlines() if ln.strip()]
                assert len(lines) >= 2
                assert all(ln.startswith(f"vless://{sub.xui_client_uuid}@") for ln in lines)
                assert "GhostFaceVPN" in unquote(lines[0])

                # 4. Забаненный пользователь -> 403
                user.is_banned = True
                await db_session.commit()
                r_banned = await client.get(f"/sub/mobile/{sub.sub_id}")
                assert r_banned.status_code == 403

                # 5. Разбаненный, но истёкший пользователь -> 403
                user.is_banned = False
                sub.expires_at = utcnow() - timedelta(hours=1)
                await db_session.commit()
                r_expired = await client.get(f"/sub/mobile/{sub.sub_id}")
                assert r_expired.status_code == 403
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_e2e_reissue_cooldown_extra_device_qr_memory_and_zip_backup(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "e2e_ops.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=eng, class_=AsyncSession, expire_on_commit=False)

    try:
        async with factory() as sess:
            await seed_initial_data(sess)
            repo = Repository(sess)
            user, _ = await repo.get_or_create_user(telegram_id=621001, username="ops_tester")

            mock_xui = AsyncMock(spec=XUIClient)
            mock_xui.add_client = AsyncMock(return_value=None)
            mock_xui.update_client = AsyncMock(return_value=None)
            mock_xui.delete_client_by_uuid = AsyncMock(return_value=None)

            vpn = VPNService(sess, xui_client_override=mock_xui)
            sub = await vpn.grant_or_extend_subscription(user, days=30)
            old_uuid = sub.xui_client_uuid
            old_sub_id = sub.sub_id
            old_exp = sub.expires_at

            # 1. Сброс ключа и блокировка повторного сброса раньше 24ч
            reissued = await vpn.reissue_key(user, force_by_admin=False)
            assert reissued.xui_client_uuid != old_uuid
            assert reissued.sub_id != old_sub_id
            assert abs((reissued.expires_at - old_exp).total_seconds()) < 2

            with pytest.raises(ValueError, match="не чаще 1 раза"):
                await vpn.reissue_key(user, force_by_admin=False)

            # 2. Докупка слота устройства
            updated_sub = await vpn.add_extra_device_slot(user, slots=1)
            assert updated_sub.limit_ip == settings.DEFAULT_LIMIT_IP + 1
            assert updated_sub.extra_devices == 1

            # 3. Генерация QR-кода в памяти без утечек памяти
            tracemalloc.start()
            gc.collect()
            png_first = generate_branded_qr_card(
                subscription_url=updated_sub.subscription_url,
                sub_id=updated_sub.sub_id,
                expires_str="21.12.2026 12:00",
                limit_ip=updated_sub.limit_ip,
            )
            assert png_first.startswith(b"\x89PNG")
            snap_before = tracemalloc.take_snapshot()
            for _ in range(5):
                b = generate_branded_qr_card(
                    subscription_url=updated_sub.subscription_url,
                    sub_id=updated_sub.sub_id,
                    expires_str="21.12.2026 12:00",
                    limit_ip=updated_sub.limit_ip,
                )
                assert b.startswith(b"\x89PNG")
                del b
            gc.collect()
            snap_after = tracemalloc.take_snapshot()
            current_mem, _ = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            diffs = snap_after.compare_to(snap_before, "lineno")
            growth_bytes = sum(d.size_diff for d in diffs if d.size_diff > 0)
            # После сборки мусора прирост не должен накапливать 5 копий холста
            assert growth_bytes < 5 * 1024 * 1024, f"Unexpected memory growth: {growth_bytes}"
            assert current_mem < 15 * 1024 * 1024

        # 4. Формирование ZIP-бэкапа базы данных и проверка его содержимого
        mock_bot = AsyncMock()
        with patch("app.tasks.scheduler.async_session_factory", factory):
            ok_backup = await create_and_send_database_backup(bot=mock_bot, target_chat_id=99999)
        assert ok_backup is True
        mock_bot.send_document.assert_awaited_once()
        doc = mock_bot.send_document.await_args.kwargs["document"]
        assert doc.filename.endswith(".zip")
        with zipfile.ZipFile(io.BytesIO(doc.data), mode="r") as zf:
            names = set(zf.namelist())
            assert {"dump.json", "dump.sql"} <= names
            dump_json = json.loads(zf.read("dump.json").decode("utf-8"))
            assert len(dump_json["users"]) >= 1
            assert len(dump_json["subscriptions"]) >= 1
            assert "INSERT INTO users" in zf.read("dump.sql").decode("utf-8")
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_e2e_concurrency_stress_30_parallel_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "XUI_MOBILE_INBOUND_ID", 2)
    monkeypatch.setattr(settings, "XUI_EXTRA_INBOUND_IDS", "")

    db_file = tmp_path / "stress_30.db"
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        echo=False,
        connect_args={"timeout": 30},
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=eng, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = override_get_db
    try:
        async with factory() as seed_sess:
            await seed_initial_data(seed_sess)
            repo = Repository(seed_sess)
            user, _ = await repo.get_or_create_user(telegram_id=631001, username="stress_hero")
            mock_xui = AsyncMock(spec=XUIClient)
            mock_xui.add_client = AsyncMock(return_value=None)
            mock_xui.get_inbound_reality_info = AsyncMock(return_value=None)
            vpn = VPNService(seed_sess, xui_client_override=mock_xui)
            sub = await vpn.grant_or_extend_subscription(user, days=30)
            sub_id = sub.sub_id

        init_data = _make_valid_init_data(
            {"id": 631001, "first_name": "Stress", "username": "stress_hero", "language_code": "ru"},
            settings.BOT_TOKEN,
        )

        mock_xui_rt = AsyncMock(spec=XUIClient)
        mock_xui_rt.get_inbound_reality_info = AsyncMock(return_value=None)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            async def timed_get(url: str, headers: dict[str, str] | None = None) -> tuple[int, float]:
                t0 = time.perf_counter()
                r = await client.get(url, headers=headers or {})
                return r.status_code, (time.perf_counter() - t0) * 1000.0

            async def timed_card() -> tuple[int, float]:
                t0 = time.perf_counter()
                async with factory() as s:
                    r_sub = await Repository(s).get_subscription_by_sub_id(sub_id)
                    assert r_sub is not None
                    card = await format_subscription_card(r_sub, user=r_sub.user)
                    assert "GhostVPN" in card
                return 200, (time.perf_counter() - t0) * 1000.0

            with patch("app.services.vpn.XUIClient.from_server", return_value=mock_xui_rt):
                tasks = (
                    [timed_get("/health") for _ in range(10)]
                    + [timed_get(f"/sub/mobile/{sub_id}") for _ in range(10)]
                    + [timed_get("/api/webapp/me", {"X-Telegram-Init-Data": init_data}) for _ in range(10)]
                    + [timed_card() for _ in range(5)]
                )
                results = await asyncio.gather(*tasks)

        statuses = [st for st, _ in results]
        latencies = sorted(ms for _, ms in results)
        assert all(st == 200 for st in statuses), f"Non-200 statuses: {statuses}"
        assert len(latencies) == 35
        assert max(latencies) < 5000.0
    finally:
        app.dependency_overrides.clear()
        await eng.dispose()
