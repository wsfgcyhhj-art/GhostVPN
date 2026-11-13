from __future__ import annotations

import html
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncGenerator

from aiogram.types import LabeledPrice, Update
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.profile import format_subscription_card
from app.bot.keyboards.user import back_to_menu_kb, subscription_card_kb
from app.bot.loader import bot, dp
from app.bot.utils import format_dt_msk
from app.core.config import settings
from app.core.logging import logger, send_admin_alert, setup_logging
from app.core.security import (
    verify_cryptobot_signature,
    verify_rub_gateway_signature,
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
from app.services.payments.rub_gateway import PlategaProvider
from app.services.payments.stars import TelegramStarsProvider
from app.services.vpn import (
    VPNService,
    build_tls_fragment_profile,
    calculate_plan_renewal_price_with_slots,
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
    tg_id = int(tg_user_data["id"])
    repo = Repository(session)
    user, _ = await repo.get_or_create_user(
        telegram_id=tg_id,
        username=tg_user_data.get("username"),
        full_name=f"{tg_user_data.get('first_name', '')} {tg_user_data.get('last_name', '')}".strip(),
        language_code=tg_user_data.get("language_code"),
    )
    sub = user.subscription
    plans = await repo.get_active_plans()
    extra_devs = sub.extra_devices if sub else 0
    base_web = settings.WEBHOOK_BASE_URL.rstrip("/")

    return {
        "telegram_id": user.telegram_id,
        "username": user.username,
        "balance_rub": str(user.balance_rub),
        "auto_renew": user.auto_renew,
        "is_active": bool(sub and sub.is_currently_valid),
        "sub_id": sub.sub_id if sub else None,
        "sub_url": sub.subscription_url if sub else "",
        "mobile_sub_url": f"{base_web}/sub/mobile/{sub.sub_id}" if sub else "",
        "days_left": sub.days_left if sub else 0,
        "total_days": sub.total_period_days if sub else 30,
        "expires_str": format_dt_msk(sub.expires_at) if sub else "—",
        "used_traffic_gb": sub.used_traffic_gb if sub else 0.0,
        "online_ips": sub.online_ips_count if sub else 0,
        "limit_ip": sub.limit_ip if sub else settings.DEFAULT_LIMIT_IP,
        "ref_link": f"https://t.me/{settings.BOT_USERNAME}?start=ref_{user.telegram_id}",
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
    if not verified or not isinstance(verified.get("user"), dict):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Telegram initData")

    tg_id = int(verified["user"]["id"])
    repo = Repository(session)
    user, _ = await repo.get_or_create_user(telegram_id=tg_id)

    if await repo.count_recent_pending_transactions(user.id, minutes=10) >= 5:
        raise HTTPException(status_code=429, detail="Слишком много неоплаченных счетов, подождите пару минут.")

    plan = await repo.get_plan_by_id(req.plan_id)
    if plan is None or not plan.is_active:
        raise HTTPException(status_code=404, detail="Тариф не найден")

    extra_devs = user.subscription.extra_devices if user.subscription else 0
    to_pay_rub = calculate_plan_renewal_price_with_slots(plan, extra_devices=extra_devs)
    provider_enum = PaymentProviderType(req.provider)

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

    inv = await PlategaProvider().create_invoice(
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
@app.get("/sub/mobile/{sub_id}")
async def get_mobile_fragment_subscription(
    sub_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    if not sub_id.isalnum() or len(sub_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid sub_id")

    repo = Repository(session)
    sub = await repo.get_subscription_by_sub_id(sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    profile_json = build_tls_fragment_profile(sub)
    return JSONResponse(
        content=profile_json,
        headers={
            "Profile-Title": "GhostVPN-Mobile-Stealth",
            "Subscription-Userinfo": f"upload=0; download={sub.used_traffic_bytes}; total=0; expire={int(sub.expires_at.timestamp())}",
        },
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

    stmt = select(Transaction).where(Transaction.external_id == invoice_id)
    if tx_payload.isdigit():
        stmt = select(Transaction).where(
            (Transaction.external_id == invoice_id) | (Transaction.id == int(tx_payload))
        )
    res = await session.execute(stmt)
    tx = res.scalar_one_or_none()
    if tx is None:
        logger.warning("CryptoBot webhook: транзакция для invoice_id=%s не найдена", invoice_id)
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


@app.post("/webhooks/rub_gateway")
async def rub_gateway_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    x_gateway_signature: str | None = Header(default=None),
) -> dict[str, bool]:
    raw_body = await request.body()
    if not verify_rub_gateway_signature(
        raw_body, x_gateway_signature, settings.RUB_GATEWAY_WEBHOOK_SECRET
    ):
        await send_admin_alert(
            bot,
            "⚠️ Отклонён вебхук рублёвого шлюза (Platega) с невалидной подписью!",
            critical=True,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    data: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    st = str(data.get("status", "")).lower()
    if st not in ("succeeded", "paid", "confirmed", "completed"):
        return {"ok": True}

    ext_id = str(data.get("external_id") or data.get("transactionId") or "")
    tx_id_raw = data.get("transaction_id") or data.get("orderId")

    stmt = select(Transaction).where(
        Transaction.provider == PaymentProviderType.RUB_GATEWAY,
        Transaction.external_id == ext_id,
    )
    if tx_id_raw and str(tx_id_raw).isdigit():
        stmt = select(Transaction).where(Transaction.id == int(tx_id_raw))

    res = await session.execute(stmt)
    tx = res.scalar_one_or_none()
    if tx is None:
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

    return {"ok": True}


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
    Поддерживает параметр `?mode=mobile` для импорта профиля с TLS Fragment.
    """
    app_key = app_name.lower().strip()
    if app_key not in DEEP_LINK_SCHEMES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unsupported client app")

    if not sub_id.isalnum() or len(sub_id) > 64:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid subscription ID")

    repo = Repository(session)
    sub = await repo.get_subscription_by_sub_id(sub_id)
    if mode == "mobile":
        sub_url = f"{settings.WEBHOOK_BASE_URL.rstrip('/')}/sub/mobile/{sub_id}"
    elif sub is not None:
        sub_url = sub.subscription_url
    else:
        sub_url = f"{settings.XUI_SUB_BASE_URL.rstrip('/')}/{sub_id}"

    app_title, scheme_tpl = DEEP_LINK_SCHEMES[app_key]
    deep_link = scheme_tpl.format(sub_url=sub_url)
    html_body = render_connect_html(app_title=app_title, deep_link=deep_link, sub_url=sub_url)
    return HTMLResponse(content=html_body, status_code=status.HTTP_200_OK)
