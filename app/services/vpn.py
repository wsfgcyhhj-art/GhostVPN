from __future__ import annotations

import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger, send_admin_alert
from app.db.models import (
    Server,
    Subscription,
    Transaction,
    TransactionStatus,
    User,
    utcnow,
)
from app.services.xui.client import XUIClient, XUIError

if TYPE_CHECKING:
    from aiogram import Bot


ALPHABET = string.ascii_lowercase + string.digits


def generate_sub_id(length: int = 16) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def calculate_new_expiry(
    current_expiry: datetime | None, days_to_add: int, now: datetime | None = None
) -> datetime:
    """
    Если подписка активна (current_expiry > now), дни прибавляются к current_expiry.
    Если истекла или отсутствует — дни отсчитываются от текущего момента (now).
    Поддерживает и положительное, и отрицательное количество дней (для админ-коррекции).
    """
    ref_now = now or utcnow()
    if current_expiry is not None:
        exp = (
            current_expiry
            if current_expiry.tzinfo
            else current_expiry.replace(tzinfo=timezone.utc)
        )
        base = exp if exp > ref_now else ref_now
    else:
        base = ref_now
    return base + timedelta(days=days_to_add)


def datetime_to_ms(dt: datetime) -> int:
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return int(aware.timestamp() * 1000)


class VPNService:
    """Оркестратор выдачи, продления, сброса и деактивации подписок в БД и 3X-UI."""

    def __init__(
        self, session: AsyncSession, xui_client_override: XUIClient | None = None
    ) -> None:
        self.session = session
        self._xui_override = xui_client_override

    async def _get_server(self) -> Server:
        res = await self.session.execute(
            select(Server).where(Server.is_active.is_(True)).order_by(Server.id.asc()).limit(1)
        )
        server = res.scalar_one_or_none()
        if server is None:
            raise RuntimeError("В базе данных нет ни одного активного сервера 3X-UI.")
        return server

    def _make_xui_client(self, server: Server) -> XUIClient:
        if self._xui_override is not None:
            return self._xui_override
        return XUIClient.from_server(server)

    async def grant_or_extend_subscription(
        self,
        user: User,
        days: int,
        *,
        limit_ip: int | None = None,
        is_trial: bool = False,
    ) -> Subscription:
        """
        Создаёт клиента в 3X-UI при первой выдаче или обновляет существующего при продлении,
        сохраняя его персональный `subId` и `Subscription URL`.
        """
        now = utcnow()
        eff_limit_ip = limit_ip or settings.DEFAULT_LIMIT_IP

        res = await self.session.execute(
            select(Subscription).where(Subscription.user_id == user.id)
        )
        sub = res.scalar_one_or_none()

        if sub is None:
            server = await self._get_server()
            new_uuid = str(uuid.uuid4())
            new_sub_id = generate_sub_id(16)
            email = f"tg_{user.telegram_id}"
            new_expires_at = calculate_new_expiry(None, days, now=now)
            expiry_ms = datetime_to_ms(new_expires_at)

            client = self._make_xui_client(server)
            try:
                await client.add_client(
                    inbound_id=server.inbound_id,
                    client_uuid=new_uuid,
                    email=email,
                    sub_id=new_sub_id,
                    expiry_time_ms=expiry_ms,
                    limit_ip=eff_limit_ip,
                    total_gb=0,
                    enable=True,
                    tg_id=str(user.telegram_id),
                )
            finally:
                if self._xui_override is None:
                    await client.close()

            sub = Subscription(
                user_id=user.id,
                server_id=server.id,
                xui_client_uuid=new_uuid,
                xui_email=email,
                sub_id=new_sub_id,
                limit_ip=eff_limit_ip,
                expires_at=new_expires_at,
                is_trial=is_trial,
                is_active=True,
                notified_3d=False,
                notified_1d=False,
                notified_expired=False,
            )
            self.session.add(sub)
            if is_trial:
                user.has_used_trial = True
            await self.session.commit()
            await self.session.refresh(sub)
            return sub

        # Продление существующей подписки (сохраняем старый subId!)
        server = sub.server or await self._get_server()
        new_expires_at = calculate_new_expiry(sub.expires_at, days, now=now)
        is_still_active = new_expires_at > now
        expiry_ms = datetime_to_ms(new_expires_at)

        client = self._make_xui_client(server)
        try:
            await client.update_client(
                inbound_id=server.inbound_id,
                client_uuid=sub.xui_client_uuid,
                email=sub.xui_email,
                sub_id=sub.sub_id,
                expiry_time_ms=expiry_ms,
                limit_ip=eff_limit_ip,
                total_gb=0,
                enable=is_still_active,
                tg_id=str(user.telegram_id),
            )
        finally:
            if self._xui_override is None:
                await client.close()

        sub.expires_at = new_expires_at
        sub.limit_ip = eff_limit_ip
        sub.is_active = is_still_active
        if not is_trial:
            sub.is_trial = False
        if is_still_active:
            sub.notified_3d = False
            sub.notified_1d = False
            sub.notified_expired = False

        if is_trial:
            user.has_used_trial = True

        await self.session.commit()
        await self.session.refresh(sub)
        return sub

    async def reissue_key(self, user: User, *, force_by_admin: bool = False) -> Subscription:
        """
        Перегенерирует UUID и subId в 3X-UI с сохранением текущего срока действия `expires_at`.
        Обычному пользователю доступно не чаще 1 раза в 24 часа.
        """
        res = await self.session.execute(
            select(Subscription).where(Subscription.user_id == user.id)
        )
        sub = res.scalar_one_or_none()
        if sub is None:
            raise ValueError("У вас ещё нет подписки для сброса ключа.")

        now = utcnow()
        if not force_by_admin and sub.last_key_reset_at is not None:
            last_reset = (
                sub.last_key_reset_at
                if sub.last_key_reset_at.tzinfo
                else sub.last_key_reset_at.replace(tzinfo=timezone.utc)
            )
            cooldown = timedelta(hours=settings.KEY_RESET_COOLDOWN_HOURS)
            if now - last_reset < cooldown:
                remaining = cooldown - (now - last_reset)
                hours_left = max(1, int(remaining.total_seconds() // 3600))
                raise ValueError(
                    f"Сброс ключа доступен не чаще 1 раза в {settings.KEY_RESET_COOLDOWN_HOURS} ч. "
                    f"Попробуйте снова примерно через {hours_left} ч."
                )

        server = sub.server or await self._get_server()
        old_uuid = sub.xui_client_uuid
        new_uuid = str(uuid.uuid4())
        new_sub_id = generate_sub_id(16)
        expiry_ms = datetime_to_ms(sub.expires_at)
        enable_flag = sub.is_currently_valid

        client = self._make_xui_client(server)
        try:
            # Удаляем старый UUID и создаём новый с тем же email и expiryTime
            try:
                await client.delete_client_by_uuid(
                    inbound_id=server.inbound_id, client_uuid=old_uuid
                )
            except XUIError as exc:
                logger.warning("Старый клиент %s не удалён (возможно уже отсутствует): %s", old_uuid, exc)

            await client.add_client(
                inbound_id=server.inbound_id,
                client_uuid=new_uuid,
                email=sub.xui_email,
                sub_id=new_sub_id,
                expiry_time_ms=expiry_ms,
                limit_ip=sub.limit_ip,
                total_gb=0,
                enable=enable_flag,
                tg_id=str(user.telegram_id),
            )
        finally:
            if self._xui_override is None:
                await client.close()

        sub.xui_client_uuid = new_uuid
        sub.sub_id = new_sub_id
        sub.last_key_reset_at = now
        await self.session.commit()
        await self.session.refresh(sub)
        return sub

    async def set_client_enabled(self, sub: Subscription, enabled: bool) -> None:
        server = sub.server or await self._get_server()
        user_res = await self.session.execute(select(User).where(User.id == sub.user_id))
        user = user_res.scalar_one()

        client = self._make_xui_client(server)
        try:
            await client.update_client(
                inbound_id=server.inbound_id,
                client_uuid=sub.xui_client_uuid,
                email=sub.xui_email,
                sub_id=sub.sub_id,
                expiry_time_ms=datetime_to_ms(sub.expires_at),
                limit_ip=sub.limit_ip,
                total_gb=0,
                enable=enabled,
                tg_id=str(user.telegram_id),
            )
        finally:
            if self._xui_override is None:
                await client.close()

        sub.is_active = enabled
        await self.session.commit()

    async def process_successful_payment(
        self,
        tx: Transaction,
        bot: "Bot | None" = None,
    ) -> tuple[bool, Subscription | None]:
        """
        Идемпотентная обработка успешного платежа:
        1) Если уже COMPLETED — ничего не делает.
        2) Если PENDING — списывает использованный внутренний баланс, обнуляет одноразовую скидку,
           начисляет 15% рефереру и переводит транзакцию в PAID_PENDING_PROVISION.
        3) Вызывает 3X-UI для выдачи/продления подписки.
           - Если 3X-UI ответил успешно -> статус COMPLETED.
           - Если 3X-UI недоступен -> остаётся PAID_PENDING_PROVISION (фоновый воркер докачает).
        """
        if tx.status == TransactionStatus.COMPLETED:
            res = await self.session.execute(
                select(Subscription).where(Subscription.user_id == tx.user_id)
            )
            return True, res.scalar_one_or_none()

        user = tx.user or await self.session.get(User, tx.user_id)
        plan = tx.plan or await self.session.get(
            type(tx.plan) if tx.plan else __import__("app.db.models", fromlist=["Plan"]).Plan,
            tx.plan_id,
        )
        if user is None or plan is None:
            raise RuntimeError(f"Транзакция #{tx.id} ссылается на несуществующего юзера или тариф")

        first_time_paid = tx.status == TransactionStatus.PENDING
        referrer_bonus = Decimal("0.00")
        referrer_user: User | None = None

        if first_time_paid:
            tx.status = TransactionStatus.PAID_PENDING_PROVISION
            tx.paid_at = utcnow()

            if tx.balance_used_rub > Decimal("0.00"):
                user.balance_rub = max(
                    Decimal("0.00"), user.balance_rub - tx.balance_used_rub
                )

            if tx.discount_percent > 0:
                user.pending_discount_percent = 0

            # Реферальное начисление 15% от фактически оплаченной суммы (или полной стоимости покупки)
            total_paid_value = tx.amount_rub + tx.balance_used_rub
            if user.referrer_id and total_paid_value > Decimal("0.00"):
                referrer_user = await self.session.get(User, user.referrer_id)
                if referrer_user is not None:
                    referrer_bonus = (
                        total_paid_value * Decimal(settings.REFERRAL_PERCENT) / Decimal(100)
                    ).quantize(Decimal("0.01"))
                    if referrer_bonus > Decimal("0.00"):
                        referrer_user.balance_rub += referrer_bonus

            await self.session.commit()

            # Уведомление рефереру
            if bot and referrer_user and referrer_bonus > Decimal("0.00"):
                try:
                    await bot.send_message(
                        chat_id=referrer_user.telegram_id,
                        text=(
                            f"🎉 <b>Реферальный бонус!</b>\n\n"
                            f"Ваш друг оплатил подписку. Вам начислено <b>+{referrer_bonus} ₽</b> "
                            f"({settings.REFERRAL_PERCENT}%) на внутренний баланс!\n"
                            f"💰 Текущий баланс: <b>{referrer_user.balance_rub} ₽</b>"
                        ),
                        parse_mode="HTML",
                    )
                except Exception as exc:
                    logger.warning("Не удалось уведомить реферера %s: %s", referrer_user.telegram_id, exc)

            # Алерт админу о новой оплате
            await send_admin_alert(
                bot,
                (
                    f"💰 <b>Новая оплата #{tx.id}</b>\n"
                    f"👤 Пользователь: <code>{user.telegram_id}</code> (@{user.username or 'нет'})\n"
                    f"📦 Тариф: <b>{plan.name}</b> ({plan.days} дн.)\n"
                    f"💳 Метод: <b>{tx.provider.value}</b>\n"
                    f"💵 Оплачено: <b>{tx.amount_rub} ₽</b> (с баланса: {tx.balance_used_rub} ₽)"
                ),
            )

        # Фаза 2: Выдача / продление в 3X-UI
        try:
            sub = await self.grant_or_extend_subscription(
                user=user,
                days=plan.days,
                limit_ip=plan.limit_ip,
                is_trial=False,
            )
            tx.status = TransactionStatus.COMPLETED
            await self.session.commit()
            return True, sub
        except Exception as exc:
            logger.error(
                "Сбой выдачи 3X-UI для оплаченной транзакции #%s (оставлена в PAID_PENDING_PROVISION): %s",
                tx.id,
                exc,
            )
            await send_admin_alert(
                bot,
                (
                    f"⚠️ <b>Задержка выдачи ключа (PAID_PENDING_PROVISION)</b>\n"
                    f"Транзакция #{tx.id} для <code>{user.telegram_id}</code> оплачена, "
                    f"но панель 3X-UI вернула ошибку:\n<code>{exc}</code>\n"
                    f"Фоновый воркер автоматически повторит выдачу через 30 сек."
                ),
                critical=True,
            )
            return False, None
