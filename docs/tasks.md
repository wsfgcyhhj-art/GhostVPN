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
