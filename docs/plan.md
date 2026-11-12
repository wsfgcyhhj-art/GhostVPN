# План реализации: GhostVPN (plan.md)

## 1. Технический контекст
- **Стек:** Python 3.11+, FastAPI, Uvicorn (1 worker), Aiogram 3.x, SQLAlchemy 2.0 (asyncpg), Alembic, HTTPX, APScheduler, QRCode + Pillow, Pydantic Settings v2.
- **Инфраструктура:** Render Free Web Service (512 MB RAM), Neon/Supabase PostgreSQL (Transaction Pooler compatible), 3X-UI v3.11.0 (VLESS + Reality).

## 2. Структура проекта
```text
GhostVPN/
├── alembic/
│   ├── versions/
│   │   └── 0001_initial.py
│   └── env.py
├── alembic.ini
├── app/
│   ├── __init__.py
│   ├── core/
│   │   ├── config.py          # Pydantic Settings (.env)
│   │   ├── logging.py         # Структурированное логирование и алерты в админ-чат
│   │   └── security.py        # Проверка подписей вебхуков (Telegram, CryptoBot, RubGateway)
│   ├── db/
│   │   ├── base.py            # DeclarativeBase
│   │   ├── models.py          # User, Server, Plan, Subscription, Transaction, PromoCode, PromoUsage
│   │   ├── session.py         # AsyncEngine (pool_pre_ping, statement_cache_size=0), retry & seed
│   │   └── repositories.py    # Сервисные операции с БД (транзакции, блокировки, выдача подписок)
│   ├── services/
│   │   ├── xui/
│   │   │   └── client.py      # Асинхронный клиент 3X-UI v3.11.0 (cookie auth, re-login, CRUD клиента)
│   │   ├── payments/
│   │   │   ├── base.py        # BasePaymentProvider (Strategy)
│   │   │   ├── stars.py       # Telegram Stars (XTR)
│   │   │   ├── cryptobot.py   # Crypto Pay API (@send) + HMAC-SHA256
│   │   │   └── rub_gateway.py # RubGatewayStub (YooKassa/Platega ready)
│   │   └── vpn.py             # Оркестратор выдачи/продления/сброса VPN и обработки PAID_PENDING_PROVISION
│   ├── bot/
│   │   ├── loader.py          # Инициализация Bot и Dispatcher
│   │   ├── middlewares.py     # Сессия БД в апдейтах, проверка бана
│   │   ├── states.py          # FSM состояния (промокод, админка, рассылка)
│   │   ├── keyboards/
│   │   │   ├── user.py        # Клавиатуры пользователя
│   │   │   └── admin.py       # Клавиатуры админ-панели
│   │   └── handlers/
│   │       ├── start.py       # /start, реферальная привязка, главное меню, инструкции
│   │       ├── trial.py       # Проверка подписки на канал и выдача 3-дневного триала
│   │       ├── profile.py     # Моя подписка, QR-код в памяти, Re-issue (раз в 24ч), рефералка, промокоды
│   │       ├── billing.py     # Выбор тарифа, списание баланса/скидки, инвойсы Stars/CryptoBot/Rub
│   │       └── admin.py       # /admin: дашборд + 3X-UI статус, юзеры, тарифы, промокоды, рассылка
│   ├── tasks/
│   │   └── scheduler.py       # APScheduler + pg_try_advisory_lock: уведомления (3d, 1d, expired) и retry выдачи
│   └── web/
│       └── app.py             # FastAPI: /health, /ping, /webhook/telegram, /webhooks/*, /connect/{app}/{sub_id}
├── docs/
│   ├── ADR.md
│   ├── spec.md
│   ├── plan.md
│   └── tasks.md
├── tests/
│   ├── conftest.py
│   ├── test_vpn_logic.py
│   ├── test_xui_client.py
│   ├── test_payments_and_webhooks.py
│   └── test_web_endpoints.py
├── main.py                    # Точка входа uvicorn
├── requirements.txt
├── render.yaml
├── .env.example
└── README.md
```

## 3. Constitution Check (Соответствие правилам)
- **Безопасность:** Все секреты в `.env`, вебхуки защищены криптографическими подписями (`hmac.compare_digest`), SQL-инъекции исключены (SQLAlchemy 2.0 ORM).
- **Корректность:** Обработка сетевых сбоев 3X-UI через статус `PAID_PENDING_PROVISION` (деньги клиента никогда не теряются).
- **Минимальность (Ponytail):** Никаких лишних брокеров (Celery/Redis не нужны на Render 512MB), QR генерируется на лету в `BytesIO`.
