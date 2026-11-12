from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Plan,
    PromoCode,
    PromoType,
    PromoUsage,
    Server,
    Subscription,
    Transaction,
    TransactionStatus,
    User,
    utcnow,
)


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Users ---
    async def get_or_create_user(
        self,
        telegram_id: int,
        username: str | None = None,
        full_name: str | None = None,
        referrer_telegram_id: int | None = None,
    ) -> tuple[User, bool]:
        res = await self.session.execute(select(User).where(User.telegram_id == telegram_id))
        user = res.scalar_one_or_none()
        if user is not None:
            changed = False
            if username and user.username != username:
                user.username = username
                changed = True
            if full_name and user.full_name != full_name:
                user.full_name = full_name
                changed = True
            if changed:
                await self.session.commit()
            return user, False

        referrer_db_id: int | None = None
        if referrer_telegram_id and referrer_telegram_id != telegram_id:
            ref_res = await self.session.execute(
                select(User).where(User.telegram_id == referrer_telegram_id)
            )
            ref_user = ref_res.scalar_one_or_none()
            if ref_user:
                referrer_db_id = ref_user.id

        user = User(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
            referrer_id=referrer_db_id,
            balance_rub=Decimal("0.00"),
        )
        self.session.add(user)
        await self.session.commit()
        await self.session.refresh(user)
        return user, True

    async def get_user_by_tg_id(self, telegram_id: int) -> User | None:
        res = await self.session.execute(select(User).where(User.telegram_id == telegram_id))
        return res.scalar_one_or_none()

    async def get_user_by_id(self, user_id: int) -> User | None:
        res = await self.session.execute(select(User).where(User.id == user_id))
        return res.scalar_one_or_none()

    async def find_user_by_query(self, query: str) -> User | None:
        q = query.strip()
        if q.startswith("@"):
            q = q[1:]

        if q.isdigit():
            res = await self.session.execute(select(User).where(User.telegram_id == int(q)))
            user = res.scalar_one_or_none()
            if user:
                return user

        res = await self.session.execute(
            select(User)
            .outerjoin(Subscription, Subscription.user_id == User.id)
            .where(
                or_(
                    func.lower(User.username) == q.lower(),
                    Subscription.sub_id == q,
                    Subscription.xui_email == q,
                )
            )
        )
        return res.scalar_one_or_none()

    async def count_referrals(self, user_id: int) -> int:
        res = await self.session.execute(
            select(func.count(User.id)).where(User.referrer_id == user_id)
        )
        return int(res.scalar() or 0)

    async def has_paid_transactions(self, user_id: int) -> bool:
        res = await self.session.execute(
            select(func.count(Transaction.id)).where(
                Transaction.user_id == user_id,
                Transaction.status.in_(
                    [TransactionStatus.COMPLETED, TransactionStatus.PAID_PENDING_PROVISION]
                ),
            )
        )
        return int(res.scalar() or 0) > 0

    # --- Servers & Plans ---
    async def get_active_server(self) -> Server | None:
        res = await self.session.execute(
            select(Server).where(Server.is_active.is_(True)).order_by(Server.id.asc()).limit(1)
        )
        return res.scalar_one_or_none()

    async def get_active_plans(self) -> Sequence[Plan]:
        res = await self.session.execute(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order.asc(), Plan.id.asc())
        )
        return res.scalars().all()

    async def get_all_plans(self) -> Sequence[Plan]:
        res = await self.session.execute(select(Plan).order_by(Plan.sort_order.asc(), Plan.id.asc()))
        return res.scalars().all()

    async def get_plan_by_id(self, plan_id: int) -> Plan | None:
        res = await self.session.execute(select(Plan).where(Plan.id == plan_id))
        return res.scalar_one_or_none()

    # --- Subscriptions ---
    async def get_subscription_by_sub_id(self, sub_id: str) -> Subscription | None:
        res = await self.session.execute(
            select(Subscription).where(Subscription.sub_id == sub_id)
        )
        return res.scalar_one_or_none()

    # --- Promocodes ---
    async def validate_and_redeem_promo(
        self, user: User, raw_code: str
    ) -> tuple[PromoCode, str]:
        """
        Проверяет промокод и фиксирует использование.
        Если тип PERCENT — устанавливает user.pending_discount_percent.
        Если тип DAYS — вызывающий код начислит дни через VPNService.
        """
        code = raw_code.strip().upper()
        res = await self.session.execute(
            select(PromoCode).where(func.upper(PromoCode.code) == code, PromoCode.is_active.is_(True))
        )
        promo = res.scalar_one_or_none()
        if promo is None:
            raise ValueError("Промокод не найден или неактивен.")

        now = utcnow()
        if promo.expires_at:
            exp = (
                promo.expires_at
                if promo.expires_at.tzinfo
                else promo.expires_at.replace(tzinfo=timezone.utc)
            )
            if exp < now:
                raise ValueError("Срок действия этого промокода истёк.")

        if promo.used_count >= promo.max_uses:
            raise ValueError("Лимит активаций этого промокода исчерпан.")

        usage_res = await self.session.execute(
            select(PromoUsage).where(
                PromoUsage.user_id == user.id, PromoUsage.promocode_id == promo.id
            )
        )
        if usage_res.scalar_one_or_none() is not None:
            raise ValueError("Вы уже активировали этот промокод ранее.")

        if promo.only_new_users and await self.has_paid_transactions(user.id):
            raise ValueError("Этот промокод доступен только для новых пользователей без покупок.")

        promo.used_count += 1
        self.session.add(PromoUsage(user_id=user.id, promocode_id=promo.id))

        if promo.type == PromoType.PERCENT:
            user.pending_discount_percent = max(user.pending_discount_percent, promo.value)
            await self.session.commit()
            return promo, f"Скидка {promo.value}% активирована и применится при следующей покупке!"

        await self.session.commit()
        return promo, f"Начислено +{promo.value} бонусных дней к подписке!"

    # --- Admin Dashboard Stats ---
    async def get_dashboard_stats(self) -> dict[str, int | Decimal]:
        now = utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now - timedelta(days=30)

        total_users = int(
            (await self.session.execute(select(func.count(User.id)))).scalar() or 0
        )
        active_subs = int(
            (
                await self.session.execute(
                    select(func.count(Subscription.id)).where(
                        Subscription.is_active.is_(True),
                        Subscription.expires_at > now,
                        Subscription.is_trial.is_(False),
                    )
                )
            ).scalar()
            or 0
        )
        trial_subs = int(
            (
                await self.session.execute(
                    select(func.count(Subscription.id)).where(
                        Subscription.is_active.is_(True),
                        Subscription.expires_at > now,
                        Subscription.is_trial.is_(True),
                    )
                )
            ).scalar()
            or 0
        )

        paid_statuses = [
            TransactionStatus.COMPLETED,
            TransactionStatus.PAID_PENDING_PROVISION,
        ]

        rev_today = (
            await self.session.execute(
                select(func.coalesce(func.sum(Transaction.amount_rub), 0)).where(
                    Transaction.status.in_(paid_statuses),
                    Transaction.paid_at >= today_start,
                )
            )
        ).scalar() or Decimal("0.00")

        rev_month = (
            await self.session.execute(
                select(func.coalesce(func.sum(Transaction.amount_rub), 0)).where(
                    Transaction.status.in_(paid_statuses),
                    Transaction.paid_at >= month_start,
                )
            )
        ).scalar() or Decimal("0.00")

        rev_total = (
            await self.session.execute(
                select(func.coalesce(func.sum(Transaction.amount_rub), 0)).where(
                    Transaction.status.in_(paid_statuses)
                )
            )
        ).scalar() or Decimal("0.00")

        return {
            "total_users": total_users,
            "active_subs": active_subs,
            "trial_subs": trial_subs,
            "rev_today": Decimal(str(rev_today)),
            "rev_month": Decimal(str(rev_month)),
            "rev_total": Decimal(str(rev_total)),
        }
