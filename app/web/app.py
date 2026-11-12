from __future__ import annotations

import html
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from aiogram.types import Update
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.profile import format_subscription_card
from app.bot.keyboards.user import back_to_menu_kb, subscription_card_kb
from app.bot.loader import bot, dp
from app.core.config import settings
from app.core.logging import logger, send_admin_alert, setup_logging
from app.core.security import (
    verify_cryptobot_signature,
    verify_rub_gateway_signature,
    verify_telegram_secret,
)
from app.db.models import (
    PaymentProviderType,
    Transaction,
    User,
)
from app.db.repositories import Repository
from app.db.session import engine, get_db_session, init_db_with_retry
from app.services.vpn import VPNService
from app.tasks.scheduler import create_scheduler

DEEP_LINK_SCHEMES: dict[str, tuple[str, str]] = {
    "happ": ("Happ", "happ://add/{sub_url}"),
    "v2raytun": ("v2rayTun", "v2raytun://import/{sub_url}"),
    "hiddify": ("Hiddify", "hiddify://import/{sub_url}"),
}


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
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f172a;
      color: #f8fafc;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      padding: 16px;
      box-sizing: border-box;
    }}
    .card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 16px;
      max-width: 440px;
      width: 100%;
      padding: 28px 24px;
      text-align: center;
      box-shadow: 0 20px 25px -5px rgba(0,0,0,0.5);
    }}
    h1 {{ font-size: 20px; margin: 0 0 12px; }}
    p {{ color: #94a3b8; font-size: 14px; line-height: 1.5; margin: 0 0 20px; }}
    .btn {{
      display: block;
      width: 100%;
      padding: 14px;
      border-radius: 10px;
      border: none;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      text-decoration: none;
      margin-bottom: 12px;
      box-sizing: border-box;
    }}
    .btn-primary {{ background: #3b82f6; color: #ffffff; }}
    .btn-secondary {{ background: #334155; color: #e2e8f0; }}
    .sub-box {{
      background: #0f172a;
      border: 1px dashed #475569;
      padding: 10px;
      border-radius: 8px;
      font-family: monospace;
      font-size: 12px;
      word-break: break-all;
      color: #38bdf8;
      margin-bottom: 16px;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1>👻 Подключение в {safe_title}</h1>
    <p>Перенаправляем вас в приложение <b>{safe_title}</b>. Если оно не открылось автоматически, нажмите кнопку ниже или скопируйте ссылку подписки вручную.</p>
    <a id="openBtn" class="btn btn-primary" href="#">📲 Открыть в {safe_title}</a>
    <div class="sub-box">{safe_sub}</div>
    <button class="btn btn-secondary" onclick="copySub()">📋 Скопировать ссылку подписки</button>
    <p id="statusMsg" style="margin-top:8px;color:#4ade80;display:none;">✅ Ссылка скопирована в буфер обмена!</p>
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
    title="GhostVPN Service",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/health")
@app.get("/ping")
async def health_check(session: AsyncSession = Depends(get_db_session)) -> JSONResponse:
    """Эндпоинт для анти-сна Render (каждые 10 мин) с проверкой соединения БД."""
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
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=(
                        "🎉 <b>Оплата через CryptoBot получена! Ваша подписка активирована:</b>\n\n"
                        + format_subscription_card(sub)
                    ),
                    reply_markup=subscription_card_kb(sub),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            else:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text="✅ <b>Оплата получена!</b> Активируем ваш доступ на VPN-сервере...",
                    reply_markup=back_to_menu_kb(),
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
            "⚠️ Отклонён вебхук рублёвого шлюза с невалидной подписью!",
            critical=True,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    data: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    if data.get("status") not in ("succeeded", "paid"):
        return {"ok": True}

    ext_id = str(data.get("external_id") or "")
    tx_id_raw = data.get("transaction_id")

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
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=(
                        "🎉 <b>Оплата получена! Ваша подписка активирована:</b>\n\n"
                        + format_subscription_card(sub)
                    ),
                    reply_markup=subscription_card_kb(sub),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            else:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text="✅ <b>Оплата получена!</b> Активируем ваш доступ...",
                    reply_markup=back_to_menu_kb(),
                    parse_mode="HTML",
                )
        except Exception:
            pass

    return {"ok": True}


@app.get("/connect/{app_name}/{sub_id}", response_class=HTMLResponse)
async def connect_deep_link(
    app_name: str,
    sub_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """
    Генерирует HTML-страницу с авто-редиректом (`window.location.href`) в схему приложения
    (`happ://add/{sub_url}`, `v2raytun://import/{sub_url}`, `hiddify://import/{sub_url}`)
    и кнопкой ручного копирования Subscription URL.
    """
    app_key = app_name.lower().strip()
    if app_key not in DEEP_LINK_SCHEMES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unsupported client app")

    # Валидация sub_id на границе доверия (только алфавитно-цифровые символы)
    if not sub_id.isalnum() or len(sub_id) > 64:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid subscription ID")

    repo = Repository(session)
    sub = await repo.get_subscription_by_sub_id(sub_id)
    if sub is not None:
        sub_url = sub.subscription_url
    else:
        sub_url = f"{settings.XUI_SUB_BASE_URL.rstrip('/')}/{sub_id}"

    app_title, scheme_tpl = DEEP_LINK_SCHEMES[app_key]
    deep_link = scheme_tpl.format(sub_url=sub_url)
    html_body = render_connect_html(app_title=app_title, deep_link=deep_link, sub_url=sub_url)
    return HTMLResponse(content=html_body, status_code=status.HTTP_200_OK)
