from __future__ import annotations

from datetime import datetime, timedelta, timezone

MSK_TZ = timezone(timedelta(hours=3))


def format_dt_msk(dt: datetime) -> str:
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    msk = aware.astimezone(MSK_TZ)
    return msk.strftime("%d.%m.%Y %H:%M МСК")
