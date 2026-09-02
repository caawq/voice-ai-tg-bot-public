"""
Кто прямо сейчас должен получить проактивную картинку недели — по локальному
времени пользователя, как и вечерний чек-ин (services/checkin.py, тот же приём).

Промпт 5 просил проактивную еженедельную отправку, но не назвал день/час —
это открытый вопрос, который я зафиксировал сам (см. WEEKLY_WEEKDAY/HOUR
ниже) и явно обозначил в сводке по шагу: понедельник, утро по местному
времени пользователя — начало недели, есть смысл увидеть картинку вперёд,
а не в конце уже прожитой недели.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User
from services import timeframe

WEEKLY_WEEKDAY = 0  # date.weekday(): понедельник
WEEKLY_HOUR = 8


def is_weekly_send_time(timezone: str, at_utc: dt.datetime) -> bool:
    """Наступил ли сейчас (at_utc) час еженедельной отправки в поясе ``timezone``."""
    local = timeframe.to_local(at_utc, timezone)
    return local.weekday() == WEEKLY_WEEKDAY and local.hour == WEEKLY_HOUR


async def due_users(session: AsyncSession, at_utc: dt.datetime) -> list[User]:
    """Пользователи, которым нужно прислать картинку недели прямо сейчас."""
    users = list((await session.scalars(select(User))).all())
    return [u for u in users if is_weekly_send_time(u.timezone, at_utc)]
