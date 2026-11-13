# Задачи: GhostVPN (tasks.md)

- [x] **T-01:** Документация и архитектурные решения (`docs/spec.md`, `docs/plan.md`, `docs/ADR.md`).
- [x] **T-02:** Конфигурация, логирование и безопасность (`app/core/config.py`, `app/core/logging.py`, `app/core/security.py`).
- [x] **T-03:** Слой базы данных, модели SQLAlchemy 2.0 и миграции Alembic (`app/db/*`, `alembic/*`).
- [x] **T-04:** Клиент API панели 3X-UI v3.11.0 и оркестратор VPN-подписок (`app/services/xui/client.py`, `app/services/vpn.py`).
- [x] **T-05:** Платёжные провайдеры Strategy (`Stars`, `CryptoBot`, `RubGatewayStub`) (`app/services/payments/*`).
- [x] **T-06:** Telegram-бот (`aiogram 3.x`): клавиатуры, мидлвари, хендлеры пользователя и админ-панель (`app/bot/*`).
- [x] **T-07:** Фоновый планировщик с PostgreSQL Advisory Locks (`app/tasks/scheduler.py`).
- [x] **T-08:** Веб-сервер FastAPI (вебхуки, `/health`, `/ping`, deep-link редиректы `/connect/{app}/{sub_id}`) и точка входа (`app/web/app.py`, `main.py`).
- [x] **T-09:** Конфигурация деплоя (`requirements.txt`, `render.yaml`, `.env.example`, `README.md`).
- [x] **T-10:** Написание и прогон автотестов (`tests/*`) + финальный аудит.
- [x] **T-11:** Модели БД v2.0, конфиг и миграция Alembic (`0002_v2_upgrade.py`).
- [x] **T-12:** Локализация (`app/core/i18n.py`) и брендированная PNG QR-карточка (`app/services/qr_card.py`).
- [x] **T-13:** Расширение `XUIClient` (онлайн, трафик, сохранённый CSRF) и `VPNService` (слоты устройств, TLS Fragment, IP-контроль).
- [x] **T-14:** Платёжный модуль `PlategaProvider` (СБП / карты РФ без ИП), триал за 1 ₽ / 10 Stars.
- [x] **T-15:** Обновление бота: Rate-Limit middleware, ЛК с прогресс-баром, FAQ, расширенная админка (CRM, компенсация, кружочки/видео в рассылке).
- [x] **T-16:** Фоновые воркеры v2.0 (автопродление за 12ч, онбординг 6ч, win-back 3д, синк 15м, очистка >7д, ежедневный бэкап БД).
- [x] **T-17:** Встроенный Telegram Mini App (`/webapp` + `HMAC-SHA256 initData` валидация).
- [x] **T-18:** Тестирование всех новых модулей (`pytest`) и деплой в GitHub.
