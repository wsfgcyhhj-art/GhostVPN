from __future__ import annotations

import html
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncGenerator
from urllib.parse import parse_qsl

from aiogram.types import LabeledPrice, Update
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.profile import format_subscription_card
from app.bot.keyboards.user import (
    SHARE_FRIEND_TEXT,
    back_to_menu_kb,
    build_friend_share_url,
    subscription_card_kb,
)
from app.bot.loader import bot, dp
from app.bot.utils import format_dt_msk
from app.core.config import settings
from app.core.logging import logger, send_admin_alert, setup_logging
from app.core.security import (
    format_fk_amount,
    verify_cryptobot_signature,
    verify_freekassa_signature,
    verify_telegram_secret,
    verify_telegram_webapp_init_data,
)
from app.db.models import (
    PaymentProviderType,
    Transaction,
    TransactionStatus,
    TransactionType,
    User,
)
from app.db.repositories import Repository
from app.db.session import engine, get_db_session, init_db_with_retry
from app.services.payments.cryptobot import CryptoBotProvider
from app.services.payments.rub_gateway import FreeKassaProvider
from app.services.payments.stars import TelegramStarsProvider
from app.services.vpn import (
    VPNService,
    build_subscription_url,
    calculate_plan_renewal_price_with_slots,
    render_mobile_yaml_profile,
)
from app.tasks.scheduler import create_scheduler

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
WEBAPP_HTML_PATH = STATIC_DIR / "webapp" / "index.html"

DEEP_LINK_SCHEMES: dict[str, tuple[str, str]] = {
    "happ": ("Happ", "happ://add/{sub_url}"),
    "v2raytun": ("v2rayTun", "v2raytun://import/{sub_url}"),
    "hiddify": ("Hiddify", "hiddify://import/{sub_url}"),
}


class WebAppPayRequest(BaseModel):
    plan_id: int
    provider: str = "RUB_GATEWAY"


def render_connect_html(app_title: str, deep_link: str, sub_url: str) -> str:
    safe_title = html.escape(app_title)
    safe_sub = html.escape(sub_url)
    safe_deep_js = json.dumps(deep_link)
    safe_sub_js = json.dumps(sub_url)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>GhostVPN — Подключение в {safe_title}</title>
  <link rel="icon" type="image/png" href="/static/images/ghostface_base.png" />
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #070709;
      color: #F8F9FA;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      padding: 16px;
      box-sizing: border-box;
    }}
    .card {{
      background: #111218;
      border: 1px solid #FF1E42;
      box-shadow: 0 0 25px rgba(255, 30, 66, 0.3);
      border-radius: 18px;
      max-width: 440px;
      width: 100%;
      padding: 28px 24px;
      text-align: center;
    }}
    .avatar {{
      width: 68px;
      height: 68px;
      border-radius: 50%;
      border: 2px solid #FF1E42;
      margin-bottom: 12px;
      object-fit: cover;
    }}
    h1 {{ font-size: 20px; margin: 0 0 10px; }}
    p {{ color: #94a3b8; font-size: 14px; line-height: 1.5; margin: 0 0 20px; }}
    .btn {{
      display: block;
      width: 100%;
      padding: 14px;
      border-radius: 10px;
      border: none;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
      margin-bottom: 12px;
      box-sizing: border-box;
    }}
    .btn-primary {{ background: #FF1E42; color: #ffffff; }}
    .btn-secondary {{ background: #181a24; color: #e2e8f0; border: 1px solid #262938; }}
    .sub-box {{
      background: #070709;
      border: 1px dashed #FF1E42;
      padding: 10px;
      border-radius: 8px;
      font-family: monospace;
      font-size: 12px;
      word-break: break-all;
      color: #F8F9FA;
      margin-bottom: 16px;
    }}
  </style>
</head>
<body>
  <div class="card">
    <img class="avatar" src="/static/images/ghostface_base.png" alt="GhostVPN" />
    <h1>👻 Подключение в {safe_title}</h1>
    <p>Перенаправляем тебя в приложение <b>{safe_title}</b>. Если оно не открылось автоматически, нажми кнопку ниже или скопируй ссылку подписки вручную.</p>
    <a id="openBtn" class="btn btn-primary" href="#">📲 Открыть в {safe_title}</a>
    <div class="sub-box">{safe_sub}</div>
    <button class="btn btn-secondary" onclick="copySub()">📋 Скопировать ссылку подписки</button>
    <p id="statusMsg" style="margin-top:8px;color:#22C55E;display:none;">✅ Ссылка скопирована в буфер обмена!</p>
  </div>
  <script>
    const deepLink = {safe_deep_js};
    const subUrl = {safe_sub_js};
    document.getElementById("openBtn").setAttribute("href", deepLink);
    window.location.href = deepLink;
    function copySub() {{
      navigator.clipboard.writeText(subUrl).then(() => {{
        document.getElementById("statusMsg").style.display = "block";
      }});
    }}
  </script>
</body>
</html>"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    setup_logging()
    await init_db_with_retry()

    scheduler = create_scheduler(bot)
    scheduler.start()

    webhook_url = f"{settings.WEBHOOK_BASE_URL.rstrip('/')}/webhook/telegram"
    try:
        await bot.set_webhook(
            url=webhook_url,
            secret_token=settings.WEBHOOK_SECRET,
            drop_pending_updates=False,
        )
        logger.info("Telegram Webhook установлен: %s", webhook_url)
    except Exception as exc:
        logger.warning("Не удалось установить Telegram Webhook при старте: %s", exc)

    yield

    scheduler.shutdown(wait=False)
    try:
        await bot.session.close()
    except Exception:
        pass
    await engine.dispose()


app = FastAPI(
    title="GhostVPN Service (Ghostface Edition)",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.api_route("/health", methods=["GET", "HEAD"])
@app.api_route("/ping", methods=["GET", "HEAD"])
async def health_check(session: AsyncSession = Depends(get_db_session)) -> JSONResponse:
    """Эндпоинт для анти-сна Render (GET и HEAD для UptimeRobot) с проверкой соединения БД."""
    db_ok = False
    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        logger.error("Healthcheck DB error: %s", exc)

    return JSONResponse(
        status_code=status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ok" if db_ok else "degraded", "database": "connected" if db_ok else "error"},
    )


# --- Telegram Mini App (/webapp) ---
@app.get("/webapp", response_class=HTMLResponse)
async def serve_webapp() -> HTMLResponse:
    if not WEBAPP_HTML_PATH.exists():
        raise HTTPException(status_code=404, detail="WebApp index not found")
    return HTMLResponse(content=WEBAPP_HTML_PATH.read_text(encoding="utf-8"), status_code=200)


@app.get("/api/webapp/me")
async def webapp_me(
    x_telegram_init_data: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    verified = verify_telegram_webapp_init_data(x_telegram_init_data or "", settings.BOT_TOKEN)
    if not verified or not isinstance(verified.get("user"), dict):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Telegram initData")

    tg_user_data = verified["user"]
    if "id" not in tg_user_data:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing user id in initData")

    tg_id = int(tg_user_data["id"])
    repo = Repository(session)
    user, _ = await repo.get_or_create_user(
        telegram_id=tg_id,
        username=tg_user_data.get("username"),
        full_name=f"{tg_user_data.get('first_name', '')} {tg_user_data.get('last_name', '')}".strip(),
        language_code=tg_user_data.get("language_code"),
    )
    if user.is_banned:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Доступ ограничен администратором")

    sub = user.subscription
    plans = await repo.get_active_plans()
    extra_devs = sub.extra_devices if sub else 0
    unified_sub_url = build_subscription_url(sub.sub_id, server=sub.server) if sub else ""

    return {
        "telegram_id": user.telegram_id,
        "username": user.username,
        "balance_rub": str(user.balance_rub),
        "auto_renew": user.auto_renew,
        "is_active": bool(sub and sub.is_currently_valid),
        "sub_id": sub.sub_id if sub else None,
        "sub_url": unified_sub_url,
        "mobile_sub_url": unified_sub_url,
        "days_left": sub.days_left if sub else 0,
        "total_days": sub.total_period_days if sub else 30,
        "expires_str": format_dt_msk(sub.expires_at) if sub else "—",
        "used_traffic_gb": sub.used_traffic_gb if sub else 0.0,
        "online_ips": sub.online_ips_count if sub else 0,
        "limit_ip": sub.limit_ip if sub else settings.DEFAULT_LIMIT_IP,
        "ref_link": f"https://t.me/{settings.BOT_USERNAME}?start=ref_{user.telegram_id}",
        "share_url": build_friend_share_url(user.telegram_id),
        "share_text": SHARE_FRIEND_TEXT,
        "plans": [
            {
                "id": p.id,
                "name": p.name,
                "days": p.days,
                "price_rub": str(calculate_plan_renewal_price_with_slots(p, extra_devices=extra_devs)),
            }
            for p in plans
        ],
    }


@app.post("/api/webapp/pay")
async def webapp_create_payment(
    req: WebAppPayRequest,
    x_telegram_init_data: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    verified = verify_telegram_webapp_init_data(x_telegram_init_data or "", settings.BOT_TOKEN)
    if not verified or not isinstance(verified.get("user"), dict) or "id" not in verified["user"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Telegram initData")

    tg_id = int(verified["user"]["id"])
    repo = Repository(session)
    user, _ = await repo.get_or_create_user(telegram_id=tg_id)
    if user.is_banned:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Доступ ограничен администратором")

    if await repo.count_recent_pending_transactions(user.id, minutes=10) >= 5:
        raise HTTPException(status_code=429, detail="Слишком много неоплаченных счетов, подождите пару минут.")

    plan = await repo.get_plan_by_id(req.plan_id)
    if plan is None or not plan.is_active:
        raise HTTPException(status_code=404, detail="Тариф не найден")

    try:
        provider_enum = PaymentProviderType(req.provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Неподдерживаемый метод оплаты") from exc

    extra_devs = user.subscription.extra_devices if user.subscription else 0
    to_pay_rub = calculate_plan_renewal_price_with_slots(plan, extra_devices=extra_devs)

    tx = Transaction(
        user_id=user.id,
        plan_id=plan.id,
        tx_type=TransactionType.SUBSCRIPTION,
        provider=provider_enum,
        currency="XTR" if provider_enum == PaymentProviderType.STARS else "RUB",
        amount_currency=to_pay_rub,
        amount_rub=to_pay_rub,
        balance_used_rub=Decimal("0.00"),
        status=TransactionStatus.PENDING,
    )
    session.add(tx)
    await session.commit()
    await session.refresh(tx)

    desc = f"GhostVPN: {plan.name} ({plan.days} дн.)"
    if provider_enum == PaymentProviderType.STARS:
        inv = await TelegramStarsProvider().create_invoice(
            transaction_id=tx.id,
            amount_rub=to_pay_rub,
            description=desc,
            user_telegram_id=user.telegram_id,
        )
        tx.external_id = inv.external_id
        await session.commit()
        try:
            await bot.send_invoice(
                chat_id=user.telegram_id,
                title=f"GhostVPN — {plan.name}",
                description=desc,
                payload=f"tx:{tx.id}",
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label=plan.name, amount=inv.stars_amount or 1)],
            )
        except Exception:
            pass
        return {"ok": True, "message": "Счёт в Telegram Stars отправлен в чат бота!"}

    if provider_enum == PaymentProviderType.CRYPTOBOT:
        inv = await CryptoBotProvider().create_invoice(
            transaction_id=tx.id,
            amount_rub=to_pay_rub,
            description=desc,
            user_telegram_id=user.telegram_id,
        )
        tx.external_id = inv.external_id
        tx.payment_url = inv.payment_url
        await session.commit()
        return {"ok": True, "payment_url": inv.payment_url}

    inv = await FreeKassaProvider().create_invoice(
        transaction_id=tx.id,
        amount_rub=to_pay_rub,
        description=desc,
        user_telegram_id=user.telegram_id,
    )
    tx.external_id = inv.external_id
    tx.payment_url = inv.payment_url
    await session.commit()
    return {"ok": True, "payment_url": inv.payment_url}


# --- Smart Mobile Anti-DPI Subscription Endpoint (/sub/mobile/{sub_id}) ---
@app.get("/sub/mobile/{sub_id}", response_model=None)
async def get_mobile_fragment_subscription(
    sub_id: str,
    request: Request,
    format: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """
    Умный мульти-эндпоинт для обхода мобильных глушилок (LTE / ТСПУ).
    Считывает актуальные `serverNames` (SNI), `publicKey`, `shortId` и `port` из основного
    (`XUI_INBOUND_ID`) и резервного (`XUI_MOBILE_INBOUND_ID`) инбаундов в 3X-UI и отдаёт оба подключения:
      1) «🇳🇱 GhostVPN • LTE Основной (Yandex SNI + Fragment)»
      2) «🇳🇱 GhostVPN • LTE Резерв (VK SNI + Fragment)»
    В зависимости от `User-Agent` (Happ / v2rayTun / Hiddify / Clash / Shadowrocket) или `?format=yaml|base64|json`
    возвращает JSON мульти-профиль, YAML-конфиг или Base64-подписку.
    """
    if not sub_id.isalnum() or not (4 <= len(sub_id) <= 64):
        raise HTTPException(status_code=400, detail="Invalid sub_id")

    repo = Repository(session)
    sub = await repo.get_subscription_by_sub_id(sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    user = sub.user or await session.get(User, sub.user_id)
    if (user and user.is_banned) or not sub.is_currently_valid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Subscription is inactive or user is banned",
        )

    vpn_service = VPNService(session)
    multi_profile = await vpn_service.fetch_live_mobile_profile(sub)

    common_headers = {
        "Profile-Title": "GhostVPN-Mobile-Stealth",
        "Subscription-Userinfo": (
            f"upload=0; download={sub.used_traffic_bytes}; total=0; "
            f"expire={int(sub.expires_at.timestamp())}"
        ),
        "X-Routing-Policy": "ads=block;ru=direct;default=proxy",
    }

    ua = (request.headers.get("user-agent") or "").lower()
    fmt = (format or "").lower().strip()

    if fmt == "yaml" or (not fmt and any(k in ua for k in ("clash", "mihomo", "stash", "flclash"))):
        yaml_body = render_mobile_yaml_profile(multi_profile)
        return Response(
            content=yaml_body,
            media_type="text/yaml; charset=utf-8",
            headers=common_headers,
        )

    if fmt == "base64" or (
        not fmt and any(k in ua for k in ("v2rayn", "shadowrocket", "streisand", "foxray"))
    ):
        return PlainTextResponse(
            content=str(multi_profile["base64_sub"]),
            headers=common_headers,
        )

    return JSONResponse(
        content=multi_profile,
        headers=common_headers,
    )


@app.post("/webhook/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    if not verify_telegram_secret(x_telegram_bot_api_secret_token, settings.WEBHOOK_SECRET):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid secret token")

    payload = await request.json()
    update = Update.model_validate(payload, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


@app.post("/webhooks/cryptobot")
async def cryptobot_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    crypto_pay_api_signature: str | None = Header(default=None, alias="crypto-pay-api-signature"),
) -> dict[str, bool]:
    raw_body = await request.body()
    if not verify_cryptobot_signature(
        raw_body, crypto_pay_api_signature, settings.CRYPTOBOT_API_TOKEN
    ):
        await send_admin_alert(
            bot,
            "⚠️ Отклонён вебхук CryptoBot с невалидной подписью!",
            critical=True,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    data: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    if data.get("update_type") != "invoice_paid":
        return {"ok": True}

    invoice = data.get("payload", {}) or {}
    if invoice.get("status") != "paid":
        return {"ok": True}

    invoice_id = str(invoice.get("invoice_id", ""))
    tx_payload = str(invoice.get("payload", ""))

    stmt = select(Transaction).where(Transaction.external_id == invoice_id).with_for_update()
    if tx_payload.isdigit():
        stmt = (
            select(Transaction)
            .where((Transaction.external_id == invoice_id) | (Transaction.id == int(tx_payload)))
            .with_for_update()
        )
    res = await session.execute(stmt)
    tx = res.scalar_one_or_none()
    if tx is None:
        logger.warning("CryptoBot webhook: транзакция для invoice_id=%s не найдена", invoice_id)
        return {"ok": True}

    if tx.status == TransactionStatus.COMPLETED:
        return {"ok": True}

    vpn_service = VPNService(session)
    ok, sub = await vpn_service.process_successful_payment(tx, bot=bot)
    user = await session.get(User, tx.user_id)
    if user:
        try:
            if ok and sub:
                card = await format_subscription_card(sub, user=user)
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"🎉 <b>Оплата через CryptoBot получена! Защита активирована:</b>\n\n{card}",
                    reply_markup=subscription_card_kb(user, sub),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            else:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text="✅ <b>Оплата получена!</b> Активируем твой доступ на VPN-сервере...",
                    reply_markup=back_to_menu_kb(user.language),
                    parse_mode="HTML",
                )
        except Exception as exc:
            logger.warning("Не удалось отправить сообщение юзеру после вебхука CryptoBot: %s", exc)

    return {"ok": True}


@app.post("/webhooks/rub_gateway", response_class=PlainTextResponse)
async def rub_gateway_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> PlainTextResponse:
    """
    Обработчик уведомлений об оплате FreeKassa SCI.
    Проверяет MD5-подпись `md5("{MERCHANT_ID}:{AMOUNT}:{SECRET_2}:{MERCHANT_ORDER_ID}")`,
    сверяет сумму и идемпотентно активирует подписку / триал / слот с возвратом `PlainTextResponse("YES")`.
    """
    raw_body = await request.body()
    content_type = request.headers.get("content-type", "").lower()

    data: dict[str, Any] = dict(request.query_params)
    if raw_body:
        body_str = raw_body.decode("utf-8", errors="ignore").strip()
        if "application/json" in content_type or body_str.startswith("{"):
            try:
                parsed_json = json.loads(body_str)
                if isinstance(parsed_json, dict):
                    data.update(parsed_json)
            except Exception:
                pass
        else:
            data.update(dict(parse_qsl(body_str, keep_blank_values=True)))

    merchant_id = str(data.get("MERCHANT_ID") or data.get("merchant_id") or "")
    amount_raw = str(data.get("AMOUNT") or data.get("amount") or "")
    order_id = str(data.get("MERCHANT_ORDER_ID") or data.get("order_id") or "")
    sign = str(data.get("SIGN") or data.get("sign") or "")
    fk_intid = str(data.get("intid") or "")

    if not verify_freekassa_signature(
        merchant_id=merchant_id,
        amount_str=amount_raw,
        order_id=order_id,
        signature=sign,
        secret_2=settings.FREEKASSA_SECRET_2,
    ):
        await send_admin_alert(
            bot,
            f"⚠️ Отклонён вебхук FreeKassa с невалидной MD5-подписью (order={order_id})!",
            critical=True,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    if not order_id.isdigit():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid order ID")

    stmt = (
        select(Transaction)
        .where(
            Transaction.id == int(order_id),
            Transaction.provider == PaymentProviderType.RUB_GATEWAY,
        )
        .with_for_update()
    )
    res = await session.execute(stmt)
    tx = res.scalar_one_or_none()
    if tx is None:
        logger.warning("FreeKassa webhook: транзакция #%s не найдена", order_id)
        return PlainTextResponse("YES")

    try:
        paid_dec = Decimal(format_fk_amount(amount_raw))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid amount") from exc

    expected_dec = tx.amount_rub.quantize(Decimal("0.01"))
    if paid_dec < expected_dec:
        logger.error(
            "FreeKassa webhook: сумма платежа (%s) меньше ожидаемой (%s) для tx #%s",
            paid_dec,
            expected_dec,
            tx.id,
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Amount mismatch")

    if tx.status == TransactionStatus.COMPLETED:
        return PlainTextResponse("YES")

    if fk_intid:
        tx.external_id = f"fk_{fk_intid}"

    vpn_service = VPNService(session)
    ok, sub = await vpn_service.process_successful_payment(tx, bot=bot)
    user = await session.get(User, tx.user_id)
    if user:
        try:
            if ok and sub:
                card = await format_subscription_card(sub, user=user)
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"🎉 <b>Оплата получена! Защита активирована:</b>\n\n{card}",
                    reply_markup=subscription_card_kb(user, sub),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            else:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text="✅ <b>Оплата получена!</b> Активируем твой доступ...",
                    reply_markup=back_to_menu_kb(user.language),
                    parse_mode="HTML",
                )
        except Exception:
            pass

    return PlainTextResponse("YES")


@app.get("/connect/{app_name}/{sub_id}", response_class=HTMLResponse)
async def connect_deep_link(
    app_name: str,
    sub_id: str,
    mode: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """
    Генерирует HTML-страницу с авто-редиректом (`window.location.href`) в схему приложения
    (`happ://add/{sub_url}`, `v2raytun://import/{sub_url}`, `hiddify://import/{sub_url}`)
    и кнопкой ручного копирования Subscription URL.
    Выдаёт данные только для существующих, активных и не забаненных подписок.
    """
    app_key = app_name.lower().strip()
    if app_key not in DEEP_LINK_SCHEMES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unsupported client app")

    if not sub_id.isalnum() or not (4 <= len(sub_id) <= 64):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid subscription ID")

    repo = Repository(session)
    sub = await repo.get_subscription_by_sub_id(sub_id)
    if sub is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")

    user = sub.user or await session.get(User, sub.user_id)
    if (user and user.is_banned) or not sub.is_currently_valid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Subscription is inactive or user is banned",
        )

    sub_url = build_subscription_url(sub.sub_id, server=sub.server)

    app_title, scheme_tpl = DEEP_LINK_SCHEMES[app_key]
    deep_link = scheme_tpl.format(sub_url=sub_url)
    html_body = render_connect_html(app_title=app_title, deep_link=deep_link, sub_url=sub_url)
    return HTMLResponse(content=html_body, status_code=status.HTTP_200_OK)
