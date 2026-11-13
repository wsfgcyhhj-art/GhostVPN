from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    MediaCache,
    PaymentProviderType,
    Plan,
    PromoCode,
    PromoType,
    PromoUsage,
    Server,
    Subscription,
    Transaction,
    TransactionStatus,
    TransactionType,
    User,
    utcnow,
)


def detect_lang(language_code: str | None) -> str:
    if not language_code:
        return "ru"
    code = language_code.lower()
    if code.startswith(("ru", "uk", "be", "kk", "uz")):
        return "ru"
    return "en"


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
        language_code: str | None = None,
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
            language=detect_lang(language_code),
            referrer_id=referrer_db_id,
            balance_rub=Decimal("0.00"),
            auto_renew=True,
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

    async def get_user_transactions(self, user_id: int, limit: int = 10) -> Sequence[Transaction]:
        res = await self.session.execute(
            select(Transaction)
            .where(Transaction.user_id == user_id)
            .order_by(Transaction.id.desc())
            .limit(limit)
        )
        return res.scalars().all()

    async def get_user_promo_usages(self, user_id: int) -> Sequence[PromoUsage]:
        res = await self.session.execute(
            select(PromoUsage)
            .where(PromoUsage.user_id == user_id)
            .order_by(PromoUsage.id.desc())
        )
        return res.scalars().all()

    async def count_referrals(self, user_id: int) -> int:
        res = await self.session.execute(
            select(func.count(User.id)).where(User.referrer_id == user_id)
        )
        return int(res.scalar() or 0)

    async def has_paid_transactions(self, user_id: int) -> bool:
        res = await self.session.execute(
            select(func.count(Transaction.id)).where(
                Transaction.user_id == user_id,
                Transaction.tx_type == TransactionType.SUBSCRIPTION,
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

    async def count_recent_pending_transactions(
        self, user_id: int, minutes: int = 10
    ) -> int:
        since = utcnow() - timedelta(minutes=minutes)
        res = await self.session.execute(
            select(func.count(Transaction.id)).where(
                Transaction.user_id == user_id,
                Transaction.status == TransactionStatus.PENDING,
                Transaction.created_at >= since,
            )
        )
        return int(res.scalar() or 0)

    async def get_cached_banner_file_id(
        self, banner_key: str, lang: str, expected_hash: str
    ) -> str | None:
        res = await self.session.execute(
            select(MediaCache).where(
                MediaCache.banner_key == banner_key,
                MediaCache.lang == lang,
            )
        )
        entry = res.scalar_one_or_none()
        if entry and entry.file_hash == expected_hash:
            return entry.telegram_file_id
        return None

    async def save_cached_banner_file_id(
        self, banner_key: str, lang: str, file_hash: str, telegram_file_id: str
    ) -> None:
        res = await self.session.execute(
            select(MediaCache).where(
                MediaCache.banner_key == banner_key,
                MediaCache.lang == lang,
            )
        )
        entry = res.scalar_one_or_none()
        if entry is None:
            entry = MediaCache(
                banner_key=banner_key,
                lang=lang,
                file_hash=file_hash,
                telegram_file_id=telegram_file_id,
            )
            self.session.add(entry)
        else:
            entry.file_hash = file_hash
            entry.telegram_file_id = telegram_file_id
        await self.session.commit()

    async def get_all_active_subscriptions(self) -> Sequence[Subscription]:
        now = utcnow()
        res = await self.session.execute(
            select(Subscription).where(
                Subscription.is_active.is_(True),
                Subscription.expires_at > now,
            )
        )
        return res.scalars().all()

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

    # --- Admin Dashboard Stats (Multi-currency & Trial Conversion) ---
    async def get_dashboard_stats(self) -> dict[str, Any]:
        now = utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        days7_start = now - timedelta(days=7)
        month_start = now - timedelta(days=30)

        total_users = int(
            (await self.session.execute(select(func.count(User.id)))).scalar() or 0
        )
        new_users_today = int(
            (
                await self.session.execute(
                    select(func.count(User.id)).where(User.created_at >= today_start)
                )
            ).scalar()
            or 0
        )
        new_users_7d = int(
            (
                await self.session.execute(
                    select(func.count(User.id)).where(User.created_at >= days7_start)
                )
            ).scalar()
            or 0
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

        # Конверсия из триала за 1 ₽ в полную покупку
        total_trial_users = int(
            (
                await self.session.execute(
                    select(func.count(User.id)).where(User.has_used_trial.is_(True))
                )
            ).scalar()
            or 0
        )
        paid_statuses = [
            TransactionStatus.COMPLETED,
            TransactionStatus.PAID_PENDING_PROVISION,
        ]
        converted_from_trial = int(
            (
                await self.session.execute(
                    select(func.count(func.distinct(Transaction.user_id)))
                    .join(User, User.id == Transaction.user_id)
                    .where(
                        User.has_used_trial.is_(True),
                        Transaction.tx_type == TransactionType.SUBSCRIPTION,
                        Transaction.status.in_(paid_statuses),
                    )
                )
            ).scalar()
            or 0
        )
        conversion_rate = (
            round((converted_from_trial / total_trial_users) * 100, 1)
            if total_trial_users > 0
            else 0.0
        )

        async def sum_rub_since(dt_from: Any = None, provider: PaymentProviderType | None = None) -> Decimal:
            conds = [Transaction.status.in_(paid_statuses)]
            if dt_from is not None:
                conds.append(Transaction.paid_at >= dt_from)
            if provider is not None:
                conds.append(Transaction.provider == provider)
            val = (
                await self.session.execute(
                    select(func.coalesce(func.sum(Transaction.amount_rub), 0)).where(*conds)
                )
            ).scalar() or Decimal("0.00")
            return Decimal(str(val)).quantize(Decimal("0.01"))

        rev_today = await sum_rub_since(today_start)
        rev_7d = await sum_rub_since(days7_start)
        rev_month = await sum_rub_since(month_start)
        rev_total = await sum_rub_since(None)

        rev_rub_gw = await sum_rub_since(month_start, PaymentProviderType.RUB_GATEWAY)
        rev_crypto_rub = await sum_rub_since(month_start, PaymentProviderType.CRYPTOBOT)
        rev_stars_rub = await sum_rub_since(month_start, PaymentProviderType.STARS)

        return {
            "total_users": total_users,
            "new_users_today": new_users_today,
            "new_users_7d": new_users_7d,
            "active_subs": active_subs,
            "trial_subs": trial_subs,
            "total_trial_users": total_trial_users,
            "converted_from_trial": converted_from_trial,
            "conversion_rate": conversion_rate,
            "rev_today": rev_today,
            "rev_7d": rev_7d,
            "rev_month": rev_month,
            "rev_total": rev_total,
            "rev_rub_gw_30d": rev_rub_gw,
            "rev_crypto_rub_30d": rev_crypto_rub,
            "rev_stars_rub_30d": rev_stars_rub,
        }
