# 👻 GhostVPN — Production-Ready Telegram Bot для продажи VPN (3X-UI v3.11.0)

Автоматизированный коммерческий Telegram-бот для продажи, выдачи и управления VPN-подписками на базе **Xray-core (VLESS + TCP + XTLS-RPRX-Vision + Reality)** и панели **3X-UI v3.11.0**.

Спроектирован специально под бесплатный хостинг **Render Free (Web Service, 512 MB RAM)** без Docker и внешнюю облачную **PostgreSQL (Neon / Supabase)** с полной защитой от засыпания, потери платежей и дублирования крон-задач.

---

## ✨ Ключевые возможности

1. **Интеграция с 3X-UI v3.11.0 по HTTPS:**
   - Модель `1 TG-аккаунт = 1 клиент в 3X-UI` (`email: tg_{telegram_id}`, `flow: xtls-rprx-vision`, `limitIp: 3`, `totalGB: 0`).
   - При продлении подписки (даже после истечения) обновляется существующий клиент — **старая `Subscription URL` продолжает работать без переустановки у пользователя**.
   - Перевыпуск ключа (`🔄 Сбросить ключ`) в личном кабинете с кулдауном 24 часа и сохранением оплаченного срока.
2. **Быстрое подключение в 1 клик (Deep Links):**
   - Встроенный веб-эндпоинт `GET /connect/{app}/{sub_id}` для автоматического импорта подписки в **Happ**, **v2rayTun** и **Hiddify** + генерация QR-кода в оперативной памяти (`BytesIO`).
3. **Отказоустойчивый биллинг (`PAID_PENDING_PROVISION`):**
   - Поддержка **Telegram Stars (`XTR`)**, **CryptoBot (`@send`)** (с проверкой `HMAC-SHA256`), **внутреннего баланса** и готового шлюза под **Карты РФ / СБП**.
   - Если в момент оплаты панель 3X-UI недоступна, платёж сохраняется в статусе `PAID_PENDING_PROVISION`, а фоновый воркер автоматически докачивает ключ каждые 30 секунд.
4. **Маркетинг и удержание:**
   - Бесплатный пробный период (3 дня) с обязательной проверкой подписки на ваш Telegram-канал.
   - Реферальная программа (**15%** от оплат приглашённых друзей на баланс бота).
   - Промокоды двух типов: `DAYS` (+дни к подписке) и `PERCENT` (скидка `%` на покупку) с фильтром «только для новых пользователей».
   - Авто-уведомления за **3 дня**, **24 часа** и **в момент истечения** подписки (защищены от дублей через `pg_try_advisory_lock`).
5. **Полноценная Админ-панель (`/admin`):**
   - Дашборд выручки (сегодня / 30 дней / всё время), статистика аудитории и live-метрики сервера 3X-UI (CPU, RAM, статус Xray).
   - Поиск юзера по `telegram_id`, `@username` или `sub_id`, начисление/списание дней и баланса, сброс ключа, бан/разбан.
   - Редактирование цен тарифов на лету, создание промокодов и сегментированная рассылка (Broadcast) с rate-limit 20 сообщ./сек.

---

## 🚀 Пошаговая инструкция развёртывания

### Шаг 1. Создание облачной базы данных PostgreSQL (Neon или Supabase)
1. Зарегистрируйтесь на [Neon.tech](https://neon.tech/) (или [Supabase.com](https://supabase.com/)).
2. Создайте новый проект (регион `Frankfurt / eu-central-1` — ближе всего к вашему VPS в Нидерландах и к Render Frankfurt).
3. Скопируйте строку подключения **Connection String (Pooled)**:
   ```text
   postgresql+asyncpg://user:password@ep-xxx-pooler.eu-central-1.aws.neon.tech/neondb?ssl=require
   ```
   *(Пул соединений в боте уже настроен с `statement_cache_size=0` и `pool_pre_ping=True` для полной совместимости с PgBouncer/Supavisor).*

### Шаг 2. Настройка CryptoBot (опционально, для приёма крипты)
1. Откройте `@send` (`@CryptoBot`) в Telegram $\rightarrow$ **Crypto Pay** $\rightarrow$ **Create App**.
2. Скопируйте `API Token` в переменную `CRYPTOBOT_API_TOKEN`.
3. В настройках приложения в `@send` включите **Webhooks** и укажите адрес:
   ```text
   https://<ваш-домен-на-render>.onrender.com/webhooks/cryptobot
   ```

### Шаг 3. Деплой на Render Free (без Docker)
1. Загрузите этот репозиторий на свой GitHub/GitLab.
2. В панели [Render.com](https://dashboard.render.com/) нажмите **New +** $\rightarrow$ **Blueprint** (для автоматического чтения `render.yaml`) или **Web Service**.
3. Выберите:
   - **Runtime:** `Python 3`
   - **Region:** `Frankfurt (EU Central)`
   - **Instance Type:** `Free` (512 MB RAM)
   - **Build Command:**
     ```bash
     pip install --upgrade pip && pip install -r requirements.txt && alembic upgrade head
     ```
   - **Start Command:**
     ```bash
     uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1
     ```
4. Заполните переменные окружения (**Environment Variables**) по образцу из `.env.example`.
5. После старта сервиса бот автоматически:
   - Создаст таблицы и выполнит seed стартового сервера и тарифов (`199 / 499 / 899 / 1590 ₽`);
   - Зарегистрирует защищённый Telegram Webhook `https://<ваш-сервис>.onrender.com/webhook/telegram`.

### Шаг 4. Настройка внешнего пингера (Анти-сон 24/7)
Чтобы бесплатный сервис на Render не засыпал каждые 15 минут:
1. Зарегистрируйтесь на [cron-job.org](https://cron-job.org/) или [UptimeRobot](https://uptimerobot.com/).
2. Создайте HTTP(S)-монитор на URL:
   ```text
   https://<ваш-домен-на-render>.onrender.com/ping
   ```
3. Установите интервал проверки: **каждые 10 минут**.

---

## 🧪 Локальный запуск и тестирование

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Запуск полного набора тестов (pytest + pytest-asyncio)
pytest -v
```
